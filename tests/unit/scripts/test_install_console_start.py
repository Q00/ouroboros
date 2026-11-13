"""The installers start the Ouroboros console after a successful install.

`scripts/install.sh` and `scripts/install.ps1` end by starting `ouroboros gui`
detached when the session is interactive with a desktop, print only the
command otherwise, and stay silent for releases without the command. Every
case runs against stub commands under a temporary HOME; nothing here installs
a package or starts a real server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import time

import pytest

from tests.unit.scripts.test_install_runtime_selection import (
    CALLS_LOG_VAR,
    INSTALL_SH,
    OUROBOROS_STUB_VAR,
    _build_no_python3_path,
    _cached_executable,
    _extract_function,
    _installer_system_path,
    _run_installer,
)

INSTALL_PS1 = INSTALL_SH.parent / "install.ps1"

# Variables from the developer's or CI's environment that decide whether the
# console starts. Every test starts from none of them and sets what it needs.
_SESSION_VARS = (
    "OUROBOROS_NO_CONSOLE",
    "CI",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "BROWSER",
)
# Keep installer output free of ANSI codes so lines compare exactly.
_COLOR_VARS = ("FORCE_COLOR",)

# Test-only knobs read by the stubs below, never by the installers.
GUI_VERSION_VAR = "OUROBOROS_TEST_GUI"  # "new" (default), "old", "hang", "stubborn"
GUI_START_VAR = "OUROBOROS_TEST_GUI_START"  # "serve" (default), "fail", "reuse", "slow"
UNAME_VAR = "OUROBOROS_TEST_UNAME"

# `ouroboros` stand-in. `gui --help` answers like a release with or without
# the console. `gui` writes the console's state file the way the real launcher
# does, then outlives the installer so the test can see it survive the
# terminal going away.
_GUI_STUB = f"""#!/bin/sh
printf 'ouroboros %s\\n' "$*" >> "${CALLS_LOG_VAR}"
if [ "$1" = gui ] && [ "$2" = --help ]; then
  printf 'probe telemetry=%s dnt=%s\\n' "${{OUROBOROS_TELEMETRY:-}}" "${{DO_NOT_TRACK:-}}" >> "${CALLS_LOG_VAR}"
  case "${{{GUI_VERSION_VAR}:-new}}" in
    old) echo "No such command 'gui'." >&2; exit 2 ;;
    hang) sleep 15 ;;
    stubborn)
      printf 'probe pid=%s\\n' "$$" >> "${CALLS_LOG_VAR}"
      trap '' TERM
      sleep 8
      printf 'probe ignored TERM and finished\\n' >> "${CALLS_LOG_VAR}"
      ;;
  esac
  exit 0
fi
if [ "$1" = gui ] && [ "$#" -eq 1 ]; then
  printf 'start browser=%s\\n' "${{BROWSER:-}}" >> "${CALLS_LOG_VAR}"
  echo "console stdout line"
  echo "console stderr line" >&2
  case "${{{GUI_START_VAR}:-serve}}" in
    fail) exit 3 ;;
    reuse) exit 0 ;;
    slow) sleep 15; exit 0 ;;
  esac
  mkdir -p "$HOME/.ouroboros/gui"
  printf '{{"host": "127.0.0.1", "port": 10300, "token": "t%s", "pid": %s}}' "$$" "$$" \\
    > "$HOME/.ouroboros/gui/server.json.tmp"
  mv "$HOME/.ouroboros/gui/server.json.tmp" "$HOME/.ouroboros/gui/server.json"
  sleep 3
  : > "$HOME/console-survived"
  exit 0
fi
exit 0
"""

_UNAME_STUB = f"""#!/bin/sh
if [ "$1" = "-s" ]; then
  echo "${{{UNAME_VAR}:-Darwin}}"
  exit 0
