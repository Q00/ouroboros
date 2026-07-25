"""Compatibility projection from the live model/effort routers to Route B.

Routing B deliberately starts as a provider-neutral contract.  The live
orchestrator already has two older, independently tested decisions: model-tier
routing and reasoning-effort routing.  This module is the narrow compatibility
boundary between those decisions and :mod:`route_policy`.

The projection is intentionally a snapshot.  It rebuilds candidates from the
resolved economics configuration rather than trusting the mutable ``tier_models``
mapping on ``ModelRouter``.  A later mutation of a router, a changed cost, or a
backend mismatch therefore cannot silently turn into an authorized route: the
decision is pinned to the snapshot and the Admission Kernel fails closed.

No provider calls, retry policy, or Final Gate behavior belongs here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from ouroboros.config._model_defaults import normalize_tier_model
from ouroboros.core.security import is_stable_authority_identity
from ouroboros.orchestrator.contract_numbers import (
    json_safe_nonnegative_int,
    parse_positive_contract_int,
)
from ouroboros.orchestrator.model_routing import (
    _BACKEND_PROVIDER,
    MODEL_TIER_LADDER,
    ModelDecision,
    ModelRouter,
)
from ouroboros.orchestrator.route_policy import (
    MAX_ROUTE_CANDIDATES,
    MAX_ROUTE_CAPABILITIES,
    RouteAdmission,
    RouteCandidate,
    RouteRegistry,
    RouteRequirements,
    admit_route,
    validate_admission,
)

if TYPE_CHECKING:
    from ouroboros.config.models import EconomicsConfig


ROUTE_COMPAT_VERSION = 1
DEFAULT_ROUTE_PERSONA = "default"
DEFAULT_ROUTE_TOOL_POLICY = "default"
DEFAULT_ROUTE_AUTHORITY_PREFIX = "runtime:"
UNRESOLVED_ROUTE_ID = "compat:unresolved"
INVALID_CAPABILITY = "compat:invalid-capability"
_SAFE_COMPAT_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")


def _canonical_compat_token(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if (
        not isinstance(normalized, str)
        or not normalized
        or not _SAFE_COMPAT_TOKEN.fullmatch(normalized)
    ):
        raise ValueError(f"{field} has an invalid shape")
    return normalized


def _bounded_tuple(values: Iterable[object], *, max_count: int) -> tuple[object, ...]:
    """Materialize an ordered caller input with a max-plus-one guard."""

    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("compatibility values must be an ordered iterable")
    if isinstance(values, (Set, Mapping)):
        raise ValueError("compatibility values must be ordered")
    try:
        iterator = iter(values)
    except Exception as exc:
        raise ValueError("compatibility values could not be iterated") from exc
    result: list[object] = []
    for index in range(max_count + 1):
        try:
            value = next(iterator)
        except StopIteration:
            return tuple(result)
        except Exception as exc:
            raise ValueError("compatibility values could not be iterated") from exc
        if index >= max_count:
            raise ValueError("compatibility values exceed their bound")
        result.append(value)
    raise ValueError("compatibility values exceed their bound")


def _has_bounded_mapping_keys(
    value: Mapping[object, object],
    *,
    expected: frozenset[str],
) -> bool:
    """Check an untrusted mapping's exact keys without materializing a tail."""

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


