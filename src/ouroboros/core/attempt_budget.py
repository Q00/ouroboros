"""Finite budgets for one atomic acceptance-criterion attempt.

The executor cannot rely on every agent runtime exposing the same native
``max_turns`` knob. It therefore enforces two runtime-neutral boundaries at
the stream owner: a wall-clock deadline and cancellation at the first observed
over-budget tool-bearing turn. Some runtimes expose a tool request before its
effect and others report it afterward, so the portable guarantee is that no
later turn is admitted. Text-only progress remains bounded by the deadline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import math

DEFAULT_MAX_ITERATIONS_PER_AC = 10
DEFAULT_AC_ATTEMPT_TIMEOUT_SECONDS = 900.0
ATTEMPT_BUDGET_PROGRESS_SCHEMA_VERSION = 1
_MICROSECONDS_PER_SECOND = 1_000_000
# Keep durable timeout values exactly representable by IEEE-754 consumers as
# well as Python's integer-based contract.  This also bounds conversions used
# by asyncio's float-based monotonic deadlines.
MAX_ATTEMPT_TIMEOUT_MICROSECONDS = (1 << 53) - 1
MAX_AC_ATTEMPT_TIMEOUT_SECONDS = MAX_ATTEMPT_TIMEOUT_MICROSECONDS // _MICROSECONDS_PER_SECOND
_ATTEMPT_BUDGET_PROGRESS_KEYS = (
    "schema_version",
    "max_agentic_steps",
    "timeout_microseconds",
    "agentic_steps_consumed",
    "remaining_timeout_microseconds",
)
_MISSING_PROGRESS_FIELD = object()


def _timeout_to_microseconds(timeout_seconds: float) -> int:
    """Return a bounded durable ceiling without rounding the allowance up."""

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("ac_attempt_timeout_seconds must be finite and > 0")
    if isinstance(timeout_seconds, float):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("ac_attempt_timeout_seconds must be finite and > 0")
        maximum_timeout_seconds = MAX_ATTEMPT_TIMEOUT_MICROSECONDS / _MICROSECONDS_PER_SECOND
        if timeout_seconds > maximum_timeout_seconds:
            raise ValueError(
                "ac_attempt_timeout_seconds exceeds the exact durable microsecond ceiling"
            )
        timeout_microseconds = math.floor(timeout_seconds * _MICROSECONDS_PER_SECOND)
    else:
        if timeout_seconds <= 0:
            raise ValueError("ac_attempt_timeout_seconds must be finite and > 0")
        timeout_microseconds = timeout_seconds * _MICROSECONDS_PER_SECOND
    if timeout_microseconds < 1:
        raise ValueError("attempt timeout must be at least one microsecond")
    if timeout_microseconds > MAX_ATTEMPT_TIMEOUT_MICROSECONDS:
        raise ValueError("ac_attempt_timeout_seconds exceeds the exact durable microsecond ceiling")
    return timeout_microseconds


def _microseconds_to_seconds(timeout_microseconds: int) -> float:
    """Convert a durable ceiling without allowing float rounding to widen it."""

    timeout_seconds = timeout_microseconds / _MICROSECONDS_PER_SECOND
    while math.floor(timeout_seconds * _MICROSECONDS_PER_SECOND) > timeout_microseconds:
        timeout_seconds = math.nextafter(timeout_seconds, 0.0)
    return timeout_seconds


def conservative_timeout_deadline(*, started_at: float, remaining_seconds: float) -> float:
    """Build a float scheduler deadline without rounding authority upward."""

    if (
        not math.isfinite(started_at)
        or started_at < 0
        or not math.isfinite(remaining_seconds)
        or remaining_seconds < 0
    ):
        raise ValueError("attempt deadline inputs must be finite and non-negative")
    deadline = started_at + remaining_seconds
    if deadline - started_at > remaining_seconds:
        deadline = math.nextafter(deadline, started_at)
    return deadline


class AttemptBudgetKind(StrEnum):
    """The finite resource that ended an atomic attempt."""

    AGENTIC_STEPS = "agentic_steps"
    WALL_CLOCK = "wall_clock"


@dataclass(frozen=True, slots=True)
class AttemptBudgetExhaustion:
    """Measured boundary that stopped one provider attempt."""

    kind: AttemptBudgetKind
    limit: float
    observed: float

    def __post_init__(self) -> None:
        for name, value in (("limit", self.limit), ("observed", self.observed)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"attempt budget {name} must be numeric")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"attempt budget {name} must be finite and non-negative")
        if self.limit <= 0:
            raise ValueError("attempt budget limit must be greater than zero")


@dataclass(frozen=True, slots=True)
class AttemptBudgetProgress:
    """Durable finite-resource state for one paused provider attempt."""

    max_agentic_steps: int
    timeout_microseconds: int
    agentic_steps_consumed: int
    remaining_timeout_microseconds: int

    def __post_init__(self) -> None:
        if type(self.max_agentic_steps) is not int or self.max_agentic_steps < 1:
            raise ValueError("maximum agentic steps must be an integer >= 1")
        if (
            type(self.timeout_microseconds) is not int
            or not 1 <= self.timeout_microseconds <= MAX_ATTEMPT_TIMEOUT_MICROSECONDS
        ):
            raise ValueError("attempt timeout exceeds the exact durable microsecond boundary")
        if (
            type(self.agentic_steps_consumed) is not int
            or not 0 <= self.agentic_steps_consumed <= self.max_agentic_steps
        ):
            raise ValueError("consumed agentic steps must be an integer >= 0")
        if (
            type(self.remaining_timeout_microseconds) is not int
            or not 0 <= self.remaining_timeout_microseconds <= self.timeout_microseconds
        ):
            raise ValueError("remaining timeout must be an integer >= 0 microseconds")

    @property
    def remaining_timeout_seconds(self) -> float:
        return _microseconds_to_seconds(self.remaining_timeout_microseconds)

    def elapsed_timeout_seconds(self) -> float:
        """Project cumulative elapsed time for audit output, never authority."""

        return _microseconds_to_seconds(
            self.timeout_microseconds - self.remaining_timeout_microseconds
        )

    def consume_elapsed_seconds(
        self,
        elapsed_timeout_seconds: int | float,
    ) -> AttemptBudgetProgress:
        """Subtract one live segment from exact remaining authority.

        A resumed attempt must never reconstruct its allowance by subtracting
        two large floats.  Near the durable upper bound, one float ULP is larger
        than a microsecond and that subtraction can mint time.  Convert only the
        small live segment, round its consumption up, and subtract it from the
        persisted integer remainder instead.
        """

        if (
            isinstance(elapsed_timeout_seconds, bool)
            or not isinstance(elapsed_timeout_seconds, (int, float))
            or elapsed_timeout_seconds < 0
        ):
            raise ValueError("elapsed attempt time must be finite and non-negative")
        if isinstance(elapsed_timeout_seconds, float) and not math.isfinite(
            elapsed_timeout_seconds
        ):
            raise ValueError("elapsed attempt time must be finite and non-negative")

        remaining_seconds = self.remaining_timeout_seconds
        if elapsed_timeout_seconds >= remaining_seconds:
            consumed_microseconds = self.remaining_timeout_microseconds
        elif isinstance(elapsed_timeout_seconds, int):
            consumed_microseconds = min(
                self.remaining_timeout_microseconds,
                elapsed_timeout_seconds * _MICROSECONDS_PER_SECOND,
            )
        else:
            consumed_microseconds = min(
                self.remaining_timeout_microseconds,
                math.ceil(elapsed_timeout_seconds * _MICROSECONDS_PER_SECOND),
            )
        return AttemptBudgetProgress(
            max_agentic_steps=self.max_agentic_steps,
            timeout_microseconds=self.timeout_microseconds,
            agentic_steps_consumed=self.agentic_steps_consumed,
            remaining_timeout_microseconds=(
                self.remaining_timeout_microseconds - consumed_microseconds
            ),
        )

    def to_contract_data(self) -> dict[str, int]:
        return {
            "schema_version": ATTEMPT_BUDGET_PROGRESS_SCHEMA_VERSION,
            "max_agentic_steps": self.max_agentic_steps,
            "timeout_microseconds": self.timeout_microseconds,
            "agentic_steps_consumed": self.agentic_steps_consumed,
            "remaining_timeout_microseconds": self.remaining_timeout_microseconds,
        }

    @classmethod
    def capture(
        cls,
        *,
        agentic_steps_consumed: int,
        elapsed_timeout_seconds: float,
        max_agentic_steps: int,
        timeout_seconds: float,
    ) -> AttemptBudgetProgress:
        """Measure current state without rounding remaining allowance upward."""

        max_agentic_steps, timeout_seconds = validate_attempt_budget(
            max_iterations_per_ac=max_agentic_steps,
            timeout_seconds=timeout_seconds,
        )
        if (
            type(agentic_steps_consumed) is not int
            or not 0 <= agentic_steps_consumed <= max_agentic_steps
        ):
            raise ValueError("consumed agentic steps exceed the configured attempt budget")
        timeout_microseconds = _timeout_to_microseconds(timeout_seconds)
        initial = cls(
            max_agentic_steps=max_agentic_steps,
            timeout_microseconds=timeout_microseconds,
            agentic_steps_consumed=agentic_steps_consumed,
            remaining_timeout_microseconds=timeout_microseconds,
        )
        return initial.consume_elapsed_seconds(elapsed_timeout_seconds)

    @classmethod
    def from_contract_data(
        cls,
        value: object,
        *,
        max_agentic_steps: int | None = None,
        timeout_seconds: float | None = None,
    ) -> AttemptBudgetProgress:
        """Validate an exact pause snapshot against its configured ceiling."""

        if not isinstance(value, Mapping):
            raise ValueError("attempt budget progress has an invalid schema")
        try:
            if len(value) != len(_ATTEMPT_BUDGET_PROGRESS_KEYS):
                raise ValueError("attempt budget progress has an invalid schema")
            projected = tuple(
                value.get(key, _MISSING_PROGRESS_FIELD) for key in _ATTEMPT_BUDGET_PROGRESS_KEYS
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("attempt budget progress has an invalid schema") from exc
        if any(item is _MISSING_PROGRESS_FIELD for item in projected):
            raise ValueError("attempt budget progress has an invalid schema")
        (
            schema_version,
            persisted_max_steps,
            persisted_timeout,
            steps,
            remaining,
        ) = projected
        if (
            type(schema_version) is not int
            or schema_version != ATTEMPT_BUDGET_PROGRESS_SCHEMA_VERSION
            or type(persisted_max_steps) is not int
            or persisted_max_steps < 1
            or type(persisted_timeout) is not int
            or not 1 <= persisted_timeout <= MAX_ATTEMPT_TIMEOUT_MICROSECONDS
            or type(steps) is not int
            or not 0 <= steps <= persisted_max_steps
            or type(remaining) is not int
            or not 0 <= remaining <= persisted_timeout
        ):
            raise ValueError("attempt budget progress exceeds its configured boundary")
        if max_agentic_steps is not None and max_agentic_steps != persisted_max_steps:
            raise ValueError("attempt budget progress has a different step ceiling")
        if timeout_seconds is not None:
            _validated_steps, validated_timeout = validate_attempt_budget(
                max_iterations_per_ac=persisted_max_steps,
                timeout_seconds=timeout_seconds,
            )
            if _timeout_to_microseconds(validated_timeout) != persisted_timeout:
                raise ValueError("attempt budget progress has a different timeout ceiling")
        return cls(
            max_agentic_steps=persisted_max_steps,
            timeout_microseconds=persisted_timeout,
            agentic_steps_consumed=steps,
            remaining_timeout_microseconds=remaining,
        )


def validate_attempt_budget(
    *,
    max_iterations_per_ac: int,
    timeout_seconds: float,
) -> tuple[int, float]:
    """Validate direct-constructor budget inputs without silently widening them."""

    if type(max_iterations_per_ac) is not int or max_iterations_per_ac < 1:
        raise ValueError("max_iterations_per_ac must be an integer >= 1")
    timeout_microseconds = _timeout_to_microseconds(timeout_seconds)
    return max_iterations_per_ac, _microseconds_to_seconds(timeout_microseconds)


def scale_attempt_budget(
    *,
    max_iterations_per_ac: int,
    timeout_seconds: float,
    multiplier: int,
) -> tuple[int, float]:
    """Scale a per-AC budget using exact integer ceilings, failing closed."""

    if type(multiplier) is not int or multiplier < 1:
        raise ValueError("attempt budget multiplier must be an integer >= 1")
    max_iterations_per_ac, _validated_timeout = validate_attempt_budget(
        max_iterations_per_ac=max_iterations_per_ac,
        timeout_seconds=timeout_seconds,
    )
    timeout_microseconds = _timeout_to_microseconds(timeout_seconds) * multiplier
    if timeout_microseconds > MAX_ATTEMPT_TIMEOUT_MICROSECONDS:
        raise ValueError("scaled attempt timeout exceeds the exact durable microsecond ceiling")
    return (
        max_iterations_per_ac * multiplier,
        _microseconds_to_seconds(timeout_microseconds),
    )


__all__ = [
    "ATTEMPT_BUDGET_PROGRESS_SCHEMA_VERSION",
    "AttemptBudgetExhaustion",
    "AttemptBudgetKind",
    "AttemptBudgetProgress",
    "DEFAULT_AC_ATTEMPT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ITERATIONS_PER_AC",
    "MAX_AC_ATTEMPT_TIMEOUT_SECONDS",
    "MAX_ATTEMPT_TIMEOUT_MICROSECONDS",
    "conservative_timeout_deadline",
    "scale_attempt_budget",
    "validate_attempt_budget",
]