fi
exec /usr/bin/uname "$@"
"""

_PYTHON3_WRAPPER = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'

_SETSID_STUB = f"""#!/bin/sh
printf 'setsid %s\\n' "$*" >> "${CALLS_LOG_VAR}"
exec "$@"
"""


def _run(
    tmp_path: Path,
    *,
    terminal: bool,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
    piped: bool = True,
    extra_commands: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    commands = {
        "ouroboros": _GUI_STUB,
        "uname": _UNAME_STUB,
        "python3": _PYTHON3_WRAPPER,
        **(extra_commands or {}),
    }
    run_env = {
        OUROBOROS_STUB_VAR: str(_cached_executable(_GUI_STUB)),
        "NO_COLOR": "1",
        **(env or {}),
    }
    return _run_installer(
        tmp_path,
        piped=piped,
        terminal=terminal,
        args=args,
        env=run_env,
        drop_env=tuple(key for key in (*_SESSION_VARS, *_COLOR_VARS) if key not in run_env),
        fake_commands=commands,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _started(tmp_path: Path) -> bool:
    return any(line.startswith("start ") for line in _calls(tmp_path))


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


NEXT_LINE = "Next: open the Ouroboros console in your browser with: ouroboros gui"


@pytest.mark.parametrize("piped", [True, False], ids=["curl-pipe", "script-file"])
def test_interactive_desktop_starts_console_detached(tmp_path: Path, piped: bool) -> None:
    result = _run(tmp_path, terminal=True, piped=piped)

    assert result.returncode == 0, result.stdout + result.stderr
    survived = tmp_path / "home" / "console-survived"
    # The installer returned while the console was still running...
    assert not survived.exists()
    assert "Done! Ouroboros is ready." in result.stdout
    assert "running in the background at http://localhost:10300/" in result.stdout
    assert 'Stop it with "Stop console" at the bottom of its sidebar.' in result.stdout
    assert "Reopen it any time with: ouroboros gui" in result.stdout
    assert NEXT_LINE not in result.stdout
    calls = _calls(tmp_path)
    assert "ouroboros gui --help" in calls
    assert "probe telemetry=0 dnt=1" in calls
    assert "ouroboros gui" in calls
    # ...with its output in the log, not in the installer's terminal...
    log = tmp_path / "home" / ".ouroboros" / "gui" / "console.log"
    assert "console stdout line" in log.read_text(encoding="utf-8")
    assert "console stderr line" in log.read_text(encoding="utf-8")
    assert "console stdout line" not in result.stdout + result.stderr
    # ...and it outlived the installer and its terminal (SIGHUP is ignored).
    assert _wait_for(survived, 15), "the console died with the installer's terminal"


def test_console_start_uses_setsid_when_available(tmp_path: Path) -> None:
    result = _run(tmp_path, terminal=True, extra_commands={"setsid": _SETSID_STUB})

    assert result.returncode == 0, result.stdout + result.stderr
    setsid_calls = [line for line in _calls(tmp_path) if line.startswith("setsid ")]
    assert len(setsid_calls) == 1
    assert setsid_calls[0].endswith("/ouroboros gui")
    assert "running in the background at http://localhost:10300/" in result.stdout


@pytest.mark.parametrize(
    "env",
    [
        {UNAME_VAR: "Linux", "DISPLAY": ":0"},
        {UNAME_VAR: "Linux", "WAYLAND_DISPLAY": "wayland-0"},
        {UNAME_VAR: "Linux", "DISPLAY": ":0", "SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"},
        {UNAME_VAR: "Linux", "WSL_DISTRO_NAME": "Ubuntu", "BROWSER": "explorer.exe"},
    ],
    ids=["x11", "wayland", "ssh-with-x11-forwarding", "wsl-browser-variable"],
)
def test_linux_desktops_start_console(tmp_path: Path, env: dict[str, str]) -> None:
    result = _run(tmp_path, terminal=True, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _started(tmp_path)
    assert "running in the background at http://localhost:10300/" in result.stdout


def test_wsl_bridge_points_the_browser_at_wslview(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        terminal=True,
        env={UNAME_VAR: "Linux", "WSL_DISTRO_NAME": "Ubuntu"},
        extra_commands={"wslview": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "start browser=wslview" in _calls(tmp_path)


@pytest.mark.parametrize(
    ("terminal", "env"),
    [
        (False, {}),
        (True, {"CI": "true"}),
        (True, {"CI": ""}),
        (True, {UNAME_VAR: "Linux"}),
        (True, {UNAME_VAR: "Linux", "WSL_DISTRO_NAME": "Ubuntu"}),
        (True, {"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}),
        (True, {"SSH_TTY": "/dev/ttys001", "DISPLAY": ":0"}),
        (True, {UNAME_VAR: "Linux", "SSH_CLIENT": "10.0.0.2 5 22", "BROWSER": "w3m"}),
    ],
    ids=[
        "no-terminal",
        "ci",
        "ci-set-empty",
        "linux-headless",
        "wsl-without-bridge",
        "macos-over-ssh",
        "macos-over-ssh-with-display",
        "linux-ssh-without-display",
    ],
)
def test_without_an_interactive_desktop_only_the_command_is_printed(
    tmp_path: Path, terminal: bool, env: dict[str, str]
) -> None:
    result = _run(tmp_path, terminal=terminal, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith(NEXT_LINE)
    assert "ouroboros gui --help" in _calls(tmp_path)
    assert not _started(tmp_path)
    assert not (tmp_path / "home" / ".ouroboros" / "gui").exists()


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--no-console",), {}),
        ((), {"OUROBOROS_NO_CONSOLE": "1"}),
        ((), {"OUROBOROS_NO_CONSOLE": " TRUE "}),
    ],
    ids=["flag", "variable", "variable-case-and-space"],
)
@pytest.mark.parametrize("piped", [True, False], ids=["curl-pipe", "script-file"])
def test_opt_out_skips_the_console_step(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], piped: bool
) -> None:
    result = _run(tmp_path, terminal=True, args=args, env=env, piped=piped)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(line.startswith("ouroboros gui") for line in _calls(tmp_path))
    assert "console" not in result.stdout.lower()


def test_unrecognized_opt_out_value_is_reported_and_skips(tmp_path: Path) -> None:
    result = _run(tmp_path, terminal=True, env={"OUROBOROS_NO_CONSOLE": "maybe"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OUROBOROS_NO_CONSOLE has an unrecognized value" in result.stdout
    assert not any(line.startswith("ouroboros gui") for line in _calls(tmp_path))


@pytest.mark.parametrize("value", ["0", "off", "False"])
def test_explicit_opt_in_values_start_the_console(tmp_path: Path, value: str) -> None:
    result = _run(tmp_path, terminal=True, env={"OUROBOROS_NO_CONSOLE": value})

    assert result.returncode == 0, result.stdout + result.stderr
    assert _started(tmp_path)


@pytest.mark.parametrize("terminal", [True, False], ids=["interactive", "non-interactive"])
def test_release_without_gui_stays_silent(tmp_path: Path, terminal: bool) -> None:
    result = _run(tmp_path, terminal=terminal, env={GUI_VERSION_VAR: "old"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ouroboros gui --help" in _calls(tmp_path)
    assert not _started(tmp_path)
    assert "console" not in result.stdout.lower()
    assert "ouroboros gui" not in result.stdout


def test_console_start_failure_keeps_install_successful(tmp_path: Path) -> None:
    result = _run(tmp_path, terminal=True, env={GUI_START_VAR: "fail"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done! Ouroboros is ready." in result.stdout
    assert "The Ouroboros console did not start (exit 3)." in result.stdout
    assert "Start it any time with: ouroboros gui" in result.stdout
    log = tmp_path / "home" / ".ouroboros" / "gui" / "console.log"
    assert f"Log: {log}" in result.stdout
    assert "console stderr line" in log.read_text(encoding="utf-8")


def _console_log(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".ouroboros" / "gui" / "console.log"


def test_new_console_log_is_private(tmp_path: Path) -> None:
    result = _run(tmp_path, terminal=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(_console_log(tmp_path).stat().st_mode) == 0o600


def test_existing_world_readable_log_is_made_private_before_appending(tmp_path: Path) -> None:
    log = _console_log(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("earlier run\n", encoding="utf-8")
    log.chmod(0o644)

    result = _run(tmp_path, terminal=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text(encoding="utf-8")
    assert text.startswith("earlier run\n")
    assert "console stdout line" in text


def test_log_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.log"
    target.write_text("keep\n", encoding="utf-8")
    target.chmod(0o644)
    log = _console_log(tmp_path)
    log.parent.mkdir(parents=True)
    log.symlink_to(target)

    result = _run(tmp_path, terminal=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running in the background at http://localhost:10300/" in result.stdout
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "Log:" not in result.stdout


def test_log_that_cannot_be_made_private_is_not_written(tmp_path: Path) -> None:
    log = _console_log(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("earlier run\n", encoding="utf-8")
    log.chmod(0o644)

    result = _run(tmp_path, terminal=True, extra_commands={"chmod": "#!/bin/sh\nexit 1\n"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running in the background at http://localhost:10300/" in result.stdout
    assert log.read_text(encoding="utf-8") == "earlier run\n"
    assert "Log:" not in result.stdout


def test_running_console_is_reused(tmp_path: Path) -> None:
    state = tmp_path / "home" / ".ouroboros" / "gui" / "server.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"host": "127.0.0.1", "port": 10301, "token": "x", "pid": 1}),
        encoding="utf-8",
    )

    result = _run(tmp_path, terminal=True, env={GUI_START_VAR: "reuse"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running at http://localhost:10301/; opened it in your browser." in (
        result.stdout
    )


def test_exit_zero_without_state_is_not_reported_as_running(tmp_path: Path) -> None:
    result = _run(tmp_path, terminal=True, env={GUI_START_VAR: "reuse"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not confirm that the Ouroboros console started." in result.stdout
    assert "running in the background" not in result.stdout


# --- the bounded waits and the state parser, driven directly -------------------

_OUTPUT_HELPERS = """
BOLD="" BLUE="" RESET=""
_say() { printf '%s\\n' "$*"; }
_blank() { printf '\\n'; }
_ok() { _say "OK $1"; }
_warn() { _say "WARN $1"; }
_info() { _say "INFO $1"; }
"""

_CONSOLE_FUNCTIONS = (
    "_console_opted_out",
    "_console_stop_child",
    "_console_run_bounded",
    "_console_supported",
    "_console_has_terminal",
    "_console_is_wsl",
    "_console_has_desktop",
    "_console_json_python",
    "_console_state_url",
    "_console_next_hint",
    "_console_log_hint",
    "_console_start",
    "_console_after_install",
)


def _state_parser_definition() -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("_CONSOLE_STATE_PARSER='")
    end = text.index("\n'\n", start) + 3
    return text[start:end]


def _drive(
    tmp_path: Path,
    body: str,
    env: dict[str, str] | None = None,
    path: str | None = None,
) -> tuple[str, float]:
    calls = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "ouroboros"
    stub.unlink(missing_ok=True)
    stub.symlink_to(_cached_executable(_GUI_STUB))
    script = "\n".join(
        [
            "set -euo pipefail",
            'NO_CONSOLE_FLAG=""',
            _OUTPUT_HELPERS,
            _state_parser_definition(),
            *(_extract_function(name) for name in _CONSOLE_FUNCTIONS),
            body,
        ]
    )
    run_env = {
        key: value
        for key, value in os.environ.items()
        if key not in _SESSION_VARS and not key.startswith("OUROBOROS_TEST_")
    }
    run_env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": path or f"{bin_dir}:{_python3_dir(tmp_path)}:{_installer_system_path()}",
            CALLS_LOG_VAR: str(calls),
            **(env or {}),
        }
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        start_new_session=True,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout, elapsed


def _python3_dir(tmp_path: Path) -> Path:
    """A directory whose `python3` is the test interpreter, for determinism."""
    directory = tmp_path / "python3-bin"
    if not directory.exists():
        directory.mkdir()
        (directory / "python3").symlink_to(_cached_executable(_PYTHON3_WRAPPER))
    return directory


def test_probe_that_hangs_is_killed_and_counts_as_unsupported(tmp_path: Path) -> None:
    out, elapsed = _drive(
        tmp_path,
        f"if _console_supported {shlex.quote(str(tmp_path / 'bin' / 'ouroboros'))} 5; "
        "then echo SUPPORTED; else echo UNSUPPORTED; fi",
        env={GUI_VERSION_VAR: "hang"},
    )

    assert out.strip() == "UNSUPPORTED"
    assert elapsed < 5


def test_probe_that_ignores_sigterm_is_killed_within_the_bound(tmp_path: Path) -> None:
    """0.5 s limit + at most 1 s after SIGTERM + at most 1 s after SIGKILL."""
    out, elapsed = _drive(
        tmp_path,
        f"if _console_supported {shlex.quote(str(tmp_path / 'bin' / 'ouroboros'))} 5; "
        "then echo SUPPORTED; else echo UNSUPPORTED $?; fi\n"
        f"pid=$(sed -n 's/^probe pid=//p' {shlex.quote(str(tmp_path / 'calls.log'))})\n"
        'if kill -0 "$pid" 2>/dev/null; then echo PROBE-ALIVE; else echo PROBE-GONE; fi',
        env={GUI_VERSION_VAR: "stubborn"},
    )

    assert out.split() == ["UNSUPPORTED", "124", "PROBE-GONE"]
    assert elapsed < 5


def test_console_still_starting_after_the_wait_is_reported_not_failed(tmp_path: Path) -> None:
    out, elapsed = _drive(
        tmp_path,
        f"_console_start {shlex.quote(str(tmp_path / 'bin' / 'ouroboros'))} 1\necho RETURNED",
        env={GUI_START_VAR: "slow"},
    )

    assert "INFO The Ouroboros console is still starting in the background" in out
    assert "WARN" not in out
    assert out.rstrip().endswith("RETURNED")
    assert elapsed < 6


def test_missing_ouroboros_command_prints_nothing(tmp_path: Path) -> None:
    out, _ = _drive(tmp_path, '_console_after_install ""\necho RETURNED')

    assert out.strip() == "RETURNED"
    assert not (tmp_path / "calls.log").exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"host": "127.0.0.1", "port": 10300, "token": "a", "pid": 1}', "http://localhost:10300/"),
        ('{"host": "localhost", "port": 1, "token": "a", "pid": 1}', "http://localhost:1/"),
        ('{"host": "0.0.0.0", "port": 65535, "token": "a", "pid": 1}', "http://localhost:65535/"),
        (' \n{"port": 10300, "host": "127.0.0.1"}\n', "http://localhost:10300/"),
        # Only the top-level fields count, never a nested copy.
        (
            '{"host":"192.168.1.5","port":123,"extra":{"host":"127.0.0.1","port":10300}}',
            "",
        ),
        ('{"extra":{"host":"127.0.0.1","port":10300}}', ""),
        # Trailing text makes the whole document invalid.
        ('{"host": "127.0.0.1", "port": 10300} junk', ""),
        ('{"host": "127.0.0.1", "port": 10300}}', ""),
        ('{"host": "127.0.0.1", "port": 10300} {"host": "x"}', ""),
        ('[{"host": "127.0.0.1", "port": 10300}]', ""),
        ('{"host": "127.0.0.1", "port": 0}', ""),
        ('{"host": "127.0.0.1", "port": 65536}', ""),
        ('{"host": "127.0.0.1", "port": 010300}', ""),
        ('{"host": "127.0.0.1", "port": "10300"}', ""),
        ('{"host": "127.0.0.1", "port": 10300.0}', ""),
        ('{"host": "127.0.0.1", "port": true}', ""),
        ('{"host": "192.168.1.5", "port": 10300}', ""),
        ('{"host": "LOCALHOST", "port": 10300}', ""),
        ('{"port": 10300}', ""),
        ("not json", ""),
        ("", ""),
    ],
)
def test_state_file_must_be_a_json_object_naming_a_loopback_url(
    tmp_path: Path, content: str, expected: str
) -> None:
    state = tmp_path / "server.json"
    state.write_text(content, encoding="utf-8")
    out, _ = _drive(
        tmp_path,
        f'_console_state_url "$(command -v python3)" {shlex.quote(str(state))} || true',
    )

    assert out.strip() == expected


def test_state_parser_refuses_non_regular_files_and_a_missing_parser(tmp_path: Path) -> None:
    fifo = tmp_path / "server.json"
    os.mkfifo(fifo)
    good = tmp_path / "good.json"
    good.write_text('{"host": "127.0.0.1", "port": 10300}', encoding="utf-8")
    out, elapsed = _drive(
        tmp_path,
        f'_console_state_url "$(command -v python3)" {shlex.quote(str(fifo))} || echo FIFO-REFUSED\n'
        f"_console_state_url '' {shlex.quote(str(good))} || echo NO-PARSER",
    )

    assert out.split() == ["FIFO-REFUSED", "NO-PARSER"]
    assert elapsed < 5


def test_unwritable_state_directory_still_returns(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".ouroboros").write_text("not a directory", encoding="utf-8")
    out, elapsed = _drive(
        tmp_path,
        f"_console_start {shlex.quote(str(tmp_path / 'bin' / 'ouroboros'))} 1\necho RETURNED",
    )

    assert "Log:" not in out
    assert "http://" not in out
    assert out.rstrip().endswith("RETURNED")
    assert elapsed < 6


def test_parser_prefers_the_interpreter_ouroboros_runs_on(tmp_path: Path) -> None:
    entry = tmp_path / "entry" / "ouroboros"
    entry.parent.mkdir()
    real = Path(os.path.realpath(sys.executable))
    entry.write_text(f"#!{real}\nraise SystemExit(0)\n", encoding="utf-8")
    entry.chmod(0o755)
    out, _ = _drive(tmp_path, f"_console_json_python {shlex.quote(str(entry))}")

    assert out == str(real)


@pytest.mark.parametrize(
    "shebang",
    ["#!/bin/sh", "#!/usr/bin/env python3", "#!/opt/my python/bin/python3"],
    ids=["shell-wrapper", "env-lookup", "path-with-space"],
)
def test_parser_falls_back_to_python3_without_running_other_interpreters(
    tmp_path: Path, shebang: str
) -> None:
    entry = tmp_path / "entry" / "ouroboros"
    entry.parent.mkdir()
    entry.write_text(f"{shebang}\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)
    out, _ = _drive(tmp_path, f"_console_json_python {shlex.quote(str(entry))}")

    assert out == str(_python3_dir(tmp_path) / "python3")


def test_without_any_python_the_start_is_reported_without_a_url(tmp_path: Path) -> None:
    no_python = _build_no_python3_path(tmp_path / "no-python")
    out, elapsed = _drive(
        tmp_path,
        f"_console_start {shlex.quote(str(tmp_path / 'bin' / 'ouroboros'))} 20\necho RETURNED",
        path=f"{tmp_path / 'bin'}:{no_python}",
    )

    # The stub did write a valid state file; without a parser it is not
    # trusted, and the wait is cut to a few seconds.
    assert (tmp_path / "home" / ".ouroboros" / "gui" / "server.json").exists()
    assert "http://" not in out
    assert "INFO The Ouroboros console is still starting in the background" in out
    assert out.rstrip().endswith("RETURNED")
    assert elapsed < 8


# --- install.ps1 --------------------------------------------------------------

_PS_LOAD = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:OOO_TEST_INSTALLER, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors -join '; ') }
foreach ($name in @('Test-ConsoleOptOut', 'Test-ConsoleCommand', 'Test-ConsoleDesktop',
                    'ConvertTo-ConsoleUrl', 'Read-ConsoleState', 'Start-ConsoleDetached',
                    'Invoke-ConsoleAfterInstall')) {
    $functions = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))
    if ($functions.Count -ne 1) { throw "Expected exactly one $name function" }
    . ([scriptblock]::Create($functions[0].Extent.Text))
}
# Host output, like the real helpers: writing to the pipeline instead would
# change what the functions under test return.
function Write-Ok([string]$Message) { Microsoft.PowerShell.Utility\Write-Host "OK $Message" }
function Write-Info([string]$Message) { Microsoft.PowerShell.Utility\Write-Host "INFO $Message" }
function Write-Warn([string]$Message) { Microsoft.PowerShell.Utility\Write-Host "WARN $Message" }
function Test-Interactive { return ($env:OOO_TEST_INTERACTIVE -eq '1') }
$script:started = @()
# Stand-in for the hidden background process: records the call, plays the
# requested outcome, and returns an object shaped like a Process.
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $RedirectStandardOutput,
          $RedirectStandardError, [switch]$PassThru)
    $script:started += , @($FilePath, ($ArgumentList -join ' '), $WindowStyle,
                           [bool]$RedirectStandardOutput, [bool]$RedirectStandardError, [bool]$PassThru)
    switch ($env:OOO_TEST_START) {
        'throw' { throw 'cannot start' }
        'fail' { return [pscustomobject]@{ Id = 7; Handle = 1; HasExited = $true; ExitCode = 3 } }
        'reuse' { return [pscustomobject]@{ Id = 7; Handle = 1; HasExited = $true; ExitCode = 0 } }
        'slow' { return [pscustomobject]@{ Id = 7; Handle = 1; HasExited = $false; ExitCode = $null } }
        default {
            $dir = Join-Path (Join-Path $env:USERPROFILE '.ouroboros') 'gui'
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            Set-Content -Path (Join-Path $dir 'server.json') -NoNewline -Value '{"host": "127.0.0.1", "port": 10300, "token": "t", "pid": 7}'
            return [pscustomobject]@{ Id = 7; Handle = 1; HasExited = $false; ExitCode = $null }
        }
    }
}
"""

pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")


def _ps(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> list[str]:
    run_env = {key: value for key, value in os.environ.items() if key not in _SESSION_VARS}
    run_env.update(
        {
            "NO_COLOR": "1",
            "OOO_TEST_INSTALLER": str(INSTALL_PS1),
            "USERPROFILE": str(tmp_path / "profile"),
            CALLS_LOG_VAR: str(tmp_path / "calls.log"),
            **(env or {}),
        }
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", _PS_LOAD + body],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


def _gui_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "ouroboros"
    stub.symlink_to(_cached_executable(_GUI_STUB))
    return stub


@pwsh
def test_ps1_interactive_desktop_starts_console_hidden(tmp_path: Path) -> None:
    stub = _gui_stub(tmp_path)
    out = _ps(
        tmp_path,
        f"Invoke-ConsoleAfterInstall '{stub}' $false\n"
        "$script:started | ForEach-Object { 'STARTED ' + ($_ -join '|') }",
        env={"OOO_TEST_INTERACTIVE": "1"},
    )

    assert f"STARTED {stub}|gui|Hidden|True|True|True" in out
    assert "OK The Ouroboros console is running in the background at http://localhost:10300/" in out
    assert (
        'INFO It opens in your browser. Stop it with "Stop console" at the bottom of its sidebar.'
        in out
    )
    assert "INFO Reopen it any time with: ouroboros gui" in out
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls == ["ouroboros gui --help", "probe telemetry=0 dnt=1"]


@pwsh
@pytest.mark.parametrize(
    "env",
    [
        {"OOO_TEST_INTERACTIVE": "0"},
        {"OOO_TEST_INTERACTIVE": "1", "CI": "true"},
        {"OOO_TEST_INTERACTIVE": "1", "SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"},
    ],
    ids=["non-interactive", "ci", "ssh"],
)
def test_ps1_without_an_interactive_desktop_only_the_command_is_printed(
    tmp_path: Path, env: dict[str, str]
) -> None:
    stub = _gui_stub(tmp_path)
    out = _ps(
        tmp_path,
        f"Invoke-ConsoleAfterInstall '{stub}' $false\n'STARTS ' + $script:started.Count",
        env=env,
    )

    assert NEXT_LINE in out
    assert "STARTS 0" in out


@pwsh
@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    [
        ("$true", "", "True"),
        ("$false", "1", "True"),
        ("$false", " Yes ", "True"),
        ("$false", "", "False"),
        ("$false", "0", "False"),
        ("$false", "off", "False"),
        ("$false", "maybe", "True"),
    ],
)
def test_ps1_opt_out_values(tmp_path: Path, flag: str, value: str, expected: str) -> None:
    out = _ps(tmp_path, f"'RESULT ' + (Test-ConsoleOptOut {flag} '{value}')")

    assert f"RESULT {expected}" in out
    warned = any("unrecognized value" in line for line in out)
    assert warned is (value == "maybe")


@pwsh
def test_ps1_opt_out_skips_probe_and_start(tmp_path: Path) -> None:
    stub = _gui_stub(tmp_path)
    out = _ps(
        tmp_path,
        f"Invoke-ConsoleAfterInstall '{stub}' $true\n'STARTS ' + $script:started.Count",
        env={"OOO_TEST_INTERACTIVE": "1"},
    )

    assert out == ["STARTS 0"]
    assert not (tmp_path / "calls.log").exists()


@pwsh
def test_ps1_release_without_gui_stays_silent(tmp_path: Path) -> None:
    stub = _gui_stub(tmp_path)
    out = _ps(
        tmp_path,
        f"Invoke-ConsoleAfterInstall '{stub}' $false\n'STARTS ' + $script:started.Count",
        env={"OOO_TEST_INTERACTIVE": "1", GUI_VERSION_VAR: "old"},
    )

    assert out == ["STARTS 0"]


@pwsh
def test_ps1_probe_that_hangs_is_killed(tmp_path: Path) -> None:
    stub = _gui_stub(tmp_path)
    started = time.monotonic()
    out = _ps(
        tmp_path,
        f"'RESULT ' + (Test-ConsoleCommand '{stub}' 500)",
        env={GUI_VERSION_VAR: "hang"},
    )

    assert "RESULT False" in out
    assert time.monotonic() - started < 25


@pwsh
def test_ps1_probe_that_ignores_sigterm_is_killed(tmp_path: Path) -> None:
    stub = _gui_stub(tmp_path)
    started = time.monotonic()
    out = _ps(
        tmp_path,
        f"'RESULT ' + (Test-ConsoleCommand '{stub}' 500)",
        env={GUI_VERSION_VAR: "stubborn"},
    )

    assert "RESULT False" in out
    # pwsh startup dominates; the stub alone would take 8 s.
    assert time.monotonic() - started < 25
    pid = next(
        int(line.split("=", 1)[1]) for line in _calls(tmp_path) if line.startswith("probe pid=")
    )
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pwsh
@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("fail", "WARN The Ouroboros console did not start (exit 3)."),
        ("throw", "WARN The Ouroboros console did not start: cannot start."),
        ("slow", "INFO The Ouroboros console is still starting in the background"),
        ("reuse", "WARN Could not confirm that the Ouroboros console started."),
    ],
)
def test_ps1_start_outcomes_never_fail(tmp_path: Path, start: str, expected: str) -> None:
    out = _ps(
        tmp_path,
        "Start-ConsoleDetached 'ouroboros' 300\n'RETURNED'",
        env={"OOO_TEST_START": start},
    )

    assert any(line.startswith(expected) for line in out), out
    assert out[-1] == "RETURNED"


