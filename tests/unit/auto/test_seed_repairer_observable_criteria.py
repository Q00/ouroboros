"""Seed repair keeps acceptance criteria specific and readable (#3045).

The grade gate flags a criterion that names no observable check. The repair
used to wrap every such criterion in one fixed English sentence ("A command/API
check returns stable observable output or artifacts proving the original
requirement for <criterion>"), so every criterion read the same, the original
sentence lost its leading capital mid-sentence, and Korean criteria were
wrapped in English. These tests pin the replacement contract: the requester's
sentence stays first and verbatim, one clause in its own language names the
evidence that proves it, and the result passes the same observability check
that flagged it, so the loop still converges in one round.
"""

from __future__ import annotations

import re

import pytest

from ouroboros.auto.grading import SeedGrade, _is_observable, _is_vague
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.seed_qa_repair import contains_seed_qa_repair_instruction
from ouroboros.auto.seed_repairer import (
    OBSERVABLE_CHECK_SEPARATOR,
    SeedRepairer,
    _observable_preserving_replacement,
)
from ouroboros.auto.seed_reviewer import SeedReviewer
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_text,
)

# The goal and two of the criteria from the Auto run recorded in #3045.
_MD2CSV_GOAL = (
    "Build a Python CLI that converts Markdown tables to CSV "
    "(file or stdin in, CSV to stdout), with pytest tests."
)
_CLI_AC = (
    "A Python CLI exists that converts Markdown table input supplied as a file path "
    "or stdin into CSV on stdout"
)
_TESTS_AC = "Pytest tests cover the Markdown-table-to-CSV CLI behavior"
_KO_GOAL = "마크다운 표를 CSV로 바꾸는 Python CLI를 만든다"
_KO_AC = "Python CLI가 마크다운 표를 CSV로 변환한다"
_LEGACY_WRAPPER_START = "A command/API check returns stable observable output or artifacts"

_EN_COMMAND = "verified when running the command prints the expected output"
_EN_TEST = "verified when the test suite passes"
_EN_API = "verified when an API request returns the expected response"
_EN_FILE = "verified when the expected file is written"
_KO_COMMAND = "명령을 실행하면 기대한 출력이 나오는지 확인한다"
_KO_TEST = "테스트를 실행해 통과하는지 확인한다"
_KO_API = "API 요청이 기대한 응답을 반환하는지 확인한다"
_KO_FILE = "기대한 파일이 생성되는지 확인한다"


def _ready_ledger(goal: str) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(goal)
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )
    return ledger


