"""The answers auto chose in its interview reach the worker as labelled assumptions (#3059).

Since #3048 the auto interview answers each product question with a concrete,
goal-specific default the answer refiner writes. The Seed kept only the
question (``Seed.open_questions``), which no execution prompt renders, so the
worker never saw a decided value: a real run decided slider ranges in the
interview and the app shipped different ones.

Now such an answer (``LedgerEntry.refined``, with the interviewer's question,
not declined by the requester) is ``Seed.sourced_scope.assumptions``: verbatim,
in the goal's language, under a worker heading that says nobody confirmed it.
It never binds: it is not a constraint, a criterion or an exit condition, and
an assumption never narrows the goal. A question auto decided nothing for (the
not-settled reply, the answerer's template, a question the requester left
unanswered) stays an open question the worker never sees.

The agent runtime and the refiner's LLM are fakes, so no agent CLI runs.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest
import yaml

from ouroboros.auto.adapters import REFINER_LANGUAGE_RULE, LLMAnswerRefiner
from ouroboros.auto.answer_text import FALLBACK_ANSWERS
from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.investigator import RepoInvestigator, mark_refined
from ouroboros.auto.ledger import (
    NOT_SETTLED_VALUE,
    REQUESTER_KEY_SUFFIX,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import (
    CONFIRMED_SCOPE_CONSTRAINT,
    ledger_assumed_answers,
    ledger_assumptions,
    ledger_open_questions,
    ledger_sourced_scope,
    partial_seed_from_evidence,
    synthesize_seed_from_ledger,
    with_ledger_carryover,
)
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.requester import RequesterQuestion, RequesterReply
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import Seed
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution
from ouroboros.core.types import Result
from ouroboros.mcp.tools.seed_handoff import project_worker_safe_seed
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.orchestrator.runner import build_system_prompt, build_task_prompt
from ouroboros.providers.base import CompletionResponse, UsageInfo

HEADING = "## Assumptions (chosen by auto, not confirmed)"
GOAL = "Build a particle demo web app with sliders for speed and particle count"
KO_GOAL = "속도와 개수를 조절하는 슬라이더가 있는 파티클 데모 웹앱을 만들어 주세요"
SPEED_QUESTION = "What range and default should the speed slider have?"
COUNT_QUESTION = "What range and default should the particle count slider have?"
KO_SPEED_QUESTION = "속도 슬라이더의 범위와 기본값은 무엇이어야 하나요?"
REPO_QUESTION = "Which test runner does the repository use today?"
SPEED = "The speed slider spans 0.1 to 5 with default 1."
COUNT = "The particle count slider spans 100 to 5000 with default 1000."
SPEED_FAILURE = "A speed outside 0.1-5 is clamped to the nearest end."
KO_SPEED = "속도 슬라이더는 0.1부터 5까지, 기본값 1입니다."
KO_SPEED_FAILURE = "속도 값이 0.1–5 밖이면 가장 가까운 끝값으로 맞춘다."
_HANGUL = re.compile(r"[가-힣]")


# -- fakes -----------------------------------------------------------------------


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(root)
    return root


def _reply(kind: str) -> str:
    return json.dumps({"kind": kind, "status": "not_found", "answer": "x", "evidence": []})


class _FakeRuntime:
    """A Codex-shaped read-only runtime. An empty repository settles nothing."""

    _runtime_backend = "codex"

    def __init__(self, reply: Callable[[str], str]) -> None:
        self._reply = reply

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **_kw):  # noqa: ARG002
        return Result.ok(TaskResult(success=True, final_message=self._reply(prompt), messages=()))


def _investigator(repo_questions: tuple[str, ...] = ()) -> RepoInvestigator:
    def reply(prompt: str) -> str:
        if any(question in prompt for question in repo_questions):
            return _reply("current_behavior")
        return _reply("intended_behavior")

    return RepoInvestigator(
        runtime_backend="codex",
        max_calls=12,
        runtime_factory=lambda **_kw: _FakeRuntime(reply),
    )


class _FakeInterviewLLM:
    """The interview LLM behind ``LLMAnswerRefiner``: one concrete value per part.

    Like a model that follows its instructions, it writes Korean only when the
    system prompt names Korean.
    """

    async def complete(self, messages, config):  # noqa: ARG002
        system, user = messages[0].content, messages[1].content
        question = re.search(r"Interview question: (.*)", user).group(1)  # type: ignore[union-attr]
        part = re.search(r"Answer only this part of it: (.*)\.", user).group(1)  # type: ignore[union-attr]
        korean = REFINER_LANGUAGE_RULE.format(language="Korean") in system
        failure = "failure" in part
        if korean:
            text = KO_SPEED_FAILURE if failure else KO_SPEED
        elif "speed" in question:
            text = SPEED_FAILURE if failure else SPEED
        else:
            text = COUNT
        return Result.ok(CompletionResponse(content=text, model="fake", usage=UsageInfo(1, 1, 2)))


class _DecliningRequester:
    async def ask(self, question: RequesterQuestion) -> RequesterReply:  # noqa: ARG002
        return RequesterReply("empty_answer", detail="no answer")


class _AnsweringRequester:
    def __init__(self, text: str) -> None:
        self.text = text

    async def ask(self, question: RequesterQuestion) -> RequesterReply:  # noqa: ARG002
        return RequesterReply("answered", answer=self.text)


async def _interview(
    workspace: Path,
    questions: tuple[str, ...],
    *,
    goal: str = GOAL,
    refiner: Any = "default",
    investigator: RepoInvestigator | None | str = "default",
    requester: Any = None,
) -> SeedDraftLedger:
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
        investigator=_investigator() if investigator == "default" else investigator,  # type: ignore[arg-type]
        answer_refiner=LLMAnswerRefiner(_FakeInterviewLLM()) if refiner == "default" else refiner,
        requester=requester,
    )
    state = AutoPipelineState(goal=goal, cwd=str(workspace))
    state.transition(AutoPhase.INTERVIEW, "interview")
    ledger = SeedDraftLedger.from_goal(goal)
    await driver.run(state, ledger)
    finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="test: close the other gaps")
    return ledger


def _generated_seed(goal: str = GOAL) -> Seed:
    """A Seed shaped like the generator's output; the pipeline adds the ledger carryover."""
    return Seed.from_dict(
        {
            "goal": goal,
            "acceptance_criteria": [
                "`npm test` exits 0 and its output shows both sliders change the animation."
            ],
            "ontology_schema": {"name": "Demo", "description": "d"},
            "metadata": {"ambiguity_score": 0.1},
        }
    )


