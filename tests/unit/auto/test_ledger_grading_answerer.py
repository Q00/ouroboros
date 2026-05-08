from __future__ import annotations

from ouroboros.auto.answerer import AutoAnswerContext, AutoAnswerer, AutoAnswerSource
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


def test_auto_answerer_uses_supplied_repo_fact_for_runtime_questions() -> None:
    ledger = SeedDraftLedger.from_goal("Update the CLI")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python 3.12 project managed with uv and Typer CLI."},
        evidence={"runtime_context": ("pyproject.toml", "src/ouroboros/cli/main.py")},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source == AutoAnswerSource.REPO_FACT
    assert answer.confidence == 0.9
    assert "Python 3.12" in answer.text
    runtime_entries = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert len(runtime_entries) == 1
    assert runtime_entries[0].source == LedgerSource.REPO_FACT
    assert runtime_entries[0].status == LedgerStatus.CONFIRMED
    assert runtime_entries[0].evidence == ["pyproject.toml", "src/ouroboros/cli/main.py"]


def test_auto_answerer_runtime_question_falls_back_without_repo_fact() -> None:
    answer = AutoAnswerer().answer(
        "Which runtime and framework should we use?",
        SeedDraftLedger.from_goal("Update the CLI"),
    )

    assert answer.source == AutoAnswerSource.EXISTING_CONVENTION
    runtime_entries = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert runtime_entries[0].source == LedgerSource.EXISTING_CONVENTION
    assert runtime_entries[0].status == LedgerStatus.DEFAULTED


def test_auto_answerer_routes_stack_selection_to_runtime_context() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Which runtime stack, repo, and project patterns should be used?",
        "What project structure should we use?",
        "Which repo should we use?",
        "What framework is this repo using?",
        "Which framework?",
        "What runtime?",
        "What package manager?",
        "Which project structure?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Update the CLI"))

        assert answer.source == AutoAnswerSource.EXISTING_CONVENTION
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert "runtime_context" in updated_sections


def test_auto_answerer_partial_runtime_facts_do_not_confirm_runtime_context() -> None:
    ledger = SeedDraftLedger.from_goal("Update the CLI")
    context = AutoAnswerContext(
        repo_facts={
            "framework": "Typer CLI",
            "package_manager": "uv",
            "project_structure": "src/ouroboros package with tests/unit coverage",
        },
        evidence={
            "framework": ("src/ouroboros/cli/main.py",),
            "package_manager": ("pyproject.toml", "uv.lock"),
            "project_structure": ("src/ouroboros/", "tests/unit/"),
        },
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source == AutoAnswerSource.EXISTING_CONVENTION
    assert answer.confidence == 0.8
    assert "Typer CLI" in answer.text
    runtime_entries = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert [entry.key for entry in runtime_entries] == [
        "runtime.existing_project",
        "runtime.partial.framework",
        "runtime.partial.package_manager",
        "runtime.partial.project_structure",
    ]
    assert runtime_entries[0].source == LedgerSource.EXISTING_CONVENTION
    assert runtime_entries[0].status == LedgerStatus.DEFAULTED
    assert runtime_entries[0].evidence == [
        "src/ouroboros/cli/main.py",
        "pyproject.toml",
        "uv.lock",
        "src/ouroboros/",
        "tests/unit/",
    ]
    partial_entries = runtime_entries[1:]
    assert {entry.source for entry in partial_entries} == {LedgerSource.REPO_FACT}
    assert {entry.status for entry in partial_entries} == {LedgerStatus.WEAK}
    assert not any(
        entry.source == LedgerSource.REPO_FACT and entry.status == LedgerStatus.CONFIRMED
        for entry in runtime_entries
    )

    AutoAnswerer().apply(answer, ledger, question="Which runtime and framework should we use?")

    assert ledger.sections["runtime_context"].status() == LedgerStatus.DEFAULTED


def test_auto_answerer_context_does_not_override_blockers() -> None:
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Production deployment uses AWS."},
        evidence={"runtime_context": ("docs/deploy.md",)},
    )

    answer = AutoAnswerer().answer(
        "Which production environment should we deploy to?",
        SeedDraftLedger.from_goal("Deploy a service"),
        context,
    )

    assert answer.blocker is not None
    assert answer.source == AutoAnswerSource.BLOCKER


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


def test_auto_answerer_preserves_product_runtime_status_semantics() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should the app display runtime status?",
        "What runtime status should the app display?",
    )

    for question in questions:
        answer = answerer.answer(
            question,
            SeedDraftLedger.from_goal("Build an operations dashboard"),
        )

        assert answer.blocker is None
        assert "product behavior" in answer.text.lower()
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert {"constraints", "acceptance_criteria"} <= updated_sections
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


def test_auto_answerer_routes_multilingual_questions_by_ledger_intent() -> None:
    answerer = AutoAnswerer()
    cases = (
        ("¿Quién es el usuario principal?", {"actors", "inputs", "outputs"}),
        ("Quelles sorties le CLI doit-il produire?", {"actors", "inputs", "outputs"}),
        (
            "Quels critères d'acceptation le rapport doit-il satisfaire?",
            {"acceptance_criteria", "verification_plan"},
        ),
        ("¿Cómo verificamos que funciona?", {"verification_plan", "acceptance_criteria"}),
        ("어떤 런타임과 저장소 구조를 사용해야 하나요?", {"runtime_context", "constraints"}),
        ("哪些功能不在范围内?", {"non_goals"}),
        ("リポジトリのランタイムは何を使いますか?", {"runtime_context", "constraints"}),
    )

    for question, expected_sections in cases:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}

        assert answer.blocker is None, question
        assert expected_sections <= updated_sections, question


