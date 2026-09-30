"""Corroborate evidence claims by replaying commands the leaf actually ran.

The transcript verifier proves a ``tests_passed`` or ``commands_run`` claim
from what the runtime recorded. That proof used to depend on recognizing the
test runner (pytest, unittest, tox, Django, SymPy, ...) and on the claim being
written in one of a few narrow formats, so ``make test``, ``npm test``,
``./run_tests.sh``, a piped ``pytest -q | tail -5`` or a free-text claim such as
``migrations (578 tests)`` was rejected even when the work was correct.

Replay removes the runner dependency. The harness takes a command the
transcript shows the leaf running (never text from the claim), re-runs it in
an isolated copy of the workspace, and treats a zero exit as its own
observation. A claim is corroborated when a linked command exits 0 on replay.

Execution rules (each one fails closed):

- Only commands from structured Bash tool calls in the transcript are
  candidates; runtime shell wrappers (``/bin/zsh -lc '...'``) are peeled. A
  command whose transcript run recorded a non-zero exit (or a failed tool
  result) is not replayed, and a replay never backs a claim when its exit
  differs from the recorded one.
- Only commands ``replay_policy.authorize_replay`` authorizes are replayed:
  allowlisted programs (test and build runners, interpreters running a
  workspace script or a test module, and workspace scripts), with the
  denylist (privilege, network, container, deletion, version-control writes,
  package installs) applied to every program of the same resolution. Every
  other command keeps the transcript-only rules.
- The command runs as a direct argv, never through a shell, in a fresh copy of
  the workspace, under the verify gate's timeout, with
  ``PYTHONDONTWRITEBYTECODE=1``. At most ``MAX_REPLAYED_COMMANDS`` commands
  per criterion. Its environment is built from scratch
  (``REPLAY_ENV_PASSTHROUGH``: ``PATH``, the locale, ``HOME`` and the
  variables that locate an installed toolchain, copied from the verify gate's
  sanitized environment) plus the command's own assignments. The narrowing
  variables (``replay_policy.narrowing_variable``: ``PYTEST_ADDOPTS``,
  ``PYTHONPATH``, ``DJANGO_SETTINGS_MODULE``, ``NODE_OPTIONS``, ``JEST_*``,
  ...) are never copied, and each run records the names it left out in
  ``scrubbed_environment``; the command's own assignments are kept, and they
  disable target linkage instead.
- The command runs under the execution sandbox
  (``ouroboros.runtime.exec_sandbox``): it can write only inside the copy and
  a per-run temp directory, and has no network. Where the sandbox is
  unavailable, nothing is replayed and the reason is recorded
  (``sandbox_unavailable`` or ``REPLAY_SKIPPED_NETWORK``).
- ``CMD [2>&1] | <filter> ...`` with only output filters after ``CMD``
  (``REPLAY_OUTPUT_FILTERS``) replays ``CMD`` alone and uses its own exit code;
  the filters are never run. Any other shell construct is not replayed.
- Every pre-existing file in the copy is digested before and after the run. A
  changed or deleted file marks the run ``mutated``, which is never success.
- Paths the copy reaches outside itself (the linked dependency trees and the
  targets of copied symlinks) are outside the writable roots, so the sandbox
  denies writes to them. Only when the sandbox is switched off (unsafe) is
  their metadata (every entry's type, size, mtime and ctime) fingerprinted
  before and after instead, any change marking the run ``mutated``; a tree
  too large to fingerprint is then not replayed.

Linkage between a claim and a replayed command is deliberately narrow. The
claim, whitespace-normalized, must equal the transcript command or its replayed
core, or equal one of them followed by a single trailing parenthetical
annotation (``make test (12 passed)``); or the claim, minus a trailing
``(N tests)`` count, is a single test target that is a positional operand of a
test runner that executes it, with no option or command-line configuration
that narrows what it runs (``replay_policy.claim_target_operands``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile

from ouroboros.config.exec_sandbox import exec_sandbox_enabled
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_has_conflicting_tool_call_ids,
    _runtime_message_is_tool_completion,
    _runtime_message_recorded_exit_status,
    _runtime_message_tool_call_id,
    _runtime_message_tool_call_ids,
    _runtime_messages_support_command_claim,
)
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import (
    _IGNORED_DIRECTORY_NAMES,
    CommandObservation,
    observation_from_message,
)
from ouroboros.orchestrator.evidence.replay_policy import (
    authorize_replay,
    claim_target_operands,
    narrowing_assignments,
    narrowing_variable,
    outside_known_roots,
)
from ouroboros.orchestrator.evidence.shell_parsing import (
    _looks_like_test_command,
    _peel_shell_wrappers,
    _top_level_shell_character_positions,
    command_line_assignments,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _TEST_COUNT_ANNOTATION_RE,
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence.test_reexecution import (
    MAX_REEXECUTED_COMMANDS,
    OUTPUT_TAIL_CHARS,
    confined_test_invocation,
)
from ouroboros.orchestrator.evidence_schema import EvidenceError, extract_evidence
from ouroboros.orchestrator.verify_command_runner import run_with_shell
from ouroboros.runtime.exec_sandbox import (
    DEFAULT_ENV_PASSTHROUGH,
    SandboxBackend,
    SandboxUnavailable,
    SandboxUnavailableReason,
    confine,
    sandbox_unavailable_reason,
)

MAX_REPLAYED_COMMANDS = MAX_REEXECUTED_COMMANDS

# Output filters: they read the command's stdout and print a view of it. They
# are not replayed: replay runs only the command in front of them and judges
# that command's own exit status, which the filter would otherwise have
# replaced. (Some of them can write or execute, e.g. GNU ``sed``'s ``w`` and
# ``e``; that is harmless because they never run.)
REPLAY_OUTPUT_FILTERS = frozenset(
    {"tail", "head", "grep", "egrep", "fgrep", "sed", "cat", "cut", "sort", "uniq", "wc", "tr"}
)


# Copy budget: past either bound the workspace is not copied and nothing is
# replayed (the claims keep the transcript-only rules).
MAX_COPY_ENTRIES = 50_000
MAX_COPY_BYTES = 1 << 30
# Not copied: version-control metadata (a replay must not reach the user's
# repository) and caches a run regenerates.
_SKIPPED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ouroboros",
    }
)
# Dependency trees: linked into the copy, not copied (they can hold hundreds
# of thousands of files). They are outside the protected bytes of the copy;
# the live trees they point at are protected instead (see ``_replay_one``).
_LINKED_DIRECTORY_NAMES = frozenset({".venv", "venv", "node_modules", ".tox", ".nox"})
# Past this many entries a live tree the copy links to cannot be fingerprinted,
# and the command is not replayed when the sandbox is switched off.
MAX_PROTECTED_LINK_ENTRIES = 250_000
REPLAY_SKIPPED_NETWORK = SandboxUnavailableReason.NETWORK_ISOLATION_UNAVAILABLE.value
# The replay environment: the sandbox defaults plus ``HOME`` and the variables
# that locate an installed toolchain and its offline caches. The sandbox makes
# all of them read-only. None of them is a narrowing variable.
REPLAY_ENV_PASSTHROUGH: tuple[str, ...] = (
    *DEFAULT_ENV_PASSTHROUGH,
    "HOME",
    "USER",
    "LOGNAME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "JAVA_HOME",
    "GOPATH",
    "GOROOT",
    "GOMODCACHE",
    "CARGO_HOME",
    "RUSTUP_HOME",
)


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    """A transcript command that can be replayed, split into its parts."""

    transcript_command: str
    core_command: str
    argv: tuple[str, ...]
    env_delta: Mapping[str, str] = field(default_factory=dict)
    cwd_relative: str = "."
    transcript_returncode: int | None = None


def _has_active_shell_syntax(text: str) -> bool:
    """Return True when ``text`` has shell syntax beyond words and quoting.

    Outside quotes, any control, redirection, grouping, substitution or line
    break counts; inside double quotes, ``$`` and backquotes (expansion) count;
    single-quoted text is literal.
    """
    quote: str | None = None
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char in "$`":
                return True
            continue
        if char in "'\"":
            quote = char
            continue
        if char in "`$;&|<>(){}\n\r":
            return True
    return quote is not None


def _is_pure_output_filter(segment: str) -> bool:
    text = segment.strip()
    if not text or _has_active_shell_syntax(text):
        return False
    try:
        parts = shlex.split(text)
    except ValueError:
        return False
    return bool(parts) and parts[0] in REPLAY_OUTPUT_FILTERS


def output_filter_core(command: str) -> str | None:
    """Return the command that decides the exit status of ``command``.

    ``command`` itself when it has no top-level pipe; ``CMD`` for
    ``CMD [2>&1] | f1 ... [| f2 ...]`` when every ``f`` is a pure output filter
    (``REPLAY_OUTPUT_FILTERS``) with no shell syntax of its own; otherwise
    None. ``2>&1`` is admitted only immediately before the first filter pipe:
    it changes what the filter sees, not ``CMD``'s exit status.
    """
    text = command.strip()
    pipes = _top_level_shell_character_positions(text, "|")
    if not pipes:
        return text or None
    if any(
        (index + 1 < len(text) and text[index + 1] == "|") or (index > 0 and text[index - 1] == "|")
        for index in pipes
    ):
        return None
    bounds = (-1, *pipes, len(text))
    segments = [text[start + 1 : end] for start, end in zip(bounds, bounds[1:], strict=False)]
    if not all(_is_pure_output_filter(segment) for segment in segments[1:]):
        return None
    core = re.sub(r"\s+2>&1$", "", segments[0].strip())
    return core or None


def replay_candidate(
    command: str, task_cwd: str | None, *, transcript_returncode: int | None = None
) -> ReplayCandidate | None:
    """Parse one recorded transcript command into a replay candidate, or None.

    Parsing only: whether the candidate may run is ``replay_admissible``.
    """
    if task_cwd is None:
        return None
    body = _peel_shell_wrappers(command)
    core = output_filter_core(body)
    if core is None:
        return None
    invocation = confined_test_invocation(core, task_cwd)
    if invocation is None:
        return None
    env_delta, argv, run_cwd = invocation
    cwd_relative = "."
    if run_cwd is not None and run_cwd != task_cwd:
        try:
            cwd_relative = os.path.relpath(run_cwd, Path(task_cwd).resolve())
        except (OSError, RuntimeError, ValueError):
            return None
    return ReplayCandidate(
        transcript_command=body,
        core_command=core,
        argv=tuple(argv),
        env_delta=dict(env_delta),
        cwd_relative=cwd_relative,
        transcript_returncode=transcript_returncode,
    )


def replay_admissible(
    candidate: ReplayCandidate,
    workspace: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return True when the allowlist admits ``candidate`` and the denylist does not refuse it.

    ``environment`` is the replay environment (the process environment when
    None). An environment assignment in the command, leading or consumed by
    an ``env`` wrapper, whose value names an absolute path outside the
    workspace and the environment roots (for example ``PATH=/tmp/elsewhere``)
    is refused as well.
    """
    if (
        authorize_replay(
            candidate.argv,
            workspace=workspace,
            cwd_relative=candidate.cwd_relative,
            environment=environment,
        )
        is None
    ):
        return False
    values = [
        *candidate.env_delta.values(),
        *(token.partition("=")[2] for token in command_line_assignments(candidate.argv)),
    ]
    return not any(
        outside_known_roots(part, workspace=workspace, environment=environment)
        for value in values
        for part in value.split(os.pathsep)
    )


