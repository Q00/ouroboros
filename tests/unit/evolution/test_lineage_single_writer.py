"""Durable lineage-writer regressions for evolve retries."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from ouroboros.core.lineage import GenerationPhase
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.events.lineage import (
    lineage_created,
    lineage_generation_completed,
    lineage_generation_failed,
    lineage_generation_interrupted,
    lineage_generation_started,
)
from ouroboros.evolution import loop_support
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.loop import GenerationResult, StepAction, StepResult
from ouroboros.evolution.loop_support import run_durable_lineage_single_flight
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ACPatch, ReflectOutput
from ouroboros.evolution.step_receipt import (
    MAX_EXECUTION_TEXT,
    decode_step_result,
    encode_step_result,
)
from ouroboros.evolution.wonder import GroundedQuestion, WonderOutput
from ouroboros.persistence import lineage_claims
from ouroboros.persistence.event_store import EventStore


def _seed() -> Seed:
    return Seed(
        goal="Prove one durable evolve writer",
        task_type="code",
        constraints=("Keep retries idempotent",),
        acceptance_criteria=("One generation has one writer",),
        ontology_schema=OntologySchema(
            name="LineageWriter",
            description="Durable generation authority",
            fields=(
                OntologyField(
                    name="owner",
                    field_type="string",
                    description="The generation writer",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="single_writer",
                description="Provider work executes once",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="one_receipt",
                description="Every waiter observes the winner",
                evaluation_criteria="Exactly one operation call",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_single_writer", ambiguity_score=0.0),
    )


async def _stores(db_path: Path) -> tuple[EventStore, EventStore]:
    database_url = f"sqlite+aiosqlite:///{db_path}"
    first = EventStore(database_url)
    second = EventStore(database_url)
    await first.initialize()
    await second.initialize()
    return first, second


def _run_process_claim(
    database_url: str,
    label: str,
    marker_path: str,
    owner_ready_path: str,
    release_path: str,
    process_started_path: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """Run one standalone process against the shared lineage claim table."""

    async def run() -> None:
        store = EventStore(database_url)
        await store.initialize()
        Path(process_started_path).write_text(label, encoding="utf-8")

        async def operation() -> str:
            Path(owner_ready_path).write_text(label, encoding="utf-8")
            while not Path(release_path).exists():
                await asyncio.sleep(0.01)
            with Path(marker_path).open("a", encoding="utf-8") as marker:
                marker.write(f"{label}\n")
            return label

        try:
            value = await run_durable_lineage_single_flight(
                store,
                "cross-process-lineage",
                "same-request",
                operation,
                generation_number=3,
                encode=lambda result: {"value": result},
                decode=lambda payload: str(payload["value"]),
            )
            result_queue.put(("ok", value))
        except BaseException as exc:
            result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            await store.close()

    asyncio.run(run())


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        time.sleep(0.01)


@pytest.mark.asyncio
async def test_durable_claim_coalesces_distinct_event_store_instances(tmp_path: Path) -> None:
    """The database claim, not the process registry, owns one generation."""
    first_store, second_store = await _stores(tmp_path / "lineage-writer.db")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def winner() -> str:
        calls.append("generation-3")
        entered.set()
        await release.wait()
        return "generation-3-winner"

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    encode = lambda value: {"value": value}  # noqa: E731
    decode = lambda payload: str(payload["value"])  # noqa: E731
    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                first_store,
                "lineage",
                "request-3",
                winner,
                generation_number=3,
                encode=encode,
                decode=decode,
            )
        )
        await entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                second_store,
                "lineage",
                "request-3",
                forbidden_duplicate,
                generation_number=3,
                encode=encode,
                decode=decode,
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()

        assert await asyncio.gather(owner, waiter) == [
            "generation-3-winner",
            "generation-3-winner",
        ]
        assert calls == ["generation-3"]

        late_retry = await run_durable_lineage_single_flight(
            second_store,
            "lineage",
            "request-3",
            forbidden_duplicate,
            generation_number=3,
            encode=encode,
            decode=decode,
        )
        assert late_retry == "generation-3-winner"

        async def next_generation() -> str:
            calls.append("generation-4")
            return "generation-4-winner"

        advanced = await run_durable_lineage_single_flight(
            first_store,
            "lineage",
            "request-4",
            next_generation,
            generation_number=4,
            encode=encode,
            decode=decode,
        )
        assert advanced == "generation-4-winner"
        assert calls == ["generation-3", "generation-4"]
    finally:
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_action", ["failed", "interrupted"])
async def test_retryable_receipt_replays_waiters_but_allows_later_attempt(
    tmp_path: Path,
    retryable_action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure receipts deduplicate overlap without pinning later recovery forever."""
    writer, retry_store = await _stores(tmp_path / f"retry-{retryable_action}.db")
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    waiter_registered = asyncio.Event()
    original_try_acquire = lineage_claims.try_acquire

    async def observe_waiter(*args, **kwargs):  # type: ignore[no-untyped-def]
        claim = await original_try_acquire(*args, **kwargs)
        if claim is not None and claim.waiter_registered:
            waiter_registered.set()
        return claim

    monkeypatch.setattr(lineage_claims, "try_acquire", observe_waiter)

    async def failed_attempt() -> str:
        calls.append(retryable_action)
        entered.set()
        await release.wait()
        return retryable_action

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    async def recovered_attempt() -> str:
        calls.append("continue")
        return "continue"

    def encode(action: str) -> dict[str, object]:
        return {"ok": True, "action": action}

    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                writer,
                "lineage",
                "same-request",
                failed_attempt,
                generation_number=2,
                encode=encode,
                decode=lambda payload: str(payload["action"]),
            )
        )
        await entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                retry_store,
                "lineage",
                "same-request",
                forbidden_duplicate,
                generation_number=2,
                encode=encode,
                decode=lambda payload: str(payload["action"]),
            )
        )
        await waiter_registered.wait()
        release.set()
        assert await asyncio.gather(owner, waiter) == [
            retryable_action,
            retryable_action,
        ]

        recovered = await run_durable_lineage_single_flight(
            retry_store,
            "lineage",
            "same-request",
            recovered_attempt,
            generation_number=2,
            encode=encode,
            decode=lambda payload: str(payload["action"]),
        )

        assert recovered == "continue"
        assert calls == [retryable_action, "continue"]
    finally:
        await writer.close()
        await retry_store.close()


