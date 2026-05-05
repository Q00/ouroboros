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


def _seed(*, ac: tuple[str, ...], goal: str = "Build a habit tracker") -> Seed:
    return Seed(
        goal=goal,
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


def test_grade_gate_blocks_seed_goal_mismatch_with_ready_ledger() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(
        goal="Build a weather dashboard",
        ac=("`weather list` prints stable stdout containing forecasts",),
    )

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.C
    assert not result.may_run
    assert {blocker.code for blocker in result.blockers} == {"seed_goal_mismatch"}


def test_grade_gate_blocks_subset_goal_mismatch_with_ready_ledger() -> None:
    ledger = SeedDraftLedger.from_goal("Build a weather dashboard")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(
        goal="Build a dashboard",
        ac=("`dashboard show` prints stable stdout containing dashboard status",),
    )

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.C
    assert {blocker.code for blocker in result.blockers} == {"seed_goal_mismatch"}


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


def test_auto_answerer_allows_git_product_branch_deletion_questions() -> None:
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a Git branch manager")

    examples = (
        "Should users be able to delete the branch?",
        "Should the app delete the branch automatically?",
        "Should the tool remove the branch after merge?",
    )

    answers = [answerer.answer(question, ledger) for question in examples]

    assert all(answer.blocker is None for answer in answers)
    assert all(answer.source != AutoAnswerSource.BLOCKER for answer in answers)
    assert all("product behavior" in answer.text.lower() for answer in answers)


def test_auto_answerer_preserves_product_behavior_phrasing_variants() -> None:
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a compliance SaaS")

    examples = (
        "Should legal documents be editable?",
        "Should users subscribe to paid service tiers?",
        "Should legal review workflows be tracked?",
        "Which password rules should the signup form enforce?",
    )

    answers = [answerer.answer(question, ledger) for question in examples]

    assert all(answer.blocker is None for answer in answers)
    assert all(answer.source != AutoAnswerSource.BLOCKER for answer in answers)
    assert all("product behavior" in answer.text.lower() for answer in answers)


def test_auto_answerer_preserves_passive_product_behavior_variants() -> None:
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a source-control compliance tool")

    examples = (
        "Should branches be deleted after merge?",
        "Should API keys be removed after rotation?",
        "Should legal documents be edited?",
    )

    answers = [answerer.answer(question, ledger) for question in examples]

    assert all(answer.blocker is None for answer in answers)
    assert all(answer.source != AutoAnswerSource.BLOCKER for answer in answers)
    assert all("product behavior" in answer.text.lower() for answer in answers)


def test_auto_answerer_still_blocks_current_branch_deletion_authority() -> None:
    answer = AutoAnswerer().answer(
        "Should we delete the current branch?",
        SeedDraftLedger.from_goal("Clean up repository branches"),
    )

    assert answer.blocker is not None
    assert answer.source == AutoAnswerSource.BLOCKER


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
        "Should admins be able to rotate production credentials?",
        "Should production credential status be shown in settings?",
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
        "Which production credential should the workflow use?",
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
        "Should admins be able to rotate production credentials?",
        "Should production credential status be shown in settings?",
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
        assert answer.blocker is None
        assert "conservative mvp" not in answer.text.lower()
        assert "product behavior" in answer.text.lower()
        assert {"constraints", "acceptance_criteria"} <= updated_sections
        assert not {"actors", "inputs", "outputs", "verification_plan"} & updated_sections


def test_auto_answerer_allows_safe_production_and_project_feature_questions() -> None:
    answerer = AutoAnswerer()
    questions = (
        "What should the production deploy output on failure?",
        "Should deleting a project also delete its tasks?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a project app"))
        assert answer.blocker is None
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert "runtime_context" not in updated_sections


def test_ledger_marks_same_key_conflicting_values_as_open_gap() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.primary",
            value="Write a JSON report",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.primary",
            value="Display an HTML dashboard",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )

    assert ledger.sections["outputs"].status() == LedgerStatus.CONFLICTING
    assert "outputs" in ledger.open_gaps()


def test_auto_answerer_acceptance_default_matches_grade_observability() -> None:
    answer = AutoAnswerer().answer(
        "Which command output verifies the acceptance criteria?",
        SeedDraftLedger.from_goal("Build a CLI"),
    )
    acceptance = [
        entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
    ]

    assert acceptance
    assert (
        "which command output verifies the acceptance criteria" not in acceptance[0].value.lower()
    )
    assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(ac=(acceptance[0].value,), goal="Build a CLI")

    assert GradeGate().grade_seed(seed, ledger=ledger).grade == SeedGrade.A


def test_auto_answerer_routes_common_input_output_prompts_to_io_ledger() -> None:
    answerer = AutoAnswerer()
    for question in (
        "What inputs does the command take?",
        "What outputs does it produce?",
    ):
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}

        assert {"actors", "inputs", "outputs"} <= updated_sections
        assert not {"constraints", "failure_modes"} >= updated_sections


