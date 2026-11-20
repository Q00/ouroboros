"""Which answer a question gets after the repository investigator ran (#3034).

The investigator (``ouroboros.auto.investigator``) reads the repository for
every question the deterministic answerer would fill with its own guess. Its
reply declares the question's kind. Only a ``current_behavior`` question can
be settled by the code, so only such a question keeps the investigator's
verdict: a checked repository fact, or a short reply that the code does not
settle it. A product decision (``intended_behavior``), or an investigation
that reached no verdict, goes back to the answerer's conservative-default path:
the deterministic draft, refined by the answer refiner into a concrete,
goal-specific default written in the user's language, stated as a non-binding
assumption.

The observed case: real Auto runs on an empty repository answered 12 of 12
requirement questions (success criteria, error handling, scope) with one fixed
English sentence deferring to the implementer.

The non-goal default and the coding profile's verification templates keep the
user's own text first, with no fixed English prefix.

The agent runtime and the refiner's LLM are fakes, so no agent CLI runs.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.auto.adapters import (
    REFINER_BROWNFIELD_RULE,
    REFINER_GREENFIELD_RULE,
    REFINER_LANGUAGE_RULE,
    LLMAnswerRefiner,
)
from ouroboros.auto.answerer import AutoAnswerer, answer_language
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.investigator import NOT_SETTLED_REPLY, RepoInvestigator
from ouroboros.auto.ledger import (
    NOT_SETTLED_VALUE,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import (
    ledger_open_questions,
    ledger_sourced_scope,
    synthesize_seed_from_ledger,
)
from ouroboros.auto.profiles.coding import CODING_PROFILE
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.providers.base import CompletionResponse, UsageInfo

GOAL = "Add a second_largest(values) helper to stats.py"
KO_GOAL = "stats.py에 second_largest(values) 함수를 추가해 주세요"
REQUIREMENT_QUESTIONS = (
    "What should second_largest return when fewer than two distinct values exist?",
    "What are the success criteria for this change?",
    "How should invalid input be handled?",
)
KO_QUESTIONS = (
    "서로 다른 값이 두 개 미만이면 second_largest는 무엇을 반환해야 하나요?",
    "이 변경의 성공 기준은 무엇인가요?",
)
REPO_QUESTION = "Which test runner does the repository use today?"
KO_REPO_QUESTION = "저장소는 지금 어떤 테스트 러너를 사용하나요?"
# What a model might claim about the code; it must never reach the transcript.
GUESS = "FABRICATED: the repository already uses pytest-xdist"
OLD_IMPLEMENTER_DEFERRAL = "open question for the implementer"
_HANGUL = re.compile(r"[\uac00-\ud7a3]")


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(root)
    return root


def _reply(kind: str, status: str = "not_found", answer: str = GUESS, evidence=()) -> str:
    return json.dumps(
        {"kind": kind, "status": status, "answer": answer, "evidence": list(evidence)}
    )


def _by_question(repo_questions: tuple[str, ...]) -> Callable[[str], str]:
    """An investigator that, like the real prompt, calls "should" questions
    ``intended_behavior``. An empty repository settles nothing."""

    def reply(prompt: str) -> str:
        if any(question in prompt for question in repo_questions):
            return _reply("current_behavior")
        return _reply("intended_behavior")

    return reply


class _FakeRuntime:
    """A Codex-shaped runtime: its exec flags are the read-only sandbox."""

    _runtime_backend = "codex"

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

    def __init__(self, reply: Callable[[str], str], calls: list[str]) -> None:
        self._reply = reply
        self._calls = calls

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **_kw):  # noqa: ARG002
        self._calls.append(prompt)
        return Result.ok(TaskResult(success=True, final_message=self._reply(prompt), messages=()))


def _investigator(reply: Callable[[str], str]) -> tuple[RepoInvestigator, list[str]]:
    calls: list[str] = []
    investigator = RepoInvestigator(
        runtime_backend="codex",
        max_calls=12,
        runtime_factory=lambda **_kw: _FakeRuntime(reply, calls),
    )
    return investigator, calls


class _FakeInterviewLLM:
    """Deterministic stand-in for the interview LLM behind ``LLMAnswerRefiner``.

    It writes one concrete default per call. Like a model that follows its
    instructions, it writes Korean only when the system prompt names Korean.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def complete(self, messages, config):  # noqa: ARG002
        system, user = messages[0].content, messages[1].content
        question = re.search(r"Interview question: (.*)", user).group(1)  # type: ignore[union-attr]
        section = re.search(r"Answer only this part of it: (.*)\.", user).group(1)  # type: ignore[union-attr]
        self.calls.append((system, question, section))
        n = len(self.calls)
        if REFINER_LANGUAGE_RULE.format(language="Korean") in system:
            text = f"[{n}] 서로 다른 값이 두 개 미만이면 None을 반환하고 docstring에 적는다."
        else:
            text = f"[{n}] Return None when fewer than two distinct values exist."
        return Result.ok(CompletionResponse(content=text, model="fake", usage=UsageInfo(1, 1, 2)))


