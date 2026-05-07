"""Capture authoring-backend attribution for auto-pipeline blockers.

Issue #690 surfaced a class of incidents where a goal like
"open and merge a PR" hit ``interview.start timed out after 60s`` and
the user could not tell whether the timeout came from the in-process
authoring path or from the runtime adapter behind ``--runtime <X>``.

This module records the resolved authoring backend on
``AutoPipelineState`` as **structured metadata**, leaving the
user-facing blocker message text unchanged. Surfaces (CLI ``--status``,
MCP responses, log sinks) that want to render the attribution can read
``state.last_authoring_backend`` and format it appropriately for their
audience. This module deliberately does not append bracketed tokens to
the blocker message itself — that exposes internal execution topology
to users who only need to know *which phase failed and what to try
next*.

The phase/tool dimension is already captured by
``state.last_tool_name`` (set by ``mark_blocked``/``mark_failed``); this
module only adds the backend dimension.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState


def authoring_backend_label(state: AutoPipelineState) -> str:
    """Return the human-readable authoring path for an auto-mode state.

    In ``ooo auto`` flow, both auto entry points (``cli/commands/auto.py``
    and ``mcp/tools/auto_handler.py``) demote a persisted
    ``opencode_mode == "plugin"`` to ``"subprocess"`` for the authoring
    handlers, because a ``_subagent`` envelope would have no receiver
    outside an active OpenCode bridge plugin session. Authoring is
    therefore always reported as in-process here — anything else would
    misrepresent what the handlers actually got and mislabel the very
    incidents this attribution module is meant to clarify.

    The MCP-handler ``_subagent`` dispatch path still exists, but it is
    only reachable when callers invoke the handlers directly from inside
    an active OpenCode bridge plugin session (not from ``ooo auto``).
    """
    backend_name = state.runtime_backend or "unspecified"
    return f"in-process ({backend_name})"


def record_authoring_backend(state: AutoPipelineState) -> None:
    """Persist the resolved authoring backend on ``state`` as metadata.

    Call this *before* ``mark_blocked`` / ``mark_failed`` at every
    authoring-side blocker site. The recorded value lives in
    ``state.last_authoring_backend`` and is intended to be read by
    diagnostic surfaces, not concatenated into user-visible messages.
    """
    state.last_authoring_backend = authoring_backend_label(state)


__all__ = ["authoring_backend_label", "record_authoring_backend"]