# One trailing parenthetical annotation after the command, as in
# ``make test (12 passed)``; the annotation holds no parentheses of its own.
_TRAILING_ANNOTATION_RE = re.compile(r"(.*\S)\s*\([^()]*\)")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _claim_test_target(claim: str) -> str | None:
    """Return the single test target a claim names, or None.

    The claim minus a trailing ``(N tests)`` count and surrounding backquotes
    or quotes, when that is one whitespace-free token not starting with ``-``.
    """
    text = _TEST_COUNT_ANNOTATION_RE.sub("", claim.strip(), count=1).strip().strip("`'\"")
    if not text or any(char.isspace() for char in text) or text.startswith("-"):
        return None
    return text


def claim_links_to_command(
    claim: str,
    *,
    transcript_command: str,
    core_command: str,
    argv: Sequence[str],
    environment: Sequence[str] = (),
) -> bool:
    """Return True when ``claim`` refers to this replayed command.

    Linked only when the whitespace-normalized claim (a) equals the transcript
    command or its replayed core, (b) equals one of them followed by one
    trailing parenthetical annotation, or (c) names a single test target that
    is a positional operand of a test runner executing it, with no option or
    command-line configuration that excludes or narrows the tests
    (``replay_policy.claim_target_operands``; ``environment`` names the
    variables the command assigns before ``argv``, and an assignment to a
    narrowing variable anywhere in the transcript command counts too). A claim
    that merely
    contains a command is not linked to it.
    """
    text = _normalize(claim)
    if not text:
        return False
    commands = {_normalize(transcript_command), _normalize(core_command)} - {""}
    if text in commands:
        return True
    annotated = _TRAILING_ANNOTATION_RE.fullmatch(text)
    if annotated is not None and annotated.group(1) in commands:
        return True
    target = _claim_test_target(claim)
    if target is None:
        return False
    assigned = (*environment, *narrowing_assignments(transcript_command))
    return target in claim_target_operands(argv, assigned)


