"""The console's reading of the interview gate and of repository answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.auto.evidence_check import EvidenceSpan
from ouroboros.auto.investigator import Investigation
from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AUTO_COMPLETE_STREAK_REQUIRED,
    BROWNFIELD_CONTEXT_CLARITY_FLOOR,
    CONSTRAINT_CLARITY_FLOOR,
    GOAL_CLARITY_FLOOR,
    SUCCESS_CRITERIA_CLARITY_FLOOR,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
    completion_floor_checks,
    get_completion_floor_failures,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.gui import interview_assist


def _found(*spans: EvidenceSpan, answer: str) -> Investigation:
    return Investigation("found", answer=answer, checked=spans, cited=len(spans), called=True)


def _span(path: str, quote: str) -> EvidenceSpan:
    return EvidenceSpan(path, 1, 1, quote)


# ── PATH 1a: auto-confirm only an exact, single-source manifest match ──


@pytest.mark.parametrize(
    ("path", "quote", "answer"),
    [
        (
            "pyproject.toml",
            'requires-python = ">=3.12"',
            "The project requires Python 3.12 or newer.",
        ),
        ("package.json", '"react": "^18.2.0",', "The front end uses React 18."),
        ("pyproject.toml", '    "typer>=0.12",', "The CLI is built with Typer."),
        ("pyproject.toml", 'fastapi = "^0.110"', "The web framework is FastAPI."),
        (".github/workflows/test.yml", 'python-version: "3.12"', "CI tests on Python 3.12."),
        ("Dockerfile", "FROM python:3.12-slim", "The image is based on Python 3.12."),
    ],
)
def test_auto_confirms_an_exact_manifest_match(
    tmp_path: Path, path: str, quote: str, answer: str
) -> None:
    assert (
        interview_assist.auto_confirm_source(_found(_span(path, quote), answer=answer), tmp_path)
        == path
    )


def test_two_spans_in_one_manifest_are_one_source(tmp_path: Path) -> None:
    found = _found(
        _span("pyproject.toml", 'requires-python = ">=3.12"'),
        _span("pyproject.toml", '    "fastapi>=0.110",'),
        answer="Python 3.12 with FastAPI.",
    )
    assert interview_assist.auto_confirm_source(found, tmp_path) == "pyproject.toml"


def test_an_answer_read_from_source_code_is_not_auto_confirmed(tmp_path: Path) -> None:
    found = _found(_span("src/app/cli.py", "import typer"), answer="The CLI uses Typer.")
    assert interview_assist.auto_confirm_source(found, tmp_path) is None


def test_citations_in_two_files_are_not_one_candidate(tmp_path: Path) -> None:
    found = _found(
        _span("pyproject.toml", 'requires-python = ">=3.12"'),
        _span(".python-version", "3.11"),
        answer="Python 3.12.",
    )
    assert interview_assist.auto_confirm_source(found, tmp_path) is None


def test_an_answer_that_does_not_state_the_cited_value_is_not_exact(tmp_path: Path) -> None:
    found = _found(_span("pyproject.toml", 'version = "1.2.0"'), answer="The version is 2.0.")
    assert interview_assist.auto_confirm_source(found, tmp_path) is None
    inferred = _found(
        _span("pyproject.toml", 'requires-python = ">=3.12"'),
        answer="The project targets a modern interpreter.",
    )
    assert interview_assist.auto_confirm_source(inferred, tmp_path) is None


# A cited value only grounds an answer that names what the value describes: the
# dependency or tool the line declares (its key, or the name it versions).


@pytest.mark.parametrize(
    ("path", "quote", "answer"),
    [
        ("package.json", '"react": "18"', "This project uses React 18."),
        ("package.json", '"react": "18"', "this project uses REACT 18."),
        ("package.json", '"react": "18"', "This project uses React."),
        ("package.json", '"react": "^18.2.0"', "The front end is react@18."),
        ("package.json", '"@types/node": "^20.11.0"', "Node types come from @types/node 20."),
        ("package.json", '"@types/node": "^20.11.0"', "Node types come from @Types/Node."),
        ("package.json", '"node": ">=20"', "The project needs Node.js 20 or newer."),
        ("pyproject.toml", '"click>=8.1",', "Click."),
        ("pyproject.toml", 'fastapi = { version = "^0.110", extras = ["all"] }', "FastAPI 0.110."),
        ("package.json", '"test": "vitest run"', "Tests run with Vitest."),
        ("compose.yaml", "    image: postgres:16", "The database is Postgres 16."),
        ("Dockerfile", "FROM python:3.12-slim AS build", "The build stage uses Python 3.12."),
        (
            ".github/workflows/ci.yml",
            '        python-version: ["3.11", "3.12"]',
            "CI tests Python version 3.11 or 3.12.",
        ),
        (
            "pyproject.toml",
            '[project]\nrequires-python = ">=3.12"',
            "The project requires Python 3.12 or newer.",
        ),
    ],
)
def test_the_answer_names_what_the_cited_value_describes(
    tmp_path: Path, path: str, quote: str, answer: str
) -> None:
    assert (
        interview_assist.auto_confirm_source(_found(_span(path, quote), answer=answer), tmp_path)
        == path
    )


@pytest.mark.parametrize(
    ("path", "quote", "answer"),
    [
        # The value matches, but the answer says it describes something else.
        ("package.json", '"react": "18"', "This project uses Vue 18."),
        ("package.json", '"react": "18"', "This project uses Preact 18."),
        ("package.json", '"react-dom": "18"', "This project uses React 18."),
        ("package.json", '"@types/node": "^20.11.0"', "The project runs on Node 20."),
        ("pyproject.toml", '"typer>=0.12",', "The CLI is built with Click 0.12."),
        ("pyproject.toml", 'requires-python = ">=3.12"', "The project requires Node 3.12."),
        ("Dockerfile", "FROM python:3.12-slim", "The image is based on Ruby 3.12."),
        ("Dockerfile", "FROM python:3.12-slim AS build", "The build stage uses Node."),
        ("compose.yaml", "    image: postgres:16", "The database is MySQL 16."),
        ("package.json", '"test": "vitest run --coverage"', "Tests run with Jest and coverage."),
        # A metadata field's value is not a named dependency.
        ("package.json", '"version": "1.2.0"', "This project uses Vue version 1.2.0."),
        # A version the citation contradicts, or one no citation holds.
        ("package.json", '"react": "^18.2.0"', "This project uses React 17."),
        ("package.json", '"react": "18"', "This project uses React 18.2."),
        ("package.json", '"react": "^18.2.0"', "React 18 and TypeScript 5."),
        ("package.json", '"react": "18"', "This project uses React and Vue 18."),
        ("package.json", '"react": "18"', "This project uses Vue 18, not React."),
        ("pyproject.toml", '"typer",', "The CLI is built with Typer 0.12."),
        # A version with nothing on the line naming what it versions.
        (".python-version", "3.11", "The project uses Ruby 3.11."),
    ],
)
def test_a_citation_that_does_not_support_the_answer_is_not_auto_confirmed(
    tmp_path: Path, path: str, quote: str, answer: str
) -> None:
    found = _found(_span(path, quote), answer=answer)
    assert interview_assist.auto_confirm_source(found, tmp_path) is None


def test_each_version_belongs_to_the_dependency_it_follows(tmp_path: Path) -> None:
    spans = (_span("package.json", '"react": "^18.2.0",'), _span("package.json", '"node": ">=20"'))
    assert (
        interview_assist.auto_confirm_source(
            _found(*spans, answer="React 18 on Node 20."), tmp_path
        )
        == "package.json"
    )
    swapped = _found(*spans, answer="React 20 on Node 18.")
    assert interview_assist.auto_confirm_source(swapped, tmp_path) is None


@pytest.mark.parametrize(
    "investigation",
    [
        None,
        Investigation("not_found", called=True),
        Investigation("intended_behavior", checked=(_span("pyproject.toml", "x = 1"),)),
        Investigation("found", answer="", checked=(_span("pyproject.toml", 'a = "3.12"'),)),
        Investigation("found", answer="Python 3.12", checked=()),
        Investigation(
            "found",
            answer="Python 3.12",
            checked=(_span("pyproject.toml", 'requires-python = ">=3.12"'),),
            failures=("quote_not_found",),
        ),
    ],
)
def test_only_a_found_fully_checked_answer_can_be_auto_confirmed(
    tmp_path: Path, investigation: Investigation | None
) -> None:
    assert interview_assist.auto_confirm_source(investigation, tmp_path) is None


def test_absolute_citations_are_read_relative_to_the_workspace(tmp_path: Path) -> None:
    span = _span(str(tmp_path / "go.mod"), "go 1.22")
    found = _found(span, answer="The module targets Go 1.22.")
    assert interview_assist.auto_confirm_source(found, tmp_path) == "go.mod"


@pytest.mark.parametrize(
    ("path", "manifest"),
    [
        ("pyproject.toml", True),
        ("services/api/package.json", True),
        ("uv.lock", True),
        ("requirements-dev.txt", True),
        ("Dockerfile.prod", True),
        (".github/workflows/ci.yaml", True),
        (".gitlab-ci.yml", True),
        ("setup.py", False),
        ("Makefile", False),
        ("src/config.py", False),
        ("README.md", False),
        ("docs/.github/workflows/x.yml", False),
    ],
)
def test_manifest_paths(path: str, manifest: bool) -> None:
    assert interview_assist.is_manifest_path(path) is manifest


# ── readiness from the server's persisted gate ──


def _breakdown(goal: float, constraints: float, success: float, context: float | None = None):
    def part(name: str, clarity: float) -> dict[str, object]:
        return {"name": name, "clarity_score": clarity, "weight": 0.3, "justification": "j"}

    data: dict[str, object] = {
        "goal_clarity": part("goal_clarity", goal),
        "constraint_clarity": part("constraint_clarity", constraints),
        "success_criteria_clarity": part("success_criteria_clarity", success),
    }
    if context is not None:
        data["context_clarity"] = part("context_clarity", context)
    return data


def _state(**fields: object) -> InterviewState:
    return InterviewState(interview_id="interview_0123456789abcdef", initial_context="x", **fields)


def test_no_readiness_before_the_server_scores() -> None:
    assert interview_assist.readiness_view(_state()) is None


def test_readiness_reports_the_gate_the_server_applies() -> None:
    state = _state(
        ambiguity_score=0.18,
        ambiguity_breakdown=_breakdown(0.9, 0.6, 0.8),
        completion_candidate_streak=0,
    )
    view = interview_assist.readiness_view(state)
    assert view is not None
    # Under the score gate, but the constraint floor fails: not qualified.
    assert view["score_ok"] is True
    assert view["qualified"] is False
    assert view["complete"] is False
    assert view["threshold"] == AMBIGUITY_THRESHOLD
    assert view["streak_required"] == AUTO_COMPLETE_STREAK_REQUIRED
    assert [(d["key"], d["floor"], d["met"]) for d in view["dimensions"]] == [
        ("goal_clarity", GOAL_CLARITY_FLOOR, True),
        ("constraint_clarity", CONSTRAINT_CLARITY_FLOOR, False),
        ("success_criteria_clarity", SUCCESS_CRITERIA_CLARITY_FLOOR, True),
    ]


def test_brownfield_readiness_adds_the_context_floor_and_streak() -> None:
    state = _state(
        is_brownfield=True,
        ambiguity_score=0.15,
        ambiguity_breakdown=_breakdown(0.9, 0.9, 0.9, context=0.7),
        completion_candidate_streak=1,
    )
    view = interview_assist.readiness_view(state)
    assert view is not None
    assert view["qualified"] is True
    assert view["streak"] == 1
    assert view["brownfield"] is True
    assert view["dimensions"][-1]["key"] == "context_clarity"
    assert view["dimensions"][-1]["floor"] == BROWNFIELD_CONTEXT_CLARITY_FLOOR


def test_readiness_complete_is_the_servers_status() -> None:
    state = _state(
        ambiguity_score=0.12,
        ambiguity_breakdown=_breakdown(0.9, 0.9, 0.9),
        status=InterviewStatus.COMPLETED,
    )
    view = interview_assist.readiness_view(state)
    assert view is not None and view["complete"] is True


def test_readiness_without_a_breakdown_claims_no_gate() -> None:
    view = interview_assist.readiness_view(_state(ambiguity_score=0.1))
    assert view is not None
    assert view["qualified"] is None and view["score_ok"] is None and view["dimensions"] == []


def test_load_interview_view_reads_the_issued_question(tmp_path: Path) -> None:
    state = _state(
        ambiguity_score=0.4,
        ambiguity_breakdown=_breakdown(0.7, 0.6, 0.5),
        rounds=[
            InterviewRound(round_number=1, question="First?", user_response="yes"),
            InterviewRound(round_number=2, question="Which database?", user_response=None),
        ],
    )
    (tmp_path / f"interview_{state.interview_id}.json").write_text(
        state.model_dump_json(), encoding="utf-8"
    )
    readiness, issued = interview_assist.load_interview_view(tmp_path, state.interview_id)
    assert issued == "Which database?"
    assert readiness is not None and readiness["score"] == 0.4
    assert interview_assist.load_interview_view(tmp_path, "interview_missing") == (None, None)


def test_floor_checks_render_the_same_failures() -> None:
    def score(goal: float, constraints: float, success: float, context: float | None):
        def part(name: str, clarity: float) -> ComponentScore:
            return ComponentScore(name=name, clarity_score=clarity, weight=0.3, justification="")

        return AmbiguityScore(
            overall_score=0.3,
            breakdown=ScoreBreakdown(
                goal_clarity=part("g", goal),
                constraint_clarity=part("c", constraints),
                success_criteria_clarity=part("s", success),
                context_clarity=None if context is None else part("x", context),
            ),
        )

    assert get_completion_floor_failures(score(0.6, 0.7, 0.69, None), is_brownfield=True) == [
        "Goal Clarity 0.60 < 0.75",
        "Success Criteria Clarity 0.69 < 0.70",
        "Context Clarity missing (< 0.60)",
    ]
    assert get_completion_floor_failures(score(0.75, 0.65, 0.7, 0.6), is_brownfield=True) == []
    checks = completion_floor_checks(score(0.75, 0.64, 0.7, None), is_brownfield=False)
    assert [check.met for check in checks] == [True, False, True]
