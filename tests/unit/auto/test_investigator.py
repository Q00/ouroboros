"""Tests for the investigating self-answer (``ouroboros.auto.investigator``).

The interviewer is unchanged. When auto would answer a question with its own
guess, a read-only agent session reads the workspace first. Its answer binds
only when every cited span passes the structural evidence check
(``ouroboros.auto.evidence_check``); otherwise the question stays open and no
guessed text reaches the interviewer, the requester, or the worker.

The agent runtime is a fake injected through ``RepoInvestigator.runtime_factory``,
so these tests exercise the real driver, ledger, and requester code paths
without spawning an agent CLI.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.auto.evidence_check import EvidenceSpan, check_span
from ouroboros.auto.grading import _unverified_provenance_findings
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.investigator import (
    INVESTIGATOR_PERMISSION_MODE,
    INVESTIGATOR_TOOLS,
    MAX_CALLS_ENV,
    NOT_SETTLED_REPLY,
    NOT_SETTLED_VALUE,
    RepoInvestigator,
    build_repo_investigator,
    judge_reply,
    resolve_max_calls,
)
from ouroboros.auto.ledger import LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.ledger_seed import ledger_assumptions
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult

GOAL = "Fix the parser so empty input exits with code 2"
QUESTION = "Which output format should the CLI use?"
GUESS = "GUESSED-TEXT-THAT-MUST-NOT-APPEAR"

_SOURCE = """\
def parse(text):
    if not text:
        raise SystemExit(2)
    return [line.split(",") for line in text.splitlines()]


def render(rows):
    return json.dumps(rows, indent=2)
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "cli.py").write_text(_SOURCE, encoding="utf-8")
    return root


def _found_reply() -> str:
    return json.dumps(
        {
            "status": "found",
            "answer": "The CLI prints rows as indented JSON via render().",
            "evidence": [
                {
                    "path": "src/cli.py",
                    "start_line": 7,
                    "end_line": 8,
                    "quote": "return json.dumps(rows,   indent=2)",
                }
            ],
        }
    )


def _not_found_reply() -> str:
    return json.dumps(
        {
            "status": "not_found",
            "answer": GUESS,
            "evidence": [
                {
                    "path": "src/cli.py",
                    "start_line": 1,
                    "end_line": 3,
                    "quote": "raise SystemExit(2)",
                }
            ],
        }
    )


class _FakeRuntime:
    def __init__(self, reply: Callable[[str], str], calls: list[dict[str, Any]]) -> None:
        self._reply = reply
        self._calls = calls

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **_kw):
        self._calls.append({"prompt": prompt, "tools": tools, "system_prompt": system_prompt})
        return Result.ok(TaskResult(success=True, final_message=self._reply(prompt), messages=()))


