"""Shared alternative-runtime picker for cross-harness recovery (PR-X X0).

No single-vendor harness can fail over to a *different* harness — that is only
possible at the meta-harness layer, where Ouroboros is the leader over many
provider runtimes. This module answers one deterministic question:

    "Given a runtime that just failed, which *other* installed, runtime-capable
     backend should we try instead?"

It is a pure selection helper — it never spawns anything. Callers (the
cross-harness redispatch hook in ``parallel_executor``, the N-version
tournament fan-out) use the returned backend name to build a fresh runtime via
``runtime_factory.create_agent_runtime``.

Selection consults three inputs, in strict precedence:

1. **Availability** — only backends whose CLI actually resolves on this machine
   are candidates (``backends.model_catalog.installed_backends``). We do not
   invent an availability notion; this is the same detection the settings UI and
   ``ooo status`` use.
2. **Capability gate** — a candidate must support agentic runtime execution
   (``BackendCapability.supports_runtime``). ``installed_backends`` already
   restricts to runtime-capable backends, so this is a defensive re-check that
   keeps the contract explicit and survives future callers passing wider sets.
3. **Outcome weights (optional, tie-break only)** — the X4 flywheel may supply a
   per-backend score. Weights only *reorder* the already-eligible set; they never
   admit an unavailable/incapable backend nor exclude an eligible one.

Determinism: given identical inputs the result is stable. Candidates are ordered
by descending weight, then ascending canonical name, so ties break
alphabetically rather than on dict iteration order.
"""

from __future__ import annotations

from collections.abc import Mapping

from ouroboros.backends import get_backend_capability, resolve_backend_alias
from ouroboros.backends.model_catalog import installed_backends

# A reference to a runtime backend is just its canonical backend name (e.g.
# "claude", "codex", "gemini"). The codebase threads runtime backends as these
# canonical strings everywhere, so an alias keeps the picker aligned with that
# convention while giving call sites a self-documenting type.
RuntimeBackendRef = str


def _canonical(ref: str | None) -> str | None:
    """Resolve a backend name/alias to its canonical name, or None if unknown."""
    if not ref or not ref.strip():
        return None
    try:
        return resolve_backend_alias(ref)
    except ValueError:
        return None


def _supports_seed_execution(name: str) -> bool:
    """Capability gate: the backend can run agentic seed-execution tasks."""
    capability = get_backend_capability(name)
    return capability is not None and capability.supports_runtime


def available_runtime_backends() -> tuple[RuntimeBackendRef, ...]:
    """Return canonical names of installed, runtime-capable backends.

    ``installed_backends`` maps every runtime-capable backend to its resolved
    CLI path (``None`` when not installed); we keep only the installed ones.
    """
    return tuple(sorted(name for name, cli_path in installed_backends().items() if cli_path))


def pick_alternative_runtime(
    failed: RuntimeBackendRef,
    *,
    exclude: set[str] | None = None,
    weights: Mapping[str, float] | None = None,
) -> RuntimeBackendRef | None:
    """Pick a healthy alternative runtime for a failed one, or ``None``.

    Args:
        failed: The backend that just failed. Always excluded from the result.
        exclude: Additional backends to exclude (already-tried alternatives,
            operator denylist). Names are canonicalized before comparison.
        weights: Optional per-backend outcome scores (X4). Used *only* as a
            tie-break to order otherwise-eligible candidates; never overrides
            availability or the capability gate. Higher is better.

    Returns:
        The canonical name of the best eligible alternative, or ``None`` when no
        installed, runtime-capable backend other than ``failed``/``exclude``
        exists. ``None`` means callers fall back to today's behavior — the
        picker never makes a failure worse.
    """
    excluded: set[str] = set()
    failed_canonical = _canonical(failed)
    if failed_canonical is not None:
        excluded.add(failed_canonical)
    for raw in exclude or set():
        canonical = _canonical(raw)
        if canonical is not None:
            excluded.add(canonical)

    candidates = [
        name
        for name in available_runtime_backends()
        if name not in excluded and _supports_seed_execution(name)
    ]
    if not candidates:
        return None

    def _weight(name: str) -> float:
        if not weights:
            return 0.0
        raw = weights.get(name)
        try:
            return float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Descending weight, then ascending name for a stable tie-break.
    candidates.sort(key=lambda name: (-_weight(name), name))
    return candidates[0]


__all__ = [
    "RuntimeBackendRef",
    "available_runtime_backends",
    "pick_alternative_runtime",
]
