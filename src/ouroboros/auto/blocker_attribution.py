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

_OPENCODE_RUNTIMES = frozenset({"opencode", "opencode_cli"})


def authoring_backend_label(state: AutoPipelineState) -> str:
    """Return the human-readable authoring path for a state.

    OpenCode + ``plugin`` mode is the only combination where the
    in-process authoring handler short-circuits to a ``_subagent``
    envelope. Every other runtime/mode pair runs the authoring step
    in-process inside the Ouroboros MCP server. The label mirrors
    ``ouroboros.mcp.tools.subagent.should_dispatch_via_plugin`` so the
    blocker text cannot drift from the actual handler dispatch.
    """
    backend = (state.runtime_backend or "").strip().lower()
    mode = (state.opencode_mode or "").strip().lower()
    if backend in _OPENCODE_RUNTIMES and mode == "plugin":
        return "dispatched (opencode bridge plugin)"
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