def test_auto_answerer_multilingual_permission_questions_route_to_product_behavior() -> None:
    """Multilingual permission/product-behavior questions must NOT misroute to
    actor/IO just because they contain an actor noun + interrogative.  Without
    multilingual product-behavior detection the classifier was asymmetric:
    actor cues recognised non-English wording but PRODUCT_BEHAVIOR did not, so
    questions like ``Quels utilisateurs peuvent supprimer des branches?`` and
    ``哪些用户可以删除分支?`` silently injected ``actors``/``inputs``/``outputs``
    assumptions instead of preserving the requested authorization behavior.
    Flagged by ouroboros-agent on commit 4694da0.
    """
    answerer = AutoAnswerer()
    questions = (
        "Quels utilisateurs peuvent supprimer des branches?",
        "哪些用户可以删除分支?",
        "어떤 사용자가 브랜치를 삭제할 수 있나요?",
        "Welche Benutzer dürfen Branches löschen?",
        "¿Qué usuarios pueden eliminar ramas?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}

        assert answer.blocker is None, question
        # Product-behavior contract is preserved (constraints + acceptance) and
        # actor/IO assumptions are NOT injected.
        assert {"constraints", "acceptance_criteria"} <= updated_sections, (
            question,
            updated_sections,
        )
        assert "actors" not in updated_sections, question
        assert "inputs" not in updated_sections, question
        assert "outputs" not in updated_sections, question


def test_auto_answerer_cjk_property_lookup_does_not_misroute_to_runtime() -> None:
    """CJK runtime cues paired with bare noun-style "selection shape" tokens
    (``설정`` / ``設定`` / ``配置``) used to misroute property/status lookups
    like ``런타임 설정은 어디에 표시되나요?`` into ``_runtime_answer()``.
    Flagged by ouroboros-agent on commit 4c1ee42.  Selection shape now
    requires a verb-distinctive cue (``사용``/``선택``/``채택``/``도입`` etc.).
    """
    answerer = AutoAnswerer()
    questions = (
        "런타임 설정은 어디에 표시되나요?",
        "ランタイム設定はどこに表示されますか?",
        "运行时配置显示在哪里？",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert "runtime_context" not in updated_sections, (question, updated_sections)


def test_auto_answerer_english_user_actor_questions_route_to_actor_io() -> None:
    """Common English actor questions like ``"Who is the primary user?"`` and
    ``"Which user is the primary user?"`` must populate ``actors`` /
    ``inputs`` / ``outputs``.  Flagged by ouroboros-agent on commit f59d4e7
    after the actor cue list previously omitted ``user``/``users``.
    """
    answerer = AutoAnswerer()
    questions = (
        "Who is the primary user?",
        "Which user is the primary user?",
        "Who is the end user?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert {"actors", "inputs", "outputs"} <= updated_sections, (question, updated_sections)


def test_auto_answerer_multilingual_direct_lookup_routes_to_runtime() -> None:
    """Bare direct-lookup runtime questions in non-English languages must
    populate ``runtime_context``.  Flagged by ouroboros-agent on commit
    6a939bf — without a multilingual direct-lookup shape, examples like
    ``"¿Qué framework?"``, ``"ランタイムは何ですか?"``, and ``"框架是什么？"``
    fell through to ``_default_answer()``.
    """
    answerer = AutoAnswerer()
    questions = (
        "¿Qué framework?",
        "Quel framework ?",
        "Welches Framework?",
        "ランタイムは何ですか?",
        "框架是什么？",
        "런타임은 무엇인가요?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert "runtime_context" in updated_sections, (question, updated_sections)


def test_auto_answerer_cjk_questions_produce_distinct_ledger_keys() -> None:
    """``_slug_key()`` must keep Unicode letters so different CJK questions
    produce different ledger keys instead of all collapsing onto the
    fallback ``"requested_behavior"``.  Flagged by ouroboros-agent on
    commit 1581a7b — the contract boundary regression where multilingual
    routing landed correctly but ledger keys silently merged unrelated
    requirements together.
    """
    answerer = AutoAnswerer()
    question_a = "哪些用户可以删除分支?"
    question_b = "用户可以验证他们的电子邮件吗？"

    answer_a = answerer.answer(question_a, SeedDraftLedger.from_goal("Build a CLI"))
    answer_b = answerer.answer(question_b, SeedDraftLedger.from_goal("Build an auth service"))

    keys_a = sorted({entry.key for _section, entry in answer_a.ledger_updates})
    keys_b = sorted({entry.key for _section, entry in answer_b.ledger_updates})

    # Both questions must produce ``constraints.behavior.<subject>`` keys
    # (or ``acceptance.<subject>`` keys for acceptance routes).
    assert any(".behavior." in k or k.startswith("acceptance.") for k in keys_a), keys_a
    assert any(".behavior." in k or k.startswith("acceptance.") for k in keys_b), keys_b
    # Keys must NOT collapse onto the language-blind fallback.
    assert not any(k.endswith(".requested_behavior") for k in keys_a), keys_a
    assert not any(k.endswith(".requested_behavior") for k in keys_b), keys_b
    # Keys for two distinct questions must differ.
    assert keys_a != keys_b, (keys_a, keys_b)


def test_auto_answerer_acceptance_status_does_not_misroute_to_acceptance_route() -> None:
    """Bare ``"acceptance"`` substring must not classify property/status
    questions like ``"What is the acceptance status?"`` as
    ``ACCEPTANCE_CRITERIA``.  Flagged by ouroboros-agent on commit f59d4e7 —
    same false-positive class as ``output`` / ``repository`` substring
    matches.
    """
    answerer = AutoAnswerer()
    answer = answerer.answer(
        "What is the acceptance status?",
        SeedDraftLedger.from_goal("Build a CLI"),
    )
    updated_sections = {section for section, _entry in answer.ledger_updates}
    assert answer.blocker is None
    # Must fall through to the conservative default — no acceptance /
    # verification ledger sections written, no actor/IO injection.
    assert "acceptance_criteria" not in updated_sections, updated_sections
    assert "verification_plan" not in updated_sections, updated_sections


def test_auto_answerer_meta_verify_questions_stay_on_verification_route() -> None:
    """Meta-verification questions like ``"Should we verify users can reset
    passwords?"`` and ``"How should we validate admins can log in?"`` share
    the same actor-noun + permission-modal + verify-verb tokens as user
    feature questions, but the OUTER subject is engineering / first-person
    plural — they ask about QA, not a product feature.  Flagged by
    ouroboros-agent on commit 4ae40d4.  ``_has_user_verify_feature_shape``
    now requires the absence of a first-person-plural meta subject.
    """
    answerer = AutoAnswerer()
    questions = (
        "Should we verify users can reset passwords?",
        "How should we validate admins can log in?",
        "Devrions-nous vérifier que les utilisateurs peuvent se connecter?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build an auth service"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        # Verification route writes verification_plan + acceptance_criteria;
        # the product-behavior route would write constraints with a
        # ``constraints.behavior.<subject>`` key, which must NOT appear here.
        assert "verification_plan" in updated_sections, (question, updated_sections)
        for _section, entry in answer.ledger_updates:
            assert not entry.key.startswith("constraints.behavior."), (
                question,
                entry.key,
            )


def test_auto_answerer_user_verify_feature_routes_to_product_behavior() -> None:
    """User-verify feature questions like ``Can users verify their email?``
    must route to PRODUCT_BEHAVIOR instead of being collapsed into a generic
    verification-plan template.  Flagged by ouroboros-agent on commit
    4c1ee42 in English / French / Chinese / Korean variants.  Routing
    precedence now prefers PRODUCT_BEHAVIOR over VERIFICATION when both are
    inferred, and verify-style verbs are recognised as product actions.
    """
    answerer = AutoAnswerer()
    questions = (
        "Can users verify their email?",
        "Les utilisateurs peuvent-ils vérifier leur e-mail ?",
        "用户可以验证他们的电子邮件吗？",
        "사용자가 이메일을 확인할 수 있나요?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build an auth service"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert {"constraints", "acceptance_criteria"} <= updated_sections, (
            question,
            updated_sections,
        )
        # The product behavior subject must be surfaced in the constraint key.
        constraint_keys = [
            entry.key
            for section, entry in answer.ledger_updates
            if section == "constraints" and entry.key.startswith("constraints.behavior.")
        ]
        assert constraint_keys, (question, [k for _s, e in answer.ledger_updates for k in [e.key]])


def test_auto_answerer_broad_design_cues_do_not_misroute_to_runtime() -> None:
    """Broad design nouns (``architecture``, ``estructura``, ``cadre``) plus a
    generic selection verb must NOT be classified as runtime intent.  The
    previous cue list paired ``estructura`` with selection verbs like
    ``usamos`` and silently routed design questions such as
    ``¿Qué estructura usamos para los datos?`` into ``_runtime_answer()``.
    Flagged by ouroboros-agent on commit 0230bab.  Cues are now anchored to
    repository-/runtime-specific phrases only.
    """
    answerer = AutoAnswerer()
    questions = (
        "¿Qué estructura usamos para los datos?",
        "Quelle architecture utilisons-nous pour le rapport?",
        "What architecture should the report use?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert "runtime_context" not in updated_sections, (question, updated_sections)


def test_auto_answerer_substring_cues_do_not_misroute_unrelated_words() -> None:
    """Bare ASCII substring cues (e.g. ``"test"``) must not match unrelated
    words (``"contest"``, ``"latest"``, ``"protest"``, ``"attestations"``).
    Flagged by ouroboros-agent on commit 52a9ee7 as a silent verification
    misrouting regression.  Cue matching now uses regex word boundaries for
    ASCII Latin cues.
    """
    answerer = AutoAnswerer()
    questions = (
        "Should users contest charges?",
        "What is the latest output path?",
        "How are protest votes counted?",
    )
    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}
        assert answer.blocker is None, question
        assert "verification_plan" not in updated_sections, (question, updated_sections)


def test_auto_answerer_german_ascii_transliteration_routes_to_product_behavior() -> None:
    """German ASCII transliterations (``duerfen``/``loeschen``) must be treated
    the same as their umlauted forms.  Flagged by ouroboros-agent on commit
    52a9ee7 as a realistic silent-misrouting gap because most German keyboards
    on dev workstations type ``ue``/``ae``/``oe`` rather than ``ü``/``ä``/``ö``.
    """
    answerer = AutoAnswerer()
    answer = answerer.answer(
        "Welche Benutzer duerfen Branches loeschen?",
        SeedDraftLedger.from_goal("Build a CLI"),
    )
    updated_sections = {section for section, _entry in answer.ledger_updates}
    assert answer.blocker is None
    assert {"constraints", "acceptance_criteria"} <= updated_sections
    assert "actors" not in updated_sections
    assert "inputs" not in updated_sections
    assert "outputs" not in updated_sections


def test_auto_answerer_property_lookup_cues_do_not_misroute_to_intent_handlers() -> None:
    """Broad cue substrings (``input``, ``output``, ``repository``, ``architecture``)
    must not by themselves trigger ACTOR_IO or RUNTIME_CONTEXT routing.  These
    questions are property/status lookups, not contract or selection questions,
    and would otherwise mutate ledger state with the wrong contract — the exact
    silent misrouting flagged by ouroboros-agent on commit 9ab5ae1.
    """
    answerer = AutoAnswerer()
    questions = (
        "What is the output directory?",
        "What is the input schema?",
        "What is the repository status?",
        "What architecture decisions are documented?",
    )

    for question in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a CLI"))
        updated_sections = {section for section, _entry in answer.ledger_updates}

        assert answer.blocker is None, question
        assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT, question
        # Routing must not write actor/IO or runtime ledger entries for these
        # property-shaped questions — the safe default is the only outcome.
        assert "actors" not in updated_sections, question
        assert "inputs" not in updated_sections, question
        assert "outputs" not in updated_sections, question
        assert "runtime_context" not in updated_sections, question


def test_auto_answerer_unknown_multilingual_question_uses_safe_default() -> None:
    answer = AutoAnswerer().answer(
        "¿Cuál es el color favorito del tablero?",
        SeedDraftLedger.from_goal("Build a dashboard"),
    )

    assert answer.blocker is None
    assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT
    assert [entry.key for _section, entry in answer.ledger_updates] == [
        "constraints.conservative_mvp",
        "failure_modes.unverified_or_scope_creep",
    ]


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


def test_auto_answerer_blocks_regulated_data_questions_instead_of_falling_back() -> None:
    answerer = AutoAnswerer()
    questions = (
        ("What PII should the system collect?", "regulated personal data handling"),
        (
            "Which fields are HIPAA regulated and how should we store them?",
            "regulated data handling",
        ),
        ("How should the migration purge tables for old users?", "destructive bulk data operation"),
    )

    for question, reason in questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == reason, question


def test_auto_answerer_does_not_block_regulated_topic_when_repo_fact_supplied() -> None:
    answerer = AutoAnswerer()
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Compliance worker on Python 3.14 (HIPAA-aware)"},
        evidence={"runtime_context": ("docs/compliance.md",)},
    )

    answer = answerer.answer(
        "Which runtime should the HIPAA worker use?",
        SeedDraftLedger.from_goal("Build a HIPAA worker"),
        context,
    )

    assert answer.source == AutoAnswerSource.REPO_FACT
    assert answer.blocker is None


def test_auto_answerer_skips_risky_fallback_for_safe_product_credential_questions() -> None:
    answerer = AutoAnswerer()
    questions = (
        "Should users be able to configure production credentials?",
        "Should the app store production credentials?",
    )

    for question in questions:
        answer = answerer.answer(
            question, SeedDraftLedger.from_goal("Build credential management settings")
        )
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_does_not_block_meta_questions_that_mention_regulated_topics() -> None:
    """Acceptance/verification meta-questions must not be gated by keyword match.

    Phrasing such as 'What acceptance criteria should the HIPAA worker satisfy?'
    or 'Which command output verifies the GDPR export flow?' is asking for an
    acceptance template or a verification plan, not for regulated-data
    handling decisions.  These routes are safe templates and predate the
    risky-fallback gate; they must continue to return non-blocker answers.
    """
    answerer = AutoAnswerer()
    feature_acceptance_questions = (
        "What acceptance criteria should the HIPAA worker satisfy?",
        "What acceptance criteria should the GDPR exporter satisfy?",
        "What acceptance criteria should the PII pipeline satisfy?",
    )

    for question in feature_acceptance_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.blocker is None, question
        assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT, question

    verification_questions = (
        "Which command output verifies the GDPR export flow?",
        "How should we verify the HIPAA worker tests pass?",
        "What is the verification plan for PII handling?",
    )

    for question in verification_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.blocker is None, question
        assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT, question


def test_auto_answerer_blocks_existing_convention_runtime_fallback_for_regulated_topic() -> None:
    """Generic 'use the existing repo runtime' fallback must also block for regulated topics.

    ``_runtime_answer`` returns ``AutoAnswerSource.EXISTING_CONVENTION`` when
    no concrete ``runtime_context`` repo fact is supplied.  The answer text
    is still a generic template, so a regulated runtime question without
    grounded facts must be gated like any other fallback path.
    """
    answer = AutoAnswerer().answer(
        "Which runtime should the HIPAA worker use?",
        SeedDraftLedger.from_goal("Build a HIPAA worker"),
    )

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert answer.blocker.reason == "regulated data handling"


def test_auto_answerer_blocks_destructive_bulk_operations_in_either_order() -> None:
    """Destructive bulk operations must be blocked regardless of verb/noun order.

    The previous matcher only caught ``verb ... noun`` phrasings such as
    ``purge tables``, so reversed phrasings ``Which tables should the
    migration truncate?`` slipped through. Broaden the verb vocabulary
    (truncate/purge/wipe with their tense variants) and the noun list
    (tables/schemas/databases/indexes/migrations) and cover both orders.
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        # verb-then-noun
        "How should the migration purge tables for old users?",
        "Should we wipe the user_data schema during the rollout?",
        "How should the system truncate the audit databases?",
        "Which tables should the migration drop?",
        "Should we erase these schemas before re-seeding?",
        # noun-then-verb (reverse phrasing)
        "Which tables should the migration truncate?",
        "Which schemas should the cleanup script purge?",
        "Which migrations should we wipe before redeploying?",
        "Which schemas should the data team erase tomorrow?",
    )

    for question in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build cleanup tooling"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == "destructive bulk data operation", question


def test_auto_answerer_allows_release_plan_drop_question() -> None:
    """Process-artefact drop questions must NOT trigger the destructive-bulk gate.

    ``Which migration should we drop from the release plan?`` is asking about
    removing a migration from a planning artefact, not about schema destruction.
    The non-data qualifier ``release plan`` must exempt the match.

    Ref: ouroboros-agent[bot] follow-up warning on #738 — ``answerer.py:666``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Which migration should we drop from the release plan?",
        "Which migrations should we drop from the release plan?",
        "Should we drop this migration from the release plan?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a release pipeline"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_allows_docs_index_drop_question() -> None:
    """Documentation-index drop questions must NOT trigger the destructive-bulk gate.

    ``Which indexes should we drop from the docs?`` is asking about removing
    entries from documentation, not about dropping database indexes.
    The non-data qualifier ``from the docs`` must exempt the match.

    Ref: ouroboros-agent[bot] follow-up warning on #738 — ``answerer.py:666``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Which indexes should we drop from the docs?",
        "Which index should we drop from the documentation?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a docs site"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_blocks_destructive_bulk_with_only_documentation_reference() -> None:
    """Authority/reference-style mentions of documentation must NOT bypass the gate.

    Earlier the non-data qualifier regex matched on bare tokens such as
    ``documentation`` or ``release plan`` anywhere in the sentence. That allowed
    real destructive operations to slip past the gate when the question merely
    *referenced* documentation as an authority (e.g. ``according to the
    documentation``, ``per the release plan``) rather than describing the
    documentation as the artefact being modified.

    The qualifier is now phrase-scoped to ``from the …``/``in the …`` so the
    exemption fires only when the artefact is the explicit object of the
    drop/wipe — i.e. ``drop X from the docs`` or ``drop X in the roadmap``,
    not ``drop X according to the docs``.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:688``.
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        "Which tables should we drop according to the documentation before redeploying?",
        "Which tables should we drop per the release plan?",
        "Per the documentation, which audit logs should we purge?",
        "According to the docs, which tables should we drop?",
        "Following the release plan, which schemas should the data team erase?",
    )

    for question in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build cleanup tooling"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == "destructive bulk data operation", question


def test_auto_answerer_blocks_destructive_bulk_with_ambiguous_singular_tokens() -> None:
    """Standalone ``doc`` and ``plan`` tokens are too ambiguous to exempt.

    ``from the doc`` is rare phrasing (use ``from the docs`` or ``from the
    documentation``), and bare ``plan`` collides with database-side meanings
    (query plan, execution plan, db plan). Both have been removed from the
    non-data qualifier list. Only the unambiguous artefact phrasings
    (``release plan``, ``docs``, ``documentation``, ``roadmap``, ``backlog``,
    ``changelog``, ``spec``) exempt the destructive-bulk gate.
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        "Which tables should we drop from the plan before redeploying?",
        "Which schemas should we wipe in the plan after migration?",
        "Which tables should we drop from the doc before the cutover?",
    )

    for question in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build cleanup tooling"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == "destructive bulk data operation", question


def test_auto_answerer_allows_in_the_artefact_drop_questions() -> None:
    """``in the …`` artefact phrasings must also exempt the destructive-bulk gate.

    The qualifier accepts both ``from the …`` and ``in the …`` artefact phrasings
    so that "Which indexes should we drop in the docs?" and "Which migration
    should we drop in the roadmap?" are recognised as process-artefact edits and
    not blocked as destructive data operations.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:698``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Which indexes should we drop in the docs?",
        "Which migration should we drop in the roadmap?",
        "Which migration should we drop in the release plan?",
        "Which tables should we drop in the changelog?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Maintain release docs"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_allows_product_semantics_questions_for_regulated_topics() -> None:
    """Product-feature questions that mention regulated nouns must NOT be blocked.

    The risky-fallback gate targets compliance-policy decisions (how to store,
    handle, retain, collect PII/HIPAA/GDPR data).  Questions that ask for
    bounded product-behavior semantics — exporting a PII report, downloading a
    GDPR export, showing SOX audit data — are asking for feature-level behavior
    and must flow through to a generative product answer instead of BLOCKER.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:716``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Should the app export PII reports?",
        "Should users be able to download GDPR exports?",
        "Should the dashboard display HIPAA audit data?",
        "Should the system expose a SOX compliance report endpoint?",
        "Should admins be able to view PII fields in the admin panel?",
        "Should the app allow users to access their GDPR data?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_allows_product_questions_with_adjectival_compliance_verbs() -> None:
    """Past-participle compliance verbs (``stored``, ``encrypted``) acting as
    adjectives must NOT reject a product-behavior question.

    The earlier allowlist rejected any question containing a compliance-policy
    verb anywhere in the text. That over-blocked legitimate product-semantics
    questions where the compliance verb appears as a past-participle adjective
    modifying the noun (``view stored PII fields``, ``display encrypted HIPAA
    files``). The main verb of the sentence is the product-semantics one
    (``view`` / ``display``), so the question is asking for product behavior
    over already-existing regulated data, not for a compliance-policy decision.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:782``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Should admins be able to view stored PII fields?",
        "Should the dashboard display encrypted HIPAA files?",
        "Should users be able to download retained GDPR exports?",
        "Should the app show collected SOX records to auditors?",
        "Can the audit panel display shared PII access events?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_routes_regulated_product_questions_before_io_or_runtime() -> None:
    """Regulated-product questions must reach ``_product_behavior_answer()`` —
    but only when the IO/runtime branch would otherwise return a non-grounded
    fallback.

    The router still checks ``_is_actor_or_io_question`` and
    ``_is_runtime_context_question`` first so grounded answers (REPO_FACT for
    runtime, ASSUMPTION-but-confirmed for IO) keep priority. When that route
    produces a *non-grounded* fallback (``ASSUMPTION``,
    ``EXISTING_CONVENTION``, ``CONSERVATIVE_DEFAULT``) for a question that
    ``_is_safe_product_regulated_question`` recognises, the answer is then
    re-routed through ``_product_behavior_answer()`` so the regulated-feature
    semantics (regulated noun, subject-specific constraints) are preserved
    in the ledger instead of being replaced by a generic IO/runtime template.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:837``.
    """
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a regulated data app")

    cases = [
        ("What inputs should the GDPR export take?", "gdpr"),
        ("What outputs should the PII export produce?", "pii"),
        ("Which runtime should the GDPR export use?", "gdpr"),
        ("What inputs should the HIPAA audit log download accept?", "hipaa"),
    ]

    for question, regulated_noun in cases:
        answer = answerer.answer(question, ledger)

        # Routed away from blocker
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question

        # Routed to _product_behavior_answer(), not _io_actor_answer / _runtime_answer
        update_keys = [u[1].key for u in answer.ledger_updates]
        assert any("behavior." in k for k in update_keys), (
            f"Expected behavior.* ledger key for {question!r}, got {update_keys}"
        )
        assert not any(k.startswith("io.") or k.startswith("runtime.") for k in update_keys), (
            f"Question {question!r} fell through to IO/runtime answer "
            f"(found IO/runtime ledger key in {update_keys})"
        )

        # Regulated noun preserved in answer text or ledger value
        combined = answer.text + " ".join(u[1].value for u in answer.ledger_updates)
        assert regulated_noun in combined.lower(), (
            f"Regulated noun {regulated_noun!r} not preserved for {question!r}"
        )


def test_auto_answerer_preserves_repo_fact_for_regulated_runtime_question() -> None:
    """The regulated-product reroute must NOT override grounded
    ``REPO_FACT`` runtime answers.

    ``Which runtime should the GDPR export use?`` is a regulated-product
    question, but when the caller supplies a concrete ``runtime_context``
    repo fact, the runtime contract requires the answer to carry that
    grounded evidence (``AutoAnswerSource.REPO_FACT``,
    ``LedgerSource.REPO_FACT``, runtime_context ledger entry with the
    supplied evidence). The reroute is gated on
    ``answer.source in _RISKY_FALLBACK_SOURCES``, and ``REPO_FACT`` is
    not a risky-fallback source, so this case must remain unchanged.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:126``.
    """
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a GDPR-compliant export pipeline")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python 3.12 project managed with uv and Typer CLI."},
        evidence={"runtime_context": ("pyproject.toml", "src/ouroboros/cli/main.py")},
    )

    answer = answerer.answer("Which runtime should the GDPR export use?", ledger, context)

    assert answer.source == AutoAnswerSource.REPO_FACT
    assert "Python 3.12" in answer.text

    update_sections = {section for section, _ in answer.ledger_updates}
    assert "runtime_context" in update_sections, (
        "Grounded runtime_context entry was dropped — regulated-product reroute "
        "must not override REPO_FACT runtime answers."
    )

    runtime_entries = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert runtime_entries, "Expected at least one runtime_context ledger entry"
    assert runtime_entries[0].source == LedgerSource.REPO_FACT
    assert runtime_entries[0].status == LedgerStatus.CONFIRMED
    assert runtime_entries[0].evidence == ["pyproject.toml", "src/ouroboros/cli/main.py"]


def test_auto_answerer_blocks_bare_compliance_scope_questions() -> None:
    """``support|enable|allow + bare regulated noun`` is a compliance-scope
    decision, not a product-behavior question.

    Prompts that frame the entire regulatory regime as a binary feature flag —
    "Should the platform support HIPAA?", "Should the app enable GDPR?",
    "Should the system allow PII?" — are compliance-policy decisions and must
    remain blocked even though they contain a product-question modal and a
    product-semantics verb. Concrete-feature variants ("support HIPAA audit
    logs", "enable GDPR consent banners", "allow GDPR data exports") still
    pass through.

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:793``.
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        ("Should the platform support HIPAA?", "regulated data handling"),
        ("Should the app enable GDPR?", "regulated data handling"),
        ("Should the system allow PII?", "regulated personal data handling"),
        ("Should the service support SOX?", "regulated data handling"),
        ("Should the platform support PCI-DSS?", "regulated data handling"),
    )

    for question, reason in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == reason, question


def test_auto_answerer_allows_qualified_compliance_scope_questions() -> None:
    """The bare-scope rejector must not over-block concrete-feature variants.

    When the regulated noun is followed by a qualifying feature noun
    ("HIPAA audit logs", "GDPR consent banners", "PII redaction"), the
    question is asking for bounded product behavior over a specific feature
    and must still pass through to ``_product_behavior_answer()``.
    """
    answerer = AutoAnswerer()
    allowed_questions = (
        "Should the platform support HIPAA audit logs?",
        "Should the app enable GDPR consent banners?",
        "Should the system allow PII redaction in exports?",
        "Should the service support SOX compliance reporting?",
        "Should the app allow users to access their GDPR data?",
    )

    for question in allowed_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.blocker is None, question
        assert answer.source != AutoAnswerSource.BLOCKER, question


def test_auto_answerer_blocks_mixed_intent_regulated_questions() -> None:
    """Mixed-intent questions with both product-semantics and active-form
    compliance verbs must remain blocked.

    Active-form compliance verbs (``store`` / ``stores`` / ``storing``,
    ``retain`` / ``retains`` / ``retaining``, ``encrypt``, ``handle``, …)
    indicate that the question is asking the pipeline to decide regulated-data
    handling, even when it also mentions a product-semantics verb. The
    allowlist must not unblock those.

    Past-participle forms (``stored``, ``encrypted``) acting as adjectives are
    a different case and remain allowed (covered by
    ``test_auto_answerer_allows_product_questions_with_adjectival_compliance_verbs``).

    Ref: ouroboros-agent[bot] BLOCKING on #738 — ``answerer.py:750``.
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        ("How should the system store and display HIPAA files?", "regulated data handling"),
        ("Should we retain and export PII records?", "regulated personal data handling"),
        (
            "Should the platform encrypt and render GDPR exports?",
            "regulated data handling",
        ),
        (
            "Can the system share and display SOX audit data with auditors?",
            "regulated data handling",
        ),
        (
            "Should we collect and show new PII fields on the dashboard?",
            "regulated personal data handling",
        ),
    )

    for question, reason in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == reason, question


def test_auto_answerer_still_blocks_compliance_policy_regulated_questions() -> None:
    """Compliance-policy questions must still be blocked after the product-semantics allowlist.

    Ensure the new ``_is_safe_product_regulated_question`` allowlist does not
    swallow questions that genuinely ask the auto pipeline to decide regulated-data
    handling (storage, retention, collection, encryption policy).
    """
    answerer = AutoAnswerer()
    blocked_questions = (
        ("What PII should the system collect?", "regulated personal data handling"),
        (
            "Which fields are HIPAA regulated and how should we store them?",
            "regulated data handling",
        ),
        ("How should the system handle GDPR data retention?", "regulated data handling"),
        ("Which runtime should the HIPAA worker use?", "regulated data handling"),
    )

    for question, reason in blocked_questions:
        answer = answerer.answer(question, SeedDraftLedger.from_goal("Build a regulated data app"))
        assert answer.source == AutoAnswerSource.BLOCKER, question
        assert answer.blocker is not None, question
        assert answer.blocker.reason == reason, question


def test_auto_answerer_routes_safe_regulated_product_questions_to_product_behavior_answerer() -> (
    None
):
    """Safe regulated-product questions must route to _product_behavior_answer(), not _default_answer().

    When _is_safe_product_regulated_question() passes a question through the risky-fallback
    gate, _is_product_behavior_question() must also return True so the router at answerer.py:122
    sends the question to _product_behavior_answer().  If that alignment is missing the question
    silently falls to _default_answer(), which produces a generic conservative-MVP ledger entry
    that discards the regulated-product feature semantics.

    Three bot example questions from ouroboros-agent[bot] BLOCKING on #738 (answerer.py:741):
    - ``Should users be able to download GDPR exports?``
    - ``Should admins be able to view PII fields in the admin panel?``
    - ``Should the app allow users to access their GDPR data?``

    Assertions:
    1. answer.blocker is None  (gate passes)
    2. answer.source == CONSERVATIVE_DEFAULT  (product-behavior, not BLOCKER)
    3. ledger_updates contain a subject-specific key (not the generic conservative_mvp key
       from _default_answer), confirming the regulated-product feature is preserved.
    4. The ledger entry value or the answer text includes the regulated noun
       (gdpr / pii) so the subject is not silently stripped.
    """
    answerer = AutoAnswerer()
    ledger = SeedDraftLedger.from_goal("Build a regulated data app")

    # Cover every verb in ``_PRODUCT_SEMANTICS_REGULATED_VERBS_RE`` so the
    # allowlist↔router alignment is locked in and cannot drift silently for
    # any single verb.
    cases = [
        ("Should users be able to download GDPR exports?", "gdpr"),
        ("Should admins be able to view PII fields in the admin panel?", "pii"),
        ("Should the app allow users to access their GDPR data?", "gdpr"),
        ("Should the app export PII reports?", "pii"),
        ("Should the app show PII reports?", "pii"),
        ("Should the dashboard display HIPAA audit data?", "hipaa"),
        ("Should the system render GDPR consent notices?", "gdpr"),
        ("Should the platform expose a SOX compliance report endpoint?", "sox"),
        ("Should the app support PII redaction in exports?", "pii"),
        ("Should the system enable HIPAA audit log download?", "hipaa"),
    ]

    for question, regulated_noun in cases:
        answer = answerer.answer(question, ledger)

        # Gate passed — not blocked
        assert answer.blocker is None, question
        assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT, question

        # Routed to _product_behavior_answer() — ledger has subject-specific keys,
        # not the generic "constraints.conservative_mvp" key from _default_answer().
        update_keys = [u[1].key for u in answer.ledger_updates]
        assert any("behavior." in k for k in update_keys), (
            f"Expected subject-specific behavior key in ledger updates for {question!r}, "
            f"got {update_keys}"
        )
        assert not any(k == "constraints.conservative_mvp" for k in update_keys), (
            f"Question {question!r} fell through to _default_answer() (conservative_mvp key found)"
        )

        # Regulated noun preserved in the answer text or a ledger entry value
        combined = answer.text + " ".join(u[1].value for u in answer.ledger_updates)
        assert regulated_noun in combined.lower(), (
            f"Regulated noun {regulated_noun!r} not found in answer/ledger for {question!r}"
        )


def test_ledger_summary_groups_active_sections_by_provenance_source() -> None:
    ledger = SeedDraftLedger.from_goal("Build hello CLI")
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.repo_fact",
            value="Python 3.14",
            source=LedgerSource.REPO_FACT,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            evidence=["pyproject.toml"],
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.mvp",
            value="Smallest safe MVP",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    ledger.add_entry(
        "actors",
        LedgerEntry(
            key="actors.assumed",
            value="Single local user",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.DEFAULTED,
        ),
    )

    summary = ledger.summary()
    provenance = summary["provenance"]

    assert "runtime_context" in provenance["repo_fact"]
    assert "goal" in provenance["user_goal"]
    assert "constraints" in provenance["conservative_default"]
    assert "actors" in provenance["assumption"]
    assert "runtime_context" in summary["evidence_backed_sections"]
    assert "goal" in summary["evidence_backed_sections"]
    assert "constraints" in summary["assumption_only_sections"]
    assert "actors" in summary["assumption_only_sections"]
    assert "runtime_context" not in summary["assumption_only_sections"]


def test_ledger_summary_excludes_inactive_entries_from_provenance() -> None:
    ledger = SeedDraftLedger.from_goal("Build hello CLI")
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.weak_guess",
            value="maybe Python",
            source=LedgerSource.REPO_FACT,
            confidence=0.4,
            status=LedgerStatus.WEAK,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.blocked",
            value="needs human input",
            source=LedgerSource.BLOCKER,
            confidence=1.0,
            status=LedgerStatus.BLOCKED,
        ),
    )

    summary = ledger.summary()
    provenance = summary["provenance"]

    assert "runtime_context" not in provenance.get("repo_fact", [])
    assert "constraints" not in provenance.get("blocker", [])
    assert "runtime_context" not in summary["evidence_backed_sections"]
    assert "runtime_context" not in summary["assumption_only_sections"]
    assert "constraints" not in summary["assumption_only_sections"]


