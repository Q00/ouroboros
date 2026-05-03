"""Unit tests for the I/O Journal foundation (issue #517).

Coverage:
- Each of the four event factories produces the documented shape and
  honours the ``target_type`` / ``target_id`` invariant.
- Optional correlation fields are omitted from the payload when not
  provided (mirrors the policy used in events/control.py).
- Preview helpers cap at the configured length, append the
  ``… <truncated len=N>`` marker only on actual truncation, and apply
  the active privacy mode (``on`` / ``off`` / ``redacted``).
- ``content_hash`` returns ``sha256:<hex>`` and is deterministic.
- ``new_call_id`` returns 26-char ULIDs from the Crockford alphabet
  and is sortable on monotonic time.
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from ouroboros.events.io import (
    PRIVACY_ENV_VAR,
    REDACTION_MARKER_TEMPLATE,
    TRUNCATION_MARKER_TEMPLATE,
    PrivacyMode,
    content_hash,
    create_llm_call_requested_event,
    create_llm_call_returned_event,
    create_tool_call_returned_event,
    create_tool_call_started_event,
    get_privacy_mode,
    new_call_id,
    shape_preview,
    truncate_preview,
)

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _strip_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRIVACY_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_returns_sha256_prefix(self) -> None:
        assert content_hash("payload").startswith("sha256:")

    def test_matches_stdlib_sha256(self) -> None:
        expected = hashlib.sha256(b"payload").hexdigest()
        assert content_hash("payload") == f"sha256:{expected}"

    def test_string_and_bytes_match(self) -> None:
        assert content_hash("payload") == content_hash(b"payload")

    def test_different_payloads_differ(self) -> None:
        assert content_hash("a") != content_hash("b")


# ---------------------------------------------------------------------------
# ULID call_id
# ---------------------------------------------------------------------------


class TestNewCallId:
    def test_format_matches_ulid(self) -> None:
        for _ in range(8):
            assert _ULID_PATTERN.match(new_call_id()) is not None

    def test_unique_across_invocations(self) -> None:
        sample = {new_call_id() for _ in range(64)}
        assert len(sample) == 64

    def test_sortable_on_creation_time(self) -> None:
        a = new_call_id()
        # Allow a millisecond gap so the ULID timestamp prefix advances.
        import time

        time.sleep(0.005)
        b = new_call_id()
        assert a < b


# ---------------------------------------------------------------------------
# Privacy mode + preview shaping
# ---------------------------------------------------------------------------


class TestPrivacyMode:
    def test_default_is_on_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_privacy(monkeypatch)
        assert get_privacy_mode() is PrivacyMode.ON

    def test_recognised_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw, mode in (
            ("on", PrivacyMode.ON),
            ("off", PrivacyMode.OFF),
            ("redacted", PrivacyMode.REDACTED),
        ):
            monkeypatch.setenv(PRIVACY_ENV_VAR, raw)
            assert get_privacy_mode() is mode
        monkeypatch.setenv(PRIVACY_ENV_VAR, "OFF")
        assert get_privacy_mode() is PrivacyMode.OFF
        monkeypatch.setenv(PRIVACY_ENV_VAR, " redacted ")
        assert get_privacy_mode() is PrivacyMode.REDACTED

    def test_unknown_value_fails_closed_to_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(PRIVACY_ENV_VAR, "loud")
        assert get_privacy_mode() is PrivacyMode.OFF
        assert PRIVACY_ENV_VAR in caplog.text

    def test_invalid_env_value_does_not_preserve_preview(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PRIVACY_ENV_VAR, "of")
        assert shape_preview("SECRET prompt") is None

    def test_invalid_value_warns_once_per_normalised_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(PRIVACY_ENV_VAR, " noisy ")
        assert get_privacy_mode() is PrivacyMode.OFF
        assert get_privacy_mode() is PrivacyMode.OFF
        monkeypatch.setenv(PRIVACY_ENV_VAR, "NOISY")
        assert get_privacy_mode() is PrivacyMode.OFF

        warnings = [
            record
            for record in caplog.records
            if "Invalid OUROBOROS_IO_JOURNAL_PREVIEWS" in record.message
        ]
        assert len(warnings) == 1


class TestTruncatePreview:
    def test_short_text_passes_through(self) -> None:
        assert truncate_preview("hi", cap=10, hard_cap=20) == "hi"

    def test_truncates_at_cap_with_marker(self) -> None:
        text = "x" * 600
        out = truncate_preview(text, cap=256, hard_cap=4096)
        cap_body = "x" * 256
        marker = TRUNCATION_MARKER_TEMPLATE.format(length=600 - 256)
        assert out == cap_body + marker

    def test_hard_cap_overrides_caller_cap(self) -> None:
        text = "y" * 600
        out = truncate_preview(text, cap=10000, hard_cap=128)
        cap_body = "y" * 128
        marker = TRUNCATION_MARKER_TEMPLATE.format(length=600 - 128)
        assert out == cap_body + marker

    def test_zero_or_negative_cap_returns_empty(self) -> None:
        assert truncate_preview("x" * 10, cap=0, hard_cap=4096) == ""
        assert truncate_preview("x" * 10, cap=-5, hard_cap=4096) == ""


class TestShapePreview:
    def test_none_input_returns_none(self) -> None:
        assert shape_preview(None, privacy=PrivacyMode.ON) is None

    def test_off_mode_drops_preview(self) -> None:
        assert shape_preview("payload", privacy=PrivacyMode.OFF) is None

    def test_redacted_mode_returns_marker(self) -> None:
        out = shape_preview("hello world", privacy=PrivacyMode.REDACTED)
        assert out == REDACTION_MARKER_TEMPLATE.format(length=len("hello world"))

    def test_on_mode_truncates(self) -> None:
        text = "z" * 600
        out = shape_preview(text, cap=256, hard_cap=4096, privacy=PrivacyMode.ON)
        assert out is not None
        assert out.startswith("z" * 256)
        assert out.endswith(TRUNCATION_MARKER_TEMPLATE.format(length=600 - 256))

    def test_uses_env_when_privacy_arg_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PRIVACY_ENV_VAR, "off")
        assert shape_preview("payload") is None


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


class TestToolCallStartedFactory:
    def test_minimal_payload_shape(self) -> None:
        event = create_tool_call_started_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            tool_name="filesystem.read",
            args_hash=content_hash('{"path": "/etc/hosts"}'),
        )
        assert event.type == "tool.call.started"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_123"
        assert event.data["tool_name"] == "filesystem.read"
        assert _ULID_PATTERN.match(event.data["call_id"]) is not None
        assert event.data["args_hash"].startswith("sha256:")
        assert "args_preview" not in event.data
        assert "session_id" not in event.data

    def test_correlation_fields_appear_only_when_provided(self) -> None:
        event = create_tool_call_started_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            tool_name="filesystem.read",
            args_hash="sha256:abc",
            session_id="sess_x",
            execution_id="exec_123",
        )
        assert event.data["session_id"] == "sess_x"
        assert event.data["execution_id"] == "exec_123"
        assert "lineage_id" not in event.data

    def test_target_invariant_enforced(self) -> None:
        with pytest.raises(ValueError):
            create_tool_call_started_event(
                target_type="",
                target_id="x",
                call_id=new_call_id(),
                tool_name="t",
                args_hash="sha256:x",
            )
        with pytest.raises(ValueError):
            create_tool_call_started_event(
                target_type="execution",
                target_id="",
                call_id=new_call_id(),
                tool_name="t",
                args_hash="sha256:x",
            )


class TestToolCallReturnedFactory:
    def test_minimal_payload_shape(self) -> None:
        event = create_tool_call_returned_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            tool_name="filesystem.read",
            duration_ms=12,
            is_error=False,
            result_hash="sha256:def",
        )
        assert event.type == "tool.call.returned"
        assert _ULID_PATTERN.match(event.data["call_id"]) is not None
        assert event.data["duration_ms"] == 12
        assert event.data["is_error"] is False
        assert "error_kind" not in event.data

    def test_pairs_with_started_via_call_id(self) -> None:
        call_id = new_call_id()
        started = create_tool_call_started_event(
            target_type="execution",
            target_id="exec_123",
            call_id=call_id,
            tool_name="t",
            args_hash="sha256:x",
        )
        returned = create_tool_call_returned_event(
            target_type="execution",
            target_id="exec_123",
            call_id=call_id,
            tool_name="t",
            duration_ms=1,
            is_error=False,
        )
        assert started.data["call_id"] == returned.data["call_id"]


class TestLLMCallRequestedFactory:
    def test_minimal_payload_shape(self) -> None:
        event = create_llm_call_requested_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            model_id="claude-opus-4",
            prompt_hash="sha256:p",
        )
        assert event.type == "llm.call.requested"
        assert event.data["model_id"] == "claude-opus-4"
        assert event.data["prompt_hash"] == "sha256:p"
        assert "prompt_preview" not in event.data
        assert "max_tokens" not in event.data

    def test_optional_fields_propagate(self) -> None:
        event = create_llm_call_requested_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            model_id="claude-opus-4",
            prompt_hash="sha256:p",
            prompt_preview="hello",
            max_tokens=2048,
            temperature=0.0,
            tool_choice="auto",
            caller="evaluator",
        )
        assert event.data["prompt_preview"] == "hello"
        assert event.data["max_tokens"] == 2048
        assert event.data["temperature"] == 0.0
        assert event.data["tool_choice"] == "auto"
        assert event.data["caller"] == "evaluator"


class TestLLMCallReturnedFactory:
    def test_minimal_payload_shape(self) -> None:
        event = create_llm_call_returned_event(
            target_type="execution",
            target_id="exec_123",
            call_id=new_call_id(),
            model_id="claude-opus-4",
            prompt_hash="sha256:p",
            duration_ms=512,
            is_error=False,
            finish_reason="stop",
            token_count_in=120,
            token_count_out=80,
        )
        assert event.type == "llm.call.returned"
        assert event.data["finish_reason"] == "stop"
        assert event.data["token_count_in"] == 120
        assert event.data["token_count_out"] == 80
        assert event.data["is_error"] is False

    def test_provider_finish_reason_passed_through_unchanged(self) -> None:
        # Provider-specific vocabulary is preserved verbatim; this PR
        # does not normalise across providers.
        for raw in ("stop", "length", "tool_calls", "content_filter", "end_turn"):
            event = create_llm_call_returned_event(
                target_type="execution",
                target_id="exec_123",
                call_id=new_call_id(),
                model_id="x",
                prompt_hash="sha256:p",
                duration_ms=1,
                is_error=False,
                finish_reason=raw,
            )
            assert event.data["finish_reason"] == raw


# ---------------------------------------------------------------------------
# Cross-cutting privacy integration
# ---------------------------------------------------------------------------


def test_shape_preview_can_be_used_by_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a caller can shape the preview and pass it to the factory."""
    monkeypatch.setenv(PRIVACY_ENV_VAR, "redacted")
    text = "secret contents"
    event = create_llm_call_requested_event(
        target_type="execution",
        target_id="exec_123",
        call_id=new_call_id(),
        model_id="claude-opus-4",
        prompt_hash=content_hash(text),
        prompt_preview=text,
    )
    assert event.data["prompt_preview"] == REDACTION_MARKER_TEMPLATE.format(length=len(text))
    assert event.data["prompt_hash"].startswith("sha256:")


