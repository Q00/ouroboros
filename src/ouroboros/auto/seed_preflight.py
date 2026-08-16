"""Deterministic executability preflight for a generated Seed.

An LLM Seed QA judge grades whether the contract *reads* well; it cannot
notice that a verify script does not exist on disk, that an environment
variable in a ``verify_command`` has no binding, or that a brownfield
context reference is a concept ("Obsidian Vault") rather than a real path.
Those are exactly the fabrications that survive prompt-level review and
kill the run later.

This module checks the Seed's own claims against the filesystem. It never
rewrites the Seed: every finding carries the open question a human (or the
interview ledger) must answer before the contract is executable.

Blocking findings are fabricated or unbound *claims*:

* a ``brownfield_context.context_references`` path that does not resolve;
* an ``existing_dependencies`` entry naming a workspace file that does not
  exist even though the Seed claims it pre-exists;
* an environment variable in a ``verify_command`` that nothing binds.

Advisory findings are open questions that do not prove fabrication: an AC
without any success contract, a verify-command file reference that is
neither claimed nor present yet, or two ACs sharing one verify command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

__all__ = [
    "PreflightFinding",
    "SeedPreflightReport",
    "run_seed_preflight",
]

# Variables any POSIX run host binds without the Seed's help.
_HOST_BOUND_ENV_VARS = frozenset({"HOME", "PATH", "PWD", "SHELL", "TMPDIR", "USER"})
_ENV_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# Variables the command binds itself: shell assignments (``tmp=$(mktemp)``),
# ``for x in ...`` loop variables, and ``${VAR:-default}`` fallbacks.
_ENV_ASSIGN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=")
_FOR_VAR_RE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_DEFAULTED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?-")
_URL_RE = re.compile(r"\S+://\S+")
# A workspace-file token inside a command: at least one directory separator
# and a short file extension, e.g. ``scripts/verify.py`` or ``./bin/run.sh``.
_FILE_TOKEN_RE = re.compile(
    r"(?:(?:\.\.?/)+|/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}\b"
    r"|(?:(?:\.\.?/)+|/)(?:[\w.-]+/)*[\w.-]+\b"
    r"|(?:\./)[\w.-]+\b"
    r"|(?<![\w./-])[\w-]+\.[A-Za-z0-9]{1,5}\b"
)


def _shell_variable_events(command: str) -> tuple[tuple[int, str, str], ...]:
    """Return ordered shell assignment/expansion events outside literal quoting."""
    expandable = [True] * len(command)
    unquoted = [True] * len(command)
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            expandable[index] = False
            escaped = False
            continue
        if character == "\\" and quote != "'":
            expandable[index] = False
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            expandable[index] = False
            unquoted[index] = False
            continue
        if quote == "'":
            expandable[index] = False
        if quote is not None:
            unquoted[index] = False

    events: list[tuple[int, str, str]] = []
    for match in _ENV_ASSIGN_RE.finditer(command):
        if unquoted[match.start()] and _is_shell_assignment_position(command, match.start()):
            events.append((match.start(), "bind", match.group(1)))
    for match in _FOR_VAR_RE.finditer(command):
        if unquoted[match.start()]:
            events.append((match.start(), "bind", match.group(1)))
    for match in _ENV_VAR_RE.finditer(command):
        if not expandable[match.start()]:
            continue
        if _DEFAULTED_VAR_RE.match(command, match.start()) is not None:
            continue
        events.append((match.start(), "expand", match.group(1)))
    return tuple(sorted(events))


def _is_shell_assignment_position(command: str, start: int) -> bool:
    """Return whether ``NAME=`` starts a shell assignment word, not an argument."""
    segment_start = max(command.rfind(token, 0, start) for token in (";", "&", "|", "\n")) + 1
    prefix = command[segment_start:start].strip()
    if not prefix:
        return True
    try:
        words = shlex.split(prefix, posix=True)
    except ValueError:
        return False
    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
    if all(assignment.fullmatch(word) for word in words):
        return True
    if words and Path(words[0]).name == "env":
        return all(word.startswith("-") or assignment.fullmatch(word) for word in words[1:])
    return False


def _nested_shell_commands(command: str) -> tuple[str, ...]:
    """Return recursively wrapped command-position ``sh -c`` payloads."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
    except ValueError:
        return ()
    segments: list[list[str]] = [[]]
    for part in parts:
        if re.fullmatch(r"[;&|]+", part):
            segments.append([])
        else:
            segments[-1].append(part)
    nested: list[str] = []
    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

    def unwrap(segment: list[str]) -> list[str]:
        remaining = list(segment)
        while remaining:
            command_name = Path(remaining[0]).name
            if command_name == "env":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-S", "--split-string"} and len(remaining) >= 2:
                        try:
                            return shlex.split(remaining[1], posix=True) + remaining[2:]
                        except ValueError:
                            return []
                    if token.startswith("--split-string="):
                        try:
                            return shlex.split(token.partition("=")[2], posix=True) + remaining[1:]
                        except ValueError:
                            return []
                    if token in {"-u", "--unset", "-C", "--chdir"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-") or assignment.fullmatch(token):
                        remaining = remaining[1:]
                        continue
                    break
                continue
            if command_name == "timeout":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-s", "--signal", "-k", "--kill-after"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-"):
                        remaining = remaining[1:]
                        continue
                    break
                if remaining:
                    remaining = remaining[1:]
                continue
            if command_name == "nice":
                remaining = remaining[1:]
                if remaining and remaining[0] in {"-n", "--adjustment"}:
                    remaining = remaining[2:]
                elif remaining and (
                    re.fullmatch(r"-n?\d+", remaining[0])
                    or remaining[0].startswith("--adjustment=")
                ):
                    remaining = remaining[1:]
                continue
            if command_name == "command":
                remaining = remaining[1:]
                while remaining and remaining[0] in {"--", "-p", "-v", "-V"}:
                    remaining = remaining[1:]
                continue
            break
        return remaining

    for segment in segments:
        while segment and assignment.fullmatch(segment[0]):
            segment = segment[1:]
        segment = unwrap(segment)
        if (
            len(segment) >= 3
            and Path(segment[0]).name in {"sh", "bash"}
            and segment[1] in {"-c", "--command"}
        ):
            nested.append(segment[2])
            nested.extend(_nested_shell_commands(segment[2]))
    return tuple(nested)