def test_ledger_summary_excludes_unresolved_sections_from_classification() -> None:
    """Sections that are not aggregate-resolved must not surface as grounded.

    A section can carry both a resolved entry (DEFAULTED) and a later blocker
    (BLOCKED), in which case ``LedgerSection.status()`` returns ``BLOCKED``.
    The provenance summary must respect the section's aggregate status so
    consumers do not see ``constraints`` listed as "evidence-backed" or
    "assumption-only" while the section is actually unresolved.
    """
    ledger = SeedDraftLedger.from_goal("Build hello CLI")
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.mvp",
            value="Smallest safe MVP",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="blocker.constraints",
            value="needs human input",
            source=LedgerSource.BLOCKER,
            confidence=1.0,
            status=LedgerStatus.BLOCKED,
        ),
    )

    summary = ledger.summary()
    assert ledger.sections["constraints"].status() == LedgerStatus.BLOCKED
    assert "constraints" not in summary["evidence_backed_sections"]
    assert "constraints" not in summary["assumption_only_sections"]
    assert "constraints" in summary["open_gaps"]
    # The raw provenance dict must also exclude unresolved sections so MCP
    # consumers cannot attribute a source to a section the ledger considers
    # blocked.
    for source_sections in summary["provenance"].values():
        assert "constraints" not in source_sections


