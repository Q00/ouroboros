"""Model-tier investment routing for the Agent-OS execution contract.

The sibling of :mod:`ouroboros.orchestrator.effort_routing`. Where effort routing
picks *how much reasoning* a unit of work gets, this module picks *which model
tier* runs it — the frugality lever ``ooo run`` leans on. The RLM thesis is that
decomposing a big goal into small, verified-MECE acceptance criteria makes each
child easy enough to run on a cheaper model, so decomposed children drop one tier
(``standard`` -> ``frugal`` -> haiku) while top-level ACs keep today's default
(``standard`` -> sonnet). A failing AC earns a stronger model on retry.

Note the deliberate asymmetry with effort routing V5: that module stopped lowering
a decomposed child's *reasoning depth* (a harder child needs at least as much
thinking as its parent). This module still lowers a child's *model tier* — a child
keeps its reasoning depth but runs on a cheaper model, because decomposition, not
weaker reasoning, is what makes it affordable.

Like effort routing, this is a single, pure decision point free of executor state:
the orchestrator decides a tier, maps it to a backend-executable model id, and the
runtime either ENFORCES the choice through a native per-call model override or is
merely *advised* of it. Keeping the policy stateless makes it testable in isolation
and keeps the live executor a thin caller on the capability contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
            decomposition makes children cheap enough for the frugal tier).
        base_tier: The tier top-level / non-decomposed ACs start at. Defaults to
            one notch above ``child_tier`` so the top keeps today's model.
        escalation_retry_threshold: The ``retry_attempt`` at which the tier is
            raised one notch (``retry_attempt`` is 0 on the initial dispatch),
            mirroring effort routing's retry-raise semantics.
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
        for model_config in tier_config.models:
            if model_config.provider == provider:
                tier_models[tier] = model_config.model
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


def _resolve_model_for_tier(tier: str, tier_models: Mapping[str, str]) -> str | None:
    """Find the model for ``tier``, preferring a stronger fallback over a cheaper one.

    A decided tier may have no model in this backend's map (a sparse config, or a
    child dropped to a tier the backend does not populate). We first walk UP the
    ladder to the nearest defined tier — never silently substituting a model
    *cheaper* than decided — and only as a last resort walk DOWN. Returns ``None``
    when the map is empty or the tier is off the ladder with no exact entry.
    """
    exact = tier_models.get(tier)
    if exact is not None:
        return exact
    if tier not in MODEL_TIER_LADDER:
        return None
    current_index = MODEL_TIER_LADDER.index(tier)
    # Walk UP first (stronger, never cheaper than decided)...
    for candidate in MODEL_TIER_LADDER[current_index + 1 :]:
        model = tier_models.get(candidate)
        if model is not None:
            return model
    # ...then DOWN as a last resort (nothing stronger exists).
    for candidate in reversed(MODEL_TIER_LADDER[:current_index]):
        model = tier_models.get(candidate)
        if model is not None:
            return model
    return None


def decide_model(
    model_override_support: ParamSupport,
    *,
    router: ModelRouter | None,
    is_decomposed_child: bool,
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
        is_decomposed_child: Whether this unit is a verified-MECE child. Unlike
            effort routing V5 (which does NOT lower a child's reasoning depth),
            this drops the child ONE tier cheaper — the RLM frugality move: a child
            keeps its reasoning depth but runs on a cheaper model because
            decomposition made it affordable.
        retry_attempt: Same-runtime retry index for this unit (0 on the initial
            dispatch). At ``router.escalation_retry_threshold`` onward the tier is
            raised one notch, applied AFTER the child drop so a failing child's
            escalation beats the drop: a hard child earns a stronger model.
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
    if is_decomposed_child:
        # THE RLM frugality move: a decomposed child runs one tier cheaper.
        tier = lower_one_notch(tier)
    if retry_attempt >= router.escalation_retry_threshold:
        # Applied after the child drop so escalation beats the drop.
        tier = raise_one_notch(tier)

    model = _resolve_model_for_tier(tier, router.tier_models)
    if model is None:
        return ModelDecision(tier=tier, model=None, mode=MODEL_MODE_NONE)

    mode = (
        MODEL_MODE_ENFORCED if model_override_support is ParamSupport.NATIVE else MODEL_MODE_ADVISED
    )
    return ModelDecision(tier=tier, model=model, mode=mode)


def resolve_execute_model(
    adapter: object,
    *,
    router: ModelRouter | None,
    is_decomposed_child: bool,
    retry_attempt: int = 0,
    suggested_tier: str | None = None,
) -> tuple[ModelDecision, dict[str, str]]:
    """Decide the model for one ``execute_task`` call and build its kwargs.

    The single place every live execute_task call site lays itself on the model
    capability contract. Reads ``adapter.capabilities.model_override_support``
    (defaulting to IGNORED when an adapter declares no capabilities — or none
    that carry the field yet), decides the model, and returns the ``execute_task``
    kwargs — which are **empty unless the runtime enforces the model**, so a
    runtime that cannot honor a per-call model override is never handed one.

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
        retry_attempt=retry_attempt,
        suggested_tier=suggested_tier,
    )
    kwargs = {"model": decision.model} if decision.is_enforced else {}
    return decision, kwargs
