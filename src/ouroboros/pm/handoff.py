"""Shared PM handoff messaging helpers."""

from __future__ import annotations

from pathlib import Path


def build_pm_dev_handoff_command(seed_path: Path | str) -> str:
    """Return the canonical command for continuing from a PM seed."""
    return f"ouroboros init start {seed_path}"


def build_pm_dev_handoff_next_step(seed_path: Path | str) -> str:
    """Return the canonical PM-to-dev handoff message."""
    return (
        f"Run '{build_pm_dev_handoff_command(seed_path)}' to continue into the dev interview. "
        "The runnable Seed is generated after that dev interview completes."
    )
