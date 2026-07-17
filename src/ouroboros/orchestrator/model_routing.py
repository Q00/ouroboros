"""Model-tier investment routing for the Agent-OS execution contract.

The sibling of :mod:`ouroboros.orchestrator.effort_routing`. Where effort routing
picks *how much reasoning* a unit of work gets, this module picks *which model
tier* runs it — the frugality lever ``ooo run`` leans on. The RLM thesis is that
decomposing a big goal into small, verified-MECE acceptance criteria makes each
child easy enough to run on a cheaper model, so trusted decomposed children drop
one tier (``standard`` -> ``frugal`` -> haiku) while top-level ACs keep today's
default (``standard`` -> sonnet). Child status alone is insufficient; the drop is
admitted only by an explicit decomposition-trust signal. A failing AC earns a
stronger model on retry, and one that keeps failing climbs the ladder
progressively — one tier per retry past the escalation threshold, capped at the
frontier ceiling.

Note the deliberate asymmetry with effort routing V5: that module stopped lowering
a decomposed child's *reasoning depth* (a harder child needs at least as much
thinking as its parent). This module still lowers a trusted child's *model tier* —
a child keeps its reasoning depth but runs on a cheaper model, because trusted
decomposition, not weaker reasoning, is what makes it affordable.

Like effort routing, this is a single, pure decision point free of executor state:
the orchestrator decides a tier, maps it to a backend-executable model id, and the
runtime either ENFORCES the choice through a native per-call model override or is
merely *advised* of it. Keeping the policy stateless makes it testable in isolation
and keeps the live executor a thin caller on the capability contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ouroboros.config._model_defaults import normalize_tier_model
from ouroboros.orchestrator.adapter import ParamSupport

if TYPE_CHECKING:
    from ouroboros.config.models import EconomicsConfig

# Ordered weakest -> strongest. Matches the tier names in
# :class:`~ouroboros.config.models.EconomicsConfig` (``frugal``/``standard``/
# ``frontier``) so this ladder and the persisted config share one vocabulary.
MODEL_TIER_LADDER: tuple[str, ...] = ("frugal", "standard", "frontier")

# Floor for the one-notch-lower helper: never cheaper than the frugal tier.
DEFAULT_TIER_FLOOR = MODEL_TIER_LADDER[0]

# Ceiling for the retry raise rule: never stronger than the frontier tier.
DEFAULT_TIER_CEILING = MODEL_TIER_LADDER[-1]

# Model-tier modes recorded per unit so enforced rows can be told apart from
# advised ones — the distinction the deterministic frugality proof depends on.
MODEL_MODE_ENFORCED = "enforced"
MODEL_MODE_ADVISED = "advised"
MODEL_MODE_NONE = "none"

# Persisted resolved-router schema. A run stores the concrete backend model map,
# not just the source config knobs, so a later resume cannot silently pick up a
# changed config/default ladder mid-session.
MODEL_ROUTING_CONTRACT_VERSION = 1

# Maps a runtime's ``runtime_backend`` property value to the config provider whose
# tier models it can execute. Keyed on the EXACT strings each runtime returns from
# ``runtime.runtime_backend`` (verified against the runtime classes:
# ``codex_cli``/``codex_mcp`` from CodexCliRuntime/LeaderDrivenWorkerRuntime,
# ``gemini_cli`` from GeminiCLIRuntime), NOT on the config ``backend`` literal — the
# redispatch guard in :func:`resolve_execute_model` compares against the live
# adapter's property, so the two must speak the same vocabulary. ``opencode`` is
# intentionally absent: its models are addressed by a composite ``provider/model``
# id (e.g. ``anthropic/claude-...``) that this flat provider->model map cannot
# express, so opencode routing stays dormant (future work). ``gemini_cli`` maps to
# ``google`` for observability only — the Gemini CLI has no per-call model-override
# knob, so its decisions land *advised*, never enforced.
_BACKEND_PROVIDER: Mapping[str, str] = {
    "claude": "anthropic",
    "claude_code": "anthropic",
    "claude_mcp": "anthropic",
    "codex_cli": "openai",
    "codex_mcp": "openai",
    "gemini_cli": "google",
}

# ExecutionProfile SuggestedModelTier vocabulary -> internal tier names.
_PROFILE_HINT_TO_TIER: Mapping[str, str] = {
    "low": "frugal",
    "medium": "standard",
    "high": "frontier",
}

# MCP ``model_tier`` tool-arg vocabulary -> internal tier names.
_MODEL_TIER_ARG_TO_TIER: Mapping[str, str] = {
    "small": "frugal",
    "medium": "standard",
    "large": "frontier",
}


def lower_one_notch(tier: str, *, floor: str = DEFAULT_TIER_FLOOR) -> str:
    """Return ``tier`` dropped one rung cheaper, never below ``floor``.

    Unknown tiers (not on :data:`MODEL_TIER_LADDER`) are returned unchanged — the
    caller chose a vocabulary this module does not model, so it is not this
    function's place to silently rewrite it.
    """
    if tier not in MODEL_TIER_LADDER:
        return tier
    floor_index = MODEL_TIER_LADDER.index(floor) if floor in MODEL_TIER_LADDER else 0
    current_index = MODEL_TIER_LADDER.index(tier)
    return MODEL_TIER_LADDER[max(floor_index, current_index - 1)]


def raise_one_notch(tier: str, *, ceiling: str = DEFAULT_TIER_CEILING) -> str:
    """Return ``tier`` lifted one rung stronger, never above ``ceiling``.

    Unknown tiers (not on :data:`MODEL_TIER_LADDER`) are returned unchanged — the
    caller chose a vocabulary this module does not model, so it is not this
    function's place to silently rewrite it.
    """
    if tier not in MODEL_TIER_LADDER:
        return tier
    ceiling_index = (
        MODEL_TIER_LADDER.index(ceiling)
        if ceiling in MODEL_TIER_LADDER
        else len(MODEL_TIER_LADDER) - 1
    )
    current_index = MODEL_TIER_LADDER.index(tier)
    return MODEL_TIER_LADDER[min(ceiling_index, current_index + 1)]


def tier_from_profile_hint(hint: str | None) -> str | None:
    """Map an ExecutionProfile ``SuggestedModelTier`` value to an internal tier.

    ``low``/``medium``/``high`` -> ``frugal``/``standard``/``frontier``. Accepts
    the bare string value (not the profile object) so this module stays free of a
    ``profile_loader`` import. ``None`` and unrecognized hints return ``None``.
    """
    if hint is None:
        return None
    return _PROFILE_HINT_TO_TIER.get(hint)


def tier_from_model_tier_arg(arg: str | None) -> str | None:
    """Map the MCP ``model_tier`` tool argument to an internal tier.

    ``small``/``medium``/``large`` -> ``frugal``/``standard``/``frontier``.
    ``None`` and unrecognized values return ``None``.
    """
    if arg is None:
        return None
    return _MODEL_TIER_ARG_TO_TIER.get(arg)


@dataclass(frozen=True)
class ModelRouter:
    """The resolved per-run model-tier policy, derived once from config + backend.

    Attributes:
        tier_models: Tier name -> backend-executable model id for THIS run's
            backend. Only tiers with a model for the run's provider are present.
        runtime_backend: The backend the ``tier_models`` were resolved for. The
            executor's cross-harness redispatch path swaps in an adapter for a
            DIFFERENT backend mid-run; a model id is only executable on the
            backend it was resolved for, so :func:`resolve_execute_model` treats
            this router as absent when the adapter's backend does not match.
        child_tier: The tier decomposed children start at (RLM thesis:
            decomposition makes trusted children cheap enough for the frugal tier;
            child status alone is insufficient.
        base_tier: The tier top-level / non-decomposed ACs start at. Defaults to
            one notch above ``child_tier`` so the top keeps today's model.
        escalation_retry_threshold: The ``retry_attempt`` at which tier escalation
            begins (``retry_attempt`` is 0 on the initial dispatch). From this
            attempt onward the tier climbs one notch PER retry (capped at the
            frontier ceiling), so a persistently failing unit walks the whole
            ladder rather than stalling one notch up.
    """

    tier_models: Mapping[str, str]
    runtime_backend: str
    child_tier: str
    base_tier: str
    escalation_retry_threshold: int


@dataclass(frozen=True)
class ModelDecision:
    """The model tier for one unit plus how the chosen runtime will honor it.

    Attributes:
        tier: The tier the unit was routed to, or ``None`` when routing is
            dormant (no router).
        model: The backend-executable model id, or ``None`` when no model could
            be resolved for the decided tier.
        mode: ``"enforced"`` when the runtime applies the model through a native
            per-call override, ``"advised"`` when a model was decided but the
            runtime cannot enforce it, or ``"none"`` when there is no model.
    """

    tier: str | None
    model: str | None
    mode: str

    @property
    def is_enforced(self) -> bool:
        return self.mode == MODEL_MODE_ENFORCED and self.model is not None


def serialize_model_router(router: ModelRouter | None) -> dict[str, Any]:
    """Serialize the resolved per-run routing contract for durable resume.

    ``enabled=False`` is a real contract, distinct from a legacy checkpoint that
    has no contract at all. Persisting that distinction keeps a kill-switched run
    dormant when it is resumed in an environment where routing defaults to on.
    """
    payload: dict[str, Any] = {
        "version": MODEL_ROUTING_CONTRACT_VERSION,
        "enabled": router is not None,
    }
    if router is None:
        return payload
    payload["router"] = {
        "tier_models": dict(sorted(router.tier_models.items())),
        "runtime_backend": router.runtime_backend,
        "child_tier": router.child_tier,
        "base_tier": router.base_tier,
        "escalation_retry_threshold": router.escalation_retry_threshold,
    }
    return payload


def deserialize_model_router(value: object) -> tuple[bool, ModelRouter | None]:
    """Deserialize a persisted routing contract.

    Returns ``(recognized, router)`` so callers can distinguish a valid dormant
    contract (``True, None``) from a missing/malformed legacy payload
    (``False, None``). The latter lets resume apply an explicit fail-closed policy
    without confusing it with the intentional kill switch.
    """
    if not isinstance(value, Mapping):
        return False, None
    version = value.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != MODEL_ROUTING_CONTRACT_VERSION
    ):
        return False, None
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        return False, None
    if not enabled:
        return True, None

    raw_router = value.get("router")
    if not isinstance(raw_router, Mapping):
        return False, None
    raw_tier_models = raw_router.get("tier_models")
    if not isinstance(raw_tier_models, Mapping) or not raw_tier_models:
        return False, None
    tier_models: dict[str, str] = {}
    for raw_tier, raw_model in raw_tier_models.items():
        if not isinstance(raw_tier, str) or not raw_tier.strip():
            return False, None
        if not isinstance(raw_model, str) or not raw_model.strip():
            return False, None
        normalized_tier = raw_tier.strip()
        if normalized_tier not in MODEL_TIER_LADDER:
            return False, None
        if normalized_tier in tier_models:
            # Whitespace-normalized aliases such as ``"frugal"`` and
            # ``" frugal "`` make the persisted policy order-dependent. A
            # versioned execution contract must have one unambiguous model per
            # tier, so fail closed instead of accepting last-write-wins.
            return False, None
        tier_models[normalized_tier] = raw_model.strip()

    runtime_backend = raw_router.get("runtime_backend")
    child_tier = raw_router.get("child_tier")
    base_tier = raw_router.get("base_tier")
    threshold = raw_router.get("escalation_retry_threshold")
    if (
        not isinstance(runtime_backend, str)
        or not runtime_backend.strip()
        or runtime_backend.strip() not in _BACKEND_PROVIDER
    ):
        return False, None
    if not isinstance(child_tier, str) or child_tier.strip() not in MODEL_TIER_LADDER:
        return False, None
    if not isinstance(base_tier, str) or base_tier.strip() not in MODEL_TIER_LADDER:
        return False, None
    # bool is an int subclass; accepting it here would make a corrupted payload
    # silently change retry escalation semantics.
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        return False, None

    return True, ModelRouter(
        tier_models=tier_models,
        runtime_backend=runtime_backend.strip(),
        child_tier=child_tier.strip(),
        base_tier=base_tier.strip(),
        escalation_retry_threshold=threshold,
    )


def build_model_router(
    economics: EconomicsConfig,
    *,
    runtime_backend: str | None,
    pinned_model: str | None = None,
    base_tier_override: str | None = None,
) -> ModelRouter | None:
    """Derive the per-run :class:`ModelRouter`, or ``None`` to stay dormant.

    Args:
        economics: The run's economic config (tiers + escalation threshold).
        runtime_backend: The backend that will execute this run, as reported by
            ``runtime.runtime_backend``. Mapped to a config provider through
            :data:`_BACKEND_PROVIDER`; a backend not in that map (e.g. opencode)
            or ``None`` keeps routing dormant.
        pinned_model: The user's explicit model pin
            (``OUROBOROS_EXECUTION_MODEL``). When set it always wins, so routing
            returns ``None`` and never overrides an explicit choice.
        base_tier_override: Force the top-level tier instead of deriving it from
            ``child_tier``.

    Returns:
        A :class:`ModelRouter`, or ``None`` when routing must stay dormant: an
        explicit pin is set, the backend has no verified tier ladder, or no tier
        resolved to a runnable model.

    The persisted-config trap: ``economics.tiers`` is read from the user's
    ``~/.ouroboros/config.yaml``, which a prior release wrote with the OLD shipped
    tier defaults verbatim (the defaults ship as concrete ids, not a ``"default"``
    sentinel). After a pin bump, an untouched shipped default in that persisted
    config is indistinguishable from a deliberate override, so routing would
    enforce a retired id (e.g. ``--model gpt-4o``) the current provider map cannot
    run — failing every AC. Each tier model is therefore normalized through
    :func:`~ouroboros.config._model_defaults.normalize_tier_model`, the same
    precedent role models use (Q00/ouroboros#1324): a legacy shipped id resolves
    to its current replacement. Because the persisted schema has no provenance,
    an explicit same-provider override equal to that legacy id is normalized as
    well; cross-provider aliases and explicit never-shipped ids are preserved.
    """
    # An explicit user pin always wins — routing must not override it.
    if pinned_model:
        return None

    # Resolve the backend to a config provider. An unmapped backend (opencode's
    # composite ids, or any runtime with no tier ladder we can execute) keeps
    # routing dormant — see :data:`_BACKEND_PROVIDER`.
    if runtime_backend is None:
        return None
    provider = _BACKEND_PROVIDER.get(runtime_backend)
    if provider is None:
        return None

    tier_models: dict[str, str] = {}
    for tier in MODEL_TIER_LADDER:
        tier_config = economics.tiers.get(tier)
        if tier_config is None:
            continue
        # First model whose provider matches this backend wins for the tier.
        # Normalize a legacy shipped id (from an older persisted config) to its
        # current replacement; cross-provider aliases and never-shipped explicit
        # ids pass through untouched (same-provider historical ids are ambiguous).
        for model_config in tier_config.models:
            if model_config.provider == provider:
                tier_models[tier] = normalize_tier_model(
                    model_config.model,
                    provider=model_config.provider,
                )
                break
    if not tier_models:
        return None

    # Activates the previously-unconsumed ``default_tier`` field: the shipped
    # default "frugal" makes decomposed children run haiku.
    child_tier = economics.default_tier
    # Top-level ACs sit one notch above the child tier, so with the shipped
    # default they keep today's sonnet — zero behavior regression at the top.
    base_tier = base_tier_override or raise_one_notch(child_tier)

    return ModelRouter(
        tier_models=tier_models,
        runtime_backend=runtime_backend,
        child_tier=child_tier,
        base_tier=base_tier,
        escalation_retry_threshold=economics.escalation_threshold,
    )


def _resolve_model_for_tier(
    tier: str,
    tier_models: Mapping[str, str],
) -> tuple[str, str] | None:
    """Find the resolved ``(tier, model)`` for a requested tier.

    A decided tier may have no model in this backend's map (a sparse config, or a
    child dropped to a tier the backend does not populate). We first walk UP the
    ladder to the nearest defined tier — never silently substituting a model
    *cheaper* than decided — and only as a last resort walk DOWN. Returns ``None``
    when the map is empty or the tier is off the ladder with no exact entry.

    Returning the fallback tier together with its model is correctness-critical:
    telemetry and the frugality proof must describe the tier that actually ran,
    not the unavailable tier the policy originally requested.
    """
    exact = tier_models.get(tier)
    if exact is not None:
        return tier, exact
    if tier not in MODEL_TIER_LADDER:
        return None
    current_index = MODEL_TIER_LADDER.index(tier)
    # Walk UP first (stronger, never cheaper than decided)...
    for candidate in MODEL_TIER_LADDER[current_index + 1 :]:
        model = tier_models.get(candidate)
        if model is not None:
            return candidate, model
    # ...then DOWN as a last resort (nothing stronger exists).
    for candidate in reversed(MODEL_TIER_LADDER[:current_index]):
        model = tier_models.get(candidate)
        if model is not None:
            return candidate, model
    return None


def decide_model(
    model_override_support: ParamSupport,
    *,
    router: ModelRouter | None,
    is_decomposed_child: bool,
    decomposition_trustworthy: bool = False,
    retry_attempt: int = 0,
    suggested_tier: str | None = None,
) -> ModelDecision:
    """Decide the per-unit model tier, its model id, and whether it is enforced.

    Args:
        model_override_support: The chosen runtime's declared support for a
            per-call model override, read from
            ``runtime.capabilities.model_override_support``.
        router: The per-run policy from :func:`build_model_router`, or ``None`` to
            leave model routing dormant.
        is_decomposed_child: Whether this unit is a runtime-decomposed child. This
            flag is not itself a MECE attestation and is insufficient to lower
            the model tier.
        decomposition_trustworthy: Whether the decomposition has an explicit
            finalized trust signal. Only exactly ``True`` permits the RLM
            frugality move: unlike effort routing V5 (which does NOT lower a
            child's reasoning depth), a trusted child drops ONE tier cheaper and
            keeps its reasoning depth while the outer success/verifier gate still
            owns final acceptance. The default is fail-closed for older callers.
        retry_attempt: Same-runtime retry index for this unit (0 on the initial
            dispatch). From ``router.escalation_retry_threshold`` onward the tier
            climbs PROGRESSIVELY — one notch per retry at or past the threshold
            (``max(0, retry_attempt - threshold + 1)`` notches), capped at the
            frontier ceiling — applied AFTER the child drop so a failing child's
            escalation beats the drop: a hard child earns a progressively stronger
            model the longer it keeps failing. This is a deliberate asymmetry with
            effort routing V5, which keeps its single-notch retry raise: reasoning
            depth has no multi-level budget pressure to climb through, but the model
            tier ladder does, so only tiers escalate step by step across retries.
        suggested_tier: An explicit starting tier (e.g. from an ExecutionProfile
            hint) used in place of ``router.base_tier``.

    Returns:
        A :class:`ModelDecision`. ``mode`` is ``"enforced"`` only when the runtime
        declared ``NATIVE`` model-override support and a model resolved, so an
        advised choice can never be mistaken for an enforced one — the property the
        proof's enforced rows rely on.
    """
    if router is None:
        return ModelDecision(tier=None, model=None, mode=MODEL_MODE_NONE)

    tier = suggested_tier or router.base_tier
    if is_decomposed_child and decomposition_trustworthy is True:
        # THE RLM frugality move: only a trusted decomposed child runs one tier cheaper.
        tier = lower_one_notch(tier)
    # PROGRESSIVE retry escalation: raise one tier per retry at or past the
    # threshold, capped at the frontier ceiling. Applied AFTER the child drop so
    # a failing child's escalation beats the drop — a hard child earns a
    # progressively stronger model the longer it keeps failing.
    escalation_notches = max(0, retry_attempt - router.escalation_retry_threshold + 1)
    for _ in range(escalation_notches):
        tier = raise_one_notch(tier)

    resolved = _resolve_model_for_tier(tier, router.tier_models)
    if resolved is None:
        return ModelDecision(tier=tier, model=None, mode=MODEL_MODE_NONE)
    resolved_tier, model = resolved

    mode = (
        MODEL_MODE_ENFORCED if model_override_support is ParamSupport.NATIVE else MODEL_MODE_ADVISED
    )
    return ModelDecision(tier=resolved_tier, model=model, mode=mode)


def resolve_execute_model(
    adapter: object,
    *,
    router: ModelRouter | None,
    is_decomposed_child: bool,
    decomposition_trustworthy: bool = False,
    retry_attempt: int = 0,
    suggested_tier: str | None = None,
) -> tuple[ModelDecision, dict[str, str]]:
    """Decide the model for one ``execute_task`` call and build its kwargs.

    The single place every live execute_task call site lays itself on the model
    capability contract. ``is_decomposed_child`` alone is insufficient to lower a
    tier; only ``decomposition_trustworthy=True`` admits the trusted-child
    discount. Reads ``adapter.capabilities.model_override_support`` (defaulting
    to IGNORED when an adapter declares no capabilities — or none that carry the
    field yet), decides the model, and returns the ``execute_task`` kwargs —
    which are **empty unless the runtime enforces the model**, so a runtime that
    cannot honor a per-call model override is never handed one.

    If the adapter's backend differs from the one the router was built for, the
    router is treated as absent for this call (none-mode decision, empty kwargs):
    the executor's cross-harness redispatch path swaps in an adapter for a
    DIFFERENT backend mid-run, and a model id resolved for one backend is not
    executable on another.

    Returns:
        ``(decision, execute_kwargs)``. ``execute_kwargs`` is ``{"model": <id>}``
        only when the chosen runtime declared NATIVE support, else ``{}``.
    """
    # Cross-harness redispatch swaps adapters mid-run; a model id only runs on the
    # backend it was resolved for, so a router built for another backend is inert.
    if router is not None:
        adapter_backend = getattr(adapter, "runtime_backend", None)
        if adapter_backend != router.runtime_backend:
            router = None

    capabilities = getattr(adapter, "capabilities", None)
    # The capability field is added by a parallel change; read it defensively so
    # this helper works before/after that lands and on adapters that omit it.
    support = getattr(capabilities, "model_override_support", ParamSupport.IGNORED)
    decision = decide_model(
        support,
        router=router,
        is_decomposed_child=is_decomposed_child,
        decomposition_trustworthy=decomposition_trustworthy,
        retry_attempt=retry_attempt,
        suggested_tier=suggested_tier,
    )
    kwargs = {"model": decision.model} if decision.is_enforced else {}
    return decision, kwargs
