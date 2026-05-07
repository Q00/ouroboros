"""Bounded auto Socratic interview driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import inspect
import re
from typing import Protocol
from uuid import uuid4

import structlog

from ouroboros.auto.answerer import (
    AutoAnswer,
    AutoAnswerContext,
    AutoAnswerer,
    AutoAnswerSource,
    AutoBlocker,
)
from ouroboros.auto.gap_detector import Gap, GapDetector
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InterviewTurn:
    """Question returned by an interview backend."""

    question: str
    session_id: str
    seed_ready: bool = False
    completed: bool = False


class InterviewBackend(Protocol):
    """Minimal backend interface needed by the auto interview driver."""

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        """Start an interview and return the first question.

        ``interview_id`` is an optional caller-supplied id.  Backends that
        persist server-side state SHOULD honour it so a driver-level cancel
        (e.g. ``asyncio.wait_for`` timeout) cannot leave the auto state with
        an id that disagrees with the on-disk interview file.
        """

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        """Record an answer and return the next question or completion metadata."""

    async def resume(self, session_id: str) -> InterviewTurn:
        """Return the outstanding question for a persisted interview session."""


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
    context_provider: Callable[[str], AutoAnswerContext] = repo_auto_answer_context
    gap_detector: GapDetector = field(default_factory=GapDetector)
    store: AutoStore | None = None
    timeout_seconds: float = 60.0
    max_rounds: int = 12

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        """Run bounded auto interview until Seed-ready or blocked."""
        self._ensure_interview_phase(state)
        answer_context = self.context_provider(state.cwd)
        interview_tool_name = "interview.start"
        # Pre-allocated interview id, kept local until we have evidence the
        # backend actually persisted (or said it did).  Writing it onto
        # ``state`` prematurely would point ``ooo auto --resume`` at a
        # nonexistent session whenever the backend rejects the start
        # outright (validation/config error).  See Q00/ouroboros#687.
        preassigned_id: str | None = None
        try:
            if state.interview_session_id:
                if state.pending_question:
                    turn = InterviewTurn(
                        question=state.pending_question,
                        session_id=state.interview_session_id,
                    )
                else:
                    interview_tool_name = "interview.resume"
                    turn = _validate_turn(
                        await self._with_timeout(
                            self.backend.resume(state.interview_session_id),
                            state,
                            tool_name=interview_tool_name,
                        )
                    )
                    state.pending_question = turn.question
                    self._save(state)
            else:
                preassigned_id = _generate_interview_id()
                turn = _validate_turn(
                    await self._with_timeout(
                        self.backend.start(state.goal, cwd=state.cwd, interview_id=preassigned_id),
                        state,
                        tool_name=interview_tool_name,
                    )
                )
                if turn.session_id != preassigned_id:
                    # Misbehaving backend ignored the supplied id.  Trust
                    # whatever id the backend actually returned; warn so
                    # operators can spot the contract violation.
                    log.warning(
                        "auto.interview.backend_ignored_preassigned_id",
                        preassigned_id=preassigned_id,
                        backend_id=turn.session_id,
                        auto_session_id=state.auto_session_id,
                    )
                state.interview_session_id = turn.session_id
                state.pending_question = turn.question
                self._save(state)
        except TimeoutError as exc:
            self._record_evidence_based_session_id(state, exc, preassigned_id)
            state.mark_blocked(str(exc), tool_name=interview_tool_name)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, state.current_round, str(exc)
            )
        except Exception as exc:
            self._record_evidence_based_session_id(state, exc, preassigned_id)
            action = "resume" if interview_tool_name == "interview.resume" else "start"
            blocker = f"interview {action} failed: {exc}"
            state.mark_blocked(blocker, tool_name=interview_tool_name)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, state.current_round, blocker
            )

        if turn.seed_ready or turn.completed:
            return self._handle_completed_turn(state, ledger, turn, state.current_round)

        for round_number in range(state.current_round + 1, self.max_rounds + 1):
            state.mark_progress(f"interview round {round_number}/{self.max_rounds}")
            self._save(state)

            answer = self._answer_with_gap_steering(turn.question, ledger, answer_context)
            if answer.blocker is not None:
                self.answerer.apply(answer, ledger, question=turn.question)
                state.ledger = ledger.to_dict()
                state.mark_blocked(answer.blocker.reason, tool_name="auto_answerer")
                self._save(state)
                return AutoInterviewResult(
                    "blocked",
                    state.interview_session_id,
                    ledger,
                    state.current_round,
                    answer.blocker.reason,
                )
            state.current_round = round_number
            self.answerer.apply(answer, ledger, question=turn.question)
            state.ledger = ledger.to_dict()
            state.pending_question = None
            state.mark_progress(
                f"answered round {round_number}/{self.max_rounds} from {answer.source.value}",
                tool_name="auto_answerer",
            )
            self._save(state)

            try:
                turn = _validate_turn(
                    await self._with_timeout(
                        self.backend.answer(turn.session_id, answer.prefixed_text),
                        state,
                        tool_name="interview.answer",
                    )
                )
            except TimeoutError as exc:
                state.mark_blocked(str(exc), tool_name="interview.answer")
                self._save(state)
                return AutoInterviewResult(
                    "blocked", state.interview_session_id, ledger, round_number, str(exc)
                )
            except Exception as exc:
                blocker = f"interview answer failed: {exc}"
                state.mark_blocked(blocker, tool_name="interview.answer")
                self._save(state)
                return AutoInterviewResult(
                    "blocked", state.interview_session_id, ledger, round_number, blocker
                )

            state.interview_session_id = turn.session_id
            if turn.seed_ready or turn.completed:
                return self._handle_completed_turn(state, ledger, turn, round_number)
            state.pending_question = turn.question
            self._save(state)

        if not ledger.is_seed_ready():
            gaps = ", ".join(ledger.open_gaps())
            blocker = f"auto interview reached max rounds with unresolved gaps: {gaps}"
            state.mark_blocked(blocker, tool_name="interview_driver")
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
            )
        blocker = "auto interview reached max rounds before backend marked the Seed ready"
        state.mark_blocked(blocker, tool_name="interview_driver")
        self._save(state)
        return AutoInterviewResult(
            "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
        )

    def _answer_with_gap_steering(
        self, question: str, ledger: SeedDraftLedger, context: AutoAnswerContext
    ) -> AutoAnswer:
        answer = self.answerer.answer(question, ledger, context)
        if answer.blocker is not None:
            return answer
        gaps = self.gap_detector.detect(ledger)
        if not gaps:
            return answer
        updated_sections = {section for section, _entry in answer.ledger_updates}
        if any(gap.section in updated_sections for gap in gaps):
            return answer
        next_gap = gaps[0]
        if not _can_steer_with_gap_prompt(question):
            return answer
        if next_gap.section == "goal" or next_gap.state in {
            LedgerStatus.CONFLICTING,
            LedgerStatus.BLOCKED,
        }:
            blocker = AutoBlocker(reason=next_gap.message, question=question)
            return AutoAnswer(
                text=f"Cannot safely decide automatically: {next_gap.message}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )
        return self.answerer.answer(_gap_prompt(next_gap), ledger, context)

    def _handle_completed_turn(
        self, state: AutoPipelineState, ledger: SeedDraftLedger, turn: InterviewTurn, rounds: int
    ) -> AutoInterviewResult:
        state.interview_session_id = turn.session_id
        state.pending_question = None
        if ledger.is_seed_ready():
            state.interview_completed = True
            self._save(state)
            return AutoInterviewResult("seed_ready", turn.session_id, ledger, rounds)
        gaps = ", ".join(ledger.open_gaps())
        blocker = f"interview backend completed before auto ledger was ready: {gaps}"
        state.mark_blocked(blocker, tool_name="interview_driver")
        self._save(state)
        return AutoInterviewResult("blocked", state.interview_session_id, ledger, rounds, blocker)

    async def _with_timeout(
        self, awaitable: Awaitable[InterviewTurn], state: AutoPipelineState, *, tool_name: str
    ) -> InterviewTurn:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)
        except TimeoutError as exc:
            msg = (
                f"{tool_name} timed out after {self.timeout_seconds:.0f}s "
                f"for {state.auto_session_id} "
                f"(policy: state.timeout_seconds_by_phase[interview])"
            )
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

    def _record_evidence_based_session_id(
        self,
        state: AutoPipelineState,
        exc: BaseException,
        preassigned_id: str | None,
    ) -> None:
        """Save an ``interview_session_id`` on auto state only with evidence.

        Two evidence channels are accepted (Q00/ouroboros#687):

        * ``PartialInterviewStartError`` carries a session id the handler
          has explicitly confirmed as persisted.
        * For ``asyncio.wait_for`` cancellations or other exceptions, the
          driver may probe the backend via the optional
          ``is_session_persisted`` method to see whether a file for the
          pre-allocated id was written before the cancel.

        Without one of these the auto state stays ``None`` so
        ``ooo auto --resume`` cannot point at a nonexistent session.
        """
        if state.interview_session_id:
            return
        # Avoid coupling to the adapter module — local import keeps
        # interview_driver importable on its own.
        from ouroboros.auto.adapters import PartialInterviewStartError

        if isinstance(exc, PartialInterviewStartError) and exc.session_id:
            state.interview_session_id = exc.session_id
            return
        if not preassigned_id:
            return
        probe = getattr(self.backend, "is_session_persisted", None)
        if probe is None:
            return
        try:
            persisted = probe(preassigned_id)
        except Exception as probe_exc:  # pragma: no cover - defensive
            log.warning(
                "auto.interview.persistence_probe_failed",
                preassigned_id=preassigned_id,
                error=str(probe_exc),
            )
            return
        if persisted:
            state.interview_session_id = preassigned_id


class FunctionInterviewBackend:
    """Adapter for tests or local integrations built from callables."""

    def __init__(
        self,
        start: Callable[[str, str], Awaitable[InterviewTurn]],
        answer: Callable[[str, str], Awaitable[InterviewTurn]],
        resume: Callable[[str], Awaitable[InterviewTurn]] | None = None,
        is_session_persisted: Callable[[str], bool] | None = None,
    ) -> None:
        self._start = start
        self._answer = answer
        self._resume = resume
        self._is_session_persisted = is_session_persisted

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        # Forward ``interview_id`` only to callables that opt into the new
        # contract; plain ``(goal, cwd)`` callables remain compatible.
        if "interview_id" in inspect.signature(self._start).parameters:
            return await self._start(goal, cwd, interview_id=interview_id)  # type: ignore[call-arg]
        return await self._start(goal, cwd)

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        return await self._answer(session_id, answer)

    async def resume(self, session_id: str) -> InterviewTurn:
        if self._resume is None:
            msg = "interview resume is unavailable because no pending question is persisted"
            raise RuntimeError(msg)
        return await self._resume(session_id)

    def is_session_persisted(self, session_id: str) -> bool:
        if self._is_session_persisted is None:
            return False
        return bool(self._is_session_persisted(session_id))


def _generate_interview_id() -> str:
    """Return a unique interview id matching the engine's plugin format."""
    return f"interview_{uuid4().hex[:16]}"


def _can_steer_with_gap_prompt(question: str) -> bool:
    lowered = question.lower()
    return bool(
        re.search(
            r"\b(what else|anything else|additional context|more context|what should we know|clarify further)\b",
            lowered,
        )
    )


def _gap_prompt(gap: Gap) -> str:
    prompts = {
        "goal": "Clarify the primary user goal for the Seed.",
        "actors": "Who are the actors, inputs, and outputs for this task?",
        "inputs": "Who are the actors, inputs, and outputs for this task?",
        "outputs": "Who are the actors, inputs, and outputs for this task?",
        "constraints": "What conservative constraints and failure modes should bound this MVP?",
        "failure_modes": "What conservative constraints and failure modes should bound this MVP?",
        "non_goals": "What non-goals should explicitly remain out of scope?",
        "acceptance_criteria": "Which command output verifies the acceptance criteria?",
        "verification_plan": "Which command output verifies the acceptance criteria?",
        "runtime_context": "Which runtime stack, repo, and project patterns should be used?",
    }
    return prompts.get(gap.section, gap.message)


def _validate_turn(value: object) -> InterviewTurn:
    if not isinstance(value, InterviewTurn):
        msg = f"interview backend returned {type(value).__name__}, expected InterviewTurn"
        raise TypeError(msg)
    if not isinstance(value.question, str):
        msg = "interview backend returned non-string question"
        raise TypeError(msg)
    if not isinstance(value.session_id, str) or not value.session_id:
        msg = "interview backend returned invalid session_id"
        raise TypeError(msg)
    if type(value.seed_ready) is not bool or type(value.completed) is not bool:
        msg = "interview backend returned non-boolean completion flags"
        raise TypeError(msg)
    return value