def test_ledger_summary_treats_non_goal_entries_as_evidence_backed() -> None:
    """Explicit non-goals are user-stated policy, not bare assumptions.

    Both user-supplied non-goals (CONFIRMED) and auto-defaulted non-goals
    (DEFAULTED) represent deliberate scope boundaries, so the section should
    surface as evidence-backed rather than assumption-only.
    """
    explicit_ledger = SeedDraftLedger.from_goal(
        "Build hello CLI. Non-goals are cloud sync and authentication."
    )
    explicit_summary = explicit_ledger.summary()

    assert "non_goals" in explicit_summary["provenance"].get("non_goal", [])
    assert "non_goals" in explicit_summary["evidence_backed_sections"]
    assert "non_goals" not in explicit_summary["assumption_only_sections"]

    defaulted_ledger = SeedDraftLedger.from_goal("Build hello CLI")
    defaulted_ledger.add_entry(
        "non_goals",
        LedgerEntry(
            key="non_goals.mvp_scope",
            value="No cloud sync; no paid services.",
            source=LedgerSource.NON_GOAL,
            confidence=0.86,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    defaulted_summary = defaulted_ledger.summary()

    assert "non_goals" in defaulted_summary["evidence_backed_sections"]
    assert "non_goals" not in defaulted_summary["assumption_only_sections"]


def test_ledger_summary_treats_inference_only_sections_as_assumption_only() -> None:
    """Inference is a model-derived guess, not anchored evidence.

    A section resolved purely from INFERENCE entries must surface in
    ``assumption_only_sections``, never in ``evidence_backed_sections`` —
    otherwise the surface would present speculative content as grounded fact
    and defeat the trust-signal purpose of the split.
    """
    ledger = SeedDraftLedger.from_goal("Build hello CLI")
    ledger.add_entry(
        "actors",
        LedgerEntry(
            key="actors.inferred",
            value="Local developer",
            source=LedgerSource.INFERENCE,
            confidence=0.7,
            status=LedgerStatus.INFERRED,
        ),
    )

    summary = ledger.summary()

    assert "actors" in summary["provenance"].get("inference", [])
    assert "actors" not in summary["evidence_backed_sections"]
    assert "actors" in summary["assumption_only_sections"]
