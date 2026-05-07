"""Tests for the source-tagged auto answer ledger view."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ouroboros.auto.interview_driver import (
    _AUTO_ANSWER_LOG_LIMIT,
    _AUTO_ANSWER_LOG_TEXT_LIMIT,
    _record_auto_answer,
    _truncate,
)
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.cli.main import app


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_auto_answer_log_default_is_empty_list() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")

    assert state.auto_answer_log == []


def test_auto_answer_log_round_trips_through_state_persistence() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.auto_answer_log = [
        {"round": 1, "source": "repo_fact", "question": "Q1?", "answer": "A1"},
    ]

    restored = AutoPipelineState.from_dict(state.to_dict())

    assert restored.auto_answer_log == [
        {"round": 1, "source": "repo_fact", "question": "Q1?", "answer": "A1"},
    ]


def test_auto_answer_log_legacy_session_hydrates_to_empty_list() -> None:
    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload.pop("auto_answer_log")

    restored = AutoPipelineState.from_dict(payload)

    assert restored.auto_answer_log == []


def test_auto_answer_log_rejects_non_list() -> None:
    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload["auto_answer_log"] = "not a list"

    with pytest.raises(ValueError, match="auto_answer_log must be a list"):
        AutoPipelineState.from_dict(payload)


def test_record_auto_answer_truncates_long_text() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    long_question = "Q " * 500

    _record_auto_answer(
        state,
        round_number=1,
        source="repo_fact",
        question=long_question,
        answer="OK",
    )

    entry = state.auto_answer_log[-1]
    assert len(entry["question"]) <= _AUTO_ANSWER_LOG_TEXT_LIMIT
    assert entry["question"].endswith("...")


def test_record_auto_answer_caps_log_at_limit() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    for round_number in range(_AUTO_ANSWER_LOG_LIMIT + 5):
        _record_auto_answer(
            state,
            round_number=round_number,
            source="repo_fact",
            question=f"Q{round_number}",
            answer=f"A{round_number}",
        )

    assert len(state.auto_answer_log) == _AUTO_ANSWER_LOG_LIMIT
    # Oldest entries are evicted; the most recent round survives.
    assert state.auto_answer_log[-1]["round"] == _AUTO_ANSWER_LOG_LIMIT + 4
    assert state.auto_answer_log[0]["round"] == 5


def test_truncate_collapses_whitespace() -> None:
    assert _truncate("hello   world", 50) == "hello world"


def test_cli_status_renders_recent_source_tagged_answers(monkeypatch, tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 3/12")
    state.auto_answer_log = [
        {
            "round": 1,
            "source": "repo_fact",
            "question": "Which runtime should be used?",
            "answer": "Python 3.12 with uv",
        },
        {
            "round": 2,
            "source": "conservative_default",
            "question": "What constraints bound the MVP?",
            "answer": "Local-only, no network calls",
        },
    ]
    store.save(state)

    monkeypatch.setattr("ouroboros.cli.commands.auto.AutoStore", lambda: store)

    cli_result = CliRunner().invoke(app, ["auto", "--resume", state.auto_session_id, "--status"])
    output = _strip_ansi(cli_result.output)

    assert cli_result.exit_code == 0
    assert "Recent auto answers (last 2):" in output
    assert "round 1 [repo_fact]" in output
    assert "Q: Which runtime should be used?" in output
    assert "A: Python 3.12 with uv" in output
    assert "round 2 [conservative_default]" in output
    assert "A: Local-only, no network calls" in output


def test_cli_status_escapes_rich_markup_in_persisted_answer_text(monkeypatch, tmp_path) -> None:
    """Backend question/answer text that contains ``[`` must not be parsed as Rich markup.

    Without the escape, ``console.print`` would interpret ``[bold]`` as a
    style and either swallow it from the output or raise a markup parse
    error, making ``ooo auto --status`` brittle for sessions whose
    interview text legitimately contains square brackets.
    """
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 4/12")
    state.auto_answer_log = [
        {
            "round": 1,
            "source": "repo_fact",
            "question": "Use [bold]uv[/] toolchain or pip?",
            "answer": "Use uv [from existing setup]",
        },
    ]
    store.save(state)

    monkeypatch.setattr("ouroboros.cli.commands.auto.AutoStore", lambda: store)

    cli_result = CliRunner().invoke(app, ["auto", "--resume", state.auto_session_id, "--status"])
    output = _strip_ansi(cli_result.output)

    assert cli_result.exit_code == 0
    # The literal bracketed segments must survive verbatim.
    assert "[bold]uv[/]" in output
    assert "[from existing setup]" in output


def test_cli_status_omits_recent_section_when_log_empty(monkeypatch, tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 1/12")
    store.save(state)

    monkeypatch.setattr("ouroboros.cli.commands.auto.AutoStore", lambda: store)

    cli_result = CliRunner().invoke(app, ["auto", "--resume", state.auto_session_id, "--status"])
    output = _strip_ansi(cli_result.output)

    assert cli_result.exit_code == 0
    assert "Recent auto answers" not in output
