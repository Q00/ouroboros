"""Recovery instructions must replay the failed operation with its original identity."""

from __future__ import annotations

import base64
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from ouroboros.cli.commands import update

runner = CliRunner()
ENV_KEY = "OUROBOROS_CODEX_CLI_PATH"
SPECIAL_TEXT = "owner's ‘smart’ ‚low‛ 한글 $cash [red]& spaced value"
CONTROL_TEXT = "tab\tcarriage\rbell\aback\bvertical\vform\fescape\x1bdelete\x7fc1\x85\nend\n\n"


def _plain(output: str) -> str:
    """Remove terminal styling without altering command characters or wrapping."""
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


@pytest.fixture
def installation(tmp_path: Path) -> update.InstallationIdentity:
    environment = tmp_path / SPECIAL_TEXT / "tools" / "ouroboros-ai"
    return update.InstallationIdentity(
        manager="uv",
        tool_name="ouroboros-ai",
        environment=environment,
        profile="ouroboros-ai[mcp]",
        console_path=environment / "bin" / "ouroboros",
        manager_binary=str(tmp_path / "original uv"),
        manager_home=environment.parent,
    )


@pytest.fixture
def cli_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    installation: update.InstallationIdentity,
) -> dict[str, update.RuntimeRefreshTopology]:
    for key in ("HOME", "USERPROFILE", "CODEX_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "isolated home"))
    monkeypatch.setattr(update, "__version__", "0.54.4")
    monkeypatch.setattr(update, "_latest_pypi_version", lambda **_kwargs: "0.55.6")
    monkeypatch.setattr(update, "_detect_installation_identity", lambda: installation)
    monkeypatch.setattr(update, "_installed_version", lambda _identity: "0.55.6")
    monkeypatch.setattr(update, "configure_omp_tool_call_timeout", lambda **_kwargs: True)
    topologies = {
        "codex": update.RuntimeRefreshTopology(
            runtime_backend="codex",
            runtime_executable=str(tmp_path / SPECIAL_TEXT / "codex"),
            runtime_executable_env_key=ENV_KEY,
        ),
        "claude": update.RuntimeRefreshTopology(runtime_backend="claude"),
    }
    monkeypatch.setattr(update, "_configured_runtime_topology", lambda runtime: topologies[runtime])
    return topologies


@pytest.mark.parametrize("stage", ["codex-config", "installed-artifacts", "codex-marketplace"])
def test_failed_refresh_shows_original_command_and_latest_retry_does_not_replay(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    installation: update.InstallationIdentity,
    cli_environment: dict[str, update.RuntimeRefreshTopology],
) -> None:
    """Exercise the real updater branches across partial failure and a latest-version retry."""
    codex = cli_environment["codex"].runtime_executable
    assert codex is not None
    expected = {
        "codex-config": [
            str(installation.console_path),
            "setup",
            "--runtime",
            "codex",
            "--preserve-existing-llm",
            "--non-interactive",
        ],
        "installed-artifacts": [str(installation.console_path), "setup", "refresh"],
        "codex-marketplace": [codex, "plugin", "marketplace", "upgrade", "ouroboros"],
    }[stage]
    overrides = {ENV_KEY: codex} if stage == "codex-config" else None
    diagnostic = [codex, "plugin", "list", "--marketplace", "ouroboros", "--json"]

    def simulate(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command == diagnostic:
            return subprocess.CompletedProcess(command, 0, b'{"installed": []}', b"")
        return subprocess.CompletedProcess(command, 1 if command == expected else 0)

    run = MagicMock(side_effect=simulate)
    monkeypatch.setattr(update.subprocess, "run", run)
    runtime = "codex" if stage == "codex-config" else "all"

    failed = runner.invoke(update.app, ["--yes", "--runtime", runtime])

    assert failed.exit_code == 1, failed.output
    assert isinstance(failed.exception, SystemExit), repr(failed.exception)
    assert "partially updated" in _plain(failed.output)
    assert _plain(failed.output).count("Recovery command (") == 1
    # Exact matching detects Rich markup interpretation or wrapping of a copyable command.
    assert update._format_recovery_command(expected, overrides) in failed.output
    if any(call.args[0] == diagnostic for call in run.call_args_list):
        assert "Codex plugin: not installed." in _plain(failed.output)
    failed_call = next(call for call in run.call_args_list if call.args[0] == expected)
    if overrides:
        assert failed_call.kwargs["env"][ENV_KEY] == codex
    else:
        assert failed_call.kwargs["env"] is None

    monkeypatch.setattr(update, "__version__", "0.55.6")
    run.reset_mock()
    latest = runner.invoke(update.app, ["--yes", "--runtime", runtime])
    assert latest.exit_code == 0, latest.output
    assert "up to date" in _plain(latest.output)
    assert "Recovery command (" not in _plain(latest.output)
    # Read-only plugin diagnostics are allowed; no refresh may be replayed.
    assert all(call.args[0] == diagnostic for call in run.call_args_list)
    if run.call_args_list:
        assert "Codex plugin: not installed." in _plain(latest.output)


@pytest.mark.parametrize("mode", ["plugin", "subprocess"])
def test_opencode_recovery_preserves_mode_and_selected_executable(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    installation: update.InstallationIdentity,
    cli_environment: dict[str, update.RuntimeRefreshTopology],
) -> None:
    runtime_cli = str(installation.environment / SPECIAL_TEXT / "opencode")
    cli_environment["opencode"] = update.RuntimeRefreshTopology(
        runtime_backend="opencode",
        opencode_mode=mode,  # type: ignore[arg-type]
        runtime_executable=runtime_cli,
        runtime_executable_env_key="OUROBOROS_OPENCODE_CLI_PATH",
    )
    expected = [
        str(installation.console_path),
        "setup",
        "--runtime",
        "opencode",
        "--opencode-mode",
        mode,
        "--non-interactive",
    ]
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1 if command == expected else 0
        ),
    )

    result = runner.invoke(update.app, ["--yes", "--runtime", "opencode"])

    assert result.exit_code == 1, result.output
    assert (
        update._format_recovery_command(expected, {"OUROBOROS_OPENCODE_CLI_PATH": runtime_cli})
        in result.output
    )


