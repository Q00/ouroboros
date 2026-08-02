"""Small deterministic helpers shared by the evolution loop."""

from __future__ import annotations

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.seed import Seed


def default_runtime_controls() -> RuntimeControlsConfig:
    """Load runtime controls through the config/env compatibility layer."""
    from ouroboros.config.loader import get_runtime_controls_config

    return get_runtime_controls_config()


def generation_execution_id(lineage_id: str, generation_number: int) -> str:
    """Build the deterministic execution ID for an evolve generation."""
    return f"evolve:{lineage_id}:generation:{generation_number}"


def conductor_preservation_error(
    approved: Seed,
    successor: Seed,
    directive: ConductorDirective | None,
) -> str | None:
    """Return a fail-closed invariant error for an unauthorized direction change."""
    if directive is None:
        return None
    changed: list[str] = []
    if directive.preserve_goal and approved.goal != successor.goal:
        changed.append("goal")
    if (
        directive.preserve_acceptance_criteria
        and approved.acceptance_criteria != successor.acceptance_criteria
    ):
        changed.append("acceptance_criteria")
    if directive.preserve_constraints and approved.constraints != successor.constraints:
        changed.append("constraints")
    if directive.preserve_non_goals and approved.to_dict().get(
        "non_goals"
    ) != successor.to_dict().get("non_goals"):
        changed.append("non_goals")
    if not changed:
        return None
    return "Conductor successor changed preserved direction fields: " + ", ".join(changed)
