"""Auto-interview answers stay in the goal's language, short, and plain (#3050).

Real runs after #3048 showed English goals answered partly or wholly in
Russian, answers of 600 to 1,500 characters with escaped line breaks, internal
field names such as "the `constraints` contract", and an interview asking
again and again which of two earlier answers was "authoritative". The refiner
prompt named no language, demanded exact samples and error behaviour for every
part, labelled every part with its field name, and told the model to keep every
earlier string verbatim.

The refiner's LLM and the interviewer are fakes, so no agent CLI or provider
runs.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

from ouroboros.auto.adapters import REFINER_LANGUAGE_RULE, LLMAnswerRefiner
from ouroboros.auto.answer_text import (
    FALLBACK_ANSWERS,
    MAX_ANSWER_PART_CHARS,
    answer_language,
    answer_problems,
    is_code_token,
    latin_prose,
    normalize_answer,
)
from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
from ouroboros.auto.evidence_check import EvidenceSpan
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.investigator import (
    Investigation,
    RepoInvestigator,
    build_prompt,
    found_answer,
    not_found_answer,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.profiles.coding import CODING_PROFILE
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.providers.base import CompletionResponse, MessageRole, UsageInfo

EN_GOAL = "Add a second_largest(values) helper to stats.py"
KO_GOAL = "stats.py에 second_largest(values) 함수를 추가해 주세요"
RU_GOAL = "Добавь функцию second_largest(values) в stats.py"
QUESTION = "What should second_largest return when fewer than two distinct values exist?"
KO_QUESTION = "서로 다른 값이 두 개 미만이면 무엇을 반환해야 하나요?"
EN_REPLY = "Return None when fewer than two distinct values exist."
KO_REPLY = "서로 다른 값이 두 개 미만이면 None을 반환한다."
RU_REPLY = "Вернуть None, если различных значений меньше двух."
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
HANGUL = re.compile(r"[가-힣]")


class _ScriptedLLM:
    """Returns the scripted replies in order (the last one repeats)."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.calls: list[list[Any]] = []

    async def complete(self, messages, config):  # noqa: ARG002
        self.calls.append(list(messages))
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if reply is None:
            return Result.err(ProviderError("provider down"))
        return Result.ok(CompletionResponse(content=reply, model="fake", usage=UsageInfo(1, 1, 2)))


async def _refine(llm: Any, goal: str = EN_GOAL, question: str = QUESTION, **kwargs: Any):
    refiner = LLMAnswerRefiner(llm)
    return await refiner(goal, question, kwargs.pop("section", "constraints"), "generic", **kwargs)


# -- the goal's language, decided without a model ------------------------------------


@pytest.mark.parametrize(
    ("goal", "language"),
    [
        (EN_GOAL, "en"),
        (KO_GOAL, "ko"),
        (RU_GOAL, "ru"),
        ("値を返す関数を追加", "ja"),
        ("添加函数", "zh"),
    ],
)
def test_answer_language_reads_the_goals_script(goal: str, language: str) -> None:
    assert answer_language(goal) == language


@pytest.mark.parametrize(
    ("goal", "name"), [(EN_GOAL, "English"), (KO_GOAL, "Korean"), (RU_GOAL, "Russian")]
)
@pytest.mark.asyncio
async def test_the_refiner_names_the_goals_language(goal: str, name: str) -> None:
    llm = _ScriptedLLM({"English": EN_REPLY, "Korean": KO_REPLY, "Russian": RU_REPLY}[name])

    await _refine(llm, goal=goal)

    system = llm.calls[0][0].content
    assert REFINER_LANGUAGE_RULE.format(language=name) in system
    assert "user's language" not in system


# -- an English goal never shows Cyrillic ----------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        RU_REPLY,  # wholly Russian
        f"{EN_REPLY} {RU_REPLY}",  # an English answer followed by a Russian copy
    ],
)
@pytest.mark.asyncio
async def test_a_russian_reply_to_an_english_goal_is_retried(first: str) -> None:
    llm = _ScriptedLLM(first, EN_REPLY)

    answer = await _refine(llm)

    assert answer == EN_REPLY
    assert len(llm.calls) == 2
    retry = llm.calls[1]
    assert retry[-2].role is MessageRole.ASSISTANT and retry[-2].content == first
    assert "English only" in retry[-1].content


