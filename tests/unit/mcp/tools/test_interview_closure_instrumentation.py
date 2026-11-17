"""Interview closure fields emitted by the MCP interview and seed handlers.

``interview.started`` carries the calling surface, ``interview.response.recorded``
who answered, ``interview.response.emitted`` / ``interview.completed`` the closure
gate snapshot, and a forced Seed emits ``interview.seed_forced``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.runtime_routing import AutoStageRuntimePlan, StageRuntime
from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.interview_closure import (
    InterviewSurface,
    current_interview_surface,
    use_interview_surface,
)
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.host_context import from_sdk_context, use_mcp_host_context
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.auto_handler import AutoHandler

FIRST_QUESTION = "What is the primary user persona?"


@dataclass(slots=True)
class _CapturingEventStore:
    events: list[BaseEvent] = field(default_factory=list)

    is_initialized = True

    async def initialize(self) -> None:
        return None

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def append_durable(self, event: BaseEvent, *, timeout: float) -> None:
        assert 0 < timeout <= 5
        self.events.append(event)

    def of(self, event_type: str) -> list[BaseEvent]:
        return [event for event in self.events if event.type == event_type]


@dataclass(slots=True)
class _StubEngine:
    state_dir: Path
    state: InterviewState | None = None
    next_question: str = FIRST_QUESTION

    async def start_interview(
        self, initial_context: str, cwd: str | None = None, interview_id: str | None = None
    ) -> Result[InterviewState, Any]:
        self.state = InterviewState(
            interview_id=interview_id or "interview_closure00000001",
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        return Result.ok(self.state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, Any]:
        return Result.ok(self.next_question)

    async def record_response(
        self, state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, Any]:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        return Result.ok(state)

    async def save_state(self, state: InterviewState) -> Result[Path, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text("{}", encoding="utf-8")
        return Result.ok(path)

    async def load_state(self, session_id: str) -> Result[InterviewState, Any]:
        assert self.state is not None
        return Result.ok(self.state)

    async def complete_interview(self, state: InterviewState) -> Result[InterviewState, Any]:
        state.status = InterviewStatus.COMPLETED
        return Result.ok(state)


def _sdk_context(name: str | None) -> SimpleNamespace:
    client_info = SimpleNamespace(name=name) if name is not None else None
    params = SimpleNamespace(client_info=client_info) if client_info is not None else None
    return SimpleNamespace(
        session=SimpleNamespace(
            client_params=params,
            client_capabilities=SimpleNamespace(extensions=None, experimental=None),
        )
    )


def _handler(tmp_path: Path, engine: _StubEngine, store: _CapturingEventStore) -> InterviewHandler:
    return InterviewHandler(
        interview_engine=engine,  # type: ignore[arg-type]
        event_store=store,  # type: ignore[arg-type]
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )


async def _drain(handler: InterviewHandler) -> None:
    if handler._bg_tasks:
        await asyncio.gather(*handler._bg_tasks, return_exceptions=True)


def _score(overall: float, clarity: float) -> AmbiguityScore:
    def component(name: str) -> ComponentScore:
        return ComponentScore(name=name, clarity_score=clarity, weight=0.3, justification="why")

    return AmbiguityScore(
        overall_score=overall,
        breakdown=ScoreBreakdown(
            goal_clarity=component("Goal Clarity"),
            constraint_clarity=component("Constraint Clarity"),
            success_criteria_clarity=component("Success Criteria Clarity"),
        ),
    )


def _pending_state(answered: int, *, streak: int = 0) -> InterviewState:
    state = InterviewState(
        interview_id="interview_closure00000002",
        initial_context="ctx",
        status=InterviewStatus.IN_PROGRESS,
        completion_candidate_streak=streak,
    )
    for index in range(1, answered + 1):
        state.rounds.append(
            InterviewRound(round_number=index, question=f"Q{index}?", user_response=f"A{index}")
        )
    state.rounds.append(
        InterviewRound(round_number=answered + 1, question="Pending?", user_response=None)
    )
    return state


# --- interview.started: calling surface --------------------------------------


async def _start(tmp_path: Path) -> _CapturingEventStore:
    store = _CapturingEventStore()
    handler = _handler(tmp_path, _StubEngine(state_dir=tmp_path), store)
    outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})
    assert outcome.is_ok
    await _drain(handler)
    return store


async def test_direct_start_without_a_caller_label_is_unknown(tmp_path: Path) -> None:
    store = await _start(tmp_path)

    (started,) = store.of("interview.started")
    assert started.data["surface"] == "unknown"
    assert "host" not in started.data
    assert started.data["initial_context"] == "Build a CLI"


@pytest.mark.parametrize(
    ("client_name", "host"),
    [("claude-code", "claude_code"), ("codex", "codex"), (None, "unknown")],
)
async def test_start_inside_an_mcp_request_records_mcp_and_the_host(
    tmp_path: Path, client_name: str | None, host: str
) -> None:
    with use_mcp_host_context(from_sdk_context(_sdk_context(client_name))):
        store = await _start(tmp_path)

    (started,) = store.of("interview.started")
    assert (started.data["surface"], started.data["host"]) == ("mcp", host)


@pytest.mark.parametrize("surface", [InterviewSurface.CONSOLE, InterviewSurface.AUTO])
async def test_start_under_a_caller_label_records_that_surface(
    tmp_path: Path, surface: InterviewSurface
) -> None:
    with use_interview_surface(surface):
        store = await _start(tmp_path)

    (started,) = store.of("interview.started")
    assert started.data["surface"] == surface.value
    assert "host" not in started.data


async def test_start_response_carries_an_unscored_closure_snapshot(tmp_path: Path) -> None:
    store = await _start(tmp_path)

    (emitted,) = store.of("interview.response.emitted")
    assert emitted.data["response_kind"] == "start"
    assert emitted.data["closure"]["verdict"] == "unscored"
    assert emitted.data["closure"]["answered_rounds"] == 0


def _batch_proposal() -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "presentation": {
                        "question": question,
                        "choices": ["First", "Second"],
                        "direct_input_label": "Type another answer",
                        "multi_select": False,
                    },
                    "dimension": dimension,
                    "recommended_index": 1,
                    "depends_on": [],
                }
                for question, dimension in (
                    ("Who uses it?", "goal"),
                    ("What size?", "constraint"),
                    ("How to measure success?", "success"),
                )
            ]
        }
    )


async def test_batch_start_records_the_surface(tmp_path: Path) -> None:
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_batch_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    store = _CapturingEventStore()
    handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,  # type: ignore[arg-type]
        event_store=store,  # type: ignore[arg-type]
        data_dir=tmp_path,
    )
    handler._create_interview_engine = lambda: (engine, adapter)  # type: ignore[method-assign]

    with use_mcp_host_context(from_sdk_context(_sdk_context("claude-code"))):
        outcome = await handler.handle(
            {"initial_context": "Build a tool", "structured_first_batch": True}
        )
    assert outcome.is_ok, outcome
    await _drain(handler)

    (started,) = store.of("interview.started")
    assert (started.data["surface"], started.data["host"]) == ("mcp", "claude_code")


async def test_sdk_client_identity_reaches_interview_started(tmp_path: Path) -> None:
    """End to end: the clientInfo a host sends at initialize labels the interview."""
    from mcp import Client
    from mcp.server import MCPServer
    from mcp.types import Implementation

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    store = _CapturingEventStore()
    handler = _handler(tmp_path, _StubEngine(state_dir=tmp_path), store)
    server = MCPServerAdapter(name="closure-surface")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(
        server._mcp_server,
        mode="auto",
        client_info=Implementation(name="claude-code", version="9.9.9"),
    ) as client:
        result = await client.call_tool(
            "ouroboros_interview", {"initial_context": "Build a CLI", "cwd": str(tmp_path)}
        )
    assert not result.is_error
    await _drain(handler)

    (started,) = store.of("interview.started")
    assert (started.data["surface"], started.data["host"]) == ("mcp", "claude_code")
    assert "9.9.9" not in json.dumps(started.data)


# --- interview.response.recorded: who answered -------------------------------


async def _answer(
    tmp_path: Path, answer: str, *, extra: dict[str, Any] | None = None
) -> _CapturingEventStore:
    store = _CapturingEventStore()
    engine = _StubEngine(state_dir=tmp_path, state=_pending_state(0))
    handler = _handler(tmp_path, engine, store)
    outcome = await handler.handle(
        {"session_id": "interview_closure00000002", "answer": answer, **(extra or {})}
    )
    assert outcome.is_ok, outcome
    await _drain(handler)
    return store


@pytest.mark.parametrize(
    ("answer", "extra", "expected"),
    [
        ("[from-code][auto-confirmed] Python 3.12 (pyproject.toml)", None, "auto_confirmed"),
        ("[from-code] FastAPI", None, "from_code"),
        ("[from-research] 100 req/s", None, "from_research"),
        ("[from-user] Analysts", None, "from_user"),
        ("[from-auto] conservative default", None, "from_auto"),
        ("It creates reports.", {"answer_provenance": "repo_fact"}, "from_code"),
        ("It creates reports.", None, "unknown"),
    ],
)
async def test_recorded_answer_carries_its_source(
    tmp_path: Path, answer: str, extra: dict[str, Any] | None, expected: str
) -> None:
    store = await _answer(tmp_path, answer, extra=extra)

    (recorded,) = store.of("interview.response.recorded")
    assert recorded.data["answer_source"] == expected
    assert recorded.data["round_number"] == 1


async def test_console_answer_without_a_prefix_is_from_user(tmp_path: Path) -> None:
    with use_interview_surface(InterviewSurface.CONSOLE):
        store = await _answer(tmp_path, "It creates reports.")

    (recorded,) = store.of("interview.response.recorded")
    assert recorded.data["answer_source"] == "from_user"


# --- closure snapshots ------------------------------------------------------


async def test_scored_answer_emits_the_gate_snapshot(tmp_path: Path) -> None:
    store = _CapturingEventStore()
    engine = _StubEngine(state_dir=tmp_path, state=_pending_state(2), next_question="Next?")
    handler = _handler(tmp_path, engine, store)
    handler._score_interview_state = AsyncMock(return_value=_score(0.43, 0.6))  # type: ignore[method-assign]

    outcome = await handler.handle(
        {"session_id": "interview_closure00000002", "answer": "[from-user] Runs locally."}
    )
    assert outcome.is_ok
    await _drain(handler)

    (emitted,) = store.of("interview.response.emitted")
    closure = emitted.data["closure"]
    assert emitted.data["round_number"] == 4
    assert closure["answered_rounds"] == 3
    assert closure["ambiguity_score"] == 0.43
    assert closure["clarity"] == {
        "goal_clarity": 0.6,
        "constraint_clarity": 0.6,
        "success_criteria_clarity": 0.6,
    }
    assert closure["floors_passed"] == {
        "goal_clarity": False,
        "constraint_clarity": False,
        "success_criteria_clarity": False,
    }
    assert closure["verdict"] == "above_threshold"
    assert "why" not in str(emitted.data)


async def test_answer_that_meets_the_gate_records_the_passing_snapshot(tmp_path: Path) -> None:
    store = _CapturingEventStore()
    engine = _StubEngine(state_dir=tmp_path, state=_pending_state(2, streak=1))
    handler = _handler(tmp_path, engine, store)
    ready = _score(0.1, 0.9)

    async def score(_adapter: Any, state: InterviewState, **_kwargs: Any) -> AmbiguityScore:
        InterviewHandler._apply_interview_score(state, ready)
        return ready

    handler._score_interview_state = score  # type: ignore[method-assign]

    outcome = await handler.handle(
        {"session_id": "interview_closure00000002", "answer": "[from-user] Done deal."}
    )
    assert outcome.is_ok
    assert outcome.value.meta["completed"] is True
    await _drain(handler)

    (completed,) = store.of("interview.completed")
    assert completed.data["total_rounds"] == 3
    assert completed.data["closure"]["verdict"] == "passed"
    assert completed.data["closure"]["streak"] == 2
    assert store.of("interview.response.emitted") == []


async def test_done_with_a_stored_ready_score_closes_with_passed(tmp_path: Path) -> None:
    state = _pending_state(3, streak=2)
    ready = _score(0.1, 0.9)
    state.store_ambiguity(score=ready.overall_score, breakdown=ready.breakdown.model_dump())
    store = _CapturingEventStore()
    handler = _handler(tmp_path, _StubEngine(state_dir=tmp_path, state=state), store)

    outcome = await handler.handle({"session_id": state.interview_id, "answer": "done"})
    assert outcome.is_ok
    await _drain(handler)

    (completed,) = store.of("interview.completed")
    assert completed.data["closure"]["verdict"] == "passed"
    assert completed.data["closure"]["answered_rounds"] == 3


async def test_safe_default_synthesis_close_is_labeled(tmp_path: Path) -> None:
    state = _pending_state(3)
    store = _CapturingEventStore()
    handler = _handler(tmp_path, _StubEngine(state_dir=tmp_path, state=state), store)
    handler._score_interview_state = AsyncMock(return_value=None)  # type: ignore[method-assign]

    outcome = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": (
                "[from-auto][safe-default-synthesis] Mark the interview complete and hand "
                "off for seed generation. Auto safe-default synthesis."
            ),
        }
    )
    assert outcome.is_ok
    await _drain(handler)

    (completed,) = store.of("interview.completed")
    assert completed.data["closure"]["verdict"] == "safe_default"


# --- interview.seed_forced ---------------------------------------------------


class _EngineForSeed:
    def __init__(self, state: InterviewState) -> None:
        self._state = state

    async def load_state(self, session_id: str) -> Result[InterviewState, Any]:
        return Result.ok(self._state)

    async def save_state(self, state: InterviewState) -> Result[None, Any]:
        return Result.ok(None)


class _SeedGenerator:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    async def generate(self, state: InterviewState, score: AmbiguityScore, **_: Any) -> Result:
        if not self.ok:
            from ouroboros.core.errors import ValidationError

            return Result.err(ValidationError("generation failed", field="seed"))
        return Result.ok(
            SimpleNamespace(
                metadata=SimpleNamespace(
                    seed_id="seed-1", interview_id=state.interview_id, ambiguity_score=0.6
                ),
                goal="goal",
                to_dict=lambda: {"goal": "goal"},
            )
        )


def _seed_state() -> InterviewState:
    state = _pending_state(4)
    high = _score(0.6, 0.5)
    state.store_ambiguity(score=high.overall_score, breakdown=high.breakdown.model_dump())
    return state


async def _generate(arguments: dict[str, Any], *, ok: bool = True) -> _CapturingEventStore:
    store = _CapturingEventStore()
    handler = GenerateSeedHandler(
        interview_engine=_EngineForSeed(_seed_state()),  # type: ignore[arg-type]
        seed_generator=_SeedGenerator(ok=ok),  # type: ignore[arg-type]
        llm_adapter=object(),  # type: ignore[arg-type]
        event_store=store,  # type: ignore[arg-type]
    )
    await handler.handle({"session_id": "interview_closure00000002", **arguments})
    return store


async def test_forced_seed_records_seed_forced_with_the_gate_snapshot() -> None:
    store = await _generate({"force": True})

    (forced,) = store.of("interview.seed_forced")
    assert forced.aggregate_id == "interview_closure00000002"
    assert forced.data["answered_rounds"] == 4
    assert forced.data["interview_completed"] is False
    assert forced.data["closure"]["verdict"] == "above_threshold"
    assert forced.data["closure"]["ambiguity_score"] == 0.6


async def test_unforced_or_failed_seed_records_nothing() -> None:
    assert (await _generate({})).events == []
    assert (await _generate({"force": True}, ok=False)).events == []


async def test_forced_seed_without_an_event_store_uses_the_default_store() -> None:
    from ouroboros.observability.interview_closure_summary import load_interview_events
    from ouroboros.persistence.event_store import EventStore

    handler = GenerateSeedHandler(
        interview_engine=_EngineForSeed(_seed_state()),  # type: ignore[arg-type]
        seed_generator=_SeedGenerator(),  # type: ignore[arg-type]
        llm_adapter=object(),  # type: ignore[arg-type]
    )
    result = await handler.handle({"session_id": "interview_closure00000002", "force": True})
    assert result.is_ok

    store = EventStore()
    try:
        await store.initialize()
        events = await load_interview_events(store)
    finally:
        await store.close()
    assert [event.type for event in events] == ["interview.seed_forced"]


# --- nothing leaves the machine ---------------------------------------------


async def test_instrumented_turns_send_no_closure_data_to_telemetry(tmp_path: Path) -> None:
    """Telemetry still sees only what it saw before: no interview event, no new field."""
    capture = MagicMock()
    with patch("ouroboros.telemetry.capture", capture):
        with use_mcp_host_context(from_sdk_context(_sdk_context("claude-code"))):
            await _start(tmp_path / "start")
            await _answer(tmp_path / "answer", "[from-user] yes")
        await _generate({"force": True})

    new_fields = {"surface", "host", "answer_source", "closure", "verdict", "clarity"}
    for call in capture.call_args_list:
        event, properties = call.args[0], call.args[1] if len(call.args) > 1 else {}
        assert not event.startswith("interview"), call
        assert not new_fields & set(properties or {}), call


# --- ooo auto (MCP) ------------------------------------------------------------


def test_mcp_auto_pipeline_labels_its_interviews_auto(tmp_path: Path) -> None:
    store = _CapturingEventStore()
    supplied = InterviewHandler(
        interview_engine=_StubEngine(state_dir=tmp_path),  # type: ignore[arg-type]
        event_store=store,  # type: ignore[arg-type]
        llm_backend="claude",
        data_dir=tmp_path,
    )
    handler = AutoHandler(
        interview_handler=supplied,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    seen: dict[str, Any] = {}

    async def run_one_interview_turn(self: Any, state: Any) -> AutoPipelineResult:
        seen["surface"] = current_interview_surface()
        backend = self.interview_driver.backend
        await backend.start("Build a CLI", cwd=str(tmp_path))
        await backend.handler._drain_bg_tasks()
        return AutoPipelineResult(
            status="complete", auto_session_id=state.auto_session_id, phase="complete"
        )

    stage = StageRuntime("opencode", "plugin")
    with (
        patch("ouroboros.mcp.tools.auto_handler.build_repo_investigator", MagicMock()),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter", return_value=MagicMock()
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.resolve_auto_stage_runtime_plan",
            return_value=AutoStageRuntimePlan(
                default=stage, interview=stage, execute=stage, evaluate=stage, reflect=stage
            ),
        ),
        patch("ouroboros.mcp.tools.auto_handler.AutoPipeline.run", new=run_one_interview_turn),
        use_mcp_host_context(from_sdk_context(_sdk_context("claude-code"))),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build a CLI",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert seen["surface"] is InterviewSurface.AUTO
    (started,) = store.of("interview.started")
    assert (started.data["surface"], started.data["host"]) == ("auto", "claude_code")
    assert current_interview_surface() is None