@pytest.mark.parametrize("failure", ["install", "update"])
def test_claude_recovery_includes_pending_update_only_when_install_failed(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    installation: update.InstallationIdentity,
    cli_environment: dict[str, update.RuntimeRefreshTopology],
) -> None:
    claude = str(installation.environment / SPECIAL_TEXT / "claude")
    cli_environment["claude"] = update.RuntimeRefreshTopology(
        runtime_backend="claude",
        runtime_executable=claude,
        runtime_executable_env_key="OUROBOROS_CLI_PATH",
    )
    failed_command = [claude, "plugin", failure, "ouroboros@ouroboros"]
    run = MagicMock(
        side_effect=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1 if command == failed_command else 0
        )
    )
    monkeypatch.setattr(update.subprocess, "run", run)

    result = runner.invoke(update.app, ["--yes", "--runtime", "claude"])

    assert result.exit_code == 1, result.output
    assert _plain(result.output).count("Recovery command (") == 1
    assert update._format_recovery_command(failed_command) in result.output
    pending_command = [claude, "plugin", "update", "ouroboros@ouroboros"]
    if failure == "install":
        assert "After the previous command succeeds" in _plain(result.output)
        assert update._format_recovery_command(pending_command) in result.output
        assert pending_command not in [call.args[0] for call in run.call_args_list]
    else:
        assert "After the previous command succeeds" not in _plain(result.output)


@pytest.mark.parametrize("scenario", ["success", "dry-run", "best-effort-marketplace"])
def test_success_dry_run_and_best_effort_failure_have_no_recovery_hint(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    installation: update.InstallationIdentity,
    cli_environment: dict[str, update.RuntimeRefreshTopology],
) -> None:
    cli_environment["claude"] = update.RuntimeRefreshTopology(
        runtime_backend="claude",
        runtime_executable=str(installation.environment / "claude"),
        runtime_executable_env_key="OUROBOROS_CLI_PATH",
    )
    run = MagicMock(
        side_effect=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1
            if scenario == "best-effort-marketplace" and command[1:3] == ["plugin", "marketplace"]
            else 0,
        )
    )
    monkeypatch.setattr(update.subprocess, "run", run)
    args = ["--yes", "--runtime", "claude"]
    if scenario == "dry-run":
        args.append("--dry-run")

    result = runner.invoke(update.app, args)

    assert result.exit_code == 0, result.output
    assert "Recovery command (" not in _plain(result.output)
    if scenario == "dry-run":
        run.assert_not_called()


