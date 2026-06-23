from __future__ import annotations

from ouroboros.auto.intent_guard import (
    IntentGuardStatus,
    diagnose_auto_state,
    guard_auto_answer,
    guard_interview_turn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger


def _cli_web_ledger() -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(
        "Build a local CLI and web app that generate reusable review outputs."
    )
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.user_locked_artifact_class",
            value="Final artifact is a local CLI plus web app; docs/checklists are only generated outputs.",
            source=LedgerSource.USER_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    return ledger


def test_inverted_intent_guard_blocks_generated_docs_only_scope_reduction() -> None:
    report = guard_auto_answer(
        goal="Build a local CLI and web app that generate reusable review outputs.",
        user_preferences={},
        ledger=_cli_web_ledger(),
        question="Should the MVP be a CLI/web implementation or a docs-only handoff package?",
        answer_text="[from-auto][conservative_default] Use a docs-only handoff package.",
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.FAIL
    assert any(check.code == "generated_option_conflict" for check in report.checks)


def test_inverted_intent_guard_warns_when_pending_question_offers_docs_only() -> None:
    report = diagnose_auto_state(
        goal="Build a local CLI and web app that generate reusable review outputs.",
        user_preferences={},
        ledger=_cli_web_ledger(),
        pending_question="Should this become a docs-only handoff package instead of the CLI/web app?",
        auto_answer_log=(),
    )

    assert report.status is IntentGuardStatus.WARN
    assert any(check.code == "generated_option_present" for check in report.checks)


def test_inverted_intent_guard_warns_when_human_changes_to_docs_only() -> None:
    report = guard_interview_turn(
        goal="Build a local CLI and web app that generate reusable review outputs.",
        question="Should the MVP be a CLI/web implementation or a docs-only handoff package?",
        answer_text="Use docs-only for this run.",
        answer_source="user",
    )

    assert report.status is IntentGuardStatus.WARN
    assert any(check.code == "user_contract_change" for check in report.checks)