def _recorded_seed(tmp_path: Path, ledger: SeedDraftLedger, goal: str = GOAL) -> Seed:
    """The Seed the pipeline records: ``_record_generated_seed`` (ledger carryover, normalization)."""
    state = AutoPipelineState(goal=goal, cwd=str(tmp_path))
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]
    return pipeline._record_generated_seed(state, ledger, _generated_seed(goal))


class _Unused:
    progress_callback = None


def _worker_input(seed: Seed) -> str:
    return "\n".join(
        (build_system_prompt(seed, resolved_context_pack_fragment=""), build_task_prompt(seed))
    )


def _assumptions_part(prompt: str) -> str:
    start = prompt.index(HEADING)
    end = prompt.find("\n\n", start)
    return prompt[start:] if end < 0 else prompt[start:end]


def _constraints_part(prompt: str) -> str:
    start = prompt.index("## Constraints")
    return prompt[start : prompt.index("\n## ", start + 1)]


def _entry(key: str, value: str, **kwargs: Any) -> LedgerEntry:
    fields: dict[str, Any] = {
        "source": LedgerSource.CONSERVATIVE_DEFAULT,
        "confidence": 0.8,
        "status": LedgerStatus.DEFAULTED,
    }
    fields.update(kwargs)
    return LedgerEntry(key=key, value=value, **fields)


def _closed(ledger: SeedDraftLedger, goal: str) -> SeedDraftLedger:
    finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="test: close the other gaps")
    return ledger


# -- the observed case: a decided default now reaches the worker -----------------


@pytest.mark.asyncio
async def test_a_refined_default_reaches_the_worker_as_a_labelled_assumption(
    empty_repo: Path, tmp_path: Path
) -> None:
    ledger = await _interview(empty_repo, (SPEED_QUESTION, COUNT_QUESTION))
    seed = _recorded_seed(tmp_path, ledger)

    assert seed.sourced_scope is not None
    assumed = seed.sourced_scope.assumptions
    assert SPEED in assumed
    assert COUNT in assumed
    assert f"Failure modes: {SPEED_FAILURE}" in assumed
    # Decided, so the questions are no longer open.
    assert SPEED_QUESTION not in seed.open_questions
    assert COUNT_QUESTION not in seed.open_questions
    # The record keeps them too, and nothing binds.
    assert set(assumed) <= set(seed.assumptions)
    assert seed.sourced_scope.constraints == ()
    assert seed.sourced_scope.acceptance_criteria == ()

    prompt = _worker_input(seed)
    assert HEADING in prompt
    for line in assumed:
        assert f"- {line}" in _assumptions_part(prompt)
        assert line not in _constraints_part(prompt)
    assert "## Constraints\nNone" in prompt
    # The question itself, with whatever options it named, never reaches the worker.
    assert SPEED_QUESTION not in prompt
    assert COUNT_QUESTION not in prompt