def _investigator(
    reply: Callable[[str], str], *, max_calls: int = 12
) -> tuple[RepoInvestigator, list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    factory_kwargs: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _FakeRuntime:
        factory_kwargs.append(kwargs)
        return _FakeRuntime(reply, calls)

    investigator = RepoInvestigator(
        runtime_backend="codex", max_calls=max_calls, runtime_factory=factory
    )
    return investigator, calls, factory_kwargs


class _CountingRequester:
    def __init__(self, answer: str | None = None) -> None:
        self.questions: list[RequesterQuestion] = []
        self._answer = answer

    async def ask(self, question: RequesterQuestion) -> RequesterReply:
        self.questions.append(question)
        if self._answer is None:
            return RequesterReply("empty_answer", detail="no answer")
        return RequesterReply("answered", answer=self._answer)


async def _run(
    workspace: Path,
    *,
    investigator: RepoInvestigator | None = None,
    requester: Any = None,
    refiner: Any = None,
    questions: tuple[str, ...] = (QUESTION,),
    max_rounds: int = 1,
    done_after: int | None = None,
) -> tuple[AutoPipelineState, SeedDraftLedger, list[str], Any]:
    """Run the real driver; the backend asks ``questions`` in turn."""
    sent: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(questions[0], "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        # Like a real interviewer, accept the driver's safe-default synthesis.
        closing = "[safe-default-synthesis]" in text
        if closing or (done_after is not None and len(sent) >= done_after):
            return InterviewTurn(
                "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.1
            )
        return InterviewTurn(questions[len(sent) % len(questions)], session_id)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(workspace.parent / "store"),
        max_rounds=max_rounds,
        timeout_seconds=600,
        requester=requester,
        answer_refiner=refiner,
        investigator=investigator,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")
    ledger = SeedDraftLedger.from_goal(GOAL)
    result = await driver.run(state, ledger)
    return state, ledger, sent, result


def _entries(ledger: SeedDraftLedger, marker: str):
    return [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if marker in entry.key
    ]


# -- evidence check (new module) ----------------------------------------------


def test_valid_span_passes_with_line_window_and_whitespace(workspace: Path) -> None:
    # Cited one line off and with different spacing: within the window, passes.
    span = EvidenceSpan("src/cli.py", 5, 6, "return   json.dumps(rows, indent=2)")
    assert check_span(workspace, span) is None


def test_absent_quote_fails(workspace: Path) -> None:
    span = EvidenceSpan("src/cli.py", 7, 8, "return yaml.safe_dump(rows)")
    assert check_span(workspace, span) == "quote_not_found"


def test_quote_outside_the_cited_window_fails(workspace: Path) -> None:
    # The quote exists in the file, but far from the cited lines.
    span = EvidenceSpan("src/cli.py", 1, 1, "return json.dumps(rows, indent=2)")
    assert check_span(workspace, span) == "quote_not_found"


def test_path_outside_workspace_fails(workspace: Path) -> None:
    (workspace.parent / "secret.py").write_text("token = 'abcdefghij'\n", encoding="utf-8")
    span = EvidenceSpan("../secret.py", 1, 1, "token = 'abcdefghij'")
    assert check_span(workspace, span) == "outside_workspace"
    absolute = EvidenceSpan(str(workspace.parent / "secret.py"), 1, 1, "token = 'abcdefghij'")
    assert check_span(workspace, absolute) == "outside_workspace"


def test_symlink_escaping_workspace_fails(workspace: Path) -> None:
    outside = workspace.parent / "outside.py"
    outside.write_text("value = 'outside the repo'\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(outside)
    span = EvidenceSpan("link.py", 1, 1, "value = 'outside the repo'")
    assert check_span(workspace, span) == "outside_workspace"


def test_missing_file_and_bad_range_fail(workspace: Path) -> None:
    assert check_span(workspace, EvidenceSpan("nope.py", 1, 1, "something long")) == "missing_file"
    assert check_span(workspace, EvidenceSpan("src/cli.py", 3, 2, "raise SystemExit")) == (
        "bad_range"
    )
    assert check_span(workspace, EvidenceSpan("src/cli.py", 1, 1, "if")) == "quote_too_short"


def test_judge_found_requires_every_span_to_pass(workspace: Path) -> None:
    assert judge_reply(workspace, _found_reply()).outcome == "found"
    reply = json.loads(_found_reply())
    reply["evidence"].append(
        {"path": "src/cli.py", "start_line": 1, "end_line": 2, "quote": "def invented()"}
    )
    verdict = judge_reply(workspace, json.dumps(reply))
    assert verdict.outcome == "evidence_rejected"
    assert verdict.answer == ""
    assert verdict.failures == ("quote_not_found",)
    # The span that did pass is kept as what the code shows.
    assert [span.label for span in verdict.checked] == ["src/cli.py:7-8"]


def test_judge_found_without_evidence_is_not_found(workspace: Path) -> None:
    reply = json.dumps({"status": "found", "answer": GUESS, "evidence": []})
    verdict = judge_reply(workspace, reply)
    assert verdict.outcome == "evidence_rejected"
    assert verdict.answer == ""


def test_judge_reads_fenced_json_and_rejects_garbage(workspace: Path) -> None:
    assert judge_reply(workspace, f"```json\n{_found_reply()}\n```").outcome == "found"
    assert judge_reply(workspace, "I think it is JSON output").outcome == "reply_invalid"
    assert judge_reply(workspace, '{"status": "maybe"}').outcome == "reply_invalid"


# -- driver integration (new API: AutoInterviewDriver.investigator) -----------


@pytest.mark.asyncio
async def test_found_answer_binds_as_confirmed_repo_fact(workspace: Path) -> None:
    investigator, calls, factory_kwargs = _investigator(lambda _p: _found_reply())

    _state, ledger, sent, _ = await _run(workspace, investigator=investigator)

    assert len(calls) == 1
    assert calls[0]["tools"] == list(INVESTIGATOR_TOOLS)
    assert QUESTION in calls[0]["prompt"] and GOAL in calls[0]["prompt"]
    assert factory_kwargs == [
        {
            "backend": "codex",
            "model": None,
            "permission_mode": INVESTIGATOR_PERMISSION_MODE,
            "cwd": workspace,
        }
    ]
    facts = _entries(ledger, ".repo.")
    assert facts
    assert all(entry.source == LedgerSource.REPO_FACT for entry in facts)
    assert all(entry.status == LedgerStatus.CONFIRMED for entry in facts)
    assert all(entry.binds_seed for entry in facts)
    assert all(entry.evidence == ["src/cli.py:7-8"] for entry in facts)
    assert sent[0].startswith("[from-auto][repo_fact] The CLI prints rows as indented JSON")
    assert "src/cli.py:7-8" in sent[0]


@pytest.mark.asyncio
async def test_not_found_renders_only_the_open_question(workspace: Path) -> None:
    investigator, _calls, _ = _investigator(lambda _p: _not_found_reply())

    _state, ledger, sent, _ = await _run(workspace, investigator=investigator)

    assert sent[0].startswith(f"[from-auto][assumption] {NOT_SETTLED_REPLY}")
    assert "src/cli.py:1-3: raise SystemExit(2)" in sent[0]
    assert GUESS not in sent[0]
    open_entries = _entries(ledger, ".investigated.")
    assert open_entries
    assert all(not entry.binds_seed for entry in open_entries)
    assert all(entry.status == LedgerStatus.DEFAULTED for entry in open_entries)
    assert all(entry.question == QUESTION for entry in open_entries)
    lines = ledger_assumptions(ledger)
    assert f"Open question: {QUESTION}" in lines
    assert not any(NOT_SETTLED_VALUE in line or GUESS in line for line in lines)
    assert not any(GUESS in entry.value for entry in open_entries)


@pytest.mark.asyncio
async def test_failed_evidence_check_is_not_found_and_drops_answer(workspace: Path) -> None:
    bad = json.dumps(
        {
            "status": "found",
            "answer": GUESS,
            "evidence": [
                {"path": "src/cli.py", "start_line": 7, "end_line": 8, "quote": "xml!!!!!"}
            ],
        }
    )
    investigator, _calls, _ = _investigator(lambda _p: bad)

    _state, ledger, sent, _ = await _run(workspace, investigator=investigator)

    assert not _entries(ledger, ".repo.")
    assert _entries(ledger, ".investigated.")
    assert GUESS not in sent[0]
    assert sent[0].startswith(f"[from-auto][assumption] {NOT_SETTLED_REPLY}")


@pytest.mark.asyncio
async def test_found_question_is_not_sent_to_requester(workspace: Path) -> None:
    investigator, calls, _ = _investigator(lambda _p: _found_reply())
    requester = _CountingRequester("requester text")

    await _run(workspace, investigator=investigator, requester=requester)

    assert len(calls) == 1
    assert requester.questions == []


@pytest.mark.asyncio
async def test_not_found_question_goes_to_requester_without_guessed_draft(
    workspace: Path,
) -> None:
    investigator, calls, _ = _investigator(lambda _p: _not_found_reply())
    requester = _CountingRequester("Emit indented JSON; nothing else changes.")

    state, _ledger, sent, _ = await _run(workspace, investigator=investigator, requester=requester)

    assert len(calls) == 1
    assert len(requester.questions) == 1
    draft = requester.questions[0].draft_answer
    assert draft.startswith(NOT_SETTLED_REPLY)
    assert GUESS not in draft
    assert requester.questions[0].draft_source == "assumption"
    assert sent[0].startswith("[from-auto][requester] Emit indented JSON")
    assert state.requester_log[0]["outcome"] == "answered"


@pytest.mark.asyncio
async def test_investigated_answers_skip_the_refiner(workspace: Path) -> None:
    refiner = AsyncMock(return_value=GUESS)
    investigator, _calls, _ = _investigator(lambda _p: _not_found_reply())

    _state, _ledger, sent, _ = await _run(workspace, investigator=investigator, refiner=refiner)

    refiner.assert_not_awaited()
    assert GUESS not in sent[0]


@pytest.mark.asyncio
async def test_call_cap_treats_remaining_questions_as_not_found(workspace: Path) -> None:
    investigator, calls, _ = _investigator(lambda _p: _found_reply(), max_calls=2)
    questions = (
        QUESTION,
        "Which output encoding should the CLI write?",
        "Which output file name should the CLI use?",
        "Which output order should rows have?",
    )

    _state, ledger, sent, _ = await _run(
        workspace, investigator=investigator, questions=questions, max_rounds=4
    )

    assert len(calls) == 2
    assert investigator.calls_made == 2
    assert all(text.startswith("[from-auto][repo_fact]") for text in sent[:2])
    assert all(
        text.startswith(f"[from-auto][assumption] {NOT_SETTLED_REPLY}") for text in sent[2:4]
    )
    assert _entries(ledger, ".investigated.")


@pytest.mark.asyncio
async def test_two_found_answers_on_one_facet_do_not_conflict(workspace: Path) -> None:
    replies = iter(
        [
            _found_reply(),
            json.dumps(
                {
                    "status": "found",
                    "answer": "Empty input exits with code 2.",
                    "evidence": [
                        {
                            "path": "src/cli.py",
                            "start_line": 2,
                            "end_line": 3,
                            "quote": "raise SystemExit(2)",
                        }
                    ],
                }
            ),
        ]
    )
    investigator, _calls, _ = _investigator(lambda _p: next(replies))

    state, ledger, sent, result = await _run(
        workspace, investigator=investigator, questions=(QUESTION, QUESTION), max_rounds=2
    )

    assert all(text.startswith("[from-auto][repo_fact]") for text in sent[:2])
    assert result.status != "blocked"
    assert ledger.count_active_conflicting_entries() == 0
    assert state.last_error_code is None


@pytest.mark.asyncio
async def test_unavailable_runtime_keeps_the_deterministic_answerer(workspace: Path) -> None:
    def broken_factory(**_kwargs: Any) -> Any:
        raise RuntimeError("no backend")

    investigator = RepoInvestigator(runtime_backend="codex", runtime_factory=broken_factory)

    _s1, ledger_on, sent_on, _ = await _run(workspace, investigator=investigator)
    _s2, ledger_off, sent_off, _ = await _run(workspace, investigator=None)

    assert investigator.unavailable == "runtime_create_failed:RuntimeError"
    assert sent_on == sent_off
    assert ledger_on.to_dict()["sections"] == ledger_off.to_dict()["sections"]


@pytest.mark.parametrize("done_after", [3, None])
@pytest.mark.asyncio
async def test_all_not_found_interview_converges_like_the_old_answerer(
    workspace: Path, done_after: int | None
) -> None:
    """Every answer not found: the interview ends the same way it ends without
    the investigator (backend closure, or the round cap), never blocked, never on
    the deadline path, and without unverified-provenance grading findings."""
    questions = (
        "Who uses this CLI and what are its inputs and outputs?",
        "Which runtime and project patterns should be used?",
        "What constraints and failure modes should bound this change?",
        "Which command output verifies the acceptance criteria?",
    )
    investigator, calls, _ = _investigator(lambda _p: _not_found_reply())

    state_on, ledger_on, sent_on, result_on = await _run(
        workspace,
        investigator=investigator,
        questions=questions,
        max_rounds=4,
        done_after=done_after,
    )
    state_off, _ledger_off, sent_off, result_off = await _run(
        workspace, questions=questions, max_rounds=4, done_after=done_after
    )

    assert calls
    assert all(GUESS not in text for text in sent_on)
    assert result_on.status == result_off.status
    assert result_on.status == "seed_ready"
    assert state_on.interview_closure_mode == state_off.interview_closure_mode
    assert state_on.last_error_code == state_off.last_error_code
    assert result_on.rounds <= 4
    assert len(sent_on) == len(sent_off)
    assert _unverified_provenance_findings(ledger_on) == []


# -- configuration and wiring ---------------------------------------------------


def test_max_calls_config() -> None:
    assert resolve_max_calls({}) == 12
    assert resolve_max_calls({MAX_CALLS_ENV: "3"}) == 3
    with pytest.raises(ValueError):
        resolve_max_calls({MAX_CALLS_ENV: "-1"})
    with pytest.raises(ValueError):
        resolve_max_calls({MAX_CALLS_ENV: "many"})
    assert build_repo_investigator("codex", None, {MAX_CALLS_ENV: "0"}) is None
    assert build_repo_investigator(None, None, {}) is None
    built = build_repo_investigator("codex", None, {MAX_CALLS_ENV: "5"})
    assert built is not None and built.max_calls == 5


def test_plugin_dispatched_stage_gets_no_investigator() -> None:
    assert build_repo_investigator("opencode", "plugin", {}) is None


def test_max_calls_env_is_denied_from_untrusted_project_env() -> None:
    assert is_untrusted_env_denied_key(MAX_CALLS_ENV)
