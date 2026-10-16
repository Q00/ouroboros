"""Tests for the opt-in ``ooo auto`` requester (``ouroboros.auto.requester``).

Auto asks the requester only where it would otherwise assume (draft answer
source CONSERVATIVE_DEFAULT or ASSUMPTION) and answers every other question
itself. These tests use a fake requester command (a small Python script) so the
real subprocess contract is exercised: JSON on stdin, JSON or plain text on
stdout, and the draft kept on timeout, nonzero exit or empty answer.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import textwrap
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource, AutoBlocker
from ouroboros.auto.intent_guard import guard_auto_answer
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.requester import (
    DEFAULT_REQUESTER_TIMEOUT_SECONDS,
    REQUESTER_COMMAND_ENV,
    REQUESTER_TIMEOUT_ENV,
    CommandRequester,
    RequesterQuestion,
    RequesterReply,
    ask_requester,
    build_requester,
    resolve_requester_command,
    resolve_requester_timeout,
)
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.cli.main import app
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key

GOAL = "Build a CSV to JSON CLI"
FIRST_QUESTION = "Which output format should the CLI use?"
REQUESTER_TEXT = "Emit one JSON array of objects to stdout; nothing else changes."

# Fake requester. argv: <mode> <log path>. Appends the stdin payload to the log
# (one JSON line per call) and then replies according to <mode>.
_FAKE_REQUESTER = textwrap.dedent(
    f"""
    import json, sys, time
    mode, log_path = sys.argv[1], sys.argv[2]
    payload = json.loads(sys.stdin.read())
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\\n")
    if mode == "json":
        print(json.dumps({{"answer": {REQUESTER_TEXT!r}}}))
    elif mode == "text":
        print("  " + {REQUESTER_TEXT!r} + "  ")
    elif mode == "exit":
        print("ignored", flush=True)
        sys.stderr.write("requester unavailable")
        sys.exit(3)
    elif mode == "empty":
        print("   ")
    elif mode == "empty_json":
        print(json.dumps({{"answer": "  "}}))
    elif mode == "bad_json":
        print(json.dumps({{"reply": "no answer key"}}))
    elif mode == "sleep":
        time.sleep(30)
    """
)


@pytest.fixture
def fake_requester(tmp_path: Path):
    script = tmp_path / "fake_requester.py"
    script.write_text(_FAKE_REQUESTER, encoding="utf-8")
    log_path = tmp_path / "requester_calls.jsonl"

    def make(mode: str, *, timeout: float = 30.0) -> CommandRequester:
        return CommandRequester((sys.executable, str(script), mode, str(log_path)), timeout)

    def calls() -> list[dict]:
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines() if line]

    make.calls = calls  # type: ignore[attr-defined]
    make.script = script  # type: ignore[attr-defined]
    make.log_path = log_path  # type: ignore[attr-defined]
    return make


def _question(round_number: int = 1) -> RequesterQuestion:
    return RequesterQuestion(
        question=FIRST_QUESTION,
        goal=GOAL,
        draft_answer="draft",
        draft_source="conservative_default",
        round=round_number,
    )


async def _run_driver(tmp_path: Path, requester=None, *, max_rounds: int = 1):
    """Run a short interview; return (state, texts the backend received)."""
    sent: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(FIRST_QUESTION, "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        return InterviewTurn("What else should we consider?", session_id)

    store_root = tmp_path / "store"
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(store_root),
        max_rounds=max_rounds,
        requester=requester,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    await driver.run(state, SeedDraftLedger.from_goal(GOAL))
    return state, sent, store_root


# -- command contract --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "text"])
async def test_command_reply_json_or_plain_text(fake_requester, mode: str) -> None:
    reply = await fake_requester(mode).ask(_question(round_number=4))

    assert reply.outcome == "answered"
    assert reply.answer == REQUESTER_TEXT
    # The command received exactly the documented payload on stdin.
    assert fake_requester.calls() == [
        {
            "question": FIRST_QUESTION,
            "goal": GOAL,
            "draft_answer": "draft",
            "draft_source": "conservative_default",
            "round": 4,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("exit", "nonzero_exit"),
        ("empty", "empty_answer"),
        ("empty_json", "empty_answer"),
        ("bad_json", "invalid_output"),
    ],
)
async def test_command_failures_carry_no_answer(fake_requester, mode: str, outcome: str) -> None:
    reply = await fake_requester(mode).ask(_question())

    assert reply.outcome == outcome
    assert reply.answer is None
    assert reply.detail
    if mode == "exit":
        assert "exit code 3" in reply.detail
        assert "requester unavailable" in reply.detail


@pytest.mark.asyncio
async def test_command_timeout_kills_and_carries_no_answer(fake_requester) -> None:
    reply = await fake_requester("sleep", timeout=0.5).ask(_question())

    assert reply.outcome == "timeout"
    assert reply.answer is None
    assert reply.elapsed_seconds < 10


@pytest.mark.asyncio
async def test_command_launch_failure(tmp_path: Path) -> None:
    requester = CommandRequester((str(tmp_path / "does-not-exist"),), 5.0)

    reply = await requester.ask(_question())

    assert reply.outcome == "launch_failed"
    assert reply.answer is None


# -- settings: option over env ------------------------------------------------


def test_option_takes_precedence_over_env() -> None:
    env = {REQUESTER_COMMAND_ENV: "env-requester --flag", REQUESTER_TIMEOUT_ENV: "12"}

    assert resolve_requester_command("opt-requester 'two words'", env) == (
        "opt-requester",
        "two words",
    )
    assert resolve_requester_command(None, env) == ("env-requester", "--flag")
    assert resolve_requester_timeout(7, env) == 7.0
    assert resolve_requester_timeout(None, env) == 12.0

    built = build_requester("opt-requester", None, env)
    assert built == CommandRequester(("opt-requester",), 12.0)


def test_requester_is_off_without_option_or_env() -> None:
    assert resolve_requester_command(None, {}) is None
    assert build_requester(None, None, {}) is None
    # An explicit empty option turns the requester off even when env names one.
    assert build_requester("", None, {REQUESTER_COMMAND_ENV: "env-requester"}) is None
    assert resolve_requester_timeout(None, {}) == DEFAULT_REQUESTER_TIMEOUT_SECONDS


@pytest.mark.parametrize("bad", ["0", "-1", "soon"])
def test_invalid_timeout_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        resolve_requester_timeout(None, {REQUESTER_TIMEOUT_ENV: bad})


def test_requester_env_is_denied_from_untrusted_project_env() -> None:
    assert is_untrusted_env_denied_key(REQUESTER_COMMAND_ENV)
    assert is_untrusted_env_denied_key(REQUESTER_TIMEOUT_ENV)


# -- driver behavior -----------------------------------------------------------


@pytest.mark.asyncio
async def test_off_keeps_auto_answers_and_state_shape(tmp_path: Path) -> None:
    state, sent, store_root = await _run_driver(tmp_path, requester=None)

    assert sent
    assert sent[0].startswith("[from-auto][conservative_default] ")
    assert all("[requester]" not in text for text in sent)
    assert all(item["source"] != "requester" for item in state.auto_answer_log)
    assert state.requester_log == []
    # Sessions without a requester persist the same JSON keys as before.
    assert "requester_log" not in state.to_dict()
    persisted = json.loads((store_root / f"{state.auto_session_id}.json").read_text())
    assert "requester_log" not in persisted


@pytest.mark.asyncio
async def test_requester_answer_replaces_draft_with_requester_provenance(
    tmp_path: Path, fake_requester
) -> None:
    state, sent, store_root = await _run_driver(tmp_path, fake_requester("json"))

    # Transcript: the backend received the requester's answer, tagged.
    assert sent[0] == f"[from-auto][requester] {REQUESTER_TEXT}"
    # The requester saw the auto draft it replaces.
    call = fake_requester.calls()[0]
    assert call["question"] == FIRST_QUESTION
    assert call["goal"] == GOAL
    assert call["draft_source"] == "conservative_default"
    assert call["draft_answer"].startswith("Treat this requested product behavior")
    assert call["round"] == 1

    first = state.auto_answer_log[0]
    assert first["source"] == "requester"
    assert first["round"] == 1
    assert state.requester_log[0]["outcome"] == "answered"
    assert state.requester_log[0]["used"] is True
    assert state.requester_log[0]["round"] == 1
    assert state.requester_log[0]["draft_source"] == "conservative_default"
    assert state.requester_log[0]["answer"] == REQUESTER_TEXT

    ledger = SeedDraftLedger.from_dict(state.ledger)
    requester_entries = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.key.endswith(".requester.round_1")
    ]
    assert requester_entries
    for entry in requester_entries:
        assert entry.value == REQUESTER_TEXT
        assert entry.source == LedgerSource.USER_PREFERENCE
        assert entry.status == LedgerStatus.CONFIRMED
        assert entry.provenance == DecisionProvenance.USER_CONFIRMED

    persisted = json.loads((store_root / f"{state.auto_session_id}.json").read_text())
    assert persisted["requester_log"][0]["outcome"] == "answered"
    assert persisted["auto_answer_log"][0]["source"] == "requester"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome", "timeout"),
    [("exit", "nonzero_exit", 30.0), ("empty", "empty_answer", 30.0), ("sleep", "timeout", 0.5)],
)
async def test_requester_failure_keeps_draft_and_records_why(
    tmp_path: Path, fake_requester, mode: str, outcome: str, timeout: float
) -> None:
    baseline, baseline_sent, _ = await _run_driver(tmp_path / "baseline", requester=None)
    state, sent, store_root = await _run_driver(
        tmp_path / "failed", fake_requester(mode, timeout=timeout)
    )

    # The draft auto would have sent without a requester is sent unchanged.
    assert sent == baseline_sent
    assert [item["source"] for item in state.auto_answer_log] == [
        item["source"] for item in baseline.auto_answer_log
    ]
    assert len(fake_requester.calls()) == 1
    record = state.requester_log[0]
    assert record["outcome"] == outcome
    assert record["used"] is False
    assert record["answer"] == ""
    assert record["detail"]
    persisted = json.loads((store_root / f"{state.auto_session_id}.json").read_text())
    assert persisted["requester_log"][0]["outcome"] == outcome


_TURN = InterviewTurn(FIRST_QUESTION, "s")


class _RecordingRequester:
    def __init__(self) -> None:
        self.questions: list[RequesterQuestion] = []

    async def ask(self, question: RequesterQuestion) -> RequesterReply:
        self.questions.append(question)
        return RequesterReply("answered", answer=REQUESTER_TEXT)


def _draft(source: AutoAnswerSource, *, blocker: AutoBlocker | None = None) -> AutoAnswer:
    entry = LedgerEntry(
        key="constraints.x",
        value="draft text",
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        status=LedgerStatus.DEFAULTED,
    )
    return AutoAnswer(
        text="draft text",
        source=source,
        confidence=0.8,
        ledger_updates=[("constraints", entry)],
        blocker=blocker,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(AutoAnswerSource))
async def test_only_conservative_default_and_assumption_go_to_requester(
    tmp_path: Path, source: AutoAnswerSource
) -> None:
    requester = _RecordingRequester()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(AsyncMock(), AsyncMock()),
        store=AutoStore(tmp_path),
        requester=requester,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    draft = _draft(source)

    out = await ask_requester(driver, draft, FIRST_QUESTION, _TURN, state, 2)

    routed = {AutoAnswerSource.CONSERVATIVE_DEFAULT, AutoAnswerSource.ASSUMPTION}
    if source in routed:
        assert [q.draft_source for q in requester.questions] == [source.value]
        assert out.source == AutoAnswerSource.REQUESTER
        assert out.text == REQUESTER_TEXT
        assert len(state.requester_log) == 1
    else:
        assert requester.questions == []
        assert out is draft
        assert state.requester_log == []


@pytest.mark.asyncio
async def test_blocked_draft_is_not_routed(tmp_path: Path) -> None:
    requester = _RecordingRequester()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(AsyncMock(), AsyncMock()),
        store=AutoStore(tmp_path),
        requester=requester,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    draft = _draft(
        AutoAnswerSource.CONSERVATIVE_DEFAULT,
        blocker=AutoBlocker(reason="unsafe", question=FIRST_QUESTION),
    )

    out = await ask_requester(driver, draft, FIRST_QUESTION, _TURN, state, 1)

    assert out is draft
    assert requester.questions == []


@pytest.mark.asyncio
async def test_driver_gap_reopen_prompt_is_not_routed(tmp_path: Path) -> None:
    requester = _RecordingRequester()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(AsyncMock(), AsyncMock()),
        store=AutoStore(tmp_path),
        requester=requester,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    draft = _draft(AutoAnswerSource.CONSERVATIVE_DEFAULT)
    synthetic = "[driver gap-reopen 'actors': backend_completed=True ledger_done=False]"

    out = await ask_requester(driver, draft, synthetic, _TURN, state, 1)

    assert out is draft
    assert requester.questions == []


@pytest.mark.asyncio
async def test_requester_called_once_per_routed_question(tmp_path: Path, fake_requester) -> None:
    state, sent, _ = await _run_driver(tmp_path, fake_requester("json"), max_rounds=2)

    calls = fake_requester.calls()
    assert [call["round"] for call in calls] == [1, 2]
    assert [entry["round"] for entry in state.requester_log] == [1, 2]
    assert sum(1 for item in state.auto_answer_log if item["source"] == "requester") == 2
    assert sent[0].startswith("[from-auto][requester] ")
    assert sent[1].startswith("[from-auto][requester] ")


def test_intent_guard_treats_requester_narrowing_as_the_users_decision() -> None:
    goal = "Create MP4 videos and transcripts from the input recordings."
    question = (
        "Which one is the actual MVP behavior to lock: `--mode auto` creates MP4s "
        "and transcripts, or `--mode review` creates only the review package and "
        "rejects automated export?"
    )
    common = {
        "goal": goal,
        "user_preferences": {"outputs": goal, "acceptance_criteria": goal},
        "ledger": SeedDraftLedger.from_goal(goal),
        "question": question,
        "answer_text": "Use REVIEW REVIEW as the MVP behavior.",
    }

    generated = guard_auto_answer(**common, answer_source="conservative_default")
    requester = guard_auto_answer(**common, answer_source=AutoAnswerSource.REQUESTER.value)

    assert generated.failed
    assert not requester.failed


# -- CLI -------------------------------------------------------------------------

runner = CliRunner()


def _cli_result() -> AutoPipelineResult:
    return AutoPipelineResult(
        status="complete",
        auto_session_id="auto_requester_cli",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        interview_session_id="interview_test",
    )


def test_cli_option_overrides_env_and_reaches_run_auto(monkeypatch) -> None:
    monkeypatch.setenv(REQUESTER_COMMAND_ENV, "env-requester")
    monkeypatch.setenv(REQUESTER_TIMEOUT_ENV, "40")
    with patch(
        "ouroboros.cli.commands.auto._run_auto", new=AsyncMock(return_value=_cli_result())
    ) as run_auto:
        result = runner.invoke(
            app,
            ["auto", "safe test goal", "--skip-run", "--requester-command", "opt-requester -q"],
        )

    assert result.exit_code == 0, result.output
    assert run_auto.await_args.kwargs["requester"] == CommandRequester(
        ("opt-requester", "-q"), 40.0
    )


def test_cli_env_alone_enables_requester(monkeypatch) -> None:
    monkeypatch.setenv(REQUESTER_COMMAND_ENV, "env-requester")
    monkeypatch.delenv(REQUESTER_TIMEOUT_ENV, raising=False)
    with patch(
        "ouroboros.cli.commands.auto._run_auto", new=AsyncMock(return_value=_cli_result())
    ) as run_auto:
        result = runner.invoke(
            app, ["auto", "safe test goal", "--skip-run", "--requester-timeout", "9"]
        )

    assert result.exit_code == 0, result.output
    assert run_auto.await_args.kwargs["requester"] == CommandRequester(("env-requester",), 9.0)


def test_cli_without_option_or_env_passes_no_requester(monkeypatch) -> None:
    monkeypatch.delenv(REQUESTER_COMMAND_ENV, raising=False)
    with patch(
        "ouroboros.cli.commands.auto._run_auto", new=AsyncMock(return_value=_cli_result())
    ) as run_auto:
        result = runner.invoke(app, ["auto", "safe test goal", "--skip-run"])

    assert result.exit_code == 0, result.output
    assert run_auto.await_args.kwargs["requester"] is None


def test_cli_rejects_invalid_requester_timeout(monkeypatch) -> None:
    monkeypatch.delenv(REQUESTER_COMMAND_ENV, raising=False)
    with patch(
        "ouroboros.cli.commands.auto._run_auto", new=AsyncMock(return_value=_cli_result())
    ) as run_auto:
        result = runner.invoke(
            app,
            [
                "auto",
                "safe test goal",
                "--requester-command",
                "opt-requester",
                "--requester-timeout",
                "0",
            ],
        )

    assert result.exit_code == 1
    run_auto.assert_not_awaited()


def test_interview_handler_guard_treats_requester_prefix_as_human() -> None:
    """The handler-side guard must not reject what the driver-side guard allowed.

    ``ooo auto`` sends the requester's answer to the interview handler as
    ``[from-auto][requester] ...``; a generated ``[from-auto]`` answer that
    narrows the output contract is rejected there, a requester answer is not.
    """
    from ouroboros.auto.intent_guard import IntentGuardStatus, guard_interview_turn
    from ouroboros.auto.requester import REQUESTER_ANSWER_PREFIX
    from ouroboros.mcp.tools.authoring_handlers import _classify_interview_answer_source

    goal = "Create MP4 videos and transcripts from the input recordings."
    question = (
        "Which one is the actual MVP behavior to lock: `--mode auto` creates MP4s "
        "and transcripts, or `--mode review` creates only the review package and "
        "rejects automated export?"
    )
    requester_answer = AutoAnswer(
        text="Use REVIEW REVIEW as the MVP behavior.",
        source=AutoAnswerSource.REQUESTER,
        confidence=0.83,
    ).prefixed_text
    generated_answer = "[from-auto][conservative_default] Use REVIEW REVIEW as the MVP behavior."

    assert requester_answer.startswith(REQUESTER_ANSWER_PREFIX)
    assert _classify_interview_answer_source(requester_answer) == "requester"
    assert _classify_interview_answer_source(generated_answer) == "generated"

    def status(answer: str) -> IntentGuardStatus:
        return guard_interview_turn(
            goal=goal,
            question=question,
            answer_text=answer,
            answer_source=_classify_interview_answer_source(answer),
        ).status

    assert status(generated_answer) is IntentGuardStatus.FAIL
    assert status(requester_answer) is IntentGuardStatus.WARN
