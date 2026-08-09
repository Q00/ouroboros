"""Formal-evaluation to Ralph convergence chaining tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evolution.evaluation_coverage import validate_seed_ac_coverage
from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
from ouroboros.evolution.loop_support import planned_evolve_generation
from ouroboros.evolution.reflect import ReflectOutput
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools import evaluate_ralph_chain, evaluation_handlers, evaluation_job
from ouroboros.mcp.tools.evaluate_ralph_chain import (
    ac_results_from_checklist,
    evaluation_summary_from_eval_meta,
    mint_chain_lineage_id,
)
from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.ralph_handlers import StartRalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import events_table


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


def _seed() -> Seed:
    return Seed(
        goal="Build a convergent artifact",
        acceptance_criteria=("passing AC", "failing AC"),
        ontology_schema=OntologySchema(name="Artifact", description="Artifact domain"),
        metadata=SeedMetadata(seed_id="seed-chain", ambiguity_score=0.1),
    )


def _seed_yaml(seed: Seed | None = None) -> str:
    return yaml.safe_dump((seed or _seed()).to_dict(), sort_keys=False)


def _rejected_result() -> MCPToolResult:
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="REJECTED"),),
        meta={
            "session_id": "orch-chain-1234",
            "final_approved": False,
            "highest_stage": 2,
            "multi_ac": True,
            "pass_rate": 0.5,
            "run_feedback": ["failing AC: output is missing"],
            "checklist": [
                {
                    "ac_text": "passing AC",
                    "passed": True,
                    "reasoning": "implemented",
                    "evidence": ["artifact.txt exists"],
                    "questions_used": [],
                    "failure_reason": None,
                },
                {
                    "ac_text": "failing AC",
                    "passed": False,
                    "reasoning": "output missing",
                    "evidence": [],
                    "questions_used": ["Does output exist?"],
                    "failure_reason": "not found",
                },
            ],
        },
    )


async def _wait_terminal(manager: JobManager, job_id: str):
    for _ in range(200):
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


class _StaticEvaluateHandler:
    def __init__(self, result: MCPToolResult) -> None:
        self.result = result

    async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
        return Result.ok(self.result)


async def test_rejected_evaluation_seeds_gen1_and_enqueues_ralph(
    event_store: EventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeStartRalphHandler:
        def __init__(self, **_: Any) -> None:
            pass

        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            calls.append(arguments)
            return Result.ok(MCPToolResult(meta={"job_id": "job_ralph_chain"}))

    monkeypatch.setattr(evaluation_handlers, "get_auto_evolve_enabled", lambda: True)
    monkeypatch.setattr(evaluate_ralph_chain, "get_auto_evolve_max_generations", lambda: 3)
    manager = JobManager(event_store)
    handler = StartEvaluateHandler(
        evaluate_handler=_StaticEvaluateHandler(_rejected_result()),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=manager,
        start_ralph_handler=FakeStartRalphHandler(),  # type: ignore[arg-type]
    )
    arguments = {
        "session_id": "orch-chain-1234",
        "artifact": "partial artifact",
        "seed_content": _seed_yaml(),
        "working_dir": str(tmp_path),
    }

    started = await handler.handle(arguments)
    assert started.is_ok
    snapshot = await _wait_terminal(manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.COMPLETED
    assert snapshot.result_meta["final_approved"] is False
    assert snapshot.result_meta["chained_ralph_job_id"] == "job_ralph_chain"
    lineage_id = snapshot.result_meta["chained_ralph_lineage_id"]
    assert lineage_id == mint_chain_lineage_id("seed-chain", "orch-chain-1234")
    assert snapshot.result_meta["chained_ralph_max_generations"] == 3
    assert calls == [
        {
            "lineage_id": lineage_id,
            "execute": True,
            "parallel": True,
            "skip_qa": False,
            "project_dir": str(tmp_path),
            "max_generations": 3,
        }
    ]
    events = await event_store.replay_lineage(lineage_id)
    assert [event.type for event in events] == [
        "lineage.created",
        "lineage.generation.completed",
    ]
    summary = events[-1].data["evaluation_summary"]
    assert summary["ac_results"][0]["rendered_verdict"] == "PASS"
    assert summary["ac_results"][1]["rendered_verdict"] == "FAIL"
    assert await planned_evolve_generation(event_store, lineage_id, execute=True) == 2


async def test_evaluation_enqueue_snapshots_budget_before_rejection(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeStartRalphHandler:
        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            calls.append(arguments)
            return Result.ok(MCPToolResult(meta={"job_id": "job_snapshotted_budget"}))

    monkeypatch.setattr(evaluation_handlers, "get_auto_evolve_enabled", lambda: True)
    monkeypatch.setattr(
        evaluation_job,
        "get_auto_evolve_max_generations",
        lambda: 2,
    )
    # If the chain reads live configuration after evaluation, this later value
    # would leak into the Ralph request and result metadata.
    monkeypatch.setattr(evaluate_ralph_chain, "get_auto_evolve_max_generations", lambda: 9)
    manager = JobManager(event_store)
    handler = StartEvaluateHandler(
        evaluate_handler=_StaticEvaluateHandler(_rejected_result()),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=manager,
        start_ralph_handler=FakeStartRalphHandler(),  # type: ignore[arg-type]
    )

    started = await handler.handle(
        {
            "session_id": "orch-budget-snapshot",
            "artifact": "partial artifact",
            "seed_content": _seed_yaml(),
        }
    )
    terminal = await _wait_terminal(manager, started.value.meta["job_id"])

    assert calls[0]["max_generations"] == 2
    assert terminal.result_meta["chained_ralph_max_generations"] == 2


async def test_real_rejection_to_ralph_completes_generation_two(
    event_store: EventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real production boundary that constructor-mocked tests missed."""

    seed = _seed()
    generated_seed = Seed.from_dict(
        {
            **seed.to_dict(),
            "metadata": {
                **seed.metadata.model_dump(mode="json"),
                "seed_id": "seed-chain-gen2",
                "parent_seed_id": seed.metadata.seed_id,
            },
        }
    )
    wonder_engine = MagicMock()
    wonder_engine.wonder = AsyncMock(
        return_value=Result.ok(
            WonderOutput(
                questions=("What output repairs failing AC?",),
                grounded_questions=(),
                should_continue=True,
            )
        )
    )
    reflect_engine = MagicMock()
    reflect_engine.reflect = AsyncMock(
        return_value=Result.ok(
            ReflectOutput(
                refined_goal=seed.goal,
                refined_constraints=seed.constraints,
                refined_acs=seed.acceptance_criteria,
                settled_ac_indices=(0,),
                ontology_mutations=(),
                reasoning="Repair only the failed AC",
            )
        )
    )
    seed_generator = MagicMock()
    seed_generator.generate_from_reflect = MagicMock(return_value=Result.ok(generated_seed))
    executor_calls: list[dict[str, Any]] = []
    evaluator_calls: list[str | None] = []

    async def executor(_seed: Seed, **kwargs: Any) -> str:
        executor_calls.append(kwargs)
        return "generation 2 repaired output"

    async def evaluator(_seed: Seed, output: str | None, **_: Any) -> EvaluationSummary:
        evaluator_calls.append(output)
        return EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=1.0,
            approval_status="approved",
            execution_completion_status="completed",
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="passing AC",
                    passed=True,
                    score=1.0,
                    evidence="boundary reverified",
                    verification_method="integration_test",
                    final_verdict="pass",
                    rendered_verdict="PASS",
                ),
                ACResult(
                    ac_index=1,
                    ac_content="failing AC",
                    passed=True,
                    score=1.0,
                    evidence="repaired output exists",
                    verification_method="integration_test",
                    final_verdict="pass",
                    rendered_verdict="PASS",
                ),
            ),
        )

    executor.frugality_provider_tracking = True  # type: ignore[attr-defined]
    evaluator.frugality_provider_tracking = True  # type: ignore[attr-defined]
    loop = EvolutionaryLoop(
        event_store=event_store,
        config=EvolutionaryLoopConfig(min_generations=1, outcome_gate_enabled=False),
        wonder_engine=wonder_engine,
        reflect_engine=reflect_engine,
        seed_generator=seed_generator,
        executor=executor,
        evaluator=evaluator,
    )
    manager = JobManager(event_store)
    evolve_step = EvolveStepHandler(evolutionary_loop=loop, event_store=event_store)
    start_ralph = StartRalphHandler(
        evolve_handler=evolve_step,
        event_store=event_store,
        job_manager=manager,
    )
    handler = StartEvaluateHandler(
        event_store=event_store,
        job_manager=manager,
        start_ralph_handler=start_ralph,
    )

    async def passing_qa(
        _self: QAHandler,
        _arguments: dict[str, Any],
    ) -> Result[MCPToolResult, Any]:
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="QA PASS"),),
                meta={
                    "verdict": "pass",
                    "passed": True,
                    "score": 1.0,
                    "pass_threshold": 0.8,
                },
            )
        )

    monkeypatch.setattr(QAHandler, "handle", passing_qa)
    monkeypatch.setattr(evaluate_ralph_chain, "get_auto_evolve_max_generations", lambda: 2)

    chained = await handler._enqueue_chained_ralph(
        _rejected_result(),
        session_id="orch-real-boundary",
        arguments={"seed_content": _seed_yaml(seed), "working_dir": str(tmp_path)},
    )
    ralph_job_id = chained.meta["chained_ralph_job_id"]
    terminal = await _wait_terminal(manager, ralph_job_id)

    assert terminal.status == JobStatus.COMPLETED
    assert "EvolutionaryLoop not configured" not in (terminal.result_text or "")
    assert terminal.result_meta["stop_reason"] == "qa passed"
    assert terminal.result_meta["generations"] == [2]
    assert executor_calls
    assert evaluator_calls == ["generation 2 repaired output"]
    events = await event_store.replay_lineage(chained.meta["chained_ralph_lineage_id"])
    completed = [event for event in events if event.type == "lineage.generation.completed"]
    assert [event.data["generation_number"] for event in completed] == [1, 2]
    assert completed[-1].data["active_ac_indices"] == [1]
    assert completed[-1].data["frozen_ac_indices"] == [0]