@pytest.mark.asyncio
async def test_a_second_russian_reply_falls_back_to_a_short_english_answer() -> None:
    llm = _ScriptedLLM(RU_REPLY, f"{EN_REPLY} {RU_REPLY}")

    answer = await _refine(llm)

    assert answer == FALLBACK_ANSWERS["en"]
    assert not CYRILLIC.search(answer)
    assert len(llm.calls) == 2  # one retry, never more


@pytest.mark.asyncio
async def test_english_goal_answers_never_contain_cyrillic_through_the_driver(
    tmp_path: Path,
) -> None:
    """The refiner's LLM answers every call in Russian: what the interviewer
    receives is still English, for every part of every answer."""
    llm = _ScriptedLLM(RU_REPLY)
    sent = await _drive(tmp_path, EN_GOAL, (QUESTION, "How should invalid input be handled?"), llm)

    assert sent
    assert not any(CYRILLIC.search(text) for text in sent)
    assert all(FALLBACK_ANSWERS["en"] in text for text in sent)
    # Two parts fell back to the same sentence: the answer says it once.
    assert all(text.count(FALLBACK_ANSWERS["en"]) == 1 for text in sent)


# -- Korean stays Korean ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_korean_goal_keeps_a_korean_reply() -> None:
    llm = _ScriptedLLM(KO_REPLY)

    assert await _refine(llm, goal=KO_GOAL, question=KO_QUESTION) == KO_REPLY
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "first",
    [EN_REPLY, f"{KO_REPLY} Return None when fewer than two distinct values exist.", RU_REPLY],
)
@pytest.mark.asyncio
async def test_korean_goal_rejects_english_or_mixed_replies(first: str) -> None:
    llm = _ScriptedLLM(first, first)

    answer = await _refine(llm, goal=KO_GOAL, question=KO_QUESTION)

    assert answer == FALLBACK_ANSWERS["ko"]
    assert HANGUL.search(answer)


@pytest.mark.asyncio
async def test_korean_reply_may_name_identifiers() -> None:
    reply = "second_largest는 `None`을 반환하고 pytest로 확인한다."
    assert await _refine(_ScriptedLLM(reply), goal=KO_GOAL) == reply


@pytest.mark.asyncio
async def test_provider_failure_keeps_english_draft_and_korean_language() -> None:
    assert await _refine(_ScriptedLLM(None)) is None  # driver keeps its English draft
    assert await _refine(_ScriptedLLM(None), goal=KO_GOAL) == FALLBACK_ANSWERS["ko"]


# -- length and line breaks ---------------------------------------------------------------


def test_escaped_and_real_line_breaks_are_normalised() -> None:
    raw = "Return None.\\nRaise TypeError for strings.\\n\\nDocument it.\nDone."

    assert normalize_answer(raw) == "Return None. Raise TypeError for strings. Document it. Done."


@pytest.mark.asyncio
async def test_a_reply_with_escaped_line_breaks_reaches_nobody_with_them() -> None:
    answer = await _refine(_ScriptedLLM("Return None.\\nDocument it in the docstring."))

    assert answer == "Return None. Document it in the docstring."
    assert "\\n" not in answer and "\n" not in answer


@pytest.mark.asyncio
async def test_a_long_reply_is_retried_then_replaced() -> None:
    long_reply = "Return None " + "and document every edge case in detail " * 10
    assert len(long_reply) > MAX_ANSWER_PART_CHARS

    assert await _refine(_ScriptedLLM(long_reply, EN_REPLY)) == EN_REPLY
    assert await _refine(_ScriptedLLM(long_reply, long_reply)) == FALLBACK_ANSWERS["en"]


def test_fallback_answers_are_short_and_in_their_language() -> None:
    for language, text in FALLBACK_ANSWERS.items():
        assert answer_problems(text, language) == [], language


# -- no internal vocabulary --------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "Per the `constraints` contract, return None.",
        "For `acceptance_criteria`, return None for short input.",
        "Record it in the ledger and return None.",
        "The Seed section says to return None.",
        "Keep the committed contract: return None.",
    ],
)
@pytest.mark.asyncio
async def test_internal_field_names_are_retried_then_replaced(reply: str) -> None:
    assert answer_problems(reply, "en") == ["uses internal field names"]
    assert await _refine(_ScriptedLLM(reply, EN_REPLY)) == EN_REPLY
    assert await _refine(_ScriptedLLM(reply, reply)) == FALLBACK_ANSWERS["en"]