def replayed_command_supports_claim(value: str, messages: tuple[AgentMessage, ...]) -> bool:
    """Return True when a replayed command linked to ``value`` exited 0."""
    for message in messages:
        observation = observation_from_message(message)
        if observation is None:
            continue
        for run in observation.command_runs:
            if not run.succeeded:
                continue
            if claim_links_to_command(
                value,
                transcript_command=run.transcript_command or run.command,
                core_command=run.command,
                argv=run.argv,
                environment=tuple(name for name, _ in run.env_delta),
            ):
                return True
    return False


def transcript_exit_status(messages: Sequence[AgentMessage], index: int) -> int | None:
    """Return the exit status the transcript recorded for the call at ``index``.

    Every record of the run counts: the call itself and each completion
    correlated with it (by tool-call id, or for an id-less call the next
    completion before another call), each read by
    ``claims._runtime_message_recorded_exit_status``. A failure in any of them,
    or records that disagree, is a failure (the non-zero status); a completion
    whose id is ambiguous is a failure too. 0 only when every record that
    states an outcome states success; None when none states one.
    """
    call = messages[index]
    if _runtime_message_has_conflicting_tool_call_ids(call):
        return 1
    records: list[AgentMessage] = [call]
    call_id = _runtime_message_tool_call_id(call)
    if call_id is not None:
        for candidate in messages:
            if candidate is call or not _runtime_message_is_tool_completion(candidate):
                continue
            if call_id not in _runtime_message_tool_call_ids(candidate):
                continue
            if _runtime_message_has_conflicting_tool_call_ids(candidate):
                return 1
            records.append(candidate)
    else:
        for candidate in messages[index + 1 :]:
            if _runtime_message_is_tool_completion(candidate):
                records.append(candidate)
                break
            if candidate.tool_name is not None:
                break
    statuses = [
        status
        for record in records
        if (status := _runtime_message_recorded_exit_status(record)) is not None
    ]
    nonzero = [status for status in statuses if status != 0]
    if nonzero:
        return nonzero[0]
    return 0 if statuses else None


