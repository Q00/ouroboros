"""Tests for deterministic semantic-miss guard."""

from __future__ import annotations

import pytest

from ouroboros.harness.claim_term_guard import (
    ClaimTermGuardFact,
    ClaimTermGuardVerdict,
    deterministic_claim_term_guard,
)


def test_accepts_when_structured_statement_terms_are_present_in_evidence() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="file_modified:src/app.py:role_matrix_added",
                evidence_handle="ev_1",
                statement="file_modified path=src/app.py expected_change=role_matrix_added",
                evidence_text="path=src/app.py; scope=whole_file; role_matrix_added",
            ),
        ),
    )

    assert verdict.accepted is True


def test_accepts_when_structured_term_value_is_present_without_key() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="test_passed:admin_delete_denied",
                evidence_handle="ev_1",
                statement="test_passed behavior=admin_delete_denied",
                evidence_text="pytest passed: admin_delete_denied",
            ),
        ),
    )

    assert verdict.accepted is True


def test_rejects_when_structured_statement_terms_are_missing_from_evidence() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="test_passed:admin_delete_denied",
                evidence_handle="ev_1",
                statement="test_passed behavior=admin_delete_denied",
                evidence_text="pytest passed for user profile update",
            ),
        ),
    )

    assert verdict.accepted is False
    assert verdict.rejected_fact_ids == ("test_passed:admin_delete_denied",)
    assert verdict.rejected_reasons == (
        "semantic_miss: test_passed:admin_delete_denied cites ev_1 but evidence text lacks "
        "required term(s): behavior=admin_delete_denied",
    )


def test_prose_only_claims_are_left_to_later_semantic_evaluators() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="fact_1",
                evidence_handle="ev_1",
                statement="The AC passed because the command succeeded.",
                evidence_text="result for ev_1",
            ),
        ),
    )

    assert verdict.accepted is True


def test_rejected_verdict_requires_reason() -> None:
    with pytest.raises(ValueError, match="must include rejection reasons"):
        ClaimTermGuardVerdict(accepted=False)
