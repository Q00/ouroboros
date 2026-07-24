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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouroboros.config._model_defaults import normalize_tier_model
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
)

if TYPE_CHECKING:
    from ouroboros.config.models import EconomicsConfig


ROUTE_COMPAT_VERSION = 1
DEFAULT_ROUTE_PERSONA = "default"
DEFAULT_ROUTE_TOOL_POLICY = "default"
DEFAULT_ROUTE_AUTHORITY_PREFIX = "runtime:"
UNRESOLVED_ROUTE_ID = "compat:unresolved"
INVALID_CAPABILITY = "compat:invalid-capability"


def _bounded_tuple(values: Iterable[object], *, max_count: int) -> tuple[object, ...]:
    """Materialize an ordered caller input with a max-plus-one guard."""

    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("compatibility values must be an ordered iterable")
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
            "escalation_retry_threshold": self.escalation_retry_threshold,
            "tier_route_ids": [
                {"tier": tier, "route_id": route_id} for tier, route_id in self.tier_route_ids
            ],
            "registry": self.registry.to_contract_data(),
        }


def _configured_models(
    economics: EconomicsConfig,
    *,
    runtime_backend: str,
) -> dict[str, str]:
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
    try:
        routed = dict(model_router.tier_models)
    except Exception:
        return None
    if routed != configured or not routed:
        return None

    identity = authority_identity or f"{DEFAULT_ROUTE_AUTHORITY_PREFIX}{runtime_backend}"
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

    if projection is None or model_decision.tier is None or model_decision.model is None:
        # ``UNRESOLVED_ROUTE_ID`` is syntactically valid but absent from every
        # projection; the Kernel consequently returns a deterministic blocked
        # result with rejection code ``route_pin_mismatch``.
        if projection is None:
            # No registry exists to evaluate.  Build a one-entry disabled
            # registry so callers still receive a genuine Kernel-produced
            # blocked decision rather than an exception or an implicit bypass.
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
                        authority_identity="runtime:unresolved",
                        enabled=False,
                    ),
                )
            )
            return admit_route(
                fallback,
                RouteRequirements(pinned_route_id=UNRESOLVED_ROUTE_ID),
            )
        requirements = RouteRequirements(pinned_route_id=UNRESOLVED_ROUTE_ID)
        return admit_route(projection.registry, requirements)

    route_id = projection.route_id_for_tier(model_decision.tier)
    if route_id is None:
        route_id = UNRESOLVED_ROUTE_ID
    try:
        normalized_required_capabilities = _bounded_tuple(
            required_capabilities,
            max_count=MAX_ROUTE_CAPABILITIES,
        )
    except (TypeError, ValueError):
        # Preserve fail-closed semantics for a malformed hostile iterable.  A
        # capability that no compatibility candidate advertises guarantees a
        # genuine blocked Kernel result instead of silently dropping constraints.
        normalized_required_capabilities = (INVALID_CAPABILITY,)
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
    return admit_route(projection.registry, requirements)


def admitted_execute_model_kwargs(
    admission: RouteAdmission,
    *,
    model_decision: ModelDecision,
) -> dict[str, str]:
    """Return a model override only for an admitted, enforced decision."""

    if admission.admitted and model_decision.is_enforced and model_decision.model is not None:
        selected = admission.selected
        if selected is not None and selected.model == model_decision.model:
            return {"model": selected.model}
    return {}


def deserialize_route_compat_projection(value: object) -> RouteCompatProjection | None:
    """Parse a persisted projection without trusting its nested containers.

    This parser validates shape and reconstructs all Route B values.  Callers
    must still compare the result with the current economics/backend snapshot;
    a syntactically valid old contract is not permission to execute a new route.
    """

    if not isinstance(value, Mapping) or value.get("version") != ROUTE_COMPAT_VERSION:
        return None
    backend = value.get("runtime_backend")
    effort = value.get("effort")
    persona = value.get("persona")
    tool_policy = value.get("tool_policy")
    authority = value.get("authority_identity")
    child_tier = value.get("child_tier")
    base_tier = value.get("base_tier")
    threshold = value.get("escalation_retry_threshold")
    raw_registry = value.get("registry")
    raw_tiers = value.get("tier_route_ids")
    if (
        not isinstance(backend, str)
        or not isinstance(persona, str)
        or not isinstance(tool_policy, str)
        or not isinstance(authority, str)
        or not isinstance(child_tier, str)
        or not isinstance(base_tier, str)
        or child_tier not in MODEL_TIER_LADDER
        or base_tier not in MODEL_TIER_LADDER
        or isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or threshold < 1
        or (effort is not None and not isinstance(effort, str))
        or not isinstance(raw_tiers, list)
    ):
        return None
    try:
        registry = RouteRegistry.from_contract_data(raw_registry)
        tier_route_ids: list[tuple[str, str]] = []
        for index, item in enumerate(raw_tiers):
            if index >= MAX_ROUTE_CANDIDATES:
                return None
            if not isinstance(item, Mapping) or set(item) != {"tier", "route_id"}:
                return None
            tier = item["tier"]
            route_id = item["route_id"]
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

    if not isinstance(value, Mapping) or value.get("version") != ROUTE_COMPAT_VERSION:
        return False, None
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        return False, None
    if not enabled:
        return True, None
    projection = deserialize_route_compat_projection(value.get("projection"))
    return (projection is not None), projection


def validate_route_compat_projection(
    projection: RouteCompatProjection | None,
    economics: EconomicsConfig,
    *,
    model_router: ModelRouter | None,
    runtime_backend: str | None,
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
        effort=projection.effort,
        persona=projection.persona,
        tool_policy=projection.tool_policy,
        authority_identity=projection.authority_identity,
        capabilities=tuple(
            capability
            for candidate in projection.registry.candidates[:1]
            for capability in candidate.capabilities
        ),
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
    "validate_route_compat_projection",
]
