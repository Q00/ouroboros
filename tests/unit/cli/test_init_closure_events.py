"""CLI ``init`` closure events, ``init list --summary``, and the CLI auto surface."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.bigbang.interview_closure import (
    HOST_LABELS,
    InterviewSurface,
    current_interview_surface,
)
from ouroboros.cli.commands import init as init_module
from ouroboros.cli.commands.init import (
    InterviewLoopOutcome,
    SeedGenerationResult,
    _run_interview,
    _run_interview_loop,
)
from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.interview import (
    interview_completed,
    interview_seed_forced,
    interview_started,
)
from ouroboros.observability.interview_closure_summary import UNRECORDED_SURFACE
from ouroboros.persistence.event_store import EventStore

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class _Store:
    """Accepts both the HITL batch appends and single closure appends."""

    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    is_initialized = True

    async def initialize(self) -> None:
        return None

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def append_durable(self, event: BaseEvent, *, timeout: float) -> None:
        assert 0 < timeout <= 5
        self.events.append(event)

    async def append_batch(self, events: list[BaseEvent]) -> None:
        self.events.extend(events)

    def of(self, event_type: str) -> list[BaseEvent]:
        return [event for event in self.events if event.type == event_type]


def _score(overall: float, clarity: float) -> AmbiguityScore:
    def component(name: str) -> ComponentScore:
        return ComponentScore(name=name, clarity_score=clarity, weight=1 / 3, justification="why")

    return AmbiguityScore(
        overall_score=overall,
        breakdown=ScoreBreakdown(
            goal_clarity=component("goal_clarity"),
            constraint_clarity=component("constraint_clarity"),
            success_criteria_clarity=component("success_criteria_clarity"),
        ),
    )


class _Engine:
    async def ask_next_question(self, state: InterviewState):
        return Result.ok(f"Question {state.current_round_number}?")

    async def record_response(self, state: InterviewState, response: str, question: str):
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=response,
            )
        )
        return Result.ok(state)

    async def save_state(self, state: InterviewState):
        return Result.ok(None)

    async def complete_interview(self, state: InterviewState):
        state.status = InterviewStatus.COMPLETED
        return Result.ok(state)


class _Scorer:
    def __init__(self, score: AmbiguityScore) -> None:
        self._score = score

    async def score(self, state: InterviewState):
        return Result.ok(self._score)


def _script(monkeypatch: pytest.MonkeyPatch, *, confirm: bool) -> None:
    answers = iter(["It lints files.", "[from-code] uses ruff", "Developers on CI."])

    async def prompt(_label: str) -> str:
        return next(answers)

    monkeypatch.setattr("ouroboros.cli.commands.init.multiline_prompt_async", prompt)
    monkeypatch.setattr("ouroboros.cli.commands.init.Confirm.ask", lambda *_a, **_k: confirm)


async def test_loop_records_each_round_and_the_close(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, confirm=False)  # "Continue with more questions?" -> no
    store = _Store()
    state = InterviewState(interview_id="interview_cli_rounds", initial_context="ctx")

    outcome = await _run_interview_loop(
        _Engine(), state, event_store=store, scorer=_Scorer(_score(0.43, 0.6))
    )

    assert outcome.state.is_complete
    recorded = store.of("interview.response.recorded")
    assert [event.data["round_number"] for event in recorded] == [1, 2, 3]
    assert [event.data["answer_source"] for event in recorded] == [
        "from_user",
        "from_code",
        "from_user",
    ]
    assert [event.data["closure"]["verdict"] for event in recorded] == [
        "unscored",
        "unscored",
        "above_threshold",
    ]
    assert recorded[2].data["closure"]["ambiguity_score"] == 0.43
    assert recorded[2].data["closure"]["answered_rounds"] == 3

    (completed,) = store.of("interview.completed")
    assert completed.data["total_rounds"] == 3
    assert completed.data["closure"]["verdict"] == "above_threshold"
    # HITL telemetry is unchanged alongside the closure events.
    assert len(store.of("hitl.answered")) == 3


async def test_loop_close_after_an_approved_score_is_qualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script(monkeypatch, confirm=True)  # "proceed to Seed generation?" -> yes
    store = _Store()
    state = InterviewState(interview_id="interview_cli_ready", initial_context="ctx")

    outcome = await _run_interview_loop(
        _Engine(), state, event_store=store, scorer=_Scorer(_score(0.1, 0.9))
    )

    assert outcome.seed_generation_approved
    (completed,) = store.of("interview.completed")
    assert completed.data["closure"]["verdict"] == "qualified"


async def test_loop_without_an_event_store_records_nothing_and_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script(monkeypatch, confirm=False)
    state = InterviewState(interview_id="interview_cli_nostore", initial_context="ctx")

    outcome = await _run_interview_loop(_Engine(), state, scorer=_Scorer(_score(0.43, 0.6)))

    assert outcome.state.is_complete


@pytest.mark.parametrize(("resume_id", "expected"), [(None, 1), ("interview_cli_start", 0)])
async def test_run_interview_records_cli_init_start_for_new_interviews_only(
    tmp_path: Path, resume_id: str | None, expected: int
) -> None:
    state = InterviewState(
        interview_id="interview_cli_start",
        initial_context="Build a CLI",
        status=InterviewStatus.COMPLETED,
    )
    engine = MagicMock()
    engine.start_interview = AsyncMock(return_value=Result.ok(state))
    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    store = _Store()

    with (
        patch("ouroboros.cli.commands.init._get_adapter", return_value=MagicMock()),
        patch("ouroboros.cli.commands.init.InterviewEngine", return_value=engine),
        patch(
            "ouroboros.cli.commands.init._run_interview_loop",
            new=AsyncMock(return_value=InterviewLoopOutcome(state=state)),
        ),
        patch(
            "ouroboros.cli.commands.init._get_init_event_store",
            new=AsyncMock(return_value=store),
        ),
        patch("ouroboros.cli.commands.init.Confirm.ask", return_value=False),
    ):
        await _run_interview("Build a CLI", resume_id=resume_id, state_dir=tmp_path)

    started = store.of("interview.started")
    assert len(started) == expected
    if expected:
        assert started[0].data == {"initial_context": "Build a CLI", "surface": "cli_init"}


def _completed_state() -> InterviewState:
    state = InterviewState(
        interview_id="interview_cli_seed",
        initial_context="ctx",
        status=InterviewStatus.COMPLETED,
    )
    state.rounds = [
        InterviewRound(round_number=n, question=f"Q{n}", user_response=f"A{n}") for n in (1, 2, 3)
    ]
    return state


def _generator() -> MagicMock:
    seed = SimpleNamespace(metadata=SimpleNamespace(seed_id="seed_cli"))
    generator = MagicMock()
    generator.generate = AsyncMock(return_value=Result.ok(seed))
    generator.save_seed = AsyncMock(return_value=Result.ok(None))
    return generator


async def test_generate_seed_anyway_records_seed_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generator = _generator()
    monkeypatch.setattr(init_module, "SeedGenerator", lambda *_a, **_k: generator)
    monkeypatch.setattr(init_module.Prompt, "ask", lambda *_a, **_k: "2")
    engine = MagicMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    store = _Store()

    _path, result = await init_module._generate_seed_from_interview(
        _completed_state(),
        MagicMock(),
        engine=engine,
        approved_score=_score(0.43, 0.6),
        event_store=store,  # type: ignore[arg-type]
    )

    assert result == SeedGenerationResult.SUCCESS
    assert generator.generate.await_args.kwargs["force"] is True
    (forced,) = store.of("interview.seed_forced")
    assert forced.data["answered_rounds"] == 3
    assert forced.data["interview_completed"] is True
    assert forced.data["closure"]["verdict"] == "above_threshold"


async def test_unforced_generation_records_no_seed_forced(tmp_path: Path) -> None:
    generator = _generator()
    engine = MagicMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    store = _Store()

    with patch.object(init_module, "SeedGenerator", lambda *_a, **_k: generator):
        _path, result = await init_module._generate_seed_from_interview(
            _completed_state(),
            MagicMock(),
            engine=engine,
            approved_score=_score(0.1, 0.9),
            event_store=store,  # type: ignore[arg-type]
        )

    assert result == SeedGenerationResult.SUCCESS
    assert generator.generate.await_args.kwargs["force"] is False
    assert store.events == []


# --- ouroboros init list --summary -------------------------------------------


def _seed_default_store(events: list[BaseEvent]) -> None:
    async def write() -> None:
        store = EventStore()
        await store.initialize()
        try:
            for event in events:
                await store.append(event)
        finally:
            await store.close()

    asyncio.run(write())


def _ago(event: BaseEvent, hours: float) -> BaseEvent:
    return event.model_copy(update={"timestamp": datetime.now(UTC) - timedelta(hours=hours)})


@pytest.fixture
def console_width(monkeypatch: pytest.MonkeyPatch):
    """Pin the shared CLI console's size.

    The console is created once at import, and ``COLUMNS`` is read then if it
    is set, so a ``CliRunner`` env cannot be relied on to change its width.
    Rich honors a fixed width only when the height is fixed too: otherwise a
    dumb terminal (``TERM=dumb``) still renders at 80 columns.
    """
    from ouroboros.cli.formatters import console

    def pin(width: int) -> None:
        monkeypatch.setattr(console, "_width", width)
        monkeypatch.setattr(console, "_height", 25)
        assert (console.size.width, console.size.height) == (width, 25)

    return pin


def _summary_tables(output: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return the outcome and question rows of ``init list --summary``, keyed by surface."""
    outcomes, _, questions = output.partition("Questions per interview by surface")
    assert questions, output

    def rows(text: str) -> dict[str, list[str]]:
        return {
            cells[0]: cells[1:]
            for line in text.splitlines()
            if line.startswith("│")
            and (cells := [cell.strip() for cell in line.strip().strip("│").split("│")])
        }

    return rows(outcomes), rows(questions)


