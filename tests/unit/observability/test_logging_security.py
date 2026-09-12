"""Regression tests for security fixes in observability/logging.py.

Covers fail-closed scalar event handling and safe tuple-subclass reconstruction.
"""

from __future__ import annotations

from collections import namedtuple
from enum import StrEnum
from typing import Any

from ouroboros.observability.logging import (
    _mask_sensitive_data,
    _mask_sequence_sensitive_data,
)


class _FakeLogger:
    """Stub logger for structlog processor tests."""


class TestScalarEventStrEnumFailOpen:
    """The 'event' key in structlog must not pass StrEnum/str subclasses unchecked."""

    def test_strenum_credential_in_event_key_is_masked(self) -> None:
        """A StrEnum member carrying a credential in the 'event' key is detected."""

        class EventKind(StrEnum):
            LEAKED = "sk-live-abc123def456ghi789"

        event_dict: dict[str, Any] = {"event": EventKind.LEAKED, "level": "info"}
        result = _mask_sensitive_data(_FakeLogger(), "info", event_dict)
        assert "sk-live-abc123def456ghi789" not in str(result["event"])

    def test_strenum_safe_value_in_event_key_normalized(self) -> None:
        """A StrEnum member with a safe value is normalized to a plain str."""

        class EventKind(StrEnum):
            SAFE = "workflow.started"

        event_dict: dict[str, Any] = {"event": EventKind.SAFE, "level": "info"}
        result = _mask_sensitive_data(_FakeLogger(), "info", event_dict)
        # Value preserved but normalized to plain str
        assert result["event"] == "workflow.started"
        assert type(result["event"]) is str

    def test_arbitrary_object_in_event_key_does_not_invoke_str(self) -> None:
        """An arbitrary object in the 'event' key is redacted without conversion."""

        class HostileObj:
            def __str__(self):
                return "sk-live-leaked-through-str"

            def __repr__(self):
                return "sk-live-leaked-through-repr"

        event_dict: dict[str, Any] = {"event": HostileObj(), "level": "info"}
        result = _mask_sensitive_data(_FakeLogger(), "info", event_dict)

        assert result["event"] == "<REDACTED>"

    def test_hostile_str_subclass_in_event_key(self) -> None:
        """A str subclass with hostile __str__ in event key is handled safely."""

        class HostileStr(str):
            def __str__(self):
                raise RuntimeError("hostile __str__")

        event_dict: dict[str, Any] = {"event": HostileStr("sk-live-hidden"), "level": "info"}
        result = _mask_sensitive_data(_FakeLogger(), "info", event_dict)
        # Must not raise, and must not leak the secret
        assert "sk-live-hidden" not in str(result["event"])

    def test_plain_str_event_passes_through_unchanged(self) -> None:
        """A plain built-in str in 'event' passes through without modification."""
        event_dict: dict[str, Any] = {"event": "ac.execution.started", "level": "info"}
        result = _mask_sensitive_data(_FakeLogger(), "info", event_dict)
        assert result["event"] == "ac.execution.started"

    def test_existing_auth_token_event_names_remain_exact(self) -> None:
        """Structured event identifiers are contracts, not credential values."""
        for event in (
            "mcp.auth.invalid_api_key",
            "mcp.auth.token_valid",
            "context.token_count.failed",
        ):
            result = _mask_sensitive_data(_FakeLogger(), "info", {"event": event})
            assert result["event"] == event


class TestMaskSequenceTupleMakeValidation:
    """_mask_sequence_sensitive_data must validate _make() result."""

    def test_namedtuple_make_preserves_sanitized_content(self) -> None:
        """A well-behaved namedtuple reconstructs with sanitized values."""
        Record = namedtuple("Record", ["key", "value"])
        data = Record(key="safe", value="harmless")
        result = _mask_sequence_sensitive_data(data)
        assert isinstance(result, tuple)
        assert result[0] == "safe"
        assert result[1] == "harmless"

    def test_namedtuple_secret_is_masked(self) -> None:
        """A namedtuple with a credential value gets it masked."""
        Record = namedtuple("Record", ["key", "value"])
        data = Record(key="config", value="sk-live-abc123def456ghi789")
        result = _mask_sequence_sensitive_data(data)
        assert "sk-live-abc123def456ghi789" not in str(result)

    def test_hostile_tuple_subclass_make_returns_original(self) -> None:
        """A tuple subclass whose _make() ignores sanitized input is degraded."""

        original_secret = "sk-live-sneaky-credential"

        class SneakyTuple(tuple):
            @classmethod
            def _make(cls, _iterable):
                # Hostile: returns original unsanitized content
                return cls((original_secret, "safe"))

        data = SneakyTuple((original_secret, "safe"))
        result = _mask_sequence_sensitive_data(data)
        # The secret must be masked
        assert original_secret not in str(result)
        # Degrades to plain tuple
        assert type(result) is tuple

    def test_hostile_tuple_subclass_make_returns_wrong_length(self) -> None:
        """A _make() returning wrong-length tuple is degraded to plain tuple."""

        class WrongLenTuple(tuple):
            @classmethod
            def _make(cls, _iterable):
                # Returns fewer elements than expected
                return cls(("only_one",))

        data = WrongLenTuple(("sk-live-secret123456", "safe"))
        result = _mask_sequence_sensitive_data(data)
        # Must fall back to plain tuple with correct sanitized content
        assert "sk-live-secret123456" not in str(result)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_hostile_tuple_subclass_make_raises(self) -> None:
        """A _make() that raises falls back to plain tuple safely."""

        class BreakingTuple(tuple):
            @classmethod
            def _make(cls, _iterable):
                raise ValueError("broken _make")

        data = BreakingTuple(("sk-live-secretvalue", "safe"))
        result = _mask_sequence_sensitive_data(data)
        assert "sk-live-secretvalue" not in str(result)
        assert isinstance(result, tuple)
        assert type(result) is tuple
