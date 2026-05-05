"""Unit tests for Kiro CLI adapters.

Tests cover:
- providers/factory.py: resolve + create for kiro backend
- providers/kiro_adapter.py: KiroCodeAdapter LLM completion
- orchestrator/runtime_factory.py: resolve + create for kiro runtime
- orchestrator/kiro_adapter.py: KiroAgentAdapter task execution
- config/loader.py: OUROBOROS_RUNTIME fallback routing
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)
from ouroboros.providers.kiro_adapter import KiroCodeAdapter

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_proc(
    stdout: bytes = b"ok\n",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    proc.stdout = _async_line_iter(stdout)
    proc.stderr = _async_line_iter(stderr)
    return proc


class _async_line_iter:
    def __init__(self, data: bytes):
        self._lines = [line + b"\n" for line in data.split(b"\n") if line]
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line

    async def readline(self) -> bytes:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            return b""


# ===========================================================================
# providers/factory.py — resolve
# ===========================================================================


class TestResolveLLMBackendKiro:
    def test_resolves_kiro_aliases(self) -> None:
        assert resolve_llm_backend("kiro") == "kiro"
        assert resolve_llm_backend("kiro_cli") == "kiro"

    def test_falls_back_to_kiro_via_ouroboros_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert resolve_llm_backend() == "kiro"

    def test_ouroboros_runtime_does_not_affect_explicit_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert resolve_llm_backend("claude") == "claude_code"


class TestResolveLLMPermissionModeKiro:
    def test_kiro_returns_default(self) -> None:
        assert resolve_llm_permission_mode(backend="kiro") == "default"

    def test_kiro_interview_returns_default(self) -> None:
        assert resolve_llm_permission_mode(backend="kiro", use_case="interview") == "default"


# ===========================================================================
# providers/factory.py — create
# ===========================================================================


class TestCreateLLMAdapterKiro:
    def test_creates_kiro_adapter(self) -> None:
        adapter = create_llm_adapter(backend="kiro")
        assert isinstance(adapter, KiroCodeAdapter)

    def test_passes_cwd_to_kiro_adapter(self) -> None:
        adapter = create_llm_adapter(backend="kiro", cwd="/tmp/project")
        assert isinstance(adapter, KiroCodeAdapter)
        assert adapter._cwd == "/tmp/project"

    def test_passes_timeout_to_kiro_adapter(self) -> None:
        adapter = create_llm_adapter(backend="kiro", timeout=42.0)
        assert isinstance(adapter, KiroCodeAdapter)
        assert adapter._timeout == 42.0

    def test_passes_max_retries_to_kiro_adapter(self) -> None:
        adapter = create_llm_adapter(backend="kiro", max_retries=5)
        assert isinstance(adapter, KiroCodeAdapter)
        assert adapter._max_retries == 5

    def test_passes_tool_envelope_to_kiro_adapter(self) -> None:
        messages: list[tuple[str, str]] = []

        def _on_message(kind: str, content: str) -> None:
            messages.append((kind, content))

        adapter = create_llm_adapter(
            backend="kiro",
            allowed_tools=[],
            max_turns=2,
            on_message=_on_message,
        )
        assert isinstance(adapter, KiroCodeAdapter)
        assert adapter._allowed_tools == []
        assert adapter._permission_mode == "default"
        assert adapter._max_turns == 2
        assert adapter._on_message is _on_message

    def test_uses_configured_cli_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ouroboros.config.get_kiro_cli_path", lambda: "/custom/kiro-cli")
        adapter = create_llm_adapter(backend="kiro")
        assert isinstance(adapter, KiroCodeAdapter)
        assert adapter._cli_path == "/custom/kiro-cli"


# ===========================================================================
# providers/kiro_adapter.py — KiroCodeAdapter
# ===========================================================================


class TestKiroCodeAdapterComplete:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        proc = _make_proc(stdout=b"Hello world", returncode=0)
        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            adapter = KiroCodeAdapter(cli_path="kiro-cli")
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                config=CompletionConfig(model="default"),
            )
        assert result.is_ok
        assert result.value.content == "Hello world"

    @pytest.mark.asyncio
    async def test_retries_on_exit_code_1(self) -> None:
        call_count = 0

        async def _factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_proc(stderr=b"err", returncode=1)
            return _make_proc(stdout=b"ok", returncode=0)

        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            side_effect=_factory,
        ):
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            adapter = KiroCodeAdapter(cli_path="kiro-cli")
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="retry")],
                config=CompletionConfig(model="default"),
            )
        assert result.is_ok
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_file_not_found(self) -> None:
        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            adapter = KiroCodeAdapter(cli_path="/bad/path")
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="hi")],
                config=CompletionConfig(model="default"),
            )
        assert result.is_err
        assert "not found" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_respects_cwd(self) -> None:
        proc = _make_proc(stdout=b"ok", returncode=0)
        captured_kwargs: dict = {}

        async def _capture(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            side_effect=_capture,
        ):
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            adapter = KiroCodeAdapter(cli_path="kiro-cli", cwd="/my/project")
            await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="test")],
                config=CompletionConfig(model="default"),
            )
        assert captured_kwargs["cwd"] == "/my/project"

    def test_build_prompt_with_system(self) -> None:
        from ouroboros.providers.base import Message, MessageRole

        adapter = KiroCodeAdapter(cli_path="kiro-cli")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Be concise"),
                Message(role=MessageRole.USER, content="Hello"),
            ]
        )
        assert "<system>" in prompt
        assert "Be concise" in prompt
        assert "User: Hello" in prompt

    def test_empty_allowed_tools_forces_text_only_prompt(self) -> None:
        from ouroboros.providers.base import Message, MessageRole

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=[])
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Question")])

        assert "Tool constraints" in prompt
        assert "Do NOT use any tools" in prompt
        assert "Respond with text only" in prompt

    def test_allowed_tools_lists_permitted_tools(self) -> None:
        from ouroboros.providers.base import Message, MessageRole

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=["Read", "Grep"])
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Question")])

        assert "Limit tool usage to ONLY" in prompt
        assert "- Read" in prompt
        assert "- Grep" in prompt

    def test_allowed_tools_none_omits_tool_constraints(self) -> None:
        from ouroboros.providers.base import Message, MessageRole

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=None)
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Question")])

        assert "Tool constraints" not in prompt

    def test_empty_allowed_tools_sets_empty_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=[])
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_allowed_tools_maps_to_kiro_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=["Read", "Grep", "Bash"])
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-tools=read,grep,shell" in cmd
        assert "--trust-all-tools" not in cmd

    def test_allowed_tools_filters_mcp_names_from_kiro_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(
            cli_path="kiro-cli",
            allowed_tools=["Read", "mcp__ouroboros__interview", "Bash"],
        )
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-tools=read,shell" in cmd
        assert "mcp" not in next(arg for arg in cmd if arg.startswith("--trust-tools="))
        assert "--trust-all-tools" not in cmd

    def test_allowed_tools_only_mcp_names_sets_empty_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(
            cli_path="kiro-cli",
            allowed_tools=["mcp__ouroboros__interview"],
        )
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_allowed_tools_none_omits_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=None)
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert not any(arg.startswith("--trust-tools") for arg in cmd)
        assert "--trust-all-tools" not in cmd

    def test_default_permission_mode_sets_empty_trust_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(cli_path="kiro-cli", permission_mode="default")
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_accept_edits_permission_mode_sets_trust_all_tools_flag(self) -> None:
        from ouroboros.providers.base import CompletionConfig

        adapter = KiroCodeAdapter(cli_path="kiro-cli", permission_mode="acceptEdits")
        cmd = adapter._build_cmd("Question", CompletionConfig(model="default"))

        assert "--trust-all-tools" in cmd

    def test_build_child_env_strips_ouroboros_vars(self) -> None:
        adapter = KiroCodeAdapter(cli_path="kiro-cli")
        with patch.dict(
            "os.environ",
            {
                "OUROBOROS_AGENT_RUNTIME": "kiro",
                "OUROBOROS_LLM_BACKEND": "kiro",
                "OUROBOROS_RUNTIME": "kiro",
                "CLAUDECODE": "1",
            },
        ):
            env = adapter._build_child_env()

        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "OUROBOROS_RUNTIME" not in env
        assert "CLAUDECODE" not in env
        assert env["_OUROBOROS_DEPTH"] == "1"
        assert env["OUROBOROS_SUBAGENT"] == "1"

    def test_build_child_env_depth_guard(self) -> None:
        adapter = KiroCodeAdapter(cli_path="kiro-cli")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
                adapter._build_child_env()

    @pytest.mark.asyncio
    async def test_depth_guard_returns_provider_error(self) -> None:
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        adapter = KiroCodeAdapter(cli_path="kiro-cli")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "Maximum Ouroboros nesting depth" in result.error.message
        assert result.error.details == {"error_type": "RuntimeError"}

    def test_audit_flags_tool_use_outside_envelope(self) -> None:
        captured: list[dict] = []

        def _warning(event: str, **kwargs) -> None:
            captured.append({"event": event, **kwargs})

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=["Read"])
        with patch("ouroboros.providers.kiro_adapter.log.warning", side_effect=_warning):
            adapter._audit_tool_envelope_violations(
                '{"type":"tool_use","name":"Read"}\n{"type":"tool_use","name":"Edit"}'
            )

        violations = [
            item for item in captured if item["event"] == "kiro_adapter.tool_envelope_violation"
        ]
        assert len(violations) == 1
        assert violations[0]["tool"] == "Edit"
        assert violations[0]["allowed_tools"] == ["Read"]

    def test_audit_normalizes_kiro_native_tool_categories(self) -> None:
        captured: list[dict] = []

        def _warning(event: str, **kwargs) -> None:
            captured.append({"event": event, **kwargs})

        adapter = KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=["Bash"])
        with patch("ouroboros.providers.kiro_adapter.log.warning", side_effect=_warning):
            adapter._audit_tool_envelope_violations('{"type":"tool_use","name":"shell"}')

        assert [
            item for item in captured if item["event"] == "kiro_adapter.tool_envelope_violation"
        ] == []

    def test_init_logs_native_tool_enforcement_not_soft_warning(self) -> None:
        info_events: list[str] = []
        warning_events: list[str] = []

        with (
            patch(
                "ouroboros.providers.kiro_adapter.log.info",
                side_effect=lambda event, **_kwargs: info_events.append(event),
            ),
            patch(
                "ouroboros.providers.kiro_adapter.log.warning",
                side_effect=lambda event, **_kwargs: warning_events.append(event),
            ),
        ):
            KiroCodeAdapter(cli_path="kiro-cli", allowed_tools=[])

        assert "kiro_adapter.native_tool_enforcement" in info_events
        assert "kiro_adapter.soft_tool_enforcement" not in warning_events

    @pytest.mark.asyncio
    async def test_on_message_receives_assistant_response(self) -> None:
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        proc = _make_proc(stdout=b"Hello world", returncode=0)
        messages: list[tuple[str, str]] = []

        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            adapter = KiroCodeAdapter(
                cli_path="kiro-cli",
                on_message=lambda kind, content: messages.append((kind, content)),
            )
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert messages == [("assistant", "Hello world")]

    @pytest.mark.asyncio
    async def test_strips_ansi_prompt_marker_from_response(self) -> None:
        """Kiro prints a colored ``> `` prompt before output. Downstream
        parsers (e.g. Seed extraction) match on prefixes like ``GOAL:`` and
        silently fail if ANSI escapes or the marker leak through. The
        adapter must yield plain text."""
        polluted = b"\x1b[38;5;141m> \x1b[0mGOAL: build a CLI\nOther line"
        proc = _make_proc(stdout=polluted, returncode=0)
        with patch(
            "ouroboros.providers.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            adapter = KiroCodeAdapter(cli_path="kiro-cli")
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="q")],
                config=CompletionConfig(model="default"),
            )
        assert result.is_ok
        content = result.value.content
        assert "\x1b" not in content
        assert not content.startswith("> ")
        assert content.startswith("GOAL: build a CLI")


# ===========================================================================
# orchestrator/runtime_factory.py
# ===========================================================================


class TestResolveAgentRuntimeBackendKiro:
    def test_resolves_kiro_aliases(self) -> None:
        from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

        assert resolve_agent_runtime_backend("kiro") == "kiro"
        assert resolve_agent_runtime_backend("kiro_cli") == "kiro"

    def test_falls_back_via_ouroboros_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

        monkeypatch.delenv("OUROBOROS_AGENT_RUNTIME", raising=False)
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert resolve_agent_runtime_backend() == "kiro"


class TestCreateAgentRuntimeKiro:
    def test_creates_kiro_runtime(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        runtime = create_agent_runtime(backend="kiro", cwd="/tmp/project")
        assert isinstance(runtime, KiroAgentAdapter)
        assert runtime.runtime_backend == "kiro"
        assert runtime.working_directory == "/tmp/project"

    def test_uses_configured_cli_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.get_kiro_cli_path",
            lambda: "/custom/kiro",
        )
        runtime = create_agent_runtime(backend="kiro")
        assert isinstance(runtime, KiroAgentAdapter)
        assert runtime._cli_path == "/custom/kiro"


# ===========================================================================
# orchestrator/kiro_adapter.py — KiroAgentAdapter
# ===========================================================================


class TestKiroAgentAdapterExecuteTask:
    @pytest.mark.asyncio
    async def test_streams_output_lines(self) -> None:
        proc = _make_proc(stdout=b"line1\nline2\n", returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli")
            messages = [msg async for msg in adapter.execute_task("do something")]

        assert messages[0].type == "system"
        assistant_msgs = [m for m in messages if m.type == "assistant"]
        assert len(assistant_msgs) == 2
        result = messages[-1]
        assert result.type == "result"
        assert result.data["subtype"] == "success"

    @pytest.mark.asyncio
    async def test_nonzero_exit_yields_error(self) -> None:
        proc = _make_proc(stdout=b"", stderr=b"something broke", returncode=2)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli")
            messages = [msg async for msg in adapter.execute_task("fail")]

        result = messages[-1]
        assert result.is_error
        assert "exit 2" in result.content

    @pytest.mark.asyncio
    async def test_respects_cwd(self) -> None:
        proc = _make_proc(stdout=b"ok\n", returncode=0)
        captured_kwargs: dict = {}

        async def _capture(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            side_effect=_capture,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli", cwd="/my/repo")
            _ = [msg async for msg in adapter.execute_task("test")]

        assert captured_kwargs["cwd"] == "/my/repo"

    @pytest.mark.asyncio
    async def test_strips_ansi_escapes_from_streamed_lines(self) -> None:
        """Each streamed assistant line must be plain text — no terminal
        prompt markers, no color escapes. Parsers downstream of
        ``AgentMessage.content`` match on literal prefixes."""
        polluted = b"\x1b[38;5;141m> \x1b[0mGOAL: build a CLI\n\x1b[0mline two\n"
        proc = _make_proc(stdout=polluted, returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli")
            messages = [m async for m in adapter.execute_task("go")]

        assistant_contents = [m.content for m in messages if m.type == "assistant"]
        for content in assistant_contents:
            assert "\x1b" not in content
            assert not content.startswith("> ")
        assert assistant_contents == ["GOAL: build a CLI", "line two"]


class TestKiroAgentAdapterExecuteTaskToResult:
    def test_timeout_messages_are_retryable(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        assert KiroAgentAdapter._is_retryable("Kiro CLI timed out (startup timeout after 60s)")
        assert KiroAgentAdapter._is_retryable("Kiro CLI became unresponsive (idle timeout)")

    @pytest.mark.asyncio
    async def test_success_returns_ok(self) -> None:
        proc = _make_proc(stdout=b"done\n", returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli")
            result = await adapter.execute_task_to_result("do it")

        assert result.is_ok
        assert result.value.success

    @pytest.mark.asyncio
    async def test_non_retryable_error_fails_immediately(self) -> None:
        proc = _make_proc(stderr=b"permission denied", returncode=126)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

            adapter = KiroAgentAdapter(cli_path="kiro-cli")
            result = await adapter.execute_task_to_result("nope")

        assert result.is_err


# ===========================================================================
# config/loader.py — OUROBOROS_RUNTIME routing
# ===========================================================================


class TestOuroborosRuntimeFallback:
    def test_get_llm_backend_uses_runtime_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.config.loader import get_llm_backend

        monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert get_llm_backend() == "kiro"

    def test_get_llm_backend_uses_kiro_cli_runtime_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ouroboros.config.loader import get_llm_backend

        monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro_cli")
        assert get_llm_backend() == "kiro"

    def test_llm_backend_env_takes_priority_over_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ouroboros.config.loader import get_llm_backend

        monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "litellm")
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert get_llm_backend() == "litellm"

    def test_no_runtime_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.config.loader import get_llm_backend

        monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
        monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)
        # Falls through to config or default "claude_code"
        result = get_llm_backend()
        assert isinstance(result, str)

    def test_get_agent_runtime_uses_runtime_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.config.loader import get_agent_runtime_backend

        monkeypatch.delenv("OUROBOROS_AGENT_RUNTIME", raising=False)
        monkeypatch.setenv("OUROBOROS_RUNTIME", "kiro")
        assert get_agent_runtime_backend() == "kiro"

    def test_kiro_model_defaults_to_sentinel(self) -> None:
        from ouroboros.config.loader import _default_model_for_backend

        assert _default_model_for_backend("claude-sonnet-4-20250514", backend="kiro") == "default"


# ===========================================================================
# Runtime contract compliance
# ===========================================================================


class TestKiroPermissionModeContract:
    def test_public_llm_backend_property_exposes_configured_backend(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", llm_backend="litellm")

        assert adapter.llm_backend == "litellm"

    def test_public_cli_path_property_exposes_resolved_cli_override(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="/custom/bin/kiro-cli")

        assert adapter.cli_path == "/custom/bin/kiro-cli"

    def test_default_mode_uses_trust_tools_empty(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="default")
        cmd = adapter._build_cmd("hello")
        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_accept_edits_uses_trust_all_tools(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="acceptEdits")
        cmd = adapter._build_cmd("hello")
        assert "--trust-all-tools" in cmd
        assert "--trust-tools=" not in cmd

    def test_bypass_uses_trust_all_tools(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="bypassPermissions")
        cmd = adapter._build_cmd("hello")
        assert "--trust-all-tools" in cmd

    def test_empty_per_call_tools_override_accept_edits_with_no_trust(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="acceptEdits")
        cmd = adapter._build_cmd("hello", tools=[])
        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_per_call_tools_map_to_kiro_trust_categories(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="bypassPermissions")
        cmd = adapter._build_cmd("hello", tools=["Read", "Grep", "Bash"])
        assert "--trust-tools=read,grep,shell" in cmd
        assert "--trust-all-tools" not in cmd

    def test_per_call_tools_filter_mcp_names_from_kiro_trust_categories(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="bypassPermissions")
        cmd = adapter._build_cmd(
            "hello",
            tools=["Read", "mcp__ouroboros__interview", "Bash"],
        )
        trust_arg = next(arg for arg in cmd if arg.startswith("--trust-tools="))
        assert trust_arg == "--trust-tools=read,shell"
        assert "mcp" not in trust_arg
        assert "--trust-all-tools" not in cmd

    def test_per_call_tools_only_mcp_names_set_empty_kiro_trust_categories(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli", permission_mode="bypassPermissions")
        cmd = adapter._build_cmd("hello", tools=["mcp__ouroboros__interview"])
        assert "--trust-tools=" in cmd
        assert "--trust-all-tools" not in cmd

    def test_runtime_child_env_strips_ouroboros_runtime_vars(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        with patch.dict(
            "os.environ",
            {
                "OUROBOROS_AGENT_RUNTIME": "kiro",
                "OUROBOROS_LLM_BACKEND": "kiro",
                "OUROBOROS_RUNTIME": "kiro",
                "CLAUDECODE": "1",
            },
        ):
            env = adapter._build_child_env()

        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "OUROBOROS_RUNTIME" not in env
        assert "CLAUDECODE" not in env
        assert env["_OUROBOROS_DEPTH"] == "1"

    @pytest.mark.asyncio
    async def test_runtime_depth_guard_yields_error_message(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            messages = [message async for message in adapter.execute_task("hello")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert "Maximum Ouroboros nesting depth" in messages[0].content
        assert messages[0].data["error_type"] == "RuntimeError"


class TestKiroFactoryDispatcherContract:
    def test_factory_passes_skill_dispatcher(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        runtime = create_agent_runtime(backend="kiro", cwd="/tmp/test")
        assert isinstance(runtime, KiroAgentAdapter)
        assert runtime._skill_dispatcher is not None


class TestKiroResumeContract:
    """Targeted resume via ``--resume-id``.

    Kiro CLI 2.2+ supports three resume flags (verified with
    ``kiro-cli chat --help`` and https://kiro.dev/docs/cli/headless/):
      -r/--resume        → "most recent in this directory" (wrong for targeted)
      --resume-id <id>   → targeted resume by session id (what we want)
      --resume-picker    → interactive, unusable in headless

    The first iteration of the Kiro adapter attached bare ``--resume`` when a
    session id was provided, which silently resumed "whatever was most recent"
    instead of the requested session. The maintainer review flagged this as
    silent degradation. These tests pin the fix: session id → ``--resume-id``.
    """

    def test_resume_session_id_uses_resume_id_flag(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        cmd = adapter._build_cmd("hello", resume_session_id="sess-123")
        # Targeted resume must use --resume-id <id> (two argv slots, adjacent).
        assert "--resume-id" in cmd
        idx = cmd.index("--resume-id")
        assert cmd[idx + 1] == "sess-123"
        # The bare -r/--resume flag (which resumes "most recent") must NOT be
        # present — otherwise we'd be silently overriding the targeted resume.
        assert "--resume" not in cmd
        assert "-r" not in cmd

    def test_no_resume_session_id_omits_both_flags(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        cmd = adapter._build_cmd("hello")
        # When no session id is given, neither resume flag should be added —
        # not even bare --resume (that would be a silent "resume most recent").
        assert "--resume" not in cmd
        assert "--resume-id" not in cmd
        assert "-r" not in cmd

    def test_unsafe_resume_session_id_rejected(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        with pytest.raises(ValueError, match="Invalid resume_session_id"):
            adapter._build_cmd("hello", resume_session_id="../etc/passwd")
        with pytest.raises(ValueError, match="Invalid resume_session_id"):
            adapter._build_cmd("hello", resume_session_id="sess 123")  # space
        with pytest.raises(ValueError, match="Invalid resume_session_id"):
            adapter._build_cmd("hello", resume_session_id="$(rm -rf)")

    def test_safe_resume_session_id_accepted(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        # Alphanumerics, hyphen, underscore all pass.
        cmd = adapter._build_cmd("hello", resume_session_id="abc_XYZ-123")
        assert "--resume-id" in cmd
        assert "abc_XYZ-123" in cmd


class TestKiroCapabilities:
    """Explicit capability metadata — no more silent backend differences."""

    def test_kiro_declares_capabilities(self) -> None:
        from ouroboros.orchestrator.adapter import RuntimeCapabilities
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        caps = KiroAgentAdapter(cli_path="kiro-cli").capabilities
        assert isinstance(caps, RuntimeCapabilities)
        assert caps.skill_dispatch is True
        # Kiro headless mode does not surface a session id on stdout/stderr
        # during a run — it can only be retrieved after the fact via
        # ``kiro-cli chat --list-sessions``. The adapter therefore cannot
        # capture a resumable handle during normal execution, so targeted
        # resume is declared False honestly.
        assert caps.targeted_resume is False
        # Kiro headless stdout is plain text, not JSONL.
        assert caps.structured_output is False

    def test_claude_declares_full_capabilities(self) -> None:
        from ouroboros.orchestrator.adapter import (
            FULL_CAPABILITIES,
            ClaudeAgentAdapter,
        )

        caps = ClaudeAgentAdapter().capabilities
        assert caps == FULL_CAPABILITIES
        assert caps.skill_dispatch is True
        assert caps.targeted_resume is True
        assert caps.structured_output is True


class TestKiroSkillInterceptWiring:
    """Verify the SkillInterceptor is composed in and consulted before subprocess."""

    def test_interceptor_is_constructed(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
        from ouroboros.orchestrator.skill_intercept import SkillInterceptor

        adapter = KiroAgentAdapter(cli_path="kiro-cli", cwd="/tmp/kiro-test")
        assert isinstance(adapter._interceptor, SkillInterceptor)
        assert adapter._interceptor._runtime_backend == "kiro"
        assert adapter._interceptor._cwd == "/tmp/kiro-test"

    @pytest.mark.asyncio
    async def test_intercept_short_circuits_subprocess(self) -> None:
        """When interceptor returns messages, kiro-cli must NOT be spawned."""
        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")

        async def _fake_dispatch(prompt, handle):  # type: ignore[no-untyped-def]
            return (
                AgentMessage(
                    type="assistant",
                    content="Calling tool: ouroboros_interview",
                    tool_name="ouroboros_interview",
                ),
                AgentMessage(
                    type="result",
                    content="Interview started",
                    data={"subtype": "success"},
                ),
            )

        adapter._interceptor.maybe_dispatch = _fake_dispatch  # type: ignore[method-assign]

        spawn_called = False

        async def _should_not_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal spawn_called
            spawn_called = True
            raise AssertionError("subprocess was spawned despite intercept hit")

        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            side_effect=_should_not_spawn,
        ):
            messages = [msg async for msg in adapter.execute_task("ooo interview")]

        assert spawn_called is False
        assert [m.type for m in messages] == ["assistant", "result"]
        assert messages[-1].data.get("subtype") == "success"

    @pytest.mark.asyncio
    async def test_non_skill_prompt_falls_through_to_subprocess(self) -> None:
        """Plain prompts with no skill prefix must go through to kiro-cli."""
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")

        async def _no_match(prompt, handle):  # type: ignore[no-untyped-def]
            return None

        adapter._interceptor.maybe_dispatch = _no_match  # type: ignore[method-assign]

        proc = _make_proc(stdout=b"result line\n", returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn:
            messages = [msg async for msg in adapter.execute_task("plain prompt, no prefix")]

        spawn.assert_called_once()
        # system init + at least one assistant + one result
        assert messages[0].type == "system"
        assert messages[-1].type == "result"


# ===========================================================================
# Kiro skill-dispatch parity with Codex/Claude
# ===========================================================================


def _write_skill(skills_dir, skill_name, frontmatter_lines):
    """Create a packaged SKILL.md that the router's resolver can read."""
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    frontmatter = "\n".join(frontmatter_lines)
    skill_md.write_text(
        f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
        encoding="utf-8",
    )
    return skill_md


class TestKiroSkillDispatchParity:
    """Kiro must honor ``ooo <skill>`` / ``/ouroboros:<skill>`` prefixes
    exactly like Codex does — otherwise selecting ``OUROBOROS_RUNTIME=kiro``
    silently loses a runtime behavior that Claude and Codex both preserve.
    This is the parity gap the maintainer review flagged.
    """

    @pytest.mark.asyncio
    async def test_ooo_prefix_dispatches_to_mcp_tool_not_kiro(self, tmp_path) -> None:
        """`ooo interview "..."` routes to the MCP tool; kiro-cli is not spawned."""
        _write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Starting interview"),
                AgentMessage(
                    type="result",
                    content="Interview started",
                    data={"subtype": "success"},
                ),
            )
        )
        adapter = KiroAgentAdapter(
            cli_path="kiro-cli",
            cwd="/tmp/kiro-project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
        ) as spawn:
            messages = [
                msg async for msg in adapter.execute_task('ooo interview "Build a REST API"')
            ]

        spawn.assert_not_called()
        dispatcher.assert_awaited_once()
        intercept = dispatcher.await_args.args[0]
        assert intercept.mcp_tool == "ouroboros_interview"
        assert intercept.first_argument == "Build a REST API"
        assert intercept.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/kiro-project",
        }
        assert [m.content for m in messages] == ["Starting interview", "Interview started"]

    @pytest.mark.asyncio
    async def test_slash_prefix_also_dispatches(self, tmp_path) -> None:
        """``/ouroboros:seed`` prefix dispatches identically to ``ooo seed``."""
        _write_skill(
            tmp_path,
            "seed",
            [
                "name: seed",
                'description: "Generate validated Seed"',
                "mcp_tool: ouroboros_generate_seed",
                "mcp_args:",
                '  cwd: "$CWD"',
            ],
        )
        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(
                    type="result",
                    content="Seed generated",
                    data={"subtype": "success"},
                ),
            )
        )
        adapter = KiroAgentAdapter(
            cli_path="kiro-cli",
            cwd="/tmp/kiro-project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
        ) as spawn:
            messages = [msg async for msg in adapter.execute_task("/ouroboros:seed")]

        spawn.assert_not_called()
        dispatcher.assert_awaited_once()
        assert dispatcher.await_args.args[0].mcp_tool == "ouroboros_generate_seed"
        assert messages[-1].content == "Seed generated"

    @pytest.mark.asyncio
    async def test_recoverable_mcp_error_falls_through_to_kiro(self, tmp_path) -> None:
        """When the MCP dispatcher reports a recoverable error, the adapter
        must fall through to ``kiro-cli`` instead of swallowing the prompt."""
        _write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  cwd: "$CWD"',
            ],
        )
        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        # Dispatcher reports a recoverable error — the interceptor should
        # detect this and return None so the adapter falls through to Kiro.
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(
                    type="result",
                    content="MCP server disconnected",
                    data={"subtype": "error", "error_type": "MCPConnectionError"},
                ),
            )
        )
        adapter = KiroAgentAdapter(
            cli_path="kiro-cli",
            cwd="/tmp/kiro-project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        proc = _make_proc(stdout=b"fallback result\n", returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn:
            messages = [msg async for msg in adapter.execute_task('ooo interview "Fallback path"')]

        # Dispatcher was tried...
        dispatcher.assert_awaited_once()
        # ...but the adapter fell through to the subprocess.
        spawn.assert_called_once()
        assert messages[0].type == "system"
        assert messages[-1].type == "result"

    @pytest.mark.asyncio
    async def test_unknown_skill_name_falls_through(self, tmp_path) -> None:
        """A prompt starting with ``ooo`` but naming a skill that doesn't
        exist in the skills directory must fall through to Kiro rather than
        be swallowed silently."""
        # No skills written → router returns NotHandled / InvalidSkill.
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(
            cli_path="kiro-cli",
            cwd="/tmp/kiro-project",
            skills_dir=tmp_path,
        )

        proc = _make_proc(stdout=b"direct-to-kiro\n", returncode=0)
        with patch(
            "ouroboros.orchestrator.kiro_adapter.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn:
            messages = [msg async for msg in adapter.execute_task("ooo nonexistent-skill xyz")]

        spawn.assert_called_once()
        assert messages[-1].type == "result"