class _Requester:
    def __init__(self) -> None:
        self.questions: list[RequesterQuestion] = []

    async def ask(self, question: RequesterQuestion) -> RequesterReply:
        self.questions.append(question)
        return RequesterReply("empty_answer", detail="no answer")


async def _run(
    workspace: Path,
    questions: tuple[str, ...],
    *,
    goal: str = GOAL,
    investigator: RepoInvestigator | None = None,
    refiner: Any = None,
    requester: Any = None,
) -> tuple[SeedDraftLedger, list[str]]:
    """Run the real driver; the interviewer asks ``questions`` once each."""
    sent: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(questions[0], "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        if len(sent) >= len(questions):
            return InterviewTurn("done", session_id, seed_ready=True, completed=True)
        return InterviewTurn(questions[len(sent)], session_id)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(workspace.parent / "store"),
        max_rounds=len(questions),
        timeout_seconds=600,
        investigator=investigator,
        answer_refiner=refiner,
        requester=requester,
    )
    state = AutoPipelineState(goal=goal, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")
    ledger = SeedDraftLedger.from_goal(goal)
    await driver.run(state, ledger)
    return ledger, [text for text in sent if "[safe-default-synthesis]" not in text]


def _investigated(ledger: SeedDraftLedger):
    return [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if ".investigated." in entry.key
    ]


# -- routing: product decisions get the conservative-default path --------------


@pytest.mark.asyncio
async def test_requirement_questions_on_an_empty_repo_get_conservative_defaults(
    empty_repo: Path,
) -> None:
    investigator, calls = _investigator(_by_question(()))
    llm = _FakeInterviewLLM()

    ledger, sent = await _run(
        empty_repo,
        REQUIREMENT_QUESTIONS,
        investigator=investigator,
        refiner=LLMAnswerRefiner(llm),
    )

    assert len(calls) == len(REQUIREMENT_QUESTIONS)  # every question was investigated
    assert len(sent) == len(REQUIREMENT_QUESTIONS)
    for text in sent:
        assert text.startswith("[from-auto][conservative_default] [")
        assert NOT_SETTLED_REPLY not in text
        assert OLD_IMPLEMENTER_DEFERRAL not in text
        assert "Return None when fewer than two distinct values exist" in text
        assert GUESS not in text  # the investigator's own text is dropped
    assert len(set(sent)) == len(sent)  # one answer per question, not one canned reply
    # Each question's default is refined on its own, with its own question.
    assert {question for _system, question, _section in llm.calls} == set(REQUIREMENT_QUESTIONS)
    entries = _investigated(ledger)
    assert entries
    assert all(entry.source is LedgerSource.CONSERVATIVE_DEFAULT for entry in entries)
    assert all(entry.status is LedgerStatus.DEFAULTED for entry in entries)
    assert all(entry.question in REQUIREMENT_QUESTIONS for entry in entries)
    assert all(not entry.binds_seed for entry in entries)
    assert ledger.count_active_conflicting_entries() == 0


@pytest.mark.asyncio
async def test_defaults_stay_assumptions_and_the_spec_stays_user_owned(
    empty_repo: Path,
) -> None:
    investigator, _calls = _investigator(_by_question(()))

    ledger, _sent = await _run(
        empty_repo,
        REQUIREMENT_QUESTIONS,
        investigator=investigator,
        refiner=LLMAnswerRefiner(_FakeInterviewLLM()),
    )
    finalize_safe_defaultable_gaps(ledger, goal=GOAL, provenance="test: close the other gaps")
    seed = synthesize_seed_from_ledger(ledger)

    bound = [*seed.constraints, *(str(item) for item in seed.acceptance_criteria)]
    bound += [field.description for field in seed.ontology_schema.fields]
    assert not any("Return None when fewer than two" in text for text in bound)
    # The refined defaults are auto's answers (#3059): non-binding assumptions
    # the worker sees, so their questions are no longer open.
    scope = ledger_sourced_scope(ledger)
    assert scope is not None
    assert scope.constraints == ()
    assert scope.acceptance_criteria == ()
    assert any("Return None when fewer than two" in line for line in scope.assumptions)
    assert not set(REQUIREMENT_QUESTIONS) & set(ledger_open_questions(ledger))


@pytest.mark.asyncio
async def test_without_a_refiner_the_deterministic_default_answers(empty_repo: Path) -> None:
    investigator, _calls = _investigator(_by_question(()))

    _ledger, sent = await _run(empty_repo, REQUIREMENT_QUESTIONS[:1], investigator=investigator)
    _ledger_off, sent_off = await _run(empty_repo, REQUIREMENT_QUESTIONS[:1])

    assert sent[0].startswith("[from-auto][conservative_default] ")
    assert NOT_SETTLED_REPLY not in sent[0]
    assert sent == sent_off  # the same draft the answerer gives without an investigator


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        ("not json at all", "reply_invalid"),
        (_reply("intended_behavior", status="found"), "intended_behavior"),
        (json.dumps({"status": "not_found", "answer": GUESS, "evidence": []}), "not_found"),
    ],
)
@pytest.mark.asyncio
async def test_no_current_behavior_verdict_falls_back_to_the_default(
    empty_repo: Path, reply: str, outcome: str
) -> None:
    """A product decision, or no verdict at all, never gets "the code does not
    settle this": that would claim a reading that settled nothing."""
    investigator, _calls = _investigator(lambda _prompt: reply)
    refiner = AsyncMock(return_value="Reject non-numeric values with TypeError.")

    _ledger, sent = await _run(
        empty_repo, REQUIREMENT_QUESTIONS[2:], investigator=investigator, refiner=refiner
    )

    assert refiner.await_count >= 1
    assert sent[0].startswith(
        "[from-auto][conservative_default] Reject non-numeric values with TypeError."
    )
    assert NOT_SETTLED_REPLY not in sent[0]
    assert GUESS not in sent[0]
    del outcome  # the label documents the case