@pytest.mark.asyncio
async def test_the_worker_heading_says_assumptions_never_bind_or_narrow(
    empty_repo: Path, tmp_path: Path
) -> None:
    seed = _recorded_seed(tmp_path, await _interview(empty_repo, (SPEED_QUESTION,)))

    section = _assumptions_part(_worker_input(seed))
    assert "Nobody confirmed these" in section
    assert "They are not requirements and do not\nbind" in section
    assert "An assumption\nnever narrows the goal" in section
    assert "is not a failure" in section


@pytest.mark.asyncio
async def test_the_plugin_worker_seed_carries_the_labelled_assumptions(
    empty_repo: Path, tmp_path: Path
) -> None:
    seed = _recorded_seed(tmp_path, await _interview(empty_repo, (SPEED_QUESTION,)))

    worker = yaml.safe_load(
        project_worker_safe_seed(yaml.safe_dump(seed.to_dict(), sort_keys=False)).seed_content
    )
    assert SPEED in worker["sourced_scope"]["assumptions"]
    assert SPEED not in worker["sourced_scope"]["constraints"]
    assert "open_questions" not in worker


# -- what stays as it was --------------------------------------------------------


def test_user_confirmed_content_stays_unlabelled_and_binding() -> None:
    goal = f"{GOAL}. Constraints are keep the page under 200 KB."
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.add_entry(
        "constraints",
        _entry(
            "constraints.mvp.investigated.1",
            SPEED,
            question=SPEED_QUESTION,
            refined=True,
        ),
    )
    ledger.add_entry(
        "inputs",
        _entry(
            f"inputs.count{REQUESTER_KEY_SUFFIX}",
            "Between 100 and 5000 particles.",
            source=LedgerSource.USER_PREFERENCE,
            status=LedgerStatus.CONFIRMED,
            question=COUNT_QUESTION,
        ),
    )
    seed = with_ledger_carryover(_generated_seed(goal), _closed(ledger, goal))

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.constraints == ("keep the page under 200 KB",)
    assert seed.sourced_scope.assumptions == (SPEED,)
    assert [item.answer for item in seed.requester_answers] == ["Between 100 and 5000 particles."]
    prompt = _worker_input(seed)
    # The confirmed constraint and the requester's answer are not labelled as assumptions.
    assert "- keep the page under 200 KB" in _constraints_part(prompt)
    assert "keep the page under 200 KB" not in _assumptions_part(prompt)
    assert "Between 100 and 5000 particles." not in _assumptions_part(prompt)
    assert "Requester answers (confirmed)" in prompt


@pytest.mark.asyncio
async def test_a_question_the_code_does_not_settle_stays_open(
    empty_repo: Path, tmp_path: Path
) -> None:
    ledger = await _interview(
        empty_repo, (REPO_QUESTION, SPEED_QUESTION), investigator=_investigator((REPO_QUESTION,))
    )
    seed = _recorded_seed(tmp_path, ledger)

    assert REPO_QUESTION in seed.open_questions
    assert SPEED_QUESTION not in seed.open_questions
    assert seed.sourced_scope is not None
    assert not any(NOT_SETTLED_VALUE.rstrip(".") in line for line in seed.sourced_scope.assumptions)
    assert not any(NOT_SETTLED_VALUE.rstrip(".") in line for line in seed.assumptions)
    prompt = _worker_input(seed)
    assert REPO_QUESTION not in prompt
    assert SPEED in prompt


@pytest.mark.asyncio
async def test_a_question_the_requester_left_unanswered_stays_open(
    empty_repo: Path, tmp_path: Path
) -> None:
    ledger = await _interview(empty_repo, (SPEED_QUESTION,), requester=_DecliningRequester())
    seed = _recorded_seed(tmp_path, ledger)

    assert seed.open_questions == (SPEED_QUESTION,)
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == ()
    prompt = _worker_input(seed)
    assert HEADING not in prompt
    assert SPEED not in prompt


@pytest.mark.asyncio
async def test_a_requester_answer_replaces_the_assumed_default(
    empty_repo: Path, tmp_path: Path
) -> None:
    ledger = await _interview(
        empty_repo, (SPEED_QUESTION,), requester=_AnsweringRequester("From 1 to 10, default 2.")
    )
    seed = _recorded_seed(tmp_path, ledger)

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == ()
    assert [item.answer for item in seed.requester_answers] == ["From 1 to 10, default 2."]
    assert SPEED not in _worker_input(seed)


