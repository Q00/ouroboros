"""Opt-in shadow-replay baseline harness (frugality-proof AC5).

The sibling of ``test_frugality_producers.py``: those pin the token/effort/
grounding axes; this pins the LAST axis — the shadow-replay BASELINE (AC5) — and
proves that supplying it closes the deterministic proof's triad contract.

Three layers are covered:

* **Proof contract** — real emitter payloads (effort + token + deliver(+regression)
  + shadow_replay) fed back through ``assemble_triads`` / ``evaluate_proof``: a
  fully-measured enforced child counts, a regression FAILs, a thin sample is
  INSUFFICIENT_SAMPLE.
* **Harness** — ``run_shadow_replay`` with a mocked runtime factory: isolation is
  established and cleaned, the baseline runs at the PARENT tier, the event payload
  shape is right, a usage-less baseline emits nothing, and an isolation failure
  skips the replay entirely (never touching the live workspace).
* **Wiring** — flag OFF is byte-identical (no replay, no event, factory untouched).

No real CLI is ever spawned; every runtime/factory here is a scripted double.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.harness.decomposition_attestation import (
    DecompositionAttestation,
    DecompositionTrustVerdict,
)
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.execution_runtime_scope import (
    ExecutionNodeIdentity,
    build_ac_runtime_identity,
)
from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_SHADOW_REPLAY,
    ProofStatus,
    assemble_triads,
    evaluate_proof,
)
from ouroboros.orchestrator.model_routing import ModelRouter, build_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.shadow_replay import (
    STRICT_EXTERNAL_EFFECT_ISOLATION,
    STRICT_FILESYSTEM_ISOLATION,
    isolated_workspace,
    run_shadow_replay,
    shadow_replay_enabled_from_env,
)


# -- Shared doubles -----------------------------------------------------------
def _capturing_event_store() -> tuple[AsyncMock, list]:
    store = AsyncMock()
    events: list = []

    async def _append(event):
        events.append(event)

    store.append.side_effect = _append
    return store, events


def _economics() -> EconomicsConfig:
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


def _usage_result(usage: dict | None) -> AgentMessage:
    data: dict = {"subtype": "success"}
    if usage is not None:
        data["usage"] = usage
    return AgentMessage(type="result", content="[TASK_COMPLETE]", data=data)


def _verified_usage_messages(usage: dict | None) -> list[AgentMessage]:
    """A profile-valid mutation + test transcript ending in measured evidence."""
    return [
        AgentMessage(
            type="tool",
            content="edit state.txt",
            tool_name="Edit",
            data={
                "tool_call_id": "edit_state",
                "tool_input": {"file_path": "state.txt"},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="updated state.txt",
            data={
                "subtype": "tool_result",
                "tool_call_id": "edit_state",
                "is_error": False,
            },
        ),
        AgentMessage(
            type="tool",
            content="run pytest",
            tool_name="Bash",
            data={
                "tool_call_id": "test_state",
                "tool_input": {"command": "pytest"},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="1 passed in 0.01s",
            data={
                "subtype": "tool_result",
                "tool_call_id": "test_state",
                "exit_code": 0,
                "output": "1 passed in 0.01s",
            },
        ),
        AgentMessage(
            type="result",
            content=(
                '```json\n{"files_touched":["state.txt"],'
                '"commands_run":["pytest"],"tests_passed":["pytest"]}\n```'
            ),
            data={
                "subtype": "success",
                **({"usage": usage} if usage is not None else {}),
            },
        ),
    ]


class _FakeBaselineRuntime:
    """A throwaway baseline runtime the mocked factory returns.

    Records the constructor kwargs so the test can assert the baseline was built
    at the parent tier + isolated cwd, and yields a scripted usage stream.
    """

    def __init__(
        self,
        *,
        backend,
        model,
        cwd,
        messages: list[AgentMessage],
        permission_mode=None,
        llm_backend=None,
        execute_error: Exception | None = None,
        close_error: Exception | None = None,
        strict_isolation: bool = True,
        external_effect_isolation: bool = True,
    ) -> None:
        self.backend = backend
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.llm_backend = llm_backend
        self.cwd_existed_at_build = bool(cwd) and os.path.isdir(cwd)
        self._messages = messages
        self._execute_error = execute_error
        self._close_error = close_error
        self.closed = False
        self.execute_calls = 0
        self.shadow_replay_filesystem_isolation = (
            STRICT_FILESYSTEM_ISOLATION if strict_isolation else None
        )
        self.shadow_replay_external_effect_isolation = (
            STRICT_EXTERNAL_EFFECT_ISOLATION if external_effect_isolation else None
        )

    @property
    def working_directory(self) -> str | None:
        return self.cwd

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        **kwargs,
    ):
        self.execute_calls += 1
        self.received_prompt = prompt
        self.received_resume_handle = resume_handle
        if self._execute_error is not None:
            raise self._execute_error
        for message in self._messages:
            yield message

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _executor_with_router(*, task_cwd: str, store: AsyncMock) -> ParallelACExecutor:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.working_directory = task_cwd
    adapter.permission_mode = "bypassPermissions"
    adapter.llm_backend = "claude_code"
    return ParallelACExecutor(
        adapter=adapter,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        model_router=_claude_router(),
        task_cwd=task_cwd,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )


def _identity():
    return build_ac_runtime_identity(
        1,
        execution_context_id="exec_frugal",
        is_sub_ac=True,
        parent_ac_index=0,
        sub_ac_index=0,
    )


def _shadow_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == EVENT_SHADOW_REPLAY]


# -- Layer 1: proof contract closes with real emitter payloads ----------------
class _EmitterHarness:
    """Emit the proof-axis events for one AC via the REAL emitter.

    Mirrors production shape: sub-AC identities carry a real
    ``ExecutionNodeIdentity`` (so every axis event has a joinable
    ``parent_node_id``), and each decomposition round is closed with the REAL
    gate-anchored ``execution.ac.decomposition_attested`` event — since the
    Round-7 fail-closed fix, a decomposed-child row with no joinable parent
    identity or no trustworthy attestation never counts in the proof.
    """

    def __init__(self) -> None:
        self.events: list = []
        self._finalized_roots: set[tuple[str, int]] = set()
        self._attested_rounds: set[tuple[str, int]] = set()

        async def _safe_emit(event) -> bool:
            self.events.append(event)
            return True

        self.emitter = ExecutionEventEmitter(AsyncMock(), safe_emit_event=_safe_emit)

    async def emit_row(
        self,
        *,
        run_id: str,
        sub_index: int,
        spend: float,
        baseline: float,
        regression: bool = False,
        effort_mode: str = "enforced",
        trustworthy: bool = True,
    ) -> None:
        root_node_identity = ExecutionNodeIdentity.root(
            execution_context_id=run_id,
            ac_index=0,
        )
        identity = build_ac_runtime_identity(
            1,
            execution_context_id=run_id,
            is_sub_ac=True,
            parent_ac_index=0,
            sub_ac_index=sub_index,
            node_identity=root_node_identity.child(sub_index),
        )
        session_id = f"sess-{run_id}"
        if (run_id, 0) not in self._attested_rounds:
            self._attested_rounds.add((run_id, 0))
            await self.emitter.emit_decomposition_attested(
                execution_id=run_id,
                session_id=session_id,
                node_identity=root_node_identity,
                attestation=DecompositionAttestation(
                    node_id=root_node_identity.node_id,
                    verdict=DecompositionTrustVerdict.TRUSTWORTHY,
                    failed_axis=None,
                    failed_sibling_id=None,
                    reason="all sibling gates and the parent gate passed",
                ),
                retry_attempt=0,
            )
        await self.emitter.emit_effort_routed(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            effort_level="high",
            effort_mode=effort_mode,
            base_reasoning_effort="high",
            runtime_backend="claude",
        )
        await self.emitter.emit_model_routed(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            model_tier="frugal",
            model="haiku-x",
            model_mode="enforced",
            retry_attempt=0,
            runtime_backend="claude",
        )
        await self.emitter.emit_token_attribution(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            retry_attempt=0,
            token_spend=spend,
            usage_breakdown={"input_tokens": spend},
            model="haiku-x",
            model_tier="frugal",
            model_mode="enforced",
            effort_level="high",
            runtime_backend="claude",
        )
        await self.emitter.emit_deliver_verdict(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            is_sub_ac=True,
            traceguard_verdict="rejected" if regression else "accepted",
            unsupported_claim_rate=1.0 if regression else 0.0,
            rejected_reasons=["unsupported"] if regression else [],
            accepted_fact_count=0 if regression else 1,
            grounding_regression=regression,
        )
        await self.emitter.emit_shadow_replay(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            baseline_token_spend=baseline,
            baseline_mode="shadow_replay",
            baseline_model="sonnet-x",
            baseline_tier="standard",
            decomposition_trustworthy=trustworthy,
        )
        root_key = (run_id, 0)
        if root_key not in self._finalized_roots:
            self._finalized_roots.add(root_key)
            self.events.append(
                BaseEvent(
                    type=EVENT_AC_OUTCOME_FINALIZED,
                    aggregate_type="execution",
                    aggregate_id=run_id,
                    data={
                        "execution_id": run_id,
                        "root_ac_index": 0,
                        "retry_attempt": 0,
                        "success": True,
                        "is_decomposed": True,
                    },
                )
            )


class TestProofContractClosesWithBaseline:
    @pytest.mark.asyncio
    async def test_single_fully_measured_child_counts(self) -> None:
        harness = _EmitterHarness()
        await harness.emit_row(run_id="run-0", sub_index=0, spend=50, baseline=100)

        rows = assemble_triads(harness.events)
        assert len(rows) == 1
        row = rows[0]
        assert row.has_all_axes is True
        # The last axis closes the triad: this enforced, trustworthy, decomposed
        # child with a positive baseline now counts toward the proof.
        assert row.counts_in_proof is True
        assert row.baseline_token_spend == pytest.approx(100)
        assert row.baseline_mode == "shadow_replay"
        assert row.grounding_regression is False

    @pytest.mark.asyncio
    async def test_full_sample_passes(self) -> None:
        harness = _EmitterHarness()
        for run in range(3):
            for sub in range(7):
                await harness.emit_row(run_id=f"run-{run}", sub_index=sub, spend=50, baseline=100)

        verdict = evaluate_proof(assemble_triads(harness.events))
        assert verdict.status is ProofStatus.PASS
        assert verdict.counted_rows == 21
        assert verdict.runs == 3
        assert verdict.token_reduction_pct == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_grounding_regression_fails_proof(self) -> None:
        harness = _EmitterHarness()
        for run in range(3):
            for sub in range(7):
                # One child lost grounding at the cheap tier — a per-AC veto.
                regression = run == 0 and sub == 0
                await harness.emit_row(
                    run_id=f"run-{run}",
                    sub_index=sub,
                    spend=50,
                    baseline=100,
                    regression=regression,
                )

        verdict = evaluate_proof(assemble_triads(harness.events))
        assert verdict.status is ProofStatus.FAIL_GROUNDING_REGRESSION
        assert verdict.grounding_regressions == 1

    @pytest.mark.asyncio
    async def test_thin_sample_is_insufficient(self) -> None:
        harness = _EmitterHarness()
        for run in range(2):
            for sub in range(2):
                await harness.emit_row(run_id=f"run-{run}", sub_index=sub, spend=50, baseline=100)

        verdict = evaluate_proof(assemble_triads(harness.events))
        # Rows are fully measured but too few over too few runs.
        assert verdict.status is ProofStatus.INSUFFICIENT_SAMPLE
        assert verdict.counted_rows == 4

    @pytest.mark.asyncio
    async def test_deliver_verdict_without_regression_omits_key(self) -> None:
        # A caller with no deterministic regression policy leaves it unset →
        # the proof honestly excludes the row (has_all_axes needs the flag set).
        harness = _EmitterHarness()
        await harness.emitter.emit_deliver_verdict(
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            is_sub_ac=True,
            traceguard_verdict="accepted",
            unsupported_claim_rate=0.0,
            rejected_reasons=[],
            accepted_fact_count=1,
        )
        assert "grounding_regression" not in harness.events[0].data


# -- Layer 2: the harness itself ---------------------------------------------
class TestShadowReplayHarness:
    @pytest.mark.asyncio
    async def test_baseline_runs_at_parent_tier_in_isolated_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real task cwd with content; snapshot it before invoking the replay.
        (tmp_path / "src.py").write_text("print('hi')\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("x")

        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 80, "output_tokens": 20}),
            )
            # Capture whether the isolated copy actually exists (and excludes junk)
            # at the moment the baseline runtime is constructed.
            runtime.isolated_has_src = os.path.isfile(os.path.join(cwd, "src.py"))
            runtime.isolated_has_node_modules = os.path.isdir(os.path.join(cwd, "node_modules"))
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="do the thing",
                system_prompt="system",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert len(built) == 1
        baseline = built[0]
        # Built at the PARENT tier (standard → sonnet-x), same backend.
        assert baseline.model == "sonnet-x"
        assert baseline.backend == "claude"
        assert baseline.permission_mode == "bypassPermissions"
        assert baseline.llm_backend == "claude_code"
        # Ran against the ISOLATED copy (not the live cwd), which held the source
        # but excluded node_modules, and was a fresh session.
        assert baseline.cwd != str(tmp_path)
        assert baseline.isolated_has_src is True
        assert baseline.isolated_has_node_modules is False
        assert baseline.received_resume_handle is None
        assert baseline.closed is True
        # The isolated copy is cleaned up after the replay.
        assert not os.path.exists(baseline.cwd)

        # One shadow_replay event carrying the measured baseline spend + provenance.
        shadow = _shadow_events(events)
        assert len(shadow) == 1
        data = shadow[0].data
        assert data["baseline_token_spend"] == pytest.approx(100)
        assert data["baseline_mode"] == "shadow_replay"
        assert data["baseline_model"] == "sonnet-x"
        assert data["baseline_tier"] == "standard"
        assert data["decomposition_trustworthy"] is True
        assert data["is_decomposed_child"] is True

    @pytest.mark.asyncio
    async def test_runtime_without_strict_isolation_contract_is_never_executed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 90, "output_tokens": 10}),
                strict_isolation=False,
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt=f"Edit {tmp_path / 'live.txt'}",
                system_prompt="system",
                tools=["Edit", "Bash"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert built[0].execute_calls == 0
        assert built[0].closed is True
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_runtime_without_external_effect_isolation_is_never_executed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 90, "output_tokens": 10}),
                external_effect_isolation=False,
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="Post a deployment notification",
                system_prompt="system",
                tools=["mcp__slack__send_message"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert built[0].execute_calls == 0
        assert built[0].closed is True
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_semantic_failure_with_success_subtype_and_usage_emits_no_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=[
                    AgentMessage(
                        type="result",
                        content="I could not complete the task; dependencies are missing",
                        data={
                            "subtype": "success",
                            "usage": {"input_tokens": 100_000},
                        },
                    )
                ],
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="implement",
                system_prompt="system",
                tools=["Edit", "Bash"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert built[0].execute_calls == 1
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_verify_command_is_never_run_outside_runtime_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside_marker = tmp_path / "host-command-ran.txt"

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            return _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 90, "output_tokens": 10}),
            )

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="implement",
                system_prompt="system",
                tools=["Edit", "Bash"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                ac_spec=AcceptanceCriterionSpec(
                    description="Implement a thing",
                    verify_command=f"touch {outside_marker}",
                ),
                isolated_cwd=isolated_cwd,
            )

        assert not outside_marker.exists()
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_usage_less_baseline_emits_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            # Baseline that reports NO usage telemetry.
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages(None),
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="p",
                system_prompt="s",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        # Missing is missing: no baseline spend measured → no event fabricated.
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_baseline_respects_live_profile_starting_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile-pinned frugal parent cannot be replayed as standard."""
        (tmp_path / "src.py").write_text("print('hi')\n")
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 8, "output_tokens": 2}),
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="do the thing",
                system_prompt="system",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
                suggested_tier="frugal",
            )

        assert built[0].model == "haiku-x"
        shadow = _shadow_events(events)
        assert len(shadow) == 1
        assert shadow[0].data["baseline_tier"] == "frugal"
        assert built[0].closed is True

    @pytest.mark.asyncio
    async def test_terminal_error_with_usage_emits_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            # A failed baseline can still consume substantial tokens while trying
            # to recover from missing snapshot dependencies/metadata. That spend
            # must never inflate the comparison denominator.
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=[
                    AgentMessage(
                        type="result",
                        content="dependency unavailable",
                        data={
                            "subtype": "error",
                            "usage": {"input_tokens": 900, "output_tokens": 100},
                        },
                    )
                ],
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="p",
                system_prompt="s",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert _shadow_events(events) == []
        assert built[0].closed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "messages",
        [
            [
                AgentMessage(
                    type="assistant",
                    content="stream ended early",
                    data={"usage": {"input_tokens": 90, "output_tokens": 10}},
                )
            ],
            [
                AgentMessage(
                    type="result",
                    content="unknown outcome",
                    data={"usage": {"input_tokens": 90, "output_tokens": 10}},
                )
            ],
            [
                _usage_result({"input_tokens": 40, "output_tokens": 10}),
                _usage_result({"input_tokens": 40, "output_tokens": 10}),
            ],
            [
                AgentMessage(
                    type="result",
                    content="contradictory outcome",
                    data={
                        "subtype": "success",
                        "is_error": True,
                        "usage": {"input_tokens": 90, "output_tokens": 10},
                    },
                )
            ],
        ],
        ids=["missing-terminal", "missing-status", "multiple-terminals", "contradictory-status"],
    )
    async def test_missing_or_ambiguous_terminal_with_usage_emits_no_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        messages: list[AgentMessage],
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=messages,
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="p",
                system_prompt="s",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert _shadow_events(events) == []
        assert built[0].closed is True

    @pytest.mark.asyncio
    async def test_runtime_close_failure_does_not_discard_measured_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 7, "output_tokens": 3}),
                close_error=RuntimeError("close boom"),
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="p",
                system_prompt="s",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert built[0].closed is True
        shadow = _shadow_events(events)
        assert len(shadow) == 1
        assert shadow[0].data["baseline_token_spend"] == pytest.approx(10)

    @pytest.mark.asyncio
    async def test_runtime_is_closed_when_baseline_execution_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=[],
                execute_error=RuntimeError("baseline boom"),
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )
        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        with isolated_workspace(str(tmp_path)) as isolated_cwd:
            assert isolated_cwd is not None
            await run_shadow_replay(
                executor,
                runtime_identity=_identity(),
                execution_id="exec_frugal",
                session_id="sess",
                ac_index=1,
                is_sub_ac=True,
                prompt="p",
                system_prompt="s",
                tools=["Read"],
                decomposition_trustworthy=True,
                ac_content="Implement a thing",
                isolated_cwd=isolated_cwd,
            )

        assert built[0].closed is True
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_isolation_failure_skips_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="p",
            system_prompt="s",
            tools=["Read"],
            decomposition_trustworthy=True,
            ac_content="Implement a thing",
            isolated_cwd=None,
        )

        # Never build a runtime, never emit — and above all never run against the
        # live workspace.
        factory.assert_not_called()
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_no_router_skips_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)
        store, events = _capturing_event_store()
        adapter = MagicMock()
        adapter.runtime_backend = "claude"
        adapter.working_directory = str(tmp_path)
        executor = ParallelACExecutor(
            adapter=adapter,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=None,  # routing dormant → no parent baseline tier
            task_cwd=str(tmp_path),
        )

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="p",
            system_prompt="s",
            tools=["Read"],
            decomposition_trustworthy=True,
            ac_content="Implement a thing",
            isolated_cwd=None,
        )

        factory.assert_not_called()
        assert _shadow_events(events) == []