def _outer_shell_scope(command: str) -> str:
    """Return top-level command segments, excluding nested-shell invocations."""
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote is None and character in ";&|\n":
            segments.append(command[start:index])
            start = index + 1
    segments.append(command[start:])
    return ";".join(segment for segment in segments if not _nested_shell_commands(segment))


# A standalone dependency entry that claims a workspace file. Requires a
# directory separator so plain product names ("next.js", "Obsidian Vault")
# are never treated as file claims.
_FILE_CLAIM_RE = re.compile(r"\.?/?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}")


@dataclass(frozen=True)
class PreflightFinding:
    """One deterministic executability defect found in a Seed."""

    code: str
    blocking: bool
    subject: str
    detail: str
    question: str


@dataclass(frozen=True)
class SeedPreflightReport:
    """Outcome of :func:`run_seed_preflight`."""

    findings: tuple[PreflightFinding, ...]

    @property
    def blocking_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def passed(self) -> bool:
        return not self.blocking_findings

    @property
    def open_questions(self) -> tuple[str, ...]:
        """Every finding's question, blocking first, deduplicated."""
        ordered = (
            *self.blocking_findings,
            *(finding for finding in self.findings if not finding.blocking),
        )
        return tuple(dict.fromkeys(finding.question for finding in ordered))


def run_seed_preflight(seed: Seed, *, workspace_root: Path | None = None) -> SeedPreflightReport:
    """Check the Seed's executability claims against the filesystem.

    ``workspace_root`` anchors relative paths (the run workspace). When it is
    ``None`` a relative path's existence cannot be judged, so those checks
    are skipped rather than guessed.
    """
    findings: list[PreflightFinding] = []
    artifacts = _declared_artifacts(seed)
    claimed_files = _claimed_dependency_files(seed)

    findings.extend(_check_context_references(seed, workspace_root))
    findings.extend(_check_claimed_dependencies(claimed_files, workspace_root, artifacts))
    findings.extend(_check_verify_commands(seed, workspace_root, artifacts, claimed_files))
    findings.extend(_check_unverifiable_criteria(seed))
    findings.extend(_check_shared_verify_commands(seed))
    return SeedPreflightReport(findings=tuple(findings))


def _specs(seed: Seed) -> tuple[AcceptanceCriterionSpec, ...]:
    return tuple(
        criterion
        for criterion in seed.acceptance_criteria
        if isinstance(criterion, AcceptanceCriterionSpec)
    )


def _normalize_workspace_path(text: str) -> str:
    return text.strip().removeprefix("./")


def _declared_artifacts(seed: Seed) -> frozenset[str]:
    return frozenset(
        _normalize_workspace_path(artifact)
        for criterion in _specs(seed)
        for artifact in criterion.expected_artifacts
    )


def _claimed_dependency_files(seed: Seed) -> frozenset[str]:
    claims: set[str] = set()
    for entry in seed.brownfield_context.existing_dependencies:
        stripped = entry.strip()
        if " " in stripped or "://" in stripped:
            continue
        if _FILE_CLAIM_RE.fullmatch(stripped):
            claims.add(_normalize_workspace_path(stripped))
    return frozenset(claims)