@pwsh
def test_ps1_running_console_is_reused(tmp_path: Path) -> None:
    state = tmp_path / "profile" / ".ouroboros" / "gui" / "server.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"host": "127.0.0.1", "port": 10301, "token": "x", "pid": 1}')
    out = _ps(
        tmp_path,
        "Start-ConsoleDetached 'ouroboros' 300",
        env={"OOO_TEST_START": "reuse"},
    )

    assert (
        "OK The Ouroboros console is already running at http://localhost:10301/; "
        "opened it in your browser." in out
    )


@pwsh
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"host": "127.0.0.1", "port": 10300}', "http://localhost:10300/"),
        ('{"host": "0.0.0.0", "port": 65535}', "http://localhost:65535/"),
        (
            '{"host":"192.168.1.5","port":123,"extra":{"host":"127.0.0.1","port":10300}}',
            "",
        ),
        ('{"extra":{"host":"127.0.0.1","port":10300}}', ""),
        ('{"host": "127.0.0.1", "port": 10300} junk', ""),
        ('{"host": "127.0.0.1", "port": 10300}}', ""),
        ('[{"host": "127.0.0.1", "port": 10300}]', ""),
        ('{"host": "127.0.0.1", "port": 10300.0}', ""),
        ('{"host": "127.0.0.1", "port": true}', ""),
        ('{"host": "127.0.0.1", "port": 0}', ""),
        ('{"host": "127.0.0.1", "port": 65536}', ""),
        ('{"host": "127.0.0.1", "port": "10300"}', ""),
        ('{"host": "192.168.1.5", "port": 10300}', ""),
        ('{"port": 10300}', ""),
        ("not json", ""),
    ],
)
def test_ps1_state_file_must_name_a_loopback_url(
    tmp_path: Path, content: str, expected: str
) -> None:
    quoted = content.replace("'", "''")
    out = _ps(tmp_path, f"'URL=' + (ConvertTo-ConsoleUrl '{quoted}')")

    assert out == [f"URL={expected}"]


