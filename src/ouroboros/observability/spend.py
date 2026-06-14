"""Spend attribution helpers.

This module is observational only: it normalizes and formats spend signals
without enforcing budgets or changing execution behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SPEND_STAGES: tuple[str, ...] = ("interview", "execute", "evaluate", "consensus")

_STAGE_ALIASES: dict[str, str] = {
    "interview": "interview",
    "questioning": "interview",
    "seed": "interview",
    "execute": "execute",
    "execution": "execute",
    "run": "execute",
    "discover": "execute",
    "define": "execute",
    "develop": "execute",
    "deliver": "execute",
    "evaluate": "evaluate",
    "evaluation": "evaluate",
    "semantic": "evaluate",
    "stage1": "evaluate",
    "stage2": "evaluate",
    "consensus": "consensus",
    "stage3": "consensus",
}


def normalize_spend_stage(value: object) -> str | None:
    """Return a canonical spend stage name for a loose phase/stage value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    return _STAGE_ALIASES.get(normalized)


def normalize_stage_breakdown(value: object) -> dict[str, dict[str, float | int]]:
    """Normalize a stage-spend mapping for UI and event payloads."""
    if not isinstance(value, Mapping):
        return {}

    normalized: dict[str, dict[str, float | int]] = {}
    for raw_stage, raw_metrics in value.items():
        stage = normalize_spend_stage(raw_stage)
        if stage is None or not isinstance(raw_metrics, Mapping):
            continue

        tokens = _coerce_non_negative_int(raw_metrics.get("tokens"))
        cost_usd = _coerce_non_negative_float(
            raw_metrics.get("cost_usd", raw_metrics.get("cost")),
        )
        if tokens == 0 and cost_usd == 0:
            continue

        normalized[stage] = {"tokens": tokens, "cost_usd": cost_usd}

    return normalized


def single_stage_breakdown(
    stage: object,
    *,
    tokens: object = 0,
    cost_usd: object = 0.0,
) -> dict[str, dict[str, float | int]]:
    """Build a one-stage attribution row when only aggregate spend is known."""
    normalized_stage = normalize_spend_stage(stage)
    if normalized_stage is None:
        return {}

    token_count = _coerce_non_negative_int(tokens)
    cost = _coerce_non_negative_float(cost_usd)
    if token_count == 0 and cost == 0:
        return {}

    return {normalized_stage: {"tokens": token_count, "cost_usd": cost}}


def merge_stage_breakdowns(
    *breakdowns: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, float | int]]:
    """Merge stage breakdowns by summing tokens and cost per canonical stage."""
    merged: dict[str, dict[str, float | int]] = {}
    for breakdown in breakdowns:
        for stage, metrics in normalize_stage_breakdown(breakdown).items():
            target = merged.setdefault(stage, {"tokens": 0, "cost_usd": 0.0})
            target["tokens"] = int(target["tokens"]) + int(metrics["tokens"])
            target["cost_usd"] = float(target["cost_usd"]) + float(metrics["cost_usd"])
    return merged


def format_stage_breakdown(value: object) -> str:
    """Format a compact per-stage cost breakdown for session/TUI surfaces."""
    breakdown = normalize_stage_breakdown(value)
    if not breakdown:
        return ""

    parts: list[str] = []
    for stage in SPEND_STAGES:
        metrics = breakdown.get(stage)
        if not metrics:
            continue
        tokens = int(metrics.get("tokens", 0))
        cost = float(metrics.get("cost_usd", 0.0))
        if tokens > 0 and cost > 0:
            parts.append(f"{stage} ${cost:.2f}/{_format_tokens(tokens)}")
        elif cost > 0:
            parts.append(f"{stage} ${cost:.2f}")
        elif tokens > 0:
            parts.append(f"{stage} {_format_tokens(tokens)}")

    return ", ".join(parts)


def _coerce_non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _coerce_non_negative_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, float(value))
    return 0.0


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


__all__ = [
    "SPEND_STAGES",
    "format_stage_breakdown",
    "merge_stage_breakdowns",
    "normalize_spend_stage",
    "normalize_stage_breakdown",
    "single_stage_breakdown",
]
