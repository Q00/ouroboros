"""An Auto chat as a structured view: ``--progress-jsonl`` events folded into ``session["auto"]``.

No agent CLI runs: the ``ouroboros auto`` child is a scripted Python process
that writes the progress file the way the CLI writes it, and the auto session
record is an ``AutoStore`` the test fills.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
from typing import Any

import pytest

from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.gui import auto_progress, harness, jobs

SESSION = "auto_0123456789ab"
EXECUTION = "exec_20e9d3785fd6"


def _event(kind: str, phase: str, message: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "auto_session_id": SESSION,
        "phase": phase,
        "kind": kind,
        "message": message,
        "round": None,
        "grade": None,
        "question": None,
        "answer": None,
        "answer_source": None,
        "timestamp": "2026-11-17T00:00:00+00:00",
        **extra,
    }


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _seed(criteria: tuple[str, ...] = ("is_palindrome ignores case", "pytest passes")) -> Seed:
    return Seed(
        goal="A palindrome checker with tests",
        constraints=("Python 3.12 standard library only",),
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(
            name="Palindrome",
            description="One function",
            fields=(OntologyField(name="text", field_type="string", description="Input"),),
        ),
        evaluation_principles=(EvaluationPrinciple(name="tested", description="Tests pass"),),
        exit_conditions=(
            ExitCondition(name="done", description="Tests pass", evaluation_criteria="pytest"),
        ),
        metadata=SeedMetadata(seed_id="seed_test", ambiguity_score=0.1),
    )


def _no_review(view: dict[str, Any]) -> None:
    """The view holds none of the Seed review's fields (#3064)."""
    assert not set(auto_progress.RETIRED_FIELDS) & set(view)


def _record(store: AutoStore, **fields: Any) -> AutoPipelineState:
    state = AutoPipelineState(goal="palindrome", cwd="/tmp", auto_session_id=SESSION)
    state.max_interview_rounds = 12
    for name, value in fields.items():
        setattr(state, name, value)
    store.save(state)
    return state


# ── reading the file ──────────────────────────────────────────────────


def test_a_line_still_being_written_waits_for_its_newline(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    tail = auto_progress.ProgressTail(path)
    assert tail.read() == []  # no file yet: nothing, and no error
    assert tail.error is None

    whole = _line(_event("phase", "interview", "interview round 1/12"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(whole[:25])
    assert tail.read() == []
    assert tail.offset == 0

    with path.open("a", encoding="utf-8") as handle:
        handle.write(whole[25:])
    events = tail.read()
    assert [e["message"] for e in events] == ["interview round 1/12"]
    assert tail.offset == len(whole.encode("utf-8"))
    assert tail.read() == []
    assert tail.skipped == 0


def test_after_the_writer_exits_a_last_line_without_newline_is_read_or_counted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(_line(_event("phase", "interview")) + json.dumps(_event("phase", "review")))
    tail = auto_progress.ProgressTail(path)
    assert [e["phase"] for e in tail.read()] == ["interview"]
    assert [e["phase"] for e in tail.read(final=True)] == ["review"]

    broken = tmp_path / "broken.jsonl"
    broken.write_text(_line(_event("phase", "interview")) + '{"auto_session_id": "auto_')
    tail = auto_progress.ProgressTail(broken)
    assert [e["phase"] for e in tail.read(final=True)] == ["interview"]
    assert tail.skipped == 1  # cut off mid-line: counted, not guessed at


def test_a_line_longer_than_one_read_is_skipped_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auto_progress, "READ_BYTES", 512)
    path = tmp_path / "p.jsonl"
    long = _line(_event("question", "interview", round=1, question="x" * 3000))
    short = _line(_event("phase", "review", "ok"))
    path.write_text(long + short, encoding="utf-8")
    tail = auto_progress.ProgressTail(path)

    events = tail.read(final=True)

    assert [e["phase"] for e in events] == ["review"]
    assert tail.skipped == 1
    assert tail.offset == path.stat().st_size


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[1, 2]",
        json.dumps({**_event("phase", "interview"), "kind": "teleport"}),
        json.dumps({**_event("phase", "Interview Phase")}),
        json.dumps({**_event("question", "interview"), "round": "3"}),
        json.dumps({**_event("question", "interview"), "round": True}),
        json.dumps({**_event("answer", "interview"), "answer": 42}),
        json.dumps({**_event("grade", "review"), "grade": "excellent"}),
        json.dumps({**_event("phase", "interview"), "auto_session_id": None}),
        json.dumps({**_event("phase", "interview"), "message": ["x"]}),
    ],
)
def test_a_line_that_is_not_an_event_is_refused_not_converted(tmp_path: Path, line: str) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(line + "\n" + _line(_event("phase", "review")), encoding="utf-8")
    tail = auto_progress.ProgressTail(path)

    assert [e["phase"] for e in tail.read()] == ["review"]
    assert tail.skipped == 1


# ── folding events ────────────────────────────────────────────────────


