"""Unit tests for ouroboros.events.base module."""

from datetime import UTC, datetime

from ouroboros.events.base import BaseEvent, sanitize_event_data_for_persistence


class TestBaseEventConstruction:
    """Test BaseEvent construction."""

    def test_base_event_is_frozen(self) -> None:
        """BaseEvent is immutable (frozen)."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        # Attempting to modify should raise an error
        try:
            event.type = "modified"  # type: ignore[misc]
            raise AssertionError("Should have raised an error")
        except Exception:
            pass  # Expected - frozen model

    def test_base_event_auto_generates_id(self) -> None:
        """BaseEvent generates UUID for id if not provided."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        assert event.id is not None
        assert len(event.id) == 36  # UUID length

    def test_base_event_auto_generates_timestamp(self) -> None:
        """BaseEvent generates timestamp if not provided."""
        before = datetime.now(UTC)
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        after = datetime.now(UTC)

        assert event.timestamp is not None
        assert before <= event.timestamp <= after

    def test_base_event_default_empty_data(self) -> None:
        """BaseEvent defaults to empty data dict."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        assert event.data == {}

    def test_base_event_stores_data(self) -> None:
        """BaseEvent stores provided data."""
        data = {"key": "value", "count": 42}
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data=data,
        )
        assert event.data == data


class TestBaseEventNaming:
    """Test event type naming convention per AC6."""

    def test_event_type_dot_notation(self) -> None:
        """Event type follows dot.notation.past_tense convention."""
        event = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
        )
        assert "." in event.type
        parts = event.type.split(".")
        assert len(parts) >= 3  # domain.entity.verb


class TestBaseEventSerialization:
    """Test BaseEvent serialization for database."""

    def test_to_db_dict_includes_all_fields(self) -> None:
        """to_db_dict() returns all required database columns."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"key": "value"},
        )

        db_dict = event.to_db_dict()

        assert "id" in db_dict
        assert "event_type" in db_dict
        assert "timestamp" in db_dict
        assert "aggregate_type" in db_dict
        assert "aggregate_id" in db_dict
        assert "payload" in db_dict
        assert "consensus_id" in db_dict

    def test_to_db_dict_maps_type_to_event_type(self) -> None:
        """to_db_dict() maps 'type' to 'event_type' column."""
        event = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
        )

        db_dict = event.to_db_dict()
        assert db_dict["event_type"] == "ontology.concept.added"

    def test_to_db_dict_maps_data_to_payload(self) -> None:
        """to_db_dict() maps 'data' to 'payload' column."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"key": "value"},
        )

        db_dict = event.to_db_dict()
        assert db_dict["payload"] == {"key": "value", "event_version": 1}

    def test_to_db_dict_excludes_raw_subscribed_payloads(self) -> None:
        """Raw subscribed runtime payloads are stripped before persistence."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={
                "progress": {
                    "messages_processed": 4,
                    "runtime": {
                        "backend": "opencode",
                        "native_session_id": "sess-123",
                        "metadata": {
                            "resume_token": "resume-123",
                            "raw_subscribed_event": {"type": "session.updated"},
                            "subscribed_event_payload": {"delta": "keep out"},
                        },
                    },
                    "subscribed_events": [{"type": "tool.started"}],
                }
            },
        )

        db_dict = event.to_db_dict()

        assert db_dict["payload"] == {
            "progress": {
                "messages_processed": 4,
                "runtime": {
                    "backend": "opencode",
                    "native_session_id": "sess-123",
                    "metadata": {
                        "resume_token": "resume-123",
                    },
                },
            },
            "event_version": 1,
        }

    def test_to_db_dict_excludes_raw_subscribed_payloads_inside_tuples(self) -> None:
        """Tuple-backed payloads should be normalized before persistence."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={
                "progress": (
                    {
                        "messages_processed": 1,
                        "raw_event": {"type": "assistant.message.delta"},
                    },
                    {
                        "runtime": {
                            "backend": "opencode",
                            "metadata": {
                                "resume_token": "resume-123",
                                "subscribed_events": [{"type": "tool.started"}],
                            },
                        }
                    },
                )
            },
        )

        db_dict = event.to_db_dict()

        assert db_dict["payload"] == {
            "progress": [
                {
                    "messages_processed": 1,
                },
                {
                    "runtime": {
                        "backend": "opencode",
                        "metadata": {
                            "resume_token": "resume-123",
                        },
                    }
                },
            ],
            "event_version": 1,
        }

    def test_from_db_row_reconstructs_event(self) -> None:
        """from_db_row() reconstructs event from database row."""
        row = {
            "id": "event-123",
            "event_type": "test.event.created",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "test",
            "aggregate_id": "test-456",
            "payload": {"key": "value"},
        }

        event = BaseEvent.from_db_row(row)

        assert event.id == "event-123"
        assert event.type == "test.event.created"
        assert event.aggregate_type == "test"
        assert event.aggregate_id == "test-456"
        assert event.data == {"key": "value"}

    def test_roundtrip_serialization(self) -> None:
        """Event survives roundtrip through to_db_dict and from_db_row."""
        original = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
            data={"concept_name": "auth", "weight": 1.5},
        )

        db_dict = original.to_db_dict()
        # Simulate what DB would return
        db_row = {
            "id": db_dict["id"],
            "event_type": db_dict["event_type"],
            "timestamp": db_dict["timestamp"],
            "aggregate_type": db_dict["aggregate_type"],
            "aggregate_id": db_dict["aggregate_id"],
            "payload": db_dict["payload"],
        }

        restored = BaseEvent.from_db_row(db_row)

        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.aggregate_type == original.aggregate_type
        assert restored.aggregate_id == original.aggregate_id
        assert restored.data == original.data
        assert restored.event_version == original.event_version


class TestBaseEventVersion:
    """Test event_version lifecycle."""

    def test_new_events_default_to_version_1(self) -> None:
        """Newly created events have event_version=1."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        assert event.event_version == 1

    def test_to_db_dict_injects_event_version_into_payload(self) -> None:
        """to_db_dict() writes event_version inside the payload JSON."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"key": "value"},
        )
        db_dict = event.to_db_dict()
        assert db_dict["payload"]["event_version"] == 1

    def test_legacy_rows_without_event_version_default_to_0(self) -> None:
        """DB rows written before this feature deserialize as version 0."""
        row = {
            "id": "legacy-123",
            "event_type": "test.event.created",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "test",
            "aggregate_id": "test-456",
            "payload": {"key": "value"},
        }
        event = BaseEvent.from_db_row(row)
        assert event.event_version == 0
        assert event.data == {"key": "value"}

    def test_event_version_stripped_from_data_on_deserialization(self) -> None:
        """event_version does not leak into the data dict after from_db_row."""
        row = {
            "id": "ver-123",
            "event_type": "test.event.created",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "test",
            "aggregate_id": "test-456",
            "payload": {"key": "value", "event_version": 1},
        }
        event = BaseEvent.from_db_row(row)
        assert "event_version" not in event.data
        assert event.event_version == 1

    def test_roundtrip_preserves_event_version(self) -> None:
        """event_version survives a to_db_dict -> from_db_row roundtrip."""
        original = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"concept": "auth"},
        )
        db_dict = original.to_db_dict()
        db_row = {
            "id": db_dict["id"],
            "event_type": db_dict["event_type"],
            "timestamp": db_dict["timestamp"],
            "aggregate_type": db_dict["aggregate_type"],
            "aggregate_id": db_dict["aggregate_id"],
            "payload": db_dict["payload"],
        }
        restored = BaseEvent.from_db_row(db_row)
        assert restored.event_version == 1
        assert restored.data == {"concept": "auth"}
        assert "event_version" not in restored.data

    def test_non_int_event_version_defaults_to_0(self) -> None:
        """Corrupted event_version values fall back to 0."""
        row = {
            "id": "corrupt-123",
            "event_type": "test.event.created",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "test",
            "aggregate_id": "test-456",
            "payload": {"key": "value", "event_version": "invalid"},
        }
        event = BaseEvent.from_db_row(row)
        assert event.event_version == 0


def test_persistence_sanitizer_redacts_credentials_without_dropping_protocol_keys() -> None:
    """Persistence redacts credentials but preserves replay and dedupe keys."""
    secret = "ghp_" + "a" * 36
    generic_secret = "a" * 14 + "123456"
    payload = {
        "runtime_backend": "\u034f" + secret,
        "nested": {"label": "\u200b" + secret},
        "detail": f"retry with token {generic_secret}",
        secret: "not-a-secret-value",
        "api_key": "unprefixed-provider-credential-1234567890",
        "apiKeyValue": "opaque-provider-credential-1234567890",
        "accessTokenValue": "opaque-provider-access-token-1234567890",
        "auth": "opaque-provider-authentication-1234567890",
        "authentication": "opaque-provider-authentication-1234567890",
        "semantic_ac_key": "ac_123",
        "idempotency_key": "turn-1",
        "correlation_key": "lane-1",
        "safe": "claude",
    }

    sanitized = sanitize_event_data_for_persistence(payload)
    persisted = BaseEvent(
        type="test.event.created",
        aggregate_type="test",
        aggregate_id="event-sanitization",
        data=payload,
    ).to_db_dict()["payload"]

    assert sanitized["runtime_backend"] == "<REDACTED>"
    assert sanitized["nested"]["label"] == "<REDACTED>"
    assert sanitized["detail"] == "retry with token [redacted]"
    assert secret not in sanitized
    assert sanitized["api_key"] == "<REDACTED>"
    assert sanitized["apiKeyValue"] == "<REDACTED>"
    assert sanitized["accessTokenValue"] == "<REDACTED>"
    assert sanitized["auth"] == "<REDACTED>"
    assert sanitized["authentication"] == "<REDACTED>"
    assert sanitized["semantic_ac_key"] == "ac_123"
    assert sanitized["idempotency_key"] == "turn-1"
    assert sanitized["correlation_key"] == "lane-1"
    assert persisted["api_key"] == "<REDACTED>"
    assert persisted["apiKeyValue"] == "<REDACTED>"
    assert persisted["accessTokenValue"] == "<REDACTED>"
    assert persisted["auth"] == "<REDACTED>"
    assert persisted["authentication"] == "<REDACTED>"
    assert persisted["semantic_ac_key"] == "ac_123"
    assert persisted["idempotency_key"] == "turn-1"
    assert persisted["correlation_key"] == "lane-1"
    assert "unprefixed-provider-credential-1234567890" not in str(persisted)
    assert secret not in str(sanitized)
    assert secret not in str(persisted)
    assert generic_secret not in str(sanitized)
    assert generic_secret not in str(persisted)


def test_persistence_sanitizer_preserves_benign_security_terms() -> None:
    """Natural-language data must not be mistaken for credential metadata."""
    payload = {
        "goal": "Implement token budget accounting",
        "criterion": "Adopt an API-first design",
        "hyphenated_criterion": "Adopt API-first-design-for-internal-services",
        "direct_goal": "token budget accounting",
        "direct_criterion": "API-first design",
        "authentication_note": "PK-based authentication should remain durable",
        "workflow": "secret_rotation_workflow",
    }

    sanitized = sanitize_event_data_for_persistence(payload)
    persisted = BaseEvent(
        type="test.event.created",
        aggregate_type="test",
        aggregate_id="event-semantic-text",
        data=payload,
    ).to_db_dict()["payload"]

    assert sanitized == payload
    assert persisted["goal"] == payload["goal"]
    assert persisted["criterion"] == payload["criterion"]
    assert persisted["hyphenated_criterion"] == payload["hyphenated_criterion"]
    assert persisted["direct_goal"] == payload["direct_goal"]
    assert persisted["direct_criterion"] == payload["direct_criterion"]
    assert persisted["authentication_note"] == payload["authentication_note"]
    assert persisted["workflow"] == payload["workflow"]


def test_persistence_sanitizer_redacts_embedded_stripe_and_pem_credentials() -> None:
    """Benign keys cannot allow common credential formats into durable events."""
    stripe = "sk_live_" + "a" * 24
    restricted = "rk_live_" + "b" * 24
    bearer = "c" * 32
    pem = "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----"
    payload = {
        "detail": f"retry with {stripe}",
        "restricted_detail": f"retry with {restricted}",
        "certificate": pem,
        "bearer": bearer,
    }

    persisted = BaseEvent(
        type="test.event.created",
        aggregate_type="test",
        aggregate_id="event-credential-shapes",
        data=payload,
    ).to_db_dict()["payload"]

    assert stripe not in persisted
    assert restricted not in persisted
    assert bearer not in persisted
    assert pem not in persisted
    assert persisted["detail"] == "retry with [redacted]"
    assert persisted["restricted_detail"] == "retry with [redacted]"
    assert persisted["certificate"] == "[redacted]"
    assert persisted["bearer"] == "<REDACTED>"