def select_replay_candidates(
    *,
    final_message: str | None,
    messages: tuple[AgentMessage, ...],
    task_cwd: str | None,
) -> tuple[ReplayCandidate, ...]:
    """Return transcript commands worth replaying for this leaf, if any.

    Empty unless some ``tests_passed`` or ``commands_run`` claim is not already
    proven by the transcript. Candidates come only from the transcript's Bash
    calls, most recent first: commands linked to an unproven claim, then
    recognized test commands (whose replay the runner-output rules judge).
    Only allowlisted commands are candidates, and the most recent run of a
    command decides: if the transcript recorded a non-zero exit for it, it is
    not replayed.
    """
    if not final_message or task_cwd is None:
        return ()
    try:
        record = extract_evidence(final_message)
    except EvidenceError:
        return ()
    support_messages = tuple(message for message in messages if not message.is_final)
    bash_messages = tuple(message for message in support_messages if message.tool_name == "Bash")
    unproven = [
        claim
        for claim in _flatten_evidence_values(record.get("tests_passed"))
        if not _runtime_messages_support_test_claim(
            value=claim,
            backed_commands=(),
            messages=support_messages,
            task_cwd=task_cwd,
        )
    ]
    unproven.extend(
        claim
        for claim in _flatten_evidence_values(record.get("commands_run"))
        if not _runtime_messages_support_command_claim(claim, bash_messages)
    )
    if not unproven:
        return ()

    linked: list[ReplayCandidate] = []
    recognized: list[ReplayCandidate] = []
    seen: set[tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]] = set()
    for index in reversed(range(len(support_messages))):
        message = support_messages[index]
        if message.tool_name != "Bash":
            continue
        recorded_status = transcript_exit_status(support_messages, index)
        for recorded in _runtime_message_command_values(message):
            candidate = replay_candidate(recorded, task_cwd, transcript_returncode=recorded_status)
            if candidate is None or not replay_admissible(candidate, task_cwd):
                continue
            key = (
                candidate.cwd_relative,
                candidate.argv,
                tuple(sorted(candidate.env_delta.items())),
            )
            if key in seen:
                continue
            if recorded_status not in (None, 0):
                # The latest run of this command failed in the transcript; an
                # earlier passing run of it must not stand in for that one.
                seen.add(key)
                continue
            if any(
                claim_links_to_command(
                    claim,
                    transcript_command=candidate.transcript_command,
                    core_command=candidate.core_command,
                    argv=candidate.argv,
                    environment=tuple(candidate.env_delta),
                )
                for claim in unproven
            ):
                seen.add(key)
                linked.append(candidate)
            elif _looks_like_test_command(candidate.core_command):
                # A recognized test run the leaf performed: the transcript-proof
                # rules for runner output (node ids, dotted labels) judge its
                # replay; it is never linked to a claim by this module.
                seen.add(key)
                recognized.append(candidate)
    return tuple([*linked, *recognized][:MAX_REPLAYED_COMMANDS])