@pytest.mark.asyncio
async def test_requester_gets_the_concrete_default_as_its_draft(empty_repo: Path) -> None:
    investigator, _calls = _investigator(_by_question(()))
    requester = _Requester()
    refiner = AsyncMock(return_value="Return None for fewer than two distinct values.")

    await _run(
        empty_repo,
        REQUIREMENT_QUESTIONS[:1],
        investigator=investigator,
        refiner=refiner,
        requester=requester,
    )

    assert len(requester.questions) == 1
    asked = requester.questions[0]
    assert asked.draft_source == "conservative_default"
    assert asked.draft_answer.startswith("Return None for fewer than two distinct values.")
    assert NOT_SETTLED_REPLY not in asked.draft_answer


# -- repository facts still come from the investigator -------------------------


@pytest.mark.asyncio
async def test_repo_fact_questions_still_use_the_investigator(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(root)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\nminversion = "8"\n')
    found = json.dumps(
        {
            "kind": "current_behavior",
            "status": "found",
            "answer": "The repository configures pytest in pyproject.toml.",
            "evidence": [
                {
                    "path": "pyproject.toml",
                    "start_line": 1,
                    "end_line": 1,
                    "quote": "[tool.pytest.ini_options]",
                }
            ],
        }
    )
    investigator, calls = _investigator(lambda _prompt: found)
    refiner = AsyncMock(return_value=GUESS)

    _ledger, sent = await _run(root, (REPO_QUESTION,), investigator=investigator, refiner=refiner)

    assert len(calls) == 1
    refiner.assert_not_awaited()
    assert sent[0].startswith(
        "[from-auto][repo_fact] The repository configures pytest in pyproject.toml."
    )
    assert "pyproject.toml:1-1: [tool.pytest.ini_options]" in sent[0]


@pytest.mark.asyncio
async def test_unsettled_repo_fact_question_says_so_and_invents_nothing(
    empty_repo: Path,
) -> None:
    investigator, calls = _investigator(_by_question((REPO_QUESTION,)))
    refiner = AsyncMock(return_value=GUESS)

    ledger, sent = await _run(
        empty_repo, (REPO_QUESTION,), investigator=investigator, refiner=refiner
    )

    assert len(calls) == 1
    refiner.assert_not_awaited()  # no model writes about code nobody read
    assert sent[0] == f"[from-auto][assumption] {NOT_SETTLED_REPLY}"
    assert GUESS not in sent[0]
    assert OLD_IMPLEMENTER_DEFERRAL not in sent[0]
    assert not any(GUESS in entry.value for entry in _investigated(ledger))
    assert ledger_open_questions(ledger) == (REPO_QUESTION,)


@pytest.mark.asyncio
async def test_a_not_settled_question_is_not_a_committed_contract_for_the_refiner(
    empty_repo: Path,
) -> None:
    """The refiner keeps committed contracts verbatim; "the code does not settle
    this" decides nothing, so it is never offered to it as one."""
    investigator, _calls = _investigator(_by_question((REPO_QUESTION,)))
    seen: list[tuple[str, ...]] = []

    async def refiner(goal, question, section, generic, committed=()):  # noqa: ARG001
        seen.append(tuple(value for _section, _key, value in committed))
        return f"Concrete default for {section}."

    ledger, sent = await _run(
        empty_repo,
        (REPO_QUESTION, REQUIREMENT_QUESTIONS[0]),
        investigator=investigator,
        refiner=refiner,
    )

    assert any(entry.value == NOT_SETTLED_VALUE for entry in _investigated(ledger))
    assert seen
    assert not any(NOT_SETTLED_VALUE in values for values in seen)
    assert sent[1].startswith("[from-auto][conservative_default] Concrete default for ")


def test_committed_decisions_skip_the_not_settled_value() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for key, value in (
        ("constraints.a.investigated.1", NOT_SETTLED_VALUE),
        ("constraints.b", "Use ints"),
    ):
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key=key,
                value=value,
                source=LedgerSource.ASSUMPTION,
                confidence=0.5,
                status=LedgerStatus.DEFAULTED,
            ),
        )

    assert ledger.committed_decisions() == [("constraints", "constraints.b", "Use ints")]


