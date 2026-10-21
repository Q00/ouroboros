"""Record the concrete model a Claude run reports it used.

Claude accepts an alias (``opus``) or a pinned id as its requested model and
resolves it itself. Only the runtime's own report of the model is evidence of
what ran: the ``model`` field of the ``system``/``init`` event, or the single
``modelUsage`` key of a result envelope that has no init event.

The observation built here has exactly the shape the Codex runtime attaches
under ``model_observation`` (``mode``, ``status``, ``requested_model``,
``effective_model``, ``source``), so token attribution, provider usage capture
and the check-package constructor read Claude runs without a Claude branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.providers.claude_cli_output import (
    CLAUDE_INIT_MODEL_SOURCE,
    normalize_claude_reported_model,
)


def claude_model_observation(
    model: object,
    *,
    requested_model: str | None,
    source: str | None,
) -> dict[str, str | None] | None:
    """Build an observed ``model_observation``, or ``None`` without a valid report.

    ``requested_model`` is the value handed to Claude (``--model`` or the SDK
    ``model`` option); it decides ``mode`` and is kept for comparison, never
    substituted for the reported model.
    """
    effective_model = normalize_claude_reported_model(model)
    if effective_model is None or not source:
        return None
    requested = normalize_claude_reported_model(requested_model)
    return {
        "mode": "pinned" if requested is not None else "automatic",
        "status": "observed",
        "requested_model": requested,
        "effective_model": effective_model,
        "source": source,
    }


def claude_init_data(init_data: object, *, requested_model: str | None) -> dict[str, Any]:
    """``AgentMessage.data`` fields for a Claude SDK ``init`` system message.

    Always carries ``session_id``; adds ``model_observation`` only when the
    init payload reports a valid model.
    """
    if not isinstance(init_data, Mapping):
        return {"session_id": None}
    fields: dict[str, Any] = {"session_id": init_data.get("session_id")}
    observation = claude_model_observation(
        init_data.get("model"),
        requested_model=requested_model,
        source=CLAUDE_INIT_MODEL_SOURCE,
    )
    if observation is not None:
        fields["model_observation"] = observation
    return fields


__all__ = ["claude_init_data", "claude_model_observation"]