@pytest.mark.asyncio
async def test_the_answerer_template_is_not_presented_as_a_decision(
    empty_repo: Path, tmp_path: Path
) -> None:
    """Without a refiner, auto has only its fixed template, which decides nothing."""
    ledger = await _interview(empty_repo, (SPEED_QUESTION,), refiner=None)
    seed = _recorded_seed(tmp_path, ledger)

    assert seed.open_questions == (SPEED_QUESTION,)
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == ()
    assert HEADING not in _worker_input(seed)


@pytest.mark.asyncio
async def test_the_refiner_fallback_is_not_presented_as_a_decision(
    empty_repo: Path, tmp_path: Path
) -> None:
    """A refiner whose replies never pass their checks falls back to a fixed sentence."""

    async def fallback(*_args: object) -> str:
        return FALLBACK_ANSWERS["en"]

    ledger = await _interview(empty_repo, (SPEED_QUESTION,), refiner=fallback)
    seed = _recorded_seed(tmp_path, ledger)

    assert seed.open_questions == (SPEED_QUESTION,)
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == ()


def test_blocked_and_superseded_entries_are_neither_assumed_nor_open() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    ledger.add_entry(
        "constraints",
        _entry(
            "constraints.speed.investigated.1",
            SPEED,
            question=SPEED_QUESTION,
            refined=True,
            status=LedgerStatus.BLOCKED,
            source=LedgerSource.BLOCKER,
        ),
    )
    ledger.add_entry(
        "constraints",
        _entry(
            "constraints.count.investigated.2",
            COUNT,
            question=COUNT_QUESTION,
            refined=True,
            status=LedgerStatus.WEAK,
        ),
    )

    assert ledger_assumed_answers(ledger) == ()
    assert ledger_open_questions(ledger) == ()


# -- language, labels and the #3036 rule -----------------------------------------


@pytest.mark.asyncio
async def test_a_korean_goal_keeps_its_assumptions_in_korean(
    empty_repo: Path, tmp_path: Path
) -> None:
    ledger = await _interview(empty_repo, (KO_SPEED_QUESTION,), goal=KO_GOAL)
    seed = _recorded_seed(tmp_path, ledger, goal=KO_GOAL)

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == (KO_SPEED, f"Failure modes: {KO_SPEED_FAILURE}")
    for line in seed.sourced_scope.assumptions:
        assert _HANGUL.search(line)
    section = _assumptions_part(_worker_input(seed))
    assert f"- {KO_SPEED}\n" in section
    assert f"- Failure modes: {KO_SPEED_FAILURE}" in section


@pytest.mark.asyncio
async def test_no_pipeline_instruction_reaches_constraints_or_assumptions(
    empty_repo: Path, tmp_path: Path
) -> None:
    seed = _recorded_seed(tmp_path, await _interview(empty_repo, (SPEED_QUESTION,)))

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.constraints == ()
    for text in (*seed.constraints, *seed.sourced_scope.assumptions, *seed.assumptions):
        # The worker heading is rendered, never written into the Seed.
        assert "chosen by auto" not in text
        assert "Nobody confirmed" not in text
    assert CONFIRMED_SCOPE_CONSTRAINT not in seed.sourced_scope.assumptions


# -- #3061: an "extra features are failures" default must not stop polishing -------

KO_GALAXY_GOAL = "아름다운 3D 은하를 만들어 주세요"
SCOPE_QUESTION = "구현 범위는 어디까지로 할까요?"
SHRINKING = "최소 구현으로 제한"
EXTRA_FEATURES_FAIL = "MVP 범위를 넘는 기능 추가"
NO_NEBULAE = "배경 별과 성운은 범위 밖"


def _galaxy_ledger() -> SeedDraftLedger:
    """A refined multi-part answer shaped like the #3061 A/B run's."""
    ledger = SeedDraftLedger.from_goal(KO_GALAXY_GOAL)
    for section, key, value in (
        ("constraints", "constraints.conservative_mvp.investigated.1", SHRINKING),
        ("failure_modes", "failure_modes.scope.investigated.1", EXTRA_FEATURES_FAIL),
        ("non_goals", "non_goals.scope.investigated.1", NO_NEBULAE),
    ):
        ledger.add_entry(section, _entry(key, value, question=SCOPE_QUESTION, refined=True))
    return _closed(ledger, KO_GALAXY_GOAL)