def test_plain_product_words_are_not_internal_terms() -> None:
    plain = "The acceptance criteria are met when the test passes and output is printed."
    assert answer_problems(plain, "en") == []


@pytest.mark.parametrize(
    "section",
    [
        "actors",
        "inputs",
        "outputs",
        "constraints",
        "non_goals",
        "acceptance_criteria",
        "verification_plan",
        "failure_modes",
        "runtime_context",
    ],
)
@pytest.mark.asyncio
async def test_the_prompt_never_shows_a_field_name(section: str) -> None:
    llm = _ScriptedLLM(EN_REPLY)
    committed = [("constraints", "constraints.a.investigated.1", "Use plain ints.")]

    await _refine(llm, section=section, committed=committed)

    user = llm.calls[0][1].content
    assert section not in user
    assert "Seed" not in user and "contract" not in user.lower()
    assert "[constraints]" not in user


# -- earlier decisions are visible, oldest first, latest standing -------------------------


def test_committed_decisions_are_oldest_first() -> None:
    ledger = SeedDraftLedger.from_goal(EN_GOAL)
    for section, key, value in (
        ("failure_modes", "failure_modes.x.investigated.3", "third"),
        ("constraints", "constraints.y.investigated.1", "first"),
        ("constraints", "constraints.z.investigated.2", "second"),
    ):
        ledger.add_entry(
            section,
            LedgerEntry(
                key=key,
                value=value,
                source=LedgerSource.CONSERVATIVE_DEFAULT,
                confidence=0.8,
                status=LedgerStatus.DEFAULTED,
            ),
        )

    assert [value for *_rest, value in ledger.committed_decisions()] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_the_refiner_sees_earlier_answers_and_the_latest_stands_rule() -> None:
    llm = _ScriptedLLM(EN_REPLY)
    committed = [
        ("failure_modes", "f.investigated.1", "Print `error: empty input`."),
        ("failure_modes", "f.investigated.2", "Print `error: empty input`."),
        ("failure_modes", "f.investigated.3", "Print `error: no values`."),
    ]

    await _refine(llm, committed=committed)

    system, user = llm.calls[0][0].content, llm.calls[0][1].content
    block = user.split("oldest first:\n", 1)[1]
    assert block.index("error: empty input") < block.index("error: no values")
    assert block.count("error: empty input") == 1  # repeated decisions are listed once
    assert "the most recent one stands" in system


# -- the contradiction loop, reproduced with a fake model and interviewer -----------------

_ERROR = re.compile(r"`(error: [a-z ]+)`")
_ERROR_WORDS = ("empty input", "no values", "bad input", "missing data", "invalid list")


class _LoopModel:
    """A fake refiner LLM that behaves the way the observed runs did.

    - It states an error message in a part when its instructions ask every part
      for error behaviour (the old prompt) or the part is about failures.
    - It reuses an error message the earlier answers already state. Told to
      keep every earlier string verbatim (the old prompt), it repeats all of
      them; told that the most recent answer stands, it keeps the latest.
    - With no earlier message to reuse it invents a new one.
    """

    def __init__(self) -> None:
        self.invented = 0

    async def complete(self, messages, config):  # noqa: ARG002
        system, user = messages[0].content, messages[1].content
        about_failure = "fail" in user.split("Earlier answers", 1)[0].lower()
        every_part = "error/stderr/exit-code behavior" in system
        latest_stands = "the most recent one stands" in system
        earlier = _ERROR.findall(user.split("Interview question:", 1)[1])
        if not (every_part or about_failure):
            text = "Accept only a list of numbers."
        elif earlier and latest_stands:
            text = f"Print `{earlier[-1]}` and exit 2."
            if len(set(earlier)) > 1:
                text = f"Print `{earlier[-1]}`; it replaces the earlier message."
        elif earlier:
            text = " ".join(f"Print `{message}`." for message in dict.fromkeys(earlier))
        else:
            word = _ERROR_WORDS[self.invented % len(_ERROR_WORDS)]
            self.invented += 1
            text = f"Print `error: {word}` and exit 2."
        return Result.ok(CompletionResponse(content=text, model="fake", usage=UsageInfo(1, 1, 2)))