def test_rounds_pair_a_question_with_its_answer_whatever_arrives_first() -> None:
    view = auto_progress.new_view()
    events = [
        _event("question", "interview", "question round 1/12", round=1, question="Q1?"),
        # An answer whose question event never arrived still brings its question.
        _event(
            "answer", "interview", round=2, question="Q2?", answer="A2", answer_source="repo_fact"
        ),
        _event(
            "answer", "interview", round=1, question="Q1?", answer="A1", answer_source="non_goal"
        ),
        _event("question", "interview", round=3, question="Q3?"),
        _event("question", "interview", question="no round: not pairable"),
    ]
    for event in events:
        auto_progress.fold(view, event)

    assert view["rounds"] == [
        {
            "round": 1,
            "question": "Q1?",
            "answer": "A1",
            "answer_source": "non_goal",
            "ambiguity": None,
        },
        {
            "round": 2,
            "question": "Q2?",
            "answer": "A2",
            "answer_source": "repo_fact",
            "ambiguity": None,
        },
        {"round": 3, "question": "Q3?", "answer": None, "answer_source": None, "ambiguity": None},
    ]


@pytest.mark.parametrize(
    ("text", "question", "value"),
    [
        (
            "(ambiguity: 0.43) 세 가지 결정을 확정해도 될까요?",
            "세 가지 결정을 확정해도 될까요?",
            0.43,
        ),
        ("(ambiguity:0.4)Which file?", "Which file?", 0.4),
        ("(  ambiguity :  .5  )   Which file?", "Which file?", 0.5),
        ("(ambiguity: 0) Done?", "Done?", 0.0),
        ("(ambiguity: 1.00) Start?", "Start?", 1.0),
    ],
)
def test_an_ambiguity_prefix_is_split_off_the_question(
    text: str, question: str, value: float
) -> None:
    assert auto_progress.split_ambiguity(text) == (question, value)


@pytest.mark.parametrize(
    "text",
    [
        "Which file should hold the tests?",
        "(ambiguity: high) Which file?",
        "(ambiguity: ) Which file?",
        "(ambiguity: 0.4.2) Which file?",
        "(ambiguity: -0.2) Which file?",
        "(ambiguity: 1.5) Which file?",
        "(Ambiguity: 0.4) Which file?",
        "(ambiguity 0.4) Which file?",
        "(ambiguity: 0.4",
        "(ambiguity: 0.4)",
        "(ambiguity: 0.4)   ",
        "Which file? (ambiguity: 0.4)",
    ],
)
def test_a_malformed_or_misplaced_prefix_leaves_the_question_untouched(text: str) -> None:
    assert auto_progress.split_ambiguity(text) == (text, None)


def test_the_fold_keeps_the_ambiguity_on_its_round_not_in_its_text() -> None:
    view = auto_progress.new_view()
    flow = [
        _event("question", "interview", round=1, question="What does it take?"),
        _event("answer", "interview", round=1, question="What does it take?", answer="A str."),
        _event("question", "interview", round=2, question="(ambiguity: 0.48) Punctuation?"),
        # The answer repeats the question with its prefix: one point, not two.
        _event(
            "answer",
            "interview",
            round=2,
            question="(ambiguity: 0.48) Punctuation?",
            answer="Kept.",
        ),
        # An answer whose question was never seen brings both text and value.
        _event(
            "answer", "interview", round=3, question="(ambiguity: 0.43) Tests?", answer="pytest."
        ),
        _event("question", "interview", round=4, question="(ambiguity: high) Unicode?"),
    ]
    for event in flow:
        auto_progress.fold(view, event)

    assert [(r["round"], r["question"], r["ambiguity"]) for r in view["rounds"]] == [
        (1, "What does it take?", None),
        (2, "Punctuation?", 0.48),
        (3, "Tests?", 0.43),
        (4, "(ambiguity: high) Unicode?", None),
    ]
    assert view["ambiguity_trend"] == [{"round": 2, "value": 0.48}, {"round": 3, "value": 0.43}]


@pytest.mark.parametrize(
    ("values", "trend"),
    [
        ([None, None], []),  # no value: the header shows nothing
        ([None, 0.43], [0.43]),  # one value: the latest only
        ([0.48, None, 0.43, 0.38], [0.48, 0.43, 0.38]),  # two or more: a trend
    ],
)
def test_the_header_trend_has_one_point_per_round_that_reported_a_value(
    values: list[float | None], trend: list[float]
) -> None:
    view = auto_progress.new_view()
    for number, value in enumerate(values, start=1):
        prefix = f"(ambiguity: {value:.2f}) " if value is not None else ""
        auto_progress.fold(
            view, _event("question", "interview", round=number, question=f"{prefix}Q{number}?")
        )

    assert [point["value"] for point in view["ambiguity_trend"]] == trend
    assert all(r["question"] == f"Q{r['round']}?" for r in view["rounds"])


def test_the_ambiguity_trend_is_bounded_like_the_rounds() -> None:
    view = auto_progress.new_view()
    total = auto_progress.MAX_ROUNDS + 10
    for number in range(1, total + 1):
        auto_progress.fold(
            view,
            _event("question", "interview", round=number, question=f"(ambiguity: 0.40) Q{number}?"),
        )
    assert len(view["ambiguity_trend"]) == auto_progress.MAX_ROUNDS
    assert view["ambiguity_trend"][0]["round"] == 11


def test_the_new_view_has_no_review_fields() -> None:
    view = auto_progress.new_view()

    assert view["v"] == auto_progress.VIEW_VERSION == 3
    _no_review(view)