def _path_exists(path_text: str, workspace_root: Path | None) -> bool | None:
    """Return existence when decidable; ``None`` for relative paths without a root."""
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        return candidate.exists()
    if workspace_root is None:
        return None
    return (workspace_root / candidate).exists()


def _check_context_references(seed: Seed, workspace_root: Path | None) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for reference in seed.brownfield_context.context_references:
        if _path_exists(reference.path, workspace_root) is False:
            findings.append(
                PreflightFinding(
                    code="context_reference_unresolved",
                    blocking=True,
                    subject=reference.path,
                    detail=(
                        f"brownfield context reference {reference.path!r} "
                        f"(role {reference.role!r}) does not resolve to an existing path"
                    ),
                    question=(
                        "What is the real, absolute path for the referenced context "
                        f"{reference.path!r}?"
                    ),
                )
            )
    return findings


def _check_claimed_dependencies(
    claimed_files: frozenset[str],
    workspace_root: Path | None,
    artifacts: frozenset[str],
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for claim in sorted(claimed_files):
        if claim in artifacts:
            continue
        if _path_exists(claim, workspace_root) is False:
            findings.append(
                PreflightFinding(
                    code="claimed_dependency_missing",
                    blocking=True,
                    subject=claim,
                    detail=(
                        f"existing_dependencies claims {claim!r} pre-exists, "
                        "but it does not exist in the workspace"
                    ),
                    question=(
                        f"{claim!r} is claimed as an existing dependency but is absent — "
                        "does it exist elsewhere, or must it be created first?"
                    ),
                )
            )
    return findings


def _check_verify_commands(
    seed: Seed,
    workspace_root: Path | None,
    artifacts: frozenset[str],
    claimed_files: frozenset[str],
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    seen_vars: set[str] = set()
    seen_tokens: set[str] = set()
    for criterion in _specs(seed):
        command = criterion.verify_command
        if not command:
            continue
        scannable = _URL_RE.sub(" ", command)
        program_tokens = _command_program_tokens(command)
        nested_shell_commands = _nested_shell_commands(scannable)
        outer_shell_command = _outer_shell_scope(scannable)
        shell_commands = (outer_shell_command, *nested_shell_commands)
        for shell_command in shell_commands:
            command_bound: set[str] = set()
            for _, event, variable in _shell_variable_events(shell_command):
                if event == "bind":
                    command_bound.add(variable)
                    continue
                if (
                    variable in _HOST_BOUND_ENV_VARS
                    or variable in command_bound
                    or variable in seen_vars
                ):
                    continue
                seen_vars.add(variable)
                findings.append(
                    PreflightFinding(
                        code="unbound_env_var",
                        blocking=True,
                        subject=f"${variable}",
                        detail=(
                            f"verify_command for {criterion.description[:80]!r} references "
                            f"${variable}, but nothing in the Seed binds it to a value"
                        ),
                        question=(
                            f"What concrete value must ${variable} hold when the verify command runs?"
                        ),
                    )
                )
        for token in _FILE_TOKEN_RE.findall(scannable):
            normalized = _normalize_workspace_path(token)
            if normalized in artifacts or normalized in claimed_files or normalized in seen_tokens:
                continue
            seen_tokens.add(normalized)
            if _path_exists(token, workspace_root) is False:
                is_program = normalized in program_tokens
                findings.append(
                    PreflightFinding(
                        code=(
                            "verify_program_missing" if is_program else "verify_script_unconfirmed"
                        ),
                        blocking=is_program,
                        subject=normalized,
                        detail=(
                            f"verify_command references {normalized!r}, which neither "
                            "exists in the workspace nor is declared as an expected artifact"
                        ),
                        question=(
                            f"Does {normalized!r} already exist, or must the run create it? "
                            "If the run creates it, declare it in expected_artifacts."
                        ),
                    )
                )
    return findings


def _command_program_tokens(command: str) -> frozenset[str]:
    """Return file operands that are the executable's verification program.

    This deliberately handles only unambiguous shell forms. A missing operand
    after ``python``/``bash``/``node`` or an explicit ``./program`` is a
    decidable fabrication; ordinary command arguments remain advisory.
    """
    runners = frozenset({"bash", "sh", "node", "ruby"})
    option_operands = {
        "bash": frozenset({"-O", "-o"}),
        "sh": frozenset({"-o"}),
        "node": frozenset({"-r", "--require", "--loader", "--import"}),
        "ruby": frozenset({"-I", "-r", "--require", "-C", "-E"}),
    }

    def is_runner(token: str) -> bool:
        executable = Path(token).name
        return (
            executable in runners
            or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?t?", executable) is not None
            or re.fullmatch(r"pypy\d*", executable) is not None
        )

    programs: set[str] = set()

    def unwrap_command(segment: list[str]) -> list[str]:
        """Strip standard execution wrappers without guessing their payload."""
        remaining = list(segment)
        while remaining:
            command_name = Path(remaining[0]).name
            if command_name == "env":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-S", "--split-string"} and len(remaining) >= 2:
                        try:
                            scan(shell_tokens(remaining[1]) + remaining[2:])
                        except ValueError:
                            pass
                        return []
                    if token.startswith("--split-string="):
                        try:
                            scan(shell_tokens(token.partition("=")[2]) + remaining[1:])
                        except ValueError:
                            pass
                        return []
                    if token in {"-u", "--unset", "-C", "--chdir"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                        remaining = remaining[1:]
                        continue
                    break
                continue
            if command_name == "timeout":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-s", "--signal", "-k", "--kill-after"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-"):
                        remaining = remaining[1:]
                        continue
                    break
                if remaining:  # duration operand
                    remaining = remaining[1:]
                continue
            if command_name == "nice":
                remaining = remaining[1:]
                if remaining and remaining[0] in {"-n", "--adjustment"}:
                    remaining = remaining[2:]
                elif remaining and (
                    re.fullmatch(r"-n?\d+", remaining[0])
                    or remaining[0].startswith("--adjustment=")
                ):
                    remaining = remaining[1:]
                continue
            if command_name == "command":
                remaining = remaining[1:]
                while remaining and remaining[0] in {"--", "-p", "-v", "-V"}:
                    remaining = remaining[1:]
                continue
            break
        return remaining

    def shell_tokens(value: str) -> list[str]:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    def scan(parts: list[str]) -> None:
        segments: list[list[str]] = [[]]
        for part in parts:
            if re.fullmatch(r"[;&|]+", part):
                segments.append([])
            else:
                segments[-1].append(part)
        for segment in segments:
            while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
                segment = segment[1:]
            segment = unwrap_command(segment)
            if segment and "/" in segment[0] and not is_runner(segment[0]):
                programs.add(_normalize_workspace_path(segment[0]))
        for segment in segments:
            while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
                segment = segment[1:]
            segment = unwrap_command(segment)
            if not segment:
                continue
            executable = Path(segment[0]).name
            if (
                executable in {"bash", "sh"}
                and len(segment) >= 3
                and segment[1] in {"-c", "--command"}
            ):
                try:
                    scan(shell_tokens(segment[2]))
                except ValueError:
                    pass
                continue
            if "/" in segment[0] and not is_runner(segment[0]):
                programs.add(_normalize_workspace_path(segment[0]))
            if not is_runner(segment[0]):
                continue
            runner = executable
            cursor = 1
            while cursor < len(segment) and segment[cursor].startswith("-"):
                option = segment[cursor]
                if (runner in {"node", "ruby"} and option in {"-e", "--eval", "-p", "--print"}) or (
                    re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?t?|pypy\d*)", runner)
                    and option in {"-c", "--command"}
                ):
                    cursor = len(segment)
                    break
                if option in {"-m", "--module"}:
                    cursor = len(segment)
                    break
                if option in {
                    "-X",
                    "-W",
                    "--check-hash-based-pycs",
                    *option_operands.get(runner, ()),
                }:
                    cursor += 2
                    continue
                cursor += 1
            if cursor < len(segment) and not segment[cursor].startswith("-"):
                programs.add(_normalize_workspace_path(segment[cursor]))

    try:
        scan(shell_tokens(command))
    except ValueError:
        return frozenset()
    return frozenset(programs)


def _check_unverifiable_criteria(seed: Seed) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for criterion in _specs(seed):
        if criterion.has_success_contract:
            continue
        findings.append(
            PreflightFinding(
                code="unverifiable_criterion",
                blocking=False,
                subject=criterion.description[:80],
                detail=(
                    f"acceptance criterion {criterion.description[:80]!r} declares no "
                    "verify_command, expected_artifacts, or output_assertion"
                ),
                question=(
                    f"What deterministic command or artifact proves {criterion.description[:80]!r}?"
                ),
            )
        )
    return findings


def _check_shared_verify_commands(seed: Seed) -> list[PreflightFinding]:
    grouped: dict[str, list[str]] = {}
    for criterion in _specs(seed):
        if not criterion.verify_command:
            continue
        normalized = " ".join(criterion.verify_command.split())
        grouped.setdefault(normalized, []).append(criterion.description[:80])
    findings: list[PreflightFinding] = []
    for command, descriptions in grouped.items():
        if len(descriptions) < 2:
            continue
        rendered = "; ".join(repr(description) for description in descriptions)
        findings.append(
            PreflightFinding(
                code="shared_verify_command",
                blocking=False,
                subject=command[:120],
                detail=(
                    f"acceptance criteria {rendered} share one verify_command, so their "
                    "verification results cannot be distinguished"
                ),
                question=(
                    f"Criteria {rendered} share one verify command — what distinct check "
                    "proves each one separately?"
                ),
            )
        )
    return findings
