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
        command_bound = {
            *_ENV_ASSIGN_RE.findall(scannable),
            *_FOR_VAR_RE.findall(scannable),
            *_DEFAULTED_VAR_RE.findall(scannable),
        }
        for variable in _ENV_VAR_RE.findall(scannable):
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
    try:
        tokens = shlex.split(command)
    except ValueError:
        return frozenset()
    runners = frozenset({"bash", "sh", "node", "ruby"})

    def is_runner(token: str) -> bool:
        executable = Path(token).name
        return (
            executable in runners
            or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?t?", executable) is not None
            or re.fullmatch(r"pypy\d*", executable) is not None
        )

    programs: set[str] = set()

    def scan(parts: list[str]) -> None:
        if parts and "/" in parts[0] and not is_runner(parts[0]):
            programs.add(_normalize_workspace_path(parts[0]))
        for index, token in enumerate(parts):
            if token in {"bash", "sh"} and index + 2 < len(parts) and parts[index + 1] == "-c":
                try:
                    scan(shlex.split(parts[index + 2]))
                except ValueError:
                    continue
            if not is_runner(token):
                continue
            cursor = index + 1
            while cursor < len(parts) and parts[cursor].startswith("-"):
                if parts[cursor] in {"-c", "--command"} and cursor + 1 < len(parts):
                    try:
                        scan(shlex.split(parts[cursor + 1]))
                    except ValueError:
                        pass
                    break
                if parts[cursor] in {"-m", "--module"}:
                    break
                if parts[cursor] in {"-X", "-W", "--check-hash-based-pycs"}:
                    cursor += 2
                    continue
                cursor += 1
            if cursor < len(parts) and not parts[cursor].startswith("-"):
                programs.add(_normalize_workspace_path(parts[cursor]))

    scan(tokens)
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
