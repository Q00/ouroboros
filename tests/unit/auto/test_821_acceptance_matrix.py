"""Regression coverage for #821 short-goal auto interview convergence."""

from __future__ import annotations

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)

_LEGACY_MAX_ROUNDS_BLOCKER = "auto interview reached max rounds with unresolved gaps"
_BARE_UNRESOLVED_GAPS = "unresolved gaps:"

_ACCEPTANCE_MATRIX_GOALS = (
    "build me a CLI tool that tracks daily habits with streak counting",
    "make me a markdown todo CLI with add/list/done commands",
    "一個本地 markdown 待辦事項 CLI",
    "로컬 markdown todo CLI를 만들어줘",
    """
    로컬에서 실행되는 간단한 카트 레이싱 게임을 만들어줘.
    범위: 키보드로 좌우 이동, 장애물 피하기, 점수 증가.
    비범위: 온라인 멀티플레이, 결제, 서버 배포.
    성공 기준: 로컬에서 실행 가능하고, 충돌 시 게임오버가 표시된다.
    검증: 로컬 명령으로 테스트 또는 실행 확인이 가능해야 한다.
    """.strip(),
    "a static html page with a counter that survives refresh via localStorage",
)


def _matrix_seed(goal: str) -> Seed:
    return Seed(
        goal=goal,
        constraints=("Keep execution local and use the repository's existing conventions.",),
        acceptance_criteria=(
            "A local command or smoke check exits 0 and demonstrates the requested behavior.",
        ),
        ontology_schema=OntologySchema(
            name="Auto821AcceptanceTask",
            description="Local product/task generated from a short auto goal.",
            fields=(
                OntologyField(
                    name="requested_behavior",
                    field_type="string",
                    description="User-visible behavior requested by the goal.",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="local_verifiability", description="Behavior is locally checkable."
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Local verification completed.",
                evaluation_criteria="All acceptance criteria have observable local evidence.",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.10),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", _ACCEPTANCE_MATRIX_GOALS)
async def test_821_acceptance_matrix_short_goals_do_not_stop_at_interview(
    tmp_path, goal: str
) -> None:
    """The #821 six-goal matrix must advance beyond INTERVIEW without legacy gap blockers.

    The backend is deterministic and local: it keeps asking broad follow-ups until
    the driver has had enough turns to fill the Seed draft ledger, then reports
    completion.  The run starter is also mocked so this covers the auto pipeline
    through a downstream RUN handoff without invoking an LLM or external API.
    """

    answers: list[str] = []

    async def start(start_goal: str, cwd: str, *, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", interview_id or "interview_821")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        completed = len(answers) >= 5
        return InterviewTurn(
            "What else should we know?",
            session_id,
            seed_ready=completed,
            completed=completed,
        )

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _matrix_seed(goal)

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str]:  # noqa: ARG001
        return {
            "job_id": "job_821",
            "execution_id": "exec_821",
            "session_id": "run_821",
        }

    state = AutoPipelineState(goal=goal, cwd=str(tmp_path))
    store = AutoStore(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=store,
        max_rounds=12,
        timeout_seconds=1,
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=store)

    result = await pipeline.run(state)
    persisted_ledger = SeedDraftLedger.from_dict(state.ledger)
    terminal_text = "\n".join(
        item
        for item in (
            result.blocker,
            state.last_error,
            result.last_progress_message,
        )
        if item
    )

    assert state.phase is not AutoPhase.INTERVIEW
    assert result.phase != AutoPhase.INTERVIEW.value
    assert result.status in {
        AutoPhase.COMPLETE.value,
        AutoPhase.BLOCKED.value,
        AutoPhase.FAILED.value,
    }
    assert state.last_tool_name != "interview_driver"
    if result.status == AutoPhase.COMPLETE.value:
        assert state.job_id == "job_821"
        assert state.execution_id == "exec_821"
        assert state.run_session_id == "run_821"
    else:
        assert state.last_tool_name in {"grade_gate", "run_starter", "ralph_starter", "evaluator"}
    assert _LEGACY_MAX_ROUNDS_BLOCKER not in terminal_text
    assert _BARE_UNRESOLVED_GAPS not in terminal_text
    assert persisted_ledger.open_gaps() == []
    assert any(
        entry.status == LedgerStatus.DEFAULTED
        for section in persisted_ledger.sections.values()
        for entry in section.entries
    )
