"""Backend-aware fan-out concurrency planning.

Ouroboros plans delivery (the parallel execution of acceptance criteria) and is
responsible for keeping that fan-out within the concurrency and rate-limit
constraints of the connected LLM backend — it must not rely on the agent
runtime to throttle itself. This policy was added after a hermes→Z.AI run
fanned out 14 acceptance criteria at once and stampeded an already-exhausted
quota because nothing on the Ouroboros side bounded concurrency for that
runtime (only the native Claude adapter carried a shared rate-limit bucket).

Policy: backends whose underlying LLM limits Ouroboros cannot know — every CLI
runtime (hermes, codex, gemini, opencode, ...) — are **serialized by default**
(one acceptance criterion at a time) and raised only by explicit operator
override via ``OUROBOROS_MAX_CONCURRENCY``. The native Claude backend is left
uncapped here because it is already governed by its RPM/TPM bucket
(``SharedRateLimitBucket``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os

from ouroboros.orchestrator.rate_limit import (
    DEFAULT_ANTHROPIC_RPM_CEILING,
    DEFAULT_ANTHROPIC_TPM_CEILING,
)

#: Default fan-out cap for backends with no Ouroboros-known LLM limits.
DEFAULT_UNKNOWN_MAX_CONCURRENCY = 1

#: Operator override for the resolved concurrency cap (applies to any backend).
MAX_CONCURRENCY_ENV = "OUROBOROS_MAX_CONCURRENCY"


@dataclass(frozen=True, slots=True)
class BackendConcurrencyLimits:
    """Concurrency/rate constraints Ouroboros applies when planning fan-out.

    Attributes:
        backend: Canonical backend identifier the limits were resolved for.
        max_concurrency: Maximum acceptance criteria to dispatch in parallel.
            ``None`` means Ouroboros imposes no fan-out cap (the backend is
            governed elsewhere, e.g. the native Claude RPM/TPM bucket).
        requests_per_minute: Known request ceiling, if any (advisory; consumed
            by the native Claude rate-limit bucket).
        tokens_per_minute: Known token ceiling, if any (advisory).
    """

    backend: str
    max_concurrency: int | None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


# Canonical aliases for backend identifiers seen on adapters / config.
_BACKEND_ALIASES = {
    "anthropic": "claude",
    "claude_code": "claude",
}

# Backends with Ouroboros-known governance. Only the native Claude adapter is
# uncapped here (its shared RPM/TPM bucket paces it); everything else falls
# through to the conservative default below.
_KNOWN_BACKENDS: dict[str, BackendConcurrencyLimits] = {
    "claude": BackendConcurrencyLimits(
        backend="claude",
        max_concurrency=None,
        requests_per_minute=DEFAULT_ANTHROPIC_RPM_CEILING,
        tokens_per_minute=DEFAULT_ANTHROPIC_TPM_CEILING,
    ),
}


def _normalize_backend(backend: str | None) -> str:
    """Lower-case, trim, and canonicalize a backend identifier."""
    name = (backend or "").strip().lower()
    return _BACKEND_ALIASES.get(name, name)


def _read_max_concurrency_override() -> int | None:
    """Return a positive ``OUROBOROS_MAX_CONCURRENCY`` override, else ``None``.

    Blank, non-integer, and non-positive values are ignored so a malformed
    override never silently disables the safety cap.
    """
    raw = os.environ.get(MAX_CONCURRENCY_ENV, "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def resolve_backend_limits(backend: str | None) -> BackendConcurrencyLimits:
    """Resolve concurrency/rate limits for ``backend``.

    Known backends use their registry entry; everything else (CLI runtimes,
    unknown, or missing) is serialized to :data:`DEFAULT_UNKNOWN_MAX_CONCURRENCY`.
    A positive ``OUROBOROS_MAX_CONCURRENCY`` env override replaces the resolved
    ``max_concurrency`` for any backend.
    """
    canonical = _normalize_backend(backend)
    base = _KNOWN_BACKENDS.get(canonical)
    if base is None:
        base = BackendConcurrencyLimits(
            backend=canonical or "unknown",
            max_concurrency=DEFAULT_UNKNOWN_MAX_CONCURRENCY,
        )

    override = _read_max_concurrency_override()
    if override is not None:
        base = replace(base, max_concurrency=override)

    return base


def plan_fan_out_concurrency(
    requested_workers: int,
    limits: BackendConcurrencyLimits,
) -> int:
    """Return the effective parallel-worker count for delivery fan-out.

    The result is the requested worker count, clamped to at least 1 and capped
    by ``limits.max_concurrency`` when the backend declares one.
    """
    requested = max(1, requested_workers)
    if limits.max_concurrency is None:
        return requested
    return max(1, min(requested, limits.max_concurrency))


__all__ = [
    "DEFAULT_UNKNOWN_MAX_CONCURRENCY",
    "MAX_CONCURRENCY_ENV",
    "BackendConcurrencyLimits",
    "plan_fan_out_concurrency",
    "resolve_backend_limits",
]
