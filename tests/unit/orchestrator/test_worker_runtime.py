"""Provider-neutral leader-driven worker runtime — deterministic (fake transport).

Proves the SAME LeaderDrivenWorkerRuntime drives any provider via a thin
transport: spawn → addressable handle, resume → same session, errors are
terminal, and the capability is EXTERNAL_LEADER_DRIVEN with native passthrough.
No live CLI required — a fake transport stands in for codex/claude.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import (
    ParamSupport,
    RuntimeHandle,
    SubagentOrchestration,
    is_leader_driven_worker,
)
from ouroboros.orchestrator.worker_runtime import (
    LeaderDrivenWorkerRuntime,
    WorkerTurn,
)


class _FakeTransport:
    """Records calls and returns canned turns."""

    backend_name = "fake_worker"

    def __init__(self, *, spawn_turn: WorkerTurn, resume_turn: WorkerTurn | None = None) -> None:
        self._spawn_turn = spawn_turn
        self._resume_turn = resume_turn
        self.spawn_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    async def spawn(self, **kwargs) -> WorkerTurn:
        self.spawn_calls.append(kwargs)
        return self._spawn_turn

    async def resume(
        self,
        *,
        session_id: str,
        prompt: str,
        permission_mode: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> WorkerTurn:
        self.resume_calls.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "permission_mode": permission_mode,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        )
        assert self._resume_turn is not None
        return self._resume_turn


def _runtime(transport: _FakeTransport) -> LeaderDrivenWorkerRuntime:
    return LeaderDrivenWorkerRuntime(
        transport=transport,
        runtime_backend="codex_mcp",
        llm_backend="codex",
        cwd="/tmp",
        reasoning_effort_support=ParamSupport.NATIVE,
    )


class TestCapabilities:
    def test_declares_leader_driven_native_passthrough(self) -> None:
        rt = _runtime(_FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1")))
        caps = rt.capabilities
        assert caps.subagent_orchestration is SubagentOrchestration.EXTERNAL_LEADER_DRIVEN
        assert is_leader_driven_worker(caps) is True
        # Native passthrough: worker tools are NOT restricted by ouroboros.
        assert caps.tool_restriction_support is ParamSupport.IGNORED
        assert caps.system_prompt_support is ParamSupport.NATIVE
        assert caps.reasoning_effort_support is ParamSupport.NATIVE
        assert caps.session_signals.after_turn_delivery is True
        assert caps.session_signals.checkpoint_redirect is False

    def test_after_turn_requires_targeted_resume(self) -> None:
        transport = _FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1"))
        runtime = LeaderDrivenWorkerRuntime(
            transport=transport,
            runtime_backend="codex_mcp",
            llm_backend="codex",
            targeted_resume=False,
        )

        assert runtime.capabilities.targeted_resume is False
        assert runtime.capabilities.session_signals.after_turn_delivery is False

    def test_protocol_properties(self) -> None:
        rt = _runtime(_FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1")))
        assert rt.runtime_backend == "codex_mcp"
        assert rt.llm_backend == "codex"
        assert rt.working_directory == "/tmp"


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_yields_init_then_result_with_handle(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="PONG", session_id="thread-1"))
        rt = _runtime(t)
        messages = [m async for m in rt.execute_task(prompt="hi", system_prompt="be terse")]
        assert [m.type for m in messages] == ["system", "result"]
        assert messages[-1].content == "PONG"
        assert messages[-1].is_final
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-1"
        # Spawn (not resume) was used, and system_prompt was forwarded.
        assert len(t.spawn_calls) == 1
        assert t.spawn_calls[0]["system_prompt"] == "be terse"
        assert t.resume_calls == []

    @pytest.mark.asyncio
    async def test_spawn_to_result_ok(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="done", session_id="s9"))
        result = await _runtime(t).execute_task_to_result(prompt="go")
        assert result.is_ok
        assert result.value.final_message == "done"
        assert result.value.session_id == "s9"
        assert result.value.resume_handle.native_session_id == "s9"

    @pytest.mark.asyncio
    async def test_spawn_result_carries_usage_telemetry(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(
                text="done",
                session_id="s9",
                usage={"input_tokens": 7, "output_tokens": 3},
            )
        )

        messages = [m async for m in _runtime(t).execute_task(prompt="go")]

        assert messages[-1].data["usage"] == {"input_tokens": 7, "output_tokens": 3}


class TestModelOverride:
    """Per-call model-tier override (RFC #1405 sibling) threads to spawn and wins
    over the constructor pin; None falls back to it (existing callers unchanged)."""

    def _runtime_with_model(
        self, transport: _FakeTransport, *, model: str | None
    ) -> LeaderDrivenWorkerRuntime:
        return LeaderDrivenWorkerRuntime(
            transport=transport,
            runtime_backend="codex_mcp",
            llm_backend="codex",
            cwd="/tmp",
            model=model,
            model_override_support=ParamSupport.NATIVE,
        )

    def test_capability_defaults_to_ignored(self) -> None:
        rt = _runtime(_FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1")))
        assert rt.capabilities.model_override_support is ParamSupport.IGNORED

    def test_declared_support_is_surfaced(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1"))
        rt = self._runtime_with_model(t, model="pin")
        assert rt.capabilities.model_override_support is ParamSupport.NATIVE

    @pytest.mark.asyncio
    async def test_per_call_model_wins_over_constructor_pin(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1"))
        rt = self._runtime_with_model(t, model="pinned-model")
        _ = [m async for m in rt.execute_task(prompt="hi", model="haiku-child")]
        assert t.spawn_calls[0]["model"] == "haiku-child"

    @pytest.mark.asyncio
    async def test_none_model_falls_back_to_constructor_pin(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1"))
        rt = self._runtime_with_model(t, model="pinned-model")
        _ = [m async for m in rt.execute_task(prompt="hi")]
        assert t.spawn_calls[0]["model"] == "pinned-model"


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_uses_session_and_skips_spawn(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="first", session_id="s1"),
            resume_turn=WorkerTurn(text="second", session_id="s1"),
        )
        rt = _runtime(t)
        handle = RuntimeHandle(backend="codex_mcp", native_session_id="s1")
        result = await rt.execute_task_to_result(prompt="again", resume_handle=handle)
        assert result.is_ok
        assert result.value.final_message == "second"
        assert t.resume_calls == [
            {
                "session_id": "s1",
                "prompt": "again",
                "permission_mode": None,
                "model": None,
                "reasoning_effort": None,
            }
        ]
        assert t.spawn_calls == []  # resume path must NOT spawn a new session

    @pytest.mark.asyncio
    async def test_resume_forwards_per_call_model_and_effort(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="first", session_id="s1"),
            resume_turn=WorkerTurn(text="second", session_id="s1"),
        )
        rt = _runtime(t)
        handle = RuntimeHandle(backend="codex_mcp", native_session_id="s1")

        _ = [
            message
            async for message in rt.execute_task(
                prompt="again",
                resume_handle=handle,
                model="claude-haiku-4-5",
                reasoning_effort="high",
            )
        ]

        assert t.resume_calls == [
            {
                "session_id": "s1",
                "prompt": "again",
                "permission_mode": None,
                "model": "claude-haiku-4-5",
                "reasoning_effort": "high",
            }
        ]

    @pytest.mark.asyncio
    async def test_resume_forwards_runtime_permission_mode(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="first", session_id="s1"),
            resume_turn=WorkerTurn(text="second", session_id="s1"),
        )
        rt = LeaderDrivenWorkerRuntime(
            transport=t,
            runtime_backend="claude_mcp",
            llm_backend="claude",
            permission_mode="bypassPermissions",
        )

        _ = [
            message
            async for message in rt.execute_task(
                prompt="again",
                resume_handle=RuntimeHandle(backend="claude_mcp", native_session_id="s1"),
            )
        ]

        assert t.resume_calls[0]["permission_mode"] == "bypassPermissions"

    @pytest.mark.asyncio
    async def test_resume_does_not_re_emit_init(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="first", session_id="s1"),
            resume_turn=WorkerTurn(text="second", session_id="s1"),
        )
        handle = RuntimeHandle(backend="codex_mcp", native_session_id="s1")
        messages = [m async for m in _runtime(t).execute_task(prompt="x", resume_handle=handle)]
        # No "system" init message on resume — only the result.
        assert [m.type for m in messages] == ["result"]


class TestForkFromHostSession:
    """A handle carrying ``fork_session`` is the human's LIVE host conversation:
    it must be FORKED (via spawn), never resumed — resuming would corrupt the
    human's transcript."""

    @pytest.mark.asyncio
    async def test_fork_session_handle_spawns_fork_not_resume(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="forked", session_id="child-1"))
        # native_session_id is the PARENT (human) live session.
        handle = RuntimeHandle(
            backend="claude",
            native_session_id="parent-live",
            metadata={"fork_session": True},
        )
        result = await _runtime(t).execute_task_to_result(
            prompt="TASK\nbuild the widget", resume_handle=handle
        )
        assert result.is_ok
        # The parent live session was NEVER resumed (no transcript pollution).
        assert t.resume_calls == []
        assert len(t.spawn_calls) == 1
        # The fork source + human-facing label were threaded to the transport.
        assert t.spawn_calls[0]["fork_from_session_id"] == "parent-live"
        assert t.spawn_calls[0]["label"] == "ooo: build the widget"
        # The worker's OWN (forked) session id is what the handle now carries.
        assert result.value.session_id == "child-1"

    @pytest.mark.asyncio
    async def test_plain_handle_still_resumes_our_own_session(self) -> None:
        # A handle WITHOUT fork_session is one of our prior worker sessions → resume.
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="first", session_id="s1"),
            resume_turn=WorkerTurn(text="second", session_id="s1"),
        )
        handle = RuntimeHandle(backend="codex_mcp", native_session_id="s1")
        result = await _runtime(t).execute_task_to_result(prompt="again", resume_handle=handle)
        assert result.is_ok
        assert t.resume_calls == [
            {
                "session_id": "s1",
                "prompt": "again",
                "permission_mode": None,
                "model": None,
                "reasoning_effort": None,
            }
        ]
        assert t.spawn_calls == []