def test_a_scope_shrinking_default_never_binds() -> None:
    ledger = _galaxy_ledger()
    synthesized = synthesize_seed_from_ledger(ledger)
    seed = with_ledger_carryover(_generated_seed(KO_GALAXY_GOAL), ledger)

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == (
        SHRINKING,
        f"Non-goal: {NO_NEBULAE}",
        f"Failure modes: {EXTRA_FEATURES_FAIL}",
    )
    bound = [*seed.sourced_scope.constraints, *seed.sourced_scope.acceptance_criteria]
    bound += [*synthesized.constraints, *(str(item) for item in synthesized.acceptance_criteria)]
    bound += [condition.description for condition in synthesized.exit_conditions]
    bound += [condition.evaluation_criteria for condition in synthesized.exit_conditions]
    for text in (SHRINKING, EXTRA_FEATURES_FAIL, NO_NEBULAE):
        assert not any(text in item for item in bound)

    prompt = _worker_input(seed)
    assert "## Constraints\nNone" in prompt
    section = _assumptions_part(prompt)
    for text in (SHRINKING, EXTRA_FEATURES_FAIL, NO_NEBULAE):
        assert text in section
        assert prompt.index(text) > prompt.index(HEADING)
    assert "doing more than an assumption describes" in section
    assert "is not a failure" in section


def _without_assumed_answers(ledger: SeedDraftLedger) -> SeedDraftLedger:
    """The same ledger routed as before #3059: no entry counts as refined."""
    copy = SeedDraftLedger.from_dict(ledger.to_dict())
    for section in copy.sections.values():
        for entry in section.entries:
            entry.refined = False
    return copy


def test_assumed_answers_leave_the_binding_seed_contract_unchanged() -> None:
    """Only ``sourced_scope.assumptions`` differs; every binding part is the same.

    Moved from the review-stage tests when the Seed review was removed (#3064):
    the grade comparison there stood in for this contract check.
    """
    ledger = _galaxy_ledger()
    before = _without_assumed_answers(ledger)
    pairs = (
        (_generated_seed(KO_GALAXY_GOAL), _generated_seed(KO_GALAXY_GOAL)),
        (synthesize_seed_from_ledger(ledger), synthesize_seed_from_ledger(before)),
    )
    for after_base, before_base in pairs:
        after = with_ledger_carryover(after_base, ledger)
        prior = with_ledger_carryover(before_base, before)
        assert after.sourced_scope is not None and after.sourced_scope.assumptions
        assert prior.sourced_scope is not None and not prior.sourced_scope.assumptions
        assert after.goal == prior.goal
        assert after.constraints == prior.constraints
        assert after.acceptance_criteria == prior.acceptance_criteria
        assert after.sourced_scope.goal == prior.sourced_scope.goal
        assert after.sourced_scope.constraints == prior.sourced_scope.constraints
        assert after.sourced_scope.acceptance_criteria == prior.sourced_scope.acceptance_criteria


# -- the refined mark ------------------------------------------------------------


def _answer(*values: str) -> AutoAnswer:
    sections = ("constraints", "failure_modes", "acceptance_criteria")
    return AutoAnswer(
        " ".join(values),
        AutoAnswerSource.CONSERVATIVE_DEFAULT,
        0.8,
        [
            (section, _entry(f"{section}.k", value))
            for section, value in zip(sections, values, strict=False)
        ],
    )


def test_mark_refined_marks_only_the_values_the_refiner_rewrote() -> None:
    draft = _answer("template one", "template two", "template three")
    refined = _answer("A concrete value", "template two", FALLBACK_ANSWERS["ko"])

    marked = mark_refined(draft, refined)

    # Rewritten: marked. Kept the template, or the refiner's fixed fallback: not marked.
    assert [entry.refined for _, entry in marked.ledger_updates] == [True, False, False]
    assert [entry.value for _, entry in marked.ledger_updates] == [
        "A concrete value",
        "template two",
        FALLBACK_ANSWERS["ko"],
    ]
    # The driver returns its input unchanged when nothing was refined.
    assert mark_refined(draft, draft) is draft
    assert not any(entry.refined for _, entry in draft.ledger_updates)


def test_the_refined_flag_round_trips_and_older_ledgers_load() -> None:
    entry = _entry("constraints.k", SPEED, question=SPEED_QUESTION, refined=True)
    assert entry.to_dict()["refined"] is True
    assert LedgerEntry.from_dict(entry.to_dict()).refined is True

    plain = _entry("constraints.k", SPEED)
    assert "refined" not in plain.to_dict()
    assert LedgerEntry.from_dict(plain.to_dict()).refined is False

    with pytest.raises(ValueError, match="refined must be a boolean"):
        LedgerEntry.from_dict({**entry.to_dict(), "refined": "true"})


