"""Signal-driven concurrency control for provider dispatch.

The static backend-limit resolver supplies a conservative *initial estimate*.
This controller then treats completed provider calls as feedback:

* a 429/rate-pressure rejection halves the dispatch window;
* ``Retry-After`` creates a bounded cooldown before another provider entrance;
* sustained successful provider completions grow the window by one; and
* an explicit usage/quota exhaustion signal is left to the durable execution
  pause owner instead of being misclassified as ordinary concurrency pressure.

The algorithm is intentionally AIMD-like.  It probes cautiously, reacts quickly
to pressure, and never cancels completed work or turns capacity policy into an
outcome/quality decision.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import math
import re
import time

import anyio

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.orchestrator.recoverable_failure import (
    is_usage_limit_pause_message,
    project_failure_metadata,
    retry_duration_seconds_from_message,
)

_HTTP_STATUS_FIELDS = (
    "http_status",
    "httpStatus",
    "http_status_code",
    "httpStatusCode",
    "status_code",
    "statusCode",
    "api_error_status",
    "apiErrorStatus",
    "status",
    "code",
    "error_code",
    "errorCode",
)
_PRESSURE_TEXT_FIELDS = (
    "error_type",
    "error_code",
    "code",
    "type",
    "reason",
    "message",
    "kind",
)
_CONCURRENCY_PRESSURE_PATTERN = re.compile(
    r"\b(?:too\s+many\s+(?:concurrent\s+)?requests"
    r"|(?:request\s+)?concurrenc(?:y|ies)\s+(?:limit|cap|maximum|exceeded|reached)"
    r"|(?:limit|cap|maximum)\s+(?:for\s+)?concurrent\s+requests?"
    r"|concurrent\s+requests?\s+(?:exceeded|rejected)"
    r"|rate\s*limit(?:ed|\s+(?:exceeded|reached))?)\b",
    re.IGNORECASE,
)

log = get_logger(__name__)

ADAPTIVE_CONCURRENCY_ALGORITHM = "aimd/v2"
ADAPTIVE_CONCURRENCY_ADMISSION_SCOPE = "provider_call"
ADAPTIVE_CONCURRENCY_SUCCESSES_BEFORE_INCREASE = 3
ADAPTIVE_CONCURRENCY_DECREASE_RATIO = "1/2"
MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS = 24 * 60 * 60


class BackendPressureKind(StrEnum):
    """One provider-capacity observation at an AC dispatch boundary."""

    SUCCESS = "success"
    CONCURRENCY_REJECTION = "concurrency_rejection"
    QUOTA_EXHAUSTION = "quota_exhaustion"
    NEUTRAL_FAILURE = "neutral_failure"


@dataclass(frozen=True, slots=True)
class ConcurrencyObservation:
    """Normalized provider feedback consumed by the AIMD controller."""

    kind: BackendPressureKind
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class AdaptiveConcurrencySnapshot:
    """Read-only controller state for logs, tests, and operator telemetry."""

    initial_limit: int
    current_limit: int
    max_limit: int
    in_flight: int
    success_streak: int
    pressure_epoch: int
    cooldown_remaining_seconds: float


def _metadata_has_http_429(metadata: Mapping[str, object]) -> bool:
    for key in _HTTP_STATUS_FIELDS:
        value = metadata.get(key)
        if type(value) is int and value == 429:
            return True
        if type(value) is str and value.strip() == "429":
            return True
    return False


def _metadata_pressure_text(metadata: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in _PRESSURE_TEXT_FIELDS:
        value = metadata.get(key)
        if isinstance(value, str):
            parts.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value))
    return " ".join(parts).replace("_", " ").replace("-", " ")


def _is_concurrency_pressure_message(message: AgentMessage) -> bool:
    if not (message.is_final and message.is_error):
        return False
    metadata_rows, metadata_overflowed = project_failure_metadata(message)
    if metadata_overflowed:
        # The quota classifier owns ambiguous/overflowing final failures and
        # fails closed as a pause.  Do not create a competing retry authority.
        return False
    if any(_metadata_has_http_429(metadata) for metadata in metadata_rows):
        return True
    if any(
        _CONCURRENCY_PRESSURE_PATTERN.search(_metadata_pressure_text(metadata)) is not None
        for metadata in metadata_rows
    ):
        return True
    return _CONCURRENCY_PRESSURE_PATTERN.search(message.content) is not None


def classify_backend_pressure(
    messages: Iterable[AgentMessage],
    *,
    provider_completed: bool,
) -> ConcurrencyObservation:
    """Classify one completed AC attempt without conflating quota and pressure.

    Quota classification has priority.  A generic/short-window 429 then becomes
    concurrency pressure, while unrelated execution or verification failures do
    not teach the capacity controller anything.  A successful final provider
    response counts as success even if a later deterministic AC gate rejects the
    artifact: backend capacity and product quality are separate signals.
    """

    bounded_messages = tuple(message for message in messages if isinstance(message, AgentMessage))
    quota_messages = tuple(
        message for message in bounded_messages if is_usage_limit_pause_message(message)
    )
    if quota_messages:
        retry_after = _bounded_retry_after_seconds(
            max(
                (
                    retry_duration_seconds_from_message(message, now=_utc_now()) or 0
                    for message in quota_messages
                ),
                default=0,
            )
        )
        return ConcurrencyObservation(
            BackendPressureKind.QUOTA_EXHAUSTION,
            retry_after_seconds=retry_after,
        )

    pressure_messages = tuple(
        message for message in bounded_messages if _is_concurrency_pressure_message(message)
    )
    if pressure_messages:
        retry_after = _bounded_retry_after_seconds(
            max(
                (
                    retry_duration_seconds_from_message(message, now=_utc_now()) or 0
                    for message in pressure_messages
                ),
                default=0,
            )
        )
        return ConcurrencyObservation(
            BackendPressureKind.CONCURRENCY_REJECTION,
            retry_after_seconds=retry_after,
        )

    successful_final = any(
        message.is_final and not message.is_error for message in bounded_messages
    )
    if provider_completed and successful_final:
        return ConcurrencyObservation(BackendPressureKind.SUCCESS)
    return ConcurrencyObservation(BackendPressureKind.NEUTRAL_FAILURE)


def has_usage_limit_pause(result: ACExecutionResult) -> bool:
    """Return whether any leaf in an AC result tree carries a quota pause."""

    if any(is_usage_limit_pause_message(message) for message in reversed(result.messages)):
        return True
    return any(has_usage_limit_pause(child) for child in result.sub_results)


def _capacity_feedback_messages(result: ACExecutionResult) -> tuple[AgentMessage, ...]:
    messages = list(result.messages)
    for child in result.sub_results:
        messages.extend(_capacity_feedback_messages(child))
    return tuple(messages)


def _utc_now() -> datetime:
    # Kept behind a tiny seam so classification remains deterministic in tests
    # that exercise absolute retry timestamps through the shared parser.
    return datetime.now(UTC)


def _bounded_retry_after_seconds(value: object) -> int | None:
    """Normalize untrusted provider cooldown metadata to a safe finite bound."""
    if type(value) is not int or value <= 0:
        return None
    return min(value, MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS)


def adaptive_concurrency_policy(
    *,
    initial_limit: int,
    max_limit: int,
    successes_before_increase: int = ADAPTIVE_CONCURRENCY_SUCCESSES_BEFORE_INCREASE,
) -> dict[str, object]:
    """Return the complete static policy that changes provider admission effects."""
    return {
        "algorithm": ADAPTIVE_CONCURRENCY_ALGORITHM,
        "admission_scope": ADAPTIVE_CONCURRENCY_ADMISSION_SCOPE,
        "initial_limit": initial_limit,
        "max_limit": max_limit,
        "successes_before_increase": successes_before_increase,
        "decrease_ratio": ADAPTIVE_CONCURRENCY_DECREASE_RATIO,
        "max_retry_after_seconds": MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS,
    }


class AdaptiveConcurrencyController:
    """Cancellation-safe AIMD window around provider entrances."""

    ALGORITHM = ADAPTIVE_CONCURRENCY_ALGORITHM

    def __init__(
        self,
        *,
        initial_limit: int,
        max_limit: int | None = None,
        successes_before_increase: int = ADAPTIVE_CONCURRENCY_SUCCESSES_BEFORE_INCREASE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = anyio.sleep,
    ) -> None:
        if type(initial_limit) is not int or initial_limit < 1:
            raise ValueError("initial concurrency limit must be a positive integer")
        resolved_max_limit = initial_limit if max_limit is None else max_limit
        if type(resolved_max_limit) is not int or resolved_max_limit < initial_limit:
            raise ValueError("adaptive concurrency ceiling must be >= the initial limit")
        if type(successes_before_increase) is not int or successes_before_increase < 1:
            raise ValueError("success growth threshold must be a positive integer")

        self._initial_limit = initial_limit
        self._current_limit = initial_limit
        self._max_limit = resolved_max_limit
        self._successes_before_increase = successes_before_increase
        self._clock = clock
        self._sleep = sleep
        self._condition = anyio.Condition()
        self._in_flight = 0
        self._success_streak = 0
        self._pressure_epoch = 0
        self._cooldown_until = 0.0

    @property
    def initial_limit(self) -> int:
        return self._initial_limit

    @property
    def max_limit(self) -> int:
        return self._max_limit

    @property
    def policy(self) -> dict[str, object]:
        """Return immutable controller semantics without live window state."""

        return adaptive_concurrency_policy(
            initial_limit=self._initial_limit,
            max_limit=self._max_limit,
            successes_before_increase=self._successes_before_increase,
        )

    def snapshot(self) -> AdaptiveConcurrencySnapshot:
        """Return a best-effort instantaneous snapshot without blocking dispatch."""

        return AdaptiveConcurrencySnapshot(
            initial_limit=self._initial_limit,
            current_limit=self._current_limit,
            max_limit=self._max_limit,
            in_flight=self._in_flight,
            success_streak=self._success_streak,
            pressure_epoch=self._pressure_epoch,
            cooldown_remaining_seconds=max(0.0, self._cooldown_until - self._clock()),
        )

    async def _acquire(self) -> int:
        while True:
            cooldown = 0.0
            async with self._condition:
                now = self._clock()
                cooldown = max(0.0, self._cooldown_until - now)
                if cooldown <= 0 and self._in_flight < self._current_limit:
                    self._in_flight += 1
                    return self._pressure_epoch
                if cooldown <= 0:
                    await self._condition.wait()
                    continue
            # Sleep outside the condition so releases and newer pressure signals
            # can update state while this waiter observes the reset boundary.
            await self._sleep(cooldown)

    async def _release(self) -> None:
        async with self._condition:
            if self._in_flight < 1:
                raise RuntimeError("adaptive concurrency permit released twice")
            self._in_flight -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[int]:
        """Yield the pressure epoch attached to one provider entrance permit."""

        epoch = await self._acquire()
        try:
            yield epoch
        finally:
            # The holder is commonly unwound from an already-cancelled sibling
            # scope. Permit accounting is reusable controller state, so cleanup
            # must finish before cancellation is allowed to propagate.
            with anyio.CancelScope(shield=True):
                await self._release()

    async def observe(
        self,
        observation: ConcurrencyObservation,
        *,
        permit_epoch: int,
    ) -> AdaptiveConcurrencySnapshot:
        """Apply one capacity observation and return the resulting state."""

        async with self._condition:
            kind = observation.kind
            if kind is BackendPressureKind.CONCURRENCY_REJECTION:
                self._pressure_epoch += 1
                self._success_streak = 0
                self._current_limit = max(1, math.floor(self._current_limit / 2))
                retry_after = _bounded_retry_after_seconds(observation.retry_after_seconds)
                if retry_after is not None:
                    self._cooldown_until = max(
                        self._cooldown_until,
                        self._clock() + float(retry_after),
                    )
            elif kind is BackendPressureKind.QUOTA_EXHAUSTION:
                # The runner owns durable pause/resume.  Invalidate successes from
                # the old pressure epoch, but do not turn quota into an AIMD retry.
                self._pressure_epoch += 1
                self._success_streak = 0
            elif kind is BackendPressureKind.SUCCESS:
                # Completions already in flight when a rejection arrived must not
                # immediately undo the multiplicative decrease.
                if permit_epoch == self._pressure_epoch and self._clock() >= self._cooldown_until:
                    self._success_streak += 1
                    if self._success_streak >= self._successes_before_increase:
                        self._current_limit = min(self._max_limit, self._current_limit + 1)
                        self._success_streak = 0
            else:
                self._success_streak = 0
            self._condition.notify_all()
            return self.snapshot()


async def observe_ac_result(
    controller: AdaptiveConcurrencyController,
    result: ACExecutionResult,
    permit_epoch: int,
    log_context: tuple[str, str, int],
) -> AdaptiveConcurrencySnapshot:
    """Feed one possibly decomposed AC result into the controller and log changes."""

    return await observe_provider_messages(
        controller,
        _capacity_feedback_messages(result),
        permit_epoch,
        log_context,
        provider_completed=True,
    )


async def observe_provider_messages(
    controller: AdaptiveConcurrencyController,
    messages: Iterable[AgentMessage],
    permit_epoch: int,
    log_context: tuple[str, str, int] | None,
    *,
    provider_completed: bool,
    on_observation: Callable[[ConcurrencyObservation], None] | None = None,
) -> AdaptiveConcurrencySnapshot:
    """Feed one exact provider call into the controller and log changes.

    Admission and feedback belong to provider calls, not composite AC roots.  A
    decomposed root can issue several sequential calls, and each completed call
    must update cooldown/window state before its next child is admitted.
    """

    observation = classify_backend_pressure(
        messages,
        provider_completed=provider_completed,
    )
    before = controller.snapshot()
    after = await controller.observe(observation, permit_epoch=permit_epoch)
    if on_observation is not None:
        on_observation(observation)
    if before.current_limit != after.current_limit or observation.kind in {
        BackendPressureKind.CONCURRENCY_REJECTION,
        BackendPressureKind.QUOTA_EXHAUSTION,
    }:
        session_id, execution_id, ac_index = log_context or (None, None, None)
        log.info(
            "parallel_executor.adaptive_concurrency_observed",
            session_id=session_id,
            execution_id=execution_id,
            ac_index=ac_index,
            signal=observation.kind.value,
            previous_limit=before.current_limit,
            current_limit=after.current_limit,
            max_limit=after.max_limit,
            retry_after_seconds=observation.retry_after_seconds,
            cooldown_remaining_seconds=after.cooldown_remaining_seconds,
            pressure_epoch=after.pressure_epoch,
        )
    return after


__all__ = [
    "ADAPTIVE_CONCURRENCY_ALGORITHM",
    "ADAPTIVE_CONCURRENCY_ADMISSION_SCOPE",
    "ADAPTIVE_CONCURRENCY_DECREASE_RATIO",
    "ADAPTIVE_CONCURRENCY_SUCCESSES_BEFORE_INCREASE",
    "AdaptiveConcurrencyController",
    "AdaptiveConcurrencySnapshot",
    "BackendPressureKind",
    "ConcurrencyObservation",
    "MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS",
    "adaptive_concurrency_policy",
    "classify_backend_pressure",
    "has_usage_limit_pause",
    "observe_ac_result",
    "observe_provider_messages",
]
