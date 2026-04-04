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


class TestKiroAgentAdapterExecuteTaskToResult:
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


class TestKiroFactoryDispatcherContract:
    def test_factory_passes_skill_dispatcher(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        runtime = create_agent_runtime(backend="kiro", cwd="/tmp/test")
        assert isinstance(runtime, KiroAgentAdapter)
        assert runtime._skill_dispatcher is not None


class TestKiroResumeContract:
    def test_resume_session_id_adds_resume_flag(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        cmd = adapter._build_cmd("hello", resume_session_id="sess-123")
        assert "--resume" in cmd

    def test_no_resume_session_id_omits_flag(self) -> None:
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        adapter = KiroAgentAdapter(cli_path="kiro-cli")
        cmd = adapter._build_cmd("hello")
        assert "--resume" not in cmd
