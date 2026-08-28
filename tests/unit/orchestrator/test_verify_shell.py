"""Tests for POSIX-shell resolution behind the AC verify gate (RFC Part A).

Windows branches are covered without Windows CI by patching the platform seam,
``os.environ`` and ``shutil.which`` — the resolver reads nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from ouroboros.orchestrator import verify_shell
from ouroboros.orchestrator.verify_shell import (
    VERIFY_BASH_ENV_VAR,
    VerifyShellRoute,
    capture_verify_shell_identity,
    resolve_verify_shell,
    verify_shell_path_from_identity,
    verify_shell_unavailable_reason,
)


@pytest.fixture(autouse=True)
def _clear_route_cache() -> Any:
    verify_shell.reset_verify_shell_cache()
    yield
    verify_shell.reset_verify_shell_cache()


def _which_over(available: set[str]) -> Any:
    def fake_which(candidate: str, *args: Any, **kwargs: Any) -> str | None:
        return candidate if candidate in available else None

    return fake_which


def _which_always(resolved: str) -> Any:
    def fake_which(candidate: str, *args: Any, **kwargs: Any) -> str:
        return resolved

    return fake_which


def _which_echoing() -> Any:
    """Model a machine where every candidate path exists as given."""

    def fake_which(candidate: str, *args: Any, **kwargs: Any) -> str:
        return candidate

    return fake_which


def _which_resolving(names: set[str], resolved: str) -> Any:
    """Model the real `which`: a bare name resolves to a full path."""

    def fake_which(candidate: str, *args: Any, **kwargs: Any) -> str | None:
        return resolved if candidate in names else None

    return fake_which


def test_route_argv_runs_the_command_verbatim() -> None:
    route = VerifyShellRoute(shell_path="/bin/bash", source="posix_default")

    assert route.argv("grep -q ok out.txt && exit 0") == (
        "/bin/bash",
        "-c",
        "grep -q ok out.txt && exit 0",
    )


def test_env_override_wins_over_every_discovered_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom-bash"
    override.write_text("")
    monkeypatch.setenv(VERIFY_BASH_ENV_VAR, str(override))
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({str(override), "/bin/bash"}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == str(override)
    assert route.source == "env"


def test_stale_env_override_falls_through_instead_of_dead_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.setenv(VERIFY_BASH_ENV_VAR, "/nonexistent/bash")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"/bin/bash"}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == os.path.realpath("/bin/bash")
    assert route.source == "posix_default"


def test_posix_prefers_bash_over_sh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"/bin/bash", "/bin/sh"}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == os.path.realpath("/bin/bash")


def test_posix_never_substitutes_sh_for_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sh` reads the same text differently, so a machine with only `sh`
    has no verification shell and records the AC as unavailable."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell, "_config_value", lambda: None)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"/bin/sh", "/usr/bin/sh"}))

    assert resolve_verify_shell() is None


def test_windows_resolves_git_bash_when_nothing_is_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    git_bash = str(PureWindowsPath(r"C:\Program Files") / "Git" / "bin" / "bash.exe")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({git_bash}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == git_bash
    assert route.source == "git_bash"


def test_windows_falls_back_to_a_bash_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    # A real `which` returns the PATH directory it found the name in; a bare
    # name would be refused, since the gate launches from the workspace.
    monkeypatch.setattr(
        verify_shell.shutil,
        "which",
        _which_resolving({"bash.exe"}, "C:\\msys64\\usr\\bin\\bash.exe"),
    )

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == "C:\\msys64\\usr\\bin\\bash.exe"
    assert route.source == "path"


def test_windows_never_falls_back_to_cmd_or_sh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"cmd.exe", "powershell.exe"}))

    assert resolve_verify_shell() is None
    assert "Git for Windows" in verify_shell_unavailable_reason()


def test_resolution_is_cached_per_path_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    calls: list[str] = []

    def counting_which(candidate: str, *args: Any, **kwargs: Any) -> str | None:
        calls.append(candidate)
        return "/bin/bash" if candidate == "/bin/bash" else None

    monkeypatch.setattr(verify_shell.shutil, "which", counting_which)

    assert resolve_verify_shell() is not None
    first_call_count = len(calls)
    assert resolve_verify_shell() is not None
    assert len(calls) == first_call_count

    # A changed PATH must not serve the previous machine's answer.
    monkeypatch.setenv("PATH", "/usr/bin:/opt/bin")
    assert resolve_verify_shell() is not None
    assert len(calls) > first_call_count


def test_windows_never_routes_verification_through_the_wsl_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`System32\\bash.exe` runs the command against a different filesystem."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    wsl = str(PureWindowsPath(r"C:\Windows") / "System32" / "bash.exe")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_always(wsl))

    assert resolve_verify_shell() is None


@pytest.mark.parametrize("source", ["env", "config"])
def test_windows_rejects_wsl_launcher_from_configured_routes(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    wsl = str(PureWindowsPath(r"C:\Windows") / "System32" / "bash.exe")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_always(wsl))
    if source == "env":
        monkeypatch.setenv(VERIFY_BASH_ENV_VAR, wsl)
        monkeypatch.setattr(verify_shell, "_config_value", lambda: None)
    else:
        monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
        monkeypatch.setattr(verify_shell, "_config_value", lambda: wsl)

    assert resolve_verify_shell() is None


def test_shell_identity_rejects_retarget_and_content_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bash"
    target.write_bytes(b"bash-v1")
    target.chmod(0o755)
    alias = tmp_path / "alias-bash"
    try:
        os.symlink(target, alias)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    monkeypatch.setattr(verify_shell, "_executes_bash_c_semantics", lambda _path, _digest: True)

    identity = capture_verify_shell_identity(VerifyShellRoute(str(alias), "config"))
    assert identity is not None
    assert identity["realpath"] == str(target)
    assert verify_shell_path_from_identity(identity) == str(target)

    target.write_bytes(b"bash-v2")
    assert verify_shell_path_from_identity(identity) is None


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX executable scripts")
def test_shell_identity_rejects_executable_that_ignores_bash_arguments(
    tmp_path: Path,
) -> None:
    impostor = tmp_path / "bash"
    impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    impostor.chmod(0o755)

    identity = capture_verify_shell_identity(VerifyShellRoute(str(impostor), "config"))

    assert identity is None


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX executable scripts")
def test_persisted_identity_rejects_executable_without_bash_semantics(
    tmp_path: Path,
) -> None:
    impostor = tmp_path / "bash"
    impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    impostor.chmod(0o755)
    realpath = str(impostor.resolve())
    digest = verify_shell._sha256_file(realpath)
    assert digest is not None

    resolved = verify_shell_path_from_identity(
        {"path": realpath, "realpath": realpath, "sha256": digest}
    )

    assert resolved is None


@pytest.mark.skipif(os.name == "nt", reason="uses the local POSIX Bash installation")
def test_shell_identity_accepts_real_bash() -> None:
    route = resolve_verify_shell()
    if route is None:
        pytest.skip("bash is not installed")

    identity = capture_verify_shell_identity(route)

    assert identity is not None


def test_windows_wsl_alias_is_rejected_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    alias = r"C:\Windows\Temp\..\System32\bash.exe"
    monkeypatch.setattr(verify_shell.shutil, "which", _which_always(alias))

    assert resolve_verify_shell() is None


def test_windows_still_accepts_a_real_bash_outside_system32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    msys = str(PureWindowsPath(r"C:\msys64\usr\bin") / "bash.exe")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_resolving({"bash.exe", "bash"}, msys))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == msys


def test_a_failed_resolution_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying an unverifiable AC only helps if a mid-run fix is noticed.

    Git Bash is discovered through `%ProgramFiles%`, not PATH, so a cached
    "no shell" keyed on PATH would survive the very install it should pick up.
    """
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    available: set[str] = set()
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over(available))

    assert resolve_verify_shell() is None

    # The operator installs bash while the run continues.
    available.add("/bin/bash")

    route = resolve_verify_shell()
    assert route is not None
    assert route.shell_path == os.path.realpath("/bin/bash")


