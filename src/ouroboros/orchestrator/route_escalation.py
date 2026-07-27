"""Bounded route escalation and raw route observations.

This module is the next policy slice after the Route B admission kernel.  It
does not dispatch a provider or declare acceptance.  It records the result of
an implementation attempt and deterministically decides whether a classified
route failure may move to the next eligible route or must end the episode as
``BLOCKED``.

The returned route is configuration evidence.  An effect owner must still
rebuild and validate a fresh :class:`~ouroboros.orchestrator.route_policy.RouteAdmission`
before entering a provider boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re

from ouroboros.core.security import is_credential_shaped
from ouroboros.orchestrator.contract_numbers import (
    json_safe_nonnegative_int,
    parse_nonnegative_contract_int,
)
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.route_policy import (
    MAX_ROUTE_CANDIDATES,
    MAX_ROUTE_CAPABILITIES,
    MAX_ROUTE_FIELD_CHARS,
    MAX_ROUTE_ID_CHARS,
    RouteCandidate,
    RouteRegistry,
    RouteRequirements,
    admit_route,
)

ROUTE_OBSERVATION_CONTRACT_VERSION = 1
MAX_EPISODE_ID_CHARS = MAX_ROUTE_ID_CHARS
MAX_ROUTE_ATTEMPTS = MAX_ROUTE_CANDIDATES
MAX_REASON_CHARS = MAX_ROUTE_FIELD_CHARS

_SAFE_TOKEN = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_REASON_CHARS - 1}}}$")
_SAFE_EPISODE_ID = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_EPISODE_ID_CHARS - 1}}}$")


class VerifierOutcome(StrEnum):
    """Provisional attempt outcomes; none can declare final acceptance."""

    ATTEMPT_SUCCEEDED = "attempt_succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class EscalationAction(StrEnum):
    """What the deterministic policy permits after an attempt."""

    ESCALATE_ROUTE = "escalate_route"
    BLOCKED = "blocked"


class EscalationReason(StrEnum):
    """Stable reason codes; arbitrary verifier prose is never persisted."""

    CLASSIFIED_FAILURE = "classified_failure"
    HUMAN_HANDOFF_REQUIRED = "human_handoff_required"
    ROUTES_EXHAUSTED = "routes_exhausted"
    NO_ELIGIBLE_ROUTE = "no_eligible_route"
    INVALID_ATTEMPT_HISTORY = "invalid_attempt_history"


def _bounded_token(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str] = _SAFE_TOKEN,
    max_chars: int = MAX_REASON_CHARS,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_chars
        or not pattern.fullmatch(normalized)
        or is_credential_shaped(normalized)
    ):
        raise ValueError(f"{field} has an invalid shape")
    return normalized


def _bounded_ordered(values: Iterable[object], *, field: str, max_count: int) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray, set, frozenset, Mapping)):
        raise ValueError(f"{field} must be an ordered sequence")
    try:
        iterator = iter(values)
    except Exception as exc:  # pragma: no cover - defensive protocol guard
        raise ValueError(f"{field} could not be iterated") from exc
    result: list[object] = []
    for index in range(max_count + 1):
        try:
            item = next(iterator)
        except StopIteration:
            return tuple(result)
        except Exception as exc:
            raise ValueError(f"{field} could not be iterated") from exc
        if index >= max_count:
            raise ValueError(f"{field} exceeds its bound")
        result.append(item)
    return tuple(result)


def _has_bounded_mapping_keys(
    value: Mapping[object, object],
    *,
    expected: frozenset[str],
) -> bool:
    """Check exact contract keys without materializing an untrusted tail."""

    try:
        iterator = iter(value)
    except Exception:
        return False
    keys: list[str] = []
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(keys) == len(expected) and set(keys) == expected
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in keys:
            return False
        keys.append(key)
    return False


def _normalize_tokens(
    values: Iterable[object],
    *,
    field: str,
    max_count: int,
    pattern: re.Pattern[str] = _SAFE_TOKEN,
    max_chars: int = MAX_REASON_CHARS,
) -> tuple[str, ...]:
    raw = _bounded_ordered(values, field=field, max_count=max_count)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw:
        token = _bounded_token(value, field=field, pattern=pattern, max_chars=max_chars)
        if token in seen:
            raise ValueError(f"{field} contains duplicate values")
        seen.add(token)
        normalized.append(token)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class RouteObservation:
    """A raw, authority-bound observation of one attempted route.

    It is deliberately not an authorization object.  The route dimensions are
    copied for auditability, while no provider credential or authority identity
    is included in the durable shape.
    """

    episode_id: str
    attempt_index: int
    route_id: str
    model: str
    harness: str
    effort: str | None
    cost_units: int
    capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    capability_match: bool
    verifier_outcome: VerifierOutcome
    failure_class: FailureClass | None = None
    escalation_reason: EscalationReason | None = None

    def __post_init__(self) -> None:
        episode_id = _bounded_token(
            self.episode_id,
            field="episode_id",
            pattern=_SAFE_EPISODE_ID,
            max_chars=MAX_EPISODE_ID_CHARS,
        )
        route_id = _bounded_token(self.route_id, field="route_id", max_chars=MAX_ROUTE_ID_CHARS)
        model = _bounded_token(self.model, field="model")
        harness = _bounded_token(self.harness, field="harness")
        effort = None if self.effort is None else _bounded_token(self.effort, field="effort")
        if type(self.attempt_index) is not int:
            raise ValueError("attempt_index must be an integer")
        if not 0 <= self.attempt_index < MAX_ROUTE_ATTEMPTS:
            raise ValueError("attempt_index exceeds its bound")
        if type(self.cost_units) is not int:
            raise ValueError("cost_units must be an integer")
        if self.cost_units < 0:
            raise ValueError("cost_units must be >= 0")
        if not isinstance(self.capability_match, bool):
            raise ValueError("capability_match must be a boolean")
        if not isinstance(self.verifier_outcome, VerifierOutcome):
            raise ValueError("verifier_outcome must be a VerifierOutcome")
        if self.failure_class is not None and not isinstance(self.failure_class, FailureClass):
            raise ValueError("failure_class must be a FailureClass")
        if self.escalation_reason is not None and not isinstance(
            self.escalation_reason, EscalationReason
        ):
            raise ValueError("escalation_reason must be an EscalationReason")
        capabilities = _normalize_tokens(
            self.capabilities,
            field="capabilities",
            max_count=MAX_ROUTE_CAPABILITIES,
        )
        required = _normalize_tokens(
            self.required_capabilities,
            field="required_capabilities",
            max_count=MAX_ROUTE_CAPABILITIES,
        )
        expected_match = set(required).issubset(capabilities)
        if self.capability_match is not expected_match:
            raise ValueError("capability_match does not match the route capabilities")
        if self.verifier_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED:
            if self.failure_class is not None or self.escalation_reason is not None:
                raise ValueError("successful attempt observations cannot carry failure metadata")
        else:
            if self.failure_class is None:
                raise ValueError("failed observations require a failure_class")
            if (
                self.verifier_outcome is VerifierOutcome.BLOCKED
                and self.failure_class is not FailureClass.BLOCKED
            ):
                raise ValueError("blocked observations require the BLOCKED failure class")
        object.__setattr__(self, "episode_id", episode_id)
        object.__setattr__(self, "route_id", route_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "required_capabilities", required)

    @classmethod
    def from_candidate(
        cls,
        candidate: RouteCandidate,
        requirements: RouteRequirements,
        *,
        episode_id: str,
        attempt_index: int,
        verifier_outcome: VerifierOutcome,
        failure_class: FailureClass | None = None,
        escalation_reason: EscalationReason | None = None,
    ) -> RouteObservation:
        """Build an observation from a registry candidate, never free-form route data."""

        if not isinstance(candidate, RouteCandidate):
            raise TypeError("candidate must be a RouteCandidate")
        if not isinstance(requirements, RouteRequirements):
            raise TypeError("requirements must be RouteRequirements")
        return cls(
            episode_id=episode_id,
            attempt_index=attempt_index,
            route_id=candidate.route_id,
            model=candidate.model,
            harness=candidate.harness,
            effort=candidate.effort,
            cost_units=candidate.cost_units,
            capabilities=candidate.capabilities,
            required_capabilities=requirements.required_capabilities,
            capability_match=set(requirements.required_capabilities).issubset(
                candidate.capabilities
            ),
            verifier_outcome=verifier_outcome,
            failure_class=failure_class,
            escalation_reason=escalation_reason,
        )

    def to_contract_data(self) -> dict[str, object]:
        return {
            "version": ROUTE_OBSERVATION_CONTRACT_VERSION,
            "episode_id": self.episode_id,
            "attempt_index": self.attempt_index,
            "route_id": self.route_id,
            "model": self.model,
            "harness": self.harness,
            "effort": self.effort,
            "cost_units": json_safe_nonnegative_int(self.cost_units),
            "capabilities": list(self.capabilities),
            "required_capabilities": list(self.required_capabilities),
            "capability_match": self.capability_match,
            "verifier_outcome": self.verifier_outcome.value,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "escalation_reason": self.escalation_reason.value if self.escalation_reason else None,
        }

    @classmethod
    def from_contract_data(cls, value: object) -> RouteObservation:
        if not isinstance(value, Mapping):
            raise ValueError("route observation must be an object")
        expected = {
            "version",
            "episode_id",
            "attempt_index",
            "route_id",
            "model",
            "harness",
            "effort",
            "cost_units",
            "capabilities",
            "required_capabilities",
            "capability_match",
            "verifier_outcome",
            "failure_class",
            "escalation_reason",
        }
        if not _has_bounded_mapping_keys(value, expected=frozenset(expected)):
            raise ValueError("route observation has an unsupported shape")
        try:
            version = value["version"]
            episode_id = value["episode_id"]
            attempt_index = value["attempt_index"]
            route_id = value["route_id"]
            model = value["model"]
            harness = value["harness"]
            effort = value["effort"]
            raw_cost_units = value["cost_units"]
            capabilities = value["capabilities"]
            required = value["required_capabilities"]
            capability_match = value["capability_match"]
            raw_outcome = value["verifier_outcome"]
            raw_failure = value["failure_class"]
            raw_reason = value["escalation_reason"]
        except Exception as exc:
            raise ValueError("route observation has an unsupported shape") from exc
        if type(version) is not int or version != ROUTE_OBSERVATION_CONTRACT_VERSION:
            raise ValueError("route observation has an unsupported shape")
        if (
            type(episode_id) is not str
            or type(attempt_index) is not int
            or type(route_id) is not str
            or type(model) is not str
            or type(harness) is not str
            or (effort is not None and type(effort) is not str)
            or type(capability_match) is not bool
        ):
            raise ValueError("route observation scalar fields are invalid")
        failure_class: FailureClass | None
        if raw_failure is None:
            failure_class = None
        else:
            if type(raw_failure) is not str:
                raise ValueError("route observation failure_class is invalid")
            try:
                failure_class = FailureClass(raw_failure)
            except (TypeError, ValueError) as exc:
                raise ValueError("route observation failure_class is invalid") from exc
        reason: EscalationReason | None
        if raw_reason is None:
            reason = None
        else:
            if type(raw_reason) is not str:
                raise ValueError("route observation escalation_reason is invalid")
            try:
                reason = EscalationReason(raw_reason)
            except (TypeError, ValueError) as exc:
                raise ValueError("route observation escalation_reason is invalid") from exc
        if type(raw_outcome) is not str:
            raise ValueError("route observation verifier_outcome is invalid")
        try:
            outcome = VerifierOutcome(raw_outcome)
        except (TypeError, ValueError) as exc:
            raise ValueError("route observation verifier_outcome is invalid") from exc
        if type(capabilities) is not list:
            raise ValueError("route observation capabilities must be a list")
        if type(required) is not list:
            raise ValueError("route observation required_capabilities must be a list")
        cost_units = parse_nonnegative_contract_int(raw_cost_units)
        if cost_units is None:
            raise ValueError("route observation cost_units is invalid")
        return cls(
            episode_id=episode_id,
            attempt_index=attempt_index,
            route_id=route_id,
            model=model,
            harness=harness,
            effort=effort,
            cost_units=cost_units,
            capabilities=tuple(
                _bounded_ordered(
                    capabilities, field="capabilities", max_count=MAX_ROUTE_CAPABILITIES
                )
            ),
            required_capabilities=tuple(
                _bounded_ordered(
                    required,
                    field="required_capabilities",
                    max_count=MAX_ROUTE_CAPABILITIES,
                )
            ),
            capability_match=capability_match,
            verifier_outcome=outcome,
            failure_class=failure_class,
            escalation_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class RouteEscalationDecision:
    """A bounded, non-authorizing next-step decision."""

    action: EscalationAction
    failure_class: FailureClass
    selected: RouteCandidate | None
    attempted_route_ids: tuple[str, ...]
    remaining_route_ids: tuple[str, ...]
    reason: EscalationReason

    def __post_init__(self) -> None:
        if not isinstance(self.action, EscalationAction):
            raise ValueError("action must be an EscalationAction")
        if not isinstance(self.failure_class, FailureClass):
            raise ValueError("failure_class must be a FailureClass")
        attempted = _normalize_tokens(
            self.attempted_route_ids,
            field="attempted_route_ids",
            max_count=MAX_ROUTE_ATTEMPTS,
            max_chars=MAX_ROUTE_ID_CHARS,
        )
        remaining = _normalize_tokens(
            self.remaining_route_ids,
            field="remaining_route_ids",
            max_count=MAX_ROUTE_CANDIDATES,
            max_chars=MAX_ROUTE_ID_CHARS,
        )
        if set(attempted).intersection(remaining):
            raise ValueError("a route cannot be attempted and remaining")
        if not isinstance(self.reason, EscalationReason):
            raise ValueError("reason must be an EscalationReason")
        if self.action is EscalationAction.ESCALATE_ROUTE:
            if not isinstance(self.selected, RouteCandidate):
                raise ValueError("route actions require a selected candidate")
            if self.selected.route_id in attempted or self.selected.route_id in remaining:
                raise ValueError("selected route must be the unattempted immediate successor")
            if self.failure_class is FailureClass.BLOCKED:
                raise ValueError("blocked failures cannot escalate")
            if self.reason is not EscalationReason.CLASSIFIED_FAILURE:
                raise ValueError("route escalation requires the classified-failure reason")
        elif self.selected is not None:
            raise ValueError("terminal/non-route actions cannot select a candidate")
        if self.action is EscalationAction.BLOCKED:
            if self.reason is EscalationReason.ROUTES_EXHAUSTED and remaining:
                raise ValueError("route exhaustion cannot retain remaining routes")
            if (
                self.reason is EscalationReason.HUMAN_HANDOFF_REQUIRED
                and self.failure_class is not FailureClass.BLOCKED
            ):
                raise ValueError("human handoff requires a blocked failure")
        object.__setattr__(self, "attempted_route_ids", attempted)
        object.__setattr__(self, "remaining_route_ids", remaining)

    @property
    def blocked(self) -> bool:
        return self.action is EscalationAction.BLOCKED

    def to_contract_data(self) -> dict[str, object]:
        return {
            "version": ROUTE_OBSERVATION_CONTRACT_VERSION,
            "action": self.action.value,
            "failure_class": self.failure_class.value,
            # The successor is effect-relevant authorization input.  Persisting
            # only its id lets a changed live registry silently rebind that id to
            # different model/cost/policy semantics during replay.
            "selected_route": self.selected.to_contract_data() if self.selected else None,
            "attempted_route_ids": list(self.attempted_route_ids),
            "remaining_route_ids": list(self.remaining_route_ids),
            "reason": self.reason.value,
        }

    @classmethod
    def from_contract_data(
        cls,
        value: object,
        *,
        registry: RouteRegistry,
    ) -> RouteEscalationDecision:
        """Strictly parse a persisted decision against its live registry."""
        if not isinstance(registry, RouteRegistry):
            raise TypeError("registry must be a RouteRegistry")
        if not isinstance(value, Mapping) or not _has_bounded_mapping_keys(
            value,
            expected=frozenset(
                {
                    "version",
                    "action",
                    "failure_class",
                    "selected_route",
                    "attempted_route_ids",
                    "remaining_route_ids",
                    "reason",
                }
            ),
        ):
            raise ValueError("route escalation decision has an unsupported shape")
        try:
            version = value["version"]
            raw_action = value["action"]
            raw_failure = value["failure_class"]
            raw_reason = value["reason"]
            attempted = value["attempted_route_ids"]
            remaining = value["remaining_route_ids"]
            raw_selected = value["selected_route"]
        except Exception as exc:
            raise ValueError("route escalation decision could not be read") from exc
        if type(version) is not int or version != ROUTE_OBSERVATION_CONTRACT_VERSION:
            raise ValueError("route escalation decision version is invalid")
        if (
            type(raw_action) is not str
            or type(raw_failure) is not str
            or type(raw_reason) is not str
        ):
            raise ValueError("route escalation decision enum is invalid")
        try:
            action = EscalationAction(raw_action)
            failure = FailureClass(raw_failure)
            reason = EscalationReason(raw_reason)
        except (TypeError, ValueError) as exc:
            raise ValueError("route escalation decision enum is invalid") from exc
        if type(attempted) is not list:
            raise ValueError("attempted_route_ids must be a list")
        if type(remaining) is not list:
            raise ValueError("remaining_route_ids must be a list")
        if raw_selected is None:
            selected = None
        else:
            try:
                selected_snapshot = RouteCandidate.from_contract_data(raw_selected)
            except (TypeError, ValueError) as exc:
                raise ValueError("selected_route snapshot is invalid") from exc
            selected = _candidate_by_id(registry, selected_snapshot.route_id)
            if selected is None:
                raise ValueError("selected_route references an unknown route")
            if selected != selected_snapshot:
                raise ValueError("selected_route semantics drifted from the live registry")
        decision = cls(
            action=action,
            failure_class=failure,
            selected=selected,
            attempted_route_ids=tuple(
                _bounded_ordered(
                    attempted,
                    field="attempted_route_ids",
                    max_count=MAX_ROUTE_ATTEMPTS,
                )
            ),  # type: ignore[arg-type]
            remaining_route_ids=tuple(
                _bounded_ordered(
                    remaining,
                    field="remaining_route_ids",
                    max_count=MAX_ROUTE_CANDIDATES,
                )
            ),  # type: ignore[arg-type]
            reason=reason,
        )
        known = {candidate.route_id for candidate in registry.candidates}
        if not set(decision.attempted_route_ids).issubset(known) or not set(
            decision.remaining_route_ids
        ).issubset(known):
            raise ValueError("route escalation decision references an unknown route")
        return decision


def _normalize_failure(value: FailureClass | str) -> FailureClass:
    try:
        return value if isinstance(value, FailureClass) else FailureClass(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("failure_class is invalid") from exc


def _candidate_by_id(registry: RouteRegistry, route_id: str) -> RouteCandidate | None:
    return next(
        (candidate for candidate in registry.candidates if candidate.route_id == route_id), None
    )


def advance_route(
    registry: RouteRegistry,
    requirements: RouteRequirements,
    *,
    current_route_id: str,
    attempted_route_ids: Sequence[str],
    failure_class: FailureClass | str,
    advisor_order: Sequence[str] = (),
) -> RouteEscalationDecision:
    """Compute one bounded next step after a classified route failure.

    ``attempted_route_ids`` is the ordered episode history.  It is never
    silently deduplicated: duplicate or unknown history is a fail-closed input
    error, which prevents a resumed episode from repeating an effect.
    """

    if not isinstance(registry, RouteRegistry):
        raise TypeError("registry must be a RouteRegistry")
    if not isinstance(requirements, RouteRequirements):
        raise TypeError("requirements must be RouteRequirements")
    failure = _normalize_failure(failure_class)
    current = _bounded_token(
        current_route_id,
        field="current_route_id",
        max_chars=MAX_ROUTE_ID_CHARS,
    )
    attempted = _normalize_tokens(
        attempted_route_ids,
        field="attempted_route_ids",
        max_count=MAX_ROUTE_ATTEMPTS,
        max_chars=MAX_ROUTE_ID_CHARS,
    )
    if not attempted or attempted[-1] != current:
        raise ValueError("current_route_id must be the last attempted route")
    if any(_candidate_by_id(registry, route_id) is None for route_id in attempted):
        raise ValueError("attempt history contains an unknown route")

    admission = admit_route(registry, requirements, advisor_order=advisor_order)
    eligible = admission.eligible_route_ids
    if current not in eligible:
        return RouteEscalationDecision(
            EscalationAction.BLOCKED,
            failure,
            None,
            attempted,
            tuple(route_id for route_id in eligible if route_id not in attempted),
            EscalationReason.NO_ELIGIBLE_ROUTE,
        )
    current_position = eligible.index(current)
    if any(route_id not in attempted for route_id in eligible[:current_position]):
        raise ValueError("attempt history skipped an eligible route")
    remaining = tuple(route_id for route_id in eligible if route_id not in attempted)
    if failure is FailureClass.BLOCKED:
        return RouteEscalationDecision(
            EscalationAction.BLOCKED,
            failure,
            None,
            attempted,
            remaining,
            EscalationReason.HUMAN_HANDOFF_REQUIRED,
        )
    if not remaining:
        return RouteEscalationDecision(
            EscalationAction.BLOCKED,
            failure,
            None,
            attempted,
            remaining,
            EscalationReason.ROUTES_EXHAUSTED,
        )
    selected = _candidate_by_id(registry, remaining[0])
    if selected is None:  # pragma: no cover - registry/admission invariant
        return RouteEscalationDecision(
            EscalationAction.BLOCKED,
            failure,
            None,
            attempted,
            remaining,
            EscalationReason.NO_ELIGIBLE_ROUTE,
        )
    return RouteEscalationDecision(
        EscalationAction.ESCALATE_ROUTE,
        failure,
        selected,
        attempted,
        remaining[1:],
        EscalationReason.CLASSIFIED_FAILURE,
    )


__all__ = [
    "EscalationAction",
    "EscalationReason",
    "MAX_ROUTE_ATTEMPTS",
    "ROUTE_OBSERVATION_CONTRACT_VERSION",
    "RouteEscalationDecision",
    "RouteObservation",
    "VerifierOutcome",
    "advance_route",
]