@dataclass(frozen=True, slots=True)
class RouteCompatProjection:
    """Immutable compatibility snapshot for one runtime/backend.

    ``tier_route_ids`` is a tuple rather than a mapping so callers cannot mutate
    the projection after it has been used to construct an admission.  The
    registry itself is a frozen Route B value and owns the copied candidates.
    """

    registry: RouteRegistry
    runtime_backend: str
    tier_route_ids: tuple[tuple[str, str], ...]
    effort: str | None
    persona: str
    tool_policy: str
    authority_identity: str
    child_tier: str
    base_tier: str
    escalation_retry_threshold: int

    def __post_init__(self) -> None:
        if not isinstance(self.registry, RouteRegistry):
            raise ValueError("compatibility projection registry is invalid")
        runtime_backend = _canonical_compat_token(
            self.runtime_backend,
            field="runtime_backend",
        )
        if runtime_backend not in _BACKEND_PROVIDER:
            raise ValueError("runtime_backend is not supported by compatibility routing")
        effort = (
            None if self.effort is None else _canonical_compat_token(self.effort, field="effort")
        )
        persona = _canonical_compat_token(self.persona, field="persona")
        tool_policy = _canonical_compat_token(self.tool_policy, field="tool_policy")
        authority_identity = _canonical_compat_token(
            self.authority_identity,
            field="authority_identity",
        )
        if not is_stable_authority_identity(authority_identity):
            raise ValueError("authority_identity must be a stable authority descriptor")
        if self.child_tier not in MODEL_TIER_LADDER:
            raise ValueError("child_tier is invalid")
        if self.base_tier not in MODEL_TIER_LADDER:
            raise ValueError("base_tier is invalid")
        if type(self.escalation_retry_threshold) is not int or self.escalation_retry_threshold < 1:
            raise ValueError("escalation_retry_threshold is invalid")
        if not isinstance(self.tier_route_ids, tuple):
            raise ValueError("tier_route_ids must be an ordered tuple")
        if not self.tier_route_ids or len(self.tier_route_ids) > MAX_ROUTE_CANDIDATES:
            raise ValueError("tier_route_ids exceed their bound")
        expected_route_ids = tuple(candidate.route_id for candidate in self.registry.candidates)
        seen_tiers: set[str] = set()
        for item in self.tier_route_ids:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("tier_route_ids entries are invalid")
            tier, route_id = item
            if (
                not isinstance(tier, str)
                or tier not in MODEL_TIER_LADDER
                or tier in seen_tiers
                or not isinstance(route_id, str)
                or route_id != f"compat:{runtime_backend}:{tier}"
            ):
                raise ValueError("tier_route_ids entries are invalid")
            seen_tiers.add(tier)
        if tuple(route_id for _, route_id in self.tier_route_ids) != expected_route_ids:
            raise ValueError("tier_route_ids do not match the registry")
        for candidate in self.registry.candidates:
            if (
                candidate.harness != runtime_backend
                or candidate.effort != effort
                or candidate.persona != persona
                or candidate.tool_policy != tool_policy
                or candidate.authority_identity != authority_identity
            ):
                raise ValueError("projection metadata does not match its registry")
        object.__setattr__(self, "runtime_backend", runtime_backend)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "persona", persona)
        object.__setattr__(self, "tool_policy", tool_policy)
        object.__setattr__(self, "authority_identity", authority_identity)

    def route_id_for_tier(self, tier: str | None) -> str | None:
        """Return the snapshotted route id for ``tier`` if it exists."""

        if tier is None:
            return None
        return dict(self.tier_route_ids).get(tier)

    def candidate_for_tier(self, tier: str | None) -> RouteCandidate | None:
        """Return the snapshotted candidate for ``tier`` if it exists."""

        route_id = self.route_id_for_tier(tier)
        if route_id is None:
            return None
        return next(
            (candidate for candidate in self.registry.candidates if candidate.route_id == route_id),
            None,
        )

    def to_contract_data(self) -> dict[str, object]:
        """Return deterministic, JSON-safe projection data for a checkpoint."""

        return {
            "version": ROUTE_COMPAT_VERSION,
            "runtime_backend": self.runtime_backend,
            "effort": self.effort,
            "persona": self.persona,
            "tool_policy": self.tool_policy,
            "authority_identity": self.authority_identity,
            "child_tier": self.child_tier,
            "base_tier": self.base_tier,
            "escalation_retry_threshold": json_safe_nonnegative_int(
                self.escalation_retry_threshold
            ),
            "tier_route_ids": [
                {"tier": tier, "route_id": route_id} for tier, route_id in self.tier_route_ids
            ],
            "registry": self.registry.to_contract_data(),
        }


def _configured_models(
    economics: EconomicsConfig,
    *,
    runtime_backend: str,
) -> dict[str, str] | None:
    """Resolve the immutable config catalog for a runtime backend."""

    provider = _BACKEND_PROVIDER.get(runtime_backend)
    if provider is None:
        return {}
    configured: dict[str, str] = {}
    for tier in MODEL_TIER_LADDER:
        tier_config = economics.tiers.get(tier)
        if tier_config is None:
            continue
        for model_config in tier_config.models:
            if model_config.provider == provider:
                configured[tier] = normalize_tier_model(
                    model_config.model,
                    provider=model_config.provider,
                )
                break
    return configured