def test_an_engine_that_still_reviews_its_seed_folds_to_its_phases_and_nothing_more() -> None:
    """A progress file from before #3064: its phases are kept, its grades and repairs are not."""
    view = auto_progress.new_view()
    flow = [
        _event("phase", "interview", "interview round 1/12", timestamp="t1"),
        _event("phase", "interview", "answered round 1/12 from assumption", timestamp="t2"),
        _event("phase", "seed_generation", "generating Seed", timestamp="t3"),
        _event("phase", "review", "reviewing Seed for required grade A", timestamp="t4"),
        _event("grade", "review", "Seed grade B", grade="B", timestamp="t5"),
        _event("repair", "repair", "repair round 1", round=1, timestamp="t6"),
        _event("grade", "review", "Seed grade A", grade="A", timestamp="t7"),
        _event("phase", "run", "starting execution", timestamp="t8"),
        _event("phase", "complete", "execution started", timestamp="t9"),
    ]
    events = [auto_progress.parse_event(json.dumps(event)) for event in flow]
    assert None not in events  # every line still parses
    reloads = [auto_progress.fold(view, event) for event in events if event is not None]

    assert [(p["phase"], p["at"]) for p in view["phases"]] == [
        ("interview", "t1"),
        ("seed_generation", "t3"),
        ("review", "t4"),
        ("repair", "t6"),
        ("review", "t7"),
        ("run", "t8"),
        ("complete", "t9"),
    ]
    assert view["phase"] == "complete"
    assert view["auto_session_id"] == SESSION
    assert set(view) == set(auto_progress.new_view())
    _no_review(view)
    # The record is read for the first event, then only when it has news: a
    # grade brings none, the phases that follow a written Seed do.
    assert reloads == [True, False, True, True, False, True, True, True, True]


def test_a_repair_event_reads_the_record_again_and_a_grade_does_not() -> None:
    view = auto_progress.new_view()
    auto_progress.fold(view, _event("phase", "review", "reviewing Seed"))
    before = {**view, "message": None}

    # Recorded in the review phase, as an engine before #3064 wrote it: no phase change.
    assert auto_progress.fold(view, _event("grade", "review", "Seed grade B", grade="B")) is False
    assert auto_progress.fold(view, _event("repair", "review", "repair round 1", round=1)) is True
    assert auto_progress.fold(view, _event("repair", "review", "repair round 2")) is True

    # Only the latest progress line moved, as it does for any event.
    assert {**view, "message": None} == before
    _no_review(view)


