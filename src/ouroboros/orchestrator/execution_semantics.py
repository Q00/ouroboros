"""Exact durable execution-semantics schema and legacy admission rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import NamedTuple

from ouroboros.config import MAX_USAGE_LIMIT_PAUSE_SECONDS
from ouroboros.orchestrator.adaptive_concurrency import adaptive_concurrency_policy
from ouroboros.orchestrator.execution_authority import (
    valid_runtime_effect_capabilities_contract,
)

CURRENT_EXECUTION_SEMANTICS_VERSION = 4
PRE_ADAPTIVE_EXECUTION_SEMANTICS_VERSION = 3


class ExecutionSemanticsRejection(NamedTuple):
    """One fail-closed resume rejection for runner-owned error translation."""

    message: str
    details: dict[str, object]


_CURRENT_KEYS = frozenset(
    {
        "version",
        "run_verify_commands",
        "verify_command_timeout_seconds",
        "ac_retry_attempts",
        "cross_harness_redispatch",
        "enable_decomposition",
        "decomposition_mode",
        "max_decomposition_depth",
        "max_parallel_workers",
        "effective_parallel_workers",
        "adaptive_concurrency_policy",
        "fat_harness_mode",
        "shadow_replay_enabled",
        "checkpoint_store_enabled",
        "session_signal_hub_enabled",
        "context_pack_enabled",
        "backend_limits_backend",
        "backend_max_concurrency",
        "backend_requests_per_minute",
        "backend_tokens_per_minute",
        "backend_self_governs_rate_limit",
        "usage_limit_pause_seconds",
        "runtime_effect_capabilities",
    }
)
_BOOLEAN_KEYS = (
    "run_verify_commands",
    "cross_harness_redispatch",
    "enable_decomposition",
    "fat_harness_mode",
    "shadow_replay_enabled",
    "checkpoint_store_enabled",
    "session_signal_hub_enabled",
    "context_pack_enabled",
    "backend_self_governs_rate_limit",
)


def _has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond the finite durable schema."""
    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


def valid_execution_semantics_contract(value: object) -> bool:
    """Validate the exact current scalar executor schema."""
    if not isinstance(value, Mapping) or not _has_exact_keys(value, _CURRENT_KEYS):
        return False
    if (
        type(value.get("version")) is not int
        or value.get("version") != CURRENT_EXECUTION_SEMANTICS_VERSION
        or any(type(value.get(key)) is not bool for key in _BOOLEAN_KEYS)
    ):
        return False
    timeout = value.get("verify_command_timeout_seconds")
    retries = value.get("ac_retry_attempts")
    max_depth = value.get("max_decomposition_depth")
    max_workers = value.get("max_parallel_workers")
    effective_workers = value.get("effective_parallel_workers")
    adaptive_policy = value.get("adaptive_concurrency_policy")
    mode = value.get("decomposition_mode")
    backend = value.get("backend_limits_backend")
    backend_max_concurrency = value.get("backend_max_concurrency")
    backend_limits = (
        backend_max_concurrency,
        value.get("backend_requests_per_minute"),
        value.get("backend_tokens_per_minute"),
    )
    usage_limit_pause_seconds = value.get("usage_limit_pause_seconds")
    expected_effective_workers = (
        min(max_workers, backend_max_concurrency)
        if type(max_workers) is int and type(backend_max_concurrency) is int
        else max_workers
    )
    return bool(
        type(timeout) is int
        and timeout >= 1
        and type(retries) is int
        and retries >= 0
        and type(max_depth) is int
        and max_depth >= 0
        and type(max_workers) is int
        and max_workers >= 1
        and type(effective_workers) is int
        and 1 <= effective_workers <= max_workers
        and effective_workers == expected_effective_workers
        and isinstance(adaptive_policy, Mapping)
        and dict(adaptive_policy)
        == adaptive_concurrency_policy(
            initial_limit=effective_workers,
            max_limit=max_workers,
        )
        and isinstance(mode, str)
        and mode in {"bounce_only", "off"}
        and (value.get("enable_decomposition") is True or mode == "off")
        and isinstance(backend, str)
        and bool(backend)
        and all(item is None or (type(item) is int and item >= 1) for item in backend_limits)
        and type(usage_limit_pause_seconds) is int
        and 1 <= usage_limit_pause_seconds <= MAX_USAGE_LIMIT_PAUSE_SECONDS
        and valid_runtime_effect_capabilities_contract(value.get("runtime_effect_capabilities"))
    )


def valid_legacy_preflight_execution_semantics_contract(value: object) -> bool:
    """Recognize only the retired current-schema preflight snapshot."""
    if (
        not isinstance(value, Mapping)
        or value.get("decomposition_mode") != "preflight"
        or value.get("enable_decomposition") is not True
    ):
        return False
    migrated = dict(value)
    migrated["decomposition_mode"] = "bounce_only"
    return valid_execution_semantics_contract(migrated)


def pre_adaptive_execution_semantics_rejection(
    value: object,
    persisted_fingerprint: object,
    *,
    fingerprint: Callable[[Mapping[str, object]], str],
) -> ExecutionSemanticsRejection | None:
    """Describe rejection of the exact v3 shape whose static limit changed meaning."""
    if (
        not isinstance(value, Mapping)
        or value.get("version") != PRE_ADAPTIVE_EXECUTION_SEMANTICS_VERSION
        or "adaptive_concurrency_policy" in value
    ):
        return None
    initial_limit = value.get("effective_parallel_workers")
    max_limit = value.get("max_parallel_workers")
    if type(initial_limit) is not int or type(max_limit) is not int:
        return None
    candidate = dict(value)
    candidate["version"] = CURRENT_EXECUTION_SEMANTICS_VERSION
    candidate["adaptive_concurrency_policy"] = adaptive_concurrency_policy(
        initial_limit=initial_limit,
        max_limit=max_limit,
    )
    if not valid_execution_semantics_contract(candidate):
        return None
    if not isinstance(persisted_fingerprint, str) or persisted_fingerprint != fingerprint(
        dict(value)
    ):
        return ExecutionSemanticsRejection(
            "Cannot resume with an invalid pre-adaptive execution contract",
            {"invalid": "execution_semantics_fingerprint"},
        )
    return ExecutionSemanticsRejection(
        "Cannot resume a pre-adaptive execution-semantics contract",
        {
            "execution_semantics_version": PRE_ADAPTIVE_EXECUTION_SEMANTICS_VERSION,
            "resume_blocked": "adaptive_concurrency_policy_unavailable",
            "retryable": False,
            "hint": "Start a new session under the durable adaptive policy.",
        },
    )


__all__ = [
    "CURRENT_EXECUTION_SEMANTICS_VERSION",
    "ExecutionSemanticsRejection",
    "pre_adaptive_execution_semantics_rejection",
    "valid_execution_semantics_contract",
    "valid_legacy_preflight_execution_semantics_contract",
]
