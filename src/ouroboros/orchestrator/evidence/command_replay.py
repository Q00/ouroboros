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
  candidates; runtime shell wrappers (``/bin/zsh -lc '...'``) are peeled.
- The command runs as a direct argv, never through a shell, in a fresh copy of
  the workspace, under the verify gate's sanitized environment and timeout.
  At most ``MAX_REPLAYED_COMMANDS`` commands per criterion.
- Network access is denied where the platform allows it (``sandbox-exec`` on
  macOS, an unprivileged network namespace on Linux); otherwise the run is
  recorded with ``network_isolated=False``.
- ``CMD [2>&1] | <filter> ...`` with only pure output filters after ``CMD``
  (``REPLAY_OUTPUT_FILTERS``) replays ``CMD`` alone and uses its own exit code.
  Any other shell construct is not replayed.
- Commands on the denylist (``replay_denied``: privilege, network, container,
  deletion, version-control writes, package installs) are never replayed; their
  claims keep the transcript-only rules.
- Every pre-existing file in the copy is digested before and after the run. A
  changed or deleted file marks the run ``mutated``, which is never success.

Linkage between a claim and a replayed command is deliberately narrow: the
claim equals or contains the command (at word boundaries), or the claim, minus
a trailing ``(N tests)`` count, is a single test target that appears verbatim
as an argument of the command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import functools
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_messages_support_command_claim,
)
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import (
    _IGNORED_DIRECTORY_NAMES,
    CommandObservation,
    observation_from_message,
)
from ouroboros.orchestrator.evidence.shell_parsing import (
    _is_env_assignment,
    _is_python_executable,
    _looks_like_test_command,
    _shell_command_body,
    _strip_env_prefix,
    _top_level_shell_character_positions,
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

MAX_REPLAYED_COMMANDS = MAX_REEXECUTED_COMMANDS

# Pure output filters: they read the command's stdout and print a view of it.
# Replay never runs them; it runs the command in front of them and judges that
# command's own exit status, which the filter would otherwise have replaced.
REPLAY_OUTPUT_FILTERS = frozenset(
    {"tail", "head", "grep", "egrep", "fgrep", "sed", "cat", "cut", "sort", "uniq", "wc", "tr"}
)

# Programs that are never replayed, whatever their arguments: privilege,
# remote access and transfer, containers, deletion, process control, system
# package managers, and programs that open other applications.
_DENIED_PROGRAMS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "curl",
        "wget",
        "nc",
        "ncat",
        "telnet",
        "ftp",
        "docker",
        "docker-compose",
        "podman",
        "kubectl",
        "rm",
        "rmdir",
        "dd",
        "shred",
        "kill",
        "pkill",
        "killall",
        "shutdown",
        "reboot",
        "launchctl",
        "systemctl",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "pacman",
        "port",
        "open",
        "xdg-open",
        "osascript",
    }
)
# Language package managers: the subcommands that install, remove or publish.
_DENIED_SUBCOMMANDS: Mapping[str, frozenset[str]] = {
    "pip": frozenset({"install", "uninstall", "download", "wheel"}),
    "pip3": frozenset({"install", "uninstall", "download", "wheel"}),
    "pipx": frozenset({"install", "uninstall", "inject", "upgrade", "reinstall", "run"}),
    "uv": frozenset({"pip", "add", "remove", "sync", "lock", "tool", "python", "publish"}),
    "poetry": frozenset({"add", "install", "remove", "update", "lock", "publish"}),
    "npm": frozenset(
        {"install", "i", "ci", "add", "uninstall", "remove", "rm", "update", "publish", "link"}
    ),
    "yarn": frozenset({"add", "install", "remove", "upgrade", "publish", "link"}),
    "pnpm": frozenset({"add", "install", "i", "remove", "rm", "update", "publish", "link"}),
    "cargo": frozenset({"install", "uninstall", "publish"}),
    "go": frozenset({"install", "get"}),
    "gem": frozenset({"install", "uninstall", "update"}),
    "bundle": frozenset({"install", "update", "add"}),
    "composer": frozenset({"install", "require", "update", "remove"}),
    "conda": frozenset({"install", "create", "remove", "update"}),
    "mamba": frozenset({"install", "create", "remove", "update"}),
}
# Package managers whose bare invocation installs.
_BARE_INSTALLERS = frozenset({"yarn", "bundle", "composer"})
# Git is replayed only for subcommands that read.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "ls-files", "rev-parse", "grep", "blame", "describe"}
)
_DENIED_PYTHON_MODULES = frozenset({"pip", "ensurepip"})
# Wrappers that run the rest of the argv; the wrapped program is what counts.
_WRAPPER_PROGRAMS = frozenset({"env", "timeout", "nice", "nohup", "time", "command", "stdbuf"})
_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_WRAPPER_ARGUMENT_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_MAX_WRAPPER_DEPTH = 4

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
# of thousands of files). They are outside the protected bytes, as in the
# workspace snapshot.
_LINKED_DIRECTORY_NAMES = frozenset({".venv", "venv", "node_modules", ".tox", ".nox"})