@pwsh
@pytest.mark.parametrize(
    ("switch", "env", "expected"),
    [
        ("[switch]$true", {}, "OPTOUT True"),
        ("[switch]$false", {"OUROBOROS_NO_CONSOLE": "1"}, "OPTOUT True"),
        ("[switch]$false", {}, "OPTOUT False"),
        (
            "[switch]$false",
            {"OOO_TEST_THROW": "1"},
            "WARN Could not start the Ouroboros console: boom",
        ),
    ],
    ids=["switch", "variable", "neither", "error-is-contained"],
)
def test_ps1_final_step_wiring(
    tmp_path: Path, switch: str, env: dict[str, str], expected: str
) -> None:
    """Run the script's own last statement with the console step stubbed out."""
    final = "try {" + INSTALL_PS1.read_text(encoding="utf-8").rsplit("\ntry {", 1)[1]
    out = _ps(
        tmp_path,
        f"$NoConsole = {switch}\n$ouroborosExe = 'ouroboros-exe'\n"
        "function Invoke-ConsoleAfterInstall([string]$Exe, [bool]$OptOut) {\n"
        "    if ($env:OOO_TEST_THROW) { throw 'boom' }\n"
        '    Microsoft.PowerShell.Utility\\Write-Host "EXE $Exe"\n'
        '    Microsoft.PowerShell.Utility\\Write-Host "OPTOUT $OptOut"\n'
        "}\n" + final + "\n'RETURNED'",
        env=env,
    )

    assert expected in out
    assert out[-1] == "RETURNED"
    if "OPTOUT" in expected:
        assert "EXE ouroboros-exe" in out


