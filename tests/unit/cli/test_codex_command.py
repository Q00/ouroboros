"""Unit tests for Codex integration helper commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import textwrap
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from ouroboros.cli.commands.codex import (
    _MCP_PROTOCOL_VERSION,
    _check_auto_dispatch_surface,
    _list_stdio_mcp_tool_names,
    app,
)
from ouroboros.codex import CodexArtifactInstallResult, install_codex_artifacts

runner = CliRunner()
_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST = {
    "ouroboros_auto",
    "ouroboros_start_auto",
    "ouroboros_interview",
    "ouroboros_generate_seed",
}


class TestCodexRefresh:
    """Tests for `ouroboros codex refresh`."""

    def test_refresh_installs_rules_and_skills_without_config_files(self, tmp_path: Path) -> None:
        rules_path = tmp_path / ".codex" / "rules" / "ouroboros.md"
        skill_paths = (
            tmp_path / ".codex" / "skills" / "ouroboros-interview",
            tmp_path / ".codex" / "skills" / "ouroboros-run",
        )
        result = CodexArtifactInstallResult(rules_path=rules_path, skill_paths=skill_paths)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.codex.install_codex_artifacts", return_value=result
            ) as mock_install,
        ):
            cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 0
        mock_install.assert_called_once_with(codex_dir=tmp_path / ".codex", prune=False)
        assert "Installed Codex rules" in cli_result.output
        assert "Installed 2 Codex skills" in cli_result.output
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".ouroboros" / "config.yaml").exists()


class TestCodexDoctor:
    """Tests for `ouroboros codex doctor`."""

    @staticmethod
    def _write_healthy_codex_surface(codex_dir: Path) -> None:
        rules_path = codex_dir / "rules" / "ouroboros.md"
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            "| `ooo auto ...` | `ouroboros_auto` |\n"
            "Do not emulate it with manual work. If unavailable, stop.\n",
            encoding="utf-8",
        )

        skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(
            "---\n"
            "name: auto\n"
            "mcp_tool: ouroboros_auto\n"
            "---\n"
            "Manual fallback is not allowed when the tool is unavailable.\n",
            encoding="utf-8",
        )

        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

    def test_check_auto_dispatch_surface_passes_for_healthy_install(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_accepts_url_mcp_server_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_live_mcp_rejects_url_mcp_server_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names") as live_probe:
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        live_probe.assert_not_called()
        assert any("uses `url`" in failure for failure in failures)
        assert any("verifies only stdio `command` entries" in failure for failure in failures)

    def test_check_auto_dispatch_surface_accepts_custom_command_mcp_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "/opt/bin/ob-mcp-wrapper"\nargs = ["--stdio"]\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_reports_uvx_without_mcp_extra(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("without the `mcp` extra" in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_direct_ouroboros_without_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "/home/user/.local/bin/ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex"]\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None):
            failures = _check_auto_dispatch_surface(codex_dir)

        assert any("cannot import `mcp`" in failure for failure in failures)

    def test_check_auto_dispatch_surface_accepts_direct_ouroboros_with_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex"]\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()):
            assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_live_mcp_verifies_required_tools(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n',
            encoding="utf-8",
        )

        live_probe = AsyncMock(
            return_value={
                "ouroboros_auto",
                "ouroboros_start_auto",
                "ouroboros_interview",
                "ouroboros_generate_seed",
            }
        )
        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=True) == []

        live_probe.assert_awaited_once_with(
            "ouroboros",
            ("mcp", "serve", "--runtime", "codex"),
            {"OUROBOROS_AGENT_RUNTIME": "codex"},
        )

    def test_list_stdio_mcp_tool_names_uses_protocol_without_local_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        assert _MCP_PROTOCOL_VERSION == "2024-11-05"
        server_path = tmp_path / "fake_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                def read_message():
                    content_length = None
                    while True:
                        line = sys.stdin.buffer.readline()
                        if not line:
                            raise SystemExit(0)
                        line = line.strip()
                        if not line:
                            break
                        key, _, value = line.partition(b":")
                        if key.lower() == b"content-length":
                            content_length = int(value.strip())
                    return json.loads(sys.stdin.buffer.read(content_length))

                def write_message(message):
                    body = json.dumps(message).encode("utf-8")
                    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                    sys.stdout.buffer.flush()

                initialize = read_message()
                if initialize["params"]["protocolVersion"] != "2024-11-05":
                    raise SystemExit(
                        f"unsupported protocol {initialize['params']['protocolVersion']}"
                    )
                write_message({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                })
                read_message()  # notifications/initialized
                tools_list = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": tools_list["id"],
                    "result": {
                        "tools": [
                            {"name": "ouroboros_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_start_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_interview", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_generate_seed", "inputSchema": {"type": "object"}},
                        ]
                    },
                })
                """
            ),
            encoding="utf-8",
        )

        tool_names = asyncio.run(
            _list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {})
        )

        assert tool_names >= _REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST

    def test_check_auto_dispatch_surface_live_mcp_accepts_uvx_mcp_extra_without_local_mcp(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=True) == []

        live_probe.assert_awaited_once_with(
            "uvx",
            ("--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"),
            {},
        )

    def test_check_auto_dispatch_surface_live_mcp_reports_missing_auto_tool(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        with patch(
            "ouroboros.cli.commands.codex._list_stdio_mcp_tool_names",
            AsyncMock(return_value={"ouroboros_interview", "ouroboros_generate_seed"}),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("missing required auto tools" in failure for failure in failures)
        assert any("ouroboros_auto" in failure for failure in failures)

    def test_check_auto_dispatch_surface_live_mcp_reports_handshake_failure(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        with patch(
            "ouroboros.cli.commands.codex._list_stdio_mcp_tool_names",
            AsyncMock(side_effect=RuntimeError("connection closed during initialize")),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("initialize/list_tools failed" in failure for failure in failures)
        assert any("connection closed during initialize" in failure for failure in failures)

    def test_packaged_codex_artifacts_satisfy_doctor_contract(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        install_codex_artifacts(codex_dir=codex_dir, prune=False)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_reports_missing_auto_contract(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        (codex_dir / "rules").mkdir(parents=True)
        (codex_dir / "rules" / "ouroboros.md").write_text(
            "| `ooo run <seed.yaml>` | `ouroboros_execute_seed` |\n",
            encoding="utf-8",
        )
        (codex_dir / "skills" / "ouroboros-auto").mkdir(parents=True)
        (codex_dir / "skills" / "ouroboros-auto" / "SKILL.md").write_text(
            "---\nname: auto\n---\n# Auto\n",
            encoding="utf-8",
        )
        (codex_dir / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")

        failures = _check_auto_dispatch_surface(codex_dir)

        assert "Codex rules do not map `ooo auto` to `ouroboros_auto`" in failures
        assert "auto skill does not declare `mcp_tool: ouroboros_auto`" in failures
        assert "Codex config does not contain [mcp_servers.ouroboros]" in failures

    def test_doctor_command_exits_nonzero_when_dispatch_surface_is_broken(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "Codex ooo auto dispatch: BROKEN" in cli_result.output
        assert "missing Codex rules file" in cli_result.output

    def test_doctor_command_reports_unreadable_artifact_without_traceback(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "skills" / "ouroboros-auto" / "SKILL.md").write_bytes(b"\xff")

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "auto skill is not valid UTF-8" in cli_result.output
        assert not isinstance(cli_result.exception, UnicodeDecodeError)

    def test_doctor_command_reports_ok_for_healthy_install(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 0
        assert "Codex ooo auto dispatch: OK" in cli_result.output

    def test_doctor_live_mcp_url_config_fails_without_stdio_success_claim(
        self, tmp_path: Path
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir), "--live-mcp"])

        assert cli_result.exit_code == 1
        assert "Codex ooo auto dispatch: BROKEN" in cli_result.output
        assert "currently verifies only" in cli_result.output
        assert "stdio `command` entries" in cli_result.output
        assert (
            "live stdio initialize/list_tools exposes required auto tools" not in cli_result.output
        )
