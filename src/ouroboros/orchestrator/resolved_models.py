"""Resolve the model-tier ladder a run executes on, and replay it on resume.

Model settings default to ``auto`` (see :mod:`ouroboros.config.model_selection`):
each tier runs on the latest model of that tier for the run's backend, the
Claude CLI aliases on alias backends and the ``"default"`` sentinel elsewhere.
The ``economics.tiers`` ladder keeps its shape (which tiers exist, their cost)
but its model ids apply only when models are pinned or the backend takes
explicit model ids (``litellm``, ``copilot``).

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

from ouroboros.config._model_defaults import recognized_shipped_defaults
from ouroboros.config.model_selection import (
    ROLE_TIERS,
    SHIPPED_TIER_MODELS,
    Tier,
    backend_model_selection,
    resolve_role_model,
    tier_model,
)
from ouroboros.config.models import ModelConfig
from ouroboros.orchestrator.model_routing import _BACKEND_PROVIDER, MODEL_TIER_LADDER
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
        others = [entry for entry in tier_config.models if entry.provider != provider]
        tiers[name] = tier_config.model_copy(
            update={"models": [ModelConfig(provider=provider, model=model), *others]}
        )
    return economics.model_copy(update={"tiers": tiers})


def resolve_route_economics(
    economics: EconomicsConfig, *, runtime_backend: object, pinned: bool
) -> EconomicsConfig:
    """Resolve each tier of ``economics`` to the model it runs on for ``runtime_backend``.

    Pinned runs and explicit backends keep the configured ladder verbatim;
    otherwise each tier becomes :func:`tier_model` for the backend.
    """
    provider = _provider(runtime_backend)
    if (
        provider is None
        or pinned
        or backend_model_selection(cast(str, runtime_backend)) == "explicit"
    ):
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
    must equal the ladder it resolves to automatically or when pinned.
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
    if dict(persisted_tiers) not in candidates:
        return None
    return _with_tier_models(economics, provider, persisted_tiers)


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