def test_both_installers_document_the_opt_out() -> None:
    sh_text = INSTALL_SH.read_text(encoding="utf-8")
    ps1_text = INSTALL_PS1.read_text(encoding="utf-8")
    header_sh = sh_text.split("set -euo pipefail", 1)[0]
    header_ps1 = ps1_text.split("[CmdletBinding()]", 1)[0]
    assert "--no-console" in header_sh and "OUROBOROS_NO_CONSOLE=1" in header_sh
    assert "-NoConsole" in header_ps1 and "OUROBOROS_NO_CONSOLE" in header_ps1
    docs = (INSTALL_SH.parents[1] / "docs" / "getting-started.md").read_text(encoding="utf-8")
    for needle in ("--no-console", "-NoConsole", "OUROBOROS_NO_CONSOLE"):
        assert needle in docs, needle


def test_console_step_is_the_last_thing_each_installer_does() -> None:
    """Anything after a started console would run while it is already open."""
    sh_lines = [
        line for line in INSTALL_SH.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert sh_lines[-1] == '_console_after_install "${OUROBOROS_SETUP_CMD:-}" || true'
    ps1_text = INSTALL_PS1.read_text(encoding="utf-8").rstrip()
    assert ps1_text.endswith("    Write-Info 'Start it any time with: ouroboros gui'\n}"), ps1_text[
        -200:
    ]
    assert "Invoke-ConsoleAfterInstall $ouroborosExe" in ps1_text.rsplit("try {", 1)[1]
