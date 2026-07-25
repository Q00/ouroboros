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
from typing import TYPE_CHECKING, NamedTuple, NoReturn
import weakref

from ouroboros.core.security import is_stable_authority_identity
from ouroboros.orchestrator.contract_numbers import (
    json_safe_nonnegative_int,
    parse_nonnegative_contract_int,
)

ROUTE_CONTRACT_VERSION = 1
MAX_ROUTE_ID_CHARS = 160
MAX_ROUTE_FIELD_CHARS = 240
MAX_ROUTE_CAPABILITIES = 32
MAX_ROUTE_CANDIDATES = 128
MAX_ADVISOR_ORDER = 128
MAX_REJECTION_REASONS = 16
MAX_ROUTE_ORDINAL = 1_000_000_000

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
        model = _model_value(self.model, field="model")
        harness = _bounded_token(self.harness, field="harness", pattern=_SAFE_TOKEN)
        effort = (
            None
            if self.effort is None
            else _bounded_token(self.effort, field="effort", pattern=_SAFE_TOKEN)
        )
        persona = _bounded_token(self.persona, field="persona", pattern=_SAFE_TOKEN)
        tool_policy = _bounded_token(self.tool_policy, field="tool_policy", pattern=_SAFE_TOKEN)
        authority_identity = _bounded_identity(
            self.authority_identity,
            field="authority_identity",
        )
        if type(self.cost_units) is not int:
            raise ValueError("cost_units must be an integer")
        if self.cost_units < 0:
            raise ValueError("cost_units must be >= 0")
        if type(self.ordinal) is not int:
            raise ValueError("ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be >= 0")
        if self.ordinal > MAX_ROUTE_ORDINAL:
            raise ValueError("ordinal exceeds its bound")
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
            "cost_units": json_safe_nonnegative_int(self.cost_units),
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
        expected = frozenset(
            {
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
        )
        _bounded_mapping_keys(value, expected=expected, field="route candidate")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, Sequence) or isinstance(
            capabilities, str | bytes | bytearray
        ):
            raise ValueError("route candidate capabilities must be a list")
        cost_units = parse_nonnegative_contract_int(value["cost_units"])
        if cost_units is None:
            raise ValueError("route candidate cost_units is invalid")
        return cls(
            route_id=value["route_id"],  # type: ignore[arg-type]
            model=value["model"],  # type: ignore[arg-type]
            harness=value["harness"],  # type: ignore[arg-type]
            effort=value["effort"],  # type: ignore[arg-type]
            cost_units=cost_units,
            persona=value["persona"],  # type: ignore[arg-type]
            tool_policy=value["tool_policy"],  # type: ignore[arg-type]
            authority_identity=value["authority_identity"],  # type: ignore[arg-type]
            capabilities=tuple(
                _bounded_iterable(
                    capabilities,
                    field="capabilities",
                    max_count=MAX_ROUTE_CAPABILITIES,
                )
            ),  # type: ignore[arg-type]
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
        if not all(type(candidate) is RouteCandidate for candidate in candidates):
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
        expected = frozenset({"version", "candidates"})
        _bounded_mapping_keys(value, expected=expected, field="route registry")
        version = value["version"]
        if type(version) is not int or version != ROUTE_CONTRACT_VERSION:
            raise ValueError("unsupported route registry version")
        raw_candidates = value["candidates"]
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, str | bytes | bytearray
        ):
            raise ValueError("route registry candidates must be a list")
        return cls(
            candidates=tuple(
                RouteCandidate.from_contract_data(item)
                for item in _bounded_iterable(
                    raw_candidates,
                    field="route registry candidates",
                    max_count=MAX_ROUTE_CANDIDATES,
                )
            ),
            version=version,  # type: ignore[arg-type]
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
                normalized = (
                    _bounded_identity(value, field=field_name)
                    if field_name == "pinned_authority_identity"
                    else _model_value(value, field=field_name)
                    if field_name == "pinned_model"
                    else _bounded_token(
                        value,
                        field=field_name,
                        pattern=_SAFE_ROUTE_ID if field_name == "pinned_route_id" else _SAFE_TOKEN,
                    )
                )
                object.__setattr__(
                    self,
                    field_name,
                    normalized,
                )


@dataclass(frozen=True, slots=True)
class RouteRejection:
    """One candidate's deterministic rejection reasons."""

    route_id: str
    reasons: tuple[RouteRejectionCode, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ROUTE_ID.fullmatch(self.route_id):
            raise ValueError("route rejection route_id is invalid")
        if not isinstance(self.reasons, tuple):
            raise ValueError("route rejection reasons must be an ordered tuple")
        if not self.reasons or len(self.reasons) > MAX_REJECTION_REASONS:
            raise ValueError("route rejection must contain at least one reason")
        if not all(isinstance(reason, RouteRejectionCode) for reason in self.reasons):
            raise ValueError("route rejection reasons are invalid")

    def to_contract_data(self) -> dict[str, object]:
        return {"route_id": self.route_id, "reasons": [reason.value for reason in self.reasons]}


def _strict_candidate_fingerprint(candidate: object) -> tuple[object, ...] | None:
    """Return a type-safe semantic fingerprint for effect-boundary checks."""

    if candidate is None:
        return None
    if type(candidate) is not RouteCandidate:
        raise TypeError("candidate must be an exact RouteCandidate")
    values = (
        candidate.route_id,
        candidate.model,
        candidate.harness,
        candidate.effort,
        candidate.cost_units,
        candidate.persona,
        candidate.tool_policy,
        candidate.authority_identity,
        candidate.capabilities,
        candidate.enabled,
        candidate.ordinal,
    )
    if any(
        type(value) is not str
        for value in values[:3] + values[5:8] + (() if values[3] is None else (values[3],))
    ):
        raise TypeError("candidate contains non-canonical string fields")
    if type(values[4]) is not int or type(values[8]) is not tuple:
        raise TypeError("candidate contains non-canonical scalar fields")
    if not all(type(item) is str for item in values[8]):
        raise TypeError("candidate capabilities are not canonical")
    if type(values[9]) is not bool or type(values[10]) is not int:
        raise TypeError("candidate contains non-canonical scalar fields")
    return values


def _strict_rejection_fingerprint(rejection: object) -> tuple[object, ...]:
    if type(rejection) is not RouteRejection:
        raise TypeError("rejection must be an exact RouteRejection")
    if type(rejection.route_id) is not str or type(rejection.reasons) is not tuple:
        raise TypeError("rejection contains non-canonical fields")
    if not all(type(reason) is RouteRejectionCode for reason in rejection.reasons):
        raise TypeError("rejection reasons are not canonical")
    return (rejection.route_id, tuple(reason.value for reason in rejection.reasons))


def _strict_route_id_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not all(type(item) is str for item in value):
        raise TypeError("route IDs are not canonical")
    return value


def _build_admission_kernel() -> tuple[type[object], object]:
    """Build the public type and kernel with bounded publication bookkeeping.

    The bookkeeping protects ordinary object projections and is not itself an
    authorization boundary; effect callers revalidate against the live registry.
    """

    class AdmissionState(NamedTuple):
        disposition: RouteDecisionDisposition
        selected: RouteCandidate | None
        selected_fingerprint: tuple[object, ...] | None
        eligible_route_ids: tuple[str, ...]
        rejections: tuple[RouteRejection, ...]
        rejection_fingerprints: tuple[tuple[object, ...], ...]
        reason: str

    trusted: dict[int, tuple[weakref.ReferenceType[object], AdmissionState]] = {}

    def candidate_fingerprint(candidate: RouteCandidate | None) -> tuple[object, ...] | None:
        return _strict_candidate_fingerprint(candidate)

    def rejection_fingerprint(rejection: RouteRejection) -> tuple[object, ...]:
        return _strict_rejection_fingerprint(rejection)

    class KernelRouteAdmission:
        """Immutable result whose publication is checked by the Kernel."""

        __slots__ = ("_sealed_state", "__weakref__")

        def __new__(cls, *_args: object, **_kwargs: object) -> KernelRouteAdmission:
            raise TypeError("route admission must be produced by the Admission Kernel")

        def __init__(
            self,
            disposition: RouteDecisionDisposition,
            selected: RouteCandidate | None,
            eligible_route_ids: tuple[str, ...],
            rejections: tuple[RouteRejection, ...],
            reason: str,
        ) -> None:
            raise TypeError("route admission must be produced by the Admission Kernel")

        @staticmethod
        def _trusted_state(instance: KernelRouteAdmission) -> AdmissionState:
            try:
                state = object.__getattribute__(instance, "_sealed_state")
            except AttributeError as exc:
                raise TypeError("unpublished route admission") from exc
            entry = trusted.get(id(instance))
            if entry is None or entry[0]() is not instance or entry[1] is not state:
                raise TypeError("unpublished route admission")
            if state.selected is not None and candidate_fingerprint(state.selected) != (
                state.selected_fingerprint
            ):
                raise TypeError("route admission state was mutated")
            if tuple(rejection_fingerprint(item) for item in state.rejections) != (
                state.rejection_fingerprints
            ):
                raise TypeError("route admission state was mutated")
            return state

        @staticmethod
        def _validate(
            disposition: RouteDecisionDisposition,
            selected: RouteCandidate | None,
            eligible_route_ids: tuple[str, ...],
            rejections: tuple[RouteRejection, ...],
            reason: str,
        ) -> None:
            if not isinstance(disposition, RouteDecisionDisposition):
                raise ValueError("disposition must be a RouteDecisionDisposition")
            if selected is not None and type(selected) is not RouteCandidate:
                raise ValueError("selected route must be a RouteCandidate")
            if disposition is RouteDecisionDisposition.ADMITTED and selected is None:
                raise ValueError("admitted route decision requires a selected candidate")
            if disposition is RouteDecisionDisposition.BLOCKED and selected is not None:
                raise ValueError("blocked route decision must not select a candidate")
            if not isinstance(eligible_route_ids, tuple):
                raise ValueError("eligible route IDs must be an ordered tuple")
            if len(eligible_route_ids) > MAX_ROUTE_CANDIDATES:
                raise ValueError("eligible route IDs exceed their bound")
            if any(
                not isinstance(route_id, str) or not _SAFE_ROUTE_ID.fullmatch(route_id)
                for route_id in eligible_route_ids
            ):
                raise ValueError("eligible route IDs are invalid")
            if len(set(eligible_route_ids)) != len(eligible_route_ids):
                raise ValueError("eligible route IDs must be unique")
            if disposition is RouteDecisionDisposition.ADMITTED:
                if not eligible_route_ids:
                    raise ValueError("admitted route decision requires eligible routes")
                if selected.route_id not in eligible_route_ids:  # type: ignore[union-attr]
                    raise ValueError("selected route must be eligible")
            elif eligible_route_ids:
                raise ValueError("blocked route decision must not contain eligible routes")
            if not isinstance(rejections, tuple):
                raise ValueError("route rejections must be an ordered tuple")
            if len(rejections) > MAX_ROUTE_CANDIDATES:
                raise ValueError("route rejections exceed their bound")
            if not all(type(rejection) is RouteRejection for rejection in rejections):
                raise ValueError("route rejections are invalid")
            rejection_ids = tuple(rejection.route_id for rejection in rejections)
            if len(set(rejection_ids)) != len(rejection_ids):
                raise ValueError("route rejections must be unique")
            if set(rejection_ids).intersection(eligible_route_ids):
                raise ValueError("route cannot be both eligible and rejected")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("route admission reason must be non-empty")
            if len(reason) > MAX_ROUTE_FIELD_CHARS:
                raise ValueError("route admission reason exceeds its bound")

        @property
        def disposition(self) -> RouteDecisionDisposition:
            return self._trusted_state(self).disposition

        @property
        def selected(self) -> RouteCandidate | None:
            return self._trusted_state(self).selected

        @property
        def eligible_route_ids(self) -> tuple[str, ...]:
            return self._trusted_state(self).eligible_route_ids

        @property
        def rejections(self) -> tuple[RouteRejection, ...]:
            return self._trusted_state(self).rejections

        @property
        def reason(self) -> str:
            return self._trusted_state(self).reason

        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError("RouteAdmission is immutable")

        def __delattr__(self, name: str) -> None:
            raise AttributeError("RouteAdmission is immutable")

        def __copy__(self) -> KernelRouteAdmission:
            self._trusted_state(self)
            return self

        def __deepcopy__(self, memo: dict[int, object]) -> KernelRouteAdmission:
            self._trusted_state(self)
            memo[id(self)] = self
            return self

        def __reduce__(self) -> object:
            raise TypeError("RouteAdmission cannot be pickled")

        def __reduce_ex__(self, protocol: int) -> object:
            raise TypeError("RouteAdmission cannot be pickled")

        def __repr__(self) -> str:
            state = self._trusted_state(self)
            return (
                "RouteAdmission("
                f"disposition={state.disposition!r}, selected={state.selected!r}, "
                f"eligible_route_ids={state.eligible_route_ids!r}, "
                f"rejections={state.rejections!r}, reason={state.reason!r})"
            )

        def __eq__(self, other: object) -> bool:
            if not isinstance(other, KernelRouteAdmission):
                return NotImplemented
            try:
                return self._trusted_state(self) == self._trusted_state(other)
            except TypeError:
                return False

        def __hash__(self) -> int:
            state = self._trusted_state(self)
            return hash(
                (
                    state.disposition,
                    state.selected_fingerprint,
                    state.eligible_route_ids,
                    state.rejection_fingerprints,
                    state.reason,
                )
            )

        @property
        def admitted(self) -> bool:
            return self._trusted_state(self).disposition is RouteDecisionDisposition.ADMITTED

        def to_contract_data(self) -> dict[str, object]:
            state = self._trusted_state(self)
            return {
                "disposition": state.disposition.value,
                "selected_route_id": state.selected.route_id if state.selected else None,
                "eligible_route_ids": list(state.eligible_route_ids),
                "rejections": [item.to_contract_data() for item in state.rejections],
                "reason": state.reason,
            }

    def admit(
        registry: RouteRegistry,
        requirements: RouteRequirements,
        *,
        advisor_order: Sequence[str] = (),
    ) -> KernelRouteAdmission:
        if type(registry) is not RouteRegistry:
            raise TypeError("registry must be a RouteRegistry")
        if type(requirements) is not RouteRequirements:
            raise TypeError("requirements must be RouteRequirements")
        advisor_rank = _advisor_rank(advisor_order, registry)
        eligible: list[RouteCandidate] = []
        rejections: list[RouteRejection] = []

        def publish(
            disposition: RouteDecisionDisposition,
            selected: RouteCandidate | None,
            eligible_route_ids: tuple[str, ...],
            rejected: tuple[RouteRejection, ...],
            reason: str,
        ) -> KernelRouteAdmission:
            KernelRouteAdmission._validate(
                disposition, selected, eligible_route_ids, rejected, reason
            )
            instance = object.__new__(KernelRouteAdmission)
            state = AdmissionState(
                disposition,
                selected,
                candidate_fingerprint(selected),
                eligible_route_ids,
                rejected,
                tuple(rejection_fingerprint(item) for item in rejected),
                reason,
            )
            object.__setattr__(instance, "_sealed_state", state)
            identifier = id(instance)

            def remove(reference: weakref.ReferenceType[object]) -> None:
                current = trusted.get(identifier)
                if current is not None and current[0] is reference:
                    trusted.pop(identifier, None)

            trusted[identifier] = (weakref.ref(instance, remove), state)
            return instance

        required_capabilities = set(requirements.required_capabilities)
        allowed_harnesses = set(requirements.allowed_harnesses)
        for candidate in registry.candidates:
            reasons: list[RouteRejectionCode] = []
            if not candidate.enabled:
                reasons.append(RouteRejectionCode.DISABLED)
            pin_checks = (
                (
                    requirements.pinned_route_id,
                    candidate.route_id,
                    RouteRejectionCode.ROUTE_PIN_MISMATCH,
                ),
                (requirements.pinned_model, candidate.model, RouteRejectionCode.MODEL_PIN_MISMATCH),
                (
                    requirements.pinned_harness,
                    candidate.harness,
                    RouteRejectionCode.HARNESS_PIN_MISMATCH,
                ),
                (
                    requirements.pinned_persona,
                    candidate.persona,
                    RouteRejectionCode.PERSONA_PIN_MISMATCH,
                ),
                (
                    requirements.pinned_tool_policy,
                    candidate.tool_policy,
                    RouteRejectionCode.TOOL_POLICY_PIN_MISMATCH,
                ),
                (
                    requirements.pinned_authority_identity,
                    candidate.authority_identity,
                    RouteRejectionCode.AUTHORITY_IDENTITY_PIN_MISMATCH,
                ),
            )
            reasons.extend(
                code
                for expected, actual, code in pin_checks
                if expected is not None and actual != expected
            )
            if allowed_harnesses and candidate.harness not in allowed_harnesses:
                reasons.append(RouteRejectionCode.HARNESS_NOT_ALLOWED)
            if (
                requirements.required_effort is not None
                and candidate.effort != requirements.required_effort
            ):
                reasons.append(RouteRejectionCode.EFFORT_MISMATCH)
            if required_capabilities.difference(candidate.capabilities):
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
            return publish(
                RouteDecisionDisposition.BLOCKED, None, (), tuple(rejections), "no_eligible_route"
            )
        return publish(
            RouteDecisionDisposition.ADMITTED,
            eligible[0],
            tuple(candidate.route_id for candidate in eligible),
            tuple(rejections),
            "cheapest_eligible_route",
        )

    return KernelRouteAdmission, admit


if TYPE_CHECKING:

    class RouteAdmission:
        """Public static contract for a Kernel-produced admission result.

        The runtime implementation is intentionally constructed in a factory
        so its publication bookkeeping is not a module-level minting surface.
        Keeping this declaration in the type-checking branch gives external
        consumers a real importable type without changing that runtime
        boundary.
        """

        def __init__(self) -> NoReturn: ...

        @property
        def disposition(self) -> RouteDecisionDisposition: ...

        @property
        def selected(self) -> RouteCandidate | None: ...

        @property
        def eligible_route_ids(self) -> tuple[str, ...]: ...

        @property
        def rejections(self) -> tuple[RouteRejection, ...]: ...

        @property
        def reason(self) -> str: ...

        @property
        def admitted(self) -> bool: ...

        def to_contract_data(self) -> dict[str, object]: ...

    def admit_route(
        registry: RouteRegistry,
        requirements: RouteRequirements,
        *,
        advisor_order: Sequence[str] = (),
    ) -> RouteAdmission: ...
else:
    _kernel_route_admission, _kernel_admit_route = _build_admission_kernel()
    RouteAdmission = _kernel_route_admission
    admit_route = _kernel_admit_route
    # The kernel class is intentionally created once, but its local class name
    # is not part of the module namespace.  Publish a resolvable runtime
    # annotation so reflection sees the same public contract.
    _kernel_route_admission.__name__ = "RouteAdmission"
    _kernel_route_admission.__qualname__ = "RouteAdmission"
    _kernel_route_admission.__module__ = __name__
    _kernel_admit_route.__name__ = "admit_route"
    _kernel_admit_route.__qualname__ = "admit_route"
    _kernel_admit_route.__module__ = __name__
    _kernel_admit_route.__annotations__["return"] = RouteAdmission
    # The implementation class is built in a factory so its publication state
    # is not a module-level minting surface. Normalize all reflective method
    # hints to the exported type before the factory locals are discarded.
    RouteAdmission.__new__.__annotations__["return"] = RouteAdmission
    RouteAdmission._trusted_state.__annotations__["instance"] = RouteAdmission
    RouteAdmission._trusted_state.__annotations__["return"] = object
    RouteAdmission.__copy__.__annotations__["return"] = RouteAdmission
    RouteAdmission.__deepcopy__.__annotations__["return"] = RouteAdmission
    for _private_name in (
        "_AdmissionState",
        "_TRUSTED_ADMISSIONS",
        "_candidate_fingerprint",
        "_rejection_fingerprint",
    ):
        globals().pop(_private_name, None)
    del _build_admission_kernel, _kernel_route_admission, _kernel_admit_route, _private_name


def validate_admission(
    registry: RouteRegistry,
    requirements: RouteRequirements,
    admission: object,
    *,
    advisor_order: Sequence[str] = (),
) -> bool:
    """Validate an admission at the effect boundary.

    ``RouteAdmission`` is an immutable result value, not a self-authenticating
    capability. Python object/closure introspection can manufacture an
    object-shaped value, so a dispatcher must recompute the Kernel result from
    the live registry and exact requirements before applying side effects; this
    function is the mandatory check at that boundary.
    """

    if type(registry) is not RouteRegistry:
        return False
    if type(requirements) is not RouteRequirements:
        return False
    if type(admission) is not RouteAdmission:
        return False
    try:
        expected = admit_route(registry, requirements, advisor_order=advisor_order)
        return (
            admission.disposition is expected.disposition
            and _strict_candidate_fingerprint(admission.selected)
            == _strict_candidate_fingerprint(expected.selected)
            and _strict_route_id_tuple(admission.eligible_route_ids)
            == _strict_route_id_tuple(expected.eligible_route_ids)
            and tuple(_strict_rejection_fingerprint(item) for item in admission.rejections)
            == tuple(_strict_rejection_fingerprint(item) for item in expected.rejections)
            and type(admission.reason) is str
            and admission.reason == expected.reason
        )
    except Exception:
        return False


def _advisor_rank(
    advisor_order: Sequence[str],
    registry: RouteRegistry,
) -> dict[str, int]:
    if not isinstance(advisor_order, Sequence) or isinstance(
        advisor_order, str | bytes | bytearray
    ):
        return {}
    known = {candidate.route_id for candidate in registry.candidates}
    ranks: dict[str, int] = {}
    try:
        iterator = iter(advisor_order)
    except Exception:
        return {}
    for index in range(MAX_ADVISOR_ORDER + 1):
        try:
            value = next(iterator)
        except StopIteration:
            break
        except Exception:
            return {}
        if index >= MAX_ADVISOR_ORDER:
            return {}
        if not isinstance(value, str):
            return {}
        try:
            # Call the base ``str`` operations explicitly.  A hostile
            # subclass may override ``strip`` or ``__hash__``; advisory input
            # must degrade to the deterministic Kernel order rather than
            # raising from membership/insertion.
            route_id = str.strip(str.__str__(value))
        except Exception:
            return {}
        if type(route_id) is not str:
            return {}
        if not _SAFE_ROUTE_ID.fullmatch(route_id):
            return {}
        if route_id in known and route_id not in ranks:
            ranks[route_id] = len(ranks)
    return ranks


def _bounded_token(value: object, *, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        # Never retain a caller-controlled ``str`` subclass.  Equality,
        # hashing, and ordering of route semantics must be plain built-ins at
        # the live effect boundary.
        normalized = str.strip(str.__str__(value))
    except Exception as exc:
        raise ValueError(f"{field} has an invalid shape") from exc
    if type(normalized) is not str or not normalized:
        raise ValueError(f"{field} has an invalid shape")
    if (
        not normalized
        or len(normalized) > MAX_ROUTE_FIELD_CHARS
        or not pattern.fullmatch(normalized)
    ):
        raise ValueError(f"{field} has an invalid shape")
    return normalized


def _bounded_identity(value: object, *, field: str) -> str:
    normalized = _bounded_token(value, field=field, pattern=_SAFE_TOKEN)
    if not is_stable_authority_identity(normalized):
        raise ValueError(f"{field} must be a stable authority descriptor")
    return normalized


def _model_value(value: object, *, field: str) -> str:
    """Copy an existing public ``ModelConfig.model`` value without narrowing it."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        normalized = str.__str__(value)
    except Exception as exc:
        raise ValueError(f"{field} has an invalid shape") from exc
    if type(normalized) is not str or not normalized:
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
    if isinstance(values, Sized):
        try:
            size = len(values)
        except Exception:
            size = None
        if size is not None and size > max_count:
            raise ValueError(f"{field} exceeds its bound")
    try:
        iterator = iter(values)
    except Exception as exc:
        raise ValueError(f"{field} could not be iterated") from exc
    for index in range(max_count + 1):
        try:
            value = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            raise ValueError(f"{field} could not be iterated") from exc
        if index >= max_count:
            raise ValueError(f"{field} exceeds its bound")
        yield value


def _bounded_mapping_keys(
    value: Mapping[object, object],
    *,
    expected: frozenset[str],
    field: str,
) -> tuple[str, ...]:
    """Validate mapping shape without materializing an untrusted key stream."""

    try:
        iterator = iter(value)
    except Exception as exc:
        raise ValueError(f"{field} could not be iterated") from exc
    keys: list[str] = []
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            if len(keys) != len(expected):
                raise ValueError(f"{field} has an unsupported shape")
            return tuple(keys)
        except Exception as exc:
            raise ValueError(f"{field} could not be iterated") from exc
        if index >= len(expected):
            raise ValueError(f"{field} has an unsupported shape (too many keys)")
        if type(key) is not str or key not in expected or key in keys:
            raise ValueError(f"{field} has an unsupported shape")
        keys.append(key)
    raise ValueError(f"{field} has an unsupported shape (too many keys)")


__all__ = [
    "ROUTE_CONTRACT_VERSION",
    "MAX_ROUTE_CANDIDATES",
    "MAX_ROUTE_ORDINAL",
    "RouteAdmission",
    "RouteCandidate",
    "RouteDecisionDisposition",
    "RouteRegistry",
    "RouteRejection",
    "RouteRejectionCode",
    "RouteRequirements",
    "admit_route",
    "validate_admission",
]
