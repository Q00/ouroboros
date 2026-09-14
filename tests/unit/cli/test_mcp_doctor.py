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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.mcp_doctor import (
    CheckResult,
    _probe_local_stdio,
    check_claude_agent_sdk_import,
    check_codex_oauth_auth,
    check_event_store,
    check_litellm_import,
    check_mcp_import,
    check_ouroboros_version,
    check_pid_file,
    check_platform,
    check_python_version,
)
from ouroboros.core.types import Result
from ouroboros.mcp.types import TransportType
from ouroboros.package_profiles import UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE

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
            patch("importlib.metadata.version", return_value="2.0.0"),
        ):
            result = check_mcp_import()
        assert result.status == "pass"
        assert "2.0.0" in result.message

    def test_fails_for_legacy_mcp_major(self):
        mock_mcp = MagicMock()
        with (
            patch.dict("sys.modules", {"mcp": mock_mcp}),
            patch("importlib.metadata.version", return_value="1.28.1"),
        ):
            result = check_mcp_import()
        assert result.status == "fail"
        assert "separate server process" in result.remediation

    def test_fails_when_not_importable(self):
        with patch.dict("sys.modules", {"mcp": None}):
            # Ensure import raises ImportError
            with patch("builtins.__import__", side_effect=_import_error_for("mcp")):
                result = check_mcp_import()
        assert result.status == "fail"
        assert "ouroboros-ai[mcp]" in result.remediation

    def test_reports_the_installed_profile_version(self):
        from importlib.metadata import version

        result = check_mcp_import()
        installed = version("mcp")

        assert installed in result.message
        assert result.name == "mcp_import"
        if installed.startswith("2."):
            assert result.status == "pass"
        else:
            # The standalone Claude profile intentionally carries MCP 1.x;
            # doctor must direct MCP serving to the isolated MCP 2 process.
            assert result.status == "fail"
            assert "separate server process" in result.remediation


# ---------------------------------------------------------------------------
# check_claude_agent_sdk_import
# ---------------------------------------------------------------------------


class TestCheckClaudeAgentSdkImport:
    """Tests for check_claude_agent_sdk_import — backend-aware behaviour."""

    @pytest.fixture(autouse=True)
    def _supported_profile_environment(self):
        with patch(
            "ouroboros.cli.commands.mcp_doctor.has_unsupported_claude_sdk_mcp_mix",
            return_value=False,
        ):
            yield

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
        assert "--runtime claude" in result.remediation
        assert "[claude-sdk]" in result.remediation

    def test_warns_when_not_importable_on_codex_backend(self):
        """Missing SDK on a Codex runtime is expected."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="codex",
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_llm_backend",
                return_value="codex",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"
        assert "codex" in result.message
        assert "ouroboros-ai[claude]" in result.remediation

    def test_warns_when_not_importable_on_opencode_backend(self):
        """Missing SDK on an OpenCode runtime is expected."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="opencode",
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_llm_backend",
                return_value="opencode",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"
        assert "opencode" in result.message
        assert "ouroboros-ai[claude]" in result.remediation

    def test_claude_llm_on_non_sdk_runtime_uses_cli_without_sdk(self):
        """The Claude completion adapter has a CLI fallback and needs no SDK."""
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_runtime_backend",
                return_value="codex",
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._get_llm_backend",
                return_value="claude_code",
            ),
            patch("builtins.__import__", side_effect=_import_error_for("claude_agent_sdk")),
        ):
            result = check_claude_agent_sdk_import()
        assert result.status == "pass"
        assert "not installed" in result.message

    def test_forced_mcp2_and_claude_sdk_mix_fails_with_canonical_message(self):
        with patch(
            "ouroboros.cli.commands.mcp_doctor.has_unsupported_claude_sdk_mcp_mix",
            return_value=True,
        ):
            result = check_claude_agent_sdk_import()

        assert result.status == "fail"
        assert "Unsupported package profiles" in result.message
        assert "ouroboros-ai[mcp]" in result.message
        assert "ouroboros-ai[claude-sdk]" in result.message

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
        assert "ouroboros-ai[litellm]" in result.remediation

    def test_does_not_fail_when_missing(self):
        with patch("builtins.__import__", side_effect=_import_error_for("litellm")):
            result = check_litellm_import()
        assert result.status != "fail"

    def test_warns_with_python_313_remediation_on_python_314(self):
        """Doctor should not recommend installing LiteLLM into Python 3.14."""
        with (
            patch("builtins.__import__", side_effect=_import_error_for("litellm")),
            patch.object(sys, "version_info", (3, 14, 0, "final", 0)),
        ):
            result = check_litellm_import()

        assert result.status == "warn"
        assert "Python >=3.12,<3.14" in result.remediation
        assert "Python 3.13" in result.remediation
        assert "python3.13 -m pip install 'ouroboros-ai[litellm]'" in result.remediation
        assert "uv tool install --python 3.13 --force" in result.remediation
        assert "ouroboros-ai[litellm]" in result.remediation
        assert "python3.14" not in result.remediation.lower()


