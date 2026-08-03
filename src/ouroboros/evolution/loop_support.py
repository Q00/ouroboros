"""Small deterministic helpers shared by the evolution loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
import inspect
import json
import time
from typing import Any
from uuid import uuid4

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.conductor import ConductorDirective, stable_payload_digest
from ouroboros.core.lineage import GenerationPhase, GenerationRecord, OntologyLineage
from ouroboros.core.seed import Seed
from ouroboros.events.control import create_control_directive_emitted_event
from ouroboros.events.lineage import lineage_generation_started
from ouroboros.evolution.directive_mapping import (
    is_terminal_directive,
    step_action_to_directive,
)
from ouroboros.persistence.event_store import EventStore


class LineageWinnerAdvanced(RuntimeError):
    """The caller must recompute its request from the durable winner state."""


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


def evolution_execution_policy(config: Any | None) -> dict[str, Any] | None:
    """Return every declared evolution config field for durable request identity."""
    if config is None:
        return None
    projected = _execution_policy_value(config)
    if not isinstance(projected, dict):
        raise TypeError("Evolution execution policy must project to an object")
    return projected


async def emit_generation_started_once(
    event_store: EventStore,
    *,
    lineage_id: str,
    generation_number: int,
    phase: str,
    seed: Seed,
) -> None:
    """Persist the sole started event while allowing later attempts to resume."""
    events = await event_store.replay_lineage(lineage_id)
    if any(
        event.type == "lineage.generation.started"
        and event.data.get("generation_number") == generation_number
        for event in events
    ):
        return
    await event_store.append(
        lineage_generation_started(
            lineage_id,
            generation_number,
            phase,
            seed.metadata.seed_id,
            json.dumps(seed.to_dict()),
        )
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
                decoded = decode(claim.result_payload)
                return await decoded if inspect.isawaitable(decoded) else decoded
            if claim.generation_number == generation_number:
                raise LineageWinnerAdvanced
            await asyncio.sleep(0.01)
            continue
        if claim.acquired:
            heartbeat = asyncio.create_task(
                _renew_claim_until_cancelled(
                    event_store,
                    scope=scope,
                    lineage_id=lineage_id,
                    owner_id=owner_id,
                )
            )
            try:
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

        try:
            while True:
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
                        decoded = decode(winner.result_payload)
                        return await decoded if inspect.isawaitable(decoded) else decoded
                    raise LineageWinnerAdvanced
                await asyncio.sleep(0.01)
        finally:
            await lineage_claims.acknowledge_waiter(
                event_store,
                scope=scope,
                lineage_id=lineage_id,
                owner_id=claim.owner_id,
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
