"""Gemini CLI event normalizer for Ouroboros.

Converts raw output lines from the Gemini CLI into a normalized internal event
schema that Ouroboros runtimes and orchestrators can work with uniformly,
regardless of whether the raw line is plain text or a structured JSON event.

The Gemini CLI can emit two kinds of output depending on how it is invoked:

- **Plain text**: A single block of prose written to stdout (typical for
  ``--non-interactive -p -`` mode).
- **JSON events** (NDJSON): Structured events emitted line-by-line when the
  CLI is run with ``--json`` or in an agentic mode; each line is a JSON object
  with at least a ``type`` field.

This normalizer handles both cases and maps them onto a minimal internal
event dict with the following guaranteed keys:

.. code-block:: python

    {
        "type": str,          # e.g. "text", "error", "tool_call", "thinking"
        "content": str,       # primary human-readable payload
        "raw": dict | list | str,  # original parsed object or raw line string
        "is_error": bool,     # True when the event represents an error
        "metadata": dict,     # supplementary key/value pairs from the raw event
    }

Usage::

    from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer

    normalizer = GeminiEventNormalizer()
    for line in gemini_output.splitlines():
        event = normalizer.normalize_line(line)
        print(event["type"], event["content"])
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

#: Event types that are considered errors
_ERROR_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "error",
        "fatal",
        "exception",
        "abort",
    }
)

#: JSON field names that carry the main text payload, tried in order
_CONTENT_FIELD_CANDIDATES: tuple[str, ...] = (
    "content",
    "text",
    "message",
    "output",
    "response",
    "data",
)

#: JSON field names that carry the event type, tried in order
_TYPE_FIELD_CANDIDATES: tuple[str, ...] = (
    "type",
    "event",
    "kind",
    "category",
)

#: Normalized event type returned for plain-text (non-JSON) lines
_PLAIN_TEXT_EVENT_TYPE = "text"

#: Normalized event type returned for unknown/unrecognised JSON events
_UNKNOWN_EVENT_TYPE = "unknown"


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class GeminiEventNormalizer:
    """Normalizes raw Gemini CLI output lines into a uniform internal event dict.

    The normalizer is stateless; the same instance can safely be reused across
    multiple completions.

    Attributes:
        strict_json: When ``True``, lines that look like JSON but fail to parse
            raise :class:`ValueError` rather than being silently downgraded to
            plain-text events.

    Example::

        normalizer = GeminiEventNormalizer()

        # Plain text line
        event = normalizer.normalize_line("Hello from Gemini.")
        assert event["type"] == "text"
        assert event["is_error"] is False

        # JSON event line
        event = normalizer.normalize_line('{"type": "error", "message": "quota"}')
        assert event["type"] == "error"
        assert event["is_error"] is True
    """

    def __init__(self, *, strict_json: bool = False) -> None:
        """Initialise the normalizer.

        Args:
            strict_json: When ``True``, JSON parse failures for lines that
                start with ``{`` raise :class:`ValueError` instead of
                falling back to plain-text treatment.
        """
        self.strict_json = strict_json

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize_line(self, raw_line: str) -> dict[str, Any]:
        """Normalize a single raw output line from the Gemini CLI.

        The function attempts JSON parsing first (when the stripped line starts
        with ``{`` or ``[``).  If the line is not JSON (or JSON parsing fails
        in non-strict mode) the line is treated as a plain-text event.

        Args:
            raw_line: A single line of raw Gemini CLI output (may have
                trailing whitespace / newline characters).

        Returns:
            Normalized event dict with keys ``type``, ``content``, ``raw``,
            ``is_error``, and ``metadata``.

        Raises:
            ValueError: If *raw_line* looks like JSON, ``strict_json=True``,
                and JSON parsing fails.
        """
        stripped = raw_line.strip()

        if stripped.startswith("{") or stripped.startswith("["):
            return self._normalize_json_line(stripped, original=raw_line)

        return self._normalize_text_line(stripped, original=raw_line)

    def normalize_lines(self, raw_output: str) -> list[dict[str, Any]]:
        """Normalize an entire multi-line Gemini CLI output string.

        Blank lines are skipped.

        Args:
            raw_output: Full stdout from a Gemini CLI subprocess call.

        Returns:
            List of normalized event dicts, one per non-blank line.
        """
        events: list[dict[str, Any]] = []
        for line in raw_output.splitlines():
            if not line.strip():
                continue
            events.append(self.normalize_line(line))
        return events

    # ------------------------------------------------------------------
    # JSON path
    # ------------------------------------------------------------------

    def _normalize_json_line(
        self,
        stripped: str,
        *,
        original: str,
    ) -> dict[str, Any]:
        """Parse and normalise a JSON event line.

        Args:
            stripped: The stripped version of the line.
            original: The original, un-stripped line (stored in ``raw``).

        Returns:
            Normalized event dict.

        Raises:
            ValueError: When ``strict_json=True`` and the JSON parse fails.
        """
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if self.strict_json:
                msg = f"Failed to parse Gemini CLI JSON event: {exc}"
                raise ValueError(msg) from exc
            log.debug("gemini_event_normalizer.json_parse_failed: %s", stripped[:120])
            return self._normalize_text_line(stripped, original=original)

        if isinstance(parsed, list):
            return self._build_event(
                event_type="list",
                content=json.dumps(parsed),
                raw=parsed,
                metadata={},
                is_error=False,
            )

        if not isinstance(parsed, dict):
            return self._normalize_text_line(stripped, original=original)

        return self._normalize_json_dict(parsed, original=original)

    def _normalize_json_dict(
        self,
        parsed: dict[str, Any],
        *,
        original: str,
    ) -> dict[str, Any]:
        """Map a parsed JSON dict to the internal event schema.

        Args:
            parsed: The parsed JSON object as a Python dict.
            original: The original raw line (stored in ``raw``).

        Returns:
            Normalized event dict.
        """
        # --- Determine event type ---
        event_type: str = _UNKNOWN_EVENT_TYPE
        for key in _TYPE_FIELD_CANDIDATES:
            if key in parsed and isinstance(parsed[key], str):
                event_type = parsed[key].strip().lower() or _UNKNOWN_EVENT_TYPE
                break

        # --- Determine content ---
        content: str = ""
        for key in _CONTENT_FIELD_CANDIDATES:
            if key in parsed:
                val = parsed[key]
                if isinstance(val, str):
                    content = val
                elif val is not None:
                    content = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
                break

        # --- Determine error flag ---
        is_error = self._is_error_event(event_type, parsed)

        # --- Collect metadata (everything except type/content fields) ---
        skip_keys = set(_TYPE_FIELD_CANDIDATES) | set(_CONTENT_FIELD_CANDIDATES)
        metadata = {k: v for k, v in parsed.items() if k not in skip_keys}

        return self._build_event(
            event_type=event_type,
            content=content,
            raw=parsed,
            metadata=metadata,
            is_error=is_error,
        )

    # ------------------------------------------------------------------
    # Plain-text path
    # ------------------------------------------------------------------

    def _normalize_text_line(
        self,
        stripped: str,
        *,
        original: str,
    ) -> dict[str, Any]:
        """Wrap a plain-text line as a ``text`` event.

        Args:
            stripped: Stripped content of the line.
            original: Original raw line (stored in ``raw``).

        Returns:
            Normalized ``text`` event dict.
        """
        return self._build_event(
            event_type=_PLAIN_TEXT_EVENT_TYPE,
            content=stripped,
            raw=original,
            metadata={},
            is_error=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_error_event(event_type: str, parsed: dict[str, Any]) -> bool:
        """Return ``True`` if the event represents an error condition.

        An event is treated as an error when:

        - Its ``type`` (or ``kind``) is in :data:`_ERROR_EVENT_TYPES`.
        - The dict contains a boolean ``"is_error"`` field set to ``True``.
        - The dict contains a boolean ``"error"`` field set to ``True``.
        - The dict contains a non-empty string ``"error"`` field (error message).
        - The dict contains a ``"status"`` field set to ``"error"`` or
          ``"failed"``.

        Args:
            event_type: The already-normalised event type string.
            parsed: The full parsed JSON dict.

        Returns:
            ``True`` when the event should be treated as an error.
        """
        if event_type in _ERROR_EVENT_TYPES:
            return True

        if parsed.get("is_error") is True:
            return True

        error_val = parsed.get("error")
        if error_val is True:
            return True
        if isinstance(error_val, str) and error_val.strip():
            return True

        status = parsed.get("status", "")
        return isinstance(status, str) and status.strip().lower() in ("error", "failed")

    @staticmethod
    def _build_event(
        *,
        event_type: str,
        content: str,
        raw: dict[str, Any] | list[Any] | str,
        metadata: dict[str, Any],
        is_error: bool,
    ) -> dict[str, Any]:
        """Construct a normalized event dict.

        Args:
            event_type: Normalized event type string.
            content: Primary human-readable payload.
            raw: The original parsed object or raw string.
            metadata: Supplementary key/value pairs.
            is_error: Whether this event represents an error condition.

        Returns:
            Normalized event dict.
        """
        return {
            "type": event_type,
            "content": content,
            "raw": raw,
            "is_error": is_error,
            "metadata": metadata,
        }


__all__ = ["GeminiEventNormalizer"]