def test_factory_applies_privacy_and_preview_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factories enforce the journal privacy policy at the boundary."""
    monkeypatch.setenv(PRIVACY_ENV_VAR, "off")
    event = create_llm_call_requested_event(
        target_type="execution",
        target_id="exec_123",
        call_id=new_call_id(),
        model_id="claude-opus-4",
        prompt_hash="sha256:p",
        prompt_preview="secret prompt",
    )
    assert "prompt_preview" not in event.data

    monkeypatch.setenv(PRIVACY_ENV_VAR, "on")
    long_preview = "x" * 10_000
    event = create_llm_call_requested_event(
        target_type="execution",
        target_id="exec_123",
        call_id=new_call_id(),
        model_id="claude-opus-4",
        prompt_hash="sha256:p",
        prompt_preview=long_preview,
    )
    assert event.data["prompt_preview"].startswith("x" * 256)
    assert "truncated len=9744" in event.data["prompt_preview"]


def test_factory_rejects_malformed_call_id() -> None:
    with pytest.raises(ValueError, match="call_id"):
        create_llm_call_requested_event(
            target_type="execution",
            target_id="exec_123",
            call_id="not-a-ulid",
            model_id="claude-opus-4",
            prompt_hash="sha256:p",
        )


# Strip a leftover env var from earlier tests so process-wide state stays
# clean for downstream test files that depend on the default privacy mode.
def teardown_module() -> None:
    os.environ.pop(PRIVACY_ENV_VAR, None)