def _seed(goal: str, criteria: tuple[str, ...]) -> Seed:
    return Seed(
        goal=goal,
        constraints=("Python 3 standard library only",),
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(
            name="Md2Csv",
            description="Markdown table to CSV converter",
            fields=(OntologyField(name="table", field_type="string", description="Table"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


def _converge(goal: str, criteria: tuple[str, ...]) -> tuple[Seed, Seed, SeedGrade, int]:
    seed = _seed(goal, criteria)
    reviewer = SeedReviewer()
    repaired, review, history = SeedRepairer(reviewer=reviewer).converge(
        seed, ledger=_ready_ledger(goal)
    )
    assert review.may_run
    return seed, repaired, review.grade_result.grade, len(history)


def test_observed_criteria_stay_specific_after_one_repair_round() -> None:
    seed = _seed(_MD2CSV_GOAL, (_CLI_AC, _TESTS_AC))
    first_review = SeedReviewer().review(seed, ledger=_ready_ledger(_MD2CSV_GOAL))
    # The trigger: the gate finds neither criterion observable.
    assert [finding.code for finding in first_review.findings] == [
        "missing_success_contract",
        "missing_success_contract",
    ]

    _seed_before, repaired, grade, rounds = _converge(_MD2CSV_GOAL, (_CLI_AC, _TESTS_AC))

    assert (grade, rounds) == (SeedGrade.A, 1)
    texts = [ac_text(criterion) for criterion in repaired.acceptance_criteria]
    # Each criterion keeps its own sentence first, with its own casing, and
    # names the evidence that fits it: the CLI by its output, the tests by the
    # test run.
    assert texts == [
        f"{_CLI_AC}{OBSERVABLE_CHECK_SEPARATOR}{_EN_COMMAND}",
        f"{_TESTS_AC}{OBSERVABLE_CHECK_SEPARATOR}{_EN_TEST}",
    ]
    for text in texts:
        assert _LEGACY_WRAPPER_START not in text
        assert "original requirement" not in text
        assert _is_observable(text)
    # The console's run card truncates each tile; the openings stay distinct.
    assert len({text[:40] for text in texts}) == len(texts)


def test_repair_writes_no_repair_instruction_into_the_seed() -> None:
    """#3036: the repair edits criteria only, and never with its own instructions."""
    seed, repaired, _grade, _rounds = _converge(_MD2CSV_GOAL, (_CLI_AC, _TESTS_AC))

    assert repaired.constraints == seed.constraints
    for text in (*repaired.constraints, *(ac_text(c) for c in repaired.acceptance_criteria)):
        assert not contains_seed_qa_repair_instruction(text)


def test_korean_criterion_gets_a_korean_clause_and_converges() -> None:
    _seed_before, repaired, grade, rounds = _converge(_KO_GOAL, (_KO_AC,))

    assert (grade, rounds) == (SeedGrade.A, 1)
    text = ac_text(repaired.acceptance_criteria[0])
    assert text == f"{_KO_AC}{OBSERVABLE_CHECK_SEPARATOR}{_KO_COMMAND}"
    clause = text.removeprefix(_KO_AC + OBSERVABLE_CHECK_SEPARATOR)
    assert not re.search(r"[A-Za-z]", clause)
    assert _is_observable(text)


def test_korean_criterion_that_names_its_evidence_is_left_alone() -> None:
    criterion = "`md2csv table.md`를 실행하면 stdout에 `a,b`가 출력된다"

    seed, repaired, grade, rounds = _converge(_KO_GOAL, (criterion,))

    assert (grade, rounds) == (SeedGrade.A, 0)
    assert repaired.acceptance_criteria == seed.acceptance_criteria


@pytest.mark.parametrize(
    "criterion",
    [
        "`md2csv table.md`를 실행하면 stdout에 `a,b`가 출력된다",
        "pytest 테스트가 모두 통과한다",
        "API 엔드포인트가 상태 코드 200으로 응답한다",
        "변환 결과가 out.csv 파일로 저장된다",
        "잘못된 입력이면 종료 코드 1로 끝난다",
    ],
)
def test_grade_gate_accepts_korean_criteria_that_name_their_evidence(criterion: str) -> None:
    assert _is_observable(criterion)


@pytest.mark.parametrize(
    "criterion",
    [
        _KO_AC,
        "사용자가 표를 쉽게 변환할 수 있다",
        "종료 코드를 처리한다",
        "pytest 테스트가 변환 동작을 다룬다",
    ],
)
def test_grade_gate_still_rejects_korean_criteria_that_name_no_check(criterion: str) -> None:
    assert not _is_observable(criterion)


@pytest.mark.parametrize(
    ("criterion", "goal", "clause"),
    [
        (_TESTS_AC, _MD2CSV_GOAL, _EN_TEST),
        ("Todos can be created and listed", "Build a REST API for todos", _EN_API),
        ("The exporter writes the summary to report.csv", "Build a reporting tool", _EN_FILE),
        # A criterion that names no evidence takes the goal's product surface,
        # not its "with pytest tests" rider.
        ("Rows with mismatched column counts are rejected", _MD2CSV_GOAL, _EN_COMMAND),
        ("Rows with mismatched column counts are rejected", "Convert Markdown", _EN_COMMAND),
        ("pytest 테스트가 변환 동작을 다룬다", _KO_GOAL, _KO_TEST),
        # An English word joined to a Korean particle ("pytest로", "API로")
        # still names the evidence.
        ("pytest로 변환 동작을 검사한다", _KO_GOAL, _KO_TEST),
        ("변환 결과는 표의 행 순서를 유지한다", "REST API로 표를 변환하는 서비스", _KO_API),
        ("REST 엔드포인트가 할 일 목록을 다룬다", "할 일 관리 서비스", _KO_API),
        ("변환 결과는 표의 행 순서를 유지한다", "마크다운 표를 CSV 파일로 저장하는 도구", _KO_FILE),
        (_KO_AC, _KO_GOAL, _KO_COMMAND),
    ],
)
def test_clause_names_the_evidence_the_criterion_is_about(
    criterion: str, goal: str, clause: str
) -> None:
    repaired = _observable_preserving_replacement(criterion, index=0, goal=goal)

    assert repaired == f"{criterion}{OBSERVABLE_CHECK_SEPARATOR}{clause}"
    assert _is_observable(repaired)
    assert not _is_vague(repaired)


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        # Removing the vague word already leaves an observable criterion.
        (
            "`habit list` prints a user-friendly table to stdout",
            "`habit list` prints a table to stdout",
        ),
        (
            "The CLI should be easy and user-friendly",
            f"The CLI{OBSERVABLE_CHECK_SEPARATOR}{_EN_COMMAND}",
        ),
        # Nothing but vague words: the criterion is named by its position.
        ("Easy and intuitive", f"Acceptance criterion 1{OBSERVABLE_CHECK_SEPARATOR}{_EN_COMMAND}"),
    ],
)
def test_vague_words_are_the_only_text_removed(criterion: str, expected: str) -> None:
    repaired = _observable_preserving_replacement(criterion, index=0, goal="Build a local CLI")

    assert repaired == expected
    assert _is_observable(repaired)
    assert not _is_vague(repaired)


@pytest.mark.parametrize(
    ("criterion", "goal"),
    [(_CLI_AC, _MD2CSV_GOAL), (_TESTS_AC, _MD2CSV_GOAL), (_KO_AC, _KO_GOAL)],
)
def test_repairing_a_repaired_criterion_changes_nothing(criterion: str, goal: str) -> None:
    once = _observable_preserving_replacement(criterion, index=0, goal=goal)

    assert _observable_preserving_replacement(once, index=0, goal=goal) == once
    assert once.count(OBSERVABLE_CHECK_SEPARATOR) == 1
