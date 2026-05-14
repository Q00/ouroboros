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


def test_accepts_existing_child_ac_result_vocabulary() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-PARENT",
        facts=(
            ClaimTermGuardFact(
                fact_id="child_ac:AC-1:test_passed",
                evidence_handle="ev_child_ac_1",
                statement="child_ac=AC-1 result=test_passed",
                evidence_text="child_ac_id=AC-1; tests passed",
            ),
            ClaimTermGuardFact(
                fact_id="child_ac:AC-2:file_modified",
                evidence_handle="ev_child_ac_2",
                statement="child_ac=AC-2 result=file_modified",
                evidence_text="child_ac_id=AC-2; path=docs/ac2.md; scope=whole_file; docs updated",
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


def test_rejects_partial_numeric_token_match() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="user_check",
                evidence_handle="ev_1",
                statement="validated user_id=1",
                evidence_text="validated user_id=10",
            ),
        ),
    )

    assert verdict.accepted is False
    assert verdict.rejected_reasons == (
        "semantic_miss: user_check cites ev_1 but evidence text lacks required term(s): user_id=1",
    )


def test_rejects_partial_path_token_match() -> None:
    verdict = deterministic_claim_term_guard(
        ac_id="AC-1",
        facts=(
            ClaimTermGuardFact(
                fact_id="file_modified:src/app.py",
                evidence_handle="ev_1",
                statement="file_modified path=src/app.py",
                evidence_text="wrote backup path=src/app.py.bak",
            ),
        ),
    )

    assert verdict.accepted is False
    assert verdict.rejected_reasons == (
        "semantic_miss: file_modified:src/app.py cites ev_1 but evidence text lacks "
        "required term(s): path=src/app.py",
    )


def test_rejected_verdict_requires_reason() -> None:
    with pytest.raises(ValueError, match="must include rejection reasons"):
        ClaimTermGuardVerdict(accepted=False)
