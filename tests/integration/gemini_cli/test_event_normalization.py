"""Integration tests for the Gemini CLI event normalization pipeline.

Verifies that raw Gemini CLI stdout/stderr output — both plain text and
NDJSON — is correctly translated into the normalized internal event schema
used throughout Ouroboros runtimes.

The internal event schema guarantees the following keys on every event:

.. code-block:: python

    {
        "type":     str,          # e.g. "text", "error", "tool_use"
        "content":  str,          # primary human-readable payload
        "raw":      dict | str,   # original parsed dict or raw line string
        "is_error": bool,         # True when the event represents an error
        "metadata": dict,         # supplementary key/value pairs
    }

Test classes
------------
- TestPlainTextNormalization        — plain stdout text lines → ``text`` events
- TestJsonEventNormalization        — known Gemini CLI JSON event types
- TestErrorDetection                — all paths that set ``is_error=True``
- TestMetadataExtraction            — ``metadata`` dict contents
- TestNormalizeLinesAPI             — ``normalize_lines()`` multi-line helper
- TestFakeEmitterPipeline           — end-to-end pipeline via FakeGeminiEventStreamEmitter
- TestHappyPathPipeline             — full happy-path event sequence
- TestToolUsePipeline               — tool-call/result event sequence
- TestErrorPipeline                 — error event sequence
- TestMultiTurnPipeline             — multi-turn (multiple tool cycles) sequence
- TestEdgeCases                     — blank lines, whitespace, unknown types, lists
- TestStrictJsonMode                — strict_json=True raises on bad JSON
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

import json
from typing import Any

import pytest

from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer

# Re-use the event constructors and helpers that live in the integration conftest
# so tests stay consistent with the broader test suite vocabulary.
from tests.integration.gemini_cli.conftest import (
    FakeGeminiEventStreamEmitter,
    gemini_event_error,
    gemini_event_init,
    gemini_event_message,
    gemini_event_result,
    gemini_event_thinking,
    gemini_event_tool_result,
    gemini_event_tool_use,
    make_error_events,
    make_happy_path_events,
    make_tool_use_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_jsonl(event: dict[str, Any]) -> str:
    """Serialise an event dict to a single JSONL line (no trailing newline)."""
    return json.dumps(event)


def _pipeline_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run a list of raw event dicts through the normalizer end-to-end.

    Simulates the typical adapter code path:
    1. Each event dict is serialised to a JSONL line (as the CLI would emit).
    2. Each line is fed through :class:`GeminiEventNormalizer`.
    3. The resulting normalized events are returned as a list.
    """
    normalizer = GeminiEventNormalizer()
    normalized: list[dict[str, Any]] = []
    for event in events:
        line = json.dumps(event)
        normalized.append(normalizer.normalize_line(line))
    return normalized


async def _collect_emitter_events(
    emitter: FakeGeminiEventStreamEmitter,
    normalizer: GeminiEventNormalizer | None = None,
) -> list[dict[str, Any]]:
    """Drain an async emitter and normalize every JSONL line it yields.

    Args:
        emitter: Preconfigured :class:`FakeGeminiEventStreamEmitter`.
        normalizer: Optional normalizer instance; a default one is created
            if not provided.

    Returns:
        List of normalized event dicts, one per emitted line.
    """
    norm = normalizer or GeminiEventNormalizer()
    result: list[dict[str, Any]] = []
    async for raw_line in emitter:
        result.append(norm.normalize_line(raw_line))
    return result


# ---------------------------------------------------------------------------
# Required keys in every normalized event
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = frozenset({"type", "content", "raw", "is_error", "metadata"})


def _assert_schema(event: dict[str, Any]) -> None:
    """Assert that *event* contains all required schema keys with correct types."""
    assert _REQUIRED_KEYS.issubset(event.keys()), f"Missing keys: {_REQUIRED_KEYS - event.keys()}"
    assert isinstance(event["type"], str), "type must be str"
    assert isinstance(event["content"], str), "content must be str"
    assert isinstance(event["is_error"], bool), "is_error must be bool"
    assert isinstance(event["metadata"], dict), "metadata must be dict"


# ===========================================================================
# 1. Plain text normalization
# ===========================================================================


