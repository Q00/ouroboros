from __future__ import annotations

from ouroboros.auto.answerer import AutoAnswerSource
from ouroboros.auto.intent_guard import (
    IntentGuardStatus,
    diagnose_auto_state,
    guard_auto_answer,
    guard_interview_turn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger

VIDEO_GOAL = (
    "I want to make a video harness when I put a video to harness, "
    "the harness will make some shorts and long form video with transcript"
)


def test_intent_guard_blocks_generated_review_only_option_over_user_contract() -> None:
    report = guard_auto_answer(
        goal=VIDEO_GOAL,
        user_preferences={
            "outputs": VIDEO_GOAL,
            "acceptance_criteria": VIDEO_GOAL,
        },
        ledger=SeedDraftLedger.from_goal(VIDEO_GOAL),
        question=(
            "Which one is the actual MVP behavior to lock: `--mode auto` creates MP4s "
            "and transcripts, or `--mode review` creates only the review package and "
            "rejects automated export?"
        ),
        answer_text="Use REVIEW REVIEW as the MVP behavior.",
        answer_source=AutoAnswerSource.CONSERVATIVE_DEFAULT.value,
    )

    assert report.status is IntentGuardStatus.FAIL
    assert any(check.code == "generated_option_conflict" for check in report.checks)


def test_intent_guard_allows_user_preference_contract_answer() -> None:
    report = guard_auto_answer(
        goal=VIDEO_GOAL,
        user_preferences={
            "outputs": VIDEO_GOAL,
            "acceptance_criteria": VIDEO_GOAL,
        },
        ledger=SeedDraftLedger.from_goal(VIDEO_GOAL),
        question=(
            "Which one is the actual MVP behavior to lock: `--mode auto` creates MP4s "
            "and transcripts, or `--mode review` creates only the review package?"
        ),
        answer_text=VIDEO_GOAL,
        answer_source=AutoAnswerSource.USER_PREFERENCE.value,
    )

    assert report.status is IntentGuardStatus.PASS


def test_intent_guard_doctor_surfaces_pending_generated_option_and_spec_pollution() -> None:
    report = diagnose_auto_state(
        goal=VIDEO_GOAL,
        user_preferences={},
        ledger=SeedDraftLedger.from_goal(VIDEO_GOAL),
        pending_question=(
            "Should this be --mode auto exporting MP4s or --mode review with review-only "
            "package output?"
        ),
        auto_answer_log=(),
        seed_artifact={
            "constraints": [
                "Use local files",
                "[seed qa lateral repair attempt 1] # Lateral Thinking:\nQA differences: bad",
            ]
        },
    )

    assert report.status is IntentGuardStatus.FAIL
    codes = {check.code: check.status for check in report.checks}
    assert codes["generated_option_present"] is IntentGuardStatus.WARN
    assert codes["spec_pollution"] is IntentGuardStatus.FAIL


def test_intent_guard_doctor_recommends_inverted_interview_for_high_context_prompt() -> None:
    report = diagnose_auto_state(
        goal=(
            "As discussed in the recording, use the transcript and raw files from the "
            "previous failed run to generate the handoff workflow."
        ),
        user_preferences={},
        ledger=SeedDraftLedger.from_goal(
            "As discussed in the recording, use the transcript and raw files from the "
            "previous failed run to generate the handoff workflow."
        ),
        pending_question="What kind of project should this be?",
        auto_answer_log=(),
    )

    assert report.status is IntentGuardStatus.WARN
    checks = {check.code: check for check in report.checks}
    assert checks["high_context_inverted_interview_recommended"].status is IntentGuardStatus.WARN
    assert "inverted interview" in (
        checks["high_context_inverted_interview_recommended"].action or ""
    )


def test_intent_guard_doctor_does_not_warn_for_low_context_prompt() -> None:
    report = diagnose_auto_state(
        goal="Build a small habit tracking CLI",
        user_preferences={},
        ledger=SeedDraftLedger.from_goal("Build a small habit tracking CLI"),
        pending_question="Who is the main user?",
        auto_answer_log=(),
    )

    assert report.status is IntentGuardStatus.PASS
    assert not any(
        check.code == "high_context_inverted_interview_recommended" for check in report.checks
    )


def test_intent_guard_doctor_suppresses_inverted_warning_after_synthesis_pass() -> None:
    goal = "Use the transcript and raw files from the previous run to create the workflow."
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.add_entry(
        "inputs",
        LedgerEntry(
            key="inputs.synthesized_context",
            value="Pre-work synthesis pass summarized the transcript and raw files.",
            source=LedgerSource.USER_GOAL,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    report = diagnose_auto_state(
        goal=goal,
        user_preferences={},
        ledger=ledger,
        pending_question=None,
        auto_answer_log=(),
    )

    assert report.status is IntentGuardStatus.PASS
    assert not any(
        check.code == "high_context_inverted_interview_recommended" for check in report.checks
    )


def test_interview_turn_guard_warns_when_human_changes_output_contract() -> None:
    report = guard_interview_turn(
        goal=VIDEO_GOAL,
        question=(
            "Should this be --mode auto exporting MP4s or --mode review with review-only "
            "package output?"
        ),
        answer_text="Let's make it review-only for now.",
        answer_source="human",
    )

    assert report.status is IntentGuardStatus.WARN
    assert any(check.code == "user_contract_change" for check in report.checks)


def test_interview_turn_guard_fails_generated_contract_drift() -> None:
    report = guard_interview_turn(
        goal=VIDEO_GOAL,
        question=(
            "Should this be --mode auto exporting MP4s or --mode review with review-only "
            "package output?"
        ),
        answer_text="[from-auto][conservative_default] Use review-only mode.",
        answer_source="generated",
    )

    assert report.status is IntentGuardStatus.FAIL
    assert any(check.code == "generated_option_conflict" for check in report.checks)