def replay_unavailable_reason(sandbox_enabled: bool | None = None) -> str | None:
    """Return why nothing can be replayed on this host, or None when it can.

    ``sandbox_enabled`` is the caller's sealed sandbox policy; None reads the
    live switch.
    """
    enabled = exec_sandbox_enabled() if sandbox_enabled is None else sandbox_enabled
    reason = sandbox_unavailable_reason(deny_network=True, enabled=enabled)
    return None if reason is None else reason.value


def _outside_links(destination: Path) -> tuple[str, ...]:
    """Return the real paths outside ``destination`` that its symlinks reach."""
    root = os.path.realpath(destination)
    reached: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(destination, followlinks=False):
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            if target != root and not target.startswith(root + os.sep):
                reached.add(target)
    return tuple(sorted(reached))


def copy_workspace(source: Path, destination: Path) -> tuple[str, ...] | None:
    """Copy ``source`` into ``destination`` for a replay; None when over budget.

    Symlinks are copied as links; dependency trees are linked, not copied;
    version-control metadata and caches are skipped. Returns the real paths
    outside the copy that its links reach (the live dependency trees and the
    targets of copied symlinks): a replay must not change them.
    """
    entries = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        target = destination / current.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = current / name
            if name in _SKIPPED_DIRECTORY_NAMES:
                continue
            if path.is_symlink():
                os.symlink(os.readlink(path), target / name)
            elif name in _LINKED_DIRECTORY_NAMES:
                os.symlink(path, target / name, target_is_directory=True)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = current / name
            entries += 1
            if entries > MAX_COPY_ENTRIES:
                return None
            status = os.lstat(path)
            if stat.S_ISLNK(status.st_mode):
                os.symlink(os.readlink(path), target / name)
            elif stat.S_ISREG(status.st_mode):
                total_bytes += status.st_size
                if total_bytes > MAX_COPY_BYTES:
                    return None
                shutil.copy2(path, target / name)
    return _outside_links(destination)


