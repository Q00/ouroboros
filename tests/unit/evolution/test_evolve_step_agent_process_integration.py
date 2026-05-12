"""Integration tests for AgentProcess wrapping of EvolutionaryLoop.evolve_step.

Issue #518 — slice 5 of M6. Pins three contracts:
1. Happy path: spawn-to-complete emits AgentProcess CONTINUE then CONVERGE directives.
2. Cooperative cancel: caller cancels the handle; loop exits, emits CANCEL.
3. Regression guard: existing lineage.* events (generation.started / .completed)
   still flow unchanged through the AgentProcess wrapper.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    OntologyLineage,
    OntologySchema,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_created, lineage_generation_interrupted
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    GenerationResult,
    StepAction,
)
from ouroboros.orchestrator.agent_process import AgentProcess, AgentProcessStatus


class _FakeEventStore:
    """Minimal in-memory EventStore sufficient for evolve_step tests."""

    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []
        self._lineage_events: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)
        self._lineage_events.append(event)

    async def replay_lineage(self, lineage_id: str) -> list[BaseEvent]:
        return []

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            e
            for e in self.appended
            if getattr(e, "aggregate_type", None) == aggregate_type
            and getattr(e, "aggregate_id", None) == aggregate_id
        ]


def _make_seed(seed_id: str = "seed-1") -> Seed:
    """Build a minimal Seed for testing."""
    ontology = OntologySchema(name="test", description="test ontology", fields=[])
    seed = MagicMock(spec=Seed)
    seed.goal = "test goal"
    seed.metadata = MagicMock()
    seed.metadata.seed_id = seed_id
    seed.metadata.parent_seed_id = None
    seed.ontology_schema = ontology
    seed.to_dict.return_value = {"seed_id": seed_id}
    return seed


def _make_generation_result(
    generation_number: int = 1,
    seed: Seed | None = None,
    phase: GenerationPhase = GenerationPhase.COMPLETED,
) -> GenerationResult:
    """Build a minimal GenerationResult for testing."""
    if seed is None:
        seed = _make_seed()
    wonder_output = MagicMock()
    wonder_output.questions = ()
    return GenerationResult(
        generation_number=generation_number,
        seed=seed,
        execution_output="ok",
        evaluation_summary=EvaluationSummary(
            score=0.8,
            final_approved=True,
            highest_stage_passed=1,
        ),
        wonder_output=wonder_output,
        phase=phase,
        success=phase == GenerationPhase.COMPLETED,
    )


async def _wait_for_agent_status(handle: Any, status: AgentProcessStatus) -> None:
    for _ in range(100):
        if handle.status() is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"AgentProcess status did not become {status}")


def _build_loop(
    event_store: _FakeEventStore,
    gen_result: GenerationResult | None = None,
) -> EvolutionaryLoop:
    """Build an EvolutionaryLoop with mocked generation execution."""
    config = EvolutionaryLoopConfig(
        max_generations=10,
        convergence_threshold=0.95,
        min_generations=1,
    )
    loop = EvolutionaryLoop(event_store=event_store, config=config)

    # Stub _run_generation_with_watchdog to return a fixed result
    if gen_result is None:
        gen_result = _make_generation_result()

    async def _fake_run_generation_with_watchdog(**kwargs: Any) -> Result[GenerationResult, Any]:
        return Result.ok(gen_result)

    loop._run_generation_with_watchdog = _fake_run_generation_with_watchdog  # type: ignore[method-assign]
    return loop


# ---------------------------------------------------------------------------
# Test 1: happy path — CONTINUE then CONVERGE directives on the agent_process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolve_step_emits_running_then_completed_directives() -> None:
    """spawn-to-complete happy path emits AgentProcess CONTINUE then CONVERGE."""
    store = _FakeEventStore()
    loop = _build_loop(store)
    seed = _make_seed()

    result = await loop.evolve_step(lineage_id="lin-test-1", initial_seed=seed)

    assert result.is_ok
    step = result.value
    assert step.action in (StepAction.CONTINUE, StepAction.CONVERGED, StepAction.EXHAUSTED)

    # Collect agent_process directive events
    ap_directives = [
        e.data["directive"]
        for e in store.appended
        if e.type == "control.directive.emitted"
        and getattr(e, "aggregate_type", None) == "agent_process"
    ]

    # Must have at least: initial CONTINUE (spawn marker) + final CONVERGE (completion)
    assert "continue" in ap_directives, f"Expected 'continue' in {ap_directives}"
    assert "converge" in ap_directives, f"Expected 'converge' in {ap_directives}"
    # CONTINUE must come before CONVERGE
    assert ap_directives.index("continue") < ap_directives.index("converge")


# ---------------------------------------------------------------------------
# Test 2: cooperative cancel — handle.cancel() causes loop to exit with CANCEL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolve_step_honors_cooperative_cancel() -> None:
    """Caller cancels the handle; loop exits at next checkpoint, emits CANCEL."""
    store = _FakeEventStore()
    config = EvolutionaryLoopConfig(max_generations=10, min_generations=1)

    # Build a custom AgentProcess that cancels the handle right after spawning
    captured_handle: list[Any] = []
    real_agent_process = AgentProcess(event_store=store)

    class _CancellingAgentProcess:
        """Wraps AgentProcess.spawn and cancels before the first work checkpoint."""

        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _cancel_before_work(handle: Any) -> None:
                captured_handle.append(handle)
                await handle.cancel(reason="test cancel")
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_cancel_before_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=config,
        agent_process=_CancellingAgentProcess(),  # type: ignore[arg-type]
    )

    seed = _make_seed()

    result = await loop.evolve_step(lineage_id="lin-cancel-1", initial_seed=seed)

    assert result.is_ok
    step = result.value
    # When cancelled at the pre-generation checkpoint, action is INTERRUPTED
    assert step.action == StepAction.INTERRUPTED

    assert len(captured_handle) == 1
    assert captured_handle[0].status() is AgentProcessStatus.CANCELLED

    interrupted_events = [e for e in store.appended if e.type == "lineage.generation.interrupted"]
    assert len(interrupted_events) == 1
    assert interrupted_events[0].data["generation_number"] == 1
    assert interrupted_events[0].data["seed_json"] == '{"seed_id": "seed-1"}'

    # CANCEL directive must appear in both agent_process and lineage decision events.
    ap_directives = [
        e.data["directive"]
        for e in store.appended
        if e.type == "control.directive.emitted"
        and getattr(e, "aggregate_type", None) == "agent_process"
    ]
    lineage_directives = [
        e.data["directive"]
        for e in store.appended
        if e.type == "control.directive.emitted" and getattr(e, "aggregate_type", None) == "lineage"
    ]
    assert "cancel" in ap_directives, f"Expected 'cancel' in {ap_directives}"
    assert "cancel" in lineage_directives, f"Expected lineage 'cancel' in {lineage_directives}"


# ---------------------------------------------------------------------------
# Test 3: regression guard — lineage.* events still flow unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolve_step_existing_lineage_events_unchanged() -> None:
    """lineage.generation.started and .completed events still appear (regression guard)."""
    store = _FakeEventStore()
    loop = _build_loop(store)
    seed = _make_seed()

    result = await loop.evolve_step(lineage_id="lin-regression-1", initial_seed=seed)

    assert result.is_ok

    event_types = [e.type for e in store.appended]

    # lineage.created must be emitted for Gen 1
    assert "lineage.created" in event_types, f"Missing lineage.created in {event_types}"

    # lineage.generation.completed must be emitted (the main lineage state event)
    assert "lineage.generation.completed" in event_types, (
        f"Missing lineage.generation.completed in {event_types}"
    )

    # Verify the completed event carries the correct generation number
    completed_events = [e for e in store.appended if e.type == "lineage.generation.completed"]
    assert len(completed_events) >= 1
    assert completed_events[0].data.get("generation_number") == 1

    # lineage.* events must be separate from agent_process events
    lineage_events = [e for e in store.appended if e.type.startswith("lineage.")]
    ap_events = [
        e
        for e in store.appended
        if e.type == "control.directive.emitted"
        and getattr(e, "aggregate_type", None) == "agent_process"
    ]
    assert len(lineage_events) >= 2, f"Expected >= 2 lineage events, got {len(lineage_events)}"
    assert len(ap_events) >= 2, f"Expected >= 2 agent_process events, got {len(ap_events)}"

    # Lineage events must NOT have aggregate_type="agent_process"
    for e in lineage_events:
        assert getattr(e, "aggregate_type", None) != "agent_process", (
            f"Lineage event {e.type!r} should not have aggregate_type='agent_process'"
        )


@pytest.mark.asyncio
async def test_evolve_step_finishes_lineage_after_cancel_past_generation_boundary() -> None:
    """Cancel after generation returns must not drop durable lineage completion."""
    store = _FakeEventStore()
    config = EvolutionaryLoopConfig(max_generations=10, min_generations=1)
    real_agent_process = AgentProcess(event_store=store)
    captured_handle: list[Any] = []

    class _CapturingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _capturing_work(handle: Any) -> None:
                captured_handle.append(handle)
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_capturing_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=config,
        agent_process=_CapturingAgentProcess(),  # type: ignore[arg-type]
    )
    generation_result = _make_generation_result(seed=_make_seed("seed-cancel-after-run"))

    async def _fake_run_generation_with_watchdog(**kwargs: Any) -> Result[GenerationResult, Any]:
        assert captured_handle, "AgentProcess handle should be captured before generation runs"
        await captured_handle[0].cancel(reason="cancel after generation returned")
        return Result.ok(generation_result)

    loop._run_generation_with_watchdog = _fake_run_generation_with_watchdog  # type: ignore[method-assign]

    result = await loop.evolve_step(lineage_id="lin-cancel-after-run", initial_seed=_make_seed())

    assert result.is_ok
    assert result.value.action is not StepAction.INTERRUPTED
    assert captured_handle[0].status() is AgentProcessStatus.COMPLETED
    event_types = [e.type for e in store.appended]
    assert "lineage.generation.completed" in event_types
    ap_directives = [
        e.data["directive"]
        for e in store.appended
        if e.type == "control.directive.emitted"
        and getattr(e, "aggregate_type", None) == "agent_process"
    ]
    assert ap_directives[-1] == "converge"


@pytest.mark.asyncio
async def test_evolve_step_reports_agent_process_work_exception() -> None:
    """Failures inside AgentProcess work surface their original exception details."""

    class _PostProcessingFailure(RuntimeError):
        pass

    class _FailingEventStore(_FakeEventStore):
        async def append(self, event: BaseEvent) -> None:
            if event.type == "lineage.generation.completed":
                raise _PostProcessingFailure("completed event write failed")
            await super().append(event)

    store = _FailingEventStore()
    loop = _build_loop(store)

    result = await loop.evolve_step(
        lineage_id="lin-post-processing-failure", initial_seed=_make_seed()
    )

    assert result.is_err
    message = str(result.error)
    assert "agent process failed during generation work" in message
    assert "_PostProcessingFailure" in message
    assert "completed event write failed" in message


@pytest.mark.asyncio
async def test_evolve_step_cancels_agent_process_work_when_caller_cancelled() -> None:
    """Caller cancellation before the durability boundary aborts generation work."""
    store = _FakeEventStore()
    real_agent_process = AgentProcess(event_store=store)
    captured_handle: list[Any] = []

    class _CapturingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _capturing_work(handle: Any) -> None:
                captured_handle.append(handle)
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_capturing_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=EvolutionaryLoopConfig(max_generations=10, min_generations=1),
        agent_process=_CapturingAgentProcess(),  # type: ignore[arg-type]
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _never_finishes(**kwargs: Any) -> Result[GenerationResult, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    loop._run_generation_with_watchdog = _never_finishes  # type: ignore[method-assign]

    task = asyncio.create_task(
        loop.evolve_step(lineage_id="lin-caller-cancel", initial_seed=_make_seed())
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert captured_handle[0].status() is AgentProcessStatus.CANCELLED
    assert "lineage.generation.completed" not in [e.type for e in store.appended]


@pytest.mark.asyncio
async def test_run_generation_phases_honors_agent_process_cancel_before_execute() -> None:
    """AgentProcess cancellation is checked inside generation phase execution."""
    store = _FakeEventStore()

    async def _executor(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("executor should not run after AgentProcess cancel")

    loop = EvolutionaryLoop(
        event_store=store,
        config=EvolutionaryLoopConfig(max_generations=10, min_generations=1),
        executor=_executor,
    )

    class _CancellingHandle:
        wait_count = 0

        async def wait_unpaused(self) -> None:
            self.wait_count += 1

        def should_cancel(self) -> bool:
            return True

    handle = _CancellingHandle()
    result = await loop._run_generation_phases(
        lineage=OntologyLineage(lineage_id="lin-phase-cancel", goal="test goal"),
        generation_number=1,
        current_seed=_make_seed(),
        agent_process_handle=handle,  # type: ignore[arg-type]
    )

    assert result.is_ok
    assert result.value.phase is GenerationPhase.INTERRUPTED
    assert handle.wait_count >= 1
    assert any(e.type == "lineage.generation.interrupted" for e in store.appended)


@pytest.mark.asyncio
async def test_evolve_step_keeps_agent_process_cancelled_for_interrupted_generation() -> None:
    """Interrupted GenerationResult must not be converted to AgentProcess completion."""
    store = _FakeEventStore()
    real_agent_process = AgentProcess(event_store=store)
    captured_handle: list[Any] = []

    class _CapturingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _capturing_work(handle: Any) -> None:
                captured_handle.append(handle)
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_capturing_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=EvolutionaryLoopConfig(max_generations=10, min_generations=1),
        agent_process=_CapturingAgentProcess(),  # type: ignore[arg-type]
    )

    async def _interrupted_generation(**kwargs: Any) -> Result[GenerationResult, Any]:
        assert captured_handle, "AgentProcess handle should be captured before generation runs"
        await captured_handle[0].cancel(reason="cancel inside generation phases")
        return Result.ok(
            GenerationResult(
                generation_number=1,
                seed=_make_seed("seed-interrupted"),
                phase=GenerationPhase.INTERRUPTED,
                success=False,
            )
        )

    loop._run_generation_with_watchdog = _interrupted_generation  # type: ignore[method-assign]

    result = await loop.evolve_step(lineage_id="lin-internal-cancel", initial_seed=_make_seed())

    assert result.is_ok
    assert result.value.action is StepAction.INTERRUPTED
    assert (
        result.value.convergence_signal.reason == "AgentProcess cancel requested during generation"
    )
    assert captured_handle[0].status() is AgentProcessStatus.CANCELLED


@pytest.mark.asyncio
async def test_evolve_step_drains_post_generation_writes_when_caller_cancelled() -> None:
    """Caller cancellation after generation completion must not drop lineage completion."""

    class _BlockingCompletedEventStore(_FakeEventStore):
        def __init__(self) -> None:
            super().__init__()
            self.completed_append_started = asyncio.Event()
            self.release_completed_append = asyncio.Event()

        async def append(self, event: BaseEvent) -> None:
            if event.type == "lineage.generation.completed":
                self.completed_append_started.set()
                await self.release_completed_append.wait()
            await super().append(event)

    store = _BlockingCompletedEventStore()
    real_agent_process = AgentProcess(event_store=store)
    captured_handle: list[Any] = []

    class _CapturingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _capturing_work(handle: Any) -> None:
                captured_handle.append(handle)
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_capturing_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=EvolutionaryLoopConfig(max_generations=10, min_generations=1),
        agent_process=_CapturingAgentProcess(),  # type: ignore[arg-type]
    )
    generation_result = _make_generation_result(seed=_make_seed("seed-post-boundary"))

    async def _fake_run_generation_with_watchdog(**kwargs: Any) -> Result[GenerationResult, Any]:
        return Result.ok(generation_result)

    loop._run_generation_with_watchdog = _fake_run_generation_with_watchdog  # type: ignore[method-assign]

    task = asyncio.create_task(
        loop.evolve_step(lineage_id="lin-post-boundary-cancel", initial_seed=_make_seed())
    )
    await asyncio.wait_for(store.completed_append_started.wait(), timeout=1.0)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    store.release_completed_append.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured_handle[0].status() is AgentProcessStatus.COMPLETED
    event_types = [e.type for e in store.appended]
    assert "lineage.generation.completed" in event_types


@pytest.mark.asyncio
async def test_evolve_step_installs_sigint_handler_before_initial_pause() -> None:
    """Pre-generation AgentProcess pause must still allow graceful SIGINT handling."""
    store = _FakeEventStore()
    loop = EvolutionaryLoop(event_store=store, config=EvolutionaryLoopConfig())
    order: list[str] = []
    handles: list[Any] = []

    def _install_sigint_handler() -> None:
        order.append("install")
        loop._shutdown_requested = False
        loop._shutdown_event = asyncio.Event()

    def _uninstall_sigint_handler() -> None:
        order.append("uninstall")

    loop._install_sigint_handler = _install_sigint_handler  # type: ignore[method-assign]
    loop._uninstall_sigint_handler = _uninstall_sigint_handler  # type: ignore[method-assign]

    class _PausedHandle:
        def __init__(self) -> None:
            self.entered_pause_wait = asyncio.Event()
            self.completed = asyncio.Event()
            self.task: asyncio.Task[None] | None = None

        async def wait_unpaused(self) -> None:
            order.append("wait_unpaused")
            self.entered_pause_wait.set()
            await asyncio.Event().wait()

        def should_cancel(self) -> bool:
            return False

        def should_complete_on_return_after_cancel(self) -> bool:
            return False

        async def cancel(self, reason: str = "cancel requested") -> None:
            if self.task is not None:
                self.task.cancel()

        async def abort(self, reason: str = "abort requested") -> None:
            if self.task is not None:
                self.task.cancel()

        async def wait_until_complete(self, *, timeout: float | None = None) -> Any:
            await asyncio.wait_for(self.completed.wait(), timeout=timeout)
            return AgentProcessStatus.CANCELLED

        def failure(self) -> BaseException | None:
            return None

    class _PausingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            handle = _PausedHandle()
            handles.append(handle)

            async def _runner() -> None:
                try:
                    await work_fn(handle)
                finally:
                    handle.completed.set()

            handle.task = asyncio.create_task(_runner())
            return handle

    loop._agent_process = _PausingAgentProcess()  # type: ignore[assignment]

    task = asyncio.create_task(
        loop.evolve_step(lineage_id="lin-initial-pause", initial_seed=_make_seed())
    )
    while not handles:
        await asyncio.sleep(0)
    await asyncio.wait_for(handles[0].entered_pause_wait.wait(), timeout=1.0)

    assert order[:2] == ["install", "wait_unpaused"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "uninstall" in order


@pytest.mark.asyncio
async def test_evolve_step_sigint_while_initial_agent_process_pause_interrupts() -> None:
    """SIGINT while paused at the initial checkpoint must not hang evolve_step."""
    store = _FakeEventStore()
    loop = EvolutionaryLoop(event_store=store, config=EvolutionaryLoopConfig())
    handles: list[Any] = []

    def _install_sigint_handler() -> None:
        loop._shutdown_requested = False
        loop._shutdown_event = asyncio.Event()

    def _uninstall_sigint_handler() -> None:
        return None

    loop._install_sigint_handler = _install_sigint_handler  # type: ignore[method-assign]
    loop._uninstall_sigint_handler = _uninstall_sigint_handler  # type: ignore[method-assign]

    real_agent_process = AgentProcess(event_store=store)

    class _PausingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _pause_before_work(handle: Any) -> None:
                handles.append(handle)
                await handle.pause()
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_pause_before_work,
                process_id=process_id,
            )

    loop._agent_process = _PausingAgentProcess()  # type: ignore[assignment]

    task = asyncio.create_task(
        loop.evolve_step(lineage_id="lin-initial-pause-sigint", initial_seed=_make_seed())
    )
    while not handles:
        await asyncio.sleep(0)
    await _wait_for_agent_status(handles[0], AgentProcessStatus.PAUSED)

    loop._shutdown_requested = True
    loop._shutdown_event.set()

    result = await asyncio.wait_for(task, timeout=1.0)

    assert result.is_ok
    assert result.value.action is StepAction.INTERRUPTED
    assert any(e.type == "lineage.generation.interrupted" for e in store.appended)


@pytest.mark.asyncio
async def test_evolve_step_pre_start_cancel_preserves_resume_phase_checkpoint() -> None:
    """A second pre-start interruption must preserve prior phase-level resume metadata."""

    class _InterruptedLineageStore(_FakeEventStore):
        async def replay_lineage(self, lineage_id: str) -> list[BaseEvent]:
            return [
                lineage_created(lineage_id, "test goal"),
                lineage_generation_interrupted(
                    lineage_id,
                    1,
                    last_completed_phase="executing",
                    seed_json='{"seed_id": "seed-1"}',
                ),
            ]

    store = _InterruptedLineageStore()
    real_agent_process = AgentProcess(event_store=store)
    captured_handle: list[Any] = []

    class _CancellingAgentProcess:
        async def spawn(self, *, intent: str, work_fn: Any, process_id: str | None = None) -> Any:
            async def _cancel_before_work(handle: Any) -> None:
                captured_handle.append(handle)
                await handle.cancel(reason="test cancel")
                await work_fn(handle)

            return await real_agent_process.spawn(
                intent=intent,
                work_fn=_cancel_before_work,
                process_id=process_id,
            )

    loop = EvolutionaryLoop(
        event_store=store,
        config=EvolutionaryLoopConfig(max_generations=10, min_generations=1),
        agent_process=_CancellingAgentProcess(),  # type: ignore[arg-type]
    )

    result = await loop.evolve_step(lineage_id="lin-resume-cancel", initial_seed=_make_seed())

    assert result.is_ok
    assert result.value.action is StepAction.INTERRUPTED
    assert captured_handle[0].status() is AgentProcessStatus.CANCELLED
    interrupted_events = [e for e in store.appended if e.type == "lineage.generation.interrupted"]
    assert interrupted_events[-1].data["last_completed_phase"] == "executing"


@pytest.mark.asyncio
async def test_check_shutdown_does_not_wait_on_pause_after_sigint() -> None:
    """SIGINT shutdown must bypass AgentProcess pause waits."""
    store = _FakeEventStore()
    loop = EvolutionaryLoop(event_store=store, config=EvolutionaryLoopConfig())
    loop._shutdown_requested = True

    class _PausedHandle:
        async def wait_unpaused(self) -> None:
            raise AssertionError("SIGINT shutdown should not wait for paused AgentProcess")

        def should_cancel(self) -> bool:
            return False

    result = await loop._check_shutdown(
        lineage_id="lin-sigint-paused",
        generation_number=1,
        last_completed_phase=None,
        current_seed=_make_seed(),
        agent_process_handle=_PausedHandle(),  # type: ignore[arg-type]
    )

    assert result is not None
    assert result.phase is GenerationPhase.INTERRUPTED


@pytest.mark.asyncio
async def test_check_shutdown_wakes_paused_agent_process_when_sigint_arrives() -> None:
    """SIGINT during an in-flight pause wait must wake shutdown handling."""
    store = _FakeEventStore()
    loop = EvolutionaryLoop(event_store=store, config=EvolutionaryLoopConfig())
    entered_pause_wait = asyncio.Event()
    pause_wait_cancelled = asyncio.Event()
    release_pause = asyncio.Event()

    class _PausedHandle:
        async def wait_unpaused(self) -> None:
            entered_pause_wait.set()
            try:
                await release_pause.wait()
            except asyncio.CancelledError:
                pause_wait_cancelled.set()
                raise

        def should_cancel(self) -> bool:
            return False

    shutdown_task = asyncio.create_task(
        loop._check_shutdown(
            lineage_id="lin-sigint-during-pause",
            generation_number=1,
            last_completed_phase=None,
            current_seed=_make_seed(),
            agent_process_handle=_PausedHandle(),  # type: ignore[arg-type]
        )
    )

    await asyncio.wait_for(entered_pause_wait.wait(), timeout=1.0)
    loop._shutdown_requested = True
    loop._shutdown_event.set()

    result = await asyncio.wait_for(shutdown_task, timeout=1.0)

    assert result is not None
    assert result.phase is GenerationPhase.INTERRUPTED
    assert pause_wait_cancelled.is_set()