def test_auto_answerer_blocks_production_environment_selection_variants() -> None:
    questions = (
        "Which production environment should we deploy to?",
        "Which AWS account should we deploy production to?",
    )
    for question in questions:
        answer = AutoAnswerer().answer(question, SeedDraftLedger.from_goal("Deploy a service"))
        assert answer.blocker is not None
        assert answer.source == AutoAnswerSource.BLOCKER


def test_ledger_later_same_key_correction_resolves_conflict() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    for value in ("Write a JSON report", "Display an HTML dashboard", "Write a JSON report"):
        ledger.add_entry(
            "outputs",
            LedgerEntry(
                key="outputs.primary",
                value=value,
                source=LedgerSource.CONSERVATIVE_DEFAULT,
                confidence=0.8,
                status=LedgerStatus.DEFAULTED,
            ),
        )

    assert ledger.sections["outputs"].status() == LedgerStatus.DEFAULTED
    assert "outputs" not in ledger.open_gaps()


def test_auto_answerer_allows_product_security_and_billing_requirement_questions() -> None:
    questions = (
        "Which password rules should the signup form enforce?",
        "Which API keys should users be able to rotate?",
        "Which billing provider integrations should the app support?",
    )

    for question in questions:
        answer = AutoAnswerer().answer(question, SeedDraftLedger.from_goal("Build a SaaS app"))
        assert answer.blocker is None


def test_ledger_later_answer_can_clear_same_key_blocker() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="blocker.auto_answer",
            value="production credential required",
            source=LedgerSource.BLOCKER,
            confidence=1.0,
            status=LedgerStatus.BLOCKED,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="blocker.auto_answer",
            value="Use staging-only dry run; no production credential is needed",
            source=LedgerSource.USER_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    assert ledger.sections["constraints"].status() == LedgerStatus.CONFIRMED
    assert "constraints" not in ledger.open_gaps()


def test_auto_answerer_non_goals_respect_explicit_goal_scope() -> None:
    cases = (
        ("Deploy this service to production", "production deployment"),
        ("Add authentication to the app", "authentication"),
        ("Enable SSO for enterprise users", "authentication"),
        ("Add OAuth support to the CLI", "authentication"),
        ("Implement authorization roles", "authentication"),
    )

    for goal, forbidden_non_goal in cases:
        answer = AutoAnswerer().answer("What are the non-goals?", SeedDraftLedger.from_goal(goal))
        assert forbidden_non_goal not in answer.text.lower()


def test_ledger_assumptions_use_latest_resolved_facts_for_risk() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    _fill_minimal_ready_ledger(ledger)
    for value in ("CLI user", "CLI user", "CLI user"):
        ledger.add_entry(
            "actors",
            LedgerEntry(
                key="actors.primary",
                value=value,
                source=LedgerSource.ASSUMPTION,
                confidence=0.72,
                status=LedgerStatus.INFERRED,
            ),
        )

    assert ledger.assumptions().count("CLI user") == 1
    assert GradeGate().grade_ledger(ledger).scores["risk"] <= 0.25


def test_auto_answerer_non_goals_use_latest_resolved_goal() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    ledger.add_entry(
        "goal",
        LedgerEntry(
            key="goal.primary",
            value="Add authentication to the app",
            source=LedgerSource.USER_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    answer = AutoAnswerer().answer("What are the non-goals?", ledger)

    assert "authentication" not in answer.text.lower()


def test_grade_seed_allows_safe_product_delete_assumptions() -> None:
    ledger = SeedDraftLedger.from_goal("Build a task app")
    _fill_minimal_ready_ledger(ledger)
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="assumption.safe_delete",
            value="Users can delete their own tasks after confirmation",
            source=LedgerSource.ASSUMPTION,
            confidence=0.72,
            status=LedgerStatus.INFERRED,
        ),
    )

    result = GradeGate().grade_seed(
        _seed(
            ac=("`task delete` prints stable stdout confirming deletion",), goal="Build a task app"
        ),
        ledger=ledger,
    )

    assert result.grade == SeedGrade.A
    assert not any(blocker.code == "high_risk_assumptions" for blocker in result.blockers)


def test_grade_gate_accepts_exit_status_and_http_status_criteria() -> None:
    ledger = SeedDraftLedger.from_goal("Build health checks")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(
        ac=("CLI exits 0 on success", "GET /health returns 200"), goal="Build health checks"
    )

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.A
    assert result.may_run


def test_auto_answerer_preserves_feature_specific_acceptance_semantics() -> None:
    answer = AutoAnswerer().answer(
        "What acceptance criteria should the delete endpoint satisfy?",
        SeedDraftLedger.from_goal("Build a delete endpoint"),
    )

    assert answer.blocker is None
    assert any(section == "acceptance_criteria" for section, _entry in answer.ledger_updates)
    assert "delete endpoint" in answer.text.lower()
    assert "stdout" not in answer.text.lower()