# -- isolated_workspace primitive --------------------------------------------
class TestIsolatedWorkspace:
    def test_copytree_copies_and_cleans(self, tmp_path: Path) -> None:
        (tmp_path / "keep.txt").write_text("data")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "big").write_text("x")

        captured: str | None = None
        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is not None
            captured = isolated
            assert os.path.isfile(os.path.join(isolated, "keep.txt"))
            # Heavy, regenerable dirs are excluded from the baseline copy.
            assert not os.path.exists(os.path.join(isolated, ".venv"))
        # Cleaned up on exit.
        assert captured is not None
        assert not os.path.exists(captured)

    def test_git_workspace_preserves_dirty_and_untracked_files_without_git_metadata(
        self, tmp_path: Path
    ) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        tracked = tmp_path / "tracked.txt"
        tracked.write_text("committed\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Shadow Replay Test",
                "-c",
                "user.email=shadow@example.invalid",
                "commit",
                "-qm",
                "initial",
            ],
            cwd=tmp_path,
            check=True,
        )
        tracked.write_text("dirty working tree\n")
        (tmp_path / "untracked.txt").write_text("untracked input\n")

        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is not None
            snapshot = Path(isolated)
            assert (snapshot / "tracked.txt").read_text() == "dirty working tree\n"
            assert (snapshot / "untracked.txt").read_text() == "untracked input\n"
            # Safety boundary: no shared/copied repository metadata. Git-dependent
            # prompts are an explicit limitation of this experiment harness.
            assert not (snapshot / ".git").exists()
            probe = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=snapshot,
                capture_output=True,
                text=True,
                check=False,
            )
            assert probe.returncode != 0

    def test_snapshot_is_frozen_at_context_entry(self, tmp_path: Path) -> None:
        state = tmp_path / "state.txt"
        state.write_text("before\n")

        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is not None
            state.write_text("after\n")
            (tmp_path / "created-after-snapshot.txt").write_text("late\n")
            snapshot = Path(isolated)
            assert (snapshot / "state.txt").read_text() == "before\n"
            assert not (snapshot / "created-after-snapshot.txt").exists()

    def test_body_exception_propagates_and_snapshot_is_cleaned(self, tmp_path: Path) -> None:
        captured: str | None = None

        with pytest.raises(RuntimeError, match="body boom"):
            with isolated_workspace(str(tmp_path)) as isolated:
                assert isolated is not None
                captured = isolated
                raise RuntimeError("body boom")

        assert captured is not None
        assert not Path(captured).exists()

    def test_escaping_symlink_rejects_snapshot(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("live data\n")
        try:
            (tmp_path / "escape").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")

        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is None


# -- shadow_replay_enabled_from_env ------------------------------------------
class TestEnvFlag:
    @pytest.mark.parametrize("value", ["1", "true", "on", "TRUE", " On "])
    def test_enabled_tokens(self, value: str) -> None:
        assert shadow_replay_enabled_from_env({"OUROBOROS_SHADOW_REPLAY": value}) is True

    @pytest.mark.parametrize("value", ["", "0", "off", "false", "yes", "nope"])
    def test_disabled_tokens(self, value: str) -> None:
        assert shadow_replay_enabled_from_env({"OUROBOROS_SHADOW_REPLAY": value}) is False

    def test_unset_is_disabled(self) -> None:
        assert shadow_replay_enabled_from_env({}) is False


# -- Layer 3: executor wiring — flag OFF is byte-identical --------------------
class _ScriptedRuntime:
    """Advised runtime yielding a scripted success stream (the live child)."""

    def __init__(
        self,
        messages: list[AgentMessage],
        *,
        working_directory: str = "/tmp/project",
        on_execute=None,
    ) -> None:
        self._messages = messages
        self._working_directory = working_directory
        self._on_execute = on_execute

    @property
    def runtime_backend(self) -> str:
        return "claude"

    @property
    def working_directory(self) -> str | None:
        return self._working_directory

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    @property
    def llm_backend(self) -> str | None:
        return "claude_code"

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        if self._on_execute is not None:
            self._on_execute()
        for message in self._messages:
            yield replace(message, resume_handle=resume_handle)


async def _run_child_leaf(executor: ParallelACExecutor):
    return await executor._execute_atomic_ac(
        ac_index=1,
        ac_content="Implement a thing",
        session_id="sess_frugal",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship it",
        depth=0,
        start_time=datetime.now(UTC),
        execution_id="exec_frugal",
        is_sub_ac=True,
        parent_ac_index=0,
        sub_ac_index=0,
        retry_attempt=0,
    )


class TestExecutorWiring:
    @pytest.mark.asyncio
    async def test_flag_off_never_replays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime([_usage_result({"input_tokens": 10, "output_tokens": 2})])
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            # shadow_replay_enabled defaults False
        )

        result = await _run_child_leaf(executor)

        assert result.success is True
        # Flag off → the harness is never entered: no factory build, no event.
        factory.assert_not_called()
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_flag_on_skips_child_without_decomposition_attestation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = tmp_path / "state.txt"
        state.write_text("before live child\n")
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, permission_mode, model, cwd, llm_backend):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                permission_mode=permission_mode,
                llm_backend=llm_backend,
                messages=_verified_usage_messages({"input_tokens": 90, "output_tokens": 10}),
            )
            runtime.snapshot_state = (Path(cwd) / "state.txt").read_text()
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime(
            _verified_usage_messages({"input_tokens": 10, "output_tokens": 2}),
            working_directory=str(tmp_path),
            on_execute=lambda: state.write_text("after live child\n"),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            task_cwd=str(tmp_path),
            shadow_replay_enabled=True,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await _run_child_leaf(executor)

        assert result.success is True
        # The live decomposer has no deterministic MECE attestation yet, so
        # opt-in shadow mode skips before spending any baseline tokens.
        assert built == []
        assert state.read_text() == "after live child\n"
        assert _shadow_events(events) == []
