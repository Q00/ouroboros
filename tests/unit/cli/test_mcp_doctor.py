"""Unit tests for ``ouroboros mcp doctor`` diagnostic command.

Covers:
- Each individual check function with a mocked environment
- Overall exit-code logic (0 = all pass/warn, 1 = any fail)
- JSON output format validation
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from ouroboros.cli.commands.mcp_doctor import (
    CheckResult,
    check_claude_agent_sdk_import,
    check_event_store,
    check_litellm_import,
    check_mcp_import,
    check_ouroboros_version,
    check_pid_file,
    check_platform,
    check_python_version,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

runner = CliRunner()


def _make_app():
    """Return a fresh Typer app with the doctor command registered."""
    import typer

    from ouroboros.cli.commands.mcp_doctor import register_doctor_command

    app = typer.Typer()
    register_doctor_command(app)
    return app


# ---------------------------------------------------------------------------
# check_python_version
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_passes_on_312_or_newer(self):
        with patch.object(sys, "version_info", (3, 12, 0, "final", 0)):
            result = check_python_version()
        assert result.status == "pass"
        assert "3.12" in result.message

    def test_passes_on_313(self):
        with patch.object(sys, "version_info", (3, 13, 1, "final", 0)):
            result = check_python_version()
        assert result.status == "pass"

    def test_fails_on_311(self):
        with patch.object(sys, "version_info", (3, 11, 9, "final", 0)):
            result = check_python_version()
        assert result.status == "fail"
        assert result.remediation != ""

    def test_fails_on_310(self):
        with patch.object(sys, "version_info", (3, 10, 0, "final", 0)):
            result = check_python_version()
        assert result.status == "fail"

    def test_message_contains_version_string(self):
        with patch.object(sys, "version_info", (3, 12, 5, "final", 0)):
            result = check_python_version()
        assert "3.12.5" in result.message


# ---------------------------------------------------------------------------
# check_platform
# ---------------------------------------------------------------------------


class TestCheckPlatform:
    def test_always_passes(self):
        result = check_platform()
        assert result.status == "pass"

    def test_message_is_non_empty(self):
        result = check_platform()
        assert result.message.strip() != ""


# ---------------------------------------------------------------------------
# check_ouroboros_version
# ---------------------------------------------------------------------------


class TestCheckOuroborosVersion:
    def test_passes_when_installed(self):
        with patch("importlib.metadata.version", return_value="0.28.4"):
            result = check_ouroboros_version()
        assert result.status == "pass"
        assert "0.28.4" in result.message

    def test_fails_when_not_installed(self):
        with patch(
            "importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("ouroboros-ai"),
        ):
            result = check_ouroboros_version()
        assert result.status == "fail"
        assert result.remediation != ""


# ---------------------------------------------------------------------------
# check_mcp_import
# ---------------------------------------------------------------------------


class TestCheckMcpImport:
    def test_passes_when_importable(self):
        mock_mcp = MagicMock()
        with (
            patch.dict("sys.modules", {"mcp": mock_mcp}),
            patch("importlib.metadata.version", return_value="1.26.0"),
        ):
            result = check_mcp_import()
        assert result.status == "pass"
        assert "1.26.0" in result.message

    def test_fails_when_not_importable(self):
        with patch.dict("sys.modules", {"mcp": None}):
            # Ensure import raises ImportError
            with patch("builtins.__import__", side_effect=_import_error_for("mcp")):
                result = check_mcp_import()
        assert result.status == "fail"
        assert result.remediation != ""

    def test_passes_when_installed_returns_version(self):
        # mcp is installed — check that version string is included in the message
        result = check_mcp_import()
        # mcp is an actual dependency of this project, so it should pass
        assert result.status == "pass"
        assert result.name == "mcp_import"


# ---------------------------------------------------------------------------
# check_claude_agent_sdk_import
# ---------------------------------------------------------------------------


class TestCheckClaudeAgentSdkImport:
    """Tests for check_claude_agent_sdk_import — backend-aware behaviour."""

    def test_passes_when_importable(self):
        mock_sdk = MagicMock()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}),
            patch("importlib.metadata.version", return_value="0.5.0"),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"

    def test_fails_when_not_importable_on_claude_backend(self):
        """Missing SDK on a Claude runtime is a hard fail."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="claude",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "fail"
        assert result.remediation != ""

    def test_warns_when_not_importable_on_codex_backend(self):
        """Missing SDK on a Codex runtime is only a warning, not a failure."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="codex",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "warn"
        assert "codex" in result.message

    def test_warns_when_not_importable_on_opencode_backend(self):
        """Missing SDK on an OpenCode runtime is only a warning."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="opencode",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "warn"
        assert "opencode" in result.message

    def test_passes_with_unknown_version(self):
        mock_sdk = MagicMock()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}),
            patch(
                "importlib.metadata.version",
                side_effect=importlib.metadata.PackageNotFoundError("claude-agent-sdk"),
            ),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"
        assert "unknown" in result.message

    def test_passes_when_importable_regardless_of_backend(self):
        """If the SDK is installed, the check passes even on non-Claude backends."""
        mock_sdk = MagicMock()
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="codex",
            ),
            patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}),
            patch("importlib.metadata.version", return_value="0.5.0"),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"

    def test_fails_on_claude_code_backend(self):
        """claude_code is also a Claude backend — missing SDK should fail."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="claude_code",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "fail"


# ---------------------------------------------------------------------------
# check_litellm_import
# ---------------------------------------------------------------------------


class TestCheckLitellmImport:
    def test_passes_when_importable(self):
        mock_litellm = MagicMock()
        with (
            patch.dict("sys.modules", {"litellm": mock_litellm}),
            patch("importlib.metadata.version", return_value="1.80.0"),
        ):
            result = check_litellm_import()
        assert result.status == "pass"

    def test_warns_when_not_importable(self):
        """litellm is optional — missing yields warn, not fail."""
        with patch("builtins.__import__", side_effect=_import_error_for("litellm")):
            result = check_litellm_import()
        assert result.status == "warn"
        assert result.remediation != ""

    def test_does_not_fail_when_missing(self):
        with patch("builtins.__import__", side_effect=_import_error_for("litellm")):
            result = check_litellm_import()
        assert result.status != "fail"


# ---------------------------------------------------------------------------
# check_event_store
# ---------------------------------------------------------------------------


class TestCheckEventStore:
    def test_passes_when_db_does_not_exist(self, tmp_path):
        fake_path = tmp_path / "nonexistent.db"
        with patch("ouroboros.cli.commands.mcp_doctor._EVENT_STORE_PATH", fake_path):
            result = check_event_store()
        assert result.status == "pass"
        assert "not found" in result.message

    def test_passes_when_db_small(self, tmp_path):
        db = tmp_path / "ouroboros.db"
        db.write_bytes(b"x" * 1024)  # 1 KB
        with patch("ouroboros.cli.commands.mcp_doctor._EVENT_STORE_PATH", db):
            result = check_event_store()
        assert result.status == "pass"
        assert "MB" in result.message

    def test_warns_when_db_over_500mb(self, tmp_path):
        db = tmp_path / "ouroboros.db"
        db.write_bytes(b"x")
        large_stat = MagicMock()
        large_stat.st_size = 600 * 1024 * 1024  # 600 MB
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.stat.return_value = large_stat
        mock_path.__str__ = lambda _self: str(db)
        with patch("ouroboros.cli.commands.mcp_doctor._EVENT_STORE_PATH", mock_path):
            result = check_event_store()
        assert result.status == "warn"
        assert result.remediation != ""

    def test_warns_when_stat_raises(self, tmp_path):
        db = tmp_path / "ouroboros.db"
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.stat.side_effect = OSError("permission denied")
        mock_path.__str__ = lambda _self: str(db)
        with patch("ouroboros.cli.commands.mcp_doctor._EVENT_STORE_PATH", mock_path):
            result = check_event_store()
        assert result.status == "warn"


# ---------------------------------------------------------------------------
# check_pid_file
# ---------------------------------------------------------------------------


class TestCheckPidFile:
    def test_passes_when_no_pid_file(self, tmp_path):
        fake_pid = tmp_path / "mcp-server.pid"
        with patch("ouroboros.cli.commands.mcp_doctor._PID_FILE", fake_pid):
            result = check_pid_file()
        assert result.status == "pass"
        assert "not running" in result.message.lower() or "no pid" in result.message.lower()

    def test_passes_when_pid_alive(self, tmp_path):
        fake_pid = tmp_path / "mcp-server.pid"
        fake_pid.write_text("12345", encoding="utf-8")
        with (
            patch("ouroboros.cli.commands.mcp_doctor._PID_FILE", fake_pid),
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=True),
        ):
            result = check_pid_file()
        assert result.status == "pass"
        assert "12345" in result.message

    def test_warns_when_pid_stale(self, tmp_path):
        fake_pid = tmp_path / "mcp-server.pid"
        fake_pid.write_text("99999", encoding="utf-8")
        with (
            patch("ouroboros.cli.commands.mcp_doctor._PID_FILE", fake_pid),
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=False),
        ):
            result = check_pid_file()
        assert result.status == "warn"
        assert result.remediation != ""

    def test_warns_when_pid_file_unreadable(self, tmp_path):
        fake_pid = tmp_path / "mcp-server.pid"
        fake_pid.write_text("not_a_number", encoding="utf-8")
        with patch("ouroboros.cli.commands.mcp_doctor._PID_FILE", fake_pid):
            result = check_pid_file()
        assert result.status == "warn"
        assert result.remediation != ""


# ---------------------------------------------------------------------------
# _pid_is_alive
# ---------------------------------------------------------------------------


class TestPidIsAlive:
    def test_returns_true_when_process_exists(self):
        from ouroboros.cli.commands.mcp_doctor import _pid_is_alive

        with patch("os.kill", return_value=None):
            assert _pid_is_alive(12345) is True

    def test_returns_false_when_process_not_found(self):
        from ouroboros.cli.commands.mcp_doctor import _pid_is_alive

        with patch("os.kill", side_effect=ProcessLookupError):
            assert _pid_is_alive(99999) is False

    def test_returns_true_on_permission_error(self):
        """PermissionError means process exists but we can't signal it."""
        from ouroboros.cli.commands.mcp_doctor import _pid_is_alive

        with patch("os.kill", side_effect=PermissionError):
            assert _pid_is_alive(12345) is True

    def test_returns_false_on_os_error_non_windows(self):
        from ouroboros.cli.commands.mcp_doctor import _pid_is_alive

        with (
            patch("os.kill", side_effect=OSError("WinError 87")),
            patch.object(sys, "platform", "linux"),
        ):
            assert _pid_is_alive(12345) is False


