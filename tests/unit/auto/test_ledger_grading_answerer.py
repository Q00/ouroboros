from __future__ import annotations

from ouroboros.auto.answerer import AutoAnswerer, AutoAnswerSource
from ouroboros.auto.gap_detector import GapDetector
from ouroboros.auto.grading import GradeGate, SeedGrade
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _fill_minimal_ready_ledger(ledger: SeedDraftLedger) -> None:
    entries = {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }
    for section, value in entries.items():
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=LedgerSource.CONSERVATIVE_DEFAULT,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )


def _seed(*, ac: tuple[str, ...]) -> Seed:
    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=ac,
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )


def test_ledger_not_ready_until_required_sections_are_resolved() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")

    assert "actors" in ledger.open_gaps()
    assert not ledger.is_seed_ready()

    _fill_minimal_ready_ledger(ledger)

    assert ledger.is_seed_ready()
    assert ledger.summary()["open_gaps"] == []


def test_weak_required_sections_remain_open_gaps() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    ledger.sections["actors"].entries.clear()
    ledger.add_entry(
        "actors",
        LedgerEntry(
            key="actors.weak_guess",
            value="Maybe a local user",
            source=LedgerSource.ASSUMPTION,
            confidence=0.2,
            status=LedgerStatus.WEAK,
        ),
    )

    assert "actors" in ledger.open_gaps()
    assert not ledger.is_seed_ready()


def test_gap_detector_reports_missing_sections() -> None:
    gaps = GapDetector().detect(SeedDraftLedger.from_goal("Build a habit tracker"))

    assert {gap.section for gap in gaps} >= {"actors", "acceptance_criteria"}


def test_grade_gate_blocks_b_or_c_from_running() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    result = GradeGate().grade_ledger(ledger)

    assert result.grade != SeedGrade.A
    assert not result.may_run


def test_grade_gate_accepts_observable_seed_with_ready_ledger() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(ac=("`habit list` prints stable stdout containing created habits",))

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.A
    assert result.may_run


def test_grade_gate_rejects_unresolved_ledger_even_with_clean_seed() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    seed = _seed(ac=("`habit list` prints stdout containing created habits",))

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.C
    assert not result.may_run
    assert any(blocker.code == "ledger_open_gap" for blocker in result.blockers)


def test_grade_gate_requires_observable_acceptance_behavior_not_keywords() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(ac=("The command uses clean architecture", "The API is maintainable"))

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.B
    assert not result.may_run
    assert (
        sum(1 for finding in result.findings if finding.code == "untestable_acceptance_criteria")
        == 2
    )


def test_grade_gate_rejects_vague_acceptance_criteria() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(ac=("The CLI should be easy and user-friendly",))

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.B
    assert not result.may_run
    assert any(finding.code == "vague_acceptance_criteria" for finding in result.findings)


def test_auto_answerer_source_tags_and_applies_updates() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    answerer = AutoAnswerer()

    answer = answerer.answer("How should we verify this is done?", ledger)
    answerer.apply(answer, ledger, question="How should we verify this is done?")

    assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT
    assert answer.prefixed_text.startswith("[from-auto][conservative_default]")
    assert "verification_plan" not in ledger.open_gaps()


def test_auto_answerer_allows_product_domain_delete_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should users be able to delete habits?",
        SeedDraftLedger.from_goal("Build a habit tracker"),
    )

    assert answer.blocker is None
    assert answer.source != AutoAnswerSource.BLOCKER


def test_auto_answerer_allows_product_domain_secret_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should the app support secret notes?",
        SeedDraftLedger.from_goal("Build a notes app"),
    )

    assert answer.blocker is None
    assert answer.source != AutoAnswerSource.BLOCKER


def test_auto_answerer_allows_product_domain_file_removal_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should users be able to remove uploaded files?",
        SeedDraftLedger.from_goal("Build a file manager"),
    )

    assert answer.blocker is None
    assert answer.source != AutoAnswerSource.BLOCKER


def test_auto_answerer_returns_blocker_for_plain_secret_questions() -> None:
    answer = AutoAnswerer().answer(
        "Which secret should the workflow use?",
        SeedDraftLedger.from_goal("Deploy a service"),
    )

    assert answer.blocker is not None
    assert answer.source == AutoAnswerSource.BLOCKER


def test_auto_answerer_returns_blocker_for_credentials() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    answerer = AutoAnswerer()

    answer = answerer.answer("Which production API key should the workflow use?", ledger)
    answerer.apply(answer, ledger, question="Which production API key should the workflow use?")

    assert answer.blocker is not None
    assert answer.source == AutoAnswerSource.BLOCKER
    assert "constraints" in ledger.open_gaps()
    assert not ledger.is_seed_ready()
    assert any(
        entry.status == LedgerStatus.BLOCKED for entry in ledger.sections["constraints"].entries
    )


def test_auto_answerer_allows_benign_sensitive_domain_vocabulary() -> None:
    answerer = AutoAnswerer()
    benign_questions = (
        "Should the app support credential login?",
        "Should legal documents be editable?",
        "Should medical records be exportable?",
        "Should users see payment history?",
        "Should users be able to rotate API keys?",
        "Should the app support password reset?",
        "Should the app support billing provider integrations?",
        "Should users subscribe to paid service tiers?",
        "Should legal review workflows be tracked?",
    )

    for question in benign_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a document app"))
        assert answer.blocker is None
        assert answer.source != AutoAnswerSource.BLOCKER


def test_auto_answerer_blocks_contextual_human_authority_questions() -> None:
    answerer = AutoAnswerer()
    blocking_questions = (
        "Which credential value should production use?",
        "Which payment provider account should we charge?",
        "What legal approval is needed for liability risk?",
        "What medical advice should the app recommend?",
        "What API key should the workflow use?",
        "Which password should CI configure?",
    )

    for question in blocking_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Deploy a service"))
        assert answer.blocker is not None
        assert answer.source == AutoAnswerSource.BLOCKER


def test_blank_goal_remains_open_gap() -> None:
    ledger = SeedDraftLedger.from_goal("   ")
    _fill_minimal_ready_ledger(ledger)

    assert "goal" in ledger.open_gaps()
    assert not ledger.is_seed_ready()


def test_auto_answerer_does_not_route_feature_semantics_to_io_actor_defaults() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should users be able to delete habits?",
        "Should users see payment history?",
        "Should users be able to rotate API keys?",
        "Should the app support password reset?",
        "Should the app support billing provider integrations?",
        "Should users subscribe to paid service tiers?",
        "Should legal review workflows be tracked?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a habit tracker"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert not {"actors", "inputs", "outputs"} & updated_sections


def test_auto_answerer_avoids_generic_defaults_for_feature_semantics() -> None:
    answerer = AutoAnswerer()
    questions = (
        "What output should the export command write?",
        "What input format does the config file use?",
        "Should completed tasks be marked done?",
        "What should users be able to edit?",
        "Which users can delete projects?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a task app"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert (
            not {"actors", "inputs", "outputs", "verification_plan", "acceptance_criteria"}
            & updated_sections
        )
