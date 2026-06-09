from __future__ import annotations

import pytest

from ouroboros.providers.gjc_rpc_protocol import (
    GjcCommandError,
    GjcProtocolError,
    UnsupportedGjcRpcFrame,
    is_passive_lifecycle_event,
    unsupported_frame_error,
    validate_response_ack,
    validate_supported_event_correlation,
)


def test_classifier_supported_passive_unsupported_unknown() -> None:
    assert unsupported_frame_error({"type": "agent_end"}) is None
    assert unsupported_frame_error({"type": "ready"}) is None
    assert is_passive_lifecycle_event({"type": "tool_execution_start"}) is True

    unsupported = unsupported_frame_error({"type": "workflow_gate", "id": "g1"})
    assert isinstance(unsupported, UnsupportedGjcRpcFrame)
    assert unsupported.details == {"frame_type": "workflow_gate", "id": "g1"}

    unknown = unsupported_frame_error({"type": "unknown"})
    assert isinstance(unknown, UnsupportedGjcRpcFrame)
    assert unknown.details == {"frame_type": "unknown", "id": None}


def test_supported_event_correlation_accepts_absent_or_prompt_id_and_rejects_unrelated() -> None:
    validate_supported_event_correlation({"type": "message_update"}, prompt_id="prompt-1")
    validate_supported_event_correlation(
        {"type": "message_update", "id": "prompt-1"}, prompt_id="prompt-1"
    )

    with pytest.raises(GjcProtocolError):
        validate_supported_event_correlation(
            {"type": "message_update", "id": "other"}, prompt_id="prompt-1"
        )


def test_supported_event_correlation_does_not_mask_unsupported_frames() -> None:
    event = {"type": "host_tool_call", "id": "other"}
    assert isinstance(unsupported_frame_error(event), UnsupportedGjcRpcFrame)


def test_validate_response_ack_rejects_wrong_command_and_failed_ack() -> None:
    validate_response_ack(
        {"type": "response", "id": "cmd-1", "command": "prompt", "success": True},
        command_id="cmd-1",
        command="prompt",
    )

    with pytest.raises(GjcProtocolError):
        validate_response_ack(
            {"type": "response", "id": "cmd-1", "command": "set_model", "success": True},
            command_id="cmd-1",
            command="prompt",
        )
    with pytest.raises(GjcProtocolError):
        validate_response_ack(
            {"type": "response", "id": "other", "command": "prompt", "success": True},
            command_id="cmd-1",
            command="prompt",
        )
    with pytest.raises(GjcCommandError):
        validate_response_ack(
            {
                "type": "response",
                "id": "cmd-1",
                "command": "prompt",
                "success": False,
                "error": "bad",
            },
            command_id="cmd-1",
            command="prompt",
        )