# -- the user's language --------------------------------------------------------


@pytest.mark.asyncio
async def test_korean_goal_gets_korean_answers(empty_repo: Path) -> None:
    investigator, _calls = _investigator(_by_question((KO_REPO_QUESTION,)))

    _ledger, sent = await _run(
        empty_repo,
        (*KO_QUESTIONS, KO_REPO_QUESTION),
        goal=KO_GOAL,
        investigator=investigator,
        refiner=LLMAnswerRefiner(_FakeInterviewLLM()),
    )

    assert len(sent) == 3
    for text in sent:
        body = text.split("] ", 2)[-1]
        assert _HANGUL.search(body), text
        assert NOT_SETTLED_REPLY not in text
        assert "Return None" not in text
    assert sent[2].startswith("[from-auto][assumption] ")


def test_korean_non_goal_answer_is_korean_and_names_the_goal() -> None:
    answer = AutoAnswerer().answer(
        "이번 작업에서 범위 제외 항목은 무엇인가요?", SeedDraftLedger.from_goal(KO_GOAL)
    )

    assert answer.source.value == "non_goal"
    assert _HANGUL.search(answer.text)
    assert KO_GOAL in answer.text


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("서로 다른 값이 두 개 미만이면?", "ko"),
        ("値が二つ未満のときは何を返しますか?", "ja"),
        ("少于两个不同的值时应该返回什么?", "zh"),
        ("What should it return?", "en"),
        ("¿Qué debe devolver?", "en"),
    ],
)
def test_answer_language_reads_the_script(text: str, language: str) -> None:
    assert answer_language(text) == language


