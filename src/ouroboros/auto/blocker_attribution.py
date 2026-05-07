"""Decorate auto-pipeline blocker messages with phase + backend attribution.

Issue #690 surfaced a class of incidents where a goal like
"open and merge a PR" hit ``interview.start timed out after 60s`` and
the user could not tell whether the timeout came from the in-process
authoring path or from the runtime adapter behind ``--runtime <X>``.

This helper appends a single
``[phase=<step>, authoring_backend=<resolved>]`` suffix to a blocker
message. It does not change timeout values, retry counts, or resume
semantics — those belong to the dedicated tickets (#686, #688).
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
    incidents this attribution helper is meant to clarify.

    The MCP-handler ``_subagent`` dispatch path still exists, but it is
    only reachable when callers invoke the handlers directly from inside
    an active OpenCode bridge plugin session (not from ``ooo auto``).
    """
    backend_name = state.runtime_backend or "unspecified"
    return f"in-process ({backend_name})"


def label_blocker(state: AutoPipelineState, message: str | None, *, phase: str) -> str:
    """Return ``message`` with a phase + authoring-backend suffix.

    Appends at most once: if the message already contains ``[phase=``
    the original is returned unchanged so nested call sites do not
    double-stamp the suffix.
    """
    text = message or ""
    if "[phase=" in text:
        return text
    suffix = f" [phase={phase}, authoring_backend={authoring_backend_label(state)}]"
    return text + suffix


__all__ = ["authoring_backend_label", "label_blocker"]
