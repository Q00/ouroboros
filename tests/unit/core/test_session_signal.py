"""Unit tests for the clean-room Ouroboros Synapse contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.core import SessionSignal as CoreSessionSignal
from ouroboros.core.session_signal import (
    MAX_MESSAGE_BYTES,
    MAX_REPLY_BYTES,
    SESSION_SIGNAL_SCHEMA_VERSION,
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalCapabilityError,
    SessionSignalContractEffect,
    SessionSignalMode,
    SessionSignalSource,
    bounded_session_signal_reply,
    resolve_session_signal_mode,
)


def _signal(**overrides: object) -> SessionSignal:
    values: dict[str, object] = {
        "signal_id": "sig_1",
        "target_session_scope_id": "exec_1_ac_2",
        "target_session_attempt_id": "exec_1_ac_2_attempt_1",
        "expected_execution_id": "exec_1",
        "mode": SessionSignalMode.REDIRECT,
        "fallback_mode": SessionSignalMode.AFTER_TURN,
        "message": "Preserve the approved AC and use the clarified interaction.",
        "source": SessionSignalSource.USER,
        "reason": "The user clarified implementation intent.",
        "idempotency_key": "turn_7_ac_2",
    }
    values.update(overrides)
    return SessionSignal(**values)  # type: ignore[arg-type]


class TestSessionSignalValidation:
    def test_minimal_signal_has_stable_digest_and_identity(self) -> None:
        signal = _signal()

        assert signal.schema_version == SESSION_SIGNAL_SCHEMA_VERSION
        assert len(signal.message_digest) == 64
        assert signal.effective_idempotency_key == (
            "exec_1",
            "exec_1_ac_2",
            "exec_1_ac_2_attempt_1",
            "turn_7_ac_2",
        )

    @pytest.mark.parametrize(
        "field",
        [
            "signal_id",
            "target_session_scope_id",
            "target_session_attempt_id",
            "expected_execution_id",
            "message",
            "reason",
            "idempotency_key",
        ],
    )
    def test_blank_required_text_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _signal(**{field: " \n\t "})

    def test_control_only_text_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="visible text"):
            _signal(message="\u0000\u0001")

    def test_utf8_byte_bound_is_enforced(self) -> None:
        oversized = "€" * ((MAX_MESSAGE_BYTES // 3) + 1)

        with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
            _signal(message=oversized)

    @pytest.mark.parametrize(
        "message",
        [
            "Use Bearer abcdefghijklmnopqrstuvwxyz for the request",
            "Use sk-abcdefghijklmnopqrstuvwxyz123456",
            "password=super-secret-value",
        ],
    )
    def test_secret_shaped_content_is_rejected(self, message: str) -> None:
        with pytest.raises(ValueError, match="secret-shaped"):
            _signal(message=message)

    def test_only_redirect_accepts_after_turn_fallback(self) -> None:
        with pytest.raises(ValueError, match="valid only for redirect"):
            _signal(mode=SessionSignalMode.INFORM, fallback_mode=SessionSignalMode.AFTER_TURN)

        with pytest.raises(ValueError, match="must be after_turn"):
            _signal(mode=SessionSignalMode.REDIRECT, fallback_mode=SessionSignalMode.INFORM)

    def test_replace_requires_user_approval(self) -> None:
        with pytest.raises(ValueError, match="user_approval_event_id"):
            _signal(mode=SessionSignalMode.REPLACE, fallback_mode=None)

        approved = _signal(
            mode=SessionSignalMode.REPLACE,
            fallback_mode=None,
            user_approval_event_id="hitl_evt_1",
        )
        assert approved.user_approval_event_id == "hitl_evt_1"

    def test_specification_change_requires_approval_and_round_trips_event_data(self) -> None:
        with pytest.raises(ValueError, match="specification_change"):
            _signal(contract_effect=SessionSignalContractEffect.SPECIFICATION_CHANGE)

        signal = _signal(
            contract_effect=SessionSignalContractEffect.SPECIFICATION_CHANGE,
            user_approval_event_id="approval_1",
        )
        restored = SessionSignal.from_event_data(signal.to_event_data(include_message=True))

        assert restored == signal

    def test_runtime_reply_is_secret_filtered_and_utf8_bounded(self) -> None:
        assert bounded_session_signal_reply("password=super-secret-value") == (
            "[Reply omitted because it contained secret-shaped content.]"
        )
        reply = bounded_session_signal_reply("€" * 1_000)
        assert len(reply.encode("utf-8")) <= MAX_REPLY_BYTES
        assert reply.endswith("…")

    def test_expiry_is_timezone_aware_and_normalized(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _signal(expires_at=datetime(2026, 7, 12, 12, 0, 0))

        expires = datetime.now(UTC) + timedelta(minutes=1)
        signal = _signal(expires_at=expires)
        assert signal.is_expired(at=expires - timedelta(seconds=1)) is False
        assert signal.is_expired(at=expires) is True

    def test_requested_event_data_includes_message_only_when_requested(self) -> None:
        signal = _signal()

        assert "message" not in signal.to_event_data()
        assert signal.to_event_data(include_message=True)["message"] == signal.message


class TestSessionSignalCapabilityResolution:
    def test_all_capabilities_default_to_unsupported(self) -> None:
        capabilities = SessionSignalCapabilities()

        assert capabilities.to_event_data() == {
            "inform_delivery": False,
            "background_reply": False,
            "after_turn_delivery": False,
            "checkpoint_redirect": False,
            "owned_turn_abort": False,
            "replacement_resume": False,
        }

    def test_inform_requires_its_own_capability(self) -> None:
        signal = _signal(mode=SessionSignalMode.INFORM, fallback_mode=None)

        with pytest.raises(SessionSignalCapabilityError, match="inform"):
            resolve_session_signal_mode(signal, SessionSignalCapabilities())

        assert (
            resolve_session_signal_mode(
                signal,
                SessionSignalCapabilities(inform_delivery=True),
            )
            is SessionSignalMode.INFORM
        )

    def test_redirect_is_used_when_supported(self) -> None:
        effective = resolve_session_signal_mode(
            _signal(),
            SessionSignalCapabilities(
                checkpoint_redirect=True,
                after_turn_delivery=True,
            ),
        )

        assert effective is SessionSignalMode.REDIRECT

    def test_redirect_falls_back_only_when_explicit(self) -> None:
        capabilities = SessionSignalCapabilities(after_turn_delivery=True)

        assert resolve_session_signal_mode(_signal(), capabilities) is SessionSignalMode.AFTER_TURN
        with pytest.raises(SessionSignalCapabilityError, match="checkpoint redirect"):
            resolve_session_signal_mode(
                replace(_signal(), fallback_mode=None),
                capabilities,
            )

    def test_after_turn_is_not_inferred_from_targeted_resume(self) -> None:
        signal = _signal(mode=SessionSignalMode.AFTER_TURN, fallback_mode=None)

        with pytest.raises(SessionSignalCapabilityError, match="after_turn"):
            resolve_session_signal_mode(signal, SessionSignalCapabilities())

    def test_replace_requires_abort_and_resume(self) -> None:
        signal = _signal(
            mode=SessionSignalMode.REPLACE,
            fallback_mode=None,
            user_approval_event_id="hitl_evt_1",
        )

        with pytest.raises(SessionSignalCapabilityError, match="abort and resume"):
            resolve_session_signal_mode(
                signal,
                SessionSignalCapabilities(owned_turn_abort=True),
            )

        assert (
            resolve_session_signal_mode(
                signal,
                SessionSignalCapabilities(
                    owned_turn_abort=True,
                    replacement_resume=True,
                ),
            )
            is SessionSignalMode.REPLACE
        )


def test_session_signal_is_reexported_from_core() -> None:
    assert CoreSessionSignal is SessionSignal