# macOS: deny IP traffic except loopback. Unix-domain sockets stay allowed.
_DARWIN_NETWORK_PROFILE = (
    "(version 1)(allow default)"
    "(deny network-outbound (remote ip))"
    '(allow network-outbound (remote ip "localhost:*"))'
    "(deny network-inbound (local ip))"
    '(allow network-inbound (local ip "localhost:*"))'
)
_ISOLATION_PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    """A transcript command that can be replayed, split into its parts."""

    transcript_command: str
    core_command: str
    argv: tuple[str, ...]
    env_delta: Mapping[str, str] = field(default_factory=dict)
    cwd_relative: str = "."


def _peel_shell_wrappers(command: str) -> str:
    text = command.strip()
    for _ in range(_MAX_WRAPPER_DEPTH):
        body = _shell_command_body(text)
        if body is None or body.strip() == text:
            break
        text = body.strip()
    return text


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


def replay_candidate(command: str, task_cwd: str | None) -> ReplayCandidate | None:
    """Parse one recorded transcript command into a replay candidate, or None."""
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
    )


def _program_name(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def replay_denied(argv: Sequence[str]) -> bool:
    """Return True when ``argv`` must never be replayed (see module docstring)."""
    parts = list(argv)
    for _ in range(_MAX_WRAPPER_DEPTH):
        if not parts:
            return True
        if _program_name(parts[0]) not in _WRAPPER_PROGRAMS:
            break
        parts = parts[1:]
        while parts and (
            parts[0].startswith("-")
            or _is_env_assignment(parts[0])
            or _WRAPPER_ARGUMENT_RE.fullmatch(parts[0])
        ):
            parts = parts[1:]
    else:
        return True
    if not parts:
        return True
    name = _program_name(parts[0])
    arguments = parts[1:]
    if name in _DENIED_PROGRAMS:
        return True
    if name in _SHELL_PROGRAMS and any(
        argument == "-c" or (argument[:1] == "-" and argument[1:2] != "-" and "c" in argument[1:])
        for argument in arguments
    ):
        # An inline shell program cannot be inspected; it is never replayed.
        return True
    if name == "git":
        return not arguments or arguments[0] not in _READ_ONLY_GIT_SUBCOMMANDS
    if _is_python_executable(name) and "-m" in arguments:
        module_index = arguments.index("-m") + 1
        if module_index < len(arguments) and arguments[module_index] in _DENIED_PYTHON_MODULES:
            return True
    denied = _DENIED_SUBCOMMANDS.get(name)
    if denied is None:
        return False
    subcommand = next((argument for argument in arguments if not argument.startswith("-")), None)
    if subcommand is None:
        return name in _BARE_INSTALLERS
    return subcommand in denied


_COMMAND_WORD_CHARACTERS = re.compile(r"[A-Za-z0-9_./:=+\-]")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _contains_command(claim: str, command: str) -> bool:
    """Return True when ``command`` occurs in ``claim`` at word boundaries."""
    start = claim.find(command)
    while start != -1:
        end = start + len(command)
        before_ok = start == 0 or not _COMMAND_WORD_CHARACTERS.match(claim[start - 1])
        after_ok = end == len(claim) or not _COMMAND_WORD_CHARACTERS.match(claim[end])
        if before_ok and after_ok:
            return True
        start = claim.find(command, start + 1)
    return False


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
) -> bool:
    """Return True when ``claim`` refers to this replayed command.

    Linked when the claim equals or contains the transcript command or its
    replayed core (whitespace-normalized, at word boundaries), or when the
    claim's single test target is one of the command's arguments.
    """
    text = _normalize(claim)
    if not text:
        return False
    for command in (transcript_command, core_command):
        normalized = _normalize(command)
        if normalized and (text == normalized or _contains_command(text, normalized)):
            return True
    target = _claim_test_target(claim)
    if target is None:
        return False
    executable = _strip_env_prefix(list(argv))
    return target in executable[1:]


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
            ):
                return True
    return False


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
    for message in reversed(bash_messages):
        for recorded in _runtime_message_command_values(message):
            candidate = replay_candidate(recorded, task_cwd)
            if candidate is None or replay_denied(candidate.argv):
                continue
            key = (
                candidate.cwd_relative,
                candidate.argv,
                tuple(sorted(candidate.env_delta.items())),
            )
            if key in seen:
                continue
            if any(
                claim_links_to_command(
                    claim,
                    transcript_command=candidate.transcript_command,
                    core_command=candidate.core_command,
                    argv=candidate.argv,
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


@functools.cache
def network_isolation_prefix() -> tuple[str, ...] | None:
    """Return the argv prefix that denies network access, or None.

    Probed once per process by running ``true`` under it; a platform without a
    working mechanism (Windows, a Linux host without unprivileged user
    namespaces, an already-sandboxed macOS process) gets None.
    """
    if sys.platform == "darwin":
        executable = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
        prefix: tuple[str, ...] = (executable, "-p", _DARWIN_NETWORK_PROFILE)
    elif sys.platform.startswith("linux"):
        executable = shutil.which("unshare") or ""
        prefix = (executable, "--user", "--map-root-user", "--net", "--")
    else:
        return None
    probe = shutil.which("true") or "/usr/bin/true"
    if not prefix[0] or not os.path.exists(prefix[0]):
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*prefix, probe],
            capture_output=True,
            timeout=_ISOLATION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if result.returncode == 0 else None


def copy_workspace(source: Path, destination: Path) -> bool:
    """Copy ``source`` into ``destination`` for a replay; False when over budget.

    Symlinks are copied as links; dependency trees are linked, not copied;
    version-control metadata and caches are skipped.
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
                return False
            status = os.lstat(path)
            if stat.S_ISLNK(status.st_mode):
                os.symlink(os.readlink(path), target / name)
            elif stat.S_ISREG(status.st_mode):
                total_bytes += status.st_size
                if total_bytes > MAX_COPY_BYTES:
                    return False
                shutil.copy2(path, target / name)
    return True


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
    isolation_prefix: tuple[str, ...] | None,
) -> CommandObservation | None:
    source = Path(workspace).resolve()
    scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="ouroboros-replay-"))
    try:
        copy_root = scratch / "workspace"
        if scratch.resolve().is_relative_to(source):
            # A copy inside the workspace would copy itself.
            return None
        copied = await asyncio.to_thread(copy_workspace, source, copy_root)
        if not copied:
            return None
        before = await asyncio.to_thread(protected_digest, copy_root)
        # Absolute workspace paths in the recorded command point at the copy.
        workspaces = (str(source), workspace.rstrip("/") or "/")
        argv = [
            _remap_workspace_path(token, workspaces, str(copy_root)) for token in candidate.argv
        ]
        env_delta = {
            key: _remap_workspace_path(value, workspaces, str(copy_root))
            for key, value in candidate.env_delta.items()
        }
        run = await run_with_shell(
            [*isolation_prefix, *argv] if isolation_prefix else argv,
            cwd=str((copy_root / candidate.cwd_relative).resolve()),
            env={**env, **env_delta} if env_delta else env,
            timeout_seconds=timeout_seconds,
        )
        if run.start_error is not None:
            return None
        after = await asyncio.to_thread(protected_digest, copy_root)
        mutated = any(after.get(path) != digest for path, digest in before.items())
        return CommandObservation(
            command=candidate.core_command,
            returncode=run.returncode,
            output_tail=run.output[-OUTPUT_TAIL_CHARS:],
            timed_out=run.timed_out,
            transcript_command=candidate.transcript_command,
            argv=candidate.argv,
            mutated=mutated,
            network_isolated=isolation_prefix is not None,
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
) -> tuple[CommandObservation, ...]:
    """Replay each candidate in its own fresh copy of ``workspace``."""
    if not candidates:
        return ()
    isolation_prefix = await asyncio.to_thread(network_isolation_prefix)
    observations: list[CommandObservation] = []
    for candidate in candidates[:MAX_REPLAYED_COMMANDS]:
        if replay_denied(candidate.argv):
            continue
        observation = await _replay_one(
            candidate,
            workspace=workspace,
            env=env,
            timeout_seconds=timeout_seconds,
            isolation_prefix=isolation_prefix,
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


__all__ = [
    "MAX_COPY_BYTES",
    "MAX_COPY_ENTRIES",
    "MAX_REPLAYED_COMMANDS",
    "REPLAY_OUTPUT_FILTERS",
    "ReplayCandidate",
    "claim_links_to_command",
    "copy_workspace",
    "network_isolation_prefix",
    "output_filter_core",
    "protected_digest",
    "replay_candidate",
    "replay_commands",
    "replay_denied",
    "replayed_command_supports_claim",
    "select_replay_candidates",
]