def _snapshot_router_models(model_router: ModelRouter) -> dict[str, str] | None:
    """Copy a router catalog with a finite tier bound."""

    raw = model_router.tier_models
    if not isinstance(raw, Mapping):
        return None
    try:
        iterator = iter(raw.items())
        snapshot: dict[str, str] = {}
        for index in range(len(MODEL_TIER_LADDER) + 1):
            try:
                item = next(iterator)
            except StopIteration:
                return snapshot
            except Exception:
                return None
            if index >= len(MODEL_TIER_LADDER):
                return None
            tier, model = item
            if (
                not isinstance(tier, str)
                or tier in snapshot
                or tier not in MODEL_TIER_LADDER
                or not isinstance(model, str)
            ):
                return None
            snapshot[tier] = model
    except Exception:
        return None
    return None


def build_route_compat_projection(
    economics: EconomicsConfig,
    *,
    model_router: ModelRouter | None,
    runtime_backend: str | None,
    effort: str | None = None,
    persona: str = DEFAULT_ROUTE_PERSONA,
    tool_policy: str = DEFAULT_ROUTE_TOOL_POLICY,
    authority_identity: str | None = None,
    capabilities: Iterable[object] = (),
) -> RouteCompatProjection | None:
    """Build a Route B registry that is compatible with the existing routers.

    The result is ``None`` when model routing is dormant.  A non-dormant router
    is accepted only when its backend and tier/model catalog exactly match the
    economics snapshot.  This prevents a mutable or tampered resume router from
    supplying a model or cost that was never configured.
    """

    if economics is None or model_router is None or runtime_backend is None:
        return None
    if model_router.runtime_backend != runtime_backend:
        return None
    configured = _configured_models(economics, runtime_backend=runtime_backend)
    if configured is None:
        return None
    routed = _snapshot_router_models(model_router)
    if routed is None:
        return None
    if routed != configured or not routed:
        return None

    identity = (
        f"{DEFAULT_ROUTE_AUTHORITY_PREFIX}{runtime_backend}"
        if authority_identity is None
        else authority_identity
    )
    if (
        not isinstance(model_router.child_tier, str)
        or not isinstance(model_router.base_tier, str)
        or model_router.child_tier not in MODEL_TIER_LADDER
        or model_router.base_tier not in MODEL_TIER_LADDER
        or type(model_router.escalation_retry_threshold) is not int
        or model_router.escalation_retry_threshold < 1
        or model_router.escalation_retry_threshold != economics.escalation_threshold
    ):
        return None
    try:
        # Materialize once so an untrusted iterable cannot change between
        # candidate construction and the registry's duplicate/bounds checks.
        capability_tokens = _bounded_tuple(
            capabilities,
            max_count=MAX_ROUTE_CAPABILITIES,
        )
        candidates: list[RouteCandidate] = []
        tier_route_ids: list[tuple[str, str]] = []
        for ordinal, tier in enumerate(MODEL_TIER_LADDER):
            model = configured.get(tier)
            tier_config = economics.tiers.get(tier)
            if model is None or tier_config is None:
                continue
            route_id = f"compat:{runtime_backend}:{tier}"
            candidates.append(
                RouteCandidate(
                    route_id=route_id,
                    model=model,
                    harness=runtime_backend,
                    effort=effort,
                    cost_units=tier_config.cost_factor,
                    persona=persona,
                    tool_policy=tool_policy,
                    authority_identity=identity,
                    capabilities=capability_tokens,
                    ordinal=ordinal,
                )
            )
            tier_route_ids.append((tier, route_id))
        if not candidates:
            return None
        registry = RouteRegistry(candidates=tuple(candidates))
    except (TypeError, ValueError):
        # Compatibility is an optional bridge.  Any malformed config is a
        # blocked route at the caller, never a reason to bypass Route B.
        return None
    return RouteCompatProjection(
        registry=registry,
        runtime_backend=runtime_backend,
        tier_route_ids=tuple(tier_route_ids),
        effort=effort,
        persona=persona,
        tool_policy=tool_policy,
        authority_identity=identity,
        child_tier=model_router.child_tier,
        base_tier=model_router.base_tier,
        escalation_retry_threshold=model_router.escalation_retry_threshold,
    )


