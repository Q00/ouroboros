"""Resolve the model-tier ladder a run executes on, and replay it on resume.

Automatic role selection follows CLI-owned defaults. The execution-time
model router separately resolves the economics ladder: Claude uses current
tier aliases unless pinned, and other supported runtimes keep their native
configured models. Costs and sparse ladder shape remain configuration.
Codex's automatic-role path stays dormant unless routing is explicitly enabled.

The runner resolves the ladder once into its economics snapshot, so the model
router and every Route B projection built from that snapshot agree. A run
records the models it resolved at start as ``resolved_models``; resume replays
them instead of resolving again, so a run keeps the models it started on across
an upgrade or a ``models.pin`` change. Replay is bounded: a persisted model is
accepted only when the current configuration could have produced it (the
automatic ladder, the configured ladder, or the shipped pin of the Execute
tier), so a tampered contract still meets the existing resume refusals.
"""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import TYPE_CHECKING, Any, cast

from ouroboros.config._model_defaults import normalize_tier_model, recognized_shipped_defaults
from ouroboros.config.model_selection import (
    ROLE_TIERS,
    SHIPPED_TIER_MODELS,
    Tier,
    backend_model_selection,
    resolve_role_model,
    tier_model,
)
from ouroboros.config.models import ModelConfig
from ouroboros.orchestrator.model_routing import (
    _BACKEND_PROVIDER,
    MODEL_TIER_LADDER,
)
from ouroboros.orchestrator.route_compat import _configured_models

if TYPE_CHECKING:
    from ouroboros.config.models import EconomicsConfig

_RESOLVED_MODELS_KEYS = frozenset({"execute", "tiers"})


def _provider(runtime_backend: object) -> str | None:
    return _BACKEND_PROVIDER.get(runtime_backend) if isinstance(runtime_backend, str) else None


def _with_tier_models(
    economics: EconomicsConfig, provider: str, models: Mapping[str, str]
) -> EconomicsConfig:
    """Return ``economics`` with ``provider``'s model for each tier in ``models``.

    Only tiers that already carry an entry for ``provider`` change; the ladder's
    shape and costs stay configuration.
    """
    tiers = {}
    for name, tier_config in economics.tiers.items():
        model = models.get(name)
        if model is None or not any(entry.provider == provider for entry in tier_config.models):
            tiers[name] = tier_config
            continue
        entries = [
            entry.model_copy(update={"model": model}) if entry.provider == provider else entry
            for entry in tier_config.models
        ]
        tiers[name] = tier_config.model_copy(update={"models": entries})
    return economics.model_copy(update={"tiers": tiers})


def resolve_route_economics(
    economics: EconomicsConfig, *, runtime_backend: object, pinned: bool
) -> EconomicsConfig:
    """Resolve each tier of ``economics`` to the model it runs on for ``runtime_backend``.

    Pinned runs and explicit backends keep the configured ladder verbatim;
    otherwise each tier becomes :func:`tier_model` for the backend.
    """
    provider = _provider(runtime_backend)
    if provider is None or pinned or backend_model_selection(cast(str, runtime_backend)) != "alias":
        return economics
    backend = cast(str, runtime_backend)
    return _with_tier_models(
        economics,
        provider,
        {tier: tier_model(cast(Tier, tier), backend) for tier in MODEL_TIER_LADDER},
    )


def resolved_models_contract(
    economics: EconomicsConfig, *, runtime_backend: object, execute_model: object
) -> dict[str, Any]:
    """Return the models a run resolved at start, for its durable contract."""
    tiers: dict[str, str] = {}
    if _provider(runtime_backend) is not None:
        tiers = _configured_models(economics, runtime_backend=cast(str, runtime_backend)) or {}
    return {
        "execute": execute_model if isinstance(execute_model, str) else None,
        "tiers": dict(sorted(tiers.items())),
    }


def valid_resolved_models(value: object) -> bool:
    """Whether a persisted ``resolved_models`` is absent (pre-upgrade) or canonical."""
    if value is None:
        return True
    if not isinstance(value, Mapping) or frozenset(value) != _RESOLVED_MODELS_KEYS:
        return False
    execute, tiers = value["execute"], value["tiers"]
    return (
        (execute is None or isinstance(execute, str) and bool(execute.strip()))
        and isinstance(tiers, Mapping)
        and all(
            tier in MODEL_TIER_LADDER and isinstance(model, str) and bool(model)
            for tier, model in tiers.items()
        )
    )


