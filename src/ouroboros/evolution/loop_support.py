"""Small deterministic helpers shared by the evolution loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
import inspect
import json
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from ouroboros.evolution.focus import EvolutionFocus

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.conductor import ConductorDirective, stable_payload_digest
from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyLineage,
)
from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.events.control import create_control_directive_emitted_event
from ouroboros.events.lineage import lineage_generation_phase_changed, lineage_generation_started
from ouroboros.evolution.directive_mapping import (
    is_terminal_directive,
    step_action_to_directive,
)
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)
_PHASE_ORDER = ("wondering", "reflecting", "seeding", "executing", "evaluating")
type GenerationClaimCallback = Callable[[int], Awaitable[None]]


class LineageWinnerAdvanced(RuntimeError):
    """The caller must recompute its request from the durable winner state."""


class LineageFlightConflict(RuntimeError):
    """A different same-lineage request already owns the local flight."""


@dataclass(frozen=True, slots=True)
class EffectiveEvolutionExecutionPolicy:
    """Task-local focus policy after applying an explicit benchmark override."""

    focused_evolution: bool
    scoped_reexecution: bool
    benchmark_control: bool


_ACTIVE_EVOLUTION_EXECUTION_POLICY: ContextVar[EffectiveEvolutionExecutionPolicy | None] = (
    ContextVar("ouroboros_effective_evolution_execution_policy", default=None)
)


def phase_should_skip(phase: str, resume_after_phase: str | None) -> bool:
    """Return whether a phase completed before the durable resume boundary."""
    if resume_after_phase is None:
        return False
    try:
        return _PHASE_ORDER.index(phase) <= _PHASE_ORDER.index(resume_after_phase)
    except ValueError:
        return False


@dataclass(slots=True)
class _LineageFlight[T]:
    request_key: str
    task: asyncio.Task[T]
    waiters: int = 0


_lineage_flights: dict[str, dict[tuple[str, str], _LineageFlight[Any]]] = {}


def _event_store_authority(event_store: EventStore) -> str:
    database_url = getattr(event_store, "_database_url", None)
    if isinstance(database_url, str) and ":memory:" not in database_url:
        return f"database:{database_url}"
    return f"object:{id(event_store)}"


def default_runtime_controls() -> RuntimeControlsConfig:
    """Load runtime controls through the config/env compatibility layer."""
    from ouroboros.config.loader import get_runtime_controls_config

    return get_runtime_controls_config()


def generation_execution_id(lineage_id: str, generation_number: int) -> str:
    """Build the deterministic execution ID for an evolve generation."""
    return f"evolve:{lineage_id}:generation:{generation_number}"


def _execution_policy_value(value: Any) -> Any:
    """Project nested config values into a stable, field-complete JSON tree."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _execution_policy_value(model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _execution_policy_value(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _execution_policy_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_execution_policy_value(item) for item in value]
    raise TypeError(f"Unsupported evolution execution-policy value: {type(value).__qualname__}")


def effective_evolution_execution_policy(
    config: Any,
    benchmark_control: bool = False,
) -> EffectiveEvolutionExecutionPolicy:
    """Resolve one immutable policy without mutating the shared loop config."""
    if not isinstance(benchmark_control, bool):
        raise TypeError("benchmark_control must be a boolean")
    return EffectiveEvolutionExecutionPolicy(
        focused_evolution=(False if benchmark_control else bool(config.focused_evolution)),
        scoped_reexecution=(False if benchmark_control else bool(config.scoped_reexecution)),
        benchmark_control=benchmark_control,
    )


@contextmanager
def evolution_execution_policy_context(
    config: Any,
    benchmark_control: bool = False,
) -> Iterator[EffectiveEvolutionExecutionPolicy]:
    """Bind one policy to the current async task and every child task it creates."""
    policy = effective_evolution_execution_policy(
        config,
        benchmark_control=benchmark_control,
    )
    token = _ACTIVE_EVOLUTION_EXECUTION_POLICY.set(policy)
    try:
        yield policy
    finally:
        _ACTIVE_EVOLUTION_EXECUTION_POLICY.reset(token)


def current_evolution_execution_policy(config: Any) -> EffectiveEvolutionExecutionPolicy:
    """Return the active task-local policy or the normal configured policy."""
    return _ACTIVE_EVOLUTION_EXECUTION_POLICY.get() or effective_evolution_execution_policy(config)


