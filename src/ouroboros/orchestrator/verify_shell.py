"""Resolve a real POSIX shell for orchestrator-run ``verify_command`` execution.

``_run_ac_verify_gate`` used to hand an AC's ``verify_command`` to
``asyncio.create_subprocess_shell``, which resolves to ``cmd.exe`` on native
Windows. Seed-authored verify commands are POSIX bash by contract (``&&``,
``grep``, single quotes, forward-slash paths), so that default silently ran
them under an interpreter that cannot parse them.

This module follows gajae-code's ``getShellConfig()`` pattern: **resolve a real
bash binary, never translate the command**. Command authors keep writing one
syntax; the orchestrator guarantees the interpreter that understands it, or
reports that this machine cannot verify the AC at all. The pass/fail signal
still comes from the exit code of the unmodified command — nothing here is
allowed to soften what "verified" means.

Bash, specifically — not "some POSIX shell". A ``sh`` substitute reads the same
text differently (``echo -e X`` prints ``X`` under bash and ``-e X`` under
``sh``), so its verdict is about a different command than the Seed declared.
The fallback for a machine without Bash is an explicit unavailable result, never
a second interpreter or a shell emulator.

Priority mirrors the existing ``get_goose_cli_path`` / ``get_pi_cli_path``
convention: env override -> config -> well-known locations -> PATH.
``OUROBOROS_VERIFY_BASH`` is an executable-path variable, so it is denied from
the untrusted project ``.env`` (see :mod:`ouroboros.config.untrusted_env`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import ntpath
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess

VERIFY_BASH_ENV_VAR = "OUROBOROS_VERIFY_BASH"

# Windows locations Git for Windows installs its bundled bash into. A user who
# has Git for Windows has a real bash even if it is not on PATH.
_GIT_BASH_RELATIVE = PureWindowsPath("Git") / "bin" / "bash.exe"
_GIT_BASH_PROGRAM_FILES_VARS = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")


@dataclass(frozen=True, slots=True)
class VerifyShellRoute:
    """A concrete interpreter chosen to run ``verify_command`` verbatim.

    Bash only. A POSIX ``sh`` reads the same command differently — `echo -e X`
    prints `X` under bash and `-e X` under `sh`, so a contract asserting on
    `-e` fails under the shell it was written for and passes under the
    substitute. A machine without Bash records verification as unavailable.

    ``shell_path`` is always absolute: the path checked during resolution must
    be the path launched, and the gate launches with ``cwd`` set to the
    verification workspace.
    """

    shell_path: str
    source: str

    def argv(self, command: str) -> tuple[str, ...]:
        """Return the exact argv that runs ``command`` unmodified."""
        return (self.shell_path, "-c", command)


def _running_on_windows() -> bool:
    """Read the platform through one seam.

    Tests cannot monkeypatch ``os.name`` globally: that flips ``pathlib.Path``
    to ``WindowsPath``, which refuses to instantiate on POSIX and breaks every
    unrelated path this call touches. Windows branches are exercised by
    patching this function instead.
    """
    return os.name == "nt"


def _is_absolute(path: str) -> bool:
    """Whether ``path`` is absolute on the platform resolution is running on."""
    if _running_on_windows():
        return PureWindowsPath(path).is_absolute()
    return os.path.isabs(path)


def _canonical_path(path: str) -> str:
    """Return one canonical executable path on real and simulated platforms."""
    if _running_on_windows():
        normalized = ntpath.normpath(path)
        return os.path.realpath(normalized) if os.name == "nt" else normalized
    return os.path.realpath(path)


def _executable(candidate: str | None) -> str | None:
    """Return an executable **absolute** path for ``candidate``, else ``None``.

    The absolute requirement is the point, not tidiness. The gate launches the
    resolved shell with ``cwd`` set to the verification workspace, so a
    relative result — from an override like ``./tools/bash``, or from a
    relative ``PATH`` entry — is checked here against one directory and
    executed there against another. A workspace can then supply the binary
    that judges its own acceptance criteria. Nothing relative is repaired by
    guessing which directory was meant: it is refused, and resolution falls
    through to the next candidate.

    Stale env/config values that no longer point at an executable are treated
    as missing for the same reason — the same tolerance ``get_goose_cli_path``
    applies.
    """
    if not candidate:
        return None
    expanded = str(Path(candidate).expanduser())
    resolved = shutil.which(expanded)
    if resolved is None or not _is_absolute(resolved):
        return None
    canonical = _canonical_path(resolved)
    if not _is_absolute(canonical):
        return None
    return canonical


def _config_value() -> str | None:
    """Read ``orchestrator.verify_bash_path`` without the environment override.

    The env-first accessor would return the same stale ``OUROBOROS_VERIFY_BASH``
    this function is called to look past, so a configured shell would never be
    reached on a machine whose override has gone bad.
    """
    try:
        from ouroboros.config.loader import get_configured_verify_bash_path
    except ImportError:  # pragma: no cover - loader is always importable in-tree
        return None
    return get_configured_verify_bash_path()


def _configured_candidate() -> tuple[str, str] | None:
    env_value = os.environ.get(VERIFY_BASH_ENV_VAR, "").strip()
    resolved = _validated_executable(env_value)
    if resolved:
        return resolved, "env"

    resolved = _validated_executable(_config_value())
    if resolved:
        return resolved, "config"
    return None


def _windows_candidates() -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for variable in _GIT_BASH_PROGRAM_FILES_VARS:
        program_files = os.environ.get(variable, "").strip()
        if program_files:
            candidates.append(
                (str(PureWindowsPath(program_files) / _GIT_BASH_RELATIVE), "git_bash")
            )
    # Cygwin / MSYS2 installs that put a real bash on PATH. `%SystemRoot%`
    # entries are filtered below: `System32\bash.exe` is the WSL launcher, not
    # a Windows-side shell.
    candidates.append(("bash.exe", "path"))
    candidates.append(("bash", "path"))
    return tuple(candidates)


def _is_wsl_launcher(resolved_path: str) -> bool:
    """Recognize ``%SystemRoot%/System32/bash.exe``, which is not a local shell.

    That binary hands the command to a Linux distribution with its own root
    filesystem: the Windows ``cwd`` the gate passes has no meaning inside it,
    and the command would judge a different tree — or fail for reasons that
    have nothing to do with the AC. Both outcomes are worse than saying this
    machine cannot verify, so it is never used as a verify shell.
    """
    system_root = os.environ.get("SYSTEMROOT", "").strip() or "C:\\Windows"
    try:
        candidate = PureWindowsPath(resolved_path)
        return candidate.name.lower() == "bash.exe" and candidate.is_relative_to(
            PureWindowsPath(system_root) / "System32"
        )
    except ValueError:
        return False


def _validated_executable(candidate: str | None) -> str | None:
    """Resolve one candidate and reject every unsafe Windows launcher."""
    resolved = _executable(candidate)
    if resolved is None:
        return None
    if _running_on_windows() and _is_wsl_launcher(resolved):
        return None
    return resolved


_VERIFY_SHELL_IDENTITY_KEYS = frozenset({"path", "realpath", "sha256"})


def _sha256_file(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as shell_file:
            for chunk in iter(lambda: shell_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


_BASH_CAPABILITY_CACHE: dict[tuple[str, str], bool] = {}


_BASH_CAPABILITY_PROBE = '[[ -n "${BASH_VERSION:-}" ]] || exit 96; exit 37'


def _executes_bash_c_semantics(path: str, digest: str) -> bool:
    """Prove one executable implements the Bash ``-c`` contract.

    This is a fixed capability handshake executed directly through the OS,
    never a command parser, translator, fallback shell, or emulator. Cache it
    by content digest so repeated AC gates do not spawn a second probe for the
    same sealed executable.
    """
    cache_key = (path, digest)
    if cache_key in _BASH_CAPABILITY_CACHE:
        return _BASH_CAPABILITY_CACHE[cache_key]
    try:
        completed = subprocess.run(
            [path, "-c", _BASH_CAPABILITY_PROBE],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=sanitized_verify_environment(),
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = False
    else:
        result = completed.returncode == 37
    _BASH_CAPABILITY_CACHE[cache_key] = result
    return result


def capture_verify_shell_identity(route: VerifyShellRoute) -> dict[str, str] | None:
    """Seal the canonical executable and its content, not a mutable alias."""
    realpath = _executable(route.shell_path)
    if realpath is None or (_running_on_windows() and _is_wsl_launcher(realpath)):
        return None
    digest = _sha256_file(realpath)
    if digest is None or not _executes_bash_c_semantics(realpath, digest):
        return None
    return {"path": route.shell_path, "realpath": realpath, "sha256": digest}


def verify_shell_path_from_identity(identity: object) -> str | None:
    """Return the sealed executable only while target and content still match."""
    if not isinstance(identity, Mapping) or set(identity) != _VERIFY_SHELL_IDENTITY_KEYS:
        return None
    path = identity.get("path")
    expected_realpath = identity.get("realpath")
    expected_digest = identity.get("sha256")
    if not all(
        isinstance(value, str) and value for value in (path, expected_realpath, expected_digest)
    ):
        return None
    assert isinstance(path, str)
    assert isinstance(expected_realpath, str)
    assert isinstance(expected_digest, str)
    current_realpath = _executable(path)
    if current_realpath != expected_realpath:
        return None
    if _running_on_windows() and _is_wsl_launcher(current_realpath):
        return None
    current_digest = _sha256_file(current_realpath)
    if current_digest != expected_digest:
        return None
    if not _executes_bash_c_semantics(current_realpath, current_digest):
        return None
    return current_realpath


def _posix_candidates() -> tuple[tuple[str, str], ...]:
    return (
        ("/bin/bash", "posix_default"),
        ("/usr/bin/bash", "posix_default"),
        ("bash", "path"),
    )


def _resolve_uncached() -> VerifyShellRoute | None:
    configured = _configured_candidate()
    if configured is not None:
        shell_path, source = configured
        return VerifyShellRoute(shell_path=shell_path, source=source)

    windows = _running_on_windows()
    candidates = _windows_candidates() if windows else _posix_candidates()
    for candidate, source in candidates:
        resolved = _validated_executable(candidate)
        if resolved:
            return VerifyShellRoute(shell_path=resolved, source=source)
    return None


_ROUTE_CACHE: dict[tuple[str, ...], VerifyShellRoute] = {}


def _cache_key() -> tuple[str, ...]:
    """Fingerprint *every* input resolution reads.

    Resolution is deterministic in these values, so one lookup per ``ooo run``
    is enough — but only if the key names all of them. A key that omitted the
    configured path, the Git-for-Windows install roots or ``%SystemRoot%``
    would keep serving the first route it ever resolved after those changed,
    which is exactly the stale interpreter the cache is supposed to avoid.
    """
    return (
        "nt" if _running_on_windows() else "posix",
        os.environ.get(VERIFY_BASH_ENV_VAR, "").strip(),
        _config_value() or "",
        os.environ.get("PATH", ""),
        os.environ.get("SYSTEMROOT", ""),
        *(os.environ.get(variable, "") for variable in _GIT_BASH_PROGRAM_FILES_VARS),
    )


def resolve_verify_shell() -> VerifyShellRoute | None:
    """Return the interpreter that runs ``verify_command``, or ``None``.

    ``None`` means this machine has no POSIX-compatible interpreter *right
    now*. Callers must treat that as unverifiable — never as a pass.

    Only a successful resolution is cached. A failure is re-resolved on every
    call, because the whole point of retrying an unverifiable AC is that the
    operator can install Git Bash or set ``OUROBOROS_VERIFY_BASH`` while the
    run continues. Git Bash is found through ``%ProgramFiles%``, not ``PATH``,
    so a cached "no shell" keyed on ``PATH`` would never notice the install
    and would defeat the retry it exists to serve.
    """
    key = _cache_key()
    cached = _ROUTE_CACHE.get(key)
    if cached is not None:
        return cached
    route = _resolve_uncached()
    if route is not None:
        _ROUTE_CACHE[key] = route
    return route


def reset_verify_shell_cache() -> None:
    """Drop the resolution cache (tests, and env changes inside one process)."""
    _ROUTE_CACHE.clear()
    _BASH_CAPABILITY_CACHE.clear()


def verify_shell_unavailable_reason() -> str:
    """Explain the terminal case in a way the operator can act on."""
    if _running_on_windows():
        return (
            "verify_command needs a POSIX shell: no bash found. Install Git for "
            "Windows (which bundles bash) or set "
            f"{VERIFY_BASH_ENV_VAR} to a bash executable."
        )
    return (
        "verify_command needs bash: none found on this machine. Install bash "
        f"or set {VERIFY_BASH_ENV_VAR} to a bash executable (an absolute path)."
    )


# Environment variables that can change a verify_command's verdict without
# changing the workspace or the command. They are stripped from the gate's
# subprocess so the verdict is computed by the tools the orchestrator meant,
# with the configuration the Seed declared.
#
# `PYTHONPATH` and `PYTEST_ADDOPTS` are the sharp ones and are *not* covered by
# the project-`.env` denylist (`config/untrusted_env.py`), which blocks the
# `LD_`/`DYLD_` families and package-manager prefixes but neither of these. A
# repository's own `.env` could therefore set `PYTEST_ADDOPTS="-k nothing"` and
# every pytest-shaped contract in that repo would pass having run no tests.
#
# `PATH` is deliberately NOT stripped: verify commands resolve real tools
# (`uv`, `pytest`, `node`) through it, and a project-local `.venv/bin` entry is
# both common and legitimate. The in-workspace channels this leaves open —
# `conftest.py`, `pytest.ini`, a workspace-owned venv — are not env problems
# and cannot be closed here; they need a differential probe.
VERIFY_ENV_STRIPPED_KEYS = frozenset(
    {
        # Python import-path and startup injection.
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        # pytest argument and plugin injection: `-k`, `-p`, `--exitfirst` and
        # friends can turn any test command into a no-op that exits 0.
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        # Node preload hook, the same class of risk for JS-shaped contracts.
        "NODE_OPTIONS",
        # Shell startup files. Bash sources `BASH_ENV` before it evaluates a
        # `-c` command, and a POSIX shell does the same with `ENV`, so a file
        # containing `exit 0` turns `bash -c 'exit 23'` into a pass. This is
        # the sharpest of the lot: it does not bend a tool's configuration, it
        # runs the repository's code inside the gate itself.
        "BASH_ENV",
        "ENV",
        # Shell option state, which reaches the command the same way. Both
        # carry `set`/`shopt` flags into the child — `xtrace` writes into the
        # combined output an assertion is checked against, `errexit` changes
        # which leg of a chain decides the status, and `xpg_echo` changes what
        # `echo` prints.
        "SHELLOPTS",
        "BASHOPTS",
        "BASH_XTRACEFD",
        "BASH_COMPAT",
        "PS4",
        # Word splitting, path search for `cd`, and glob suppression: each
        # changes what the command means without changing its text.
        "IFS",
        "CDPATH",
        "GLOBIGNORE",
    }
)

# Dynamic-loader preload families, stripped by prefix for the same reason the
# untrusted-env policy rejects them by prefix: the member names vary by
# platform and new ones appear.
# `BASH_FUNC_` carries exported shell *functions*, which a `-c` command
# resolves before any executable of the same name: one named `pytest` or `git`
# replaces the tool the contract meant to run.
VERIFY_ENV_STRIPPED_PREFIXES = ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_FUNC_")


def sanitized_verify_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment a ``verify_command`` may be judged in.

    The gate inherits the orchestrator's environment, which in turn may carry
    values a project `.env` or the operator's shell put there. Anything able to
    bend the verdict from outside the workspace is removed; everything a real
    tool needs to run is kept.
    """
    source = os.environ if base is None else base
    return {
        key: value
        for key, value in source.items()
        if key.upper() not in VERIFY_ENV_STRIPPED_KEYS
        and not key.upper().startswith(VERIFY_ENV_STRIPPED_PREFIXES)
    }