def test_refiner_prompt_asks_for_the_users_language_and_no_repo_facts() -> None:
    assert REFINER_LANGUAGE_RULE in LLMAnswerRefiner._SYSTEM
    assert "{language}" in REFINER_LANGUAGE_RULE
    for has_existing_code, rule in (
        (True, REFINER_BROWNFIELD_RULE),
        (False, REFINER_GREENFIELD_RULE),
    ):
        refiner = LLMAnswerRefiner(AsyncMock(), has_existing_code=has_existing_code)
        system = refiner.system_prompt("en")
        assert "Write the answer in English only" in system
        assert system.endswith(rule)
        assert "never state what the existing code does or contains" in rule


# -- the non-goal template ---------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [GOAL, "Deploy this service to production", "Add authentication to the app"],
)
def test_non_goal_answer_is_bounded_by_the_goal_not_a_fixed_list(goal: str) -> None:
    answer = AutoAnswerer().answer("What are the non-goals?", SeedDraftLedger.from_goal(goal))

    assert goal in answer.text
    for unrelated in ("cloud sync", "paid services", "production deployment"):
        assert unrelated not in answer.text.lower()
    assert answer.ledger_updates[0][1].status is LedgerStatus.DEFAULTED
    assert not answer.ledger_updates[0][1].binds_seed


# -- the coding profile's verification templates ----------------------------------

_OLD_PROFILE_PREFIXES = (
    "Command exits 0 on success and non-zero on failure:",
    "Run test suite and all tests pass:",
    "Linter reports zero errors:",
    "Type checker reports zero errors:",
    "Mention command output, file/artifact, API response, or test result:",
)


@pytest.mark.parametrize(
    ("goal", "language_check"),
    [
        (GOAL, lambda text: "checked by its command output" in text),
        (KO_GOAL, lambda text: _HANGUL.search(text.split(" — ", 1)[1]) is not None),
    ],
)
def test_coding_profile_verification_answer_keeps_the_goal_first(
    goal: str, language_check: Callable[[str], bool]
) -> None:
    """With the coding profile active, a verification question the profile
    recognizes is answered with the user's goal first, plus how it is checked
    in the goal's language: no English prefix, and the question is not echoed."""
    question = "How do we verify that the API returns status 200?"
    answer = AutoAnswerer(active_profile=CODING_PROFILE).answer(
        question, SeedDraftLedger.from_goal(goal)
    )

    assert answer.text.startswith(f"{goal} — ")
    assert language_check(answer.text)
    assert question not in answer.text
    assert not any(prefix in answer.text for prefix in _OLD_PROFILE_PREFIXES)
    assert all(entry.value == answer.text for _section, entry in answer.ledger_updates)


@pytest.mark.parametrize("predicate", CODING_PROFILE.verifiable_predicates)
@pytest.mark.parametrize(
    ("criterion", "script"),
    [
        ("second_largest returns 3 for [1, 3, 5]", re.compile(r"[a-z]")),
        ("second_largest([1, 3, 5])는 3을 반환한다", _HANGUL),
        ("second_largest([1, 3, 5]) は 3 を返す", re.compile(r"[぀-ヿ]")),
        ("second_largest([1, 3, 5]) 返回 3", re.compile(r"[一-鿿]")),
    ],
)
def test_coding_repair_templates_keep_the_criterion_first_in_its_language(
    predicate: Any, criterion: str, script: re.Pattern[str]
) -> None:
    repaired = predicate.repair_template(criterion)

    text, clause = repaired.split(" — ", 1)
    assert text == criterion
    assert script.search(clause)
    assert not any(prefix in repaired for prefix in _OLD_PROFILE_PREFIXES)
