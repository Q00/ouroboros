"""Copilot-side mapping for the orchestrator-level ``runtime_profile``.

The ``OrchestratorConfig.runtime_profile`` setting names a profile in the
orchestrator's own vocabulary (e.g. ``"worker"``). The Copilot backend
translates that name to its own ``--agent`` identifier and applies it at
command-build time.

Copilot CLI exposes ``--agent <name>`` to select a custom agent definition.
We map orchestrator profiles onto agent names so the same vocabulary that
configures the Codex ``--profile`` flag drives Copilot's agent selection.
"""

from __future__ import annotations

from typing import Any

# Maps the orchestrator-level ``runtime_profile`` value to the Copilot-side
# ``--agent`` name. Keep parity with ``codex/runtime_profile.py``: only the
# ``worker`` profile is shipped today; new entries land alongside the matching
# agent definition.
RUNTIME_PROFILE_TO_COPILOT_AGENT: dict[str, str] = {
    "worker": "ouroboros-worker",
}


def resolve_copilot_agent(
    runtime_profile: str | None,
    *,
    logger: Any,
    log_namespace: str,
) -> str | None:
    """Translate an orchestrator runtime_profile to a Copilot ``--agent`` name."""
    if not runtime_profile:
        return None
    mapped = RUNTIME_PROFILE_TO_COPILOT_AGENT.get(runtime_profile)
    if mapped is None:
        logger.warning(
            f"{log_namespace}.runtime_profile_unmapped",
            runtime_profile=runtime_profile,
            hint="No Copilot backend mapping; running without --agent.",
        )
    return mapped


__all__ = ["RUNTIME_PROFILE_TO_COPILOT_AGENT", "resolve_copilot_agent"]
