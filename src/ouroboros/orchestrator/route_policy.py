"""Provider-neutral route candidates and deterministic admission.

Routing is deliberately split into two authorities:

* an Advisor may rank candidates, but its output is only a preference order;
* the Admission Kernel applies hard constraints and selects the cheapest
  eligible candidate.

This module contains no provider calls and no acceptance logic.  It is the
small, replayable policy boundary used by the live router in later slices.
Keeping the contract here free of executors and adapters makes malformed route
configuration fail closed before a provider boundary is entered.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set, Sized
from dataclasses import dataclass, field
from enum import StrEnum
import re

ROUTE_CONTRACT_VERSION = 1
MAX_ROUTE_ID_CHARS = 160
MAX_ROUTE_FIELD_CHARS = 240
MAX_ROUTE_CAPABILITIES = 32
MAX_ROUTE_CANDIDATES = 128
MAX_ADVISOR_ORDER = 128
MAX_REJECTION_REASONS = 16

_SAFE_ROUTE_ID = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_ROUTE_ID_CHARS - 1}}}$")
_SAFE_TOKEN = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_ROUTE_FIELD_CHARS - 1}}}$")


class RouteDecisionDisposition(StrEnum):
    """Result of deterministic route admission."""

    ADMITTED = "admitted"
    BLOCKED = "blocked"


class RouteRejectionCode(StrEnum):
    """Stable reason codes for a candidate rejected by the Kernel."""

    DISABLED = "disabled"
    ROUTE_PIN_MISMATCH = "route_pin_mismatch"
    MODEL_PIN_MISMATCH = "model_pin_mismatch"
    HARNESS_PIN_MISMATCH = "harness_pin_mismatch"
    PERSONA_PIN_MISMATCH = "persona_pin_mismatch"
    TOOL_POLICY_PIN_MISMATCH = "tool_policy_pin_mismatch"
    AUTHORITY_IDENTITY_PIN_MISMATCH = "authority_identity_pin_mismatch"
    HARNESS_NOT_ALLOWED = "harness_not_allowed"
    EFFORT_MISMATCH = "effort_mismatch"
    MISSING_CAPABILITIES = "missing_capabilities"


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """One executable provider-neutral route.

    ``cost_units`` is an integer relative cost supplied by route configuration;
    it is intentionally not inferred from model names.  ``ordinal`` is a
    stable registry order and is only a final tie-breaker after cost and any
    advisory rank.
    """

    route_id: str
    model: str
    harness: str
    effort: str | None
    cost_units: int
    persona: str
    tool_policy: str
    authority_identity: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    ordinal: int = 0

    def __post_init__(self) -> None:
        route_id = _bounded_token(self.route_id, field="route_id", pattern=_SAFE_ROUTE_ID)
        model = _bounded_token(self.model, field="model", pattern=_SAFE_TOKEN)
        harness = _bounded_token(self.harness, field="harness", pattern=_SAFE_TOKEN)
        effort = (
            None
            if self.effort is None
            else _bounded_token(self.effort, field="effort", pattern=_SAFE_TOKEN)
        )
        persona = _bounded_token(self.persona, field="persona", pattern=_SAFE_TOKEN)
        tool_policy = _bounded_token(self.tool_policy, field="tool_policy", pattern=_SAFE_TOKEN)
        authority_identity = _bounded_token(
            self.authority_identity,
            field="authority_identity",
            pattern=_SAFE_TOKEN,
        )
        if isinstance(self.cost_units, bool) or not isinstance(self.cost_units, int):
            raise ValueError("cost_units must be an integer")
        if self.cost_units < 0:
            raise ValueError("cost_units must be >= 0")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be >= 0")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        capabilities = _normalize_tokens(
            self.capabilities,
            field="capabilities",
            max_count=MAX_ROUTE_CAPABILITIES,
        )
        object.__setattr__(self, "route_id", route_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "persona", persona)
        object.__setattr__(self, "tool_policy", tool_policy)
        object.__setattr__(self, "authority_identity", authority_identity)
        object.__setattr__(self, "capabilities", capabilities)

    def to_contract_data(self) -> dict[str, object]:
        """Return the deterministic route contract representation."""

        return {
            "route_id": self.route_id,
            "model": self.model,
            "harness": self.harness,
            "effort": self.effort,
            "cost_units": self.cost_units,
            "persona": self.persona,
            "tool_policy": self.tool_policy,
            "authority_identity": self.authority_identity,
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_contract_data(cls, value: object) -> RouteCandidate:
        """Parse one route, rejecting unknown or malformed fields."""

        if not isinstance(value, Mapping):
            raise ValueError("route candidate must be an object")
        expected = {
            "route_id",
            "model",
            "harness",
            "effort",
            "cost_units",
            "persona",
            "tool_policy",
            "authority_identity",
            "capabilities",
            "enabled",
            "ordinal",
        }
        if set(value) != expected:
            raise ValueError("route candidate has an unsupported shape")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, Sequence) or isinstance(
            capabilities, str | bytes | bytearray
        ):
            raise ValueError("route candidate capabilities must be a list")
        if len(capabilities) > MAX_ROUTE_CAPABILITIES:
            raise ValueError("capabilities exceeds its bound")
        return cls(
            route_id=value["route_id"],  # type: ignore[arg-type]
            model=value["model"],  # type: ignore[arg-type]
            harness=value["harness"],  # type: ignore[arg-type]
            effort=value["effort"],  # type: ignore[arg-type]
            cost_units=value["cost_units"],  # type: ignore[arg-type]
            persona=value["persona"],  # type: ignore[arg-type]
            tool_policy=value["tool_policy"],  # type: ignore[arg-type]
            authority_identity=value["authority_identity"],  # type: ignore[arg-type]
            capabilities=tuple(capabilities),  # type: ignore[arg-type]
            enabled=value["enabled"],  # type: ignore[arg-type]
            ordinal=value["ordinal"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RouteRegistry:
    """Versioned, immutable set of configured route candidates."""

    candidates: tuple[RouteCandidate, ...]
    version: int = ROUTE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != ROUTE_CONTRACT_VERSION:
            raise ValueError(f"unsupported route registry version: {self.version!r}")
        candidates = tuple(
            _bounded_iterable(
                self.candidates,
                field="route registry candidates",
                max_count=MAX_ROUTE_CANDIDATES,
            )
        )
        if not candidates:
            raise ValueError("route registry must contain at least one candidate")
        if not all(isinstance(candidate, RouteCandidate) for candidate in candidates):
            raise ValueError("route registry candidates must be RouteCandidate values")
        route_ids = [candidate.route_id for candidate in candidates]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route registry route_id values must be unique")
        object.__setattr__(self, "candidates", candidates)

    def to_contract_data(self) -> dict[str, object]:
        return {
            "version": self.version,
            "candidates": [candidate.to_contract_data() for candidate in self.candidates],
        }

    @classmethod
    def from_contract_data(cls, value: object) -> RouteRegistry:
        if not isinstance(value, Mapping):
            raise ValueError("route registry must be an object")
        if set(value) != {"version", "candidates"}:
            raise ValueError("route registry has an unsupported shape")
        if value.get("version") != ROUTE_CONTRACT_VERSION:
            raise ValueError("unsupported route registry version")
        raw_candidates = value.get("candidates")
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, str | bytes | bytearray
        ):
            raise ValueError("route registry candidates must be a list")
        if len(raw_candidates) > MAX_ROUTE_CANDIDATES:
            raise ValueError("route registry exceeds the bounded candidate count")
        return cls(
            candidates=tuple(RouteCandidate.from_contract_data(item) for item in raw_candidates),
            version=value["version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RouteRequirements:
    """Hard constraints supplied by the user and execution authority."""

    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    allowed_harnesses: tuple[str, ...] = field(default_factory=tuple)
    required_effort: str | None = None
    pinned_route_id: str | None = None
    pinned_model: str | None = None
    pinned_harness: str | None = None
    pinned_persona: str | None = None
    pinned_tool_policy: str | None = None
    pinned_authority_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_capabilities",
            _normalize_tokens(
                self.required_capabilities,
                field="required_capabilities",
                max_count=MAX_ROUTE_CAPABILITIES,
            ),
        )
        object.__setattr__(
            self,
            "allowed_harnesses",
            _normalize_tokens(
                self.allowed_harnesses,
                field="allowed_harnesses",
                max_count=MAX_ROUTE_CAPABILITIES,
            ),
        )
        for field_name in (
            "required_effort",
            "pinned_route_id",
            "pinned_model",
            "pinned_harness",
            "pinned_persona",
            "pinned_tool_policy",
            "pinned_authority_identity",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _bounded_token(
                        value,
                        field=field_name,
                        pattern=_SAFE_ROUTE_ID if field_name == "pinned_route_id" else _SAFE_TOKEN,
                    ),
                )


@dataclass(frozen=True, slots=True)
class RouteRejection:
    """One candidate's deterministic rejection reasons."""

    route_id: str
    reasons: tuple[RouteRejectionCode, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ROUTE_ID.fullmatch(self.route_id):
            raise ValueError("route rejection route_id is invalid")
        if not self.reasons or len(self.reasons) > MAX_REJECTION_REASONS:
            raise ValueError("route rejection must contain at least one reason")
        if not all(isinstance(reason, RouteRejectionCode) for reason in self.reasons):
            raise ValueError("route rejection reasons are invalid")

    def to_contract_data(self) -> dict[str, object]:
        return {"route_id": self.route_id, "reasons": [reason.value for reason in self.reasons]}


@dataclass(frozen=True, slots=True)
class RouteAdmission:
    """Deterministic Kernel result; only ``selected`` may enter dispatch."""

    disposition: RouteDecisionDisposition
    selected: RouteCandidate | None
    eligible_route_ids: tuple[str, ...]
    rejections: tuple[RouteRejection, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, RouteDecisionDisposition):
            raise ValueError("disposition must be a RouteDecisionDisposition")
        if self.disposition is RouteDecisionDisposition.ADMITTED and self.selected is None:
            raise ValueError("admitted route decision requires a selected candidate")
        if self.disposition is RouteDecisionDisposition.BLOCKED and self.selected is not None:
            raise ValueError("blocked route decision must not select a candidate")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("route admission reason must be non-empty")

    @property
    def admitted(self) -> bool:
        """Whether the Admission Kernel authorized one route."""

        return self.disposition is RouteDecisionDisposition.ADMITTED

    def to_contract_data(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "selected_route_id": self.selected.route_id if self.selected else None,
            "eligible_route_ids": list(self.eligible_route_ids),
            "rejections": [item.to_contract_data() for item in self.rejections],
            "reason": self.reason,
        }


def admit_route(
    registry: RouteRegistry,
    requirements: RouteRequirements,
    *,
    advisor_order: Sequence[str] = (),
) -> RouteAdmission:
    """Apply hard constraints and select the cheapest eligible route.

    ``advisor_order`` can only break ties between equal-cost eligible routes.
    Unknown or repeated IDs are ignored for selection and never create a route;
    therefore an Advisor cannot authorize a dispatch or bypass a constraint.
    """

    if not isinstance(registry, RouteRegistry):
        raise TypeError("registry must be a RouteRegistry")
    if not isinstance(requirements, RouteRequirements):
        raise TypeError("requirements must be RouteRequirements")
    advisor_rank = _advisor_rank(advisor_order, registry)
    eligible: list[RouteCandidate] = []
    rejections: list[RouteRejection] = []
    required_capabilities = set(requirements.required_capabilities)
    allowed_harnesses = set(requirements.allowed_harnesses)

    for candidate in registry.candidates:
        reasons: list[RouteRejectionCode] = []
        if not candidate.enabled:
            reasons.append(RouteRejectionCode.DISABLED)
        if requirements.pinned_route_id is not None and (
            candidate.route_id != requirements.pinned_route_id
        ):
            reasons.append(RouteRejectionCode.ROUTE_PIN_MISMATCH)
        if requirements.pinned_model is not None and candidate.model != requirements.pinned_model:
            reasons.append(RouteRejectionCode.MODEL_PIN_MISMATCH)
        if (
            requirements.pinned_harness is not None
            and candidate.harness != requirements.pinned_harness
        ):
            reasons.append(RouteRejectionCode.HARNESS_PIN_MISMATCH)
        if (
            requirements.pinned_persona is not None
            and candidate.persona != requirements.pinned_persona
        ):
            reasons.append(RouteRejectionCode.PERSONA_PIN_MISMATCH)
        if (
            requirements.pinned_tool_policy is not None
            and candidate.tool_policy != requirements.pinned_tool_policy
        ):
            reasons.append(RouteRejectionCode.TOOL_POLICY_PIN_MISMATCH)
        if (
            requirements.pinned_authority_identity is not None
            and candidate.authority_identity != requirements.pinned_authority_identity
        ):
            reasons.append(RouteRejectionCode.AUTHORITY_IDENTITY_PIN_MISMATCH)
        if allowed_harnesses and candidate.harness not in allowed_harnesses:
            reasons.append(RouteRejectionCode.HARNESS_NOT_ALLOWED)
        if (
            requirements.required_effort is not None
            and candidate.effort != requirements.required_effort
        ):
            reasons.append(RouteRejectionCode.EFFORT_MISMATCH)
        missing = required_capabilities.difference(candidate.capabilities)
        if missing:
            reasons.append(RouteRejectionCode.MISSING_CAPABILITIES)
        if reasons:
            rejections.append(RouteRejection(candidate.route_id, tuple(reasons)))
        else:
            eligible.append(candidate)

    eligible.sort(
        key=lambda candidate: (
            candidate.cost_units,
            advisor_rank.get(candidate.route_id, len(registry.candidates) + 1),
            candidate.ordinal,
            candidate.route_id,
        )
    )
    if not eligible:
        return RouteAdmission(
            disposition=RouteDecisionDisposition.BLOCKED,
            selected=None,
            eligible_route_ids=(),
            rejections=tuple(rejections),
            reason="no_eligible_route",
        )
    selected = eligible[0]
    return RouteAdmission(
        disposition=RouteDecisionDisposition.ADMITTED,
        selected=selected,
        eligible_route_ids=tuple(candidate.route_id for candidate in eligible),
        rejections=tuple(rejections),
        reason="cheapest_eligible_route",
    )


def _advisor_rank(
    advisor_order: Sequence[str],
    registry: RouteRegistry,
) -> dict[str, int]:
    if not isinstance(advisor_order, Sequence) or isinstance(
        advisor_order, str | bytes | bytearray
    ):
        return {}
    if len(advisor_order) > MAX_ADVISOR_ORDER:
        return {}
    known = {candidate.route_id for candidate in registry.candidates}
    ranks: dict[str, int] = {}
    for value in advisor_order:
        if not isinstance(value, str):
            return {}
        route_id = value.strip()
        if route_id in known and route_id not in ranks:
            ranks[route_id] = len(ranks)
    return ranks


def _bounded_token(value: object, *, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_ROUTE_FIELD_CHARS
        or not pattern.fullmatch(normalized)
    ):
        raise ValueError(f"{field} has an invalid shape")
    return normalized


def _normalize_tokens(
    values: Iterable[object],
    *,
    field: str,
    max_count: int,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in _bounded_iterable(values, field=field, max_count=max_count):
        token = _bounded_token(value, field=field, pattern=_SAFE_TOKEN)
        if token in seen:
            raise ValueError(f"{field} contains duplicate values")
        normalized.append(token)
        seen.add(token)
    return tuple(normalized)


def _bounded_iterable(
    values: Iterable[object],
    *,
    field: str,
    max_count: int,
) -> Iterable[object]:
    """Yield a bounded, ordered input without materializing untrusted tails."""

    if isinstance(values, str | bytes | bytearray):
        raise ValueError(f"{field} must be an ordered sequence")
    if isinstance(values, (Set, Mapping)):
        raise ValueError(f"{field} must be ordered")
    if not isinstance(values, Iterable):
        raise ValueError(f"{field} must be an ordered sequence")
    if isinstance(values, Sized) and len(values) > max_count:
        raise ValueError(f"{field} exceeds its bound")
    for index, value in enumerate(values):
        if index >= max_count:
            raise ValueError(f"{field} exceeds its bound")
        yield value


__all__ = [
    "ROUTE_CONTRACT_VERSION",
    "MAX_ROUTE_CANDIDATES",
    "RouteAdmission",
    "RouteCandidate",
    "RouteDecisionDisposition",
    "RouteRegistry",
    "RouteRejection",
    "RouteRejectionCode",
    "RouteRequirements",
    "admit_route",
]