class TestPlainTextNormalization:
    """Plain stdout/stderr text lines should produce ``text`` events."""

    def test_plain_line_type_is_text(self) -> None:
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("Hello from Gemini.")
        assert event["type"] == "text"

    def test_plain_line_content_matches_input(self) -> None:
        normalizer = GeminiEventNormalizer()
        text = "The quick brown fox jumps over the lazy dog."
        event = normalizer.normalize_line(text)
        assert event["content"] == text

    def test_plain_line_is_not_error(self) -> None:
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("Some normal output line.")
        assert event["is_error"] is False

    def test_plain_line_raw_is_original(self) -> None:
        """raw field stores the original (stripped) line for plain-text events."""
        normalizer = GeminiEventNormalizer()
        line = "  indented line  "
        event = normalizer.normalize_line(line)
        # raw receives the *original* line (may still have outer whitespace)
        assert event["raw"] == line

    def test_plain_line_metadata_is_empty(self) -> None:
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("Simple text output.")
        assert event["metadata"] == {}

    def test_plain_line_schema_complete(self) -> None:
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("Any line of text.")
        _assert_schema(event)

    def test_whitespace_only_leading_trailing_stripped_in_content(self) -> None:
        """Leading/trailing whitespace is stripped from the content value."""
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("   hello world   ")
        assert event["content"] == "hello world"

    def test_multiline_text_via_normalize_lines(self) -> None:
        """normalize_lines produces one text event per non-blank line."""
        normalizer = GeminiEventNormalizer()
        output = "line one\nline two\nline three\n"
        events = normalizer.normalize_lines(output)
        assert len(events) == 3
        assert all(e["type"] == "text" for e in events)
        contents = [e["content"] for e in events]
        assert contents == ["line one", "line two", "line three"]

    def test_stderr_like_plain_text_normalized(self) -> None:
        """stderr diagnostic messages (plain text) should produce text events."""
        normalizer = GeminiEventNormalizer()
        stderr_line = "Warning: model gemini-2.5-pro may take longer than expected."
        event = normalizer.normalize_line(stderr_line)
        assert event["type"] == "text"
        assert event["is_error"] is False


# ===========================================================================
# 2. JSON event normalization — known Gemini CLI event types
# ===========================================================================


