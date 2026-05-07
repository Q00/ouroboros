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
from ouroboros.auto.blocker_attribution import record_authoring_backend
from ouroboros.auto.gap_detector import GapDetector
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.auto.progress import AutoProgressCallback, AutoProgressEvent
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
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
    progress_callback: AutoProgressCallback | None = None
    _last_emitted_message: str | None = field(default=None, init=False, repr=False)

    def _emit(self, state: AutoPipelineState) -> None:
        """Emit a progress snapshot for the current state via the callback.

        Deduped on ``last_progress_message`` so consumers do not see a
        torrent of identical events for unchanged state. Callback errors
        are swallowed so an observer can never break the interview loop.
        """
        if self.progress_callback is None:
            return
        message = state.last_progress_message
        if message == self._last_emitted_message:
            return
        self._last_emitted_message = message
        event = AutoProgressEvent(
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            kind="phase",
            message=message,
        )
        try:
            self.progress_callback(event)
        except Exception:
            pass

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        """Run bounded auto interview until Seed-ready or blocked."""
        self._last_emitted_message = None
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
            message = str(exc)
            state.mark_blocked(message, tool_name=interview_tool_name)
            record_authoring_backend(state)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, state.current_round, message
            )
        except Exception as exc:
            self._record_evidence_based_session_id(state, exc, preassigned_id)
            action = "resume" if interview_tool_name == "interview.resume" else "start"
            blocker = f"interview {action} failed: {exc}"
            state.mark_blocked(blocker, tool_name=interview_tool_name)
            record_authoring_backend(state)
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
                blocker_text = answer.blocker.reason
                state.mark_blocked(blocker_text, tool_name="auto_answerer")
                record_authoring_backend(state)
                self._save(state)
                return AutoInterviewResult(
                    "blocked",
                    state.interview_session_id,
                    ledger,
                    state.current_round,
                    blocker_text,
                )
            state.current_round = round_number
            self.answerer.apply(answer, ledger, question=turn.question)
            state.ledger = ledger.to_dict()
            state.pending_question = None
            _record_auto_answer(
                state,
                round_number=round_number,
                source=answer.source.value,
                question=turn.question,
                answer=answer.text,
            )
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
                message = str(exc)
                state.mark_blocked(message, tool_name="interview.answer")
                record_authoring_backend(state)
                self._save(state)
                return AutoInterviewResult(
                    "blocked", state.interview_session_id, ledger, round_number, message
                )
            except Exception as exc:
                blocker = f"interview answer failed: {exc}"
                state.mark_blocked(blocker, tool_name="interview.answer")
                record_authoring_backend(state)
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
            finalization = finalize_safe_defaultable_gaps(
                ledger,
                goal=state.goal,
                provenance=f"auto interview max rounds {self.max_rounds}",
                pending_question=state.pending_question,
            )
            state.ledger = ledger.to_dict()
            if finalization.completed and ledger.is_seed_ready():
                state.pending_question = None
                state.interview_completed = True
                state.mark_progress(
                    "auto interview finalized safe-defaultable gaps at max rounds",
                    tool_name="interview_driver",
                )
                self._save(state)
                return AutoInterviewResult(
                    "seed_ready", state.interview_session_id, ledger, self.max_rounds
                )
            gaps = ", ".join(finalization.unsafe_gaps or ledger.open_gaps())
            blocker = f"auto interview reached max rounds with unresolved gaps: {gaps}"
            state.mark_blocked(blocker, tool_name="interview_driver")
            record_authoring_backend(state)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
            )
        blocker = "auto interview reached max rounds before backend marked the Seed ready"
        state.mark_blocked(blocker, tool_name="interview_driver")
        record_authoring_backend(state)
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
        open_before = tuple(ledger.open_gaps())
        if not open_before:
            return answer
        gaps = self.gap_detector.detect(ledger)
        first_gap = gaps[0]

        # `goal` can never be filled by an auto-default — it must come from the
        # user. Block immediately so callers don't send placeholder text to the
        # backend.
        if first_gap.section == "goal":
            blocker = AutoBlocker(reason=first_gap.message, question=question)
            return AutoAnswer(
                text=f"Cannot safely decide automatically: {first_gap.message}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )

        # Steering only kicks in when the answer is a repeated generic fallback
        # or the prompt is broad enough that gap-targeted steering is helpful.
        # Backend-specific answers (e.g. an acceptance follow-up) are preserved
        # even if they don't reduce the required-gap set this turn.
        is_repeated_default = self._is_repeated_default_answer(answer, ledger)
        is_broad_prompt = _can_steer_with_gap_prompt(question)
        if not (is_repeated_default or is_broad_prompt):
            return answer

        # Same-turn repair: a current answer that actually reduces required
        # gaps — including a CONFLICTING/BLOCKED one — is allowed through
        # before we raise a hard blocker. This lets the driver recover from
        # persisted ledger conflicts when the next prompt yields a correcting
        # answer.
        if not is_repeated_default and self._answer_reduces_open_gaps(
            question, answer, ledger, open_before
        ):
            return answer

        if first_gap.state in {LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}:
            blocker = AutoBlocker(reason=first_gap.message, question=question)
            return AutoAnswer(
                text=f"Cannot safely decide automatically: {first_gap.message}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )

        gap_answer = self.answerer.answer_gap(first_gap.section, ledger, context)
        if gap_answer.blocker is not None:
            return gap_answer
        if self._answer_reduces_open_gaps(question, gap_answer, ledger, open_before):
            return gap_answer

        blocker = AutoBlocker(
            reason=(
                f"auto answer did not reduce open required ledger gaps: {', '.join(open_before)}"
            ),
            question=question,
        )
        return AutoAnswer(
            text=(
                "Cannot safely decide automatically: auto answer did not reduce open "
                f"required ledger gaps: {', '.join(open_before)}"
            ),
            source=AutoAnswerSource.BLOCKER,
            confidence=1.0,
            blocker=blocker,
        )

    def _answer_reduces_open_gaps(
        self,
        question: str,
        answer: AutoAnswer,
        ledger: SeedDraftLedger,
        open_before: tuple[str, ...],
    ) -> bool:
        if answer.blocker is not None:
            return False
        simulated = SeedDraftLedger.from_dict(ledger.to_dict())
        self.answerer.apply(answer, simulated, question=question)
        open_after = tuple(simulated.open_gaps())
        return len(open_after) < len(open_before) and set(open_after).issubset(open_before)

    def _is_repeated_default_answer(self, answer: AutoAnswer, ledger: SeedDraftLedger) -> bool:
        # Only the catch-all generic-default route counts as a "repeated
        # generic fallback". Feature-specific helpers (acceptance, runtime,
        # IO/actor, verification, non-goal, product behavior) may also use
        # ``CONSERVATIVE_DEFAULT`` as their answer source but should not be
        # treated as fallback answers — repeated specific follow-ups stay
        # preserved instead of being swapped for an unrelated gap fill.
        if not answer.generic_default:
            return False
        proposed = _normalize_answer_text(answer.prefixed_text)
        return any(
            _normalize_answer_text(item.get("answer", "")) == proposed
            for item in ledger.question_history
        )

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
        record_authoring_backend(state)
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
        # Per-round / per-error progress lives in ``state.last_progress_message``;
        # emit it here so observers see every interview-loop save without each
        # call site needing to remember to fire the callback.
        self._emit(state)

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


_BROAD_PROMPT_RE = re.compile(
    r"\b(what else|anything else|additional context|more context|"
    r"what should we know|clarify further)\b"
)


def _can_steer_with_gap_prompt(question: str) -> bool:
    """Return True when ``question`` is broad enough to benefit from gap-targeted steering."""
    return bool(_BROAD_PROMPT_RE.search(question.lower()))


_AUTO_ANSWER_LOG_LIMIT = 25
_AUTO_ANSWER_LOG_TEXT_LIMIT = 200


def _record_auto_answer(
    state: AutoPipelineState,
    *,
    round_number: int,
    source: str,
    question: str,
    answer: str,
) -> None:
    """Append a source-tagged auto answer entry to ``state.auto_answer_log``.

    The log is bounded to the last :data:`_AUTO_ANSWER_LOG_LIMIT` entries so the
    persisted state file stays compact across long sessions.
    """
    state.auto_answer_log.append(
        {
            "round": round_number,
            "source": source,
            "question": _truncate(question, _AUTO_ANSWER_LOG_TEXT_LIMIT),
            "answer": _truncate(answer, _AUTO_ANSWER_LOG_TEXT_LIMIT),
        }
    )
    if len(state.auto_answer_log) > _AUTO_ANSWER_LOG_LIMIT:
        del state.auto_answer_log[: len(state.auto_answer_log) - _AUTO_ANSWER_LOG_LIMIT]


def _truncate(text: str, limit: int) -> str:
    if not isinstance(text, str):
        text = str(text)
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[: limit - 3]}..."


def _normalize_answer_text(text: str) -> str:
    return " ".join(str(text).casefold().split())


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