def evolution_execution_policy(
    config: Any | None,
    benchmark_control: bool = False,
) -> dict[str, Any] | None:
    """Return every declared evolution config field for durable request identity."""
    if config is None:
        return None
    projected = _execution_policy_value(config)
    if not isinstance(projected, dict):
        raise TypeError("Evolution execution policy must project to an object")
    effective = effective_evolution_execution_policy(
        config,
        benchmark_control=benchmark_control,
    )
    projected["benchmark_control"] = effective.benchmark_control
    projected["effective_focused_evolution"] = effective.focused_evolution
    projected["effective_scoped_reexecution"] = effective.scoped_reexecution
    return projected


def generation_phase_checkpoint(
    *,
    lineage_id: str,
    generation_number: int,
    phase: GenerationPhase,
    last_completed_phase: str | None,
    seed: Seed,
    active_ac_indices: tuple[int, ...],
    frozen_ac_indices: tuple[int, ...],
    wonder_output: Any | None = None,
    reflect_output: Any | None = None,
    execution_output: str | None = None,
    execution_boundary_completed: bool = False,
    validation_output: str | None = None,
    validation_boundary_completed: bool = False,
    evaluation_summary: Any | None = None,
) -> BaseEvent:
    """Persist bounded state needed to resume without replaying prior side effects."""
    partial_state = generation_partial_state(
        wonder_output=wonder_output,
        reflect_output=reflect_output,
        execution_output=execution_output,
        execution_boundary_completed=execution_boundary_completed,
        validation_output=validation_output,
        validation_boundary_completed=validation_boundary_completed,
        evaluation_summary=evaluation_summary,
        focus_checkpointed=True,
    )
    return lineage_generation_phase_changed(
        lineage_id,
        generation_number,
        phase.value,
        last_completed_phase=last_completed_phase,
        seed_id=seed.metadata.seed_id,
        seed_json=json.dumps(seed.to_dict()),
        partial_state=partial_state,
        active_ac_indices=list(active_ac_indices),
        frozen_ac_indices=list(frozen_ac_indices),
    )


def generation_partial_state(
    *,
    wonder_output: Any | None = None,
    reflect_output: Any | None = None,
    execution_output: str | None = None,
    execution_boundary_completed: bool = False,
    validation_output: str | None = None,
    validation_boundary_completed: bool = False,
    evaluation_summary: Any | None = None,
    focus_checkpointed: bool = False,
) -> dict[str, Any]:
    """Serialize bounded phase outputs with explicit completeness markers."""
    partial_state: dict[str, Any] = {
        "execution_boundary_completed": execution_boundary_completed,
        "validation_boundary_completed": validation_boundary_completed,
        "wonder_output_complete": True,
        "reflect_output_complete": True,
        "evaluation_summary_complete": True,
    }
    if focus_checkpointed:
        partial_state["focus_checkpointed"] = True
    if wonder_output is not None:
        partial_state["wonder_questions"] = list(wonder_output.questions)
        partial_state["wonder_output"] = wonder_output.model_dump(mode="json")
    if reflect_output is not None:
        partial_state["reflect_output"] = reflect_output.model_dump(mode="json")
        partial_state["reflect_patch_identity_explicit"] = bool(
            reflect_output.ac_patch_identity_explicit
        )
    if execution_output is not None:
        partial_state["execution_output"] = execution_output[:10_000]
        partial_state["execution_output_complete"] = len(execution_output) <= 10_000
    if validation_output is not None:
        partial_state["validation_output"] = validation_output[:5_000]
        partial_state["validation_output_complete"] = len(validation_output) <= 5_000
    if evaluation_summary is not None:
        partial_state["evaluation_summary"] = evaluation_summary.model_dump(mode="json")
    return partial_state