# ---------------------------------------------------------------------------
# check_codex_oauth_auth
# ---------------------------------------------------------------------------


class TestCheckCodexOauthAuth:
    def test_passes_when_codex_auth_json_exists_without_openai_key(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")
        (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            patch("ouroboros.cli.commands.mcp_doctor._get_runtime_backend", return_value="codex"),
            patch("ouroboros.cli.commands.mcp_doctor._get_llm_backend", return_value="codex"),
        ):
            result = check_codex_oauth_auth()

        assert result.status == "pass"
        assert "auth.json" in result.message
        assert "OPENAI_API_KEY not required" in result.message

    def test_fails_when_codex_backend_active_without_auth_json(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            patch("ouroboros.cli.commands.mcp_doctor._get_runtime_backend", return_value="hermes"),
            patch("ouroboros.cli.commands.mcp_doctor._get_llm_backend", return_value="codex"),
        ):
            result = check_codex_oauth_auth()

        assert result.status == "fail"
        assert "Codex backend active" in result.message
        assert "CODEX_HOME/HOME" in result.remediation
        assert "OPENAI_API_KEY" in result.remediation

    def test_passes_when_codex_backend_uses_openai_api_key_without_auth_json(
        self, tmp_path, monkeypatch
    ):
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        with (
            patch("ouroboros.cli.commands.mcp_doctor._get_runtime_backend", return_value="hermes"),
            patch("ouroboros.cli.commands.mcp_doctor._get_llm_backend", return_value="codex"),
        ):
            result = check_codex_oauth_auth()

        assert result.status == "pass"
        assert "OPENAI_API_KEY is present" in result.message
        assert "API-key-backed Codex profile" in result.message
        assert "codex login" in result.remediation

    def test_warns_when_codex_backend_inactive_without_auth_json(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        with (
            patch("ouroboros.cli.commands.mcp_doctor._get_runtime_backend", return_value="claude"),
            patch("ouroboros.cli.commands.mcp_doctor._get_llm_backend", return_value="claude_code"),
        ):
            result = check_codex_oauth_auth()

        assert result.status == "warn"
        assert "Codex backend not active" in result.message


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
    def _patch_paths(self, tmp_path):
        """Isolate both the legacy PID file and the per-instance registry."""
        return (
            patch("ouroboros.cli.commands.mcp_doctor._PID_FILE", tmp_path / "mcp-server.pid"),
            patch(
                "ouroboros.cli.commands.mcp_doctor._PID_REGISTRY_DIR",
                tmp_path / "mcp-servers",
            ),
        )

    def test_passes_when_no_pid_file(self, tmp_path):
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with pid_patch, registry_patch:
            result = check_pid_file()
        assert result.status == "pass"
        assert "not running" in result.message.lower() or "no pid" in result.message.lower()

    def test_passes_when_pid_alive(self, tmp_path):
        (tmp_path / "mcp-server.pid").write_text("12345", encoding="utf-8")
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with (
            pid_patch,
            registry_patch,
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=True),
        ):
            result = check_pid_file()
        assert result.status == "pass"
        assert "12345" in result.message

    def test_warns_when_pid_stale(self, tmp_path):
        (tmp_path / "mcp-server.pid").write_text("99999", encoding="utf-8")
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with (
            pid_patch,
            registry_patch,
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=False),
        ):
            result = check_pid_file()
        assert result.status == "warn"
        assert result.remediation != ""

    def test_warns_when_pid_file_unreadable(self, tmp_path):
        (tmp_path / "mcp-server.pid").write_text("not_a_number", encoding="utf-8")
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with pid_patch, registry_patch:
            result = check_pid_file()
        assert result.status == "warn"
        assert result.remediation != ""

    def test_registry_live_instances_pass(self, tmp_path):
        registry = tmp_path / "mcp-servers"
        registry.mkdir()
        (registry / "111.pid").write_text("111 1700000000.0", encoding="utf-8")
        (registry / "222.pid").write_text("222 None", encoding="utf-8")
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with (
            pid_patch,
            registry_patch,
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=True),
        ):
            result = check_pid_file()
        assert result.status == "pass"
        assert "2 MCP server instance(s)" in result.message
        assert "111" in result.message
        assert "222" in result.message

    def test_registry_stale_records_warn_without_kill_advice(self, tmp_path):
        registry = tmp_path / "mcp-servers"
        registry.mkdir()
        (registry / "333.pid").write_text("333 1700000000.0", encoding="utf-8")
        pid_patch, registry_patch = self._patch_paths(tmp_path)
        with (
            pid_patch,
            registry_patch,
            patch("ouroboros.cli.commands.mcp_doctor._pid_is_alive", return_value=False),
        ):
            result = check_pid_file()
        assert result.status == "warn"
        assert "stale instance record" in result.message
        assert "kill" not in result.remediation.lower() or "do not kill" in result.remediation


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