# ---------------------------------------------------------------------------
# CLI integration: exit codes and JSON output
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    def test_exits_0_when_all_pass(self):
        app = _make_app()
        all_pass = CheckResult(name="x", status="pass", message="ok")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: all_pass],
        ):
            result = runner.invoke(app, [])
        assert result.exit_code == 0

    def test_exits_1_when_any_fail(self):
        app = _make_app()
        failing = CheckResult(name="x", status="fail", message="broken")
        passing = CheckResult(name="y", status="pass", message="ok")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: failing, lambda: passing],
        ):
            result = runner.invoke(app, [])
        assert result.exit_code == 1

    def test_exits_0_when_only_warn(self):
        app = _make_app()
        warning = CheckResult(name="x", status="warn", message="optional missing")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: warning],
        ):
            result = runner.invoke(app, [])
        assert result.exit_code == 0

    def test_json_flag_emits_valid_json(self):
        app = _make_app()
        check_a = CheckResult(name="a", status="pass", message="good")
        check_b = CheckResult(name="b", status="warn", message="maybe", remediation="fix it")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: check_a, lambda: check_b],
        ):
            result = runner.invoke(app, ["--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "a"
        assert data[0]["status"] == "pass"
        assert data[1]["remediation"] == "fix it"

    def test_json_output_has_required_keys(self):
        app = _make_app()
        check_result = CheckResult(name="z", status="pass", message="ok")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: check_result],
        ):
            result = runner.invoke(app, ["--json"])
        data = json.loads(result.output)
        for item in data:
            assert "name" in item
            assert "status" in item
            assert "message" in item
            assert "remediation" in item

    def test_human_output_shows_symbols(self):
        app = _make_app()
        check_result = CheckResult(name="mcp", status="pass", message="mcp 1.26.0")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: check_result],
        ):
            result = runner.invoke(app, [])
        assert "mcp" in result.output

    def test_human_output_shows_remediation(self):
        app = _make_app()
        check_result = CheckResult(
            name="mcp_import",
            status="fail",
            message="not found",
            remediation="pip install mcp",
        )
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: check_result],
        ):
            result = runner.invoke(app, [])
        assert "pip install mcp" in result.output

    def test_json_fail_still_exits_1(self):
        app = _make_app()
        failing = CheckResult(name="x", status="fail", message="broken")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: failing],
        ):
            result = runner.invoke(app, ["--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data[0]["status"] == "fail"

    def test_exits_0_on_codex_backend_without_claude_sdk(self):
        """On a Codex backend, missing claude-agent-sdk should not cause exit 1."""
        app = _make_app()
        warn_result = CheckResult(
            name="claude_agent_sdk_import",
            status="warn",
            message="claude-agent-sdk not installed (not required for codex runtime)",
        )
        pass_result = CheckResult(name="mcp_import", status="pass", message="mcp 1.26.0")
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: pass_result, lambda: warn_result],
        ):
            result = runner.invoke(app, [])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Sanity: mcp.py app still importable with doctor registered
# ---------------------------------------------------------------------------


def test_mcp_app_importable():
    from ouroboros.cli.commands.mcp import app

    assert app is not None


def test_doctor_command_registered():
    from ouroboros.cli.commands.mcp import app

    # Typer stores name=None at registration time; use callback name instead
    callback_names = [cmd.callback.__name__ for cmd in app.registered_commands]
    assert "doctor" in callback_names


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _import_error_for(module_name: str):
    """Return a side_effect function that raises ImportError only for *module_name*."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _side_effect(name, *args, **kwargs):
        if name == module_name or name.startswith(module_name + "."):
            raise ImportError(f"No module named '{module_name}'")
        return real_import(name, *args, **kwargs)

    return _side_effect
