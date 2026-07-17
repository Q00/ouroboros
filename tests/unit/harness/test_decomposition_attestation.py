"""Tests for the gate-anchored decomposition trust attestation judge.

The judge is pure and deterministic: every test builds synthetic
:class:`SiblingVerifyOutcome`/:class:`ParentVerifyOutcome` inputs (no real
subprocess execution, no LLM calls) and asserts the resulting
:class:`DecompositionAttestation`.
"""

from __future__ import annotations

import pytest

from ouroboros.harness.decomposition_attestation import (
    DecompositionTrustAxis,
    DecompositionTrustVerdict,
    ParentVerifyOutcome,
    SiblingVerifyOutcome,
    attest_decomposition,
)


def _sibling(
    sibling_id: str = "0",
    *,
    has_verify_command: bool = True,
    passed: bool | None = True,
    reason: str = "",
) -> SiblingVerifyOutcome:
    return SiblingVerifyOutcome(
        sibling_id=sibling_id,
        has_verify_command=has_verify_command,
        passed=passed,
        reason=reason,
    )


def _parent(
    *,
    has_verify_command: bool = True,
    passed: bool | None = True,
    reason: str = "",
) -> ParentVerifyOutcome:
    return ParentVerifyOutcome(
        has_verify_command=has_verify_command,
        passed=passed,
        reason=reason,
    )


class TestAllChildrenPassAndParentGatePasses:
    def test_trustworthy(self) -> None:
        attestation = attest_decomposition(
            node_id="root:0",
            siblings=(_sibling("0"), _sibling("1"), _sibling("2")),
            parent=_parent(),
        )

        assert attestation.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        assert attestation.trustworthy is True
        assert attestation.failed_axis is None
        assert attestation.failed_sibling_id is None
        assert attestation.node_id == "root:0"

    def test_event_data_serialization(self) -> None:
        attestation = attest_decomposition(
            node_id="root:0",
            siblings=(_sibling("0"),),
            parent=_parent(),
        )
        data = attestation.to_event_data()
        assert data == {
            "node_id": "root:0",
            "verdict": "trustworthy",
            "trustworthy": True,
            "failed_axis": None,
            "failed_sibling_id": None,
            "reason": attestation.reason,
        }


class TestSiblingFailsOwnGate:
    def test_not_trustworthy(self) -> None:
        attestation = attest_decomposition(
            node_id="root:1",
            siblings=(
                _sibling("0", passed=True),
                _sibling("1", passed=False, reason="verify_command exited with status 1"),
                _sibling("2", passed=True),
            ),
            parent=_parent(),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "1"
        assert "verify_command exited with status 1" in attestation.reason

    def test_first_failing_sibling_is_attributed(self) -> None:
        """Deterministic: the FIRST failing sibling (in input order) is blamed."""
        attestation = attest_decomposition(
            node_id="root:1",
            siblings=(
                _sibling("0", passed=False, reason="first failure"),
                _sibling("1", passed=False, reason="second failure"),
            ),
            parent=_parent(),
        )

        assert attestation.failed_sibling_id == "0"
        assert "first failure" in attestation.reason


class TestParentGateFailsAfterChildrenSucceed:
    """The clobbering scenario: every child individually succeeded, but the
    parent's own re-run gate fails — a sibling overwrote another's work or
    left a gap the parent's contract cares about."""

    def test_not_trustworthy(self) -> None:
        attestation = attest_decomposition(
            node_id="root:2",
            siblings=(_sibling("0"), _sibling("1")),
            parent=_parent(passed=False, reason="expected_artifacts missing: out/report.json"),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.PARENT_GATE
        assert attestation.failed_sibling_id is None
        assert "expected_artifacts missing" in attestation.reason


class TestNoVerifyCommandOnParent:
    def test_indeterminate_and_not_trustworthy(self) -> None:
        attestation = attest_decomposition(
            node_id="root:3",
            siblings=(_sibling("0"), _sibling("1")),
            parent=_parent(has_verify_command=False, passed=None),
        )

        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.PARENT_GATE


class TestNoVerifyCommandOnSibling:
    """Fail-closed applies symmetrically: an unproven sibling axis is never
    read as a passing one, even though this is the common case in a codebase
    where decomposed children don't yet carry structured verify contracts."""

    def test_indeterminate_and_not_trustworthy(self) -> None:
        attestation = attest_decomposition(
            node_id="root:4",
            siblings=(_sibling("0", has_verify_command=False, passed=None), _sibling("1")),
            parent=_parent(),
        )

        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"


class TestNoSiblingsRecorded:
    def test_indeterminate(self) -> None:
        attestation = attest_decomposition(node_id="root:5", siblings=(), parent=_parent())

        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False


class TestConstructionInvariants:
    def test_sibling_rejects_passed_without_verify_command(self) -> None:
        with pytest.raises(ValueError, match="has_verify_command"):
            SiblingVerifyOutcome(sibling_id="0", has_verify_command=False, passed=True)

    def test_parent_rejects_passed_without_verify_command(self) -> None:
        with pytest.raises(ValueError, match="has_verify_command"):
            ParentVerifyOutcome(has_verify_command=False, passed=False)