def test_durable_claim_coalesces_distinct_processes(tmp_path: Path) -> None:
    """Separate interpreters still execute the generation operation exactly once."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cross-process.db'}"
    initializer = EventStore(database_url)
    asyncio.run(initializer.initialize())
    asyncio.run(initializer.close())

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    marker = tmp_path / "operation-calls.txt"
    owner_ready = tmp_path / "owner-ready"
    release = tmp_path / "release-owner"
    first_started = tmp_path / "first-started"
    second_started = tmp_path / "second-started"
    common = (database_url, "process-a", str(marker), str(owner_ready), str(release))
    first = context.Process(
        target=_run_process_claim,
        args=(*common, str(first_started), result_queue),
    )
    second = context.Process(
        target=_run_process_claim,
        args=(
            database_url,
            "process-b",
            str(marker),
            str(owner_ready),
            str(release),
            str(second_started),
            result_queue,
        ),
    )
    first.start()
    try:
        _wait_for_path(first_started)
        _wait_for_path(owner_ready)
        second.start()
        _wait_for_path(second_started)
        time.sleep(0.1)
        release.write_text("release", encoding="utf-8")
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0
        assert second.exitcode == 0
        results = sorted([result_queue.get(timeout=1), result_queue.get(timeout=1)])
        assert results == [("ok", "process-a"), ("ok", "process-a")]
        assert marker.read_text(encoding="utf-8").splitlines() == ["process-a"]
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        result_queue.close()


@pytest.mark.asyncio
async def test_expired_owner_fails_closed_without_implicit_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed writer requires recovery instead of silently duplicating work."""
    first_store, second_store = await _stores(tmp_path / "expired-writer.db")
    monkeypatch.setattr(lineage_claims, "DEFAULT_LEASE_SECONDS", 0.03)
    executed = False

    async def forbidden_operation() -> str:
        nonlocal executed
        executed = True
        return "must-not-run"

    try:
        claim = await lineage_claims.try_acquire(
            first_store,
            scope="evolve-core",
            lineage_id="lineage",
            generation_number=2,
            owner_id="crashed-owner",
            request_key="request-2",
        )
        assert claim is not None and claim.acquired

        with pytest.raises(RuntimeError, match="recover_expired_claim=true"):
            await asyncio.wait_for(
                run_durable_lineage_single_flight(
                    second_store,
                    "lineage",
                    "request-2",
                    forbidden_operation,
                    generation_number=2,
                    encode=lambda value: {"value": value},
                    decode=lambda payload: str(payload["value"]),
                ),
                timeout=1.0,
            )
        assert not executed
        assert await lineage_claims.recover_expired(
            second_store,
            scope="evolve-core",
            lineage_id="lineage",
        )
        recovered = await run_durable_lineage_single_flight(
            second_store,
            "lineage",
            "request-2",
            forbidden_operation,
            generation_number=2,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert recovered == "must-not-run"
        assert executed
    finally:
        await lineage_claims.release(
            first_store,
            scope="evolve-core",
            lineage_id="lineage",
            owner_id="crashed-owner",
        )
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
async def test_heartbeat_failure_before_completion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost renewal cannot be hidden behind a successful provider result."""
    store, reader = await _stores(tmp_path / "heartbeat-loss.db")

    async def failed_heartbeat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database renewal failed")

    async def operation() -> str:
        await asyncio.sleep(0)
        return "unowned-result"

    monkeypatch.setattr(loop_support, "_renew_claim_until_cancelled", failed_heartbeat)
    try:
        with pytest.raises(RuntimeError, match="heartbeat failed before completion"):
            await run_durable_lineage_single_flight(
                store,
                "lineage",
                "request",
                operation,
                generation_number=1,
                encode=lambda value: {"value": value},
                decode=lambda payload: str(payload["value"]),
            )
        assert (
            await lineage_claims.observe(
                reader,
                scope="evolve-core",
                lineage_id="lineage",
            )
            is None
        )
    finally:
        await store.close()
        await reader.close()


@pytest.mark.asyncio
async def test_post_commit_heartbeat_error_does_not_override_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the receipt commits, a racing heartbeat exception is no longer authoritative."""
    writer, reader = await _stores(tmp_path / "heartbeat-after-commit.db")
    committed = asyncio.Event()
    original_complete = lineage_claims.complete

    async def heartbeat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        await committed.wait()
        raise RuntimeError("late heartbeat failure")

    async def complete_then_fail_heartbeat(*args, **kwargs):  # type: ignore[no-untyped-def]
        published = await original_complete(*args, **kwargs)
        committed.set()
        await asyncio.sleep(0)
        return published

    monkeypatch.setattr(loop_support, "_renew_claim_until_cancelled", heartbeat)
    monkeypatch.setattr(lineage_claims, "complete", complete_then_fail_heartbeat)
    try:
        result = await run_durable_lineage_single_flight(
            writer,
            "lineage",
            "request",
            lambda: asyncio.sleep(0, result="committed-result"),
            generation_number=1,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert result == "committed-result"

        replayed = await run_durable_lineage_single_flight(
            reader,
            "lineage",
            "request",
            lambda: asyncio.sleep(0, result="duplicate"),
            generation_number=1,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert replayed == "committed-result"
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_failed_step_receipt_replays_seed_from_started_event(tmp_path: Path) -> None:
    """Cross-process waiters reproduce a failed winner without inventing a Seed."""
    writer, reader = await _stores(tmp_path / "failed-receipt.db")
    seed = _seed()
    lineage_id = "failed-lineage"
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_started(
                lineage_id,
                1,
                GenerationPhase.EXECUTING.value,
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )
        await writer.append(
            lineage_generation_failed(
                lineage_id,
                1,
                GenerationPhase.EXECUTING.value,
                "provider failed",
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        result = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    phase=GenerationPhase.FAILED,
                    success=False,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="provider failed",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.FAILED,
                next_generation=1,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(result))

        assert replayed.is_ok
        assert replayed.value.action is StepAction.FAILED
        assert replayed.value.generation_result.seed == seed
        assert replayed.value.generation_result.phase is GenerationPhase.FAILED
        assert not replayed.value.generation_result.success
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_interrupted_step_receipt_replays_partial_generation(tmp_path: Path) -> None:
    """A waiter recovers interrupted Wonder/Reflect evidence from the durable checkpoint."""
    writer, reader = await _stores(tmp_path / "interrupted-receipt.db")
    seed = _seed()
    lineage_id = "interrupted-lineage"
    questions = ("[AC 1] What writer evidence remains unresolved?",)
    wonder = WonderOutput(
        questions=questions,
        grounded_questions=(
            GroundedQuestion(question=questions[0], kind="challenge", ac_indices=(0,)),
        ),
        ontology_tensions=("owner and waiter observe one contract",),
        should_continue=True,
        reasoning="challenge",
    )
    reflect = ReflectOutput(
        refined_goal=seed.goal,
        refined_constraints=seed.constraints,
        refined_acs=("One generation has one writer",),
        ac_patches=(ACPatch(op="keep", index=0, reason="preserve identity"),),
        reasoning="preserve the partial reflection",
    )
    reflect.restore_durable_patch_identity(seed)
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )
        await writer.append(
            lineage_generation_interrupted(
                lineage_id,
                2,
                last_completed_phase=GenerationPhase.EXECUTING.value,
                partial_state={
                    "wonder_questions": list(questions),
                    "reflect_output": reflect.model_dump(mode="json"),
                    "execution_output": "partial execution",
                },
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        result = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=2,
                    seed=seed,
                    wonder_output=wonder,
                    reflect_output=reflect,
                    execution_output="partial execution",
                    phase=GenerationPhase.INTERRUPTED,
                    success=False,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="Generation interrupted by SIGINT",
                    ontology_similarity=0.0,
                    generation=2,
                ),
                lineage=lineage,
                action=StepAction.INTERRUPTED,
                next_generation=2,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(result))

        assert replayed.is_ok
        generation = replayed.value.generation_result
        assert replayed.value.action is StepAction.INTERRUPTED
        assert generation.phase is GenerationPhase.INTERRUPTED
        assert generation.wonder_output == wonder
        assert generation.reflect_output == reflect
        assert generation.reflect_output is not None
        assert generation.reflect_output.ac_patch_identity_explicit is True
        assert generation.execution_output == "partial execution"
        assert not generation.success
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_completed_step_receipt_preserves_structured_phase_outputs(tmp_path: Path) -> None:
    """Completed projection cannot erase the winner result observed by a waiter."""
    writer, reader = await _stores(tmp_path / "completed-structured-receipt.db")
    seed = _seed()
    lineage_id = "completed-structured-lineage"
    wonder = WonderOutput(
        questions=(),
        grounded_questions=(),
        ontology_tensions=("empty questions still carry a tension",),
        should_continue=True,
        reasoning="authoritative empty-question result",
    )
    reflect = ReflectOutput(
        refined_goal=seed.goal,
        refined_constraints=seed.constraints,
        refined_acs=("One generation has one writer",),
        ac_patches=(ACPatch(op="keep", index=0, reason="stable contract"),),
        settled_ac_indices=(0,),
        reasoning="parser-issued keep",
    )
    reflect.restore_durable_patch_identity(seed)
    execution_output = "execution-start\n" + ("x" * 12_000) + "\nTRAILING FAILURE MARKER"
    validation_output = "validation-start\n" + ("v" * 4_000) + "\nVALIDATION TAIL"
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                wonder_questions=[],
                seed_json=json.dumps(seed.to_dict()),
                execution_output=execution_output,
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        assert lineage.generations[-1].partial_state is None
        winner = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    wonder_output=wonder,
                    reflect_output=reflect,
                    execution_output=execution_output,
                    validation_output=validation_output,
                    phase=GenerationPhase.COMPLETED,
                    success=True,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="continue",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.CONTINUE,
                next_generation=2,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(winner))

        assert replayed.is_ok
        waiter_generation = replayed.value.generation_result
        assert waiter_generation.wonder_output == wonder
        assert waiter_generation.reflect_output == reflect
        assert waiter_generation.reflect_output is not None
        assert waiter_generation.reflect_output.ac_patch_identity_explicit is True
        assert waiter_generation.execution_output == execution_output
        assert waiter_generation.execution_output.endswith("TRAILING FAILURE MARKER")
        assert waiter_generation.validation_output == validation_output
        assert waiter_generation.validation_output.endswith("VALIDATION TAIL")
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_step_receipt_fails_closed_when_execution_output_exceeds_cap(
    tmp_path: Path,
) -> None:
    """Incomplete bounded evidence never becomes a handler verification artifact."""
    writer, reader = await _stores(tmp_path / "oversized-execution-receipt.db")
    seed = _seed()
    lineage_id = "oversized-execution-lineage"
    execution_output = "x" * (MAX_EXECUTION_TEXT + 1)
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                seed_json=json.dumps(seed.to_dict()),
                execution_output=execution_output,
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        winner = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    execution_output=execution_output,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="continue",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.CONTINUE,
                next_generation=2,
            )
        )
        payload = encode_step_result(winner)
        assert payload["execution_output_complete"] is False

        with pytest.raises(ValueError, match="execution output is incomplete"):
            await decode_step_result(reader, payload)
    finally:
        await writer.close()
        await reader.close()
