"""The model-tier router wired into the live execution paths.

Sibling of ``test_effort_routed_event.py``: those tests pin the effort dial's
seam in ``parallel_executor``/``runner``; these pin the model dial's. The
frugality proof reads the ``execution.ac.model_routed`` events these paths emit
and depends on the per-call ``model`` kwarg reaching only runtimes that enforce
it, so both the event payload and the kwarg plumbing are tested directly rather
than assumed.

The dormant default (no router) MUST be byte-identical to today's behavior:
``execute_task`` receives NO ``model`` kwarg, so a runtime that never declared a
``model`` parameter is never handed one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import (
    EconomicsConfig,
    ModelConfig,
    OuroborosConfig,
    TierConfig,
    get_default_config,
)
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeHandle,
)
from ouroboros.orchestrator.model_routing import ModelRouter, build_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.runner import OrchestratorRunner


def _economics() -> EconomicsConfig:
    """A minimal anthropic-populated economics config (mirrors test_model_routing)."""
    return EconomicsConfig(  # type: ignore[arg-type]
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="haiku-x")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="sonnet-x")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="opus-x")],
            ),
        },
    )


def _claude_router() -> ModelRouter:
    router = build_model_router(_economics(), runtime_backend="claude")
    assert router is not None
    return router


def _capturing_event_store() -> tuple[AsyncMock, list]:
    store = AsyncMock()
    events: list = []

    async def _append(event):
        events.append(event)

    store.append.side_effect = _append
    return store, events


class _EnforcedModelRuntime:
    """A runtime that declares NATIVE model override and captures the model kwarg."""

    _runtime_handle_backend = "claude"

    def __init__(self) -> None:
        self.received_model: str | None = "UNSET"

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return "/tmp/project"

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    @property
    def capabilities(self):
        return replace(FULL_CAPABILITIES, model_override_support=ParamSupport.NATIVE)

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
    ):
        self.received_model = model
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
            resume_handle=resume_handle,
        )


class _NoModelKwargRuntime:
    """A runtime with no capability declaration and no ``model`` kwarg (the default)."""

    _runtime_handle_backend = "opencode"

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return "/tmp/project"

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
            resume_handle=resume_handle,
        )


def _model_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == "execution.ac.model_routed"]


async def _run_one_ac(
    executor: ParallelACExecutor,
    *,
    is_sub_ac: bool,
    retry_attempt: int = 0,
    decomposition_trustworthy: bool = False,
):
    return await executor._execute_atomic_ac(
        ac_index=1,
        ac_content="Implement a thing",
        session_id="sess_model",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship it",
        depth=0,
        start_time=datetime.now(UTC),
        execution_id="exec_model",
        is_sub_ac=is_sub_ac,
        parent_ac_index=0 if is_sub_ac else None,
        sub_ac_index=0 if is_sub_ac else None,
        retry_attempt=retry_attempt,
        decomposition_trustworthy=decomposition_trustworthy,
    )


class TestExecutorModelWiring:
    @pytest.mark.asyncio
    async def test_top_level_ac_receives_standard_tier_model(self) -> None:
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        await _run_one_ac(executor, is_sub_ac=False)

        routed = _model_events(events)
        assert len(routed) == 1
        assert routed[0].data["model_tier"] == "standard"
        assert routed[0].data["model"] == "sonnet-x"
        assert routed[0].data["model_mode"] == "enforced"
        # The NATIVE runtime actually received the enforced model.
        assert runtime.received_model == "sonnet-x"

    @pytest.mark.asyncio
    async def test_untrusted_decomposed_child_receives_base_tier_model(self) -> None:
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        await _run_one_ac(executor, is_sub_ac=True)

        routed = _model_events(events)
        assert routed[0].data["model_tier"] == "standard"
        assert routed[0].data["model"] == "sonnet-x"
        assert routed[0].data["is_decomposed_child"] is True
        assert runtime.received_model == "sonnet-x"

    @pytest.mark.asyncio
    async def test_trusted_decomposed_child_receives_frugal_tier_model(self) -> None:
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        await _run_one_ac(executor, is_sub_ac=True, decomposition_trustworthy=True)

        routed = _model_events(events)
        assert routed[0].data["model_tier"] == "frugal"
        assert routed[0].data["model"] == "haiku-x"
        assert runtime.received_model == "haiku-x"

    @pytest.mark.asyncio
    async def test_retry_escalation_beats_child_drop(self) -> None:
        # A failing child earns a stronger model: escalation is applied AFTER the
        # child drop, so at the threshold it climbs back to the base tier.
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        await _run_one_ac(
            executor,
            is_sub_ac=True,
            retry_attempt=2,
            decomposition_trustworthy=True,
        )

        routed = _model_events(events)
        assert routed[0].data["model_tier"] == "standard"  # drop then raise = base
        assert routed[0].data["model"] == "sonnet-x"
        assert routed[0].data["retry_attempt"] == 2
        assert runtime.received_model == "sonnet-x"

    @pytest.mark.asyncio
    async def test_advised_runtime_records_advised_without_passing_kwarg(self) -> None:
        # A router built for the runtime's backend but a runtime that cannot enforce
        # a per-call model: the decision is recorded advised, no kwarg is passed.
        store, events = _capturing_event_store()
        router = build_model_router(_economics(), runtime_backend="opencode")
        assert router is None  # opencode is unmapped -> dormant
        runtime = _NoModelKwargRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=router,
        )

        # Must not raise even though execute_task has no ``model`` parameter.
        await _run_one_ac(executor, is_sub_ac=False)

        assert _model_events(events) == []  # dormant router emits nothing

    @pytest.mark.asyncio
    async def test_dormant_default_passes_no_model_kwarg(self) -> None:
        # Router None (the shipped default) is byte-identical to today: the NATIVE
        # runtime is never handed a ``model`` kwarg, so nothing is routed.
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            # model_router defaults None -> dormant
        )

        await _run_one_ac(executor, is_sub_ac=False)

        assert _model_events(events) == []
        # Byte-identical to today: no model override was passed.
        assert runtime.received_model is None


class TestModelRoutedEvent:
    @pytest.mark.asyncio
    async def test_enforced_event_payload_shape(self) -> None:
        store, events = _capturing_event_store()
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        await _run_one_ac(executor, is_sub_ac=False)

        routed = _model_events(events)
        assert len(routed) == 1
        data = routed[0].data
        # Payload carries every field the frugality proof joins on.
        assert data["execution_id"] == "exec_model"
        assert data["session_id"] == "sess_model"
        assert data["ac_index"] == 1
        assert data["is_decomposed_child"] is False
        assert data["model_tier"] == "standard"
        assert data["model"] == "sonnet-x"
        assert data["model_mode"] == "enforced"
        assert data["retry_attempt"] == 0
        assert data["runtime_backend"] == "claude"

    @pytest.mark.asyncio
    async def test_model_event_store_failure_does_not_abort_ac(self) -> None:
        """A degraded event store degrades the proof event to a warning, not an AC failure."""
        store = AsyncMock()
        model_append_attempts = 0

        async def _append(event):
            nonlocal model_append_attempts
            if getattr(event, "type", None) == "execution.ac.model_routed":
                model_append_attempts += 1
                raise RuntimeError("event store unavailable")

        store.append.side_effect = _append
        runtime = _EnforcedModelRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
        )

        # Must not raise even though the proof-event append fails; the AC still dispatches.
        await _run_one_ac(executor, is_sub_ac=False)

        assert runtime.received_model == "sonnet-x"
        assert model_append_attempts >= 1


class TestRunnerRouterConstruction:
    def _adapter(self, backend: str = "claude") -> MagicMock:
        adapter = MagicMock()
        adapter.runtime_backend = backend
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
        return adapter

    def _runner(self, adapter: MagicMock, **kwargs) -> OrchestratorRunner:
        return OrchestratorRunner(adapter, AsyncMock(), MagicMock(), **kwargs)

    def test_kill_switch_env_keeps_router_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        runner = self._runner(self._adapter("claude"))
        assert runner._model_router is None

    def test_execution_model_pin_env_keeps_router_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.setenv("OUROBOROS_EXECUTION_MODEL", "claude-opus-4-8")
        runner = self._runner(self._adapter("claude"))
        assert runner._model_router is None

    def test_default_builds_router_for_adapter_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        runner = self._runner(self._adapter("claude"))
        assert runner._model_router is not None
        assert runner._model_router.runtime_backend == "claude"

    def test_missing_user_config_uses_shipped_routing_and_verify_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A fresh HOME must not make routing accidentally dormant."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)

        runner = self._runner(self._adapter("claude"))

        assert runner._model_router is not None
        assert runner._model_router.runtime_backend == "claude"
        assert runner._model_router.child_tier == "frugal"
        assert runner._model_router.base_tier == "standard"
        assert runner._run_verify_commands is True
        assert runner._verify_command_timeout_seconds == 600
        assert runner._ac_retry_attempts == 2

    def test_existing_config_with_empty_tiers_uses_shipped_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fill only empty tiers; preserve every explicit non-tier economics knob."""
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        loaded_economics = EconomicsConfig(
            default_tier="frontier",
            escalation_threshold=7,
            downgrade_success_streak=11,
        )
        monkeypatch.setattr(
            "ouroboros.config.load_config",
            lambda: OuroborosConfig(economics=loaded_economics),
        )
        captured: dict[str, EconomicsConfig] = {}

        def _capture_economics(economics: EconomicsConfig, **_kwargs) -> None:
            captured["economics"] = economics
            return None

        monkeypatch.setattr(
            "ouroboros.orchestrator.model_routing.build_model_router",
            _capture_economics,
        )

        self._runner(self._adapter("claude"))

        economics = captured["economics"]
        assert economics.tiers == get_default_config().economics.tiers
        assert economics.default_tier == "frontier"
        assert economics.escalation_threshold == 7
        assert economics.downgrade_success_streak == 11

    def test_non_empty_custom_tiers_are_preserved_exactly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any non-empty user ladder remains authoritative, even when custom."""
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        monkeypatch.setattr(
            "ouroboros.config.load_config",
            lambda: OuroborosConfig(economics=_economics()),
        )

        runner = self._runner(self._adapter("claude"))

        assert runner._model_router is not None
        assert dict(runner._model_router.tier_models) == {
            "frugal": "haiku-x",
            "standard": "sonnet-x",
            "frontier": "opus-x",
        }

    def test_unmapped_backend_stays_dormant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        # opencode is intentionally unmapped -> routing dormant even by default.
        runner = self._runner(self._adapter("opencode"))
        assert runner._model_router is None

    def test_base_model_tier_override_threads_to_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        runner = self._runner(self._adapter("claude"), base_model_tier="frontier")
        assert runner._model_router is not None
        assert runner._model_router.base_tier == "frontier"
