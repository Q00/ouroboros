"""Shared response/meta helpers for OpenClaw workflow surfaces."""

from __future__ import annotations

from typing import Any


def extract_seed_yaml(text: str) -> str:
    """Extract inline seed YAML from a generate-seed tool response."""
    marker = "--- Seed YAML ---"
    if marker not in text:
        raise ValueError("generate_seed response did not include inline seed YAML")
    return text.split(marker, 1)[1].strip()


def build_channel_workflow_meta(**kwargs: Any) -> dict[str, Any]:
    """Build a stable metadata shape for channel workflow responses."""
    meta = {
        "action": None,
        "channel_key": None,
        "workflow_id": None,
        "stage": None,
        "entry_point": None,
        "reason": None,
        "repo": None,
        "session_id": None,
        "execution_id": None,
        "job_id": None,
        "seed_id": None,
        "pr_url": None,
        "job_status": None,
        "cursor": None,
        "changed": None,
        "ambiguity_score": None,
        "seed_ready": None,
        "next_workflow_started": False,
        "duplicate_delivery": False,
        "duplicate_of": None,
        "active": None,
    }
    meta.update(kwargs)
    return meta
