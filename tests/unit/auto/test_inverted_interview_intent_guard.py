from __future__ import annotations

import pytest

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


def test_inverted_intent_guard_blocks_build_goal_docs_only_reduction_without_ledger() -> None:
    report = guard_auto_answer(
        goal="Build a local CLI and web app.",
        user_preferences={},
        ledger=SeedDraftLedger.from_goal("Build a local CLI and web app."),
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


@pytest.mark.parametrize("adopted_source", ["data_fact", "research_fact"])
def test_user_adopted_external_facts_warn_like_human_never_fail_closed(
    adopted_source: str,
) -> None:
    """[from-data]/[from-research] answers are user-confirmed before forwarding.

    They must get the human-grade "confirm this scope change is intentional"
    WARN — not the silent repo_fact evidence pass (they are point-in-time and
    not cheaply re-verifiable), and never the generated fail-closed branch.
    """
    report = guard_interview_turn(
        goal="Build a local CLI and web app that generate reusable review outputs.",
        question="Should the MVP be a CLI/web implementation or a docs-only handoff package?",
        answer_text="Use docs-only for this run.",
        answer_source=adopted_source,
    )

    assert report.status is IntentGuardStatus.WARN
    assert any(check.code == "user_contract_change" for check in report.checks)
    assert not any(check.code == "generated_option_conflict" for check in report.checks)


def test_interview_answer_source_classifier_maps_data_and_research_prefixes() -> None:
    from ouroboros.mcp.tools.authoring_handlers import _classify_interview_answer_source

    assert _classify_interview_answer_source("[from-data] 78% of MAU are free tier") == "data_fact"
    assert (
        _classify_interview_answer_source("[from-research] API limit is 100 rps") == "research_fact"
    )
    assert _classify_interview_answer_source("[from-code] FastAPI per manifest") == "repo_fact"
    assert _classify_interview_answer_source("just my preference") == "human"


def test_inverted_intent_guard_allows_generated_interview_answer_for_explicit_narrowed_goal() -> (
    None
):
    report = guard_interview_turn(
        goal="Create docs-only handoff files for the team.",
        question="Should this produce docs-only handoff files?",
        answer_text="[from-auto][conservative_default] Use docs-only handoff files.",
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.PASS
    assert not any(check.code == "generated_option_conflict" for check in report.checks)


@pytest.mark.parametrize(
    "generated_reduction",
    [
        "docs-only handoff package",
        "checklist-only output",
        "checklist only output",
    ],
)
def test_inverted_intent_guard_preserves_goal_contract_when_checklist_is_supporting_output(
    generated_reduction: str,
) -> None:
    report = guard_auto_answer(
        goal="Build a local CLI and web app that generate checklist packages.",
        user_preferences={},
        ledger=SeedDraftLedger.from_goal(
            "Build a local CLI and web app that generate checklist packages."
        ),
        question=(f"Should the MVP be a CLI/web implementation or {generated_reduction}?"),
        answer_text=(f"[from-auto][conservative_default] Use a {generated_reduction}."),
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.FAIL
    assert any(check.code == "generated_option_conflict" for check in report.checks)


@pytest.mark.parametrize(
    ("goal", "question", "answer"),
    [
        (
            "Create docs-only handoff files for the team.",
            "Should this produce docs-only handoff files?",
            "[from-auto][conservative_default] Use docs-only handoff files.",
        ),
        (
            "Write a review-only package for maintainers.",
            "Should this stay review-only?",
            "[from-auto][conservative_default] Use review-only mode.",
        ),
        (
            "Generate checklist-only output for release triage.",
            "Should this produce checklist-only output?",
            "[from-auto][conservative_default] Use checklist-only output.",
        ),
        (
            "Create a docs-only PDF handoff for the team.",
            "Should this produce a docs-only handoff?",
            "[from-auto][conservative_default] Use a docs-only handoff.",
        ),
        (
            "Create docs-only HTML handoff files for the team.",
            "Should this produce docs-only handoff files?",
            "[from-auto][conservative_default] Use docs-only handoff files.",
        ),
        (
            "Create docs-only handoff files for the web team.",
            "Should this produce docs-only handoff files?",
            "[from-auto][conservative_default] Use docs-only handoff files.",
        ),
        (
            "Create docs-only handoff files for the app team.",
            "Should this produce docs-only handoff files?",
            "[from-auto][conservative_default] Use docs-only handoff files.",
        ),
    ],
)
def test_inverted_intent_guard_allows_explicit_narrowed_output_user_contracts(
    goal: str,
    question: str,
    answer: str,
) -> None:
    report = guard_auto_answer(
        goal=goal,
        user_preferences={},
        ledger=SeedDraftLedger.from_goal(goal),
        question=question,
        answer_text=answer,
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.PASS
    assert not any(check.code == "generated_option_conflict" for check in report.checks)


def test_inverted_intent_guard_does_not_warn_when_pending_question_matches_explicit_narrowed_contract() -> (
    None
):
    goal = "Create docs-only handoff files for the web team."

    report = diagnose_auto_state(
        goal=goal,
        user_preferences={},
        ledger=SeedDraftLedger.from_goal(goal),
        pending_question="Should this produce docs-only handoff files?",
        auto_answer_log=(),
    )

    assert report.status is IntentGuardStatus.PASS
    assert not any(check.code == "generated_option_present" for check in report.checks)


def test_inverted_intent_guard_blocks_generated_docs_only_when_contract_excludes_it() -> None:
    ledger = SeedDraftLedger.from_goal(
        "Build executable software that can produce docs as a supporting output."
    )
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.not_final_artifact",
            value=(
                "Final artifact is executable software; docs-only handoff is not "
                "the final artifact."
            ),
            source=LedgerSource.USER_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    report = guard_auto_answer(
        goal="Build executable software that can produce docs as a supporting output.",
        user_preferences={},
        ledger=ledger,
        question="Should the MVP be executable software or a docs-only handoff package?",
        answer_text="[from-auto][conservative_default] Use a docs-only handoff package.",
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.FAIL
    assert any(check.code == "generated_option_conflict" for check in report.checks)


def test_inverted_intent_guard_does_not_treat_app_substrings_as_artifact_contracts() -> None:
    report = guard_auto_answer(
        goal="Make the interview approach clearer for maintainers.",
        user_preferences={},
        ledger=SeedDraftLedger.from_goal("Make the interview approach clearer for maintainers."),
        question="Should this be a normal improvement or review-only mode?",
        answer_text="[from-auto][conservative_default] Use review-only mode.",
        answer_source="conservative_default",
    )

    assert report.status is IntentGuardStatus.PASS
    assert report.checks == ()