def admit_compat_route(
    projection: RouteCompatProjection | None,
    *,
    model_decision: ModelDecision,
    effort: str | None,
    required_capabilities: Iterable[object] = (),
) -> RouteAdmission:
    """Pin an existing model/effort decision through the Admission Kernel.

    A missing projection or unresolved model is represented as a normal Kernel
    ``blocked`` decision, not as an exception and not as permission to continue
    to the provider.  The selected model, backend, effort, persona, tool policy,
    and authority identity are all pinned so a changed catalog cannot pass by
    merely retaining the same tier name.
    """

    if not isinstance(model_decision, ModelDecision):
        return _blocked_compat_admission(projection)
    if projection is None or model_decision.tier is None or model_decision.model is None:
        # ``UNRESOLVED_ROUTE_ID`` is syntactically valid but absent from every
        # projection; the Kernel consequently returns a deterministic blocked
        # result with rejection code ``route_pin_mismatch``.
        if projection is None:
            # No registry exists to evaluate.  Build a one-entry disabled
            # registry so callers still receive a genuine Kernel-produced
            # blocked decision rather than an exception or an implicit bypass.
            return _blocked_compat_admission(None)
        try:
            requirements = RouteRequirements(pinned_route_id=UNRESOLVED_ROUTE_ID)
            return admit_route(projection.registry, requirements)
        except Exception:
            return _blocked_compat_admission(projection)

    try:
        requirements = _compat_requirements(
            projection,
            model_decision=model_decision,
            effort=effort,
            required_capabilities=required_capabilities,
        )
        if requirements is None:
            return _blocked_compat_admission(projection)
        return admit_route(projection.registry, requirements)
    except Exception:
        # The compatibility bridge is an optional fail-closed layer.  A
        # malformed runtime decision must never become an implicit provider
        # dispatch or leak an input-validation exception to the executor.
        return _blocked_compat_admission(projection)


def _blocked_compat_admission(projection: RouteCompatProjection | None) -> RouteAdmission:
    """Return a genuine Kernel-produced blocked decision for invalid input."""

    try:
        registry = projection.registry if isinstance(projection, RouteCompatProjection) else None
        if registry is None:
            registry = RouteRegistry(
                candidates=(
                    RouteCandidate(
                        route_id=UNRESOLVED_ROUTE_ID,
                        model="unresolved",
                        harness="unresolved",
                        effort=None,
                        cost_units=0,
                        persona=DEFAULT_ROUTE_PERSONA,
                        tool_policy=DEFAULT_ROUTE_TOOL_POLICY,
                        authority_identity="authority-default",
                        enabled=False,
                    ),
                )
            )
        return admit_route(registry, RouteRequirements(pinned_route_id=UNRESOLVED_ROUTE_ID))
    except Exception:
        # This branch is defensive only; the fixed fallback above is a valid
        # RouteRegistry.  Avoid raising from a compatibility guard if a caller
        # supplies an object with hostile attribute access.
        fallback = RouteRegistry(
            candidates=(
                RouteCandidate(
                    route_id=UNRESOLVED_ROUTE_ID,
                    model="unresolved",
                    harness="unresolved",
                    effort=None,
                    cost_units=0,
                    persona=DEFAULT_ROUTE_PERSONA,
                    tool_policy=DEFAULT_ROUTE_TOOL_POLICY,
                    authority_identity="authority-default",
                    enabled=False,
                ),
            )
        )
        return admit_route(fallback, RouteRequirements(pinned_route_id=UNRESOLVED_ROUTE_ID))


def _compat_requirements(
    projection: RouteCompatProjection,
    *,
    model_decision: ModelDecision,
    effort: str | None,
    required_capabilities: Iterable[object],
) -> RouteRequirements | None:
    if not isinstance(projection, RouteCompatProjection):
        return None
    tier = model_decision.tier
    model = model_decision.model
    if not isinstance(tier, str) or not isinstance(model, str):
        return None
    route_id = projection.route_id_for_tier(tier)
    if route_id is None:
        route_id = UNRESOLVED_ROUTE_ID
    try:
        normalized_required_capabilities = _bounded_tuple(
            required_capabilities,
            max_count=MAX_ROUTE_CAPABILITIES,
        )
    except (TypeError, ValueError):
        # A sentinel can collide with configured capabilities. Return no
        # requirements so callers produce a guaranteed Kernel block instead.
        return None
    requirements = RouteRequirements(
        required_capabilities=normalized_required_capabilities,
        allowed_harnesses=(projection.runtime_backend,),
        required_effort=effort,
        pinned_route_id=route_id,
        pinned_model=model_decision.model,
        pinned_harness=projection.runtime_backend,
        pinned_persona=projection.persona,
        pinned_tool_policy=projection.tool_policy,
        pinned_authority_identity=projection.authority_identity,
    )
    return requirements


