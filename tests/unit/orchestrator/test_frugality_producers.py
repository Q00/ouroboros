"""The frugality-proof producers + consumer wired into the live run path.

Siblings of ``test_effort_routed_event.py`` / ``test_model_routing_wiring.py``:
those pin the effort and model dials; these pin the two remaining proof axes the
seed exists to activate — the **token** axis (per-AC runtime spend, AC2) and the
**grounding** axis (TraceGuard deliver verdict, AC4) — plus the run-end
**consumer** that assembles triads and evaluates the deterministic proof.

The contract the producers must satisfy is fixed in ``frugality_proof``; these
tests feed the actual produced events back through ``assemble_triads`` /
``evaluate_proof`` so the wiring is proven against that contract rather than
assumed. No real CLI is ever spawned — every runtime here is a scripted double.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind, EvidenceManifest
from ouroboros.orchestrator import parallel_executor as pe_module
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeHandle,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.execution_runtime_scope import build_ac_runtime_identity
from ouroboros.orchestrator.frugality_proof import (
    EVENT_DELIVER_VERDICT,
    EVENT_EFFORT_ROUTED,
    EVENT_SHADOW_REPLAY,
    EVENT_TOKEN_ATTRIBUTION,
    ProofStatus,
    assemble_triads,
    evaluate_proof,
)
from ouroboros.orchestrator.model_routing import ModelRouter, build_model_router
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionResult,
    ParallelACExecutor,
    ParallelExecutionResult,
)
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionTracker


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


def _claude_result(usage: dict | None = None, content: str = "[TASK_COMPLETE]") -> AgentMessage:
    data: dict = {"subtype": "success"}
    if usage is not None:
        data["usage"] = usage
    return AgentMessage(type="result", content=content, data=data)


def _codex_turn_completed(usage: dict) -> AgentMessage:
    # Mirrors codex_cli_runtime: a ``turn.completed`` system message carrying usage.
    return AgentMessage(
        type="system", content="", data={"subtype": "turn.completed", "usage": usage}
    )


class _ScriptedRuntime:
    """Advised runtime (no native knobs) that yields a scripted message stream.

    When ``raise_after`` is set it raises after yielding its scripted messages,
    exercising the executor's exception path (spend is still spend there).
    """

    def __init__(
        self,
        messages: list[AgentMessage],
        *,
        backend: str = "claude",
        raise_after: bool = False,
    ) -> None:
        self._messages = messages
        self._backend = backend
        self._raise_after = raise_after

    @property
    def runtime_backend(self) -> str:
        return self._backend

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
        for message in self._messages:
            yield replace(message, resume_handle=resume_handle)
        if self._raise_after:
            raise RuntimeError("runtime failed mid-stream")


class _EnforcedModelUsageRuntime:
    """NATIVE model-override runtime that reports usage — for the model/tier join."""

    _runtime_handle_backend = "claude"

    def __init__(self, usage: dict) -> None:
        self.received_model: str | None = None
        self._usage = usage

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
        yield _claude_result(self._usage)


def _token_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == EVENT_TOKEN_ATTRIBUTION]


def _deliver_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == EVENT_DELIVER_VERDICT]


async def _run_one_ac(
    executor: ParallelACExecutor,
    *,
    is_sub_ac: bool = True,
    retry_attempt: int = 0,
):
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
        is_sub_ac=is_sub_ac,
        parent_ac_index=0 if is_sub_ac else None,
        sub_ac_index=0 if is_sub_ac else None,
        retry_attempt=retry_attempt,
    )


# -- Producer 1: token attribution (seed AC2) --------------------------------
class TestTokenAttribution:
    @pytest.mark.asyncio
    async def test_summed_spend_from_multi_usage_stream(self) -> None:
        # A Claude result usage plus a Codex turn.completed usage in one stream are
        # summed — a child's full spend is attributed even when reported in pieces.
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime(
            [
                _codex_turn_completed({"input_tokens": 30, "output_tokens": 7}),
                _claude_result({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
            ]
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        await _run_one_ac(executor)

        token = _token_events(events)
        assert len(token) == 1
        data = token[0].data
        # token_spend = Σ(input_tokens + output_tokens) across both messages.
        assert data["token_spend"] == pytest.approx(30 + 7 + 100 + 20)
        # usage_breakdown carries the summed per-key totals.
        assert data["usage_breakdown"]["input_tokens"] == pytest.approx(130)
        assert data["usage_breakdown"]["output_tokens"] == pytest.approx(27)
        assert data["usage_breakdown"]["total_tokens"] == pytest.approx(120)
        assert data["token_source"] == "runtime_usage"

    @pytest.mark.asyncio
    async def test_no_usage_emits_no_event(self) -> None:
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime([_claude_result()])  # result with no usage
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        await _run_one_ac(executor)

        # Missing is missing: the proof never fabricates a char-proxy spend.
        assert _token_events(events) == []

    @pytest.mark.asyncio
    async def test_malformed_usage_entry_is_skipped(self) -> None:
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime(
            [
                # Non-dict usage → skipped entirely.
                AgentMessage(type="assistant", content="", data={"usage": "not-a-dict"}),
                # Non-numeric / non-finite values within a dict → those keys skipped.
                _claude_result({"input_tokens": 10, "output_tokens": float("nan"), "extra": "x"}),
            ]
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        await _run_one_ac(executor)

        token = _token_events(events)
        assert len(token) == 1
        # Only the finite input_tokens survived; NaN output and non-numeric dropped.
        assert token[0].data["token_spend"] == pytest.approx(10)
        assert token[0].data["usage_breakdown"] == {"input_tokens": 10}

    @pytest.mark.asyncio
    async def test_payload_carries_model_tier_and_execution_id(self) -> None:
        store, events = _capturing_event_store()
        runtime = _EnforcedModelUsageRuntime({"input_tokens": 40, "output_tokens": 10})
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            reasoning_effort="high",
        )

        # Decomposed child → frugal tier (standard base dropped one notch).
        await _run_one_ac(executor, is_sub_ac=True)

        token = _token_events(events)
        assert len(token) == 1
        data = token[0].data
        assert data["execution_id"] == "exec_frugal"
        assert data["session_id"] == "sess_frugal"
        assert data["is_decomposed_child"] is True
        assert data["model_tier"] == "frugal"
        assert data["model"] == "haiku-x"
        assert data["model_mode"] == "enforced"
        assert data["effort_level"] == "high"  # child inherits parent effort unchanged
        assert data["runtime_backend"] == "claude"
        # ac_id is present so the proof can join token × effort × grounding.
        assert data["ac_id"]

    @pytest.mark.asyncio
    async def test_failure_path_still_emits_when_usage_present(self) -> None:
        # Spend is spend: a runtime that reports usage then raises mid-stream must
        # still have its spend attributed on the exception path.
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime(
            [
                AgentMessage(
                    type="assistant",
                    content="",
                    data={"usage": {"input_tokens": 12, "output_tokens": 3}},
                )
            ],
            raise_after=True,
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await _run_one_ac(executor)

        assert result.success is False  # the AC genuinely failed
        token = _token_events(events)
        assert len(token) == 1
        assert token[0].data["token_spend"] == pytest.approx(15)


# -- Producer 2: deliver verdict (seed AC4, observe-only) ---------------------
def _runtime_identity(ac_id_index: int = 1):
    return build_ac_runtime_identity(
        ac_id_index,
        execution_context_id="exec_frugal",
        is_sub_ac=True,
        parent_ac_index=0,
        sub_ac_index=0,
        retry_attempt=0,
    )


def _manifest_with_handle(ac_id: str, handle: str) -> EvidenceManifest:
    now = datetime.now(UTC)
    return EvidenceManifest(
        ac_id=ac_id,
        entries=(
            EvidenceEntry(
                handle=handle,
                kind=EvidenceKind.FILE_MODIFIED,
                ok=True,
                started_at=now,
                ended_at=now,
                payload={"tool_name": "Write", "result_preview": "wrote foo.py"},
                source_event_ids=("evt-1",),
            ),
        ),
    )


def _deliver_executor() -> tuple[ParallelACExecutor, list]:
    store, events = _capturing_event_store()
    executor = ParallelACExecutor(
        adapter=_ScriptedRuntime([_claude_result()]),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    return executor, events


class TestDeliverVerdict:
    @pytest.mark.asyncio
    async def test_structured_evidence_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor, events = _deliver_executor()
        identity = _runtime_identity()
        manifest = _manifest_with_handle(identity.ac_id, "h1")

        async def _fake_load(*args, **kwargs):
            return manifest

        monkeypatch.setattr(pe_module, "load_ac_evidence_manifest", _fake_load)

        typed_evidence = EvidenceRecord(
            data={
                "observed_facts": [{"fact_id": "f1", "evidence_handle": "h1", "statement": "did x"}]
            },
        )
        await executor._observe_deliver_verdict(
            runtime_identity=identity,
            execution_id="exec_frugal",
            session_id="sess_frugal",
            is_sub_ac=True,
            success=True,
            typed_evidence=typed_evidence,
        )

        deliver = _deliver_events(events)
        assert len(deliver) == 1
        data = deliver[0].data
        assert data["traceguard_verdict"] == "accepted"
        assert data["unsupported_claim_rate"] == pytest.approx(0.0)
        assert data["accepted_fact_count"] == 1
        # observe-only: grounding_regression is deliberately never set.
        assert "grounding_regression" not in data

    @pytest.mark.asyncio
    async def test_unsupported_fact_rejected_with_positive_rate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor, events = _deliver_executor()
        identity = _runtime_identity()
        # Manifest carries NO matching handle → the claimed fact is unsupported.
        empty_manifest = EvidenceManifest(ac_id=identity.ac_id, entries=())

        async def _fake_load(*args, **kwargs):
            return empty_manifest

        monkeypatch.setattr(pe_module, "load_ac_evidence_manifest", _fake_load)

        typed_evidence = EvidenceRecord(
            data={"observed_facts": [{"fact_id": "f1", "evidence_handle": "ghost"}]},
        )
        await executor._observe_deliver_verdict(
            runtime_identity=identity,
            execution_id="exec_frugal",
            session_id="sess_frugal",
            is_sub_ac=True,
            success=True,
            typed_evidence=typed_evidence,
        )

        deliver = _deliver_events(events)
        assert len(deliver) == 1
        data = deliver[0].data
        assert data["traceguard_verdict"] == "rejected"
        assert data["unsupported_claim_rate"] > 0
        assert data["rejected_reasons"]

    @pytest.mark.asyncio
    async def test_no_claim_surface_emits_no_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor, events = _deliver_executor()
        identity = _runtime_identity()
        load_calls = 0

        async def _fake_load(*args, **kwargs):
            nonlocal load_calls
            load_calls += 1
            return EvidenceManifest(ac_id=identity.ac_id, entries=())

        monkeypatch.setattr(pe_module, "load_ac_evidence_manifest", _fake_load)

        # No structured facts array → the common non-fat-harness case → skip silently.
        await executor._observe_deliver_verdict(
            runtime_identity=identity,
            execution_id="exec_frugal",
            session_id="sess_frugal",
            is_sub_ac=True,
            success=True,
            typed_evidence=EvidenceRecord(data={"summary": "prose only"}),
        )

        assert _deliver_events(events) == []
        assert load_calls == 0  # never even loaded a manifest — no fabrication

    @pytest.mark.asyncio
    async def test_verdict_exception_is_swallowed_observe_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor, events = _deliver_executor()
        identity = _runtime_identity()

        async def _boom(*args, **kwargs):
            raise RuntimeError("manifest load exploded")

        monkeypatch.setattr(pe_module, "load_ac_evidence_manifest", _boom)

        typed_evidence = EvidenceRecord(
            data={"observed_facts": [{"fact_id": "f1", "evidence_handle": "h1"}]},
        )
        # Must not raise, and must emit nothing — the verdict never touches the AC.
        await executor._observe_deliver_verdict(
            runtime_identity=identity,
            execution_id="exec_frugal",
            session_id="sess_frugal",
            is_sub_ac=True,
            success=True,
            typed_evidence=typed_evidence,
        )

        assert _deliver_events(events) == []

    @pytest.mark.asyncio
    async def test_unaccepted_leaf_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor, events = _deliver_executor()
        identity = _runtime_identity()
        load_calls = 0

        async def _fake_load(*args, **kwargs):
            nonlocal load_calls
            load_calls += 1
            return EvidenceManifest(ac_id=identity.ac_id, entries=())

        monkeypatch.setattr(pe_module, "load_ac_evidence_manifest", _fake_load)

        await executor._observe_deliver_verdict(
            runtime_identity=identity,
            execution_id="exec_frugal",
            session_id="sess_frugal",
            is_sub_ac=True,
            success=False,  # not accepted → no verdict
            typed_evidence=EvidenceRecord(
                data={"observed_facts": [{"fact_id": "f1", "evidence_handle": "h1"}]},
            ),
        )

        assert _deliver_events(events) == []
        assert load_calls == 0

    @pytest.mark.asyncio
    async def test_full_ac_run_result_unchanged_by_observation(self) -> None:
        # Observe-only pin at the AC level: a normal successful leaf (no execution
        # profile → no structured claim surface) completes untouched, and no
        # deliver-verdict event is produced.
        store, events = _capturing_event_store()
        executor = ParallelACExecutor(
            adapter=_ScriptedRuntime([_claude_result({"input_tokens": 5, "output_tokens": 1})]),
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await _run_one_ac(executor)

        assert result.success is True
        assert _deliver_events(events) == []


# -- Consumer: run-end proof evaluation (runner) -----------------------------
def _triad_events(run_id: str, ac_id: str, *, spend: float, baseline: float) -> list[dict]:
    return [
        {
            "type": EVENT_EFFORT_ROUTED,
            "data": {
                "ac_id": ac_id,
                "execution_id": run_id,
                "effort_level": "low",
                "effort_mode": "enforced",
                "is_decomposed_child": True,
            },
        },
        {
            "type": EVENT_TOKEN_ATTRIBUTION,
            "data": {"ac_id": ac_id, "execution_id": run_id, "token_spend": spend},
        },
        {
            "type": EVENT_DELIVER_VERDICT,
            "data": {
                "ac_id": ac_id,
                "execution_id": run_id,
                "traceguard_verdict": "accepted",
                "unsupported_claim_rate": 0.0,
                "grounding_regression": False,
            },
        },
        {
            "type": EVENT_SHADOW_REPLAY,
            "data": {
                "ac_id": ac_id,
                "execution_id": run_id,
                "baseline_token_spend": baseline,
                "decomposition_trustworthy": True,
            },
        },
    ]


def _consumer_runner(fabricated: list) -> tuple[OrchestratorRunner, list, MagicMock]:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.working_directory = "/tmp/project"
    adapter.permission_mode = "acceptEdits"
    store = AsyncMock()
    appended: list = []

    async def _append(event):
        appended.append(event)

    store.append.side_effect = _append
    store.query_execution_related_events.return_value = fabricated
    console = MagicMock()
    runner = OrchestratorRunner(adapter, store, console)
    return runner, appended, console


class TestFrugalityProofConsumer:
    @pytest.mark.asyncio
    async def test_fabricated_full_triads_pass_and_emit(self) -> None:
        fabricated: list = []
        for run in range(3):
            for ac in range(7):
                fabricated.extend(
                    _triad_events(f"run-{run}", f"ac-{run}-{ac}", spend=50, baseline=100)
                )
        runner, appended, console = _consumer_runner(fabricated)

        await runner._evaluate_frugality_proof("run-0")

        emitted = [e for e in appended if e.type == "execution.frugality_proof.evaluated"]
        assert len(emitted) == 1
        data = emitted[0].data
        assert data["status"] == ProofStatus.PASS.value
        assert data["counted_rows"] == 21
        assert data["runs"] == 3
        assert data["token_reduction_pct"] == pytest.approx(50.0)
        # Exactly one concise console line.
        console.print.assert_called_once()
        assert "Frugality proof:" in console.print.call_args.args[0]

    @pytest.mark.asyncio
    async def test_empty_events_report_insufficient_data(self) -> None:
        runner, appended, console = _consumer_runner([])

        await runner._evaluate_frugality_proof("run-empty")

        emitted = [e for e in appended if e.type == "execution.frugality_proof.evaluated"]
        assert len(emitted) == 1
        assert emitted[0].data["status"] == ProofStatus.INSUFFICIENT_DATA.value
        assert emitted[0].data["counted_rows"] == 0
        console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_query_failure_never_raises(self) -> None:
        runner, appended, _console = _consumer_runner([])
        runner._event_store.query_execution_related_events.side_effect = RuntimeError("db down")

        # Best-effort: a broken query degrades to a warning, never fails the run.
        await runner._evaluate_frugality_proof("run-x")

        assert [e for e in appended if e.type == "execution.frugality_proof.evaluated"] == []


# -- End-to-end honesty check: produced events → contract match --------------
class TestProducedEventsMatchProofContract:
    @pytest.mark.asyncio
    async def test_token_plus_effort_row_lands_but_does_not_count_without_baseline(self) -> None:
        # Run the REAL token producer, capture its event, pair it with a matching
        # effort event, and prove assemble_triads lands both axes on one row — yet
        # the row honestly does NOT count without the grounding/baseline axes.
        store, events = _capturing_event_store()
        runtime = _EnforcedModelUsageRuntime({"input_tokens": 40, "output_tokens": 10})
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            reasoning_effort="high",
        )

        await _run_one_ac(executor, is_sub_ac=True)

        token = _token_events(events)
        assert len(token) == 1
        token_event = token[0]
        ac_id = token_event.data["ac_id"]
        run_id = token_event.data["execution_id"]

        effort_event = {
            "type": EVENT_EFFORT_ROUTED,
            "data": {
                "ac_id": ac_id,
                "execution_id": run_id,
                "effort_level": "high",
                "effort_mode": "enforced",
                "is_decomposed_child": True,
            },
        }

        rows = assemble_triads([token_event, effort_event])
        assert len(rows) == 1
        row = rows[0]
        # Both produced axes landed on the same row.
        assert row.token_spend == pytest.approx(50)
        assert row.effort_level == "high"
        assert row.effort_mode == "enforced"
        assert row.is_decomposed_child is True
        # ...but grounding + baseline are absent, so the row honestly does not count.
        assert row.has_all_axes is False
        assert row.counts_in_proof is False
        # And the whole-run verdict is INSUFFICIENT_DATA, as intended pre-baseline.
        assert evaluate_proof(rows).status is ProofStatus.INSUFFICIENT_DATA


# -- Regression: the LIVE run paths must trigger the run-end proof ------------
def _mini_seed() -> Seed:
    return Seed(
        goal="Build a task management CLI",
        constraints=("Python 3.12+",),
        acceptance_criteria=("Tasks can be created",),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management ontology",
            fields=(OntologyField(name="tasks", field_type="array", description="tasks"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="completeness", description="All requirements are met"),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria satisfied",
                evaluation_criteria="100% criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.15),
    )


def _runner_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.working_directory = "/tmp/project"
    adapter.permission_mode = "acceptEdits"
    return adapter


class TestLiveRunPathsTriggerProof:
    @pytest.mark.asyncio
    async def test_parallel_completion_evaluates_proof(self) -> None:
        # The bug this pins: ``ooo run`` takes the PARALLEL path, whose terminal
        # event must be followed by a proof evaluation. Without the wiring a real
        # run produced token/effort events but never a frugality_proof.evaluated.
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        seed = _mini_seed()
        runner = OrchestratorRunner(_runner_adapter(), AsyncMock(), MagicMock())
        tracker = SessionTracker.create("exec_parallel", seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=1,
            failure_count=0,
            total_messages=1,
        )

        class _FakeParallelExecutor:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def execute_parallel(self, **kwargs: object) -> ParallelExecutionResult:
                return parallel_result

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                _FakeParallelExecutor,
            ),
            patch.object(runner, "_evaluate_frugality_proof", AsyncMock()) as proof,
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        # The proof was evaluated for THIS execution after the terminal event.
        proof.assert_awaited_once_with("exec_parallel")