async def _drive(
    tmp_path: Path,
    goal: str,
    questions: tuple[str, ...],
    llm: Any,
    *,
    interviewer: Callable[[list[str], int], str] | None = None,
    max_rounds: int | None = None,
    investigate: bool = False,
) -> list[str]:
    """Run the real driver with the real refiner over ``llm``; returns what it sent.

    ``investigate`` adds the repository investigator on an empty repository,
    answering every question ``intended_behavior`` like the real prompt does,
    so each answer is keyed by its round as in the observed runs.
    """
    sent: list[str] = []
    asked = [questions[0]]

    def next_question(answers: list[str], n: int) -> str:
        return questions[n % len(questions)]

    ask = interviewer or next_question

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(questions[0], "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        rounds = max_rounds or len(questions)
        if len(sent) >= rounds:
            return InterviewTurn("done", session_id, seed_ready=True, completed=True)
        asked.append(ask(sent, len(sent)))
        return InterviewTurn(asked[-1], session_id)

    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path / "store"),
        max_rounds=max_rounds or len(questions),
        timeout_seconds=600,
        answer_refiner=LLMAnswerRefiner(llm),
        investigator=_intended_behavior_investigator() if investigate else None,
    )
    state = AutoPipelineState(goal=goal, cwd=str(root))
    state.transition(AutoPhase.INTERVIEW, "interview")
    await driver.run(state, SeedDraftLedger.from_goal(goal))
    return [text for text in sent if "[safe-default-synthesis]" not in text]


class _ReadOnlyRuntime:
    """A Codex-shaped read-only runtime replying with a fixed JSON reply."""

    _runtime_backend = "codex"

    def __init__(self, reply: str, calls: list[str]) -> None:
        self.reply = reply
        self.calls = calls

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

    async def execute_task_to_result(self, prompt: str, **_kw: Any):
        self.calls.append(prompt)
        return Result.ok(TaskResult(success=True, final_message=self.reply, messages=()))


def _investigator(kind: str, calls: list[str] | None = None) -> RepoInvestigator:
    reply = json.dumps({"kind": kind, "status": "not_found", "answer": "", "evidence": []})
    seen = calls if calls is not None else []
    return RepoInvestigator(
        runtime_backend="codex",
        max_calls=12,
        runtime_factory=lambda **_kw: _ReadOnlyRuntime(reply, seen),
    )


def _intended_behavior_investigator() -> RepoInvestigator:
    return _investigator("intended_behavior")


LOOP_QUESTIONS = (
    "How should invalid input be handled?",
    "What should happen when the list is empty?",
    "What should happen when the list holds one value?",
)


def _loop_interviewer(answers: list[str], n: int) -> str:
    """Asks which message is authoritative whenever two open answers disagree."""
    messages: list[str] = []
    for text in answers:
        found = _ERROR.findall(text)
        if "replaces" in text and found:
            messages = [found[-1]]
        else:
            messages += [message for message in found if message not in messages]
    if len(messages) > 1:
        return f"Which error message is authoritative: `{messages[0]}` or `{messages[1]}`?"
    return LOOP_QUESTIONS[n % len(LOOP_QUESTIONS)]


@pytest.mark.asyncio
async def test_the_contradiction_loop_does_not_happen(tmp_path: Path) -> None:
    sent = await _drive(
        tmp_path,
        EN_GOAL,
        LOOP_QUESTIONS,
        _LoopModel(),
        interviewer=_loop_interviewer,
        max_rounds=6,
        investigate=True,
    )

    messages = {message for text in sent for message in _ERROR.findall(text)}
    assert len(sent) == 6
    assert len(messages) == 1  # one error message, reused by every later answer
    assert all(text.count("`error:") <= 1 for text in sent)