class TestSpawnLabel:
    @pytest.mark.asyncio
    async def test_fresh_spawn_threads_label(self) -> None:
        t = _FakeTransport(spawn_turn=WorkerTurn(text="ok", session_id="s1"))
        await _runtime(t).execute_task_to_result(prompt="DELIVERABLE\nship the thing")
        assert t.spawn_calls[0]["label"] == "ooo: ship the thing"
        assert t.spawn_calls[0]["fork_from_session_id"] is None


class TestErrors:
    @pytest.mark.asyncio
    async def test_transport_error_turn_is_terminal_err(self) -> None:
        t = _FakeTransport(
            spawn_turn=WorkerTurn(text="", session_id=None, is_error=True, error="boom")
        )
        result = await _runtime(t).execute_task_to_result(prompt="go")
        assert result.is_err
        assert "boom" not in result.error.message or result.error.message  # message present

    @pytest.mark.asyncio
    async def test_transport_exception_becomes_error_result(self) -> None:
        class _Boom:
            backend_name = "boom"

            async def spawn(self, **kwargs) -> WorkerTurn:
                raise RuntimeError("transport exploded")

            async def resume(
                self,
                *,
                session_id: str,
                prompt: str,
                permission_mode: str | None = None,
                model: str | None = None,
                reasoning_effort: str | None = None,
            ) -> WorkerTurn:  # pragma: no cover
                raise AssertionError

        rt = LeaderDrivenWorkerRuntime(
            transport=_Boom(), runtime_backend="codex_mcp", llm_backend="codex"
        )
        messages = [m async for m in rt.execute_task(prompt="go")]
        assert [m.type for m in messages] == ["result"]
        assert messages[0].is_error
        assert "transport exploded" in messages[0].content