def test_a_progress_file_with_grade_and_repair_lines_reads_whole(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(
        _line(_event("phase", "review", "reviewing Seed for required grade A"))
        + _line(_event("grade", "review", "Seed grade B", grade="B"))
        + _line(_event("repair", "review", "repair round 1", round=1))
        + _line(_event("grade", "review", "Seed grade A", grade="A")),
        encoding="utf-8",
    )
    tail = auto_progress.ProgressTail(path)

    events = tail.read(final=True)

    assert [e["kind"] for e in events] == ["phase", "grade", "repair", "grade"]
    assert tail.skipped == 0  # read, not refused as unknown lines


_AUTO_VIEW = Path(__file__).resolve().parents[3] / "gui" / "src" / "pages" / "AutoView.tsx"


def test_the_page_puts_every_working_phase_on_one_of_five_steps() -> None:
    """The stepper's table in ``gui/src/pages/AutoView.tsx`` against the engine's phases.

    ``review`` and ``repair`` are kept in the engine for saved sessions (#3064)
    and sit on the spec step with ``seed_generation``. The terminal phases end
    the chat rather than mark a step.
    """
    source = _AUTO_VIEW.read_text(encoding="utf-8")
    steps = re.search(r"const STEP_IDS: AutoStepId\[\] = \[([^\]]*)\];", source)
    table = re.search(r"const PHASE_STEP: Record<string, number> = \{(.*?)\};", source, re.S)
    assert steps is not None and table is not None
    assert re.findall(r'"([a-z]+)"', steps.group(1)) == [
        "interview",
        "spec",
        "run",
        "verify",
        "result",
    ]
    step = {"0": "interview", "SPEC": "spec", "RUN": "run", "VERIFY": "verify"}
    mapped = {
        phase: step[value]
        for phase, value in re.findall(r"^\s*([a-z_]+): (\w+),$", table.group(1), re.M)
    }

    terminal = {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED}
    assert set(mapped) == {phase.value for phase in AutoPhase if phase not in terminal}
    assert mapped == {
        "created": "interview",
        "interview": "interview",
        "seed_generation": "spec",
        "review": "spec",
        "repair": "spec",
        "run": "run",
        "ralph_handoff": "verify",
        "evaluate": "verify",
        "unstuck_lateral": "verify",
    }


def test_the_view_stays_bounded() -> None:
    view = auto_progress.new_view()
    total = auto_progress.MAX_ROUNDS + 25
    for number in range(1, total + 1):
        auto_progress.fold(
            view, _event("question", "interview", round=number, question="질문 " * 1000)
        )
        auto_progress.fold(
            view,
            _event(
                "answer", "interview", round=number, answer="a" * 5000, answer_source="assumption"
            ),
        )
    for index in range(auto_progress.MAX_PHASES + 10):
        auto_progress.fold(view, _event("phase", "run" if index % 2 else "evaluate"))
    for _ in range(25):
        auto_progress.fold(view, _event("grade", "review", grade="B"))

    assert len(view["rounds"]) == auto_progress.MAX_ROUNDS
    assert view["rounds_dropped"] == 25
    assert view["rounds"][0]["round"] == 26 and view["rounds"][-1]["round"] == total
    first = view["rounds"][0]
    assert len(first["question"]) == auto_progress.MAX_TEXT and first["question"].endswith("…")
    assert len(first["answer"]) == auto_progress.MAX_TEXT and first["answer"].endswith("…")
    assert len(view["phases"]) == auto_progress.MAX_PHASES
    _no_review(view)
    # A late event for a round already dropped does not come back.
    auto_progress.fold(view, _event("answer", "interview", round=3, answer="late"))
    assert len(view["rounds"]) == auto_progress.MAX_ROUNDS
    assert all(entry["round"] != 3 for entry in view["rounds"])


# ── the auto session record ───────────────────────────────────────────


def test_the_record_gives_the_seed_execution_and_round_limit(tmp_path: Path) -> None:
    store = AutoStore(tmp_path)
    _record(
        store,
        seed_artifact=_seed().to_dict(),
        seed_path="/seeds/seed_test.yaml",
        execution_id=EXECUTION,
        job_id="job_0123",
        last_grade="A",
        findings=[{"message": "Success criteria are vague"}, {"code": "no message"}],
    )

    facts = auto_progress.load_facts(SESSION, store)

    assert facts is not None
    assert (facts["max_rounds"], facts["execution_id"], facts["job_id"]) == (
        12,
        EXECUTION,
        "job_0123",
    )
    # The record's legacy review fields are not read.
    assert set(facts) == {"max_rounds", "execution_id", "job_id", "seed", "seed_path"}
    view = auto_progress.new_view()
    auto_progress.apply_facts(view, facts)
    assert (view["max_rounds"], view["execution_id"]) == (12, EXECUTION)
    _no_review(view)
    fields = auto_progress.seed_fields(facts["seed"], facts["seed_path"])
    assert fields["text"] == "A palindrome checker with tests"
    assert fields["acceptance_criteria"] == ["is_palindrome ignores case", "pytest passes"]
    assert fields["constraints"] == ["Python 3.12 standard library only"]
    assert fields["seed_yaml"].startswith("goal: A palindrome checker")
    assert fields["seed_path"] == "/seeds/seed_test.yaml"
    assert auto_progress.load_facts("auto_missing000", store) is None
    assert auto_progress.load_facts("../escape", store) is None


def test_a_seed_too_large_for_the_chat_keeps_its_path_not_its_yaml() -> None:
    criteria = tuple(f"criterion {index} " + "x" * 2000 for index in range(60))
    fields = auto_progress.seed_fields(_seed(criteria).to_dict(), "/seeds/big.yaml")

    assert fields["seed_yaml"] is None
    assert fields["seed_path"] == "/seeds/big.yaml"
    assert len(fields["acceptance_criteria"]) == auto_progress.MAX_SEED_ITEMS
    assert fields["acceptance_total"] == 60
    assert all(len(c) <= auto_progress.MAX_SEED_ITEM for c in fields["acceptance_criteria"])


# ── the harness, end to end ───────────────────────────────────────────

_SCRIPT = """
import json, sys, time
path = sys.argv[sys.argv.index("--progress-jsonl") + 1]
lines = json.loads({lines!r})
for index, line in enumerate(lines):
    with open(path, "a", encoding="utf-8") as handle:
        if index == {split}:
            # Half a line, then a pause longer than the console's poll.
            handle.write(line[:30]); handle.flush(); time.sleep(0.8)
            handle.write(line[30:])
        else:
            handle.write(line)
    time.sleep(0.02)
print("2026-11-17T00:00:00Z [info     ] structlog noise lineno=234")
print("  Execution ID: {execution}")
print("Run handoff status: completed")
"""


def _flow(*, review: bool) -> list[dict[str, Any]]:
    """The events of one Auto job; ``review`` is an engine from before #3064."""
    reviewed = [
        _event("phase", "review", "reviewing Seed for required grade A"),
        _event("grade", "review", "Seed grade B", grade="B"),
        _event("repair", "repair", "repair round 1", round=1),
        _event("grade", "review", "Seed grade A", grade="A"),
    ]
    return [
        _event("phase", "interview", "interview round 1/12"),
        _event(
            "question", "interview", "question round 1/12", round=1, question="구두점도 무시할까요?"
        ),
        _event(
            "answer",
            "interview",
            round=1,
            question="구두점도 무시할까요?",
            answer="Ignore punctuation.",
            answer_source="assumption",
        ),
        _event(
            "question", "interview", round=2, question="(ambiguity: 0.42) Where do the tests live?"
        ),
        _event(
            "answer",
            "interview",
            round=2,
            question="(ambiguity: 0.42) Where do the tests live?",
            answer="tests/test_palindrome.py",
            answer_source="repo_fact",
        ),
        _event("phase", "seed_generation", "generating Seed from auto interview"),
        *(reviewed if review else []),
        _event("phase", "run", "starting execution"),
        _event("phase", "complete", "execution started"),
    ]


def _fake_auto(monkeypatch: pytest.MonkeyPatch, lines: list[str], split: int = -1) -> None:
    script = _SCRIPT.format(lines=json.dumps(lines), split=split, execution=EXECUTION)
    monkeypatch.setattr(
        harness, "ouroboros_command", lambda *args: [sys.executable, "-c", script, *args]
    )


class _NoBoard:
    """The run's event tail: this run never wrote a board row."""

    def __init__(self, *_args: Any) -> None:
        pass

    def fetch_new(self) -> list[dict[str, Any]]:
        return []


def _wait(h: harness.Harness, chat_id: str, check: Any, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = h.get(chat_id) or {}
        if check(session):
            return session
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state: {json.dumps(h.get(chat_id), default=str)[:3000]}")


def _run_card_settled(session: dict[str, Any]) -> bool:
    return session.get("status") == "done" and any(
        m.get("kind") == "run" and m.get("run_status") != "running"
        for m in session.get("messages", [])
    )


@pytest.fixture
def no_board(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.dashboard_web import reader

    monkeypatch.setattr(reader, "EventTail", _NoBoard)


@pytest.mark.usefixtures("no_board")
@pytest.mark.parametrize(
    ("review", "phases"),
    [
        pytest.param(False, ["interview", "seed_generation", "run", "complete"], id="engine"),
        pytest.param(
            True,
            ["interview", "seed_generation", "review", "repair", "review", "run", "complete"],
            id="engine-before-3064",
        ),
    ],
)
def test_an_auto_chat_becomes_a_structured_view_and_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: bool, phases: list[str]
) -> None:
    store = AutoStore()  # the default store, under the isolated HOME
    # The record of an engine before #3064 also holds its review.
    legacy = {"last_grade": "A", "findings": [{"message": "vague"}]} if review else {}
    _record(
        store,
        seed_artifact=_seed().to_dict(),
        execution_id=EXECUTION,
        job_id="job_0123",
        **legacy,
    )
    _fake_auto(monkeypatch, [_line(e) for e in _flow(review=review)], split=2)
    h = harness.Harness(jobs.JobRegistry())

    chat = h.create({"goal": "palindrome", "mode": "auto", "cwd": str(tmp_path)})
    job = h._jobs.get(chat["auto_job_id"])
    assert job is not None
    progress = auto_progress.progress_file(harness.chats_dir(), chat["id"])
    assert job.argv[-2:] == ["--progress-jsonl", str(progress)]

    session = _wait(h, chat["id"], _run_card_settled)

    view = session["auto"]
    assert view["auto_session_id"] == SESSION
    assert view["rounds"] == [
        {
            "round": 1,
            "question": "구두점도 무시할까요?",
            "answer": "Ignore punctuation.",
            "answer_source": "assumption",
            "ambiguity": None,
        },
        {
            "round": 2,
            "question": "Where do the tests live?",
            "answer": "tests/test_palindrome.py",
            "answer_source": "repo_fact",
            "ambiguity": 0.42,
        },
    ]
    assert view["ambiguity_trend"] == [{"round": 2, "value": 0.42}]
    assert view["skipped"] == 0  # the half-written line was read once it was whole
    assert [p["phase"] for p in view["phases"]] == phases
    assert view["max_rounds"] == 12
    _no_review(view)
    assert (view["execution_id"], view["outcome"]) == (EXECUTION, "completed")

    kinds = [m["kind"] for m in session["messages"]]
    assert kinds == ["text", "log", "seed", "run", "note", "result"]
    seed = session["messages"][2]
    assert (seed["source"], seed["text"]) == ("auto", "A palindrome checker with tests")
    assert seed["acceptance_criteria"] == ["is_palindrome ignores case", "pytest passes"]
    run = session["messages"][3]
    assert (run["execution_id"], run["job_id"]) == (EXECUTION, "job_0123")
    # The run never reported an end before Auto did: it is not shown as running.
    assert run["run_status"] == "interrupted"
    log = session["messages"][1]
    assert any("structlog noise" in line for line in log["lines"])  # the raw log is kept

    # Saved with the chat: a new console reads back the same view and messages.
    again = harness.Harness(jobs.JobRegistry()).get(chat["id"]) or {}
    assert again["auto"] == view
    assert again["messages"] == session["messages"]


def test_a_restart_marks_a_running_auto_chat_and_its_run_card_interrupted() -> None:
    chat = {
        "id": "live00000001",
        "title": "t",
        "goal": "t",
        "cwd": "/tmp",
        "mode": "auto",
        "status": "running",
        "version": 7,
        "auto": {**auto_progress.new_view(), "phase": "complete", "execution_id": EXECUTION},
        "messages": [
            {
                "id": "r1",
                "role": "assistant",
                "kind": "run",
                "text": "",
                "ts": 0,
                "execution_id": EXECUTION,
                "run_status": "running",
                "progress": None,
            },
        ],
    }
    (harness.chats_dir() / "live00000001.json").write_text(json.dumps(chat), encoding="utf-8")

    loaded = harness.Harness(jobs.JobRegistry()).get("live00000001") or {}

    assert loaded["status"] == "interrupted"
    assert loaded["messages"][0]["run_status"] == "interrupted"
    assert loaded["auto"] == chat["auto"]  # the story so far is kept as it was


def test_an_old_auto_chat_without_a_view_loads_exactly_as_saved() -> None:
    old = {
        "id": "old000000001",
        "title": "t",
        "goal": "t",
        "cwd": "/tmp",
        "mode": "auto",
        "status": "done",
        "version": 3,
        "messages": [
            {"id": "m1", "role": "user", "kind": "text", "text": "t", "ts": 0},
            {
                "id": "m2",
                "role": "assistant",
                "kind": "log",
                "text": "ouroboros auto",
                "ts": 0,
                "lines": ["[auto] interview — interview round 1/50"],
                "job_id": "j",
            },
        ],
    }
    (harness.chats_dir() / "old000000001.json").write_text(json.dumps(old), encoding="utf-8")

    loaded = harness.Harness(jobs.JobRegistry()).get("old000000001")

    assert loaded == old
    assert "auto" not in (loaded or {})


@pytest.mark.usefixtures("no_board")
def test_a_repaired_seed_replaces_the_card_rather_than_adding_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AutoStore()
    _record(store, seed_artifact=_seed(("first draft",)).to_dict(), last_grade="B")
    h = harness.Harness(jobs.JobRegistry())
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "auto",
        "status": "running",
        "version": 0,
        "auto": auto_progress.new_view(),
        "messages": [],
    }
    h._sessions["c1"]["auto"]["auto_session_id"] = SESSION

    first = h._auto_facts(SESSION)
    assert first is not None
    h._update("c1", lambda s: harness._apply_auto_facts(s, first))
    _record(store, seed_artifact=_seed(("repaired", "second")).to_dict(), last_grade="A")
    second = h._auto_facts(SESSION)
    assert second is not None
    h._update("c1", lambda s: harness._apply_auto_facts(s, second))

    seeds = [m for m in (h.get("c1") or {})["messages"] if m["kind"] == "seed"]
    assert len(seeds) == 1
    assert seeds[0]["acceptance_criteria"] == ["repaired", "second"]


def test_the_auto_run_card_stops_with_the_auto_job_and_leaves_the_chat_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_board: None
) -> None:
    h = harness.Harness(jobs.JobRegistry())
    job = jobs.Job(id="j-auto", kind="auto", title="t", argv=["ouroboros", "auto"])
    job.status = "succeeded"
    h._jobs._jobs[job.id] = job
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "auto",
        "status": "running",
        "version": 0,
        "auto": auto_progress.new_view(),
        "messages": [
            {
                "id": "r1",
                "role": "assistant",
                "kind": "run",
                "text": "",
                "ts": 0,
                "execution_id": EXECUTION,
                "run_status": "running",
                "progress": None,
            },
        ],
    }

    started = time.monotonic()
    h._follow_run("c1", EXECUTION, None, auto_job_id=job.id)  # returns: the job has ended

    assert time.monotonic() - started < 5
    session = h.get("c1") or {}
    assert session["status"] == "running"  # the Auto job's end decides this, not the card
    assert [m["kind"] for m in session["messages"]] == ["run"]
    assert session["messages"][0]["run_status"] == "interrupted"