@pytest.mark.asyncio
async def test_asked_which_earlier_answer_holds_the_latest_one_is_kept(tmp_path: Path) -> None:
    """Seeded with two different messages, the next answer keeps the most
    recent one and says it replaces the other; nobody asks again."""
    asked: list[str] = []

    def interviewer(answers: list[str], n: int) -> str:
        question = _loop_interviewer(answers, n)
        asked.append(question)
        return question

    model = _LoopModel()
    first = "Which error message is authoritative: `error: empty input` or `error: no values`?"
    llm_answers = await _drive(
        tmp_path,
        EN_GOAL,
        (first, *LOOP_QUESTIONS),
        _SeededLoopModel(model, ["error: empty input", "error: no values"]),
        interviewer=interviewer,
        max_rounds=4,
        investigate=True,
    )

    assert "replaces" in llm_answers[0]
    assert _ERROR.findall(llm_answers[0]) == ["error: no values"]
    assert not any("authoritative" in question for question in asked)


class _SeededLoopModel:
    """``_LoopModel`` with two earlier answers already in its prompt."""

    def __init__(self, model: _LoopModel, earlier: list[str]) -> None:
        self.model = model
        self.earlier = earlier

    async def complete(self, messages, config):
        user = messages[1].content
        lines = "\n".join(f"- Print `{message}`." for message in self.earlier)
        if "Earlier answers" not in user:
            head, tail = user.split("\n\nA starting point", 1)
            user = f"{head}\nEarlier answers in this interview, oldest first:\n{lines}\n\nA starting point{tail}"
        seeded = [
            messages[0],
            type(messages[1])(role=messages[1].role, content=user),
            *messages[2:],
        ]
        return await self.model.complete(seeded, config)


# -- the investigator, the non-goal and the coding templates name the language too ----------


def test_investigator_prompt_names_the_goals_language() -> None:
    assert "Write the answer in English." in build_prompt(EN_GOAL, QUESTION, [])
    assert "Write the answer in Korean." in build_prompt(KO_GOAL, KO_QUESTION, [])


def test_a_found_answer_in_another_language_is_not_shown() -> None:
    span = EvidenceSpan("stats.py", 1, 1, "def second_largest(values):")
    found = Investigation("found", answer=RU_REPLY, checked=(span,), cited=1, called=True)
    draft = AutoAnswer(
        "x",
        AutoAnswerSource.CONSERVATIVE_DEFAULT,
        0.8,
        [
            (
                "constraints",
                LedgerEntry(
                    key="constraints.x",
                    value="x",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                ),
            )
        ],
    )

    answer = found_answer(draft, found, QUESTION, 1, language="en")

    assert not CYRILLIC.search(answer.text)
    assert answer.text.startswith("The repository settles this; the cited lines show how.")
    assert "stats.py:1-1: def second_largest(values):" in answer.text
    kept = found_answer(
        draft, Investigation("found", answer=EN_REPLY, checked=(span,)), QUESTION, 1
    )
    assert kept.text.startswith(EN_REPLY)


def test_russian_goal_gets_russian_fixed_sentences() -> None:
    non_goal = AutoAnswerer().answer("What is out of scope?", SeedDraftLedger.from_goal(RU_GOAL))
    assert CYRILLIC.search(non_goal.text) and RU_GOAL in non_goal.text
    draft = AutoAnswerer().answer(QUESTION, SeedDraftLedger.from_goal(RU_GOAL))
    reply = not_found_answer(
        draft, Investigation("not_found", kind="current_behavior"), QUESTION, 1, language="ru"
    )
    assert CYRILLIC.search(reply.text) and "repository" not in reply.text
    question = "How do we verify that the API returns status 200?"
    verify = AutoAnswerer(active_profile=CODING_PROFILE).answer(
        question, SeedDraftLedger.from_goal(RU_GOAL)
    )
    assert verify.text.startswith(f"{RU_GOAL} — ") and CYRILLIC.search(verify.text.split("—")[1])