def test_init_list_summary_prints_one_row_per_surface(console_width) -> None:
    console_width(80)
    _seed_default_store(
        [
            _ago(interview_started("c1", "ctx", surface="console"), 50),
            _ago(interview_completed("c1", 6), 49),
            _ago(interview_started("c2", "ctx", surface="console"), 40),
            _ago(interview_seed_forced("c2", answered_rounds=4, interview_completed=False), 39),
            _ago(interview_started("c3", "ctx", surface="console"), 30),
            _ago(interview_started("m1", "ctx", surface="mcp", host="claude_code"), 2),
            _ago(interview_completed("m1", 9), 1),
            _ago(interview_started("legacy", "ctx"), 90),
        ]
    )

    result = runner.invoke(app, ["init", "list", "--summary"])

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    outcomes, questions = _summary_tables(output)
    # started, completed, forced early, abandoned, abandon rate, open
    assert outcomes["console"] == ["3", "1", "1", "1", "33%", "0"]
    assert outcomes["mcp:claude_code"] == ["1", "1", "0", "0", "0%", "0"]
    assert outcomes["unrecorded"] == ["1", "0", "0", "1", "100%", "0"]
    # questions per completed interview (median, mean), per forced one (median)
    assert questions["console"] == ["6", "6", "4"]
    assert questions["mcp:claude_code"] == ["9", "9", "-"]
    assert questions["unrecorded"] == ["-", "-", "-"]
    assert "Forced early: a Seed was generated with the closure gate bypassed" in output