# -- the Seed record -------------------------------------------------------------


def test_the_seed_round_trips_the_assumed_answers_and_older_seeds_serialize_as_before() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    ledger.add_entry(
        "constraints",
        _entry("constraints.speed.investigated.1", SPEED, question=SPEED_QUESTION, refined=True),
    )
    seed = with_ledger_carryover(_generated_seed(), _closed(ledger, GOAL))

    data = seed.to_dict()
    assert data["sourced_scope"]["assumptions"] == [SPEED]
    restored = Seed.from_dict(yaml.safe_load(yaml.safe_dump(data, allow_unicode=True)))
    assert restored.sourced_scope == seed.sourced_scope
    assert SeedContract.from_seed(restored).assumptions == (SPEED,)

    # A sourced scope with no assumption serializes exactly as before.
    plain = with_ledger_carryover(_generated_seed(), _without_assumed_answers(ledger))
    assert plain.to_dict()["sourced_scope"] == {
        "goal": GOAL,
        "constraints": [],
        "acceptance_criteria": [],
    }


def test_a_seed_without_sourced_scope_renders_its_assumptions_as_before() -> None:
    seed = _generated_seed().model_copy(update={"assumptions": ("A legacy assumption",)})

    prompt = render_seed_contract_for_execution(SeedContract.from_seed(seed))
    assert "## Assumptions (non-binding)" in prompt
    assert HEADING not in prompt


def test_the_record_lists_one_value_once_under_its_first_label() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section in ("constraints", "failure_modes"):
        ledger.add_entry(
            section,
            _entry(f"{section}.k.investigated.1", SPEED, question=SPEED_QUESTION, refined=True),
        )

    assert ledger_assumed_answers(ledger) == (SPEED,)
    assert ledger_assumptions(ledger) == (SPEED,)


# -- the straight-to-run pipeline (#3064) ----------------------------------------


@pytest.mark.asyncio
async def test_the_written_seed_hands_the_assumed_answers_straight_to_the_run(
    empty_repo: Path, tmp_path: Path
) -> None:
    """Seed generation -> RUN with nothing in between: the run gets the assumptions."""
    ledger = await _interview(empty_repo, (SPEED_QUESTION,))
    started: list[Seed] = []
    phases: list[str] = []

    async def generate_seed(_session_id: str) -> Seed:
        return _generated_seed()

    async def run_seed(seed: Seed, **_: Any) -> dict[str, str]:
        started.append(seed)
        return {"job_id": "job_assumed"}

    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.ledger = ledger.to_dict()
    state.interview_session_id = "interview_assumed"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    pipeline = AutoPipeline(
        _Unused(),  # type: ignore[arg-type]
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path / "store"),
        progress_callback=lambda event: phases.append(event.phase),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert phases == ["seed_generation", "run", "complete"]
    (seed,) = started
    assert seed.sourced_scope is not None
    assert SPEED in seed.sourced_scope.assumptions
    assert SPEED in result.assumed_answers
    assert f"- {SPEED}" in _assumptions_part(_worker_input(seed))


# -- reports ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_auto_result_reports_the_assumed_answers(
    empty_repo: Path, tmp_path: Path
) -> None:
    from ouroboros.cli.commands.auto import _print_result
    from ouroboros.cli.formatters import console
    from ouroboros.mcp.tools.auto_handler import _format_result, _result_meta

    ledger = await _interview(empty_repo, (SPEED_QUESTION,))
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]
    pipeline._record_generated_seed(state, ledger, _generated_seed())
    result = pipeline._result(state, ledger)

    assert SPEED in result.assumed_answers
    assert SPEED_QUESTION not in result.open_questions
    assert _result_meta(result)["assumed_answers"] == list(result.assumed_answers)
    text = _format_result(result)
    with console.capture() as capture:
        _print_result(result, show_ledger=False)
    cli_output = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capture.get()).split())
    for output in (text, cli_output):
        assert "Assumed answers (chosen by auto, not confirmed; given to the worker):" in output
        assert SPEED in output

    empty = AutoPipelineResult(status="complete", auto_session_id="auto_x", phase="complete")
    assert "assumed_answers" not in _result_meta(empty)
    assert "Assumed answers" not in _format_result(empty)


def test_a_result_without_a_sourced_scope_reports_no_assumed_answers(tmp_path: Path) -> None:
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    state.seed_artifact = _generated_seed().to_dict()
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]

    result = pipeline._result(state, SeedDraftLedger.from_goal(GOAL))

    assert result.assumed_answers == ()