_Fingerprint = tuple[tuple[str, int, int, int, int], ...]


def _fingerprint_outside_paths(paths: Sequence[str]) -> _Fingerprint | None:
    """Return the metadata of every entry under ``paths``; None when over budget.

    Each entry (directories, files and symlinks, not followed) contributes its
    type, size, mtime and ctime; a missing path is recorded as missing. A
    creation, deletion or write anywhere under the paths changes the result.
    """
    entries: list[tuple[str, int, int, int, int]] = []
    for base in paths:
        try:
            status = os.lstat(base)
        except OSError:
            entries.append((base, -1, 0, 0, 0))
            continue
        entries.append(
            (base, status.st_mode, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        )
        if not stat.S_ISDIR(status.st_mode):
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for name in (*dirnames, *filenames):
                path = os.path.join(dirpath, name)
                try:
                    status = os.lstat(path)
                except OSError:
                    continue
                entries.append(
                    (path, status.st_mode, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                )
                if len(entries) > MAX_PROTECTED_LINK_ENTRIES:
                    return None
    return tuple(sorted(entries))


def protected_digest(root: Path) -> dict[str, str]:
    """Return a SHA-256 digest of every regular file a replay must not change.

    Build outputs, caches and dependency trees (the workspace snapshot's
    ignored directories) are excluded; symlinks are not followed.
    """
    digests: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIRECTORY_NAMES]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            except OSError:
                continue
            digests[os.path.relpath(path, root)] = digest.hexdigest()
    return digests


def _remap_workspace_path(value: str, workspaces: Sequence[str], copy_root: str) -> str:
    """Point absolute workspace paths in ``value`` at the copy, in one pass."""
    alternatives = "|".join(
        re.escape(workspace) for workspace in sorted(set(workspaces), key=len, reverse=True)
    )
    pattern = r"(?<![\w./-])(?:" + alternatives + r")(?=/|$)"
    return re.sub(pattern, lambda _match: copy_root, value)


async def _replay_one(
    candidate: ReplayCandidate,
    *,
    workspace: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    sandbox_enabled: bool,
) -> CommandObservation | None:
    source = Path(workspace).resolve()
    scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="ouroboros-replay-"))
    try:
        copy_root = scratch / "workspace"
        temp_dir = scratch / "tmp"
        if scratch.resolve().is_relative_to(source):
            # A copy inside the workspace would copy itself.
            return None
        outside = await asyncio.to_thread(copy_workspace, source, copy_root)
        if outside is None:
            return None
        temp_dir.mkdir()
        # Absolute workspace paths in the recorded command point at the copy.
        workspaces = (str(source), workspace.rstrip("/") or "/")
        argv = [
            _remap_workspace_path(token, workspaces, str(copy_root)) for token in candidate.argv
        ]
        env_delta = {
            key: _remap_workspace_path(value, workspaces, str(copy_root))
            for key, value in candidate.env_delta.items()
        }
        # Configuration the replay would inherit from the worker's environment
        # must not narrow what a test runner collects or selects.
        inherited = {key: value for key, value in env.items() if not narrowing_variable(key)}
        scrubbed = tuple(sorted(key for key in env if narrowing_variable(key)))
        confined = confine(
            argv,
            cwd=str((copy_root / candidate.cwd_relative).resolve()),
            writable_roots=(str(copy_root),),
            temp_dir=str(temp_dir),
            deny_network=True,
            env_source=inherited,
            env_passthrough=REPLAY_ENV_PASSTHROUGH,
            # No bytecode caches: they would be writes into linked live trees.
            env_set={**env_delta, "PYTHONDONTWRITEBYTECODE": "1"},
            enabled=sandbox_enabled,
        )
        if isinstance(confined, SandboxUnavailable):
            return None
        outside_before: _Fingerprint | None = None
        if confined.backend is SandboxBackend.DISABLED and outside:
            # Unconfined (unsafe off switch): writes to the live paths the copy
            # links to can only be detected, not denied.
            outside_before = await asyncio.to_thread(_fingerprint_outside_paths, outside)
            if outside_before is None:
                return None
        before = await asyncio.to_thread(protected_digest, copy_root)
        run = await run_with_shell(
            confined.argv,
            cwd=confined.cwd,
            env=confined.env,
            timeout_seconds=timeout_seconds,
        )
        if run.start_error is not None:
            return None
        after = await asyncio.to_thread(protected_digest, copy_root)
        mutated = any(after.get(path) != digest for path, digest in before.items())
        if outside_before is not None:
            outside_after = await asyncio.to_thread(_fingerprint_outside_paths, outside)
            mutated = mutated or outside_after != outside_before
        return CommandObservation(
            command=candidate.core_command,
            returncode=run.returncode,
            output_tail=run.output[-OUTPUT_TAIL_CHARS:],
            timed_out=run.timed_out,
            transcript_command=candidate.transcript_command,
            argv=candidate.argv,
            mutated=mutated,
            network_isolated=confined.network_denied,
            transcript_returncode=candidate.transcript_returncode,
            env_delta=tuple(sorted(candidate.env_delta.items())),
            scrubbed_environment=scrubbed,
        )
    except OSError:
        return None
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)