def _closed_surface_labels() -> set[str]:
    return (
        {surface.value for surface in InterviewSurface}
        | {f"mcp:{host}" for host in HOST_LABELS}
        | {UNRECORDED_SURFACE}
    )


@pytest.mark.parametrize("width", [80, 60])
def test_init_list_summary_keeps_every_surface_label_whole(console_width, width: int) -> None:
    console_width(width)
    events: list[BaseEvent] = []
    for index, label in enumerate(sorted(_closed_surface_labels())):
        interview_id = f"i{index}"
        if label == UNRECORDED_SURFACE:
            started = interview_started(interview_id, "ctx")
        else:
            surface, _, host = label.partition(":")
            started = interview_started(interview_id, "ctx", surface=surface, host=host or None)
        events.append(_ago(started, 50))
        if index % 2:
            events.append(_ago(interview_completed(interview_id, 12), 49))
    _seed_default_store(events)

    result = runner.invoke(app, ["init", "list", "--summary"])

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert max(len(line) for line in output.splitlines()) <= width, output
    for rows in _summary_tables(output):
        # Every label is a whole first cell, and no count is cut short.
        assert set(rows) == _closed_surface_labels(), output
        assert not any("…" in cell for cells in rows.values() for cell in cells), output
    if width == 80:
        # At the default width, headers are whole too.
        assert "…" not in output, output


def test_init_list_summary_without_an_event_store_says_so() -> None:
    result = runner.invoke(app, ["init", "list", "--summary"], env={"COLUMNS": "220"})

    assert result.exit_code == 0
    assert "No interviews recorded yet" in _plain(result.output)


def test_init_list_summary_rejects_state_dir(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", "list", "--summary", "--state-dir", str(tmp_path)], env={"COLUMNS": "220"}
    )

    assert result.exit_code == 2
    assert "--summary reads the event store" in _plain(result.output)


def test_init_list_summary_reports_an_unreadable_store(tmp_path: Path) -> None:
    from ouroboros.config.models import resolve_event_store_path

    db_path = resolve_event_store_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a sqlite database at all" * 64)

    result = runner.invoke(app, ["init", "list", "--summary"], env={"COLUMNS": "220"})

    assert result.exit_code == 1
    assert "Cannot read interview events" in _plain(result.output)


def test_init_list_without_summary_still_lists_sessions(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "list", "--state-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "No interviews found." in result.output


# --- ooo auto (CLI) ------------------------------------------------------------


@pytest.mark.usefixtures("auto_project_cwd")
def test_cli_auto_pipeline_runs_under_the_auto_surface() -> None:
    from ouroboros.cli.commands.auto import _run_auto

    seen: dict[str, Any] = {}

    async def run(self: Any, state: Any) -> AutoPipelineResult:
        seen["surface"] = current_interview_surface()
        return AutoPipelineResult(
            status="complete", auto_session_id=state.auto_session_id, phase="complete"
        )

    with (
        patch("ouroboros.cli.commands.auto.InterviewHandler", MagicMock()),
        patch("ouroboros.cli.commands.auto.GenerateSeedHandler", MagicMock()),
        patch("ouroboros.cli.commands.auto.ExecuteSeedHandler", MagicMock()),
        patch("ouroboros.cli.commands.auto.StartExecuteSeedHandler", MagicMock()),
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=run),
    ):
        asyncio.run(
            _run_auto(
                goal="safe goal",
                resume=None,
                runtime="claude",
                max_interview_rounds=2,
                skip_run=True,
            )
        )

    assert seen["surface"] is InterviewSurface.AUTO
    assert current_interview_surface() is None