def replay_route_economics(
    economics: EconomicsConfig,
    configured: EconomicsConfig,
    *,
    runtime_backend: object,
    persisted_tiers: Mapping[str, str],
) -> EconomicsConfig | None:
    """Return ``economics`` carrying ``persisted_tiers``, or ``None`` when not replayable.

    ``configured`` is the unresolved configuration snapshot. The persisted ladder
    must equal the ladder it resolves to automatically or when pinned, including
    pre-family-namespace MCP resolution and its authorized historical IDs.
    """
    provider = _provider(runtime_backend)
    if provider is None or not persisted_tiers:
        return None
    backend = cast(str, runtime_backend)
    candidates = [
        _configured_models(
            resolve_route_economics(configured, runtime_backend=backend, pinned=pinned),
            runtime_backend=backend,
        )
        for pinned in (False, True)
    ]
    # Before native tier routing, automatic sentinel backends persisted one
    # unresolved default per configured tier. Replay that frozen contract as
    # advisory instead of replacing it with newly shipped concrete models.
    if backend_model_selection(backend) == "sentinel":
        configured_models = _configured_models(configured, runtime_backend=backend) or {}
        candidates.append(dict.fromkeys(configured_models, "default"))

    # A pin bump changes new runs, not a frozen run. Accept an old shipped
    # id only in the same provider AND tier whose current authorized model
    # it normalizes to; never admit a cross-tier or arbitrary custom value.
    def historically_authorized(candidate: Mapping[str, str] | None) -> bool:
        if candidate is None or set(candidate) != set(persisted_tiers):
            return False
        for tier, model in persisted_tiers.items():
            normalized = normalize_tier_model(model, provider=provider)
            if backend == "gemini_cli" and provider == "google":
                normalized = {
                    "gemini-2.0-flash": "flash",
                    "gemini-2.5-pro": "pro",
                }.get(normalized, normalized)
            if normalized != candidate[tier]:
                return False
        return True

    legacy_match = False
    family = {"codex_mcp": "codex", "claude_mcp": "claude"}.get(backend)
    if family is not None and dict(persisted_tiers) not in candidates:
        # Before shared MCP family namespaces applied, only exact-runtime and
        # vendor entries participated. Reproduce that whole resolution, never
        # authorize individual lower-priority or mixed-family tier choices.
        def without_family(snapshot: EconomicsConfig) -> EconomicsConfig:
            return snapshot.model_copy(
                update={
                    "tiers": {
                        name: tier.model_copy(
                            update={"models": [m for m in tier.models if m.provider != family]}
                        )
                        for name, tier in snapshot.tiers.items()
                    }
                }
            )

        legacy_configured = without_family(configured)
        legacy_candidates = [
            _configured_models(
                resolve_route_economics(legacy_configured, runtime_backend=backend, pinned=pinned),
                runtime_backend=backend,
            )
            for pinned in (False, True)
        ]
        if backend_model_selection(backend) == "sentinel":
            legacy_models = _configured_models(legacy_configured, runtime_backend=backend) or {}
            legacy_candidates.append(dict.fromkeys(legacy_models, "default"))
        legacy_match = dict(persisted_tiers) in legacy_candidates or any(
            historically_authorized(candidate) for candidate in legacy_candidates
        )
        if legacy_match:
            # Family-only tiers were absent from an old sparse map. Keep them
            # absent while retaining the configured tier shape and costs.
            economics = without_family(economics)
    if dict(persisted_tiers) not in candidates and not legacy_match:
        if not any(historically_authorized(candidate) for candidate in candidates):
            return None
    # Only the validated frozen map enters this runtime-specific namespace.
    # Vendor legacy-default normalization must not rewrite a resumed model.
    tiers = dict(economics.tiers)
    for name, model in persisted_tiers.items():
        tier = tiers.get(name)
        if tier is not None:
            tiers[name] = tier.model_copy(
                update={
                    "models": [
                        ModelConfig(provider=backend, model=model),
                        *(entry for entry in tier.models if entry.provider != backend),
                    ]
                }
            )
    return economics.model_copy(update={"tiers": tiers})


def replay_constructor_model(
    adapter: object, *, runtime_backend: object, persisted: object
) -> bool:
    """Rebind an automatic constructor model to the model the run started on.

    Applies only when the adapter holds the automatic Execute model for its
    backend and ``persisted`` is a model the current configuration could have
    run: the pinned Execute resolution or a shipped pin of the Execute tier.
    """
    try:
        current = inspect.getattr_static(adapter, "_model")
    except AttributeError:
        return False
    if not isinstance(runtime_backend, str) or not isinstance(persisted, str):
        return False
    tier = ROLE_TIERS["execute"]
    if persisted == current or current != tier_model(tier, runtime_backend):
        return False
    allowed = {
        resolve_role_model("execute", backend=runtime_backend, pinned=True).model,
        *recognized_shipped_defaults(SHIPPED_TIER_MODELS[tier]),
    }
    if persisted not in allowed:
        return False
    setattr(adapter, "_model", persisted)  # noqa: B010 - the runtimes' constructor pin
    return True


__all__ = [
    "replay_constructor_model",
    "replay_route_economics",
    "resolve_route_economics",
    "resolved_models_contract",
    "valid_resolved_models",
]