def admitted_execute_model_kwargs(
    admission: RouteAdmission,
    *,
    model_decision: ModelDecision,
    projection: RouteCompatProjection | None = None,
    effort: str | None = None,
    required_capabilities: Iterable[object] = (),
) -> dict[str, str]:
    """Return a model override only after projection-boundary revalidation.

    ``projection`` is intentionally required in practice.  Without the live
    registry and exact compatibility requirements there is no safe way to
    distinguish a Kernel result from a forged or stale admission, so the
    function returns an empty override.
    """

    if not isinstance(model_decision, ModelDecision) or not model_decision.is_enforced:
        return {}
    if not validate_compat_admission(
        projection,
        admission,
        model_decision=model_decision,
        effort=effort,
        required_capabilities=required_capabilities,
    ):
        return {}
    try:
        if admission.admitted and model_decision.model is not None:
            selected = admission.selected
            if selected is not None and selected.model == model_decision.model:
                return {"model": selected.model}
    except Exception:
        return {}
    return {}


def validate_compat_admission(
    projection: RouteCompatProjection | None,
    admission: RouteAdmission,
    *,
    model_decision: ModelDecision,
    effort: str | None,
    required_capabilities: Iterable[object] = (),
) -> bool:
    """Revalidate one carried admission against the current live projection."""

    if not isinstance(model_decision, ModelDecision) or projection is None:
        return False
    try:
        requirements = _compat_requirements(
            projection,
            model_decision=model_decision,
            effort=effort,
            required_capabilities=required_capabilities,
        )
        return requirements is not None and validate_admission(
            projection.registry,
            requirements,
            admission,
        )
    except Exception:
        return False


def deserialize_route_compat_projection(value: object) -> RouteCompatProjection | None:
    """Parse a persisted projection without trusting its nested containers.

    This parser validates shape and reconstructs all Route B values.  Callers
    must still compare the result with the current economics/backend snapshot;
    a syntactically valid old contract is not permission to execute a new route.
    """

    expected_fields = {
        "version",
        "runtime_backend",
        "effort",
        "persona",
        "tool_policy",
        "authority_identity",
        "child_tier",
        "base_tier",
        "escalation_retry_threshold",
        "tier_route_ids",
        "registry",
    }
    if not isinstance(value, Mapping) or not _has_bounded_mapping_keys(
        value,
        expected=frozenset(expected_fields),
    ):
        return None
    try:
        version = value["version"]
        if type(version) is not int or version != ROUTE_COMPAT_VERSION:
            return None
        backend = value["runtime_backend"]
        effort = value["effort"]
        persona = value["persona"]
        tool_policy = value["tool_policy"]
        authority = value["authority_identity"]
        child_tier = value["child_tier"]
        base_tier = value["base_tier"]
        threshold = parse_positive_contract_int(value["escalation_retry_threshold"])
        raw_registry = value["registry"]
        raw_tiers = value["tier_route_ids"]
    except Exception:
        return None
    if (
        not isinstance(backend, str)
        or not isinstance(persona, str)
        or not isinstance(tool_policy, str)
        or not isinstance(authority, str)
        or not isinstance(child_tier, str)
        or not isinstance(base_tier, str)
        or child_tier not in MODEL_TIER_LADDER
        or base_tier not in MODEL_TIER_LADDER
        or threshold is None
        or (effort is not None and not isinstance(effort, str))
        or not isinstance(raw_tiers, list)
        or len(raw_tiers) > MAX_ROUTE_CANDIDATES
    ):
        return None
    try:
        registry = RouteRegistry.from_contract_data(raw_registry)
        tier_route_ids: list[tuple[str, str]] = []
        for index, item in enumerate(raw_tiers):
            if index >= MAX_ROUTE_CANDIDATES:
                return None
            if not isinstance(item, Mapping) or not _has_bounded_mapping_keys(
                item,
                expected=frozenset({"tier", "route_id"}),
            ):
                return None
            try:
                tier = item["tier"]
                route_id = item["route_id"]
            except Exception:
                return None
            if not isinstance(tier, str) or not isinstance(route_id, str):
                return None
            if tier not in MODEL_TIER_LADDER or tier_route_ids and tier in dict(tier_route_ids):
                return None
            tier_route_ids.append((tier, route_id))
        if tuple(route_id for _, route_id in tier_route_ids) != tuple(
            candidate.route_id for candidate in registry.candidates
        ):
            return None
        return RouteCompatProjection(
            registry=registry,
            runtime_backend=backend,
            tier_route_ids=tuple(tier_route_ids),
            effort=effort,
            persona=persona,
            tool_policy=tool_policy,
            authority_identity=authority,
            child_tier=child_tier,
            base_tier=base_tier,
            escalation_retry_threshold=threshold,
        )
    except (TypeError, ValueError):
        return None