def hard_crash_recovery(
    lineage: OntologyLineage,
    generation_number: int,
    last_phase: GenerationPhase,
    *,
    execute: bool,
) -> tuple[GenerationRecord | None, str | None]:
    """Validate whether a nonterminal generation has enough authority to resume."""
    if last_phase in {
        GenerationPhase.COMPLETED,
        GenerationPhase.FAILED,
        GenerationPhase.INTERRUPTED,
    }:
        return None, None
    record = next(
        (
            generation
            for generation in reversed(lineage.generations)
            if generation.generation_number == generation_number
        ),
        None,
    )
    partial_state = record.partial_state if record is not None else None
    if last_phase != GenerationPhase.WONDERING and (
        record is None
        or record.last_completed_phase is None
        or not partial_state
        or partial_state.get("focus_checkpointed") is not True
    ):
        return record, (
            "Cannot safely recover hard-crashed generation: durable phase checkpoint is unavailable"
        )
    if last_phase == GenerationPhase.EXECUTING and execute:
        return record, (
            "Cannot safely redispatch a hard-crashed executing generation; reconcile the "
            "external execution before retrying"
        )
    if last_phase != GenerationPhase.WONDERING:
        assert record is not None
        partial_state = record.partial_state
        assert partial_state is not None
        if partial_state.get("wonder_output_complete") is not True:
            return record, (
                "Cannot safely recover hard-crashed generation: complete Wonder output is "
                "unavailable"
            )
        if record.last_completed_phase in {
            GenerationPhase.REFLECTING.value,
            GenerationPhase.SEEDING.value,
        } and not isinstance(partial_state.get("wonder_output"), Mapping):
            return record, (
                "Cannot safely recover hard-crashed generation: complete Wonder output is "
                "unavailable"
            )
        if record.last_completed_phase in {
            GenerationPhase.REFLECTING.value,
            GenerationPhase.SEEDING.value,
        } and (
            partial_state.get("reflect_output_complete") is not True
            or not isinstance(partial_state.get("reflect_output"), Mapping)
            or not isinstance(partial_state.get("reflect_patch_identity_explicit"), bool)
        ):
            return record, (
                "Cannot safely recover hard-crashed generation: complete Reflect output and "
                "patch provenance are unavailable"
            )
        if (
            record.last_completed_phase == GenerationPhase.EVALUATING.value
            and partial_state.get("evaluation_summary_complete") is not True
        ):
            return record, (
                "Cannot safely recover hard-crashed generation: complete evaluation summary "
                "is unavailable"
            )
    if last_phase == GenerationPhase.EVALUATING and execute:
        assert partial_state is not None
        if partial_state.get("execution_boundary_completed") is not True or (
            "execution_output" in partial_state
            and partial_state.get("execution_output_complete") is not True
        ):
            return record, (
                "Cannot safely recover evaluation: durable execution output is unavailable "
                "or incomplete"
            )
        if (
            "validation_output" in partial_state
            and partial_state.get("validation_output_complete") is not True
        ):
            return record, (
                "Cannot safely recover evaluation: durable validation output is incomplete"
            )
    return record, None


@dataclass(frozen=True, slots=True)
class RestoredPhaseState:
    """Typed in-memory projection of one durable nonterminal phase checkpoint."""

    resume_after_phase: str | None
    wonder_output: Any | None = None
    reflect_output: Any | None = None
    execution_output: str | None = None
    execution_boundary_completed: bool = False
    evaluation_summary: EvaluationSummary | None = None
    validation_output: str | None = None
    validation_boundary_completed: bool = False
    checkpoint_focus: Any | None = None


