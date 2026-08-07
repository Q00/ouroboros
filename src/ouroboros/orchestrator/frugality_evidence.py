"""Fail-closed primary-runtime evidence normalization for frugality receipts."""

from __future__ import annotations

from collections.abc import Mapping
import math

from ouroboros.orchestrator.adapter import AgentMessage

_TOKEN_SPEND_FALLBACK_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_TOKEN_USAGE_KEYS: tuple[str, ...] = (
    *_TOKEN_SPEND_FALLBACK_KEYS,
    "cached_input_tokens",
    "total_tokens",
)


def _nonnegative_token_counter(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def harvest_token_spend(
    messages: list[AgentMessage],
) -> tuple[float, dict[str, float]] | None:
    """Sum a stream only when every runtime counter is known and reconciled."""
    breakdown: dict[str, float] = {}
    token_spend = 0.0
    observed_spend = False
    for message in messages:
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            continue
        if data.get("usage_invalid") is True:
            return None
        if "usage" not in data:
            continue
        usage = data["usage"]
        if not isinstance(usage, Mapping):
            return None
        unknown_token_keys = {
            key
            for key in usage
            if isinstance(key, str) and key.endswith("_tokens") and key not in _TOKEN_USAGE_KEYS
        }
        if unknown_token_keys:
            return None
        usable_usage: dict[str, float] = {}
        for key in _TOKEN_USAGE_KEYS:
            if key not in usage:
                continue
            counter = _nonnegative_token_counter(usage[key])
            if counter is None:
                return None
            number = float(counter)
            usable_usage[key] = number
            breakdown[key] = breakdown.get(key, 0.0) + number

        total_tokens = usable_usage.get("total_tokens")
        if total_tokens is not None:
            fallback_keys_present = {
                key for key in _TOKEN_SPEND_FALLBACK_KEYS if key in usable_usage
            }
            if fallback_keys_present and not {"input_tokens", "output_tokens"}.issubset(
                fallback_keys_present
            ):
                return None
            spend_components = [
                usable_usage[key] for key in _TOKEN_SPEND_FALLBACK_KEYS if key in usable_usage
            ]
            if spend_components and total_tokens != sum(spend_components):
                return None
            token_spend += total_tokens
            observed_spend = True
            continue

        spend_components = [
            usable_usage[key] for key in _TOKEN_SPEND_FALLBACK_KEYS if key in usable_usage
        ]
        if {
            "input_tokens",
            "output_tokens",
        }.issubset(usable_usage):
            token_spend += sum(spend_components)
            observed_spend = True
        elif spend_components:
            return None

    if (
        not observed_spend
        or token_spend <= 0
        or not math.isfinite(token_spend)
        or any(not math.isfinite(value) for value in breakdown.values())
    ):
        return None
    return token_spend, breakdown


def observed_effective_model(messages: list[AgentMessage]) -> str | None:
    """Return one runtime-observed model or fail closed on an opaque stream."""
    observations_present = False
    models: list[str] = []
    for message in messages:
        data = getattr(message, "data", None)
        if not isinstance(data, Mapping) or "model_observation" not in data:
            continue
        observations_present = True
        observation = data["model_observation"]
        if not isinstance(observation, Mapping) or "effective_model" not in observation:
            return None
        raw_model = observation["effective_model"]
        if raw_model is None:
            continue
        if (
            not isinstance(raw_model, str)
            or not raw_model.strip()
            or raw_model.strip().lower() == "unknown"
        ):
            return None
        models.append(raw_model.strip())
    if not observations_present or not models or len(set(models)) != 1:
        return None
    return models[0]


__all__ = ["harvest_token_spend", "observed_effective_model"]