@pytest.mark.parametrize("verified_version", [None, "0.54.4"])
def test_unverified_package_does_not_offer_runtime_refresh(
    verified_version: str | None,
    monkeypatch: pytest.MonkeyPatch,
    cli_environment: dict[str, update.RuntimeRefreshTopology],
) -> None:
    monkeypatch.setattr(update, "_installed_version", lambda _identity: verified_version)
    run = MagicMock(return_value=subprocess.CompletedProcess(["uv"], 0))
    monkeypatch.setattr(update.subprocess, "run", run)

    result = runner.invoke(update.app, ["--yes", "--runtime", "codex"])

    assert result.exit_code == 1, result.output
    assert "Recovery command (" not in _plain(result.output)
    assert run.call_count == 1


@pytest.mark.parametrize("error", [OSError("cannot launch"), subprocess.TimeoutExpired("setup", 1)])
def test_failed_launch_or_timeout_prints_literal_unwrapped_recovery(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()
    monkeypatch.setattr(update, "console", Console(file=buffer, width=20, color_system=None))
    monkeypatch.setattr(update.subprocess, "run", MagicMock(side_effect=error))
    command = [str(Path("/managed") / SPECIAL_TEXT / "ouroboros"), "setup", "refresh"]

    assert not update._run_step(
        command, description="Refresh artifacts", dry_run=False, recovery=True
    )

    assert update._format_recovery_command(command) in buffer.getvalue()


@pytest.mark.parametrize(
    ("command", "overrides", "follow_up"),
    [
        pytest.param(
            ["/managed/:smile:/ouroboros", "setup", "refresh"],
            None,
            None,
            id="executable-path",
        ),
        pytest.param(
            ["/managed/ouroboros", "setup", "--runtime", "codex"],
            {ENV_KEY: "/managed/:rocket:/codex"},
            None,
            id="environment-override",
        ),
        pytest.param(
            ["/managed/:tada:/claude", "plugin", "install", "ouroboros@ouroboros"],
            None,
            [["/managed/:tada:/claude", "plugin", "update", "ouroboros@ouroboros"]],
            id="pending-plugin-update",
        ),
    ],
)
def test_recovery_output_preserves_literal_emoji_shortcodes(
    command: list[str],
    overrides: dict[str, str] | None,
    follow_up: list[list[str]] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve valid POSIX path text through Rich without executing any commands."""
    buffer = StringIO()
    monkeypatch.setattr(
        update, "console", Console(file=buffer, width=20, color_system=None, emoji=True)
    )

    update._print_step_recovery(command, overrides, follow_up)

    for invocation in [command, *(follow_up or [])]:
        assert update._format_recovery_command(invocation, overrides) in buffer.getvalue()


def _printed_recovery_commands(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    overrides: dict[str, str] | None = None,
    follow_up: list[list[str]] | None = None,
) -> list[str]:
    """Exercise the final Rich output, retaining literal command line breaks."""
    output = StringIO()
    monkeypatch.setattr(update, "console", Console(file=output, width=200, color_system=None))
    update._print_step_recovery(command, overrides, follow_up)
    commands = output.getvalue().split("use the same environment):\n", 1)[1]
    return [
        text.removesuffix("\n")
        for text in commands.split("After the previous command succeeds, complete the step with:\n")
    ]


def _run_recovery_shell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    windows = sys.platform == "win32"
    shell = shutil.which("powershell") if windows else shutil.which("sh")
    if shell is None:
        pytest.skip("Native shell is unavailable")
    if windows:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command = [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    else:
        command = [shell, "-c", script]
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
    )


@pytest.mark.parametrize("prior_value", [None, "", "original 한글 $value [red]& apostrophe's"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_copyable_command_round_trips_real_shell_and_restores_environment(
    prior_value: str | None,
    exit_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay printed commands twice, including an originally present empty value."""
    windows = sys.platform == "win32"
    recorder = tmp_path / f"{SPECIAL_TEXT}.py"
    recorder.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'args': sys.argv[3:], 'value': os.environ.get('OUROBOROS_CODEX_CLI_PATH'),"
        "'present': 'OUROBOROS_CODEX_CLI_PATH' in os.environ"
        "}, ensure_ascii=False), encoding='utf-8')\n"
        "sys.exit(int(sys.argv[2]))\n",
        encoding="utf-8",
    )
    before = tmp_path / "before.json"
    during = tmp_path / "during.json"
    after = tmp_path / "after.json"
    args = [SPECIAL_TEXT, "$(echo should-not-run)", "a;b|c", "`literal`", "--flag"]
    command = [sys.executable, str(recorder), str(during), str(exit_code), *args]
    recovery = _printed_recovery_commands(monkeypatch, command, {ENV_KEY: SPECIAL_TEXT})[0]
    observe_before = update._format_recovery_command(
        [sys.executable, str(recorder), str(before), "0"], windows=windows
    )
    observe_after = update._format_recovery_command(
        [sys.executable, str(recorder), str(after), "0"], windows=windows
    )
    # The second execution must reuse the native helper without a type conflict.
    capture_exit = "$recoveryExit = $LASTEXITCODE" if windows else "recoveryExit=$?"
    verify_exit = (
        f"if ($recoveryExit -ne {exit_code}) {{ exit 91 }}"
        if windows
        else f'[ "$recoveryExit" -eq {exit_code} ] || exit 91'
    )
    caller_scope = "$oooPreviousEnv0 = 'caller value'" if windows else ":"
    verify_scope = "if ($oooPreviousEnv0 -ne 'caller value') { exit 92 }" if windows else ":"
    shell_script = "\n".join(
        [
            caller_scope,
            observe_before,
            recovery,
            capture_exit,
            verify_exit,
            recovery,
            capture_exit,
            verify_exit,
            verify_scope,
            observe_after,
        ]
    )
    env = dict(os.environ)
    if prior_value is None:
        env.pop(ENV_KEY, None)
    else:
        env[ENV_KEY] = prior_value
    result = _run_recovery_shell(shell_script, env)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    original = {"args": [], "value": prior_value, "present": prior_value is not None}
    assert json.loads(before.read_text(encoding="utf-8")) == original
    assert json.loads(during.read_text(encoding="utf-8")) == {
        "args": args,
        "value": SPECIAL_TEXT,
        "present": True,
    }
    assert json.loads(after.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("follow_up", [False, True])
def test_printed_control_arguments_and_environment_round_trip(
    follow_up: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control bytes must survive rendering without reaching the terminal literally."""
    recorder = tmp_path / "record.py"
    recorder.write_text(
        f"import json, os, sys\nprint(json.dumps([sys.argv[1:], os.environ.get({ENV_KEY!r})]))\n",
        encoding="utf-8",
    )
    args = [CONTROL_TEXT, SPECIAL_TEXT, "percent%s\\011$(echo nope)"]
    command = [sys.executable, str(recorder), *args]
    commands = _printed_recovery_commands(
        monkeypatch,
        command,
        {ENV_KEY: CONTROL_TEXT},
        [command] if follow_up else None,
    )
    for printed in commands:
        assert re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", printed) is None
        result = _run_recovery_shell(printed, dict(os.environ))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [args, CONTROL_TEXT]


@pytest.mark.skipif(sys.platform == "win32", reason="Control characters are POSIX filenames")
@pytest.mark.parametrize("piece", ["tab\tpath", "carriage\rpath", "new\npath\n", "escape\x1bpath"])
def test_printed_posix_control_executable_path_runs(
    piece: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / piece
    directory.mkdir()
    executable = directory / "recorder"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' executed\n", encoding="utf-8")
    executable.chmod(0o700)
    printed = _printed_recovery_commands(monkeypatch, [str(executable)])[0]
    assert re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", printed) is None
    result = _run_recovery_shell(printed, dict(os.environ))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"executed\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native helper initialization")
def test_windows_helper_failure_leaves_environment_and_command_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-run"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    printed = _printed_recovery_commands(monkeypatch, command, {ENV_KEY: "override"})[0]
    observer = update._format_recovery_command(
        [
            sys.executable,
            "-c",
            f"import json, os; print(json.dumps(dict(os.environ).get({ENV_KEY!r})))",
        ]
    )
    script = (
        "function Add-Type { throw 'fixture: native helper unavailable' }\n"
        "try {\n" + printed + "\n} catch {}\n" + observer
    )
    result = _run_recovery_shell(script, {**os.environ, ENV_KEY: ""})
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert json.loads(result.stdout) == ""