def restore_phase_state(
    lineage: OntologyLineage,
    generation_number: int,
    current_seed: Seed,
    generation_parent_seed: Seed,
    resume_after_phase: str | None,
) -> RestoredPhaseState:
    """Restore checkpointed outputs without trusting them across unfinished boundaries."""
    if not resume_after_phase or not lineage.generations:
        return RestoredPhaseState(resume_after_phase=resume_after_phase)
    from ouroboros.evolution import focus
    from ouroboros.evolution.reflect import ReflectOutput
    from ouroboros.evolution.wonder import WonderOutput, ground_questions

    phase_order = ["wondering", "reflecting", "seeding", "executing", "evaluating"]

    def should_skip(phase: str) -> bool:
        try:
            return phase_order.index(phase) <= phase_order.index(resume_after_phase)
        except ValueError:
            return False

    record = next(
        (
            generation
            for generation in reversed(lineage.generations)
            if generation.generation_number == generation_number
        ),
        None,
    )
    if record is None or not record.partial_state:
        return RestoredPhaseState(resume_after_phase=resume_after_phase)
    partial_state = record.partial_state
    checkpoint_focus = None
    if partial_state.get("focus_checkpointed") is True:
        active = record.active_ac_indices
        frozen = record.frozen_ac_indices
        expected = set(range(len(current_seed.acceptance_criteria)))
        if (
            len(active) == len(set(active))
            and len(frozen) == len(set(frozen))
            and not set(active) & set(frozen)
            and set(active) | set(frozen) == expected
        ):
            checkpoint_focus = focus.EvolutionFocus(
                active_ac_indices=active,
                frozen_ac_indices=frozen,
                reason="restored durable phase checkpoint",
            )
    wonder_output = None
    if should_skip("wondering") and partial_state.get("wonder_output_complete") is True:
        checkpointed_wonder = partial_state.get("wonder_output")
        if checkpointed_wonder is not None:
            try:
                wonder_output = WonderOutput.model_validate(checkpointed_wonder)
            except Exception as exc:
                raise ValueError(f"Failed to restore complete Wonder checkpoint: {exc}") from exc
    elif should_skip("wondering") and "wonder_questions" in partial_state:
        questions = tuple(partial_state["wonder_questions"])
        wonder_output = WonderOutput(
            questions=questions,
            grounded_questions=ground_questions(
                questions,
                len(generation_parent_seed.acceptance_criteria),
            ),
            should_continue=True,
        )
    reflect_output = None
    if should_skip("reflecting") and partial_state.get("reflect_output"):
        try:
            restored_reflect_output = ReflectOutput.model_validate(partial_state["reflect_output"])
            durable_reflect = partial_state.get("reflect_output_complete") is True
            explicit_patch_identity = partial_state.get("reflect_patch_identity_explicit")
            if durable_reflect and not isinstance(explicit_patch_identity, bool):
                raise ValueError("Complete Reflect checkpoint lacks patch provenance")
            if durable_reflect and explicit_patch_identity is True:
                restored_reflect_output.restore_durable_patch_identity(generation_parent_seed)
            if (
                restored_reflect_output.ac_patches
                and not restored_reflect_output.ac_patch_identity_explicit
                and not durable_reflect
                and not should_skip("seeding")
            ):
                resume_after_phase = "wondering"
            else:
                reflect_output = restored_reflect_output
        except Exception as exc:
            if partial_state.get("reflect_output_complete") is True:
                raise ValueError(f"Failed to restore complete Reflect checkpoint: {exc}") from exc
            logger.warning(
                "evolution.resume.reflect_output_restore_failed",
                extra={"error": str(exc)},
            )
    # Graceful-interruption events written before durable phase checkpoints
    # carried ``last_completed_phase`` but no explicit boundary marker.  That
    # event is itself an authoritative acknowledgement that the boundary
    # completed.  Hard-crash records must still carry the explicit marker and
    # therefore remain fail-closed.
    graceful_execution_completed = record.phase == GenerationPhase.INTERRUPTED and (
        record.last_completed_phase
        in {GenerationPhase.EXECUTING.value, GenerationPhase.EVALUATING.value}
    )
    execution_boundary_completed = should_skip("executing") and (
        partial_state.get("execution_boundary_completed") is True or graceful_execution_completed
    )
    execution_output = (
        partial_state.get("execution_output") if execution_boundary_completed else None
    )
    if not isinstance(execution_output, str):
        execution_output = None
    evaluation_summary = None
    if should_skip("evaluating") and partial_state.get("evaluation_summary"):
        try:
            evaluation_summary = EvaluationSummary.model_validate(
                partial_state["evaluation_summary"]
            )
        except Exception as exc:
            if partial_state.get("evaluation_summary_complete") is True:
                raise ValueError(
                    f"Failed to restore complete evaluation checkpoint: {exc}"
                ) from exc
            logger.warning(
                "evolution.resume.evaluation_summary_restore_failed",
                extra={"error": str(exc)},
            )
    validation_boundary_completed = should_skip("executing") and (
        partial_state.get("validation_boundary_completed") is True or graceful_execution_completed
    )
    validation_output = (
        partial_state.get("validation_output") if validation_boundary_completed else None
    )
    if not isinstance(validation_output, str):
        validation_output = None
    return RestoredPhaseState(
        resume_after_phase=resume_after_phase,
        wonder_output=wonder_output,
        reflect_output=reflect_output,
        execution_output=execution_output,
        execution_boundary_completed=execution_boundary_completed,
        evaluation_summary=evaluation_summary,
        validation_output=validation_output,
        validation_boundary_completed=validation_boundary_completed,
        checkpoint_focus=checkpoint_focus,
    )