async def test_server_composition_reuses_configured_chain_handlers(
    event_store: EventStore,
    tmp_path: Path,
) -> None:
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    with (
        patch("ouroboros.orchestrator.create_agent_runtime", return_value=MagicMock()),
        patch("ouroboros.providers.create_llm_adapter", return_value=MagicMock()),
    ):
        server = create_ouroboros_server(
            event_store=event_store,
            state_dir=tmp_path / "state",
            runtime_backend="opencode",
            llm_backend="claude_code",
            opencode_mode="plugin",
        )

    start_execute = server._tool_handlers["ouroboros_start_execute_seed"]
    start_evaluate = server._tool_handlers["ouroboros_start_evaluate"]
    start_ralph = server._tool_handlers["ouroboros_start_ralph"]
    assert start_execute.start_evaluate_handler is start_evaluate
    assert start_evaluate.start_ralph_handler is not start_ralph
    assert start_ralph.opencode_mode == "plugin"
    assert start_evaluate.start_ralph_handler.opencode_mode is None
    assert start_evaluate.start_ralph_handler._evolve_handler.opencode_mode is None
    assert start_evaluate.start_ralph_handler._evolve_handler.evolutionary_loop is not None


def _builtin_runtime(
    runtime_backend: str,
    opencode_mode: str | None = None,
) -> Any:
    if runtime_backend == "codex":
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        return CodexCliRuntime(cli_path="codex")
    if runtime_backend == "hermes":
        from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

        return HermesCliRuntime(cli_path="hermes")
    if runtime_backend == "opencode":
        from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime

        return OpenCodeRuntime(cli_path="opencode", opencode_mode=opencode_mode)

    raise AssertionError(f"unsupported builtin test runtime: {runtime_backend}")


