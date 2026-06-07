"""Backend-aware fan-out concurrency planning.

Ouroboros plans delivery (the parallel execution of acceptance criteria) and is
responsible for keeping that fan-out within the concurrency and rate-limit
constraints of the connected LLM backend — it must not rely on the agent
runtime to throttle itself. See ``docs/rca-june-2026.md`` (R3): a hermes→Z.AI
run fanned out 14 acceptance criteria at once and stampeded an already-exhausted
quota because nothing on the Ouroboros side bounded concurrency for that
runtime (only the native Claude adapter carries a shared rate-limit bucket).

Policy: backends whose underlying LLM limits Ouroboros cannot know — every CLI
runtime (hermes, codex, gemini, opencode, ...) — are **serialized by default**
(one acceptance criterion at a time) and raised only by explicit operator
override. The native Claude backend is left uncapped for fan-out here because it
is already governed by its RPM/TPM bucket (``SharedRateLimitBucket``).

Every limit — fan-out concurrency, requests-per-minute, tokens-per-minute — is
configurable **without source-level changes** through three layers, highest
precedence first:

1. Environment variables — ``OUROBOROS_MAX_CONCURRENCY`` (cap, any backend) and
   per-backend ``OUROBOROS_<BACKEND>_RPM`` / ``OUROBOROS_<BACKEND>_TPM`` (the
   backend name upper-cased with non-alphanumerics collapsed to ``_``, e.g.
   ``hermes_cli`` → ``OUROBOROS_HERMES_CLI_RPM``).
2. A YAML config file at ``~/.ouroboros/backend_limits.yaml`` (path overridable
   via ``OUROBOROS_BACKEND_LIMITS``) — mirrors the ``tool_capabilities.yaml``
   pattern: lazy, mtime-cached, and fault-tolerant.
3. The built-in registry below, then a conservative serialize-by-default cap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import stat
from typing import Any

import yaml

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.rate_limit import (
    DEFAULT_ANTHROPIC_RPM_CEILING,
    DEFAULT_ANTHROPIC_TPM_CEILING,
)

log = get_logger(__name__)

#: Default fan-out cap for backends with no Ouroboros-known LLM limits.
DEFAULT_UNKNOWN_MAX_CONCURRENCY = 1

#: Operator override for the resolved concurrency cap (applies to any backend).
MAX_CONCURRENCY_ENV = "OUROBOROS_MAX_CONCURRENCY"

#: Path override for the backend-limits config file.
BACKEND_LIMITS_PATH_ENV = "OUROBOROS_BACKEND_LIMITS"

#: Reserved config key carrying fallback limits for any backend.
_CONFIG_DEFAULT_KEY = "default"

_RPM_ENV_SUFFIX = "RPM"
_TPM_ENV_SUFFIX = "TPM"


@dataclass(frozen=True, slots=True)
class BackendConcurrencyLimits:
    """Concurrency/rate constraints Ouroboros applies when planning fan-out.

    Attributes:
        backend: Canonical backend identifier the limits were resolved for.
        max_concurrency: Maximum acceptance criteria to dispatch in parallel.
            ``None`` means Ouroboros imposes no fan-out cap (the backend is
            governed elsewhere, e.g. the native Claude RPM/TPM bucket).
        requests_per_minute: Known request ceiling, if any. Consumed by the
            shared rate-limit bucket; ``None`` leaves request pacing dormant.
        tokens_per_minute: Known token ceiling, if any; ``None`` leaves token
            pacing dormant.
    """

    backend: str
    max_concurrency: int | None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


# Canonical aliases for backend identifiers seen on adapters / config.
#
# Operators select a runtime by its user-facing name (``orchestrator.runtime_backend``
# / ``OUROBOROS_AGENT_RUNTIME``: ``hermes``, ``codex``, ``gemini``, ``copilot``, ...),
# but several adapters report a ``*_cli`` *handle* name from ``runtime_backend``
# (``HermesCliRuntime`` → ``"hermes_cli"``, ``CodexCliRuntime`` → ``"codex_cli"``,
# ``GeminiCLIRuntime`` → ``"gemini_cli"``, ``CopilotCliRuntime`` → ``"copilot_cli"``).
# Canonicalizing the handle names back to the user-facing IDs keeps one identity
# across operator config, env keys, and the value the executor actually passes in,
# so ``OUROBOROS_HERMES_RPM`` / ``backends: hermes:`` apply to the real Hermes
# runtime instead of silently leaving the dispatch gate dormant.
_BACKEND_ALIASES = {
    "anthropic": "claude",
    "claude_code": "claude",
    "hermes_cli": "hermes",
    "codex_cli": "codex",
    "gemini_cli": "gemini",
    "copilot_cli": "copilot",
    "opencode_cli": "opencode",
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
    """Lower-case, trim, and canonicalize a backend identifier.

    Non-string input (the protocol guarantees ``str``, but test doubles may
    pass a mock) collapses to ``""`` so downstream string handling — env-prefix
    construction, registry lookup — never sees a non-string identifier.
    """
    if not isinstance(backend, str):
        return ""
    name = backend.strip().lower()
    return _BACKEND_ALIASES.get(name, name)


def _coerce_positive_int(value: Any) -> int | None:
    """Return ``value`` as a positive int, or ``None`` when not usable.

    Blank, non-integer, zero, and negative values all yield ``None`` so a
    malformed config entry never silently disables a safety limit.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# -- Config file loading (mirrors capabilities.py override loader) ----------