async def emit_generation_started_once(
    event_store: EventStore,
    *,
    lineage_id: str,
    generation_number: int,
    phase: str,
    seed: Seed,
    focus: EvolutionFocus | None = None,
) -> None:
    """Persist the sole started event while allowing later attempts to resume.

    ``focus`` (when the generation is scoped by prior evaluation evidence)
    is projected into the event's ``ac_focus`` block so observers can report
    which ACs this generation is redoing vs. keeping frozen. Only AC
    descriptions are included — never verify_command/output_assertion.
    """
    events = await event_store.replay_lineage(lineage_id)
    if any(
        event.type == "lineage.generation.started"
        and event.data.get("generation_number") == generation_number
        for event in events
    ):
        return
    active_ac_descriptions: list[str] | None = None
    if focus is not None:
        criteria = tuple(getattr(seed, "acceptance_criteria", ()))
        active_ac_descriptions = [
            str(getattr(criteria[index], "description", criteria[index]))[:200]
            for index in focus.active_ac_indices
            if 0 <= index < len(criteria)
        ]
    await event_store.append(
        lineage_generation_started(
            lineage_id,
            generation_number,
            phase,
            seed.metadata.seed_id,
            json.dumps(seed.to_dict()),
            active_ac_indices=(list(focus.active_ac_indices) if focus is not None else None),
            frozen_ac_indices=(list(focus.frozen_ac_indices) if focus is not None else None),
            active_ac_descriptions=active_ac_descriptions,
            focus_reason=(focus.reason if focus is not None else None),
        )
    )


async def emit_execution_started_once(
    event_store: EventStore,
    lineage_id: str,
    generation_number: int,
    seed: Seed,
    focus: EvolutionFocus,
) -> None:
    """Persist the final execution working set for a generation."""
    await emit_generation_started_once(
        event_store,
        lineage_id=lineage_id,
        generation_number=generation_number,
        phase=GenerationPhase.EXECUTING.value,
        seed=seed,
        focus=focus,
    )


def evolve_request_key(
    initial_seed: Seed | None,
    *,
    execute: bool,
    parallel: bool,
    conductor_directive: ConductorDirective | None,
    project_dir: str | None = None,
    generation_number: int | None = None,
    execution_policy: Mapping[str, Any] | None = None,
) -> str:
    """Identify inputs whose concurrent callers may share one evolve result."""
    return stable_payload_digest(
        {
            "initial_seed": initial_seed.to_dict() if initial_seed is not None else None,
            "execute": execute,
            "parallel": parallel,
            "project_dir": project_dir,
            "generation_number": generation_number,
            "execution_policy": dict(execution_policy) if execution_policy is not None else None,
            "conductor_directive": (
                conductor_directive.to_event_data() if conductor_directive is not None else None
            ),
        }
    )


def _release_lineage_flight(
    authority: str,
    flight_key: tuple[str, str],
    flight: _LineageFlight[Any],
) -> None:
    flights = _lineage_flights.get(authority)
    if flights is None or flights.get(flight_key) is not flight:
        return
    flights.pop(flight_key, None)
    if not flights:
        _lineage_flights.pop(authority, None)


def _finish_lineage_flight(
    task: asyncio.Task[Any],
    authority: str,
    flight_key: tuple[str, str],
    flight: _LineageFlight[Any],
) -> None:
    _release_lineage_flight(authority, flight_key, flight)
    if not task.cancelled():
        task.exception()


