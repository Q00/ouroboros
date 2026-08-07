"""Feedback-loop tests for provider adaptive concurrency control."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import anyio
import pytest

from ouroboros.orchestrator.adapter import AgentMessage
import ouroboros.orchestrator.adaptive_concurrency as adaptive_concurrency
from ouroboros.orchestrator.adaptive_concurrency import (
    MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS,
    AdaptiveConcurrencyController,
    BackendPressureKind,
    ConcurrencyObservation,
    classify_backend_pressure,
    observe_ac_result,
)
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult


def _success_message() -> AgentMessage:
    return AgentMessage(type="result", content="done", data={"subtype": "success"})


def _error_message(content: str, **data: Any) -> AgentMessage:
    return AgentMessage(
        type="result",
        content=content,
        data={"subtype": "error", **data},
    )


def test_successful_provider_completion_is_capacity_success() -> None:
    observation = classify_backend_pressure([_success_message()], provider_completed=True)

    assert observation == ConcurrencyObservation(BackendPressureKind.SUCCESS)


def test_generic_short_window_429_is_concurrency_pressure() -> None:
    observation = classify_backend_pressure(
        [_error_message("HTTP 429", http_status=429, retry_after_seconds=30)],
        provider_completed=True,
    )

    assert observation == ConcurrencyObservation(
        BackendPressureKind.CONCURRENCY_REJECTION,
        retry_after_seconds=30,
    )


def test_explicit_quota_exhaustion_is_not_concurrency_pressure() -> None:
    observation = classify_backend_pressure(
        [
            _error_message(
                "Usage quota exhausted until reset",
                http_status=429,
                quota_exhausted=True,
                retry_after_seconds=7_200,
            )
        ],
        provider_completed=True,
    )

    assert observation.kind is BackendPressureKind.QUOTA_EXHAUSTION
    assert observation.retry_after_seconds == 7_200


def test_explicit_concurrency_rejection_wins_over_long_generic_429_window() -> None:
    observation = classify_backend_pressure(
        [
            _error_message(
                "Interface request concurrency exceeded",
                http_status=429,
                kind="concurrency_limit",
                retry_after_seconds=3_600,
            )
        ],
        provider_completed=True,
    )

    assert observation.kind is BackendPressureKind.CONCURRENCY_REJECTION
    assert observation.retry_after_seconds == 3_600


def test_nested_retry_after_header_drives_pressure_cooldown() -> None:
    observation = classify_backend_pressure(
        [
            _error_message(
                "Too many concurrent requests",
                http_status=429,
                headers={"Retry-After": "5"},
            )
        ],
        provider_completed=True,
    )

    assert observation.retry_after_seconds == 5


@pytest.mark.parametrize(
    "headers",
    (
        {"rEtRy-AfTeR": "5"},
        {"RETRY-AFTER": "Wed, 05 Aug 2026 09:00:00 GMT"},
    ),
)
@pytest.mark.asyncio
async def test_retry_after_header_forms_pause_controller_entrance(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    monkeypatch.setattr(
        adaptive_concurrency,
        "_utc_now",
        lambda: datetime(2026, 8, 5, 8, 59, 55, tzinfo=UTC),
    )
    observation = classify_backend_pressure(
        [
            _error_message(
                "Too many concurrent requests",
                http_status=429,
                headers=headers,
            )
        ],
        provider_completed=True,
    )
    clock = {"now": 100.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    controller = AdaptiveConcurrencyController(
        initial_limit=1,
        clock=lambda: clock["now"],
        sleep=fake_sleep,
    )
    async with controller.slot() as epoch:
        await controller.observe(observation, permit_epoch=epoch)
    async with controller.slot():
        pass

    assert observation.retry_after_seconds == 5
    assert sleeps == [5.0]


def test_hostile_retry_after_is_saturated_before_clock_arithmetic() -> None:
    observation = classify_backend_pressure(
        [
            _error_message(
                "Interface request concurrency exceeded",
                kind="concurrency_limit",
                retry_after_seconds=10**1000,
            )
        ],
        provider_completed=True,
    )

    assert observation == ConcurrencyObservation(
        BackendPressureKind.CONCURRENCY_REJECTION,
        retry_after_seconds=MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS,
    )


def test_unrelated_provider_failure_is_neutral() -> None:
    observation = classify_backend_pressure(
        [_error_message("Compilation failed", error_type="RuntimeExecutionError")],
        provider_completed=True,
    )

    assert observation.kind is BackendPressureKind.NEUTRAL_FAILURE


@pytest.mark.asyncio
async def test_sustained_success_additively_grows_to_ceiling() -> None:
    controller = AdaptiveConcurrencyController(
        initial_limit=1,
        max_limit=3,
        successes_before_increase=2,
    )

    for _ in range(4):
        async with controller.slot() as epoch:
            await controller.observe(
                ConcurrencyObservation(BackendPressureKind.SUCCESS),
                permit_epoch=epoch,
            )

    snapshot = controller.snapshot()
    assert snapshot.current_limit == 3
    assert snapshot.max_limit == 3


@pytest.mark.asyncio
async def test_rejection_halves_window_and_stale_success_cannot_undo_it() -> None:
    controller = AdaptiveConcurrencyController(initial_limit=4, max_limit=8)

    async with controller.slot() as old_epoch:
        after_rejection = await controller.observe(
            ConcurrencyObservation(BackendPressureKind.CONCURRENCY_REJECTION),
            permit_epoch=old_epoch,
        )
        after_stale_success = await controller.observe(
            ConcurrencyObservation(BackendPressureKind.SUCCESS),
            permit_epoch=old_epoch,
        )

    assert after_rejection.current_limit == 2
    assert after_stale_success.current_limit == 2
    assert after_stale_success.success_streak == 0


@pytest.mark.asyncio
async def test_retry_after_pauses_new_entrances_then_resumes() -> None:
    clock = {"now": 100.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    controller = AdaptiveConcurrencyController(
        initial_limit=2,
        max_limit=4,
        clock=lambda: clock["now"],
        sleep=fake_sleep,
    )
    async with controller.slot() as epoch:
        await controller.observe(
            ConcurrencyObservation(
                BackendPressureKind.CONCURRENCY_REJECTION,
                retry_after_seconds=7,
            ),
            permit_epoch=epoch,
        )

    async with controller.slot():
        pass

    assert sleeps == [7.0]
    assert controller.snapshot().cooldown_remaining_seconds == 0


@pytest.mark.asyncio
async def test_cancelled_slot_releases_permit_before_propagating_cancellation() -> None:
    controller = AdaptiveConcurrencyController(initial_limit=1)
    entered = anyio.Event()

    async def hold_slot() -> None:
        async with controller.slot():
            entered.set()
            await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(hold_slot)
        await entered.wait()
        assert controller.snapshot().in_flight == 1
        task_group.cancel_scope.cancel()

    assert controller.snapshot().in_flight == 0
    with anyio.fail_after(1):
        async with controller.slot():
            pass


@pytest.mark.asyncio
async def test_quota_invalidates_success_epoch_without_shrinking_window() -> None:
    controller = AdaptiveConcurrencyController(initial_limit=3, max_limit=6)

    async with controller.slot() as epoch:
        snapshot = await controller.observe(
            ConcurrencyObservation(BackendPressureKind.QUOTA_EXHAUSTION),
            permit_epoch=epoch,
        )

    assert snapshot.current_limit == 3
    assert snapshot.pressure_epoch == 1


@pytest.mark.asyncio
async def test_decomposed_result_pressure_reaches_live_controller() -> None:
    controller = AdaptiveConcurrencyController(initial_limit=4, max_limit=6)
    result = ACExecutionResult(
        ac_index=0,
        ac_content="root",
        success=False,
        sub_results=(
            ACExecutionResult(
                ac_index=100,
                ac_content="leaf",
                success=False,
                messages=(
                    _error_message(
                        "Interface request concurrency exceeded",
                        http_status=429,
                    ),
                ),
            ),
        ),
    )

    async with controller.slot() as epoch:
        snapshot = await observe_ac_result(
            controller,
            result,
            epoch,
            ("session", "execution", 0),
        )

    assert snapshot.current_limit == 2
    assert snapshot.pressure_epoch == 1


@pytest.mark.parametrize(
    ("initial", "maximum"),
    [(0, 1), (-1, 1), (2, 1)],
)
def test_invalid_window_is_rejected(initial: int, maximum: int) -> None:
    with pytest.raises(ValueError):
        AdaptiveConcurrencyController(initial_limit=initial, max_limit=maximum)


def test_static_policy_excludes_live_window_state() -> None:
    controller = AdaptiveConcurrencyController(initial_limit=1, max_limit=4)

    assert controller.policy == {
        "algorithm": "aimd/v2",
        "admission_scope": "provider_call",
        "initial_limit": 1,
        "max_limit": 4,
        "successes_before_increase": 3,
        "decrease_ratio": "1/2",
        "max_retry_after_seconds": MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS,
    }
