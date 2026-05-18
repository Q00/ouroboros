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
from ouroboros.auto.safe_defaults import (
    build_safe_default_synthesis,
    finalize_safe_defaultable_gaps,
)
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InterviewTurn:
    """Question returned by an interview backend."""

    question: str
    session_id: str
    seed_ready: bool = False
    completed: bool = False
    # Optional diagnostic surface — backend's own ambiguity reading for the
    # current turn. The driver never gates on this; it is only used to build
    # informative blocker messages when the mutual-agreement closure gate
    # exhausts its budget without both parties converging.
    ambiguity_score: float | None = None


class InterviewBackend(Protocol):
    """Minimal backend interface needed by the auto interview driver."""

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        """Start an interview and return the first question.

        ``interview_id`` is an optional caller-supplied id.  Backends that
        persist server-side state SHOULD honour it so a driver-level cancel
        (e.g. ``asyncio.wait_for`` timeout) cannot leave the auto state with
        an id that disagrees with the on-disk interview file.
        """

    async def answer(
        self, session_id: str, answer: str, *, last_question: str | None = None
    ) -> InterviewTurn:
        """Record an answer and return the next question or completion metadata.

        ``last_question`` is supplied when the driver is answering a
        driver-originated probe rather than an unanswered backend turn. The
        MCP interview handler requires it when reopening an already-answered
        seed-ready interview so the transcript is not bound to stale question
        text.
        """

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

        # Closure gate: an interview closes only when the backend (semantic
        # ambiguity model) AND the driver-side ledger (structural completeness)
        # agree on the same turn. Disagreement in either direction is reframed
        # as the next answer instead of being treated as a terminal block:
        #
        # * backend signals completion but the ledger has open gaps → answer
        #   the first open gap so the backend re-scores against substantive
        #   new content and either accepts or asks a follow-up.
        # * backend keeps asking but the ledger is structurally full → keep
        #   answering normally; let the backend drive the dialogue.
        #
        # ``max_rounds`` is the sole budget. If the loop exits without mutual
        # agreement, the blocker reports both readiness states so callers can
        # decide whether to raise ``max_rounds`` or sharpen the goal.
        for round_number in range(state.current_round + 1, self.max_rounds + 1):
            backend_done = turn.seed_ready or turn.completed
            ledger_done = ledger.is_seed_ready()
            if backend_done and ledger_done:
                state.pending_question = None
                state.interview_completed = True
                self._save(state)
                return AutoInterviewResult(
                    "seed_ready", state.interview_session_id, ledger, state.current_round
                )

            state.mark_progress(f"interview round {round_number}/{self.max_rounds}")
            self._save(state)

            if backend_done and not ledger_done:
                # Backend said done but ledger isn't — pick the first detected
                # gap and answer it. This drives the backend to reopen with
                # substantive new content; we never accept closure unilaterally.
                # Mirror the safety guards that ``_answer_with_gap_steering``
                # enforces so a backend-reported "done" against a CONFLICTING /
                # BLOCKED / goal-missing ledger does NOT silently get a
                # fabricated auto-answer appended — those terminal conditions
                # must surface the unresolved conflict immediately.
                detected_gaps = self.gap_detector.detect(ledger)
                if not detected_gaps:
                    # Defensive: ``ledger.is_seed_ready()`` was False yet the
                    # structured detector finds no actionable gap. Treat as
                    # the canonical "must keep asking" path so we at least
                    # send something through the backend instead of crashing.
                    answer = self._answer_with_gap_steering(turn.question, ledger, answer_context)
                    question_for_record = turn.question
                else:
                    first_gap = detected_gaps[0]
                    if first_gap.section == "goal" or first_gap.state in {
                        LedgerStatus.CONFLICTING,
                        LedgerStatus.BLOCKED,
                    }:
                        blocker_text = first_gap.message
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
                    answer = self.answerer.answer_gap(first_gap.section, ledger, answer_context)
                    question_for_record = (
                        f"[driver gap-reopen '{first_gap.section}': "
                        "backend_completed=True ledger_done=False]"
                    )
            else:
                answer = self._answer_with_gap_steering(turn.question, ledger, answer_context)
                question_for_record = turn.question

            if answer.blocker is not None:
                self.answerer.apply(answer, ledger, question=question_for_record)
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
            self.answerer.apply(answer, ledger, question=question_for_record)
            state.ledger = ledger.to_dict()
            state.pending_question = None
            _record_auto_answer(
                state,
                round_number=round_number,
                source=answer.source.value,
                question=question_for_record,
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
                        self.backend.answer(
                            turn.session_id,
                            answer.prefixed_text,
                            last_question=question_for_record,
                        ),
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
            state.pending_question = turn.question
            self._save(state)

        # max_rounds exhausted — one final closure check, then safe-default
        # finalization for autonomous ``ooo auto``.  The manual
        # ``ooo interview`` path remains strict/fail-closed in its own driver;
        # this auto driver is allowed to close missing/weak required sections
        # when the goal is local, reversible, and covered by the audited
        # safe-default policy.  This is intentionally after the bounded
        # interview loop: explicit/backend-provided answers always win first,
        # and defaults are only the final escape from a benign stalled dialog.
        backend_done = turn.seed_ready or turn.completed
        ledger_done = ledger.is_seed_ready()
        if backend_done and ledger_done:
            state.pending_question = None
            state.interview_completed = True
            self._save(state)
            return AutoInterviewResult(
                "seed_ready", state.interview_session_id, ledger, self.max_rounds
            )

        finalization = None
        if not backend_done:
            finalization = finalize_safe_defaultable_gaps(
                ledger,
                goal=state.goal,
                provenance=f"auto interview max_rounds={self.max_rounds}",
                pending_question=turn.question,
                active_profile=getattr(self.answerer, "active_profile", None),
            )
        if finalization is not None and finalization.completed and ledger.is_seed_ready():
            synthesis = build_safe_default_synthesis(finalization)
            if synthesis and state.interview_session_id:
                try:
                    synthesis_turn = _validate_turn(
                        await self._with_timeout(
                            self.backend.answer(
                                state.interview_session_id,
                                synthesis,
                                last_question=(
                                    "[driver safe-default finalization: "
                                    f"max_rounds={self.max_rounds}]"
                                ),
                            ),
                            state,
                            tool_name="interview.safe_default_synthesis",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - preserve transcript/ledger SSOT
                    _revert_safe_default_entries(ledger, finalization.defaulted_sections)
                    blocker = (
                        "safe-default synthesis transcript sync failed; "
                        f"rolled back defaulted sections: {', '.join(finalization.defaulted_sections)}; "
                        f"error={exc}"
                    )
                    log.warning(
                        "auto.interview.safe_default_synthesis_failed",
                        auto_session_id=state.auto_session_id,
                        interview_session_id=state.interview_session_id,
                        defaulted_sections=finalization.defaulted_sections,
                        error=str(exc),
                    )
                    state.ledger = ledger.to_dict()
                    state.mark_blocked(blocker, tool_name="interview.safe_default_synthesis")
                    record_authoring_backend(state)
                    self._save(state)
                    return AutoInterviewResult(
                        "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
                    )
                state.interview_session_id = synthesis_turn.session_id
                state.pending_question = synthesis_turn.question
                if not (synthesis_turn.seed_ready or synthesis_turn.completed):
                    _revert_safe_default_entries(ledger, finalization.defaulted_sections)
                    blocker = (
                        "safe-default synthesis did not close the persisted interview: "
                        "backend_done=False, ledger defaults rolled back"
                    )
                    state.ledger = ledger.to_dict()
                    state.mark_blocked(blocker, tool_name="interview.safe_default_synthesis")
                    record_authoring_backend(state)
                    self._save(state)
                    return AutoInterviewResult(
                        "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
                    )
            state.ledger = ledger.to_dict()
            state.pending_question = None
            state.interview_completed = True
            state.mark_progress(
                "safe-default finalization closed interview gaps: "
                + ", ".join(finalization.defaulted_sections),
                tool_name="interview_driver",
            )
            self._save(state)
            return AutoInterviewResult(
                "seed_ready", state.interview_session_id, ledger, self.max_rounds
            )

        if turn.ambiguity_score is not None:
            ambiguity_part = f"ambiguity_score={turn.ambiguity_score:.2f}"
        else:
            ambiguity_part = "ambiguity_score=unknown"
        open_gaps = ledger.open_gaps()
        gaps_part = f"open_gaps={open_gaps}" if open_gaps else "open_gaps=[]"
        blocker = (
            f"auto interview reached max_rounds={self.max_rounds} without closure: "
            f"backend_done={backend_done} ({ambiguity_part}), "
            f"ledger_done={ledger_done} ({gaps_part})"
        )
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
        answer: Callable[..., Awaitable[InterviewTurn]],
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

    async def answer(
        self, session_id: str, answer: str, *, last_question: str | None = None
    ) -> InterviewTurn:
        # Forward ``last_question`` only to callables that opt into the
        # reopened-interview contract; legacy ``(session_id, answer)`` test
        # callables remain compatible.
        if "last_question" in inspect.signature(self._answer).parameters:
            return await self._answer(session_id, answer, last_question=last_question)
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


def _revert_safe_default_entries(
    ledger: SeedDraftLedger, defaulted_sections: tuple[str, ...]
) -> None:
    """Remove the safe-default policy's entries from the named sections.

    Used when the safe-default synthesis cannot be persisted to the interview
    transcript: rolling back the policy's own DEFAULTED entries restores the
    ledger to its pre-finalization state so ``open_gaps()`` and the block
    message report the genuinely unresolved sections to downstream consumers
    of the convergence contract.
    """
    for section_name in defaulted_sections:
        section = ledger.sections.get(section_name)
        if section is None:
            continue
        # Match the EXACT key the safe-default policy writes
        # (``{section}.safe_default_finalization``) instead of any key
        # that happens to end with the suffix. The earlier
        # ``endswith(...)`` form would also delete a user-authored
        # entry whose key coincidentally ended in
        # ``.safe_default_finalization`` — for example, an answerer-
        # synthesized constraint key
        # ``constraints.my.safe_default_finalization``. The
        # ``finalize_safe_defaultable_gaps`` writer is the SOLE
        # producer of the canonical key shape, so an exact equality
        # check is both correct (matches every entry the policy
        # wrote) and safer (matches only those entries).
        canonical_key = f"{section_name}.safe_default_finalization"
        section.entries = [entry for entry in section.entries if entry.key != canonical_key]


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
    if value.ambiguity_score is not None and not isinstance(value.ambiguity_score, (int, float)):
        msg = "interview backend returned non-numeric ambiguity_score"
        raise TypeError(msg)
    return value
