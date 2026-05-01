"""Bounded auto Socratic interview driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.gap_detector import GapDetector
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore


@dataclass(frozen=True, slots=True)
class InterviewTurn:
    """Question returned by an interview backend."""

    question: str
    session_id: str
    seed_ready: bool = False
    completed: bool = False


class InterviewBackend(Protocol):
    """Minimal backend interface needed by the auto interview driver."""

    async def start(self, goal: str, *, cwd: str) -> InterviewTurn:
        """Start an interview and return the first question."""

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        """Record an answer and return the next question or completion metadata."""


@dataclass(frozen=True, slots=True)
class AutoInterviewResult:
    """Result from running the bounded auto interview loop."""

    status: str
    session_id: str | None
    ledger: SeedDraftLedger
    rounds: int
    blocker: str | None = None


@dataclass(slots=True)
class AutoInterviewDriver:
    """Drive an interview backend with conservative auto answers.

    The driver never relies on the backend to terminate by itself.  All backend
    calls are timeout-bounded and the loop is capped by ``max_rounds``.
    """

    backend: InterviewBackend
    answerer: AutoAnswerer = field(default_factory=AutoAnswerer)
    gap_detector: GapDetector = field(default_factory=GapDetector)
    store: AutoStore | None = None
    timeout_seconds: float = 60.0
    max_rounds: int = 12

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        """Run bounded auto interview until Seed-ready or blocked."""
        self._ensure_interview_phase(state)
        try:
            if state.interview_session_id:
                turn = InterviewTurn(
                    question="Continue from persisted auto interview state.",
                    session_id=state.interview_session_id,
                )
            else:
                turn = await self._with_timeout(
                    self.backend.start(state.goal, cwd=state.cwd),
                    state,
                    tool_name="interview.start",
                )
                state.interview_session_id = turn.session_id
                self._save(state)
        except TimeoutError as exc:
            state.mark_blocked(str(exc), tool_name="interview.start")
            self._save(state)
            return AutoInterviewResult("blocked", state.interview_session_id, ledger, state.current_round, str(exc))

        for round_number in range(state.current_round + 1, self.max_rounds + 1):
            state.current_round = round_number
            state.mark_progress(f"interview round {round_number}/{self.max_rounds}")
            self._save(state)

            answer = self.answerer.answer(turn.question, ledger)
            if answer.blocker is not None:
                state.mark_blocked(answer.blocker.reason, tool_name="auto_answerer")
                self._save(state)
                return AutoInterviewResult(
                    "blocked",
                    state.interview_session_id,
                    ledger,
                    round_number,
                    answer.blocker.reason,
                )
            self.answerer.apply(answer, ledger, question=turn.question)
            state.ledger = ledger.to_dict()
            state.mark_progress(
                f"answered round {round_number}/{self.max_rounds} from {answer.source.value}",
                tool_name="auto_answerer",
            )
            self._save(state)

            try:
                turn = await self._with_timeout(
                    self.backend.answer(turn.session_id, answer.prefixed_text),
                    state,
                    tool_name="interview.answer",
                )
            except TimeoutError as exc:
                state.mark_blocked(str(exc), tool_name="interview.answer")
                self._save(state)
                return AutoInterviewResult("blocked", state.interview_session_id, ledger, round_number, str(exc))

            state.interview_session_id = turn.session_id
            self._save(state)
            if (turn.seed_ready or turn.completed) and ledger.is_seed_ready():
                return AutoInterviewResult("seed_ready", turn.session_id, ledger, round_number)

        if not ledger.is_seed_ready():
            gaps = ", ".join(ledger.open_gaps())
            blocker = f"auto interview reached max rounds with unresolved gaps: {gaps}"
            state.mark_blocked(blocker, tool_name="interview_driver")
            self._save(state)
            return AutoInterviewResult("blocked", state.interview_session_id, ledger, self.max_rounds, blocker)
        return AutoInterviewResult("seed_ready", state.interview_session_id, ledger, self.max_rounds)

    async def _with_timeout(self, awaitable: Awaitable[InterviewTurn], state: AutoPipelineState, *, tool_name: str) -> InterviewTurn:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)
        except TimeoutError as exc:
            msg = f"{tool_name} timed out after {self.timeout_seconds:.0f}s for {state.auto_session_id}"
            raise TimeoutError(msg) from exc

    def _ensure_interview_phase(self, state: AutoPipelineState) -> None:
        if state.phase == AutoPhase.CREATED:
            state.transition(AutoPhase.INTERVIEW, "starting auto interview")
            self._save(state)
        elif state.phase != AutoPhase.INTERVIEW:
            msg = f"Auto interview cannot run from phase {state.phase.value}"
            raise ValueError(msg)

    def _save(self, state: AutoPipelineState) -> None:
        if self.store is not None:
            self.store.save(state)


class FunctionInterviewBackend:
    """Adapter for tests or local integrations built from callables."""

    def __init__(
        self,
        start: Callable[[str, str], Awaitable[InterviewTurn]],
        answer: Callable[[str, str], Awaitable[InterviewTurn]],
    ) -> None:
        self._start = start
        self._answer = answer

    async def start(self, goal: str, *, cwd: str) -> InterviewTurn:
        return await self._start(goal, cwd)

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        return await self._answer(session_id, answer)
