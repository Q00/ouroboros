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


class TestEvaluatedFailureBeatsIndeterminacy:
    """Evaluate-all-then-prioritize-failure ordering: an axis that actually
    ran and FAILED must win the verdict as UNTRUSTWORTHY, never be masked by
    an earlier axis's mere absence of evidence."""

    def test_parent_failure_wins_over_indeterminate_sibling(self) -> None:
        """The exact masking bug this reorder fixes: a sibling with no
        evaluable gate used to short-circuit the scan BEFORE the parent's
        real, evaluated failure was ever considered."""
        attestation = attest_decomposition(
            node_id="root:6",
            siblings=(_sibling("0", has_verify_command=False, passed=None), _sibling("1")),
            parent=_parent(passed=False, reason="expected_artifacts missing: out.json"),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_axis is DecompositionTrustAxis.PARENT_GATE
        assert attestation.failed_sibling_id is None

    def test_later_sibling_failure_wins_over_earlier_indeterminate_sibling(self) -> None:
        attestation = attest_decomposition(
            node_id="root:7",
            siblings=(
                _sibling("0", has_verify_command=False, passed=None),
                _sibling("1", passed=False, reason="real failure"),
            ),
            parent=_parent(has_verify_command=False, passed=None),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "1"

    def test_sibling_failure_attributed_before_parent_failure(self) -> None:
        attestation = attest_decomposition(
            node_id="root:8",
            siblings=(_sibling("0", passed=False, reason="sibling failed"),),
            parent=_parent(passed=False, reason="parent failed too"),
        )

        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"

    def test_empty_siblings_with_failing_parent_is_untrustworthy(self) -> None:
        """Even with no sibling evidence at all, a parent gate that ran and
        failed is real negative evidence -- tightened from the previous
        INDETERMINATE early return."""
        attestation = attest_decomposition(
            node_id="root:9",
            siblings=(),
            parent=_parent(passed=False, reason="parent failed"),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_axis is DecompositionTrustAxis.PARENT_GATE


class TestReorderNeverMintsNewTrustworthy:
    """Safety invariant 5: relative to the previous first-indeterminate-
    returns-early scan, the reorder may only tighten INDETERMINATE to
    UNTRUSTWORTHY when real failing evidence exists. TRUSTWORTHY still
    requires every axis evaluated AND passed, so no axis-evaluation
    permutation containing an indeterminate or failing axis may yield
    TRUSTWORTHY."""

    _SIBLING_STATES: tuple[tuple[bool, bool | None], ...] = (
        (True, True),  # evaluated, passed
        (True, False),  # evaluated, failed
        (False, None),  # unevaluable
    )

    @pytest.mark.parametrize("first", _SIBLING_STATES, ids=["pass", "fail", "none"])
    @pytest.mark.parametrize("second", _SIBLING_STATES, ids=["pass", "fail", "none"])
    @pytest.mark.parametrize("parent_state", _SIBLING_STATES, ids=["pass", "fail", "none"])
    def test_trustworthy_requires_all_axes_evaluated_and_passed(
        self,
        first: tuple[bool, bool | None],
        second: tuple[bool, bool | None],
        parent_state: tuple[bool, bool | None],
    ) -> None:
        attestation = attest_decomposition(
            node_id="root:perm",
            siblings=(
                _sibling("0", has_verify_command=first[0], passed=first[1]),
                _sibling("1", has_verify_command=second[0], passed=second[1]),
            ),
            parent=_parent(has_verify_command=parent_state[0], passed=parent_state[1]),
        )

        all_passed = all(state == (True, True) for state in (first, second, parent_state))
        any_failed = any(state == (True, False) for state in (first, second, parent_state))

        if all_passed:
            # The ONLY permutation that is trustworthy -- identical to the
            # condition under the pre-reorder logic.
            assert attestation.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        elif any_failed:
            # Real evaluated failure always wins (this is where the reorder
            # may TIGHTEN a previously-INDETERMINATE verdict).
            assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        else:
            assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        # Never trustworthy unless everything was evaluated and passed.
        assert attestation.trustworthy is (all_passed is True)
