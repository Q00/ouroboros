"""Unit tests for the Codex CLI-backed LLM adapter."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.codex_cli_stream import (
    DEFAULT_CLI_COMPLETION_TIMEOUT_SECONDS,
    collect_stream_lines,
    terminate_runtime_process,
)


class _FakeStream:
    def __init__(
        self,
        text: str = "",
        *,
        read_size: int | None = None,
    ) -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0
        self._read_size = read_size

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""

        size = self._read_size or chunk_size
        next_cursor = min(self._cursor + size, len(self._buffer))
        chunk = self._buffer[self._cursor : next_cursor]
        self._cursor = next_cursor
        return chunk


class _FakeStdin:
    """Minimal stdin stub that captures written bytes."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        wait_forever: bool = False,
        read_size: int | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout, read_size=read_size)
        self.stderr = _FakeStream(stderr, read_size=read_size)
        self.returncode = None if wait_forever else returncode
        self._final_returncode = returncode
        self._wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self._wait_forever and self.returncode is None:
            await asyncio.Future()
        self.returncode = self._final_returncode
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        raise AssertionError("communicate() should not be used by the streaming adapter")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


class _LegacyFakeProcess:
    """Process stub that exercises the communicate() fallback branch."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self.communicate_calls = 0

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        return self._stdout, self._stderr


class _ProcessGroupFakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return -1 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()


@pytest.mark.asyncio
async def test_terminate_runtime_process_signals_process_group_before_fallback() -> None:
    """Group-aware cleanup reaches wrapper descendants instead of only the parent."""
    process = _ProcessGroupFakeProcess()
    sent_signals: list[int] = []

    def fake_killpg(_pgid: int, sig: int) -> None:
        sent_signals.append(sig)
        if sig == signal.SIGKILL:
            process.returncode = -9
            process._done.set()

    with (
        patch("ouroboros.providers.codex_cli_stream.os.getpgid", return_value=4242),
        patch("ouroboros.providers.codex_cli_stream.os.killpg", side_effect=fake_killpg),
    ):
        await terminate_runtime_process(
            process,
            shutdown_timeout=0.001,
            terminate_process_group=True,
        )

    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.terminated is False
    assert process.killed is False


@pytest.mark.asyncio
async def test_terminate_runtime_process_signals_saved_group_after_parent_exit() -> None:
    """Completed parent processes can still reap companion process group members."""
    process = _ProcessGroupFakeProcess()
    process.returncode = 0
    sent_signals: list[int] = []

    def fake_killpg(_pgid: int, sig: int) -> None:
        sent_signals.append(sig)

    with patch("ouroboros.providers.codex_cli_stream.os.killpg", side_effect=fake_killpg):
        await terminate_runtime_process(
            process,
            shutdown_timeout=0.001,
            terminate_process_group=True,
            process_group_id=4242,
        )

    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.terminated is False
    assert process.killed is False


class TestCodexCliLLMAdapter:
    """Tests for CodexCliLLMAdapter."""

    @staticmethod
    def _write_wrapper(path: Path) -> Path:
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32 + b"zeude codex-wrapper")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_real_cli(path: Path) -> Path:
        path.write_text("#!/usr/bin/env node\nconsole.log('codex')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_profile_help_cli(
        path: Path,
        help_text: str,
        *,
        delay_seconds: float = 0.0,
    ) -> Path:
        """Write an executable CLI probe with a production-shaped help contract."""
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "import time\n"
            "if '--help' in sys.argv:\n"
            f"    time.sleep({delay_seconds!r})\n"
            f"    print({help_text!r})\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_unlaunchable_cli(path: Path) -> Path:
        """Write an executable whose missing interpreter makes subprocess raise OSError."""
        path.write_text("#!/definitely/missing/python\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_build_prompt_preserves_system_and_roles(self) -> None:
        """Prompt builder keeps system instructions and conversation order."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Follow JSON strictly."),
                Message(role=MessageRole.USER, content="Explain the bug."),
                Message(role=MessageRole.ASSISTANT, content="Need more context."),
                Message(role=MessageRole.USER, content="It fails on startup."),
            ]
        )

        assert "## System Instructions" in prompt
        assert "Follow JSON strictly." in prompt
        assert "User: Explain the bug." in prompt
        assert "Assistant: Need more context." in prompt
        assert "User: It fails on startup." in prompt

    def test_build_prompt_includes_tool_constraints_and_turn_budget(self) -> None:
        """Prompt includes advisory interview settings for backend parity."""
        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            allowed_tools=["Read", "Grep"],
            max_turns=5,
        )

        prompt = adapter._build_prompt(
            [Message(role=MessageRole.USER, content="Inspect the repo.")]
        )

        assert "## Tool Constraints" in prompt
        assert "- Read" in prompt
        assert "- Grep" in prompt
        assert "## Execution Budget" in prompt
        assert "5 tool-assisted turns" in prompt

    def test_build_prompt_omits_tool_constraints_when_tools_unspecified(self) -> None:
        """Default adapters keep tool policy unspecified for non-interview flows."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Summarize this.")])

        assert "## Tool Constraints" not in prompt
        assert "Do NOT use any tools or MCP calls" not in prompt

    def test_build_prompt_explicit_empty_tools_forbids_tool_use(self) -> None:
        """An explicit empty tool list requests a text-only response."""
        adapter = CodexCliLLMAdapter(cli_path="codex", allowed_tools=[], max_turns=5)

        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Summarize this.")])

        assert "## Tool Constraints" in prompt
        assert "Do NOT use any tools or MCP calls" in prompt
        assert "tool-assisted turns" not in prompt
        assert "avoid turning this into a multi-step tool workflow" in prompt

    def test_normalize_model_omits_default_sentinel(self) -> None:
        """The backend-safe default sentinel is translated to no explicit model."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        assert adapter._normalize_model("default") is None
        assert adapter._normalize_model(" o3 ") == "o3"

    def test_resolve_cli_path_falls_back_from_wrapper(self, tmp_path: Path) -> None:
        """Provider adapter should apply the same wrapper-safe fallback as runtime."""
        wrapper = self._write_wrapper(tmp_path / "codex-wrapper")
        real_dir = tmp_path / "bin"
        real_dir.mkdir()
        real_cli = self._write_real_cli(real_dir / "codex")

        with (
            patch.dict(os.environ, {"PATH": str(real_dir)}),
            patch("ouroboros.providers.codex_cli_adapter.log.warning") as mock_warning,
            patch("ouroboros.providers.codex_cli_adapter.log.info") as mock_info,
        ):
            adapter = CodexCliLLMAdapter(cli_path=wrapper)

        assert adapter._cli_path == str(real_cli)
        mock_warning.assert_called_once_with(
            "codex_cli_adapter.cli_wrapper_detected",
            wrapper_path=str(wrapper),
            hint="Searching PATH for the real Codex CLI.",
        )
        mock_info.assert_called_once_with(
            "codex_cli_adapter.cli_resolved_via_fallback",
            fallback_path=str(real_cli),
        )

    def test_build_command_uses_read_only_by_default(self) -> None:
        """Default permission mode maps to a read-only sandbox."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--sandbox" in command
        assert "read-only" in command
        assert "mcp_servers.ouroboros.enabled=false" not in command

    @pytest.mark.parametrize(
        "transport",
        ('command = "ouroboros"', 'url = "http://127.0.0.1:8765/mcp"'),
    )
    def test_strict_child_disables_only_configured_ouroboros_mcp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        transport: str,
    ) -> None:
        """Strict isolation is a transient override and leaves other MCP state intact."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        config_path = codex_home / "config.toml"
        original = (
            f"[mcp_servers.ouroboros]\n{transport}\n\n"
            '[mcp_servers.other]\ncommand = "other-server"\n'
        )
        config_path.write_text(original, encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "mcp_servers.ouroboros.enabled=false" in command
        assert "mcp_servers.other.enabled=false" not in command
        assert config_path.read_text(encoding="utf-8") == original

    @pytest.mark.parametrize(
        ("runtime_profile", "task_profile", "profile_name"),
        (
            (None, "custom-task", "custom-task"),
            ("worker", "ignored-task", "ouroboros-worker"),
        ),
    )
    def test_strict_child_reads_effective_profile_v2_mcp_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime_profile: str | None,
        task_profile: str,
        profile_name: str,
    ) -> None:
        """The profile selected by the command participates in strict isolation."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / f"{profile_name}.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=True
        ):
            adapter = CodexCliLLMAdapter(
                cli_path="codex",
                runtime_profile=runtime_profile,
                strict_mcp_config=True,
            )

            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile=task_profile,
            )

        assert "mcp_servers.ouroboros.enabled=false" in command

    def test_strict_child_detects_actual_profile_v2_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real unified help probe enables the safe profile transport override."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "custom.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        cli = self._write_profile_help_cli(
            tmp_path / "codex-unified",
            "  -p, --profile <CONFIG_PROFILE_V2>\n"
            "          Layer $CODEX_HOME/<name>.config.toml on top of the base user config",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="custom",
        )

        assert command[0] == str(cli)
        assert command[-2:] == ["--profile", "custom"]
        assert "mcp_servers.ouroboros.enabled=false" in command

    @pytest.mark.parametrize(
        ("base_server", "profile_server"),
        (
            ('command = "ouroboros"\nenabled = false', "enabled = true"),
            ("enabled = true", 'command = "ouroboros"'),
        ),
    )
    def test_strict_child_deep_merges_profile_v2_mcp_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        base_server: str,
        profile_server: str,
    ) -> None:
        """Split MCP fields are evaluated from Codex's effective merged config."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        config_path = codex_home / "config.toml"
        profile_path = codex_home / "custom.config.toml"
        base_contents = (
            f"[mcp_servers.ouroboros]\n{base_server}\n\n"
            '[mcp_servers.other]\ncommand = "other-server"\n'
        )
        profile_contents = (
            f"[mcp_servers.ouroboros]\n{profile_server}\n\n[mcp_servers.other]\nenabled = false\n"
        )
        config_path.write_text(base_contents, encoding="utf-8")
        profile_path.write_text(profile_contents, encoding="utf-8")
        cli = self._write_profile_help_cli(
            tmp_path / "codex-unified",
            "  -p, --profile <CONFIG_PROFILE_V2>\n"
            "          Layer $CODEX_HOME/<name>.config.toml on top of the base user config",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="custom",
        )

        assert "mcp_servers.ouroboros.enabled=false" in command
        assert "mcp_servers.other.enabled=false" not in command
        assert config_path.read_text(encoding="utf-8") == base_contents
        assert profile_path.read_text(encoding="utf-8") == profile_contents

    def test_strict_profile_v2_respects_profile_disable_after_deep_merge(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A selected profile can disable an otherwise effective base transport."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        (codex_home / "custom.config.toml").write_text(
            "[mcp_servers.ouroboros]\nenabled = false\n", encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=True
        ):
            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_strict_child_fails_closed_when_unified_help_exceeds_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A slow supported selector cannot silently inherit profile-scoped MCP."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "custom.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        # Shrink the probe budget (and the CLI's stall) rather than waiting out
        # the production 5s grace period; the contract under test is that any
        # overrun is uncertainty, not the length of the overrun.
        monkeypatch.setattr(
            "ouroboros.codex.runtime_profile.HELP_PROBE_TIMEOUT_SECONDS",
            0.2,
        )
        cli = self._write_profile_help_cli(
            tmp_path / "codex-slow-unified",
            "  -p, --profile <CONFIG_PROFILE_V2>\n"
            "          Layer $CODEX_HOME/<name>.config.toml on top of the base user config",
            delay_seconds=1.0,
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        with pytest.raises(ProviderError, match="Cannot guarantee strict MCP isolation"):
            adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

    def test_strict_child_fails_closed_when_help_raises_oserror(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unlaunchable resolved CLI is unknown, never definitive legacy mode."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "custom.config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:8765/mcp"\n',
            encoding="utf-8",
        )
        cli = self._write_unlaunchable_cli(tmp_path / "codex-broken-interpreter")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        assert adapter._cli_path == str(cli)
        with pytest.raises(ProviderError, match="Cannot guarantee strict MCP isolation"):
            adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

    @pytest.mark.asyncio
    async def test_strict_unknown_profile_mode_never_starts_child_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Provider completion returns an isolation error before spawning Codex."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "ouroboros-worker.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        cli = self._write_unlaunchable_cli(tmp_path / "codex-broken-interpreter")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(
            cli_path=cli,
            runtime_profile="worker",
            strict_mcp_config=True,
        )

        with patch("ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec") as spawn:
            result = await adapter._complete_once(
                [Message(role=MessageRole.USER, content="Do not inherit MCP")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "Cannot guarantee strict MCP isolation" in str(result.error)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_strict_unknown_profile_mode_fails_closed_for_merged_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown selector mode cannot ignore transport fields split across layers."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\nenabled = false\n',
            encoding="utf-8",
        )
        (codex_home / "ouroboros-worker.config.toml").write_text(
            "[mcp_servers.ouroboros]\nenabled = true\n", encoding="utf-8"
        )
        cli = self._write_unlaunchable_cli(tmp_path / "codex-broken-interpreter")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(
            cli_path=cli,
            runtime_profile="worker",
            strict_mcp_config=True,
        )

        with patch("ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec") as spawn:
            result = await adapter._complete_once(
                [Message(role=MessageRole.USER, content="Do not inherit merged MCP")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "Cannot guarantee strict MCP isolation" in str(result.error)
        spawn.assert_not_called()

    def test_strict_legacy_profile_ignores_dormant_profile_v2_mcp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy --profile must not inspect a dormant profile-v2 config file."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[profiles.custom]\nmodel = "gpt-5.4"\n', encoding="utf-8"
        )
        (codex_home / "custom.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=False
        ):
            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_strict_legacy_profile_does_not_merge_dormant_transport_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Definitive legacy mode never completes base MCP from a dormant v2 file."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\nenabled = false\n',
            encoding="utf-8",
        )
        (codex_home / "custom.config.toml").write_text(
            "[mcp_servers.ouroboros]\nenabled = true\n", encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=False
        ):
            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_strict_legacy_profile_ignores_nested_mcp_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex 0.133 does not promote nested profile MCP into the base namespace."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[profiles.custom.mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=False
        ):
            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

        assert command[1] == "exec"
        assert command[-2:] == ["--profile", "custom"]
        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_strict_child_detects_actual_legacy_nested_transport_as_dormant(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real legacy help probe does not promote nested profile MCP state."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[profiles.custom.mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
            encoding="utf-8",
        )
        (codex_home / "custom.config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "dormant"\n', encoding="utf-8"
        )
        cli = self._write_profile_help_cli(
            tmp_path / "codex-legacy",
            "  -p, --profile <CONFIG_PROFILE>\n"
            "          Configuration profile from config.toml to specify default options",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="custom",
        )

        assert command[-2:] == ["--profile", "custom"]
        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_strict_legacy_profile_disables_top_level_mcp_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex 0.133 still loads base MCP while selecting legacy profile options."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "\n".join(
                (
                    "[mcp_servers.ouroboros]",
                    'command = "ouroboros"',
                    "",
                    "[profiles.custom]",
                    'model = "gpt-5.4"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        # Pin the legacy contract instead of probing whichever ``codex`` happens
        # to be installed on the machine running the tests.
        with patch(
            "ouroboros.providers.codex_cli_adapter.codex_uses_profile_v2", return_value=False
        ):
            command = adapter._build_command(
                output_last_message_path="/tmp/out.txt",
                output_schema_path=None,
                model=None,
                profile="custom",
            )

        assert command[-2:] == ["--profile", "custom"]
        assert "mcp_servers.ouroboros.enabled=false" in command

    def test_strict_child_disables_top_level_transport_without_help_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Base MCP isolation is deterministic even when help cannot execute."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n', encoding="utf-8"
        )
        cli = self._write_unlaunchable_cli(tmp_path / "codex-broken-interpreter")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path=cli, strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="custom",
        )

        assert command[-2:] == ["--profile", "custom"]
        assert "mcp_servers.ouroboros.enabled=false" in command

    @pytest.mark.parametrize(
        "contents",
        (
            None,
            "[mcp_servers.ouroboros\n",
            '[mcp_servers.ouroboros]\ncommand = ""\n',
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\nenabled = false\n',
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\nenabled = "false"\n',
        ),
    )
    def test_strict_child_does_not_synthesize_invalid_mcp_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        contents: str | None,
    ) -> None:
        """Missing, malformed, empty, or disabled servers receive no override table."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        if contents is not None:
            (codex_home / "config.toml").write_text(contents, encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex", strict_mcp_config=True)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_non_strict_child_keeps_configured_ouroboros_mcp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary Codex completion commands preserve the user's MCP configuration."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "mcp_servers.ouroboros.enabled=false" not in command

    def test_build_command_forwards_valid_reasoning_effort(self) -> None:
        """Completion profiles retain their per-call Codex effort after setup migration."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            reasoning_effort="high",
        )

        assert "-c" in command
        assert "model_reasoning_effort=high" in command

    def test_build_command_rejects_unknown_reasoning_effort(self) -> None:
        """Only Codex's allow-listed effort values reach its config override."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            reasoning_effort="drop-table",
        )

        assert not any("model_reasoning_effort" in arg for arg in command)

    def test_build_command_prefers_profile_over_model(self) -> None:
        """Codex task profiles use --profile and avoid a conflicting --model."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model="gpt-5.4",
            profile="ouroboros-deep",
        )

        assert "--profile" in command
        assert "ouroboros-deep" in command
        assert "--model" not in command

    def test_build_command_matches_codex_0134_unified_profile_v2_contract(self) -> None:
        """Codex 0.134 uses --profile to load ~/.codex/<name>.config.toml files."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="ouroboros-frontier",
        )

        assert "--profile" in command
        assert "--profile-v2" not in command
        assert command[command.index("--profile") + 1] == "ouroboros-frontier"

    @pytest.mark.asyncio
    async def test_complete_resolves_codex_profile_from_task_role(self) -> None:
        """Role profile resolution reaches the Codex CLI command line."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        task_config = OuroborosConfig(
            llm_profiles={
                "fast": {
                    "providers": {
                        "codex": {
                            "profile": "ouroboros-fast",
                            "model": "gpt-5.3-codex-spark",
                        },
                    },
                },
            },
            llm_role_profiles={"qa": "fast"},
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Profiled answer", encoding="utf-8")
            assert "--profile" in command
            assert "ouroboros-fast" in command
            assert "--model" not in command
            return _FakeProcess(returncode=0)

        with (
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch("ouroboros.providers.profiles.load_config", return_value=task_config),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a QA verdict.")],
                CompletionConfig(model="default", role="qa"),
            )

        assert result.is_ok
        assert result.value.content == "Profiled answer"

    def test_build_command_uses_full_auto_for_accept_edits(self) -> None:
        """acceptEdits maps to Codex full-auto mode."""
        adapter = CodexCliLLMAdapter(cli_path="codex", permission_mode="acceptEdits")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--full-auto" in command
        assert "--sandbox" not in command

    def test_build_command_uses_dangerous_bypass_when_requested(self) -> None:
        """bypassPermissions maps to the Codex dangerous bypass flag."""
        adapter = CodexCliLLMAdapter(cli_path="codex", permission_mode="bypassPermissions")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in command

    def test_build_command_omits_profile_flag_when_runtime_profile_unset(self) -> None:
        """Default runtime_profile=None preserves existing command shape (regression)."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--profile" not in command

    def test_build_command_adds_worker_profile_when_configured(self) -> None:
        """runtime_profile='worker' maps to Codex `--profile ouroboros-worker`."""
        adapter = CodexCliLLMAdapter(cli_path="codex", runtime_profile="worker")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--profile" in command
        profile_index = command.index("--profile")
        assert command[profile_index + 1] == "ouroboros-worker"
        assert profile_index < command.index("--json")

    def test_build_command_skips_unknown_runtime_profile_with_warning(self) -> None:
        """Unmapped runtime_profile values fall back to no profile flag and log a warning."""
        with patch("ouroboros.providers.codex_cli_adapter.log.warning") as mock_warning:
            adapter = CodexCliLLMAdapter(cli_path="codex", runtime_profile="future-tier")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--profile" not in command
        mock_warning.assert_called_once()
        assert mock_warning.call_args.args[0] == "codex_cli_adapter.runtime_profile_unmapped"
        assert mock_warning.call_args.kwargs["runtime_profile"] == "future-tier"

    def test_runtime_profile_prevents_duplicate_task_profile_flags(self) -> None:
        """Worker isolation owns Codex's singular --profile flag over task profiles."""
        adapter = CodexCliLLMAdapter(cli_path="codex", runtime_profile="worker")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
            profile="ouroboros-fast",
        )

        assert command.count("--profile") == 1
        assert command[command.index("--profile") + 1] == "ouroboros-worker"
        assert "ouroboros-fast" not in command

    @pytest.mark.asyncio
    async def test_complete_success_reads_output_file(self) -> None:
        """Successful completions return the CLI output and session id."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            assert "--model" not in command
            assert Path(kwargs["cwd"]) == Path("/tmp/project")
            # Prompt is now fed via stdin, not as a positional argument
            assert kwargs.get("stdin") is not None
            return _FakeProcess(
                stdout=json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                returncode=0,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Summarize this change.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert result.value.content == "Final answer"
        assert result.value.model == "default"
        assert result.value.raw_response["session_id"] == "thread-123"

    @pytest.mark.asyncio
    async def test_complete_passes_json_schema_output_constraints(self) -> None:
        """Structured-output requests write and pass a JSON schema file."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text('{"approved": true}', encoding="utf-8")

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a verdict.")],
                CompletionConfig(
                    model="o3",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                            "required": ["approved"],
                        },
                    },
                ),
            )

        assert result.is_ok
        assert seen_schema["type"] == "object"
        assert seen_schema["required"] == ["approved"]
        assert seen_schema["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_complete_normalizes_optional_object_fields_for_codex_schema(self) -> None:
        """Codex schemas must require every property and disallow extras."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                '{"approved": true, "confidence": 0.92, "reasoning": "Looks good."}',
                encoding="utf-8",
            )

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a vote.")],
                CompletionConfig(
                    model="default",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {
                                "approved": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "reasoning": {"type": "string"},
                            },
                            "required": ["approved"],
                        },
                    },
                ),
            )

        assert result.is_ok
        assert seen_schema["required"] == ["approved", "confidence", "reasoning"]
        assert seen_schema["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_complete_restores_open_map_objects_after_codex_schema_rewrite(self) -> None:
        """Open-map object schemas are rewritten for Codex and restored on output."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        seen_schema: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps(
                    {
                        "score": 0.9,
                        "verdict": "pass",
                        "dimensions": [
                            {"key": "coverage", "value": 0.88},
                            {"key": "ux", "value": 0.91},
                        ],
                        "differences": [],
                        "suggestions": [],
                        "reasoning": "Looks solid.",
                    }
                ),
                encoding="utf-8",
            )

            schema_index = command.index("--output-schema") + 1
            seen_schema.update(json.loads(Path(command[schema_index]).read_text(encoding="utf-8")))
            return _FakeProcess(returncode=0)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a QA verdict.")],
                CompletionConfig(
                    model="default",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "number"},
                                "verdict": {"type": "string"},
                                "dimensions": {
                                    "type": "object",
                                    "additionalProperties": {"type": "number"},
                                },
                                "differences": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "suggestions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "reasoning": {"type": "string"},
                            },
                            "required": [
                                "score",
                                "verdict",
                                "dimensions",
                                "differences",
                                "suggestions",
                                "reasoning",
                            ],
                            "additionalProperties": False,
                        },
                    },
                ),
            )

        assert result.is_ok
        assert json.loads(result.value.content)["dimensions"] == {
            "coverage": 0.88,
            "ux": 0.91,
        }
        dimensions_schema = seen_schema["properties"]["dimensions"]  # type: ignore[index]
        assert dimensions_schema["type"] == "array"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_complete_returns_provider_error_on_nonzero_exit(self) -> None:
        """CLI failures are surfaced as ProviderError results."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(stderr="boom", returncode=2)

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Do the thing.")],
                CompletionConfig(model="o3"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.details["returncode"] == 2
        assert "boom" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_emits_debug_callbacks_from_json_events(self) -> None:
        """Codex adapter translates JSON events into debug callbacks."""
        callback_events: list[tuple[str, str]] = []

        def callback(message_type: str, content: str) -> None:
            callback_events.append((message_type, content))

        adapter = CodexCliLLMAdapter(cli_path="codex", on_message=callback)

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout="\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "reasoning", "text": "Thinking..."},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "pytest -q",
                                },
                            }
                        ),
                    ]
                ),
                returncode=0,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Run the checks.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert callback_events == [("thinking", "Thinking..."), ("tool", "Bash: pytest -q")]

    @pytest.mark.asyncio
    async def test_complete_streams_events_incrementally_and_times_out_once(self) -> None:
        """Timeout should terminate the child while preserving streamed partial events."""
        callback_events: list[tuple[str, str]] = []
        create_calls = 0
        process_holder: dict[str, _FakeProcess] = {}

        def callback(message_type: str, content: str) -> None:
            callback_events.append((message_type, content))

        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            on_message=callback,
            timeout=0.01,
            max_retries=3,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            nonlocal create_calls
            create_calls += 1
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            process = _FakeProcess(
                stdout=json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "reasoning", "text": "Still working..."},
                    }
                )
                + "\n",
                returncode=124,
                wait_forever=True,
                read_size=5,
            )
            process_holder["process"] = process
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Analyze dependencies.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert create_calls == 1
        assert callback_events == [("thinking", "Still working...")]
        assert process_holder["process"].terminated or process_holder["process"].killed

    @pytest.mark.asyncio
    async def test_default_timeout_bounds_wait_when_no_timeout_configured(self) -> None:
        """A wedged `codex` child must not block the generation forever.

        ``timeout`` defaults to ``None`` and no production call site passes one,
        so without the module-level ceiling ``await process.wait()`` is an
        unbounded wait on the default path.
        """
        process_holder: dict[str, _FakeProcess] = {}
        adapter = CodexCliLLMAdapter(cli_path="codex", max_retries=1)
        assert adapter._timeout is None
        assert adapter._effective_timeout_seconds() == DEFAULT_CLI_COMPLETION_TIMEOUT_SECONDS
        adapter._default_completion_timeout_seconds = 0.01

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            process = _FakeProcess(returncode=124, wait_forever=True)
            process_holder["process"] = process
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await asyncio.wait_for(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="Hang")],
                    CompletionConfig(model="default"),
                ),
                timeout=10,
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert result.error.details["timeout_seconds"] == pytest.approx(0.01)
        assert result.error.details["timeout_was_default"] is True
        assert process_holder["process"].terminated or process_holder["process"].killed

    def test_build_command_does_not_include_prompt_as_positional_arg(self) -> None:
        """Prompt is fed via stdin, not as a positional CLI argument."""
        adapter = CodexCliLLMAdapter(cli_path="codex", cwd="/tmp/project")

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        # Last element should be a flag, not user-supplied text
        assert command[-1] in ("--ephemeral", "/tmp/out.txt") or command[-1].startswith("--")

    @pytest.mark.asyncio
    async def test_collect_stream_lines_rejects_unbounded_capture(self) -> None:
        """The shared stream collector should fail once cumulative capture exceeds its cap."""
        stream = _FakeStream("line-1\nline-2\n")

        with pytest.raises(ProviderError, match="stream capture exceeded"):
            await collect_stream_lines(stream, max_total_bytes=8)

    @pytest.mark.asyncio
    async def test_complete_returns_provider_error_when_stderr_capture_overflows(self) -> None:
        """Adapter converts stream-capture guard trips into ProviderError results."""
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(stderr="overflow\n", returncode=0)

        with (
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch(
                "ouroboros.providers.codex_cli_adapter.collect_stream_lines",
                side_effect=ProviderError(
                    message="Codex CLI stream capture exceeded 8 bytes",
                    provider="codex_cli",
                    details={"capture_limit_bytes": 8, "overflow_stage": "stream_capture"},
                ),
            ),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Do the thing.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.details["overflow_stage"] == "stream_capture"
        assert result.error.details["capture_limit_bytes"] == 8

    @pytest.mark.asyncio
    async def test_complete_surfaces_stdout_error_events_on_nonzero_exit(self) -> None:
        """Codex emits in-flight failures as JSONL on stdout — those messages must
        reach ProviderError.message and details, not just the static stderr banner.

        Regression for #560: previously the adapter forwarded only stderr so all
        codex failures looked identical regardless of root cause.
        """
        adapter = CodexCliLLMAdapter(cli_path="codex")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(
                stdout="\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "t1"}),
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Reconnecting... 1/5 (502 Bad Gateway)",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.failed",
                                "error": {"message": "502 Bad Gateway final"},
                            }
                        ),
                    ]
                ),
                stderr="Reading prompt from stdin...",
                returncode=1,
            )

        with (
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            # 502 is retryable, so the adapter walks its full backoff ladder —
            # skip the real waiting, keep the retries.
            patch(
                "ouroboros.providers.codex_cli_adapter.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Reflect on the run.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert "502 Bad Gateway final" in result.error.message
        stdout_errors = result.error.details["stdout_errors"]
        assert len(stdout_errors) == 2
        assert "Reconnecting... 1/5" in stdout_errors[0]
        assert "502 Bad Gateway final" in stdout_errors[1]
        assert result.error.details["stderr"] == "Reading prompt from stdin..."

    @pytest.mark.asyncio
    async def test_legacy_complete_surfaces_stdout_error_events_on_nonzero_exit(
        self,
    ) -> None:
        """The communicate() fallback reports stdout JSONL errors like the streaming path."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        process_holder: dict[str, _LegacyFakeProcess] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _LegacyFakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            process = _LegacyFakeProcess(
                stdout="\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "legacy-thread"}),
                        json.dumps(
                            {
                                "type": "error",
                                "message": "legacy transient failure",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.failed",
                                "error": {"message": "legacy final failure"},
                            }
                        ),
                    ]
                ),
                stderr="Reading prompt from stdin...",
                returncode=1,
            )
            process_holder["process"] = process
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Reflect on the legacy run.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "codex_cli"
        assert result.error.message == "legacy final failure"
        assert result.error.details["session_id"] == "legacy-thread"
        assert result.error.details["stdout_errors"] == [
            "legacy transient failure",
            "legacy final failure",
        ]
        assert result.error.details["stderr"] == "Reading prompt from stdin..."
        assert process_holder["process"].communicate_calls == 1

    @pytest.mark.asyncio
    async def test_complete_classifies_openai_responses_auth_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nested Codex auth-plane failures should carry actionable diagnostics."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")
        (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        adapter = CodexCliLLMAdapter(cli_path="codex")
        auth_error = (
            "HTTP 400: 401 Unauthorized: Missing bearer or basic authentication "
            "in header from https://api.openai.com/v1/responses"
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return _FakeProcess(
                stdout=json.dumps({"type": "turn.failed", "error": {"message": auth_error}}),
                stderr="Reading prompt from stdin...",
                returncode=1,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Ask the next question.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.details["failure_category"] == "codex_auth"
        assert result.error.details["auth_plane"] == "codex_cli"
        assert result.error.details["openai_responses_endpoint_seen"] is True
        context = result.error.details["codex_auth_context"]
        assert context["codex_home"] == str(codex_home)
        assert context["codex_auth_json_exists"] is True
        assert context["codex_config_toml_exists"] is True
        assert context["openai_api_key_present"] is False
        assert "CODEX_HOME/auth.json" in result.error.details["remediation"]

    def test_codex_failure_details_does_not_classify_endpoint_only_errors_as_auth(
        self,
    ) -> None:
        details = CodexCliLLMAdapter._codex_failure_details(
            returncode=1,
            session_id="thread_1",
            stderr="Reading prompt from stdin...",
            stdout_errors=["429 from https://api.openai.com/v1/responses"],
            message="429 from https://api.openai.com/v1/responses",
        )

        assert details["returncode"] == 1
        assert "failure_category" not in details
        assert "remediation" not in details

    def test_codex_failure_details_does_not_classify_non_openai_401_as_auth(
        self,
    ) -> None:
        """Bot review (PR #656): a nested tool / MCP service returning its own
        ``401 Unauthorized`` must NOT be misclassified as Codex auth-plane
        failure. Only auth phrases combined with a Codex/OpenAI marker should
        trigger the ``codex_auth`` category."""
        nested_tool_401 = (
            "tool failed: HTTP 401 Unauthorized from https://internal.example.com/api/v3"
        )

        details = CodexCliLLMAdapter._codex_failure_details(
            returncode=1,
            session_id="thread_2",
            stderr="Reading prompt from stdin...",
            stdout_errors=[nested_tool_401],
            message=nested_tool_401,
        )

        assert details["returncode"] == 1
        assert "failure_category" not in details, (
            "Generic 401 from a non-Codex/non-OpenAI service must not be "
            "labeled codex_auth — operators would be sent to inspect "
            "CODEX_HOME/auth.json for an unrelated failure."
        )
        assert "remediation" not in details

    def test_codex_failure_details_classifies_openai_chat_completions_401_as_auth(
        self,
    ) -> None:
        """Auth phrase + OpenAI domain (any endpoint, not just /v1/responses)
        should still classify as Codex auth — Codex CLI may use other
        endpoints depending on profile, but they all share the same auth
        plane."""
        msg = "401 Unauthorized: Invalid API key from https://api.openai.com/v1/chat/completions"
        details = CodexCliLLMAdapter._codex_failure_details(
            returncode=1,
            session_id="thread_3",
            stderr="Reading prompt from stdin...",
            stdout_errors=[msg],
            message=msg,
        )

        assert details["failure_category"] == "codex_auth"
        # /v1/responses-specific flag must reflect actual presence so
        # downstream renderers can distinguish endpoints.
        assert details["openai_responses_endpoint_seen"] is False
        assert "CODEX_HOME/auth.json" in details["remediation"]

    def test_looks_like_codex_auth_failure_classifier_unit_cases(self) -> None:
        """Spot-check the classifier directly so future drift in either the
        auth-phrase list or the provider-marker list fails loudly here."""
        cls = CodexCliLLMAdapter

        # Positive cases — both signal classes present.
        assert cls._looks_like_codex_auth_failure(
            "401 Unauthorized from api.openai.com/v1/responses"
        )
        assert cls._looks_like_codex_auth_failure("codex login required: invalid bearer token")
        assert cls._looks_like_codex_auth_failure(
            "Missing bearer or basic authentication, see openai.com docs"
        )

        # Negative — auth phrase alone, no Codex/OpenAI marker.
        assert not cls._looks_like_codex_auth_failure(
            "tool failed: 401 Unauthorized from internal.example.com"
        )
        assert not cls._looks_like_codex_auth_failure("invalid api key for stripe")

        # Negative — provider marker alone, no auth phrase.
        assert not cls._looks_like_codex_auth_failure("rate limited: 429 from api.openai.com")
        assert not cls._looks_like_codex_auth_failure(
            "codex CLI exited with code 1: model not found"
        )

    def test_codex_failure_details_classifies_model_unavailable_without_update_claim(self) -> None:
        """A missing model is actionable, but not proof that Codex is outdated."""
        details = CodexCliLLMAdapter._codex_failure_details(
            returncode=1,
            session_id="thread_4",
            stderr="",
            stdout_errors=[],
            message="Codex: model not found",
        )

        assert details["failure_category"] == "codex_model_unavailable"
        assert "update" not in str(details["model_guidance"]).lower()

        error = ProviderError(
            "Codex: model not found",
            provider="codex_cli",
            details=details,
        )
        assert "codex_model_unavailable" in error.format_details()
        assert "requested model is not available" in error.format_details().lower()

    def test_codex_failure_details_reports_mismatched_app_and_cli_versions(self) -> None:
        """A known version mismatch gives a specific, actionable retry path."""
        versions = {
            "/usr/local/bin/codex": "codex-cli 0.139.0",
            "/Applications/ChatGPT.app/Contents/Resources/codex": "codex-cli 0.140.0",
        }

        with patch.object(
            CodexCliLLMAdapter,
            "_codex_version",
            side_effect=lambda path: versions.get(path),
        ):
            details = CodexCliLLMAdapter._codex_failure_details(
                returncode=1,
                session_id="thread_5",
                stderr="",
                stdout_errors=[],
                message="Codex: unsupported model",
                cli_path="/usr/local/bin/codex",
            )

        assert details["codex_app_version"] == "codex-cli 0.140.0"
        assert details["codex_cli_version"] == "codex-cli 0.139.0"
        assert details["codex_app_cli_versions_match"] is False
        assert "Update both Codex installations" in str(details["model_guidance"])

    @pytest.mark.asyncio
    async def test_complete_emits_tool_started_callbacks_from_json_events(self) -> None:
        """Chat renderers can show nested tool/MCP progress before completion."""
        callback_events: list[tuple[str, str]] = []
        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            on_message=lambda message_type, content: callback_events.append(
                (message_type, content)
            ),
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout="\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.started",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "name": "mcp__ouroboros__ouroboros_interview",
                                    "input": {"initial_context": "Build a tool"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "name": "mcp__ouroboros__ouroboros_interview",
                                    "input": {"initial_context": "Build a tool"},
                                },
                            }
                        ),
                    ]
                ),
                returncode=0,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Run interview MCP.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert callback_events == [
            ("tool_started", "mcp__ouroboros__ouroboros_interview: Build a tool"),
            ("tool", "mcp__ouroboros__ouroboros_interview: Build a tool"),
        ]

    def test_emit_callback_splits_started_file_change_and_web_search_events(self) -> None:
        callback_events: list[tuple[str, str]] = []
        adapter = CodexCliLLMAdapter(
            cli_path="codex",
            on_message=lambda message_type, content: callback_events.append(
                (message_type, content)
            ),
        )

        adapter._emit_callback_for_event(
            {
                "type": "item.started",
                "item": {"type": "file_change", "path": "src/demo.py"},
            }
        )
        adapter._emit_callback_for_event(
            {
                "type": "item.completed",
                "item": {"type": "file_change", "path": "src/demo.py"},
            }
        )
        adapter._emit_callback_for_event(
            {
                "type": "item.started",
                "item": {"type": "web_search", "query": "codex oauth"},
            }
        )
        adapter._emit_callback_for_event(
            {
                "type": "item.completed",
                "item": {"type": "web_search", "query": "codex oauth"},
            }
        )

        assert callback_events == [
            ("tool_started", "Edit: src/demo.py"),
            ("tool", "Edit: src/demo.py"),
            ("tool_started", "WebSearch: codex oauth"),
            ("tool", "WebSearch: codex oauth"),
        ]

    def test_extract_stdout_errors_returns_messages_in_arrival_order(self) -> None:
        """Helper extracts only error/turn.failed events and preserves order."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        stdout_lines = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": "first transient"}),
            json.dumps({"type": "item.completed", "item": {"text": "ignored"}}),
            json.dumps({"type": "error", "message": "second transient"}),
            json.dumps({"type": "turn.failed", "error": {"message": "final fatal"}}),
            "not-a-json-line",
        ]
        errors = adapter._extract_stdout_errors(stdout_lines)
        assert errors == ["first transient", "second transient", "final fatal"]

    def test_extract_stdout_errors_handles_empty_and_malformed(self) -> None:
        """Empty input and malformed events do not crash."""
        adapter = CodexCliLLMAdapter(cli_path="codex")
        assert adapter._extract_stdout_errors([]) == []
        assert adapter._extract_stdout_errors([json.dumps({"type": "error"})]) == []
        assert adapter._extract_stdout_errors(
            [json.dumps({"type": "turn.failed", "error": "string fatal"})]
        ) == ["string fatal"]


class TestLazyImport:
    """Test lazy import of CodexCliLLMAdapter from providers package."""

    def test_codex_cli_adapter_accessible_from_providers_package(self) -> None:
        """CodexCliLLMAdapter is available via providers.__getattr__."""
        import ouroboros.providers as providers

        adapter_class = providers.CodexCliLLMAdapter
        assert adapter_class is CodexCliLLMAdapter

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """Accessing a nonexistent attribute raises AttributeError."""
        import ouroboros.providers as providers

        with pytest.raises(AttributeError, match="NonExistent"):
            _ = providers.NonExistent


class TestStreamDrainLifecycle:
    """Regression tests for review blockers #1 and #2 on Codex CLI adapter."""

    @pytest.mark.asyncio
    async def test_prompt_drain_uses_completion_deadline(self) -> None:
        """A child that stops reading stdin is terminated at the completion deadline."""

        class _BlockedStdin(_FakeStdin):
            async def drain(self) -> None:
                await asyncio.Future()

        class _BlockedStdinProcess(_FakeProcess):
            def __init__(self) -> None:
                super().__init__(wait_forever=True)
                self.stdin = _BlockedStdin()

        process = _BlockedStdinProcess()
        adapter = CodexCliLLMAdapter(cli_path="codex", max_retries=1)
        adapter._default_completion_timeout_seconds = 0.05

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _BlockedStdinProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await asyncio.wait_for(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="Block prompt delivery")],
                    CompletionConfig(model="default"),
                ),
                timeout=5,
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert result.error.details["timeout_seconds"] == pytest.approx(0.05)
        assert result.error.details["timeout_was_default"] is True
        assert process.terminated is True

    @pytest.mark.asyncio
    async def test_streaming_exited_parent_non_eof_pipe_bounded(self) -> None:
        """Process exits but a descendant holds stdout open — must not hang.

        Regression for review blocker #1: if the direct child exits but a
        stream never reaches EOF, the adapter must still return within the
        completion deadline rather than hanging indefinitely on the pipe drain.
        """

        class _NeverEOFStream:
            """A stream whose read() hangs forever, simulating a held-open pipe."""

            async def read(self, chunk_size: int = 16384) -> bytes:
                await asyncio.Future()  # never returns
                return b""  # unreachable

        process_holder: dict[str, Any] = {}

        class _ExitedButPipesOpenProcess:
            def __init__(self) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _NeverEOFStream()
                self.stderr = _FakeStream("")
                self.returncode = 0
                self.terminated = False
                self.killed = False

            async def wait(self) -> int:
                return 0  # process exited immediately

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

        adapter = CodexCliLLMAdapter(cli_path="codex", max_retries=1)
        adapter._default_completion_timeout_seconds = 0.05

        async def fake_create_subprocess_exec(
            *command: str, **kwargs: Any
        ) -> _ExitedButPipesOpenProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("", encoding="utf-8")
            proc = _ExitedButPipesOpenProcess()
            process_holder["process"] = proc
            return proc

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await asyncio.wait_for(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="Hang on pipe")],
                    CompletionConfig(model="default"),
                ),
                timeout=5,
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert result.error.details["timeout_was_default"] is True

    @pytest.mark.asyncio
    async def test_legacy_timeout_returns_result_err_provider_error(self) -> None:
        """Legacy communicate() timeout must be translated to Result.err(ProviderError).

        Regression for review blocker #2: the legacy path must not propagate a
        bare TimeoutError. It must terminate the child, clean temp artifacts,
        and return a structured error with timed_out, timeout_seconds, and
        timeout_was_default details.
        """

        class _HangingLegacyProcess:
            """Legacy process whose communicate() hangs forever."""

            def __init__(self) -> None:
                self.stdin = _FakeStdin()
                self.returncode = None
                self.terminated = False
                self.killed = False

            async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.Future()  # never returns
                return b"", b""  # unreachable

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

        adapter = CodexCliLLMAdapter(cli_path="codex", max_retries=1)
        adapter._default_completion_timeout_seconds = 0.05

        async def fake_create_subprocess_exec(
            *command: str, **kwargs: Any
        ) -> _HangingLegacyProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("partial output", encoding="utf-8")
            return _HangingLegacyProcess()

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await asyncio.wait_for(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="Hang legacy")],
                    CompletionConfig(model="default"),
                ),
                timeout=5,
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert result.error.details["timeout_seconds"] == pytest.approx(0.05)
        assert result.error.details["timeout_was_default"] is True
        assert "timed out" in result.error.message