def test_ledger_sourced_scope_assumptions_follow_provenance_not_text() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    # The same wording as a refined answer, but nobody asked a question: a
    # templated fill, kept in the record only.
    ledger.add_entry("constraints", _entry("constraints.fill", SPEED, refined=True))
    # Asked and refined: auto's answer.
    ledger.add_entry(
        "outputs",
        _entry("outputs.k.investigated.1", COUNT, question=COUNT_QUESTION, refined=True),
    )
    # Asked, not refined: the answerer's template, so the question stays open.
    ledger.add_entry(
        "inputs", _entry("inputs.k", "Preserve the requested behavior", question=SPEED_QUESTION)
    )

    scope = ledger_sourced_scope(ledger)
    assert scope is not None
    assert scope.assumptions == (f"Outputs: {COUNT}",)
    assert ledger_open_questions(ledger) == (SPEED_QUESTION,)
    # The unasked fill stays in the record, cleaned like every other ledger line.
    assert SPEED.rstrip(".") in ledger_assumptions(ledger)
    assert "Inputs: Preserve the requested behavior" not in ledger_assumptions(ledger)


# -- verbatim: only the section label is added ------------------------------------

_LABELS = {
    "constraints": "",
    "non_goals": "Non-goal: ",
    "acceptance_criteria": "",
    "actors": "Actors: ",
    "inputs": "Inputs: ",
    "outputs": "Outputs: ",
    "verification_plan": "Verification plan: ",
    "failure_modes": "Failure modes: ",
    "runtime_context": "Runtime context: ",
}

VERBATIM_ANSWERS = (
    ("constraints", "Answer ends."),
    ("constraints", "Ratio: 3:1 by default;"),
    ("acceptance_criteria", "- Leading dash stays"),
    ("acceptance_criteria", "*Starred* text stays*"),
    ("failure_modes", "First sentence. Second sentence; third clause."),
    ("verification_plan", ": a leading colon and a trailing colon:"),
    ("non_goals", "배경 별과 성운은 범위 밖입니다."),
    ("outputs", "속도 슬라이더는 0.1부터 5까지; 기본값은 1입니다..."),
    ("inputs", "One value\n- with a bullet line is not split"),
)


def _assumed_ledger(answers: tuple[tuple[str, str], ...]) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for index, (section, value) in enumerate(answers, 1):
        ledger.add_entry(
            section,
            _entry(
                f"{section}.k.investigated.{index}",
                value,
                question=f"Question {index}?",
                refined=True,
            ),
        )
    return _closed(ledger, GOAL)


@pytest.mark.parametrize(("section", "value"), VERBATIM_ANSWERS)
def test_an_assumed_answer_reaches_every_surface_verbatim(
    section: str, value: str, tmp_path: Path
) -> None:
    from ouroboros.mcp.tools.auto_handler import _format_result, _result_meta

    line = f"{_LABELS[section]}{value}"
    ledger = _assumed_ledger(((section, value),))

    assert ledger_assumed_answers(ledger) == (line,)
    seed = _recorded_seed(tmp_path, ledger)
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == (line,)
    assert line in seed.assumptions
    # The Seed file round trip keeps every byte.
    restored = Seed.from_dict(yaml.safe_load(yaml.safe_dump(seed.to_dict(), allow_unicode=True)))
    assert restored.sourced_scope == seed.sourced_scope
    # The worker prompt adds only the bullet.
    assert f"\n- {line}" in _assumptions_part(_worker_input(seed))
    # The auto result, MCP text and meta carry it unchanged.
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]
    pipeline._record_generated_seed(state, ledger, _generated_seed())
    result = pipeline._result(state, ledger)
    assert result.assumed_answers == (line,)
    assert _result_meta(result)["assumed_answers"] == [line]
    assert f"- {line}" in _format_result(result)


def test_the_cli_result_prints_an_assumed_answer_unchanged(tmp_path: Path) -> None:
    from ouroboros.cli.commands.auto import _print_result
    from ouroboros.cli.formatters import console

    value = "[bold]Markup[/bold] stays text: 3:1;"
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_x",
        phase="complete",
        assumed_answers=(value,),
    )
    with console.capture() as capture:
        _print_result(result, show_ledger=False)
    assert f"- {value}" in re.sub(r"\x1b\[[0-9;]*m", "", capture.get())