def _fake_probe_client(
    adapter,
    captured: dict[str, object],
    *,
    enter_error: Exception | None = None,
    exit_error: Exception | None = None,
):
    """Return an adapter factory with explicit connection and cleanup results."""

    def create_client(*, max_retries):
        captured["max_retries"] = max_retries

        async def connect(config):
            captured["config"] = config
            if enter_error is not None:
                adapter.transport_entered = False
                return Result.err(enter_error)
            adapter.transport_entered = True
            return Result.ok(None)

        async def disconnect():
            captured["cleaned_up"] = True
            return Result.err(exit_error) if exit_error else Result.ok(None)

        adapter.connect = AsyncMock(side_effect=connect)
        adapter.disconnect = AsyncMock(side_effect=disconnect)
        return adapter

    return create_client


def _successful_probe_adapter(*tool_names: str):
    adapter = MagicMock()
    adapter.server_snapshot = SimpleNamespace(protocol_version="2025-06-18")
    adapter.protocol_version = "2025-06-18"
    adapter.list_tools = AsyncMock(
        return_value=Result.ok(tuple(SimpleNamespace(name=tool_name) for tool_name in tool_names))
    )
    adapter.call_tool = AsyncMock()
    return adapter


class TestLocalStdioProbe:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("host_platform", ["darwin", "win32"])
    async def test_uses_exact_local_stdio_command_on_supported_host_families(
        self, host_platform, monkeypatch
    ):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter("ouroboros_interview", "ouroboros_run")
        monkeypatch.setenv("OUROBOROS_MCP_COMMAND", "do-not-execute --configured-command")

        with (
            patch.object(sys, "platform", host_platform),
            patch(
                "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
                _fake_probe_client(adapter, captured),
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._expected_ouroboros_tool_names",
                return_value=frozenset({"ouroboros_interview", "ouroboros_run"}),
            ),
        ):
            results = await _probe_local_stdio()

        config = captured["config"]
        assert config.command == sys.executable
        assert config.args[:3] == ("-I", "-B", "-c")
        assert "os.chdir(" in config.args[3]
        assert "_serve_local_stdio_probe" in config.args[3]
        assert "do-not-execute" not in config.args[3]
        assert config.transport is TransportType.STDIO
        assert config.url is None
        assert "OUROBOROS_MCP_COMMAND" not in config.env
        assert config.env["HOME"] == config.env["USERPROFILE"]
        assert captured["max_retries"] == 1
        assert captured["cleaned_up"] is True
        assert [result.status for result in results] == ["pass", "pass", "pass"]
        adapter.list_tools.assert_awaited_once_with()
        adapter.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_startup_failure_is_staged_and_fail_closed(self):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter("ouroboros_interview")
        with patch(
            "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
            _fake_probe_client(
                adapter,
                captured,
                enter_error=OSError("child could not start"),
            ),
        ):
            results = await _probe_local_stdio()

        assert [result.name for result in results] == [
            "local_stdio_startup_transport",
            "local_stdio_protocol_discovery",
            "local_stdio_tool_recognition",
        ]
        assert [result.status for result in results] == ["fail", "fail", "fail"]
        assert "child could not start" in results[0].message
        assert all(result.remediation for result in results)
        adapter.list_tools.assert_not_awaited()
        adapter.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_discovery_snapshot_stops_before_tool_listing(self):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter("ouroboros_interview")
        adapter.server_snapshot = None
        with patch(
            "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
            _fake_probe_client(adapter, captured),
        ):
            results = await _probe_local_stdio()

        assert [result.status for result in results] == ["pass", "fail", "fail"]
        assert results[1].name == "local_stdio_protocol_discovery"
        assert "no snapshot" in results[1].message
        assert captured["cleaned_up"] is True
        adapter.list_tools.assert_not_awaited()
        adapter.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_tools_failure_is_distinct_and_cleans_up(self):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter()
        adapter.list_tools.return_value = Result.err(RuntimeError("tools/list unavailable"))
        with patch(
            "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
            _fake_probe_client(adapter, captured),
        ):
            results = await _probe_local_stdio()

        assert [result.status for result in results] == ["pass", "pass", "fail"]
        assert results[2].name == "local_stdio_tool_recognition"
        assert "tools/list unavailable" in results[2].message
        assert results[2].remediation
        assert captured["cleaned_up"] is True
        adapter.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_expected_tool_fails_without_invoking_any_tool(self):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter("ouroboros_interview")
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
                _fake_probe_client(adapter, captured),
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._expected_ouroboros_tool_names",
                return_value=frozenset({"ouroboros_interview", "ouroboros_run"}),
            ),
        ):
            results = await _probe_local_stdio()

        assert [result.status for result in results] == ["pass", "pass", "fail"]
        assert "ouroboros_run" in results[2].message
        assert captured["cleaned_up"] is True
        adapter.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cleanup_failure_turns_probe_into_lifecycle_failure(self):
        captured: dict[str, object] = {}
        adapter = _successful_probe_adapter("ouroboros_interview")
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor.MCPClientAdapter",
                _fake_probe_client(
                    adapter,
                    captured,
                    exit_error=RuntimeError("child reap failed"),
                ),
            ),
            patch(
                "ouroboros.cli.commands.mcp_doctor._expected_ouroboros_tool_names",
                return_value=frozenset({"ouroboros_interview"}),
            ),
        ):
            results = await _probe_local_stdio()

        assert captured["cleaned_up"] is True
        assert [result.status for result in results] == ["fail", "fail", "fail"]
        assert "child reap failed" in results[0].message
        adapter.call_tool.assert_not_awaited()


