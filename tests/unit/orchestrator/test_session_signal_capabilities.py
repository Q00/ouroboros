"""Runtime capability guards for Ouroboros Synapse."""

from dataclasses import replace

from ouroboros.core.session_signal import SessionSignalCapabilities
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    ClaudeAgentAdapter,
    RuntimeCapabilities,
)


def test_runtime_capabilities_default_synapse_to_unsupported() -> None:
    capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
    )

    assert capabilities.session_signals == SessionSignalCapabilities()
    assert capabilities.targeted_resume is True
    assert capabilities.session_signals.after_turn_delivery is False
    assert capabilities.session_signals.checkpoint_redirect is False


def test_full_capabilities_does_not_claim_synapse_support() -> None:
    assert FULL_CAPABILITIES.session_signals == SessionSignalCapabilities()


def test_runtime_must_explicitly_opt_in() -> None:
    opted_in = replace(
        FULL_CAPABILITIES,
        session_signals=SessionSignalCapabilities(after_turn_delivery=True),
    )

    assert opted_in.session_signals.after_turn_delivery is True
    assert FULL_CAPABILITIES.session_signals.after_turn_delivery is False


def test_claude_sdk_declares_after_turn_delivery() -> None:
    runtime = ClaudeAgentAdapter(cwd="/tmp/project")

    assert runtime.capabilities.targeted_resume is True
    assert runtime.capabilities.session_signals == SessionSignalCapabilities(
        inform_delivery=True,
        background_reply=True,
        after_turn_delivery=True,
    )