class TestJsonEventNormalization:
    """Each known Gemini CLI JSON event type is mapped to the correct schema."""

    def test_init_type(self) -> None:
        raw = gemini_event_init("sess-abc123")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "init"

    def test_init_session_id_in_metadata(self) -> None:
        raw = gemini_event_init("sess-abc123")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        # session_id is not a canonical content/type field → lands in metadata
        assert event["metadata"].get("session_id") == "sess-abc123"

    def test_init_not_error(self) -> None:
        raw = gemini_event_init("sess-001")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_init_schema(self) -> None:
        raw = gemini_event_init("sess-002")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- thinking ----

    def test_thinking_type(self) -> None:
        raw = gemini_event_thinking("Let me reason about this.")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "thinking"

    def test_thinking_content(self) -> None:
        reasoning = "I should read the relevant file first."
        raw = gemini_event_thinking(reasoning)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["content"] == reasoning

    def test_thinking_not_error(self) -> None:
        raw = gemini_event_thinking("step 1 is to inspect the workspace.")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_thinking_schema(self) -> None:
        raw = gemini_event_thinking("thinking …")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- message ----

    def test_message_type(self) -> None:
        raw = gemini_event_message("Task completed.")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "message"

    def test_message_content(self) -> None:
        text = "All acceptance criteria satisfied."
        raw = gemini_event_message(text)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["content"] == text

    def test_message_not_error(self) -> None:
        raw = gemini_event_message("Done.")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_message_schema(self) -> None:
        raw = gemini_event_message("hello")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- tool_use ----

    def test_tool_use_type(self) -> None:
        raw = gemini_event_tool_use("Read", {"file_path": "src/foo.py"})
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "tool_use"

    def test_tool_use_name_in_metadata(self) -> None:
        raw = gemini_event_tool_use("Read", {"file_path": "src/foo.py"})
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"].get("name") == "Read"

    def test_tool_use_args_in_metadata(self) -> None:
        args = {"file_path": "src/foo.py"}
        raw = gemini_event_tool_use("Read", args)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"].get("input") == args

    def test_tool_use_not_error(self) -> None:
        raw = gemini_event_tool_use("Read")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_tool_use_schema(self) -> None:
        raw = gemini_event_tool_use("Bash", {"command": "ls"})
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- tool_result ----

    def test_tool_result_type(self) -> None:
        raw = gemini_event_tool_result("Read", "file contents here")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "tool_result"

    def test_tool_result_output_in_content_or_metadata(self) -> None:
        """``output`` field should surface either in content or metadata."""
        raw = gemini_event_tool_result("Read", "contents")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        # output may map to content (via _CONTENT_FIELD_CANDIDATES fallback)
        # or to metadata; either is acceptable — the data must not be lost.
        payload_present = (
            event["content"] == "contents" or event["metadata"].get("output") == "contents"
        )
        assert payload_present, f"output not found in event: {event}"

    def test_tool_result_not_error_by_default(self) -> None:
        raw = gemini_event_tool_result("Read", "ok", is_error=False)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_tool_result_schema(self) -> None:
        raw = gemini_event_tool_result("Read", "data")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- done ----

    def test_done_type(self) -> None:
        raw = gemini_event_result(exit_code=0)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "result"

    def test_done_exit_code_in_metadata(self) -> None:
        raw = gemini_event_result(exit_code=0)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"].get("exit_code") == 0

    def test_done_not_error(self) -> None:
        raw = gemini_event_result()
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is False

    def test_done_schema(self) -> None:
        raw = gemini_event_result()
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- error ----

    def test_error_type(self) -> None:
        raw = gemini_event_error("quota exceeded")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["type"] == "error"

    def test_error_is_error_flag(self) -> None:
        raw = gemini_event_error("something went wrong")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["is_error"] is True

    def test_error_message_in_content(self) -> None:
        msg = "Rate limit exceeded. Please try again later."
        raw = gemini_event_error(msg)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["content"] == msg

    def test_error_exit_code_in_metadata(self) -> None:
        raw = gemini_event_error("bad thing", exit_code=1)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"].get("exit_code") == 1

    def test_error_schema(self) -> None:
        raw = gemini_event_error("err")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        _assert_schema(event)

    # ---- raw field is the parsed dict ----

    def test_raw_field_is_original_dict(self) -> None:
        raw_dict = gemini_event_message("hello")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw_dict))
        assert event["raw"] == raw_dict


# ===========================================================================
# 3. Error detection — all paths that set is_error=True
# ===========================================================================