async def replay_commands(
    candidates: Sequence[ReplayCandidate],
    *,
    workspace: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    sandbox_enabled: bool | None = None,
) -> tuple[CommandObservation, ...]:
    """Replay each candidate in its own fresh copy of ``workspace``.

    Nothing runs when the execution sandbox is unavailable
    (``replay_unavailable_reason``) or for a candidate the allowlist and
    denylist do not admit. ``sandbox_enabled`` is the caller's sealed sandbox
    policy (the executor's, from the execution-semantics contract); None
    reads the live switch.
    """
    if not candidates:
        return ()
    if sandbox_enabled is None:
        sandbox_enabled = await asyncio.to_thread(exec_sandbox_enabled)
    if await asyncio.to_thread(replay_unavailable_reason, sandbox_enabled) is not None:
        return ()
    observations: list[CommandObservation] = []
    for candidate in candidates[:MAX_REPLAYED_COMMANDS]:
        if not replay_admissible(candidate, workspace, env):
            continue
        observation = await _replay_one(
            candidate,
            workspace=workspace,
            env=env,
            timeout_seconds=timeout_seconds,
            sandbox_enabled=sandbox_enabled,
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


__all__ = [
    "MAX_COPY_BYTES",
    "MAX_COPY_ENTRIES",
    "MAX_PROTECTED_LINK_ENTRIES",
    "MAX_REPLAYED_COMMANDS",
    "REPLAY_ENV_PASSTHROUGH",
    "REPLAY_OUTPUT_FILTERS",
    "REPLAY_SKIPPED_NETWORK",
    "ReplayCandidate",
    "claim_links_to_command",
    "copy_workspace",
    "output_filter_core",
    "protected_digest",
    "replay_candidate",
    "replay_admissible",
    "replay_commands",
    "replay_unavailable_reason",
    "replayed_command_supports_claim",
    "select_replay_candidates",
    "transcript_exit_status",
]