def test_deleting_an_auto_chat_removes_its_progress_file() -> None:
    h = harness.Harness(jobs.JobRegistry())
    h._sessions["c1"] = {"id": "c1", "mode": "auto", "messages": [], "version": 0}
    progress = auto_progress.progress_file(harness.chats_dir(), "c1")
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(_line(_event("phase", "interview")), encoding="utf-8")

    h.delete("c1")

    assert not progress.exists()


# ── upgrading a view saved by an older console ───────────────────────


def _fixture(name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


_V1 = _fixture("gui_auto_view_v1.json")
# Saved while Auto still reviewed its Seed: a grade B, one repair, then A (#3064).
_V2 = _fixture("gui_auto_view_v2.json")


def _v1_chat(chat_id: str, saved: dict[str, Any] = _V1) -> dict[str, Any]:
    return {
        "id": chat_id,
        "title": "word_count",
        "goal": "word_count",
        "cwd": "/tmp",
        "mode": "auto",
        "status": "done",
        "version": 58,
        "auto": json.loads(json.dumps(saved["view"])),
        "messages": [{"id": "m1", "role": "user", "kind": "text", "text": "w", "ts": 0}],
    }


def _write_v1(chat_id: str, *, with_progress: bool, saved: dict[str, Any] = _V1) -> Path:
    path = harness.chats_dir() / f"{chat_id}.json"
    path.write_text(json.dumps(_v1_chat(chat_id, saved), ensure_ascii=False), encoding="utf-8")
    if with_progress:
        progress = auto_progress.progress_file(harness.chats_dir(), chat_id)
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text("".join(_line(e) for e in saved["events"]), encoding="utf-8")
    return path


def test_the_fixture_is_a_real_v1_view_with_the_raw_prefix() -> None:
    view = _V1["view"]
    assert view["v"] == 1 and "ambiguity_trend" not in view
    assert view["rounds"][1]["question"].startswith("(ambiguity: 0.43) ")
    assert "ambiguity" not in view["rounds"][1]


def test_the_v2_fixture_carries_the_seed_review() -> None:
    view = _V2["view"]
    assert view["v"] == 2
    assert set(auto_progress.RETIRED_FIELDS) <= set(view)
    assert [g["grade"] for g in view["grades"]] == ["B", "A"]
    assert (view["repair_round"], view["findings"]) == (
        1,
        ["Acceptance criteria should name an observable check"],
    )
    assert {e["kind"] for e in _V2["events"]} == {"phase", "question", "answer", "grade", "repair"}


def test_a_v1_view_is_rebuilt_from_its_progress_file_when_the_console_loads() -> None:
    path = _write_v1("v1chat000001", with_progress=True)

    view = (harness.Harness(jobs.JobRegistry()).get("v1chat000001") or {})["auto"]

    assert view["v"] == auto_progress.VIEW_VERSION == 3
    assert [(r["round"], r["question"], r["ambiguity"]) for r in view["rounds"]] == [
        (1, "word_count는 어떤 입력을 받나요?", None),
        (2, "세 가지 결정(구두점, 대소문자, 빈 입력)을 이렇게 확정해도 될까요?", 0.43),
        (3, "CLI와 패키징은 범위 밖으로 둘까요?", 0.38),
    ]
    assert view["ambiguity_trend"] == [{"round": 2, "value": 0.43}, {"round": 3, "value": 0.38}]
    # Folded again from the events…
    assert [p["phase"] for p in view["phases"]] == [p["phase"] for p in _V1["view"]["phases"]]
    _no_review(view)
    # …and what the events never carried is kept from the old view.
    old = _V1["view"]
    for key in ("max_rounds", "execution_id", "seed_loaded", "outcome"):
        assert view[key] == old[key], key
    # Saved once, so the next start finds the current shape.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["auto"]["v"] == 3 and saved["auto"]["ambiguity_trend"] == view["ambiguity_trend"]
    _no_review(saved["auto"])


def test_a_v1_view_without_its_progress_file_is_migrated_in_place() -> None:
    _write_v1("v1chat000002", with_progress=False)

    view = (harness.Harness(jobs.JobRegistry()).get("v1chat000002") or {})["auto"]

    assert view["v"] == 3
    assert [r["question"] for r in view["rounds"]][1:] == [
        "세 가지 결정(구두점, 대소문자, 빈 입력)을 이렇게 확정해도 될까요?",
        "CLI와 패키징은 범위 밖으로 둘까요?",
    ]
    assert [r["ambiguity"] for r in view["rounds"]] == [None, 0.43, 0.38]
    assert [p["value"] for p in view["ambiguity_trend"]] == [0.43, 0.38]
    rest = {k: v for k, v in view.items() if k not in ("v", "rounds", "ambiguity_trend")}
    retired = ("v", "rounds", *auto_progress.RETIRED_FIELDS)
    assert rest == {k: v for k, v in _V1["view"].items() if k not in retired}


def test_a_v2_view_with_the_seed_review_is_rebuilt_without_it() -> None:
    path = _write_v1("v2review0001", with_progress=True, saved=_V2)

    view = (harness.Harness(jobs.JobRegistry()).get("v2review0001") or {})["auto"]

    assert view["v"] == 3
    _no_review(view)
    # Everything else is what was saved: the replay covered it and changed nothing.
    old = _V2["view"]
    assert view == {
        **{k: v for k, v in old.items() if k not in auto_progress.RETIRED_FIELDS},
        "v": 3,
    }
    # The review's phases stay in the history; the page puts them on the spec step.
    assert [p["phase"] for p in view["phases"]] == [
        "interview",
        "seed_generation",
        "review",
        "repair",
        "review",
        "run",
        "complete",
    ]
    assert json.loads(path.read_text(encoding="utf-8"))["auto"] == view


def test_a_v2_view_without_its_progress_file_keeps_every_other_field() -> None:
    chat = _v1_chat("v2review0002", _V2)
    # Its first round was dropped from the view, but its trend point was kept.
    chat["auto"]["rounds"] = chat["auto"]["rounds"][2:]
    chat["auto"]["rounds_dropped"] = 2
    path = harness.chats_dir() / "v2review0002.json"
    path.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    view = (harness.Harness(jobs.JobRegistry()).get("v2review0002") or {})["auto"]

    _no_review(view)
    kept = {k: v for k, v in chat["auto"].items() if k not in auto_progress.RETIRED_FIELDS}
    assert view == {**kept, "v": 3}
    assert view["ambiguity_trend"] == [{"round": 2, "value": 0.43}, {"round": 3, "value": 0.38}]


_CLEAN_QUESTIONS = {
    1: "word_count는 어떤 입력을 받나요?",
    2: "세 가지 결정(구두점, 대소문자, 빈 입력)을 이렇게 확정해도 될까요?",
    3: "CLI와 패키징은 범위 밖으로 둘까요?",
}


def _assert_nothing_saved_is_lost(view: dict[str, Any], saved: dict[str, Any] = _V1) -> None:
    """Every round, phase and fact the old view held is still there; its review is not."""
    old = saved["view"]
    assert view["v"] == 3
    _no_review(view)
    rounds = {entry["round"]: entry for entry in view["rounds"]}
    for entry in old["rounds"]:
        kept = rounds[entry["round"]]
        assert kept["question"] == _CLEAN_QUESTIONS[entry["round"]]
        assert (kept["answer"], kept["answer_source"]) == (entry["answer"], entry["answer_source"])
    assert [r["ambiguity"] for r in view["rounds"]][:3] == [None, 0.43, 0.38]
    assert [p["phase"] for p in view["phases"]][: len(old["phases"])] == [
        p["phase"] for p in old["phases"]
    ]
    for key in (
        "auto_session_id",
        "max_rounds",
        "execution_id",
        "seed_loaded",
        "outcome",
    ):
        assert view[key] == old[key], key


def _load_v1_with(
    chat_id: str, progress_text: str | None, saved: dict[str, Any] = _V1
) -> tuple[dict[str, Any], Path]:
    path = _write_v1(chat_id, with_progress=False, saved=saved)
    if progress_text is not None:
        progress = auto_progress.progress_file(harness.chats_dir(), chat_id)
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(progress_text, encoding="utf-8")
    view = (harness.Harness(jobs.JobRegistry()).get(chat_id) or {})["auto"]
    return view, path


_FULL = "".join(_line(e) for e in _V1["events"])


def _lossy_files(saved: dict[str, Any]) -> list[Any]:
    """Progress files that cannot cover the view ``saved`` holds."""
    events = saved["events"]
    full = "".join(_line(e) for e in events)
    return [
        pytest.param("", id="empty"),
        pytest.param(
            '{"auto_session_id": "auto_9495\nnot json at all\n[1, 2]\n{"kind"', id="garbage"
        ),
        pytest.param(
            full[: full.index("\n") + 1] + full[full.index("\n") + 1 :][:40], id="partial"
        ),
        pytest.param("".join(_line(e) for e in events[:4]), id="truncated"),
        pytest.param("".join(_line(e) for e in events[6:]), id="rotated"),
        pytest.param(
            "".join(_line({**e, "auto_session_id": "auto_other000001"}) for e in events),
            id="another-session",
        ),
    ]


@pytest.mark.parametrize(
    ("saved", "progress_text"),
    [
        *[pytest.param(_V1, *case.values, id=f"v1-{case.id}") for case in _lossy_files(_V1)],
        *[pytest.param(_V2, *case.values, id=f"v2-{case.id}") for case in _lossy_files(_V2)],
    ],
)
def test_a_progress_file_that_cannot_cover_the_saved_view_never_replaces_it(
    saved: dict[str, Any], progress_text: str
) -> None:
    view, path = _load_v1_with("v1lossy00001", progress_text, saved)

    _assert_nothing_saved_is_lost(view, saved)
    # The migrated view is what was saved, without its review: nothing more, nothing less.
    assert len(view["rounds"]) == len(saved["view"]["rounds"])
    assert len(view["phases"]) == len(saved["view"]["phases"])
    assert json.loads(path.read_text(encoding="utf-8"))["auto"] == view


def test_a_full_progress_file_is_replayed_and_only_adds() -> None:
    later = _event("phase", "ralph_handoff", "verifying the run", timestamp="t-later")
    later["auto_session_id"] = _V1["view"]["auto_session_id"]

    view, _ = _load_v1_with("v1replay0001", _FULL + _line(later))

    _assert_nothing_saved_is_lost(view)
    # The replay brought what the saved view missed, and replaced nothing.
    assert [p["phase"] for p in view["phases"]][-1] == "ralph_handoff"
    assert view["phase"] == "ralph_handoff"
    assert view["ambiguity_trend"] == [{"round": 2, "value": 0.43}, {"round": 3, "value": 0.38}]


@pytest.mark.parametrize("version", [4, "1", None, True])
def test_a_view_this_console_cannot_read_safely_is_kept_exactly_as_saved(version: object) -> None:
    chat = _v1_chat("vnewer000001")
    chat["auto"]["v"] = version
    path = harness.chats_dir() / "vnewer000001.json"
    path.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    progress = auto_progress.progress_file(harness.chats_dir(), "vnewer000001")
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("", encoding="utf-8")

    view = (harness.Harness(jobs.JobRegistry()).get("vnewer000001") or {})["auto"]

    assert view == chat["auto"]
    assert path.read_text(encoding="utf-8") == before  # not saved over


@pytest.mark.parametrize(
    ("saved", "name", "bad"),
    [
        pytest.param(_V1, "rounds", "not a round", id="v1-round"),
        pytest.param(_V2, "rounds", {"round": "3"}, id="v2-round"),
        pytest.param(_V2, "phases", {"phase": None}, id="v2-phase"),
        pytest.param(_V2, "ambiguity_trend", {"round": 4, "value": "0.3"}, id="v2-trend-value"),
        pytest.param(_V2, "ambiguity_trend", {"round": True, "value": 0.3}, id="v2-trend-round"),
    ],
)
def test_an_old_view_with_entries_of_the_wrong_shape_is_kept_as_saved(
    saved: dict[str, Any], name: str, bad: object
) -> None:
    chat = _v1_chat("vbroken00001", saved)
    chat["auto"][name].append(bad)
    path = harness.chats_dir() / "vbroken00001.json"
    path.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    view = (harness.Harness(jobs.JobRegistry()).get("vbroken00001") or {})["auto"]

    assert view == chat["auto"]
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("progress_text", ["", "".join(_line(e) for e in _V1["events"][:3]), _FULL])
def test_a_current_view_is_loaded_as_saved_whatever_its_progress_file_holds(
    progress_text: str,
) -> None:
    chat = _v1_chat("v2chat000001")
    chat["auto"] = {**auto_progress.new_view(), "phase": "run", "execution_id": EXECUTION}
    path = harness.chats_dir() / "v2chat000001.json"
    path.write_text(json.dumps(chat), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    progress = auto_progress.progress_file(harness.chats_dir(), "v2chat000001")
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(progress_text, encoding="utf-8")

    view = (harness.Harness(jobs.JobRegistry()).get("v2chat000001") or {})["auto"]

    assert view == chat["auto"]
    assert path.read_text(encoding="utf-8") == before


# ── how an Auto job ends ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cancelled", "status", "keys"),
    [(True, "interrupted", ["result"]), (False, "failed", ["auto_exited", "result"])],
)
def test_a_stopped_auto_job_is_interrupted_and_only_a_failure_reports_its_exit(
    tmp_path: Path, cancelled: bool, status: str, keys: list[str]
) -> None:
    h = harness.Harness(jobs.JobRegistry())
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "auto",
        "status": "running",
        "version": 0,
        "auto": auto_progress.new_view(),
        "messages": [],
    }
    snap = {"status": "failed", "exit_code": -15 if cancelled else 1, "found": {}}

    h._finish_auto("c1", snap, cancelled=cancelled)

    session = h.get("c1") or {}
    assert session["status"] == status
    assert [m.get("key") or m["kind"] for m in session["messages"]] == keys
    assert session["auto"]["outcome"] == ("stopped" if cancelled else "failed")