def serialize_route_compat_contract(
    projection: RouteCompatProjection | None,
) -> dict[str, object]:
    """Serialize an explicit enabled/dormant compatibility contract."""

    if projection is None:
        return {"version": ROUTE_COMPAT_VERSION, "enabled": False}
    return {
        "version": ROUTE_COMPAT_VERSION,
        "enabled": True,
        "projection": projection.to_contract_data(),
    }


def deserialize_route_compat_contract(
    value: object,
) -> tuple[bool, RouteCompatProjection | None]:
    """Parse a compatibility contract while preserving dormant-vs-invalid."""

    if not isinstance(value, Mapping) or not (
        _has_bounded_mapping_keys(
            value,
            expected=frozenset({"version", "enabled", "projection"}),
        )
        or _has_bounded_mapping_keys(
            value,
            expected=frozenset({"version", "enabled"}),
        )
    ):
        return False, None
    try:
        version = value["version"]
        if type(version) is not int or version != ROUTE_COMPAT_VERSION:
            return False, None
        enabled = value["enabled"]
    except Exception:
        return False, None
    if not isinstance(enabled, bool):
        return False, None
    if not enabled:
        # Dormant contracts intentionally omit ``projection``.  The bounded
        # key check above allows the three-key enabled shape, so validate the
        # two-key dormant shape with the same finite parser before accepting it.
        if not _has_bounded_mapping_keys(
            value,
            expected=frozenset({"version", "enabled"}),
        ):
            return False, None
        return True, None
    try:
        projection_value = value["projection"]
    except Exception:
        return False, None
    projection = deserialize_route_compat_projection(projection_value)
    return (projection is not None), projection


def validate_route_compat_projection(
    projection: RouteCompatProjection | None,
    economics: EconomicsConfig,
    *,
    model_router: ModelRouter | None,
    runtime_backend: str | None,
    current_effort: str | None = None,
    current_persona: str = DEFAULT_ROUTE_PERSONA,
    current_tool_policy: str = DEFAULT_ROUTE_TOOL_POLICY,
    current_authority_identity: str | None = None,
    current_capabilities: Iterable[object] = (),
) -> bool:
    """Compare a persisted projection with a freshly resolved catalog.

    Parsing proves only that a payload is well-shaped.  This second check is the
    resume/effect-boundary defense: costs, model ids, backend, and route identity
    dimensions must still equal the current immutable configuration snapshot.
    """

    if projection is None:
        return False
    expected = build_route_compat_projection(
        economics,
        model_router=model_router,
        runtime_backend=runtime_backend,
        effort=current_effort,
        persona=current_persona,
        tool_policy=current_tool_policy,
        authority_identity=current_authority_identity,
        capabilities=current_capabilities,
    )
    return expected == projection


__all__ = [
    "DEFAULT_ROUTE_AUTHORITY_PREFIX",
    "DEFAULT_ROUTE_PERSONA",
    "DEFAULT_ROUTE_TOOL_POLICY",
    "INVALID_CAPABILITY",
    "ROUTE_COMPAT_VERSION",
    "RouteCompatProjection",
    "admit_compat_route",
    "admitted_execute_model_kwargs",
    "build_route_compat_projection",
    "deserialize_route_compat_contract",
    "deserialize_route_compat_projection",
    "serialize_route_compat_contract",
    "validate_compat_admission",
    "validate_route_compat_projection",
]