def test_a_successful_resolution_is_still_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    calls: list[str] = []

    def counting_which(candidate: str, *args: Any, **kwargs: Any) -> str | None:
        calls.append(candidate)
        return "/bin/bash" if candidate == "/bin/bash" else None

    monkeypatch.setattr(verify_shell.shutil, "which", counting_which)

    assert resolve_verify_shell() is not None
    settled = len(calls)
    assert resolve_verify_shell() is not None
    assert len(calls) == settled


# ---------------------------------------------------------------------------
# The environment a verdict may be computed in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "NODE_OPTIONS",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    ],
)
def test_verdict_bending_variables_are_stripped(key: str) -> None:
    """`PYTEST_ADDOPTS="--collect-only"` makes any pytest contract exit 0."""
    sanitized = verify_shell.sanitized_verify_environment({key: "hostile", "HOME": "/home/u"})

    assert key not in sanitized
    assert sanitized["HOME"] == "/home/u"


def test_path_is_kept_because_contracts_need_real_tools() -> None:
    """Stripping PATH would break `uv`/`pytest`, including a project `.venv/bin`."""
    sanitized = verify_shell.sanitized_verify_environment({"PATH": "/w/.venv/bin:/usr/bin"})

    assert sanitized["PATH"] == "/w/.venv/bin:/usr/bin"


