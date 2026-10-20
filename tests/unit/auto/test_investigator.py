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

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import time
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
from ouroboros.auto.investigation_workspace import (
    IsolationError,
    create_isolated_copy,
    remove_isolated_copy,
    workspace_state,
)
from ouroboros.auto.investigator import (
    CALL_TIMEOUT_ENV,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    INVESTIGATOR_PERMISSION_MODE,
    INVESTIGATOR_TOOLS,
    MAX_CALLS_ENV,
    NOT_SETTLED_REPLY,
    NOT_SETTLED_VALUE,
    OPEN_FOR_IMPLEMENTER_REPLY,
    RepoInvestigator,
    build_repo_investigator,
    enforces_read_only,
    judge_reply,
    resolve_call_timeout,
    resolve_max_calls,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.ledger_seed import (
    ledger_assumptions,
    partial_seed_from_evidence,
    synthesize_seed_from_ledger,
    with_ledger_carryover,
)
from ouroboros.auto.phase_clock import phase_clock_paused, wait_for_pausable
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.bigbang.answer_provenance import (
    ANSWER_PROVENANCE_ARGUMENT,
    WITHHELD_REPO_FACT_NOTE,
    declare_provenance,
    extraction_rounds,
)
from ouroboros.bigbang.interview import InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
from ouroboros.core.seed import RepoFact, Seed, ac_texts
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import (
    render_acceptance_criteria_section,
    render_seed_contract_for_execution,
)
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult

GOAL = "Fix the parser so empty input exits with code 2"
QUESTION = "Which output format should the CLI use?"
GUESS = "GUESSED-TEXT-THAT-MUST-NOT-APPEAR"
FOUND_ANSWER = "The CLI prints rows as indented JSON via render()."
EVIDENCE_LINE = "src/cli.py:7-8: return json.dumps(rows, indent=2)"
VERIFY_QUESTION = "Which command output verifies the acceptance criteria?"

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
            "answer": FOUND_ANSWER,
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
    """A Codex-shaped runtime: its exec flags are the read-only sandbox."""

    _runtime_backend = "codex"

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

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
async def test_found_answer_is_a_confirmed_checked_repo_fact(workspace: Path) -> None:
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
    facts = _entries(ledger, ".repo_fact.")
    assert facts
    assert all(entry.source == LedgerSource.REPO_FACT for entry in facts)
    assert all(entry.status == LedgerStatus.CONFIRMED for entry in facts)
    assert all(entry.evidence == [EVIDENCE_LINE] for entry in facts)
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

    assert not _entries(ledger, ".repo_fact.")
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


async def _run_never_confirming(
    workspace: Path,
    investigator: RepoInvestigator,
    *,
    requester: Any = None,
    goal: str = GOAL,
    max_rounds: int = 30,
) -> tuple[AutoPipelineState, SeedDraftLedger, list[str], Any, list[tuple[str, dict]]]:
    """An interviewer that never confirms readiness and always asks a new question."""
    sent: list[str] = []
    events: list[tuple[str, dict]] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Which output format should the CLI use, question 0?", "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        return InterviewTurn(
            f"Which output detail should the CLI settle, question {len(sent)}?",
            session_id,
            ambiguity_score=0.6,
        )

    class _Events:
        async def append(self, event: Any) -> None:
            events.append((event.type, dict(event.data)))

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(workspace.parent / "store"),
        max_rounds=max_rounds,
        timeout_seconds=600,
        requester=requester,
        investigator=investigator,
        event_store=_Events(),  # type: ignore[arg-type]
    )
    state = AutoPipelineState(goal=goal, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")
    ledger = SeedDraftLedger.from_goal(goal)
    result = await driver.run(state, ledger)
    await driver.wait_for_pending_emits()
    return state, ledger, sent, result, events


@pytest.mark.asyncio
async def test_without_requester_the_interview_closes_at_the_call_cap(workspace: Path) -> None:
    """Once the last call is spent, the interview closes with what is known:
    no ``cap_reached`` rounds, no deadline, no backend confirmation needed."""
    investigator, calls, _ = _investigator(lambda _p: _not_found_reply(), max_calls=3)

    state, ledger, sent, result, events = await _run_never_confirming(workspace, investigator)

    assert len(calls) == 3
    assert result.status == "seed_ready"
    assert state.interview_completed is True
    assert state.interview_closure_mode == "ledger_only"
    assert state.last_error_code is None
    answers = [text for text in sent if "[safe-default-synthesis]" not in text]
    assert len(answers) == 3  # one answer per investigated question, none after the cap
    assert ledger.is_seed_ready()
    closed = [data for kind, data in events if kind == "auto.interview.closed_with_open_questions"]
    assert len(closed) == 1 and closed[0]["calls_made"] == 3
    outcomes = [data["outcome"] for kind, data in events if kind == "auto.interview.investigated"]
    assert "cap_reached" not in outcomes
    seed = synthesize_seed_from_ledger(ledger)
    assert not seed.metadata.degraded
    lines = ledger_assumptions(ledger)
    assert sum(line.startswith("Open question: ") for line in lines) == 3
    assert not any(GUESS in line or NOT_SETTLED_VALUE in line for line in lines)


@pytest.mark.asyncio
async def test_with_requester_the_cap_does_not_close_the_interview(workspace: Path) -> None:
    """With a requester, a question past the cap still goes to the requester."""
    investigator, calls, _ = _investigator(lambda _p: _not_found_reply(), max_calls=2)
    requester = _CountingRequester(None)

    state, _ledger, sent, _result, events = await _run_never_confirming(
        workspace, investigator, requester=requester, max_rounds=5
    )

    assert len(calls) == 2
    assert state.interview_closure_mode != "ledger_only"
    assert not [kind for kind, _ in events if kind == "auto.interview.closed_with_open_questions"]
    assert len(requester.questions) == 5  # every round, past the cap too
    assert len([text for text in sent if "[safe-default-synthesis]" not in text]) >= 5


@pytest.mark.asyncio
async def test_no_requester_not_found_reply_leaves_the_question_to_the_implementer(
    workspace: Path,
) -> None:
    investigator, _calls, _ = _investigator(lambda _p: _not_found_reply())
    _s, _l, sent_alone, _ = await _run(workspace, investigator=investigator)
    assert sent_alone[0].startswith(
        f"[from-auto][assumption] {NOT_SETTLED_REPLY} {OPEN_FOR_IMPLEMENTER_REPLY}"
    )

    investigator2, _c, _ = _investigator(lambda _p: _not_found_reply())
    requester = _CountingRequester(None)
    await _run(workspace, investigator=investigator2, requester=requester)
    assert requester.questions[0].draft_answer.startswith(NOT_SETTLED_REPLY)
    assert OPEN_FOR_IMPLEMENTER_REPLY not in requester.questions[0].draft_answer


@pytest.mark.asyncio
async def test_cap_closure_keeps_found_answers_and_open_questions(workspace: Path) -> None:
    replies = iter([_found_reply(), _not_found_reply()])
    investigator, _calls, _ = _investigator(lambda _p: next(replies), max_calls=2)

    state, ledger, _sent, result, _events = await _run_never_confirming(workspace, investigator)

    assert result.status == "seed_ready" and state.interview_closure_mode == "ledger_only"
    seed = synthesize_seed_from_ledger(ledger)
    assert [fact.answer for fact in seed.repo_facts] == [FOUND_ANSWER]
    assert any(line.startswith("Open question: ") for line in ledger_assumptions(ledger))


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


# -- found answers reach the worker only as checked repository facts --------


class _ProvenanceBackend:
    """Interview backend that records the provenance declared with each answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG002
        return InterviewTurn(QUESTION, "s")

    async def answer(
        self,
        session_id: str,
        answer: str,
        *,
        last_question: str | None = None,  # noqa: ARG002
        provenance: str | None = None,
    ) -> InterviewTurn:
        self.calls.append((answer, provenance))
        return InterviewTurn("What else should we consider?", session_id)

    async def resume(self, session_id: str) -> InterviewTurn:
        return InterviewTurn(QUESTION, session_id)


@pytest.mark.asyncio
async def test_found_answer_is_declared_repo_fact_provenance(workspace: Path) -> None:
    investigator, _calls, _ = _investigator(lambda _p: _found_reply())
    backend = _ProvenanceBackend()
    driver = AutoInterviewDriver(
        backend,
        store=AutoStore(workspace.parent / "store"),
        max_rounds=1,
        timeout_seconds=600,
        investigator=investigator,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")

    await driver.run(state, SeedDraftLedger.from_goal(GOAL))

    text, provenance = backend.calls[0]
    assert FOUND_ANSWER in text
    assert provenance == "repo_fact"


def test_seed_extraction_withholds_a_repo_fact_answer() -> None:
    state = InterviewState(interview_id="interview_0000000000000009", initial_context=GOAL)
    declare_provenance(
        state.record_answer(QUESTION, f"[from-auto][repo_fact] {FOUND_ANSWER}"),
        {ANSWER_PROVENANCE_ARGUMENT: "repo_fact"},
    )
    state.record_answer("Which error must go away?", "The empty-input crash.")

    projected = extraction_rounds(state)
    context = SeedGenerator(llm_adapter=None)._build_interview_context(state)  # type: ignore[arg-type]
    reloaded = InterviewState.model_validate(state.model_dump(mode="json"))

    assert projected[0].withheld is True
    assert projected[0].answer == WITHHELD_REPO_FACT_NOTE
    assert FOUND_ANSWER not in context
    assert WITHHELD_REPO_FACT_NOTE in context
    assert "The empty-input crash." in context
    assert [r.provenance for r in reloaded.rounds] == ["repo_fact", "user"]


@pytest.mark.asyncio
async def test_found_answer_reaches_worker_only_as_repo_facts_block(workspace: Path) -> None:
    """A found answer settles its sections, but the Seed carries it only in
    ``repo_facts``: never in constraints, acceptance criteria or ontology."""
    investigator, _calls, _ = _investigator(lambda _p: _found_reply())

    _state, ledger, _sent, result = await _run(
        workspace, investigator=investigator, questions=(VERIFY_QUESTION,)
    )

    assert result.status == "seed_ready"
    assert any(
        ".repo_fact." in entry.key for entry in ledger.sections["acceptance_criteria"].entries
    )
    for seed in (
        synthesize_seed_from_ledger(ledger),
        partial_seed_from_evidence(ledger, reason="test"),
    ):
        assert seed.repo_facts == (
            RepoFact(question=VERIFY_QUESTION, answer=FOUND_ANSWER, evidence=(EVIDENCE_LINE,)),
        )
        bound = [*ac_texts(seed.acceptance_criteria), *seed.constraints]
        bound += [field.description for field in seed.ontology_schema.fields]
        # Seed lines drop trailing punctuation, so look for the answer's core.
        core = FOUND_ANSWER.rstrip(".")
        assert not any(core in text for text in bound)
        assert not any(core in text for text in seed.assumptions)
        assert Seed.from_dict(seed.to_dict()).repo_facts == seed.repo_facts

    contract = SeedContract.from_seed(synthesize_seed_from_ledger(ledger))
    prompt = render_seed_contract_for_execution(contract)
    block = prompt.split("## Repository facts (checked)", 1)[1].split("\n## ", 1)[0]
    assert f"> {FOUND_ANSWER}" in block
    assert f"- {EVIDENCE_LINE}" in block
    assert f"> {VERIFY_QUESTION}" in block
    assert FOUND_ANSWER.rstrip(".") not in render_acceptance_criteria_section(contract)


def test_generated_seed_gets_repo_facts_from_the_ledger() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.output.repo_fact.1",
            value=FOUND_ANSWER,
            source=LedgerSource.REPO_FACT,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            evidence=[EVIDENCE_LINE],
            question=QUESTION,
        ),
    )
    seed = partial_seed_from_evidence(SeedDraftLedger.from_goal(GOAL), reason="test")
    assert seed.repo_facts == ()
    assert "repo_facts" not in seed.to_dict()

    carried = with_ledger_carryover(seed, ledger)

    assert carried.repo_facts == (
        RepoFact(question=QUESTION, answer=FOUND_ANSWER, evidence=(EVIDENCE_LINE,)),
    )
    assert with_ledger_carryover(carried, ledger) is carried


# -- no time budget: the phase clock stops while the investigator runs ---------


class _SlowRuntime(_FakeRuntime):
    def __init__(self, reply: Callable[[str], str], calls: list[dict[str, Any]], delay: float):
        super().__init__(reply, calls)
        self._delay = delay

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **kw):
        await asyncio.sleep(self._delay)
        return await super().execute_task_to_result(prompt, tools, system_prompt, **kw)


@pytest.mark.asyncio
async def test_paused_clock_stops_the_phase_timeout_and_moves_the_deadline() -> None:
    state = AutoPipelineState(goal=GOAL, cwd=".")
    state.deadline_at = time.monotonic() + 100.0
    state.deadline_at_epoch = time.time() + 100.0
    before = (state.deadline_at, state.deadline_at_epoch)

    async def investigate() -> str:
        async with phase_clock_paused(state):
            await asyncio.sleep(0.3)
        return "done"

    assert await wait_for_pausable(investigate(), 0.1) == "done"
    assert state.deadline_at - before[0] >= 0.29
    assert state.deadline_at_epoch - before[1] >= 0.29

    async def unpaused() -> None:
        await asyncio.sleep(0.3)

    with pytest.raises(TimeoutError):
        await wait_for_pausable(unpaused(), 0.1)


@pytest.mark.asyncio
async def test_slow_investigation_never_hits_the_interview_phase_timeout(
    workspace: Path,
) -> None:
    """Each investigation takes longer than the whole phase timeout; the
    interview still finishes normally because the clock stops while it runs."""
    calls: list[dict[str, Any]] = []
    investigator = RepoInvestigator(
        runtime_backend="codex",
        runtime_factory=lambda **_k: _SlowRuntime(lambda _p: _not_found_reply(), calls, 0.4),
    )

    _state, _ledger, sent, result = await wait_for_pausable(
        _run(
            workspace,
            investigator=investigator,
            questions=(QUESTION, "Which output encoding should the CLI write?"),
            max_rounds=2,
        ),
        0.3,
    )

    assert len(calls) == 2
    assert result.status == "seed_ready"
    assert all(text.startswith("[from-auto]") for text in sent)


@pytest.mark.asyncio
async def test_hang_guard_turns_a_stuck_call_into_not_found(workspace: Path) -> None:
    calls: list[dict[str, Any]] = []
    investigator = RepoInvestigator(
        runtime_backend="codex",
        call_timeout_seconds=0.05,
        runtime_factory=lambda **_k: _SlowRuntime(lambda _p: _found_reply(), calls, 5.0),
    )

    _state, ledger, sent, _ = await _run(workspace, investigator=investigator)

    assert not _entries(ledger, ".repo_fact.")
    assert sent[0].startswith(f"[from-auto][assumption] {NOT_SETTLED_REPLY}")


# -- isolation: read-only mode where verified, a throwaway copy elsewhere -------


class _GeminiShapedRuntime(_FakeRuntime):
    """A runtime without a read-only mode that edits whatever its cwd holds."""

    _runtime_backend = "gemini"

    def __init__(self, reply, calls, cwd: Path, real: Path | None = None) -> None:
        super().__init__(reply, calls)
        self.cwd = Path(cwd)
        self._real = real

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **kw):
        (self.cwd / "EDITED_BY_RUNTIME.txt").write_text("edit", encoding="utf-8")
        (self.cwd / "src" / "cli.py").write_text("def invented():\n    pass\n", encoding="utf-8")
        if self._real is not None:  # a runtime that escapes its cwd
            (self._real / "ESCAPED.txt").write_text("escape", encoding="utf-8")
        return await super().execute_task_to_result(prompt, tools, system_prompt, **kw)


class _WritableCodexRuntime(_FakeRuntime):
    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "workspace-write"]


def _isolating_investigator(
    runtime_cls: type, reply: Callable[[str], str], **runtime_kwargs: Any
) -> tuple[RepoInvestigator, list[dict[str, Any]], list[Path]]:
    calls: list[dict[str, Any]] = []
    cwds: list[Path] = []

    def factory(**kwargs: Any) -> Any:
        cwds.append(Path(kwargs["cwd"]))
        if runtime_cls is _GeminiShapedRuntime:
            return runtime_cls(reply, calls, kwargs["cwd"], **runtime_kwargs)
        return runtime_cls(reply, calls)

    backend = "gemini" if runtime_cls is _GeminiShapedRuntime else "codex"
    return RepoInvestigator(runtime_backend=backend, runtime_factory=factory), calls, cwds


@pytest.mark.parametrize(
    ("backend", "admitted"),
    [
        ("codex", "codex"),
        ("codex_cli", "codex"),
        ("claude", "claude"),
        ("gemini", "gemini"),
        ("opencode", "opencode"),
        ("codex_mcp", "codex_mcp"),
        ("claude_mcp", "claude_mcp"),
        ("not-a-backend", None),
    ],
)
def test_every_known_backend_gets_an_investigator(backend: str, admitted: str | None) -> None:
    built = build_repo_investigator(backend, None, {})
    assert (built.runtime_backend if built else None) == admitted


@pytest.mark.asyncio
async def test_codex_reads_the_real_workspace_in_read_only_mode(workspace: Path) -> None:
    investigator, calls, kwargs = _investigator(lambda _p: _found_reply())

    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert result is not None and result.found
    assert result.isolation == "read_only_mode"
    assert [Path(k["cwd"]) for k in kwargs] == [workspace]
    assert kwargs[0]["permission_mode"] == "default"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_runtime_without_read_only_mode_reads_a_copy(workspace: Path) -> None:
    """Gemini-shaped runtime edits its cwd; the real workspace is untouched and
    the answer is still checked against the real files."""
    before = (workspace / "src" / "cli.py").read_text(encoding="utf-8")
    investigator, calls, cwds = _isolating_investigator(
        _GeminiShapedRuntime, lambda _p: _found_reply()
    )

    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert len(calls) == 1
    assert result is not None and result.isolation == "workspace_copy"
    assert cwds and cwds[0] != workspace and not cwds[0].is_relative_to(workspace)
    assert not cwds[0].exists()  # the copy is deleted afterwards
    assert not (workspace / "EDITED_BY_RUNTIME.txt").exists()
    assert (workspace / "src" / "cli.py").read_text(encoding="utf-8") == before
    # The runtime rewrote cli.py in its copy, yet the citation passes because it
    # matches the real file.
    assert result.found
    assert [span.label for span in result.checked] == ["src/cli.py:7-8"]


@pytest.mark.asyncio
async def test_the_copy_cannot_fabricate_evidence(workspace: Path) -> None:
    fabricated = json.dumps(
        {
            "status": "found",
            "answer": GUESS,
            "evidence": [
                {"path": "src/cli.py", "start_line": 1, "end_line": 2, "quote": "def invented():"}
            ],
        }
    )
    investigator, _calls, _cwds = _isolating_investigator(
        _GeminiShapedRuntime, lambda _p: fabricated
    )

    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert result is not None and result.outcome == "evidence_rejected"
    assert result.failures == ("quote_not_found",)


@pytest.mark.asyncio
async def test_absolute_paths_inside_the_copy_are_read_as_workspace_paths(
    workspace: Path,
) -> None:
    seen: dict[str, Path] = {}

    def reply(_prompt: str) -> str:
        data = json.loads(_found_reply())
        data["evidence"][0]["path"] = str(seen["cwd"] / "src" / "cli.py")
        return json.dumps(data)

    investigator, _calls, cwds = _isolating_investigator(_GeminiShapedRuntime, reply)
    original = investigator.runtime_factory
    assert original is not None

    def factory(**kwargs: Any) -> Any:
        seen["cwd"] = Path(kwargs["cwd"])
        return original(**kwargs)

    investigator.runtime_factory = factory

    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert result is not None and result.found


@pytest.mark.asyncio
async def test_real_workspace_change_discards_the_answer(workspace: Path) -> None:
    investigator, _calls, _cwds = _isolating_investigator(
        _GeminiShapedRuntime, lambda _p: _found_reply(), real=workspace
    )

    result = await investigator.investigate(GOAL, QUESTION, workspace)
    assert result is not None and result.outcome == "workspace_changed"

    (workspace / "ESCAPED.txt").unlink()
    investigator2, _c, _w = _isolating_investigator(
        _GeminiShapedRuntime, lambda _p: _found_reply(), real=workspace
    )
    _state, ledger, sent, _ = await _run(workspace, investigator=investigator2)
    assert not _entries(ledger, ".repo_fact.")
    assert sent[0].startswith(f"[from-auto][assumption] {NOT_SETTLED_REPLY}")


@pytest.mark.asyncio
async def test_codex_without_read_only_flags_reads_a_copy(workspace: Path) -> None:
    investigator, calls, cwds = _isolating_investigator(
        _WritableCodexRuntime, lambda _p: _found_reply()
    )

    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert result is not None and result.isolation == "workspace_copy"
    assert cwds[0] == workspace and cwds[-1] != workspace
    assert len(calls) == 1


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_git_copy_holds_the_working_tree_without_ignored_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "tracked.py").write_text("committed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "tracked.py").write_text("edited on disk\n", encoding="utf-8")
    (root / "new.py").write_text("untracked\n", encoding="utf-8")
    (root / "ignored.txt").write_text("secret\n", encoding="utf-8")
    state = workspace_state(root)

    copy = create_isolated_copy(root)
    try:
        assert (copy / "tracked.py").read_text(encoding="utf-8") == "edited on disk\n"
        assert (copy / "new.py").read_text(encoding="utf-8") == "untracked\n"
        assert not (copy / "ignored.txt").exists()
        assert not (copy / ".git").exists()
        (copy / "tracked.py").write_text("changed in the copy\n", encoding="utf-8")
    finally:
        remove_isolated_copy(copy)
    assert not copy.exists()
    assert workspace_state(root) == state
    (root / "new.py").write_text("untracked, edited\n", encoding="utf-8")
    assert workspace_state(root) != state


# -- the copy never holds a path that resolves outside it --------------------


def _linked_workspace(tmp_path: Path, *, git: bool) -> tuple[Path, Path]:
    """A workspace with every kind of symbolic link, and an outside folder."""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    (root / "pkg").mkdir(parents=True)
    outside.mkdir()
    (root / "pkg" / "real.py").write_text("REAL = 1\n", encoding="utf-8")
    (outside / "secret.txt").write_text("outside bytes\n", encoding="utf-8")
    (root / "file_link.py").symlink_to("pkg/real.py")
    (root / "abs_file_link.py").symlink_to(root / "pkg" / "real.py")
    (root / "dir_link").symlink_to("pkg")
    (root / "outside_file_link.txt").symlink_to(outside / "secret.txt")
    (root / "outside_dir_link").symlink_to(outside)
    (root / "dangling_outside").symlink_to(outside / "missing.txt")
    (root / "dangling_inside").symlink_to("pkg/missing.py")
    if git:
        _git(root, "init", "-q")
    return root, outside


def _resolves_inside(path: Path, root: Path) -> bool:
    resolved = Path(os.path.realpath(path))
    return resolved == root or resolved.is_relative_to(root)


@pytest.mark.parametrize("git", [False, True])
def test_copy_links_never_lead_outside_the_copy(tmp_path: Path, git: bool) -> None:
    root, outside = _linked_workspace(tmp_path, git=git)

    copy = create_isolated_copy(root)
    try:
        for name in ("file_link.py", "abs_file_link.py", "dir_link", "dangling_inside"):
            assert (copy / name).is_symlink()
            assert _resolves_inside(copy / name, copy)
        assert (copy / "file_link.py").read_text(encoding="utf-8") == "REAL = 1\n"
        assert (copy / "dir_link" / "real.py").read_text(encoding="utf-8") == "REAL = 1\n"
        # An outside regular file arrives as bytes, never as a link.
        assert not (copy / "outside_file_link.txt").is_symlink()
        assert (copy / "outside_file_link.txt").read_text(encoding="utf-8") == "outside bytes\n"
        assert not (copy / "outside_dir_link").exists()
        assert not (copy / "dangling_outside").exists()
        assert not (copy / "dangling_outside").is_symlink()
        for current, dirs, files in os.walk(copy):
            for name in (*dirs, *files):
                assert _resolves_inside(Path(current) / name, copy), name
    finally:
        remove_isolated_copy(copy)


@pytest.mark.parametrize("git", [False, True])
@pytest.mark.asyncio
async def test_writing_runtime_through_links_leaves_real_targets_unchanged(
    tmp_path: Path, git: bool
) -> None:
    root, outside = _linked_workspace(tmp_path, git=git)
    (root / "src").mkdir()
    (root / "src" / "cli.py").write_text(_SOURCE, encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class _LinkWriter(_FakeRuntime):
        _runtime_backend = "gemini"

        def __init__(self, cwd: Path) -> None:
            super().__init__(lambda _p: _found_reply(), calls)
            self.cwd = Path(cwd)

        async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **kw):
            for name in ("file_link.py", "abs_file_link.py", "outside_file_link.txt"):
                (self.cwd / name).write_text("OVERWRITTEN\n", encoding="utf-8")
            (self.cwd / "dir_link" / "new.py").write_text("x\n", encoding="utf-8")
            (self.cwd / "dangling_inside").write_text("x\n", encoding="utf-8")
            return await super().execute_task_to_result(prompt, tools, system_prompt, **kw)

    investigator = RepoInvestigator(
        runtime_backend="gemini", runtime_factory=lambda **k: _LinkWriter(k["cwd"])
    )

    result = await investigator.investigate(GOAL, QUESTION, root)

    assert len(calls) == 1
    assert result is not None and result.isolation == "workspace_copy"
    assert result.found  # checked against the real, unchanged workspace
    assert (root / "pkg" / "real.py").read_text(encoding="utf-8") == "REAL = 1\n"
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside bytes\n"
    assert not (root / "pkg" / "new.py").exists()
    assert not (root / "pkg" / "missing.py").exists()
    assert not (outside / "missing.txt").exists()


@pytest.mark.asyncio
async def test_copy_with_an_escaping_path_is_never_used(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.auto import investigation_workspace

    original = investigation_workspace._copy_entry

    def leaky(root: Path, destination: Path, name: str) -> None:
        original(root, destination, name)
        if not (destination / "leak").exists():
            (destination / "leak").symlink_to(root)

    monkeypatch.setattr(investigation_workspace, "_copy_entry", leaky)
    with pytest.raises(IsolationError):
        create_isolated_copy(workspace)

    calls: list[dict[str, Any]] = []
    investigator = RepoInvestigator(
        runtime_backend="gemini",
        runtime_factory=lambda **k: _GeminiShapedRuntime(
            lambda _p: _found_reply(), calls, k["cwd"]
        ),
    )
    result = await investigator.investigate(GOAL, QUESTION, workspace)

    assert result is not None and result.outcome == "isolation_failed"
    assert calls == []


def _real_runtime(backend: str, cwd: Path) -> Any:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    try:
        return create_agent_runtime(
            backend=backend, model=None, permission_mode=INVESTIGATOR_PERMISSION_MODE, cwd=cwd
        )
    except Exception as exc:  # noqa: BLE001 - a CLI missing on this machine
        pytest.skip(f"{backend} runtime cannot be built here: {exc}")


def test_real_runtime_permission_mapping(tmp_path: Path) -> None:
    """Read off the real runtime classes: Codex runs ``--sandbox read-only``;
    Gemini turns ``default`` into ``auto_edit`` and is refused."""
    codex = _real_runtime("codex", tmp_path)
    assert codex._build_permission_args() == ["--sandbox", "read-only"]
    assert enforces_read_only(codex)
    gemini = _real_runtime("gemini", tmp_path)
    assert gemini._permission_mode != "default"
    assert not enforces_read_only(gemini)


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
    assert built.call_timeout_seconds == DEFAULT_CALL_TIMEOUT_SECONDS == 1800.0
    assert resolve_call_timeout({CALL_TIMEOUT_ENV: "60"}) == 60.0
    with pytest.raises(ValueError):
        resolve_call_timeout({CALL_TIMEOUT_ENV: "0"})


def test_plugin_dispatched_stage_gets_no_investigator() -> None:
    assert build_repo_investigator("opencode", "plugin", {}) is None


def test_investigator_env_is_denied_from_untrusted_project_env() -> None:
    assert is_untrusted_env_denied_key(MAX_CALLS_ENV)
    assert is_untrusted_env_denied_key(CALL_TIMEOUT_ENV)


@pytest.mark.asyncio
async def test_cap_closure_does_nothing_when_the_ledger_cannot_be_completed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsafe gap (the safe-default policy cannot complete the ledger)
    keeps the existing rules: no cap closure, the interview goes on."""
    from ouroboros.auto import safe_defaults

    monkeypatch.setattr(safe_defaults, "finalize_safe_defaultable_gaps", lambda *_a, **_k: None)
    investigator, calls, _ = _investigator(lambda _p: _not_found_reply(), max_calls=2)

    state, _ledger, sent, _result, events = await _run_never_confirming(
        workspace, investigator, max_rounds=4
    )

    assert len(calls) == 2
    assert state.interview_closure_mode != "ledger_only"
    assert not [kind for kind, _ in events if kind == "auto.interview.closed_with_open_questions"]
    assert len(sent) >= 4


def _ready_ledger() -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section in ("actors", "inputs", "outputs", "constraints", "non_goals"):
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.stated",
                value=f"The {section} the request states.",
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )
    for section in ("acceptance_criteria", "verification_plan", "failure_modes", "runtime_context"):
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.stated",
                value=f"The {section} the request states.",
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )
    return ledger


@pytest.mark.parametrize("with_requester", [False, True])
@pytest.mark.asyncio
async def test_backend_done_with_only_open_questions_closes_without_requester(
    workspace: Path, with_requester: bool
) -> None:
    """The backend says the interview is complete but its ambiguity stays above
    the gate because the code left a question open. Without a requester the
    open question does not keep the interview going; with one, nothing changes."""
    sent: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(QUESTION, "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        return InterviewTurn(
            "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.5
        )

    investigator, calls, _ = _investigator(lambda _p: _not_found_reply())
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(workspace.parent / "store"),
        max_rounds=3,
        timeout_seconds=600,
        requester=_CountingRequester(None) if with_requester else None,
        investigator=investigator,
    )
    state = AutoPipelineState(goal=GOAL, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")
    ledger = _ready_ledger()

    result = await driver.run(state, ledger)

    if with_requester:
        assert state.interview_closure_mode != "ledger_only"
    else:
        assert len(calls) == 1
        assert result.status == "seed_ready"
        assert state.interview_closure_mode == "ledger_only"
        assert len(sent) == 1
        assert f"Open question: {QUESTION}" in ledger_assumptions(ledger)