def _builtin_runtime_tools(
    runtime_backend: str,
    opencode_mode: str | None = None,
) -> tuple[Any, ...]:
    if runtime_backend in {"codex", "hermes", "opencode"}:
        runtime = _builtin_runtime(runtime_backend, opencode_mode)
        return tuple(runtime._get_builtin_mcp_handlers().values())

    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    return get_ouroboros_tools(
        runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
        include_auto=False,
    )


def test_ownerless_opencode_factory_stays_lightweight_and_side_effect_free() -> None:
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    with patch("ouroboros.mcp.server.adapter.create_ouroboros_server") as create_server:
        tools = get_ouroboros_tools(
            runtime_backend="opencode",
            opencode_mode="plugin",
            include_auto=False,
        )

    start_evaluate = next(tool for tool in tools if isinstance(tool, StartEvaluateHandler))
    create_server.assert_not_called()
    assert type(tools) is tuple
    assert start_evaluate.start_ralph_handler is None


@pytest.mark.parametrize(
    ("runtime_backend", "opencode_mode"),
    [("opencode", "plugin"), ("codex", None), ("hermes", None)],
)
def test_runtime_factory_reuses_configured_parent_owned_convergence_graph(
    runtime_backend: str,
    opencode_mode: str | None,
) -> None:
    """Runtime interceptors must not receive the old unconfigured mini graph."""
    from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler
    from ouroboros.mcp.tools.execution_handlers import (
        ExecuteSeedHandler,
        StartExecuteSeedHandler,
    )
    from ouroboros.mcp.tools.ralph_handlers import StartRalphHandler

    tools = _builtin_runtime_tools(runtime_backend, opencode_mode)
    start_evaluate = next(tool for tool in tools if isinstance(tool, StartEvaluateHandler))
    start_execute = next(tool for tool in tools if isinstance(tool, StartExecuteSeedHandler))
    execute = next(tool for tool in tools if isinstance(tool, ExecuteSeedHandler))
    public_start_ralph = next(tool for tool in tools if isinstance(tool, StartRalphHandler))
    parent_start_ralph = start_evaluate.start_ralph_handler

    assert parent_start_ralph is not None
    if runtime_backend == "opencode":
        assert parent_start_ralph is not public_start_ralph
        assert public_start_ralph.opencode_mode == "plugin"
    assert parent_start_ralph.opencode_mode is None
    assert parent_start_ralph._evolve_handler.opencode_mode is None
    assert parent_start_ralph._evolve_handler.evolutionary_loop is not None
    assert start_evaluate._event_store is parent_start_ralph._event_store
    assert start_evaluate._job_manager is parent_start_ralph._job_manager
    assert start_execute._event_store is start_evaluate._event_store
    assert execute.seed_handoff_registry is start_execute.seed_handoff_registry
    assert execute.seed_handoff_registry is start_evaluate.seed_handoff_registry