def test_every_section_label_renders_in_order() -> None:
    answers = tuple((section, f"Answer for {section}.") for section in _LABELS)
    seed = with_ledger_carryover(_generated_seed(), _assumed_ledger(answers))

    expected = tuple(f"{label}Answer for {section}." for section, label in _LABELS.items())
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == expected
    section_text = _assumptions_part(_worker_input(seed))
    rendered = [line[2:] for line in section_text.splitlines() if line.startswith("- ")]
    assert tuple(rendered) == expected


# -- question-level decision: one chosen section decides the question --------------

OTHER_QUESTION = "Should the demo pause when the tab is hidden?"


def _mixed_ledger(*, refined_sections: tuple[str, ...]) -> SeedDraftLedger:
    """One question answered in two sections; ``refined_sections`` were rewritten."""
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section, value in (
        ("constraints", SPEED),
        ("failure_modes", "Failure includes unverified behavior or scope expansion."),
    ):
        ledger.add_entry(
            section,
            _entry(
                f"{section}.k.investigated.1",
                value,
                question=SPEED_QUESTION,
                refined=section in refined_sections,
            ),
        )
    return _closed(ledger, GOAL)


def test_one_refined_section_decides_the_question() -> None:
    ledger = _mixed_ledger(refined_sections=("constraints",))

    assert SPEED_QUESTION not in ledger_open_questions(ledger)
    # Only the chosen answer is an assumption; the template that stayed is neither.
    assert ledger_assumed_answers(ledger) == (SPEED,)
    for seed in (
        synthesize_seed_from_ledger(ledger),
        partial_seed_from_evidence(ledger, reason="test"),
        with_ledger_carryover(_generated_seed(), ledger),
    ):
        assert SPEED_QUESTION not in seed.open_questions


def test_a_question_with_no_refined_section_stays_open() -> None:
    ledger = _mixed_ledger(refined_sections=())

    assert ledger_open_questions(ledger) == (SPEED_QUESTION,)
    assert ledger_assumed_answers(ledger) == ()
    seed = with_ledger_carryover(_generated_seed(), ledger)
    assert seed.open_questions == (SPEED_QUESTION,)
    assert seed.sourced_scope is not None and seed.sourced_scope.assumptions == ()


@pytest.mark.asyncio
async def test_a_failed_refiner_call_for_one_section_does_not_reopen_the_question(
    empty_repo: Path, tmp_path: Path
) -> None:
    async def refiner(goal: str, question: str, section: str, value: str, committed) -> str:  # noqa: ARG001
        if section == "failure_modes":
            raise RuntimeError("provider unavailable")
        return SPEED

    ledger = await _interview(empty_repo, (SPEED_QUESTION,), refiner=refiner)
    entries = {
        section: entry
        for section, ledger_section in ledger.sections.items()
        for entry in ledger_section.entries
        if entry.question == SPEED_QUESTION
    }
    assert entries["constraints"].refined
    assert not entries["failure_modes"].refined  # the template stayed

    seed = _recorded_seed(tmp_path, ledger)
    assert SPEED_QUESTION not in seed.open_questions
    assert seed.sourced_scope is not None
    assert seed.sourced_scope.assumptions == (SPEED,)
    prompt = _worker_input(seed)
    assert SPEED in _assumptions_part(prompt)
    assert entries["failure_modes"].value not in prompt
    assert SPEED_QUESTION not in prompt


def test_carryover_drops_a_decided_question_the_seed_already_lists(tmp_path: Path) -> None:
    """A Seed recorded before the answer was chosen (resume, repair) is reconciled."""
    ledger = _mixed_ledger(refined_sections=("constraints",))
    listed = _generated_seed().model_copy(
        update={"open_questions": (SPEED_QUESTION, OTHER_QUESTION)}
    )

    carried = with_ledger_carryover(listed, ledger)
    assert carried.open_questions == (OTHER_QUESTION,)
    # Recording again changes nothing.
    assert with_ledger_carryover(carried, ledger).open_questions == (OTHER_QUESTION,)

    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]
    recorded = pipeline._record_generated_seed(state, ledger, listed)
    assert recorded.open_questions == (OTHER_QUESTION,)
    result = pipeline._result(state, ledger)
    assert result.open_questions == (OTHER_QUESTION,)
    assert result.assumed_answers == (SPEED,)


def test_carryover_keeps_an_undecided_question_the_seed_already_lists() -> None:
    ledger = _mixed_ledger(refined_sections=())
    listed = _generated_seed().model_copy(update={"open_questions": (OTHER_QUESTION,)})

    assert with_ledger_carryover(listed, ledger).open_questions == (
        OTHER_QUESTION,
        SPEED_QUESTION,
    )
