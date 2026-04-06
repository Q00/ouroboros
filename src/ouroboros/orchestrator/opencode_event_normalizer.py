"""Event normalization for OpenCode CLI runtime output.

OpenCode's ``run --format json`` emits JSON lines with a distinct event schema
compared to Codex CLI.  This module converts those events into the internal
:class:`~ouroboros.orchestrator.adapter.AgentMessage` format used by the
Ouroboros orchestrator.

OpenCode JSON event types:
    - ``tool_use``    — tool call completed (or errored)
    - ``text``        — assistant text block
    - ``reasoning``   — thinking/reasoning block
    - ``step_start``  — turn/step lifecycle start
    - ``step_finish`` — turn/step lifecycle end
    - ``error``       — session-level error

Each event carries a ``sessionID`` field and a ``part`` payload whose shape
varies by type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle


@dataclass(frozen=True, slots=True)
class OpenCodeEventContext:
    """Contextual state tracked across a stream of OpenCode events.

    Attributes:
        session_id: The OpenCode session ID for the current execution.
        current_handle: The latest RuntimeHandle for resume support.
    """

    session_id: str | None = None
    current_handle: RuntimeHandle | None = None


class OpenCodeEventNormalizer:
    """Stateless converter from OpenCode JSON events to AgentMessage tuples.

    All conversion logic is exposed as ``@classmethod`` or
    ``@staticmethod`` methods.  No instance state is kept; the class
    serves purely as a namespace for the normalisation functions.

    Attributes:
        _TOOL_NAME_MAP: Mapping from lowercase OpenCode tool names to
            their canonical Ouroboros equivalents.
    """

    # Tool names that map to well-known Ouroboros tool concepts.
    _TOOL_NAME_MAP: dict[str, str] = {
        "bash": "Bash",
        "edit": "Edit",
        "write": "Write",
        "read": "Read",
        "glob": "Glob",
        "grep": "Grep",
        "list": "List",
        "webfetch": "WebFetch",
        "websearch": "WebSearch",
        "codesearch": "CodeSearch",
        "task": "Task",
        "todowrite": "TodoWrite",
        "skill": "Skill",
    }

    @classmethod
    def normalize(
        cls,
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Convert a single OpenCode JSON event into zero or more messages.

        Dispatches to the appropriate per-type handler based on the
        ``type`` field of *event*.

        Args:
            event: Parsed JSON dict from an
                ``opencode run --format json`` line.
            context: Accumulated session context for handle tracking.

        Returns:
            Tuple of normalised
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            values (possibly empty).
        """
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return ()

        handler = _EVENT_HANDLERS.get(event_type)
        if handler is not None:
            return handler(event, context)

        return ()

    # ------------------------------------------------------------------
    # Per-type handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_tool_use(
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise a ``tool_use`` event into an assistant message.

        Extracts tool name, input, status, output, error, and
        metadata from the event payload and produces a single
        :class:`~ouroboros.orchestrator.adapter.AgentMessage` with
        a human-readable content summary.

        Args:
            event: Parsed ``tool_use`` JSON event dict.
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple containing the normalised
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`,
            or an empty tuple if the event payload is malformed.
        """
        part = event.get("part", {})
        if not isinstance(part, dict):
            return ()

        tool = part.get("tool", "unknown")
        state = part.get("state", {})
        if not isinstance(state, dict):
            state = {}

        status = state.get("status", "")
        tool_input = state.get("input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}

        mapped_tool = OpenCodeEventNormalizer._TOOL_NAME_MAP.get(tool, tool)

        # Build a human-readable content summary
        detail = OpenCodeEventNormalizer._extract_tool_detail(tool, tool_input)
        content = (
            f"Calling tool: {mapped_tool}: {detail}" if detail else f"Calling tool: {mapped_tool}"
        )

        data: dict[str, Any] = {
            "tool_input": tool_input,
            "opencode_tool": tool,
            "status": status,
        }

        # Include output for completed tools
        output = state.get("output", "")
        if isinstance(output, str) and output.strip():
            data["output"] = output.strip()

        # Include error for failed tools
        error = state.get("error", "")
        if isinstance(error, str) and error.strip():
            data["error"] = error.strip()
            data["subtype"] = "runtime_error"

        # Include metadata if present
        metadata = state.get("metadata", {})
        if isinstance(metadata, dict) and metadata:
            data["metadata"] = metadata

        return (
            AgentMessage(
                type="assistant",
                content=content,
                tool_name=mapped_tool,
                data=data,
                resume_handle=context.current_handle,
            ),
        )

    @staticmethod
    def _handle_text(
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise a ``text`` event into an assistant message.

        Args:
            event: Parsed ``text`` JSON event dict.
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple with the text message, or an empty
            tuple when the text payload is empty or missing.
        """
        part = event.get("part", {})
        if not isinstance(part, dict):
            return ()

        text = part.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return ()

        return (
            AgentMessage(
                type="assistant",
                content=text.strip(),
                resume_handle=context.current_handle,
            ),
        )

    @staticmethod
    def _handle_reasoning(
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise a ``reasoning`` event into an assistant message.

        The reasoning text is stored in the ``data["thinking"]`` key
        alongside the main content.

        Args:
            event: Parsed ``reasoning`` JSON event dict.
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple with the reasoning message, or an
            empty tuple when empty.
        """
        part = event.get("part", {})
        if not isinstance(part, dict):
            return ()

        text = part.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return ()

        return (
            AgentMessage(
                type="assistant",
                content=text.strip(),
                data={"thinking": text.strip()},
                resume_handle=context.current_handle,
            ),
        )

    @staticmethod
    def _handle_error(
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise an ``error`` event into a final result message.

        Extracts a human-readable error string from the ``error``
        field, which may be a dict with ``name``/``data`` keys or a
        plain string.

        Args:
            event: Parsed ``error`` JSON event dict.
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple containing a ``result``-type
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`.
        """
        error = event.get("error", {})
        if isinstance(error, dict):
            name = error.get("name", "")
            data = error.get("data", {})
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message", "")
            error_text = msg or name or "OpenCode reported an error"
        elif isinstance(error, str):
            error_text = error
        else:
            error_text = "OpenCode reported an error"

        return (
            AgentMessage(
                type="result",
                content=error_text,
                data={"subtype": "error", "error_type": "OpenCodeError"},
                resume_handle=context.current_handle,
            ),
        )

    @staticmethod
    def _handle_step_start(
        _event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise a ``step_start`` event into a system message.

        Emitted as a lifecycle marker so downstream consumers can
        track turn boundaries.

        Args:
            _event: Parsed ``step_start`` JSON event dict (unused).
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple containing a ``system``-type
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`.
        """
        return (
            AgentMessage(
                type="system",
                content="OpenCode step started",
                data={"subtype": "step_start"},
                resume_handle=context.current_handle,
            ),
        )

    @staticmethod
    def _handle_step_finish(
        _event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> tuple[AgentMessage, ...]:
        """Normalise a ``step_finish`` event into a system message.

        Args:
            _event: Parsed ``step_finish`` JSON event dict (unused).
            context: Session context carrying the current handle.

        Returns:
            Single-element tuple containing a ``system``-type
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`.
        """
        return (
            AgentMessage(
                type="system",
                content="OpenCode step finished",
                data={"subtype": "step_finish"},
                resume_handle=context.current_handle,
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_detail(tool: str, tool_input: dict[str, Any]) -> str:
        """Extract a short human-readable detail from tool input.

        Looks up a canonical key for the given tool (e.g.
        ``"command"`` for bash, ``"filePath"`` for edit) and returns
        its value, truncated to 80 characters.

        Args:
            tool: Lowercase OpenCode tool name.
            tool_input: Parsed input dict from the event payload.

        Returns:
            Detail string (up to 80 chars), or an empty string when
            no suitable key is found.
        """
        _detail_keys: dict[str, str] = {
            "bash": "command",
            "edit": "filePath",
            "write": "filePath",
            "read": "filePath",
            "glob": "pattern",
            "grep": "pattern",
            "webfetch": "url",
            "websearch": "query",
            "codesearch": "query",
            "task": "description",
            "skill": "name",
        }
        key = _detail_keys.get(tool)
        if key:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                if len(detail) > 80:
                    return detail[:77] + "..."
                return detail
        return ""


def normalize_opencode_event(
    event: dict[str, Any],
    context: OpenCodeEventContext,
) -> tuple[AgentMessage, ...]:
    """Module-level convenience for :meth:`OpenCodeEventNormalizer.normalize`.

    Args:
        event: Parsed JSON event dict.
        context: Session context for handle tracking.

    Returns:
        Tuple of normalised
        :class:`~ouroboros.orchestrator.adapter.AgentMessage` values.
    """
    return OpenCodeEventNormalizer.normalize(event, context)


# Dispatch table mapping event type strings to handler methods.
_EVENT_HANDLERS: dict[str, Any] = {
    "tool_use": OpenCodeEventNormalizer._handle_tool_use,
    "text": OpenCodeEventNormalizer._handle_text,
    "reasoning": OpenCodeEventNormalizer._handle_reasoning,
    "error": OpenCodeEventNormalizer._handle_error,
    "step_start": OpenCodeEventNormalizer._handle_step_start,
    "step_finish": OpenCodeEventNormalizer._handle_step_finish,
}


__all__ = [
    "OpenCodeEventContext",
    "OpenCodeEventNormalizer",
    "normalize_opencode_event",
]