def test_auto_answerer_allows_secret_token_product_requirement_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should users be able to store secret tokens?",
        SeedDraftLedger.from_goal("Build a token vault"),
    )

    assert answer.blocker is None
    assert answer.source != AutoAnswerSource.BLOCKER


def test_auto_answerer_preserves_open_ended_feature_acceptance_semantics() -> None:
    answer = AutoAnswerer().answer(
        "What acceptance criteria should the webhook delivery flow satisfy?",
        SeedDraftLedger.from_goal("Build webhook delivery"),
    )

    assert answer.blocker is None
    assert any(section == "acceptance_criteria" for section, _entry in answer.ledger_updates)
    assert "webhook delivery flow" in answer.text.lower()
    assert "stdout" not in answer.text.lower()


def test_grade_gate_ignores_inactive_high_risk_assumptions() -> None:
    ledger = SeedDraftLedger.from_goal("Build a local task app")
    _fill_minimal_ready_ledger(ledger)
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="assumption.old_production",
            value="Use production credential",
            source=LedgerSource.ASSUMPTION,
            confidence=0.2,
            status=LedgerStatus.WEAK,
        ),
    )

    result = GradeGate().grade_seed(
        _seed(ac=("`task list` prints stable stdout",), goal="Build a local task app"),
        ledger=ledger,
    )

    assert result.grade == SeedGrade.A
    assert not any(blocker.code == "high_risk_assumptions" for blocker in result.blockers)


def test_grade_gate_blocks_high_ambiguity_seed() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    _fill_minimal_ready_ledger(ledger)
    seed = _seed(ac=("`task list` prints stable stdout",)).model_copy(
        update={"metadata": SeedMetadata(ambiguity_score=0.45)}
    )

    result = GradeGate().grade_seed(seed, ledger=ledger)

    assert result.grade == SeedGrade.C
    assert not result.may_run
    assert any(blocker.code == "high_ambiguity_score" for blocker in result.blockers)


def test_auto_answerer_preserves_safe_product_behavior_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should completed tasks be marked done?",
        SeedDraftLedger.from_goal("Build a task app"),
    )

    assert answer.blocker is None
    assert "marked done" in answer.text.lower()
    assert "conservative mvp" not in answer.text.lower()
    acceptance = [
        entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
    ]
    assert acceptance
    ledger = SeedDraftLedger.from_goal("Build a task app")
    _fill_minimal_ready_ledger(ledger)
    assert (
        GradeGate()
        .grade_seed(_seed(ac=(acceptance[0].value,), goal="Build a task app"), ledger=ledger)
        .grade
        == SeedGrade.A
    )


def test_auto_answerer_preserves_output_behavior_questions() -> None:
    answer = AutoAnswerer().answer(
        "What output should the export command write?",
        SeedDraftLedger.from_goal("Build an export command"),
    )

    assert answer.blocker is None
    assert "export command write" in answer.text.lower()
    assert "conservative mvp" not in answer.text.lower()
    acceptance = [
        entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
    ]
    assert acceptance
    ledger = SeedDraftLedger.from_goal("Build an export command")
    _fill_minimal_ready_ledger(ledger)
    assert (
        GradeGate()
        .grade_seed(_seed(ac=(acceptance[0].value,), goal="Build an export command"), ledger=ledger)
        .grade
        == SeedGrade.A
    )


def test_auto_answerer_allows_credential_auth_product_questions() -> None:
    answer = AutoAnswerer().answer(
        "Should the app use credential-based authentication?",
        SeedDraftLedger.from_goal("Build an auth app"),
    )

    assert answer.blocker is None
    assert "credential-based authentication" in answer.text.lower()


def test_auto_answerer_allows_user_managed_secret_and_integration_deletion() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should users be able to delete an API key?",
        "Should users be able to delete a secret?",
        "Should users be able to remove a repo integration?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build settings UI"))
        assert answer.blocker is None
        assert "product behavior" in answer.text.lower()


def test_auto_answerer_allows_user_managed_token_and_key_product_questions() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should users be able to rotate private keys?",
        "Should the app display access tokens?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build identity settings"))
        assert answer.blocker is None
        assert "product behavior" in answer.text.lower()


def test_auto_answerer_allows_production_credential_product_semantics() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should users be able to configure production credentials?",
        "Should the app store production credentials?",
        "What credential fields should the production settings form display?",
    )

    for question in questions:
        answer = answerer.answer(
            question,
            SeedDraftLedger.from_goal("Build credential management settings"),
        )
        assert answer.blocker is None
        assert answer.source != AutoAnswerSource.BLOCKER
        assert "product behavior" in answer.text.lower()


def test_auto_answerer_still_blocks_real_production_credential_authority() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Which credential value should production use?",
        "Which credentials should CI configure for production?",
        "Use the production credential secret for deployment?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Deploy a service"))
        assert answer.blocker is not None
        assert answer.source == AutoAnswerSource.BLOCKER