def test_stripping_is_case_insensitive() -> None:
    sanitized = verify_shell.sanitized_verify_environment({"pytest_addopts": "-p no:randomly"})

    assert sanitized == {}


def test_stale_env_override_does_not_mask_a_valid_configured_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-first accessor would return the same stale value, so a machine
    with no default bash would report "cannot verify" despite valid config."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.setenv(VERIFY_BASH_ENV_VAR, "/nonexistent/bash")
    monkeypatch.setattr(verify_shell, "_config_value", lambda: "/opt/bash")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"/opt/bash"}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == "/opt/bash"
    assert route.source == "config"


def test_resolution_cache_notices_a_changed_configured_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config is a resolution input, so it must be in the cache fingerprint."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"/a/bash", "/b/bash"}))

    monkeypatch.setattr(verify_shell, "_config_value", lambda: "/a/bash")
    first = resolve_verify_shell()
    assert first is not None and first.shell_path == "/a/bash"

    monkeypatch.setattr(verify_shell, "_config_value", lambda: "/b/bash")
    second = resolve_verify_shell()

    assert second is not None
    assert second.shell_path == "/b/bash"


def test_resolution_cache_notices_a_changed_git_bash_install_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git Bash is found through `%ProgramFiles%`, never PATH: a key without it
    keeps serving the route resolved before the install moved."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: True)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell, "_config_value", lambda: None)
    monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_echoing())

    first = resolve_verify_shell()
    assert first is not None
    assert first.shell_path.startswith("C:\\Program Files")

    monkeypatch.setenv("ProgramFiles", "D:\\Apps")
    second = resolve_verify_shell()

    assert second is not None
    assert second.shell_path.startswith("D:\\Apps")


def test_a_relative_override_is_refused_not_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate launches with `cwd` set to the verification workspace, so a
    relative path names one binary at resolution and another at launch — the
    workspace could supply the shell that judges its own criteria."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.setenv(VERIFY_BASH_ENV_VAR, "./tools/bash")
    monkeypatch.setattr(verify_shell, "_config_value", lambda: None)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"./tools/bash", "/bin/bash"}))

    route = resolve_verify_shell()

    assert route is not None
    assert route.shell_path == os.path.realpath("/bin/bash")
    assert route.source == "posix_default"


def test_a_relative_configured_shell_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell, "_config_value", lambda: "tools/bash")
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"tools/bash"}))

    assert resolve_verify_shell() is None


def test_a_relative_path_entry_cannot_supply_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PATH=./bin` makes `which` hand back a relative path for a bare name."""
    monkeypatch.setattr(verify_shell, "_running_on_windows", lambda: False)
    monkeypatch.delenv(VERIFY_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(verify_shell, "_config_value", lambda: None)
    monkeypatch.setattr(verify_shell.shutil, "which", _which_over({"./bin/bash"}))

    assert resolve_verify_shell() is None


@pytest.mark.parametrize(
    "key",
    ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "PS4", "IFS", "CDPATH"],
)
def test_shell_startup_and_option_controls_are_stripped(key: str) -> None:
    """Bash sources `BASH_ENV` before evaluating a `-c` command, so a file
    holding `exit 0` would turn every failing contract into a pass."""
    assert key not in verify_shell.sanitized_verify_environment(
        {key: "/tmp/injected.sh", "PATH": "/usr/bin"}
    )


def test_exported_shell_functions_are_stripped() -> None:
    """A function named `pytest` resolves before the executable of that name."""
    sanitized = verify_shell.sanitized_verify_environment(
        {"BASH_FUNC_pytest%%": "() { exit 0; }", "PATH": "/usr/bin"}
    )

    assert list(sanitized) == ["PATH"]