class TestDoctorCommand:
    def test_default_does_not_launch_local_probe(self):
        app = _make_app()
        passing = CheckResult(name="existing", status="pass", message="ok")
        probe = AsyncMock()
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
                [lambda: passing],
            ),
            patch("ouroboros.cli.commands.mcp_doctor._probe_local_stdio", probe),
        ):
            result = runner.invoke(app, [])

        assert result.exit_code == 0
        assert "local_stdio" not in result.output
        probe.assert_not_awaited()

    def test_exact_opt_in_flag_adds_probe_results_and_preserves_json(self):
        app = _make_app()
        passing = CheckResult(name="existing", status="pass", message="ok")
        probe_results = [
            CheckResult(
                name="local_stdio_startup_transport",
                status="pass",
                message="connected",
            ),
            CheckResult(
                name="local_stdio_protocol_discovery",
                status="pass",
                message="negotiated",
            ),
            CheckResult(
                name="local_stdio_tool_recognition",
                status="pass",
                message="recognized",
            ),
        ]
        probe = AsyncMock(return_value=probe_results)
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
                [lambda: passing],
            ),
            patch("ouroboros.cli.commands.mcp_doctor._probe_local_stdio", probe),
        ):
            result = runner.invoke(app, ["--probe-local-stdio", "--json"])

        assert result.exit_code == 0
        assert [item["name"] for item in json.loads(result.output)] == [
            "existing",
            "local_stdio_startup_transport",
            "local_stdio_protocol_discovery",
            "local_stdio_tool_recognition",
        ]
        probe.assert_awaited_once_with()

    def test_probe_failure_uses_existing_exit_one_behavior(self):
        app = _make_app()
        passing = CheckResult(name="existing", status="pass", message="ok")
        probe = AsyncMock(
            return_value=[
                CheckResult(
                    name="local_stdio_startup_transport",
                    status="fail",
                    message="broken",
                    remediation="repair it",
                )
            ]
        )
        with (
            patch(
                "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
                [lambda: passing],
            ),
            patch("ouroboros.cli.commands.mcp_doctor._probe_local_stdio", probe),
        ):
            result = runner.invoke(app, ["--probe-local-stdio", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output)[-1]["status"] == "fail"

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
        check_result = CheckResult(name="mcp", status="pass", message="mcp 2.0.0")
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

    def test_human_output_preserves_literal_package_profiles(self):
        app = _make_app()
        check_result = CheckResult(
            name="claude_agent_sdk_import",
            status="fail",
            message=UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
            remediation="Use ouroboros-ai[mcp,claude-cli] in the MCP 2 process.",
        )
        with patch(
            "ouroboros.cli.commands.mcp_doctor._ALL_CHECKS",
            [lambda: check_result],
        ):
            result = runner.invoke(app, [])

        assert result.exit_code == 1
        for profile in (
            "ouroboros-ai[mcp]",
            "ouroboros-ai[claude]",
            "[claude-sdk]",
            "[claude-cli]",
            "ouroboros-ai[mcp,claude-cli]",
        ):
            assert profile in result.output

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
        pass_result = CheckResult(name="mcp_import", status="pass", message="mcp 2.0.0")
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
