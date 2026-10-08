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
    run = MagicMock(
        side_effect=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1 if command == expected else 0
        )
    )
    monkeypatch.setattr(update.subprocess, "run", run)
    runtime = "codex" if stage == "codex-config" else "all"

    failed = runner.invoke(update.app, ["--yes", "--runtime", runtime])

    assert failed.exit_code == 1, failed.output
    assert "partially updated" in _plain(failed.output)
    assert _plain(failed.output).count("Recovery command (") == 1
    # Exact matching detects Rich markup interpretation or wrapping of a copyable command.
    assert update._format_recovery_command(expected, overrides) in failed.output
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
    diagnostic = [codex, "plugin", "list", "--marketplace", "ouroboros", "--json"]
    assert all(call.args[0] == diagnostic for call in run.call_args_list)


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


@pytest.mark.parametrize("prior_value", [None, "original 한글 $value [red]& apostrophe's"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_copyable_command_round_trips_real_shell_and_restores_environment(
    prior_value: str | None,
    exit_code: int,
    tmp_path: Path,
) -> None:
    """Execute harmless recorders in native PowerShell on Windows and /bin/sh elsewhere."""
    windows = sys.platform == "win32"
    shell = shutil.which("powershell") if windows else shutil.which("sh")
    if shell is None:
        pytest.skip("Native shell is unavailable")
    recorder = tmp_path / f"{SPECIAL_TEXT}.py"
    recorder.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'args': sys.argv[3:], 'value': os.environ.get('OUROBOROS_CODEX_CLI_PATH')"
        "}, ensure_ascii=False), encoding='utf-8')\n"
        "sys.exit(int(sys.argv[2]))\n",
        encoding="utf-8",
    )
    during = tmp_path / "during.json"
    after = tmp_path / "after.json"
    args = [SPECIAL_TEXT, "$(echo should-not-run)", "a;b|c", "`literal`", "--flag"]
    command = [sys.executable, str(recorder), str(during), str(exit_code), *args]
    recovery = update._format_recovery_command(command, {ENV_KEY: SPECIAL_TEXT}, windows=windows)
    observe_after = update._format_recovery_command(
        [sys.executable, str(recorder), str(after), "0"], windows=windows
    )
    shell_script = recovery + "\n" + observe_after
    env = dict(os.environ)
    if prior_value is None:
        env.pop(ENV_KEY, None)
    else:
        env[ENV_KEY] = prior_value
    if windows:
        encoded = base64.b64encode(shell_script.encode("utf-16-le")).decode("ascii")
        shell_command = [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    else:
        shell_command = [shell, "-c", shell_script]

    result = subprocess.run(
        shell_command,
        env=env,
        capture_output=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(during.read_text(encoding="utf-8")) == {
        "args": args,
        "value": SPECIAL_TEXT,
    }
    assert json.loads(after.read_text(encoding="utf-8")) == {"args": [], "value": prior_value}