@pytest.mark.parametrize(
    ("runtime_backend", "opencode_mode"),
    [("codex", None), ("hermes", None), ("opencode", "plugin")],
)
def test_builtin_composition_reuses_canonicalized_runtime_owner(
    runtime_backend: str,
    opencode_mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handle aliases such as codex_cli must not materialize a nested runtime."""
    runtime = _builtin_runtime(runtime_backend, opencode_mode)

    def unexpected_runtime_factory(**_: Any) -> Any:
        raise AssertionError("configured composition replaced its injected runtime owner")

    monkeypatch.setattr(
        "ouroboros.orchestrator.create_agent_runtime",
        unexpected_runtime_factory,
    )

    handlers = runtime._get_builtin_mcp_handlers()

    assert handlers
    assert runtime._builtin_mcp_tool_composition is not None


@pytest.mark.parametrize(
    ("runtime_backend", "opencode_mode"),
    [("codex", None), ("hermes", None), ("opencode", "plugin")],
)
async def test_builtin_runtime_owns_and_shuts_down_composed_resources(
    runtime_backend: str,
    opencode_mode: str | None,
) -> None:
    runtime = _builtin_runtime(runtime_backend, opencode_mode)
    runtime._get_builtin_mcp_handlers()
    composition = runtime._builtin_mcp_tool_composition
    assert composition is not None
    close_order: list[str] = []

    async def drain() -> None:
        close_order.append("drain")

    async def shutdown() -> None:
        close_order.append("shutdown")

    composition.server_owner.job_manager.drain = AsyncMock(side_effect=drain)
    composition.server_owner.shutdown = AsyncMock(side_effect=shutdown)

    await runtime.aclose()
    await composition.shutdown()

    composition.server_owner.job_manager.drain.assert_awaited_once()
    composition.server_owner.shutdown.assert_awaited_once()
    assert close_order == ["drain", "shutdown"]
    assert runtime._builtin_mcp_handlers is None
    assert runtime._builtin_mcp_tool_composition is None
    assert runtime.aclose is None


async def test_builtin_runtime_preserves_composition_owner_when_shutdown_fails() -> None:
    runtime = _builtin_runtime("codex")
    handlers = runtime._get_builtin_mcp_handlers()
    composition = runtime._builtin_mcp_tool_composition
    assert composition is not None
    composition.server_owner.job_manager.drain = AsyncMock(side_effect=RuntimeError("drain"))
    composition.server_owner.shutdown = AsyncMock()

    with pytest.raises(RuntimeError, match="drain"):
        await runtime.aclose()

    composition.server_owner.shutdown.assert_not_awaited()
    assert runtime._builtin_mcp_handlers is handlers
    assert runtime._builtin_mcp_tool_composition is composition


@pytest.mark.parametrize("runtime_backend", ["codex", "hermes"])
async def test_builtin_runtime_rejection_starts_pollable_ralph_job(
    runtime_backend: str,
    tmp_path: Path,
) -> None:
    """Executing builtin factories must honor the advertised auto-evolve contract."""
    tools = _builtin_runtime_tools(runtime_backend)
    start_evaluate = next(tool for tool in tools if isinstance(tool, StartEvaluateHandler))
    parent_start_ralph = start_evaluate.start_ralph_handler
    assert parent_start_ralph is not None
    await start_evaluate._event_store.initialize()

    async def converged_generation(_: dict[str, Any]) -> Result[MCPToolResult, Any]:
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="converged"),),
                meta={
                    "lineage_id": "factory-runtime-lineage",
                    "action": "converged",
                    "generation": 2,
                    "qa": {"verdict": "pass"},
                },
            )
        )

    parent_start_ralph._evolve_handler.handle = converged_generation
    chained = await start_evaluate._enqueue_chained_ralph(
        _rejected_result(),
        session_id=f"orch-{runtime_backend}-factory",
        arguments={"seed_content": _seed_yaml(), "working_dir": str(tmp_path)},
    )

    ralph_job_id = chained.meta.get("chained_ralph_job_id")
    assert isinstance(ralph_job_id, str), (
        chained.meta.get("chained_ralph_error"),
        chained.meta.get("chained_ralph_skipped"),
    )
    terminal = await _wait_terminal(start_evaluate._job_manager, ralph_job_id)
    assert terminal.status == JobStatus.COMPLETED
    assert terminal.result_meta["stop_reason"] == "qa passed"


@pytest.mark.parametrize("meta", [{"final_approved": True}, {}])
async def test_approved_or_unjudged_evaluation_does_not_chain(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
    meta: dict[str, Any],
) -> None:
    manager = JobManager(event_store)
    handler = StartEvaluateHandler(
        evaluate_handler=_StaticEvaluateHandler(MCPToolResult(meta=meta)),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=manager,
    )
    started = await handler.handle(
        {"session_id": "orch-approved", "artifact": "artifact", "seed_content": _seed_yaml()}
    )
    snapshot = await _wait_terminal(manager, started.value.meta["job_id"])
    assert "chained_ralph_job_id" not in snapshot.result_meta


@pytest.mark.parametrize(
    ("config_enabled", "override"),
    [(False, None), (True, False)],
)
async def test_auto_evolve_opt_out_matrix(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
    config_enabled: bool,
    override: bool | None,
) -> None:
    monkeypatch.setattr(
        evaluation_handlers,
        "get_auto_evolve_enabled",
        lambda: config_enabled,
    )
    manager = JobManager(event_store)
    handler = StartEvaluateHandler(
        evaluate_handler=_StaticEvaluateHandler(_rejected_result()),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=manager,
    )
    arguments: dict[str, Any] = {
        "session_id": "orch-optout",
        "artifact": "artifact",
        "seed_content": _seed_yaml(),
    }
    if override is not None:
        arguments["auto_evolve"] = override
    started = await handler.handle(arguments)
    snapshot = await _wait_terminal(manager, started.value.meta["job_id"])
    assert "chained_ralph_job_id" not in snapshot.result_meta
    lineage_id = mint_chain_lineage_id("seed-chain", "orch-optout")
    assert await event_store.replay_lineage(lineage_id) == []


async def test_seed_unavailable_skips_chain_fail_open(event_store: EventStore) -> None:
    manager = JobManager(event_store)
    handler = StartEvaluateHandler(event_store=event_store, job_manager=manager)

    result = await handler._enqueue_chained_ralph(
        _rejected_result(),
        session_id="orch-no-seed",
        arguments={"seed_content": "not: [valid"},
    )

    assert result.meta["final_approved"] is False
    assert result.meta["chained_ralph_skipped"] == "seed_unavailable"
    assert "chained_ralph_job_id" not in result.meta


async def test_enqueue_error_keeps_rejection_and_gen1_snapshot(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRalph:
        def __init__(self, **_: Any) -> None:
            pass

        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            raise RuntimeError("ralph enqueue exploded")

    handler = StartEvaluateHandler(
        event_store=event_store,
        job_manager=JobManager(event_store),
        start_ralph_handler=FailingRalph(),  # type: ignore[arg-type]
    )
    result = await handler._enqueue_chained_ralph(
        _rejected_result(),
        session_id="orch-error",
        arguments={"seed_content": _seed_yaml()},
    )

    assert result.meta["final_approved"] is False
    assert result.meta["chained_ralph_error"] == "ralph enqueue exploded"
    lineage_id = mint_chain_lineage_id("seed-chain", "orch-error")
    assert len(await event_store.replay_lineage(lineage_id)) == 2


async def test_gen1_seed_is_idempotent_and_active_job_is_reused(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager(event_store)
    monkeypatch.setattr(
        manager,
        "find_active_job_by_lineage",
        lambda *_args, **_kwargs: _async_value(SimpleNamespace(job_id="job_existing")),
    )
    handler = StartEvaluateHandler(event_store=event_store, job_manager=manager)
    arguments = {"seed_content": _seed_yaml()}

    first = await handler._enqueue_chained_ralph(
        _rejected_result(), session_id="orch-reuse", arguments=arguments
    )
    second = await handler._enqueue_chained_ralph(
        _rejected_result(), session_id="orch-reuse", arguments=arguments
    )

    assert first.meta["chained_ralph_job_id"] == "job_existing"
    assert second.meta["chained_ralph_job_id"] == "job_existing"
    lineage_id = mint_chain_lineage_id("seed-chain", "orch-reuse")
    events = await event_store.replay_lineage(lineage_id)
    assert [event.type for event in events] == [
        "lineage.created",
        "lineage.generation.completed",
    ]


async def test_gen1_batch_failure_leaves_no_partial_lineage_and_retry_repairs(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed()
    summary = evaluation_summary_from_eval_meta(seed, _rejected_result().meta)
    lineage_id = mint_chain_lineage_id(seed.metadata.seed_id, "orch-atomic-gen1")
    original_append_batch = event_store.append_batch
    attempts = 0

    async def _fail_first_batch(events):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated atomic batch failure")
        await original_append_batch(events)

    monkeypatch.setattr(event_store, "append_batch", _fail_first_batch)

    with pytest.raises(RuntimeError, match="simulated atomic batch failure"):
        await evaluate_ralph_chain.seed_gen1_lineage(
            event_store,
            lineage_id=lineage_id,
            seed=seed,
            summary=summary,
        )
    assert await event_store.replay_lineage(lineage_id) == []

    assert await evaluate_ralph_chain.seed_gen1_lineage(
        event_store,
        lineage_id=lineage_id,
        seed=seed,
        summary=summary,
    )
    assert [event.type for event in await event_store.replay_lineage(lineage_id)] == [
        "lineage.created",
        "lineage.generation.completed",
    ]


async def test_concurrent_gen1_projection_publishes_one_complete_pair(
    event_store: EventStore,
) -> None:
    seed = _seed()
    summary = evaluation_summary_from_eval_meta(seed, _rejected_result().meta)
    lineage_id = mint_chain_lineage_id(seed.metadata.seed_id, "orch-concurrent-gen1")

    await asyncio.gather(
        *(
            evaluate_ralph_chain.seed_gen1_lineage(
                event_store,
                lineage_id=lineage_id,
                seed=seed,
                summary=summary,
            )
            for _ in range(4)
        )
    )

    assert [event.type for event in await event_store.replay_lineage(lineage_id)] == [
        "lineage.created",
        "lineage.generation.completed",
    ]


async def test_concurrent_gen1_with_different_evidence_replays_first_winner(
    event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed()
    summary = evaluation_summary_from_eval_meta(seed, _rejected_result().meta)
    alternate = summary.model_copy(update={"failure_reason": "alternate evaluator wording"})
    lineage_id = mint_chain_lineage_id(seed.metadata.seed_id, "orch-competing-gen1")
    original_append_batch = event_store.append_batch

    async def _slow_append_batch(events):
        await asyncio.sleep(0.05)
        await original_append_batch(events)

    monkeypatch.setattr(event_store, "append_batch", _slow_append_batch)

    results = await asyncio.gather(
        evaluate_ralph_chain.seed_gen1_lineage(
            event_store,
            lineage_id=lineage_id,
            seed=seed,
            summary=summary,
        ),
        evaluate_ralph_chain.seed_gen1_lineage(
            event_store,
            lineage_id=lineage_id,
            seed=seed,
            summary=alternate,
        ),
    )

    assert sorted(results) == [False, True]
    assert [event.type for event in await event_store.replay_lineage(lineage_id)] == [
        "lineage.created",
        "lineage.generation.completed",
    ]


async def test_legacy_creation_only_lineage_is_completed_under_claim(
    event_store: EventStore,
) -> None:
    seed = _seed()
    summary = evaluation_summary_from_eval_meta(seed, _rejected_result().meta)
    lineage_id = mint_chain_lineage_id(seed.metadata.seed_id, "orch-partial-gen1")
    await event_store.append(evaluate_ralph_chain.lineage_created(lineage_id, seed.goal))

    assert await evaluate_ralph_chain.seed_gen1_lineage(
        event_store,
        lineage_id=lineage_id,
        seed=seed,
        summary=summary,
    )
    assert [event.type for event in await event_store.replay_lineage(lineage_id)] == [
        "lineage.created",
        "lineage.generation.completed",
    ]


async def test_concurrent_ralph_enqueue_has_one_durable_winner(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'ralph-successor.db'}"
    stores = [EventStore(database_url), EventStore(database_url)]
    for store in stores:
        await store.initialize()

    class EmptyJobManager:
        async def find_active_job_by_lineage(
            self,
            lineage_id: str,
            *,
            job_type: str | None = None,
            include_terminal: bool = False,
        ) -> None:
            assert lineage_id == "lineage-concurrent-successor"
            assert job_type == "ralph"
            assert include_terminal is True
            return None

    class SlowRalphStarter:
        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            self.calls += 1
            await asyncio.sleep(0.05)
            return Result.ok(MCPToolResult(meta={"job_id": "job_single_winner"}))

    starter = SlowRalphStarter()
    try:
        job_ids = await asyncio.gather(
            *(
                evaluate_ralph_chain._claim_or_start_chained_ralph(
                    lineage_id="lineage-concurrent-successor",
                    arguments={},
                    max_generations=3,
                    event_store=store,
                    job_manager=EmptyJobManager(),
                    start_ralph_handler=starter,
                )
                for store in stores
            )
        )
    finally:
        for store in stores:
            await store.close()

    assert [receipt.job_id for receipt in job_ids] == [
        "job_single_winner",
        "job_single_winner",
    ]
    assert [receipt.max_generations for receipt in job_ids] == [3, 3]
    assert starter.calls == 1


async def test_terminal_ralph_job_closes_successor_crash_gap(
    event_store: EventStore,
) -> None:
    calls: list[bool] = []

    class TerminalJobManager:
        async def find_active_job_by_lineage(
            self,
            _lineage_id: str,
            *,
            job_type: str | None = None,
            include_terminal: bool = False,
        ) -> SimpleNamespace:
            assert job_type == "ralph"
            calls.append(include_terminal)
            return SimpleNamespace(job_id="job_already_terminal")

    class NeverStartRalph:
        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            raise AssertionError("terminal Ralph successor must be reattached")

    receipt = await evaluate_ralph_chain._claim_or_start_chained_ralph(
        lineage_id="lineage-terminal-successor",
        arguments={},
        max_generations=3,
        event_store=event_store,
        job_manager=TerminalJobManager(),
        start_ralph_handler=NeverStartRalph(),
    )

    assert receipt.job_id == "job_already_terminal"
    assert receipt.max_generations == 3
    assert calls == [True]


async def test_ralph_budget_receipt_survives_later_config_change(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'ralph-budget.db'}")
    await store.initialize()

    class EmptyJobManager:
        async def find_active_job_by_lineage(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class OneRalphStarter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            self.calls.append(arguments)
            return Result.ok(MCPToolResult(meta={"job_id": "job_budget_owner"}))

    starter = OneRalphStarter()
    try:
        first = await evaluate_ralph_chain._claim_or_start_chained_ralph(
            lineage_id="lineage-budget-owner",
            arguments={},
            max_generations=2,
            event_store=store,
            job_manager=EmptyJobManager(),
            start_ralph_handler=starter,
        )
        replay = await evaluate_ralph_chain._claim_or_start_chained_ralph(
            lineage_id="lineage-budget-owner",
            arguments={},
            max_generations=7,
            event_store=store,
            job_manager=EmptyJobManager(),
            start_ralph_handler=starter,
        )
    finally:
        await store.close()

    assert first == replay
    assert replay.max_generations == 2
    assert starter.calls[0]["max_generations"] == 2
    assert len(starter.calls) == 1


async def _async_value(value: Any) -> Any:
    return value


def test_checklist_conversion_preserves_seed_coverage_and_not_evaluated_shape() -> None:
    seed = _seed()
    checklist = _rejected_result().meta["checklist"][:1]
    results = ac_results_from_checklist(seed, checklist)
    summary = evaluation_summary_from_eval_meta(
        seed, {"final_approved": False, "checklist": checklist}
    )

    assert len(results) == 2
    assert results[1].ac_content == "failing AC"
    assert results[1].ac_verdict_state == "not_evaluated"
    assert results[1].rendered_verdict == "NOT_EVALUATED"
    assert validate_seed_ac_coverage(seed, summary, require_authoritative=False).complete is True


def test_single_ac_without_checklist_degrades_to_empty_ac_results() -> None:
    seed = Seed(
        goal="single",
        acceptance_criteria=("only AC",),
        ontology_schema=OntologySchema(name="Single", description="Single domain"),
        metadata=SeedMetadata(seed_id="single", ambiguity_score=0.1),
    )
    assert ac_results_from_checklist(seed, None) == ()
    lineage_id = mint_chain_lineage_id("single", "orch-123456")
    assert lineage_id.startswith("ralph-")
    assert len(lineage_id) == 36


def test_lineage_identity_fits_production_aggregate_id_width() -> None:
    lineage_id = mint_chain_lineage_id("seed_2be2907edc07", "orch_abc111111111")
    aggregate_id_width = events_table.c.aggregate_id.type.length

    assert aggregate_id_width == 36
    assert len(lineage_id) <= aggregate_id_width
    assert lineage_id == mint_chain_lineage_id("seed_2be2907edc07", "orch_abc111111111")


def test_lineage_identity_uses_the_complete_session_id() -> None:
    first = mint_chain_lineage_id("same-seed", "orch_abc111111111")
    second = mint_chain_lineage_id("same-seed", "orch_abc222222222")

    assert first != second
    assert first == mint_chain_lineage_id("same-seed", "orch_abc111111111")


@pytest.mark.parametrize("highest_stage", [1, 3])
def test_gen1_summary_preserves_formal_evaluation_stage(highest_stage: int) -> None:
    seed = _seed()
    meta = {**_rejected_result().meta, "highest_stage": highest_stage}

    summary = evaluation_summary_from_eval_meta(seed, meta)

    assert summary.highest_stage_passed == highest_stage


def test_gen1_summary_missing_stage_falls_back_conservatively() -> None:
    seed = _seed()
    meta = {key: value for key, value in _rejected_result().meta.items() if key != "highest_stage"}

    summary = evaluation_summary_from_eval_meta(seed, meta)

    assert summary.highest_stage_passed == 1