class TestErrorDetection:
    """The normalizer must set ``is_error=True`` for all error signals."""

    @pytest.mark.parametrize("error_type", ["error", "fatal", "exception", "abort"])
    def test_error_type_names_trigger_is_error(self, error_type: str) -> None:
        line = json.dumps({"type": error_type, "content": "something bad"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True, f"Expected is_error for type={error_type!r}"

    def test_error_field_bool_true(self) -> None:
        line = json.dumps({"type": "message", "content": "ok", "error": True})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True

    def test_error_field_bool_false_not_error(self) -> None:
        line = json.dumps({"type": "message", "content": "ok", "error": False})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is False

    def test_error_field_non_empty_string(self) -> None:
        line = json.dumps({"type": "message", "content": "partial", "error": "quota"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True

    def test_error_field_empty_string_not_error(self) -> None:
        line = json.dumps({"type": "message", "content": "ok", "error": ""})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is False

    def test_status_error_string(self) -> None:
        line = json.dumps({"type": "response", "content": "nope", "status": "error"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True

    def test_status_failed_string(self) -> None:
        line = json.dumps({"type": "response", "content": "nope", "status": "failed"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True

    def test_status_success_not_error(self) -> None:
        line = json.dumps({"type": "response", "content": "ok", "status": "success"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is False

    def test_plain_text_never_error(self) -> None:
        event = GeminiEventNormalizer().normalize_line("this is error output from CLI")
        # Plain text is never marked as error regardless of word content
        assert event["is_error"] is False

    def test_status_case_insensitive(self) -> None:
        """Status field matching is case-insensitive."""
        line = json.dumps({"type": "response", "content": "x", "status": "ERROR"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True

    def test_multiple_error_signals_combined(self) -> None:
        """When multiple error signals are present is_error is still True."""
        line = json.dumps(
            {
                "type": "error",
                "message": "critical failure",
                "error": True,
                "status": "failed",
            }
        )
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is True


# ===========================================================================
# 4. Metadata extraction
# ===========================================================================


class TestMetadataExtraction:
    """Fields that are not type/content candidates go into metadata."""

    def test_metadata_excludes_type_field(self) -> None:
        line = json.dumps({"type": "message", "content": "hi"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert "type" not in event["metadata"]

    def test_metadata_excludes_content_field(self) -> None:
        line = json.dumps({"type": "message", "content": "hi"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert "content" not in event["metadata"]

    def test_extra_fields_land_in_metadata(self) -> None:
        line = json.dumps(
            {
                "type": "tool_use",
                "content": "",
                "name": "Write",
                "input": {"file_path": "out.txt"},
                "call_id": "call-42",
            }
        )
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["metadata"]["name"] == "Write"
        assert event["metadata"]["input"] == {"file_path": "out.txt"}
        assert event["metadata"]["call_id"] == "call-42"

    def test_session_id_in_metadata_for_init(self) -> None:
        raw = gemini_event_init("gemini-sess-xyz")
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"]["session_id"] == "gemini-sess-xyz"

    def test_exit_code_in_metadata_for_done(self) -> None:
        raw = gemini_event_result(exit_code=0)
        event = GeminiEventNormalizer().normalize_line(_as_jsonl(raw))
        assert event["metadata"]["exit_code"] == 0

    def test_metadata_empty_for_minimal_json(self) -> None:
        """A JSON dict with only type + content has no extra metadata."""
        line = json.dumps({"type": "message", "content": "hello"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["metadata"] == {}

    def test_metadata_preserves_nested_dicts(self) -> None:
        """Nested dict values in extra fields are preserved as-is."""
        line = json.dumps(
            {
                "type": "tool_use",
                "content": "",
                "input": {"nested": {"deep": True}},
            }
        )
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["metadata"]["input"] == {"nested": {"deep": True}}


# ===========================================================================
# 5. normalize_lines() multi-line API
# ===========================================================================


class TestNormalizeLinesAPI:
    """normalize_lines() processes an entire multi-line output string."""

    def test_returns_list_of_events(self) -> None:
        normalizer = GeminiEventNormalizer()
        output = (
            json.dumps(gemini_event_init("s1"))
            + "\n"
            + json.dumps(gemini_event_message("hi"))
            + "\n"
            + json.dumps(gemini_event_result())
            + "\n"
        )
        events = normalizer.normalize_lines(output)
        assert isinstance(events, list)
        assert len(events) == 3

    def test_blank_lines_are_skipped(self) -> None:
        normalizer = GeminiEventNormalizer()
        output = "line one\n\n\nline two\n"
        events = normalizer.normalize_lines(output)
        assert len(events) == 2

    def test_empty_string_returns_empty_list(self) -> None:
        normalizer = GeminiEventNormalizer()
        events = normalizer.normalize_lines("")
        assert events == []

    def test_whitespace_only_string_returns_empty_list(self) -> None:
        normalizer = GeminiEventNormalizer()
        events = normalizer.normalize_lines("   \n  \n  ")
        assert events == []

    def test_event_types_preserved_in_order(self) -> None:
        normalizer = GeminiEventNormalizer()
        events_raw = make_happy_path_events("sess-1", "All done.")
        output = "\n".join(json.dumps(e) for e in events_raw) + "\n"
        normalized = normalizer.normalize_lines(output)
        types = [e["type"] for e in normalized]
        assert types == ["init", "thinking", "message", "result"]

    def test_each_event_passes_schema_check(self) -> None:
        normalizer = GeminiEventNormalizer()
        events_raw = make_tool_use_events("sess-2")
        output = "\n".join(json.dumps(e) for e in events_raw) + "\n"
        normalized = normalizer.normalize_lines(output)
        for event in normalized:
            _assert_schema(event)

    def test_mixed_json_and_text_lines(self) -> None:
        """Lines that aren't JSON are treated as text events; JSON lines are parsed."""
        normalizer = GeminiEventNormalizer()
        output = (
            "plain text output line\n"
            + json.dumps({"type": "thinking", "content": "reasoning"})
            + "\n"
            + "another plain text line\n"
        )
        events = normalizer.normalize_lines(output)
        assert len(events) == 3
        assert events[0]["type"] == "text"
        assert events[1]["type"] == "thinking"
        assert events[2]["type"] == "text"


# ===========================================================================
# 6. FakeGeminiEventStreamEmitter pipeline
# ===========================================================================


class TestFakeEmitterPipeline:
    """End-to-end pipeline: async emitter → normalize_line → schema check."""

    @pytest.mark.asyncio
    async def test_emitter_yields_normalizable_lines(self) -> None:
        emitter = FakeGeminiEventStreamEmitter([gemini_event_message("hi")])
        events = await _collect_emitter_events(emitter)
        assert len(events) == 1
        assert events[0]["type"] == "message"

    @pytest.mark.asyncio
    async def test_all_emitter_events_pass_schema(self) -> None:
        raw_events = make_happy_path_events("sess-emu-1")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        for event in events:
            _assert_schema(event)

    @pytest.mark.asyncio
    async def test_emitter_event_count_matches(self) -> None:
        raw_events = make_happy_path_events("sess-emu-2")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        assert len(events) == emitter.event_count

    @pytest.mark.asyncio
    async def test_error_emitter_produces_error_event(self) -> None:
        raw_events = make_error_events("sess-emu-3", "timeout")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        error_events = [e for e in events if e["is_error"]]
        assert len(error_events) >= 1

    @pytest.mark.asyncio
    async def test_tool_use_emitter_contains_tool_use(self) -> None:
        raw_events = make_tool_use_events("sess-emu-4")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        types = [e["type"] for e in events]
        assert "tool_use" in types

    @pytest.mark.asyncio
    async def test_tool_use_emitter_contains_tool_result(self) -> None:
        raw_events = make_tool_use_events("sess-emu-5")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        types = [e["type"] for e in events]
        assert "tool_result" in types

    @pytest.mark.asyncio
    async def test_emitter_types_in_order(self) -> None:
        raw_events = make_happy_path_events("sess-emu-6", "Done!")
        emitter = FakeGeminiEventStreamEmitter(raw_events)
        events = await _collect_emitter_events(emitter)
        types = [e["type"] for e in events]
        assert types == ["init", "thinking", "message", "result"]


# ===========================================================================
# 7. Happy-path full pipeline (fixture-driven)
# ===========================================================================


class TestHappyPathPipeline:
    """Full happy-path sequence: init → thinking → message → done."""

    def test_correct_event_count(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        assert len(events) == len(gemini_happy_path_events)

    def test_event_types_in_order(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        types = [e["type"] for e in events]
        assert types == ["init", "thinking", "message", "result"]

    def test_no_error_events_in_happy_path(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        assert all(e["is_error"] is False for e in events)

    def test_message_content_correct(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        message_events = [e for e in events if e["type"] == "message"]
        assert len(message_events) == 1
        assert message_events[0]["content"] == "Task completed successfully."

    def test_thinking_content_non_empty(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        thinking_events = [e for e in events if e["type"] == "thinking"]
        assert len(thinking_events) == 1
        assert thinking_events[0]["content"]  # non-empty

    def test_session_id_preserved(
        self,
        gemini_happy_path_events: list[dict],
        gemini_session_id: str,
    ) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        started_events = [e for e in events if e["type"] == "init"]
        assert started_events[0]["metadata"]["session_id"] == gemini_session_id

    def test_all_events_pass_schema(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        for event in events:
            _assert_schema(event)

    def test_done_exit_code_zero(self, gemini_happy_path_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_happy_path_events)
        done_events = [e for e in events if e["type"] == "result"]
        assert len(done_events) == 1
        assert done_events[0]["metadata"]["exit_code"] == 0


# ===========================================================================
# 8. Tool-use pipeline
# ===========================================================================


class TestToolUsePipeline:
    """Tool-call/result event sequence is correctly normalized end-to-end."""

    def test_event_count(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        assert len(events) == len(gemini_tool_use_events)

    def test_event_types_in_order(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        types = [e["type"] for e in events]
        assert types == [
            "init",
            "thinking",
            "tool_use",
            "tool_result",
            "message",
            "result",
        ]

    def test_tool_use_name_preserved(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        tool_use = next(e for e in events if e["type"] == "tool_use")
        assert tool_use["metadata"]["name"] == "Read"

    def test_tool_use_args_preserved(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        tool_use = next(e for e in events if e["type"] == "tool_use")
        assert "input" in tool_use["metadata"]

    def test_tool_result_present(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        result_events = [e for e in events if e["type"] == "tool_result"]
        assert len(result_events) == 1

    def test_no_error_in_successful_tool_use(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        assert all(e["is_error"] is False for e in events)

    def test_all_events_pass_schema(self, gemini_tool_use_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_tool_use_events)
        for event in events:
            _assert_schema(event)


# ===========================================================================
# 9. Error pipeline
# ===========================================================================


class TestErrorPipeline:
    """Error event sequence produces at least one error-flagged event."""

    def test_error_event_is_marked(self, gemini_error_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_error_events)
        error_events = [e for e in events if e["is_error"]]
        assert len(error_events) >= 1

    def test_error_event_type_is_error(self, gemini_error_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_error_events)
        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) >= 1

    def test_error_message_in_content(self, gemini_error_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_error_events)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events[0]["content"]  # non-empty message

    def test_init_precedes_error(self, gemini_error_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_error_events)
        types = [e["type"] for e in events]
        assert types[0] == "init"
        assert "error" in types

    def test_all_events_pass_schema(self, gemini_error_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_error_events)
        for event in events:
            _assert_schema(event)

    def test_rate_limit_error_content(
        self,
        gemini_error_events: list[dict],
    ) -> None:
        events = _pipeline_from_events(gemini_error_events)
        error_events = [e for e in events if e["type"] == "error"]
        assert "rate limit" in error_events[0]["content"].lower()


# ===========================================================================
# 10. Multi-turn pipeline
# ===========================================================================


class TestMultiTurnPipeline:
    """Multi-turn sequences (3 tool cycles) are fully normalized."""

    def test_event_count(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        assert len(events) == len(gemini_multi_turn_events)

    def test_three_tool_use_events(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        tool_uses = [e for e in events if e["type"] == "tool_use"]
        assert len(tool_uses) == 3

    def test_three_tool_result_events(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 3

    def test_three_message_events(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        messages = [e for e in events if e["type"] == "message"]
        assert len(messages) == 3

    def test_no_errors_in_multi_turn(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        assert all(e["is_error"] is False for e in events)

    def test_ends_with_done(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        assert events[-1]["type"] == "result"

    def test_all_events_pass_schema(self, gemini_multi_turn_events: list[dict]) -> None:
        events = _pipeline_from_events(gemini_multi_turn_events)
        for event in events:
            _assert_schema(event)


# ===========================================================================
# 11. Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge cases: unknown events, list JSON, whitespace, malformed lines."""

    def test_unknown_json_event_type_is_unknown(self) -> None:
        line = json.dumps({"type": "new_future_event", "content": "x"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "new_future_event"

    def test_json_without_type_field_uses_unknown(self) -> None:
        line = json.dumps({"content": "some message"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "unknown"

    def test_empty_type_field_falls_back_to_unknown(self) -> None:
        line = json.dumps({"type": "", "content": "x"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "unknown"

    def test_json_array_produces_list_event(self) -> None:
        line = json.dumps(["item1", "item2"])
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "list"

    def test_json_array_content_is_serialized(self) -> None:
        payload = ["item1", "item2"]
        line = json.dumps(payload)
        event = GeminiEventNormalizer().normalize_line(line)
        assert json.loads(event["content"]) == payload

    def test_json_array_not_error(self) -> None:
        line = json.dumps(["a", "b"])
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["is_error"] is False

    def test_whitespace_only_line_treated_as_text(self) -> None:
        """A line with only whitespace becomes an empty-content text event."""
        normalizer = GeminiEventNormalizer()
        event = normalizer.normalize_line("   ")
        assert event["type"] == "text"
        assert event["content"] == ""

    def test_malformed_json_fallback_to_text(self) -> None:
        """A line that starts with '{' but isn't valid JSON falls back to text."""
        line = '{"broken json'
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "text"
        assert event["is_error"] is False

    def test_deeply_nested_json_preserved_in_raw(self) -> None:
        nested = {
            "type": "tool_use",
            "content": "",
            "input": {"level1": {"level2": {"level3": True}}},
        }
        event = GeminiEventNormalizer().normalize_line(json.dumps(nested))
        assert event["raw"] == nested

    def test_type_field_is_lowercased(self) -> None:
        """The normalizer lowercases the event type for uniform matching."""
        line = json.dumps({"type": "MESSAGE", "content": "hi"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["type"] == "message"

    def test_content_field_candidates_order(self) -> None:
        """If 'content' is absent the normalizer falls back to 'text', then 'message'."""
        # 'text' field should be used when 'content' is missing
        line = json.dumps({"type": "thinking", "text": "my reasoning"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["content"] == "my reasoning"

    def test_message_fallback_when_content_and_text_missing(self) -> None:
        line = json.dumps({"type": "error", "message": "fatal crash"})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["content"] == "fatal crash"

    def test_integer_content_value_coerced_to_str(self) -> None:
        """Non-string content values are coerced to string."""
        line = json.dumps({"type": "result", "content": 42})
        event = GeminiEventNormalizer().normalize_line(line)
        assert event["content"] == "42"

    def test_null_content_value_results_in_empty_content(self) -> None:
        """null content (JSON null) should not crash and produce empty string or skip."""
        line = json.dumps({"type": "message", "content": None})
        event = GeminiEventNormalizer().normalize_line(line)
        # When content is None, normalizer should either produce "" or check next candidate
        assert isinstance(event["content"], str)

    def test_non_dict_non_list_json_falls_back_to_text(self) -> None:
        """A JSON primitive (number, string) falls back to text event."""
        line = "42"
        event = GeminiEventNormalizer().normalize_line(line)
        # 42 does not start with { or [ so it's plain text
        assert event["type"] == "text"

    def test_normalize_line_returns_dict(self) -> None:
        """normalize_line always returns a dict regardless of input."""
        normalizer = GeminiEventNormalizer()
        for line in [
            "plain text",
            '{"type": "message", "content": "hi"}',
            '{"broken":',
            "",
            "   ",
        ]:
            result = normalizer.normalize_line(line)
            assert isinstance(result, dict)


# ===========================================================================
# 12. Strict JSON mode
# ===========================================================================


class TestStrictJsonMode:
    """strict_json=True raises ValueError on malformed JSON lines."""

    def test_strict_raises_on_invalid_json(self) -> None:
        normalizer = GeminiEventNormalizer(strict_json=True)
        with pytest.raises(ValueError, match="Failed to parse Gemini CLI JSON event"):
            normalizer.normalize_line('{"unclosed":')

    def test_strict_does_not_raise_on_valid_json(self) -> None:
        normalizer = GeminiEventNormalizer(strict_json=True)
        event = normalizer.normalize_line('{"type": "message", "content": "hi"}')
        assert event["type"] == "message"

    def test_strict_does_not_raise_on_plain_text(self) -> None:
        normalizer = GeminiEventNormalizer(strict_json=True)
        event = normalizer.normalize_line("plain text line")
        assert event["type"] == "text"

    def test_non_strict_fallback_to_text_on_bad_json(self) -> None:
        normalizer = GeminiEventNormalizer(strict_json=False)
        # Should not raise — falls back to text
        event = normalizer.normalize_line('{"broken":')
        assert event["type"] == "text"

    def test_strict_mode_default_is_false(self) -> None:
        """GeminiEventNormalizer defaults to non-strict mode."""
        normalizer = GeminiEventNormalizer()
        assert normalizer.strict_json is False


# ===========================================================================
# 13. Reuse of normalizer instance (stateless)
# ===========================================================================


class TestNormalizerStateless:
    """The normalizer is stateless and can be reused across calls."""

    def test_reuse_across_event_types(self) -> None:
        normalizer = GeminiEventNormalizer()
        e1 = normalizer.normalize_line("plain text")
        e2 = normalizer.normalize_line('{"type": "message", "content": "hi"}')
        e3 = normalizer.normalize_line('{"type": "error", "message": "oops"}')

        assert e1["type"] == "text"
        assert e2["type"] == "message"
        assert e3["type"] == "error"
        assert e3["is_error"] is True

    def test_no_state_bleed_between_calls(self) -> None:
        normalizer = GeminiEventNormalizer()
        # First call with error
        normalizer.normalize_line('{"type": "error", "message": "boom"}')
        # Second call should not be contaminated
        event = normalizer.normalize_line('{"type": "message", "content": "ok"}')
        assert event["is_error"] is False