#: Mapping from resolved config path to (mtime, normalized per-backend limits).
#: Invalidated by mtime so edits take effect without a process restart.
_RAW_LIMITS_CACHE: dict[Path, tuple[float, dict[str, Mapping[str, Any]]]] = {}


def _default_backend_limits_path() -> Path:
    configured = os.environ.get(BACKEND_LIMITS_PATH_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ouroboros" / "backend_limits.yaml"


def _read_raw_backend_limits(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read and parse the limits YAML into normalized per-backend mappings.

    Every failure mode — missing file, non-regular file (FIFO, socket, device,
    directory), unreadable file, malformed YAML, unexpected shape — is handled
    locally and yields an empty mapping. A broken config must never propagate
    onto the orchestrator hot path, and ``read_text()`` on a FIFO/device would
    block forever, so non-regular files are refused before opening.
    """
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("backend_limits.stat_failed", path=str(path), error=str(exc))
        return {}

    if not stat.S_ISREG(stat_result.st_mode):
        log.warning(
            "backend_limits.not_regular_file",
            path=str(path),
            mode=oct(stat_result.st_mode),
        )
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("backend_limits.read_failed", path=str(path), error=str(exc))
        return {}

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning("backend_limits.yaml_parse_failed", path=str(path), error=str(exc))
        return {}

    if not isinstance(raw, Mapping):
        return {}

    # Accept either a top-level ``backends:`` block or a flat backend→fields map.
    raw_backends = raw.get("backends", raw)
    if not isinstance(raw_backends, Mapping):
        return {}

    parsed: dict[str, Mapping[str, Any]] = {}
    for key, value in raw_backends.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        # ``default`` is reserved as the cross-backend fallback; everything else
        # is canonicalized so aliases (anthropic/claude_code) match.
        normalized_key = key if key == _CONFIG_DEFAULT_KEY else _normalize_backend(key)
        parsed[normalized_key] = dict(value)

    # A top-level ``default:`` block (sibling of ``backends:``) also applies.
    top_default = raw.get(_CONFIG_DEFAULT_KEY)
    if isinstance(top_default, Mapping) and _CONFIG_DEFAULT_KEY not in parsed:
        parsed[_CONFIG_DEFAULT_KEY] = dict(top_default)

    return parsed


def _load_raw_backend_limits(path: str | Path | None = None) -> dict[str, Mapping[str, Any]]:
    """Load per-backend limit mappings with mtime-based caching.

    Fault-tolerant by design: any failure returns an empty mapping so limit
    resolution always succeeds. The config file is an optional enhancement.
    """
    try:
        config_path = (
            Path(path).expanduser() if path is not None else _default_backend_limits_path()
        )
    except (OSError, ValueError) as exc:
        log.warning("backend_limits.path_resolution_failed", path=str(path), error=str(exc))
        return {}

    try:
        stat_result = config_path.stat()
    except FileNotFoundError:
        _RAW_LIMITS_CACHE.pop(config_path, None)
        return {}
    except OSError as exc:
        log.warning("backend_limits.read_failed", path=str(config_path), error=str(exc))
        return {}

    if not stat.S_ISREG(stat_result.st_mode):
        _RAW_LIMITS_CACHE.pop(config_path, None)
        log.warning(
            "backend_limits.not_regular_file",
            path=str(config_path),
            mode=oct(stat_result.st_mode),
        )
        return {}

    mtime = stat_result.st_mtime
    cached = _RAW_LIMITS_CACHE.get(config_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    raw = _read_raw_backend_limits(config_path)
    _RAW_LIMITS_CACHE[config_path] = (mtime, raw)
    return raw


def _apply_config_limits(
    base: BackendConcurrencyLimits,
    canonical: str,
) -> BackendConcurrencyLimits:
    """Layer config-file limits onto ``base`` (default block, then backend)."""
    raw = _load_raw_backend_limits()
    if not raw:
        return base

    for key in (_CONFIG_DEFAULT_KEY, canonical):
        fields = raw.get(key)
        if not isinstance(fields, Mapping):
            continue
        max_concurrency = _coerce_positive_int(fields.get("max_concurrency"))
        rpm = _coerce_positive_int(fields.get("requests_per_minute"))
        tpm = _coerce_positive_int(fields.get("tokens_per_minute"))
        base = replace(
            base,
            max_concurrency=max_concurrency
            if max_concurrency is not None
            else base.max_concurrency,
            requests_per_minute=rpm if rpm is not None else base.requests_per_minute,
            tokens_per_minute=tpm if tpm is not None else base.tokens_per_minute,
        )
    return base


# -- Environment overrides --------------------------------------------------


def _read_max_concurrency_override() -> int | None:
    """Return a positive ``OUROBOROS_MAX_CONCURRENCY`` override, else ``None``.

    Blank, non-integer, and non-positive values are ignored so a malformed
    override never silently disables the safety cap.
    """
    return _coerce_positive_int(os.environ.get(MAX_CONCURRENCY_ENV, "").strip() or None)


def _backend_env_prefix(canonical: str) -> str | None:
    """Build the ``OUROBOROS_<BACKEND>`` env prefix for a canonical backend."""
    token = re.sub(r"[^A-Z0-9]+", "_", canonical.upper()).strip("_")
    return f"OUROBOROS_{token}" if token else None


def _read_backend_rate_override(canonical: str, suffix: str) -> int | None:
    """Return a positive ``OUROBOROS_<BACKEND>_<SUFFIX>`` override, else ``None``."""
    prefix = _backend_env_prefix(canonical)
    if prefix is None:
        return None
    raw = os.environ.get(f"{prefix}_{suffix}", "").strip()
    return _coerce_positive_int(raw or None)


def _apply_env_overrides(
    base: BackendConcurrencyLimits,
    canonical: str,
) -> BackendConcurrencyLimits:
    """Layer environment overrides onto ``base`` (highest precedence)."""
    rpm = _read_backend_rate_override(canonical, _RPM_ENV_SUFFIX)
    tpm = _read_backend_rate_override(canonical, _TPM_ENV_SUFFIX)
    max_concurrency = _read_max_concurrency_override()
    return replace(
        base,
        max_concurrency=max_concurrency if max_concurrency is not None else base.max_concurrency,
        requests_per_minute=rpm if rpm is not None else base.requests_per_minute,
        tokens_per_minute=tpm if tpm is not None else base.tokens_per_minute,
    )


def resolve_backend_limits(backend: str | None) -> BackendConcurrencyLimits:
    """Resolve concurrency/rate limits for ``backend``.

    Layers, highest precedence last: built-in registry (or conservative
    serialize-by-default for unknown backends) → config file → environment
    overrides. Every dimension is overridable at each layer without code edits.
    """
    canonical = _normalize_backend(backend)
    base = _KNOWN_BACKENDS.get(canonical)
    if base is None:
        base = BackendConcurrencyLimits(
            backend=canonical or "unknown",
            max_concurrency=DEFAULT_UNKNOWN_MAX_CONCURRENCY,
        )

    base = _apply_config_limits(base, canonical)
    base = _apply_env_overrides(base, canonical)
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
    "BACKEND_LIMITS_PATH_ENV",
    "DEFAULT_UNKNOWN_MAX_CONCURRENCY",
    "MAX_CONCURRENCY_ENV",
    "BackendConcurrencyLimits",
    "plan_fan_out_concurrency",
    "resolve_backend_limits",
]
