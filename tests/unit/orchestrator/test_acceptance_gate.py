"""Whether a fat-harness leaf rejection decides (orchestrator/evidence/acceptance_gate.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.orchestrator.evidence.acceptance_gate import (
    split_leaf_rejection,
    transcript_verifier_mode,
    verifier_mode_fields,
)

VERIFIER = "Fat-harness verifier failed (fabrication_suspected)."
VERIFY_GATE = "Verify gate failed: exit 1"


def _gate(*, passed: bool, unverifiable: bool = False) -> SimpleNamespace:
    return SimpleNamespace(passed=passed, environment_unverifiable=unverifiable)


@pytest.mark.parametrize("gated", [False, True])
def test_record_mode_records_a_verifier_rejection_whatever_the_gate(gated: bool) -> None:
    assert split_leaf_rejection(
        VERIFIER, mode="record", verify_gate_outcome=_gate(passed=True), gated=gated
    ) == (None, VERIFIER, None)
    assert split_leaf_rejection(VERIFIER, mode="record", verify_gate_outcome=None, gated=gated) == (
        None,
        VERIFIER,
        None,
    )


def test_a_failing_verify_command_decides_in_record_mode_too() -> None:
    """The Seed's own verify_command is not the verifier's judgement: it still decides."""
    assert split_leaf_rejection(
        VERIFY_GATE, mode="record", verify_gate_outcome=_gate(passed=False), gated=False
    ) == (VERIFY_GATE, None, None)
    # With the check package gate installed it is the advisory legacy rejection, as before.
    assert split_leaf_rejection(
        VERIFY_GATE, mode="record", verify_gate_outcome=_gate(passed=False), gated=True
    ) == (None, None, VERIFY_GATE)


def test_decide_mode_is_the_behaviour_before_record_mode() -> None:
    assert split_leaf_rejection(VERIFIER, mode="decide", verify_gate_outcome=None, gated=False) == (
        VERIFIER,
        None,
        None,
    )
    assert split_leaf_rejection(VERIFIER, mode="decide", verify_gate_outcome=None, gated=True) == (
        None,
        None,
        VERIFIER,
    )


@pytest.mark.parametrize("mode", ["decide", "record"])
def test_no_rejection_passes_through(mode: str) -> None:
    assert split_leaf_rejection(
        None,
        mode=mode,  # type: ignore[arg-type]
        verify_gate_outcome=None,
        gated=True,
    ) == (None, None, None)


def test_event_fields_say_whether_the_verifier_decided() -> None:
    assert verifier_mode_fields("record", enforced=True, would_have_rejected=VERIFIER) == {
        "enforced": False,
        "transcript_verifier": "record",
        "would_have_rejected": VERIFIER,
    }
    assert verifier_mode_fields("decide", enforced=True, would_have_rejected=None)["enforced"]


def test_mode_is_closed() -> None:
    assert transcript_verifier_mode("record") == "record"
    with pytest.raises(ValueError, match="transcript_verifier"):
        transcript_verifier_mode("on")