async def _await_lineage_flight[T](flight: _LineageFlight[T]) -> T:
    """Await a shared task while preserving sole-caller cancellation."""
    flight.waiters += 1
    caller_cancelled = False
    try:
        return await asyncio.shield(flight.task)
    except asyncio.CancelledError as cancellation:
        caller_cancelled = True
        flight.waiters -= 1
        if flight.waiters == 0 and not flight.task.done():
            flight.task.cancel()
            while not flight.task.done():
                try:
                    await asyncio.shield(flight.task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        raise cancellation
    finally:
        if not caller_cancelled:
            flight.waiters -= 1


async def run_lineage_single_flight[T](
    event_store: EventStore,
    lineage_id: str,
    request_key: str,
    operation: Callable[[], Awaitable[T]],
    *,
    scope: str = "evolve-core",
    replan_on_different: bool = False,
    reject_different: bool = False,
) -> T:
    """Run one process-local writer per lineage and coalesce identical calls.

    A caller with identical inputs observes the winner's exact result. A caller
    with different inputs waits for the durable winner, then retries against the
    newly replayable lineage state. Shielding prevents one duplicate caller from
    cancelling provider work shared with the others.
    """
    authority = _event_store_authority(event_store)
    flight_key = (scope, lineage_id)
    while True:
        flights = _lineage_flights.setdefault(authority, {})
        current = flights.get(flight_key)
        if current is None:
            task: asyncio.Task[T] = asyncio.create_task(operation())
            current = _LineageFlight(request_key=request_key, task=task)
            flights[flight_key] = current
            task.add_done_callback(
                lambda completed, flight=current: _finish_lineage_flight(
                    completed, authority, flight_key, flight
                )
            )
            return await _await_lineage_flight(current)

        if current.request_key == request_key:
            return await _await_lineage_flight(current)

        if reject_different:
            raise LineageFlightConflict(
                f"lineage {lineage_id} is owned by a concurrent evolve_step request"
            )

        try:
            await asyncio.shield(current.task)
        except asyncio.CancelledError:
            if not current.task.cancelled():
                raise
        except Exception:
            pass
        _release_lineage_flight(authority, flight_key, current)
        if replan_on_different:
            raise LineageWinnerAdvanced


async def run_durable_lineage_single_flight[T](
    event_store: EventStore,
    lineage_id: str,
    request_key: str,
    operation: Callable[[], Awaitable[T]],
    *,
    generation_number: int,
    encode: Callable[[T], dict[str, Any]],
    decode: Callable[[Mapping[str, Any]], T | Awaitable[T]],
    scope: str = "evolve-core",
    on_claimed: Callable[[int], Awaitable[None]] | None = None,
    operation_for_generation: Callable[[int], Awaitable[T]] | None = None,
    operation_with_claim: (
        Callable[[int, Callable[[int], Awaitable[None]]], Awaitable[T]] | None
    ) = None,
) -> T:
    """Coordinate one lineage writer across EventStore instances and processes."""
    from ouroboros.persistence import lineage_claims

    if not lineage_claims.supports_durable_claims(event_store):
        return await operation()
    await event_store.initialize()
    while True:
        owner_id = str(uuid4())
        claim = await lineage_claims.try_acquire(
            event_store,
            scope=scope,
            lineage_id=lineage_id,
            generation_number=generation_number,
            owner_id=owner_id,
            request_key=request_key,
        )
        if claim is None:  # pragma: no cover - production claims fail closed.
            raise RuntimeError("Durable lineage claim acquisition returned no authority")
        if claim.completed:
            if (
                claim.generation_number == generation_number
                and claim.request_key == request_key
                and claim.result_payload is not None
            ):
                if on_claimed is not None:
                    await on_claimed(claim.generation_number)
                decoded = decode(claim.result_payload)
                return await decoded if inspect.isawaitable(decoded) else decoded
            if claim.generation_number == generation_number:
                raise LineageWinnerAdvanced
            await asyncio.sleep(0.01)
            continue
        if claim.acquired:
            claimed_generation = claim.generation_number
            heartbeat = asyncio.create_task(
                _renew_claim_until_cancelled(
                    event_store,
                    scope=scope,
                    lineage_id=lineage_id,
                    owner_id=owner_id,
                )
            )
            try:
                if on_claimed is not None:
                    await on_claimed(claimed_generation)

                async def rebind_generation(authoritative_generation: int) -> None:
                    rebound = await lineage_claims.rebind_generation(
                        event_store,
                        scope=scope,
                        lineage_id=lineage_id,
                        owner_id=owner_id,
                        generation_number=authoritative_generation,
                    )
                    if not rebound:
                        raise RuntimeError(
                            "Lost durable lineage advancement claim before generation binding"
                        )

                if operation_with_claim is not None:
                    result = await operation_with_claim(claimed_generation, rebind_generation)
                elif operation_for_generation is not None:
                    result = await operation_for_generation(claimed_generation)
                else:
                    result = await operation()
                if heartbeat.done():
                    heartbeat_error = None if heartbeat.cancelled() else heartbeat.exception()
                    if heartbeat_error is not None:
                        raise RuntimeError(
                            "Durable lineage claim heartbeat failed before completion"
                        ) from heartbeat_error
                    raise RuntimeError("Lost durable lineage advancement claim before completion")
                published = await lineage_claims.complete(
                    event_store,
                    scope=scope,
                    lineage_id=lineage_id,
                    owner_id=owner_id,
                    result_payload=encode(result),
                )
                if not published:
                    raise RuntimeError("Lost durable lineage advancement claim before completion")
                return result
            except BaseException:
                await lineage_claims.release(
                    event_store,
                    scope=scope,
                    lineage_id=lineage_id,
                    owner_id=owner_id,
                )
                raise
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # A committed receipt is authoritative. A heartbeat failure
                    # observed before commit is raised above; one racing after a
                    # successful commit must not replace that durable outcome.
                    pass

        if not claim.waiter_registered:
            await asyncio.sleep(0.01)
            continue

        waiter_id = claim.waiter_id
        if waiter_id is None:
            raise RuntimeError("Durable lineage waiter registration has no identity")
        waiter_heartbeat = asyncio.create_task(
            _renew_waiter_until_cancelled(
                event_store,
                scope=scope,
                lineage_id=lineage_id,
                owner_id=claim.owner_id,
                waiter_id=waiter_id,
            )
        )
        try:
            while True:
                if waiter_heartbeat.done():
                    heartbeat_error = (
                        None if waiter_heartbeat.cancelled() else waiter_heartbeat.exception()
                    )
                    if heartbeat_error is not None:
                        raise RuntimeError(
                            "Durable lineage waiter heartbeat failed before receipt replay"
                        ) from heartbeat_error
                    raise RuntimeError("Lost durable lineage waiter registration")
                winner = await lineage_claims.observe(
                    event_store,
                    scope=scope,
                    lineage_id=lineage_id,
                )
                if winner is None or winner.owner_id != claim.owner_id:
                    raise LineageWinnerAdvanced
                if not winner.completed and winner.lease_expires_at_ms <= int(time.time() * 1000):
                    raise RuntimeError(
                        "Durable lineage owner lease expired; confirm the owner process is dead, "
                        "then rerun ouroboros_evolve_step with recover_expired_claim=true"
                    )
                if winner.completed:
                    if winner.request_key == request_key and winner.result_payload is not None:
                        if on_claimed is not None:
                            await on_claimed(winner.generation_number)
                        decoded = decode(winner.result_payload)
                        return await decoded if inspect.isawaitable(decoded) else decoded
                    raise LineageWinnerAdvanced
                await asyncio.sleep(0.01)
        finally:
            waiter_heartbeat.cancel()
            try:
                await waiter_heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            await lineage_claims.acknowledge_waiter(
                event_store,
                scope=scope,
                lineage_id=lineage_id,
                owner_id=claim.owner_id,
                waiter_id=waiter_id,
            )


async def _renew_claim_until_cancelled(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> None:
    from ouroboros.persistence import lineage_claims

    while True:
        await asyncio.sleep(lineage_claims.DEFAULT_LEASE_SECONDS / 3)
        if not await lineage_claims.renew(
            event_store,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
        ):
            raise RuntimeError("Lost durable lineage advancement claim during renewal")


async def _renew_waiter_until_cancelled(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    waiter_id: str,
) -> None:
    """Heartbeat one waiter registration until its winner receipt is consumed."""
    from ouroboros.persistence import lineage_claims

    while True:
        await asyncio.sleep(lineage_claims.WAITER_LEASE_SECONDS / 3)
        if not await lineage_claims.renew_waiter(
            event_store,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            waiter_id=waiter_id,
        ):
            raise RuntimeError("Lost durable lineage waiter registration during renewal")


async def planned_evolve_generation(
    event_store: EventStore,
    lineage_id: str,
    *,
    execute: bool,
) -> int:
    """Return the generation identity a fresh evolve request would advance."""
    from ouroboros.core.lineage import GenerationPhase, LineageStatus
    from ouroboros.evolution.projector import LineageProjector

    events = await event_store.replay_lineage(lineage_id)
    if not events:
        return 1
    projector = LineageProjector()
    lineage = projector.project(events)
    if lineage is None:
        raise ValueError("Failed to project lineage for advancement claim")
    if lineage.status in {LineageStatus.CONVERGED, LineageStatus.EXHAUSTED}:
        return lineage.current_generation
    last_generation, last_phase, _ = projector.find_resume_point(events)
    if lineage.verification_handoff_pending and not execute:
        return lineage.current_generation
    if last_phase != GenerationPhase.COMPLETED:
        return last_generation
    return last_generation + 1


async def refresh_lineage(
    event_store: EventStore,
    fallback: OntologyLineage,
) -> OntologyLineage:
    """Project the durable state written by a failed or interrupted generation."""
    from ouroboros.evolution.projector import LineageProjector

    projected = LineageProjector().project(await event_store.replay_lineage(fallback.lineage_id))
    return projected or fallback


def generation_parent_seed(current_seed: Seed, previous: GenerationRecord | None) -> Seed:
    """Restore the completed parent that owns the previous evaluation."""
    if previous is None or not isinstance(previous.seed_json, str) or not previous.seed_json:
        return current_seed
    try:
        return Seed.from_dict(json.loads(previous.seed_json))
    except Exception as exc:
        raise ValueError(
            f"Failed to reconstruct generation parent Seed from seed_json: {exc}"
        ) from exc


def unfinished_generation_seed(
    lineage: OntologyLineage,
    generation_number: int,
) -> Seed:
    """Restore the durable starting Seed for a hard-crashed generation."""
    unfinished = next(
        (
            generation
            for generation in reversed(lineage.generations)
            if generation.generation_number == generation_number
        ),
        None,
    )
    if unfinished is None or not unfinished.seed_json:
        raise ValueError(
            "Cannot recover unfinished generation: its durable starting Seed is unavailable"
        )
    try:
        return Seed.from_dict(json.loads(unfinished.seed_json))
    except Exception as exc:
        raise ValueError(
            f"Failed to reconstruct unfinished generation seed from seed_json: {exc}"
        ) from exc


def recovery_plan(
    lineage: OntologyLineage,
    last_generation: int,
    last_phase: GenerationPhase,
) -> tuple[int, Seed | None]:
    """Keep unfinished generation identity and recover hard-crash Seed authority."""
    if last_phase == GenerationPhase.COMPLETED:
        return last_generation + 1, None
    if last_phase in {GenerationPhase.FAILED, GenerationPhase.INTERRUPTED}:
        return last_generation, None
    return last_generation, unfinished_generation_seed(lineage, last_generation)


def watchdog_timeout_action(timeout_kind: str) -> str:
    """Map a watchdog timeout to its public evolve action value."""
    from ouroboros.evolution.directive_mapping import watchdog_timeout_to_directive

    directive = watchdog_timeout_to_directive(timeout_kind)
    if directive is None:
        return "failed"
    return "exhausted" if is_terminal_directive(directive) else "stagnated"


def watchdog_has_directive_metadata(details: Mapping[str, object]) -> bool:
    """Return whether watchdog evidence identifies the claimed execution."""
    execution_id = details.get("execution_id")
    return isinstance(execution_id, str) and bool(execution_id)


async def phase_for_failed_step_directive(
    event_store: EventStore,
    *,
    lineage_id: str,
    generation_number: int,
) -> str:
    """Recover the real failed phase, looking past watchdog cancellation."""
    events = await event_store.replay_lineage(lineage_id)
    saw_cancelled_failure = False
    for event in reversed(events):
        if event.data.get("generation_number") != generation_number:
            continue
        if event.type == "lineage.generation.failed":
            phase = event.data.get("phase")
            if isinstance(phase, str) and phase:
                if phase != GenerationPhase.CANCELLED.value:
                    return phase
                saw_cancelled_failure = True
                continue
        if saw_cancelled_failure and event.type in {
            "lineage.generation.phase_changed",
            "lineage.generation.started",
        }:
            phase = event.data.get("phase")
            if isinstance(phase, str) and phase:
                return phase
    return GenerationPhase.FAILED.value


async def emit_step_directive(
    event_store: EventStore,
    action: str,
    *,
    lineage_id: str,
    generation_number: int,
    phase: str,
    reason: str,
    retry_budget_remaining: int = 1,
) -> None:
    """Persist the shared control directive for one evolution outcome."""
    directive = step_action_to_directive(action, retry_budget_remaining=retry_budget_remaining)
    if directive is None:
        return
    await event_store.append(
        create_control_directive_emitted_event(
            target_type="lineage",
            target_id=lineage_id,
            emitted_by="evolver",
            directive=directive,
            reason=reason,
            lineage_id=lineage_id,
            generation_number=generation_number,
            phase=phase,
            extra={"step_action": action, "is_terminal": is_terminal_directive(directive)},
        )
    )


def conductor_preservation_error(
    approved: Seed,
    successor: Seed,
    directive: ConductorDirective | None,
) -> str | None:
    """Return a fail-closed invariant error for an unauthorized direction change."""
    if directive is None:
        return None
    changed: list[str] = []
    if directive.preserve_goal and approved.goal != successor.goal:
        changed.append("goal")
    if (
        directive.preserve_acceptance_criteria
        and approved.acceptance_criteria != successor.acceptance_criteria
    ):
        changed.append("acceptance_criteria")
    if directive.preserve_constraints and approved.constraints != successor.constraints:
        changed.append("constraints")
    if directive.preserve_non_goals and approved.to_dict().get(
        "non_goals"
    ) != successor.to_dict().get("non_goals"):
        changed.append("non_goals")
    if not changed:
        return None
    return "Conductor successor changed preserved direction fields: " + ", ".join(changed)