@pytest.mark.asyncio
async def test_english_goal_with_a_korean_question_answers_in_english(tmp_path: Path) -> None:
    """The goal decides the language; an interviewer that drifts into another
    language does not carry the answers with it."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    calls: list[str] = []
    sent: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("second_largest는 지금 무엇을 반환하나요?", "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        sent.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path / "store"),
        max_rounds=1,
        timeout_seconds=600,
        investigator=_investigator("current_behavior", calls),
    )
    state = AutoPipelineState(goal=EN_GOAL, cwd=str(root))
    state.transition(AutoPhase.INTERVIEW, "interview")
    await driver.run(state, SeedDraftLedger.from_goal(EN_GOAL))

    assert calls and "Write the answer in English." in calls[0]
    assert sent[0] == "[from-auto][assumption] The repository code does not settle this question."


# -- Latin prose in a non-Latin answer is a second language --------------------------------

MIXED = "contains English words outside inline code"


@pytest.mark.parametrize(
    ("language", "reply"),
    [
        ("ko", "한국어 Return None"),
        ("zh", "中文 Return None"),
        ("ru", "Русский Return None"),
        ("ja", "日本語の Return None"),
        ("ko", "서로 다른 값이 두 개 미만이면 return None when empty."),
        ("ko", "None을 반환한다. Return None otherwise."),
        ("zh", "返回 None。 Otherwise raise ValueError."),
        ("ru", "Вернуть None. Then exit."),
        ("ja", "None を返す。 Return None otherwise."),
        # A word next to a code token is prose too; commands go in inline code.
        ("ko", "pytest -q로 실행합니다."),
        ("ko", "CI는 GitHub Actions에서 실행합니다."),
    ],
)
def test_latin_prose_in_a_non_latin_answer_is_mixed(language: str, reply: str) -> None:
    assert MIXED in answer_problems(reply, language)


@pytest.mark.parametrize(
    ("language", "reply"),
    [
        ("ko", "pytest로 테스트를 실행합니다."),
        ("ko", "`pathlib`를 씁니다."),
        ("ko", "`pytest -q`로 실행합니다."),
        ("ko", "서로 다른 값이 두 개 미만이면 None을 반환하고 ValueError는 내지 않습니다."),
        ("ko", "mode.py와 config.toml을 읽고 CSV 파일을 UTF-8로 저장합니다."),
        ("ko", "second_largest([1, 3, 5])는 3을 반환하고 Python 3.12에서 실행합니다."),
        ("ko", "HTTP 200과 JSON 응답을 확인합니다."),
        ("ko", "src/stats.py config.toml 두 파일만 바꿉니다."),
        ("zh", "当少于两个不同的值时返回 None，并用 pytest 测试。"),
        ("zh", "读取 data.csv，失败时抛出 ValueError。"),
        ("ru", "Вернуть None, если меньше двух значений; проверить через pytest."),
        ("ru", "Читать mode.py и при ошибке выбрасывать ValueError."),
        ("ja", "異なる値が二つ未満なら None を返し、pytest で確認する。"),
        ("ja", "CSV を読み、失敗したら ValueError を投げる。"),
    ],
)
def test_non_latin_answers_may_name_code(language: str, reply: str) -> None:
    assert answer_problems(reply, language) == []


@pytest.mark.parametrize(
    "token",
    ["None", "True", "false", "mode.py", "src/cli.py", "second_largest", "C++", "UTF-8", "v2"]
    + ["ValueError", "GitHub", "CSV", "JSON", "HTTP"],
)
def test_code_tokens(token: str) -> None:
    assert is_code_token(token)


@pytest.mark.parametrize("token", ["pytest", "Return", "Python", "framework", "Actions", "README"])
def test_word_tokens(token: str) -> None:
    """``README`` has six capitals: past the five-letter acronym bound, it is a word."""
    assert not is_code_token(token)


def test_a_lone_word_or_a_row_of_code_is_not_prose() -> None:
    assert not latin_prose("pytest로 실행")
    assert not latin_prose("mode.py config.toml None")
    assert not latin_prose("Python 3.12에서")
    assert latin_prose("Return None")
    assert latin_prose("hello, world")


@pytest.mark.parametrize(
    "reply",
    ["second_largest는 `None`을 반환한다.", "None을 반환한다", "`None` 반환"],
)
def test_english_goal_rejects_a_korean_answer_with_code_tokens(reply: str) -> None:
    assert "contains another language (hangul script)" in answer_problems(reply, "en")


@pytest.mark.asyncio
async def test_a_mixed_korean_reply_is_retried_then_replaced() -> None:
    assert await _refine(_ScriptedLLM("한국어 Return None", KO_REPLY), goal=KO_GOAL) == KO_REPLY
    replaced = await _refine(_ScriptedLLM("한국어 Return None"), goal=KO_GOAL)
    assert replaced == FALLBACK_ANSWERS["ko"]
