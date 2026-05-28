"""Bounded auto Socratic interview driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import inspect
import re
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING to avoid a runtime cycle: adapters.py
    # imports ``InterviewBackend`` / ``InterviewTurn`` from this module.
    from ouroboros.auto.adapters import LateralResult
    from ouroboros.persistence.event_store import EventStore

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
from ouroboros.auto.lateral_routing import select_persona_for_safe_default_block
from ouroboros.auto.ledger import LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.progress import AutoProgressCallback, AutoProgressEvent
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.safe_defaults import (
    SafeDefaultFinalization,
    build_safe_default_synthesis,
    finalize_safe_defaultable_gaps,
)
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.events.base import BaseEvent
from ouroboros.resilience.lateral import ThinkingPersona

log = structlog.get_logger(__name__)

# RFC #1256 §I4 — observer is OFF the pipeline's critical path.
#
# The §I4 fail-open contract requires that EventStore persistence
# cannot weaken cancellation, phase deadlines, or the top-level run
# budget. The naive shape (`await self.event_store.append(...)` inline)
# violates this because `AutoPipeline.run` wraps the driver in
# `asyncio.wait_for(self.interview_driver.run(state, ledger),
# timeout=_deadline_capped_timeout(...))` at
# `src/ouroboros/auto/pipeline.py:602`, and the cap can validly fall
# BELOW the observer's own fail-open bound when the top-level deadline
# is nearly expired (or when `phase_timeout_seconds(INTERVIEW)` is
# set to 1 via persisted/env policy). A bounded inline await still
# spends the remaining pipeline budget — bot review on commit
# 4fd6cfc1 (req_1779890159_141) reproduced this: a 5 s slow `opened`
# append + `asyncio.wait_for(driver.run(...), timeout=0.1)` raised
# `TimeoutError` after ~0.102 s, before `_run_inner` ever ran.
#
# The fix moves the append off the critical path entirely:
# `_emit_event` schedules a background `asyncio.Task` and returns
# immediately. The pipeline's wait_for never sees observer latency.
# Each background task is still bounded by
# `_EVENT_STORE_EMIT_TIMEOUT_SECONDS` so a stuck observer cannot
# leak indefinitely; exceptions and timeouts are downgraded to
# typed structlog warnings.
#
# Tasks are tracked on `_pending_emit_tasks` so tests/operators can
# `await driver.wait_for_pending_emits()` for deterministic
# inspection. The set is cleared via `add_done_callback` so it never
# grows unbounded.
_EVENT_STORE_EMIT_TIMEOUT_SECONDS = 1.0

# RFC #1256 §I4 — durability is a COMPOSITION-ROOT responsibility.
#
# The driver intentionally does NOT drain ``_pending_emit_tasks``
# inside ``run()``. Two prior bot blockers explain why both extremes
# fail:
#
# * Fire-and-forget with no drain anywhere (commit ef0fec17,
#   req_1779891123_145) silently loses every event when the owning
#   event loop closes — the bot reproduced this with a 0.01 s append
#   store and ``asyncio.run(driver.run(...))``.
# * Drain inside ``run()`` (commit c5549124, req_1779938459_153)
#   re-introduces observer latency on the pipeline-critical path:
#   ``AutoPipeline.run`` wraps ``driver.run`` in
#   ``asyncio.wait_for(..., timeout=interview_timeout)``, so even a
#   50 ms drain after a 0.08 s ``_run_inner`` converts a completed
#   interview into an interview-phase timeout at the deadline-capped
#   budget (bot probe: ``_run_inner`` at 0.08 s + 0.2 s append +
#   outer 0.1 s ``wait_for`` → ``TimeoutError`` at ~0.102 s).
#
# The stable shape: ``_emit_event`` schedules persistence as a
# background ``asyncio.Task`` and returns immediately; ``run()``
# never awaits the pending set. Composition roots (``AutoPipeline``
# is the production owner today; future CLI/MCP composition roots
# when the next wiring slice lands) are responsible for calling
# ``await driver.wait_for_pending_emits()`` — typically under their
# own bounded shield — OUTSIDE their critical ``wait_for`` boundary so:
#
#   1. The §I4 substrate contract — "supply an EventStore, get
#      queryable lifecycle evidence" — is honoured by the post-result
#      drain that the composition root owns.
#   2. The interview phase's hard deadline budget is never spent by
#      observability work; a slow EventStore cannot weaken
#      cancellation, phase timeouts, or top-level deadline
#      enforcement.
#
# ``AutoPipeline.run`` implements this contract via
# ``_drain_interview_observer_events`` (see pipeline.py) on both the
# clean-exit and timeout-exit paths of the interview ``wait_for``,
# with a bounded ``asyncio.shield`` so the drain itself cannot be
# cancelled by a top-level deadline once it begins, and a fail-open
# timeout that downgrades pathologically slow observers to a typed
# structlog warning.

INTERVIEW_SAFE_DEFAULT_SYNTHESIS_STOP_REASON_CODE = "interview_safe_default_synthesis_incomplete"
BACKEND_READY_AMBIGUITY_THRESHOLD = 0.20

# Issue #1248 — L5 ladder typed terminal for a safe-default lateral escalation
# that exhausted every available persona without resolving the matcher fire.
# Mirrors the runtime ``watchdog_wall_clock_exceeded`` / interview
# ``interview_unsafe_gaps_remain`` patterns: a module-level const so callers
# emit the same string, and so the alphabet of valid stop_reason_code values
# can be discovered by reading the module surface.
UNSTUCK_EXHAUSTED_STOP_REASON_CODE = "unstuck_exhausted"

# Issue #1248 PR-B review — the safe-default lateral escalation may only
# demote active ``CONSERVATIVE_DEFAULT`` ledger entries to ``ASSUMPTION``
# when the lateral persona response includes a machine-checkable clearance
# line. The auto pipeline's production ``ouroboros_lateral_think`` inline
# path returns a *prompt template* (not an executed judgement), so without
# this gate a bare prompt would be enough to reclassify entries — including
# entries that record genuinely unsafe scope (production deploys, credential
# handling). The canonical line, anchored to its own line so a stray mention
# inside prose does not satisfy the gate, is:
#
#     CLEARANCE: lexical_false_positive
#
# Any other shape (no marker, an explicit ``UNSAFE_CONFIRMED: <reason>``
# counter-marker, a plain prompt template) means *the lateral was attempted
# but no judgement is available* — the persona invocation is still recorded
# on ``state.personas_invoked`` for audit, but the ledger stays put and the
# loop either tries the next persona or, after both are tried, surfaces a
# typed ``unstuck_exhausted`` BLOCKED. This is conservative-by-default: the
# matcher fire stays loud (no silent closure on unsafe scope) until a real
# judge layered on top of the prompt emits the marker.
_LATERAL_CLEARANCE_MARKER_RE = re.compile(
    r"^\s*CLEARANCE:\s*lexical_false_positive\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _lateral_response_authorizes_demotion(text: str | None) -> bool:
    """Return ``True`` iff the lateral response includes the canonical clearance marker.

    Empty / ``None`` / unmarked responses return ``False`` — the inline
    ``ouroboros_lateral_think`` path's bare prompt template falls in this
    bucket, so a successful prompt-generation call is *not* enough to
    authorize ledger demotion.
    """
    if not text:
        return False
    return bool(_LATERAL_CLEARANCE_MARKER_RE.search(text))


# Structural alias for the production lateral-thinker callable. Mirrors the
# alias declared on ``ouroboros.auto.pipeline``; duplicated locally because
# ``adapters.LateralResult`` cannot be imported at module load time without
# creating a cycle (adapters imports ``InterviewBackend`` from this module).
# At runtime the alias is just ``Callable[..., Awaitable[Any]]``; the typing
# block above gives mypy the precise return type.
LateralThinker = Callable[..., Awaitable["LateralResult"]]


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
    # Issue #1248 — optional lateral-thinker handle used by the safe-default
    # escalation path to disambiguate matcher-fire false positives from
    # real unsafe scope before falling to BLOCKED. ``None`` preserves the
    # pre-issue behavior (matcher fire → immediate
    # ``interview_unsafe_gaps_remain`` BLOCKED), so existing call sites and
    # tests that construct the driver without this argument are unaffected.
    # The behavior change that consumes this field ships in a separate PR.
    lateral_thinker: LateralThinker | None = None
    # RFC #1256 §I4 — Unified observability for ooo auto interview.
    # When set, the driver emits typed ``auto.interview.*`` events to the
    # EventStore alongside the existing structlog ``log.info(...)`` calls,
    # so post-hoc evidence inspection via ``ouroboros_query_events`` can
    # surface the interview's lifecycle. Defaults to ``None`` to preserve
    # back-compat for every existing call site (CLI, MCP handler, tests)
    # that constructs the driver without observability wiring. Errors
    # raised by the event store are caught and logged as warnings — an
    # observer is never permitted to break the interview loop.
    event_store: EventStore | None = None
    _last_emitted_message: str | None = field(default=None, init=False, repr=False)
    # Track outstanding background `_emit_event` tasks so tests and
    # operators can await deterministic completion via
    # ``wait_for_pending_emits``. The set is cleaned up via
    # ``add_done_callback`` so it never grows unbounded — see the
    # module-level comment on ``_EVENT_STORE_EMIT_TIMEOUT_SECONDS``.
    _pending_emit_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )

    async def _append_with_fail_open(
        self,
        event_type: str,
        aggregate_id: str,
        data: dict[str, object],
    ) -> None:
        """Append a single event with bounded fail-open semantics.

        Runs inside the background task scheduled by :meth:`_emit_event`.
        Bounded by ``_EVENT_STORE_EMIT_TIMEOUT_SECONDS`` so a stuck
        observer cannot leak indefinitely; timeouts and exceptions are
        downgraded to typed structlog warnings.
        """
        assert self.event_store is not None  # guarded by caller
        try:
            await asyncio.wait_for(
                self.event_store.append(
                    BaseEvent(
                        type=event_type,
                        aggregate_type="auto_interview",
                        aggregate_id=aggregate_id,
                        data=data,
                    )
                ),
                timeout=_EVENT_STORE_EMIT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "auto.interview.event_store_emit_timed_out",
                event_type=event_type,
                auto_session_id=aggregate_id,
                timeout_seconds=_EVENT_STORE_EMIT_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # Background task cancelled (e.g. event loop shutdown).
            # The §I4 contract treats observer loss during shutdown
            # as acceptable; re-raising would surface as an unhandled
            # task exception in logs without operator-actionable
            # information.
            return
        except Exception as exc:  # noqa: BLE001 — observer must not break the loop
            log.warning(
                "auto.interview.event_store_emit_failed",
                event_type=event_type,
                auto_session_id=aggregate_id,
                error=str(exc),
            )

    async def _emit_event(
        self,
        event_type: str,
        aggregate_id: str | None,
        **data: object,
    ) -> None:
        """Schedule a typed ``auto.interview.*`` event for background
        persistence.

        The append is **dispatched as a background** ``asyncio.Task``
        **and not awaited** so EventStore latency never consumes the
        pipeline-side ``asyncio.wait_for(interview_timeout)`` budget
        (see the module-level comment for the full rationale and the
        bot-review reproducers that motivated the dispatch shape).

        The method is ``async`` so call sites keep ``await
        self._emit_event(...)`` as the syntactic anchor; the body
        completes in O(``create_task``) — no I/O on the critical path.
        Background errors and timeouts are downgraded to typed
        structlog warnings inside :meth:`_append_with_fail_open`;
        the interview loop is never blocked by observability.

        ``aggregate_id`` is the ``auto_session_id`` of the live state
        so ``ouroboros_query_events(auto_session_id)`` finds every
        interview event for that session.
        """
        if self.event_store is None:
            return
        if not aggregate_id:
            return
        task = asyncio.create_task(
            self._append_with_fail_open(event_type, aggregate_id, dict(data))
        )
        # Hold a strong reference so the task is not garbage-collected
        # mid-flight, and clean it up on completion so the set stays
        # bounded.
        self._pending_emit_tasks.add(task)
        task.add_done_callback(self._pending_emit_tasks.discard)

    async def wait_for_pending_emits(self) -> None:
        """Await every outstanding background emit task.

        Composition-root durability boundary per the §I4 contract
        (see module-level comment): ``_emit_event`` schedules typed
        ``auto.interview.*`` appends as background tasks and ``run()``
        never awaits them, so observability work cannot weaken the
        pipeline's ``asyncio.wait_for(interview_timeout)`` budget.
        Composition roots (``AutoPipeline`` today via
        ``_drain_interview_observer_events``; future CLI/MCP roots
        when their wiring slice lands) call this method OUTSIDE their
        critical ``wait_for`` to persist scheduled lifecycle events
        before continuing — typically under their own bounded
        ``asyncio.shield`` so a slow observer cannot extend the
        post-result phase indefinitely.

        Exceptions are not propagated — the same fail-open contract
        that downgrades errors inside the task applies here. The
        method returns once every currently-pending task is done.
        Tasks scheduled during the await are gathered in the next
        iteration of the while-loop.
        """
        while self._pending_emit_tasks:
            pending = list(self._pending_emit_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

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
        """Run bounded auto interview until Seed-ready or blocked.

        Public entry point that wraps :meth:`_run_inner` with RFC #1256 §I4
        lifecycle event emission: ``auto.interview.opened`` before the loop
        starts, and ``auto.interview.finalized`` (or
        ``auto.interview.failed`` if an exception escapes the inner loop)
        once a terminal result is known. The 13 internal ``return
        AutoInterviewResult(...)`` paths inside ``_run_inner`` keep their
        existing structlog instrumentation untouched; this wrapper only
        adds the typed events that ``ouroboros_query_events`` consumes.
        """

        await self._emit_event(
            "auto.interview.opened",
            state.auto_session_id,
            goal=state.goal,
            max_rounds=self.max_rounds,
            cwd=state.cwd,
            resumed=bool(state.interview_session_id),
        )
        try:
            result = await self._run_inner(state, ledger)
        except Exception as exc:
            # NOTE: we intentionally catch ``Exception`` (NOT
            # ``BaseException``) so ``asyncio.CancelledError`` — which
            # inherits from ``BaseException`` — propagates immediately
            # without awaiting the best-effort ``_emit_event`` append.
            # ``AutoPipeline.run`` wraps this call in
            # ``asyncio.wait_for(..., timeout=interview_timeout)``; if
            # the wrapper awaited persistence during cancellation, the
            # phase deadline could be exceeded by whatever the
            # EventStore's append latency happens to be. The §I4
            # observer must never weaken deadlines or cancellation —
            # that is a hard runtime control path, not a
            # best-effort persistence path. Bot review on commit
            # 0a1a9c34 (req_1779886484_124) reproduced the overrun
            # with a 0.05s wait_for and a 0.2s blocking append; the
            # narrowed catch closes that contract failure.
            await self._emit_event(
                "auto.interview.failed",
                state.auto_session_id,
                exception_type=type(exc).__name__,
                exception_message=str(exc)[:500],
            )
            raise
        await self._emit_event(
            "auto.interview.finalized",
            state.auto_session_id,
            status=result.status,
            rounds=result.rounds,
            interview_session_id=result.session_id,
            blocker=(result.blocker or "")[:500],
        )
        # NOTE: deliberately no drain here. ``_emit_event`` schedules
        # the typed lifecycle appends as background tasks; the
        # composition root (``AutoPipeline._drain_interview_observer_events``
        # for the production wiring path; see pipeline.py) is
        # responsible for awaiting them OUTSIDE the pipeline-side
        # ``asyncio.wait_for(interview_timeout)`` boundary so a slow
        # EventStore cannot turn a completed interview into a phase
        # timeout. Bot review on commit ``c5549124``
        # (req_1779938459_153) reproduced the contract failure when
        # this drain ran inside ``run()``. See the module-level
        # comment for the full rationale.
        return result

    async def _run_inner(
        self, state: AutoPipelineState, ledger: SeedDraftLedger
    ) -> AutoInterviewResult:
        """Bounded auto interview loop. See :meth:`run` for §I4 wrapping."""
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
            if interview_tool_name == "interview.start":
                fallback = self._try_close_after_backend_start_failure(state, ledger, exc)
                if fallback is not None:
                    return fallback
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
            if action == "start":
                fallback = self._try_close_after_backend_start_failure(state, ledger, exc)
                if fallback is not None:
                    return fallback
            blocker = f"interview {action} failed: {exc}"
            state.mark_blocked(blocker, tool_name=interview_tool_name)
            record_authoring_backend(state)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, state.current_round, blocker
            )

        # Closure gate (ledger-primary, backend-advisory per SSOT #1157
        # *Closure Policy* section, 2026-05-27): an interview closes the
        # moment ``ledger.is_seed_ready()`` returns True, regardless of
        # backend ``seed_ready`` state. Backend signals remain useful as
        # advisory metadata but no longer gate closure on their own.
        # Disagreement is reframed as the next answer instead of a terminal
        # block:
        #
        # * backend signals completion but the ledger has open gaps → answer
        #   the first open gap so the backend re-scores against substantive
        #   new content; the loop refuses backend-only closure (the
        #   premature-closure invariant preserved below).
        # * backend keeps asking but the ledger is structurally full → close
        #   immediately as ``ledger_only``; do NOT let the backend extend the
        #   dialogue past structural completeness, because an LLM evaluator
        #   never saturates and waiting for its agreement stalls indefinitely
        #   (the #1170 R2-diag failure mode).
        #
        # ``max_rounds`` is the budget for ledger-filling rounds. The interview
        # closes the moment ``ledger.is_seed_ready()`` returns True
        # (ledger-primary closure policy per SSOT #1157 "Closure Policy"
        # section, formalized 2026-05-27 after #1170 R2-diag confirmed the
        # legacy AND-gate stalls the simplest canonical case). Backend
        # ``seed_ready`` / ``completed`` / ``ambiguity_score`` is recorded as
        # advisory signal on ``state.interview_closure_mode`` but no longer
        # gates closure — an LLM evaluator never saturates, so requiring its
        # agreement makes every populated ledger wait indefinitely for an
        # acknowledgement that never comes. PR-B1 (#1148) shipped a
        # ``ledger_only`` escape hatch at ``max_rounds`` but kept the AND-gate
        # as the primary path; this PR promotes ``ledger_only`` to the
        # primary in-loop closure mode (see SSOT freshness sync 2026-05-27).
        #
        # Premature-closure invariant preserved below: when the backend
        # reports ``seed_ready`` / ``completed`` but the ledger has open
        # required gaps, the loop refuses backend-only closure and steers
        # the next answer toward filling the next detected gap.
        for round_number in range(state.current_round + 1, self.max_rounds + 1):
            backend_done = turn.seed_ready or turn.completed
            ledger_done = ledger.is_seed_ready()
            if ledger_done:
                closure_mode = "mutual_agreement" if backend_done else "ledger_only"
                # Observability parity with the previous max_rounds-only
                # ledger_only path: emit the same ``auto.interview.ledger_only_closure``
                # event so existing log-grep consumers and #1170 R2 evidence
                # captures continue to work. ``mutual_agreement`` is not given
                # a dedicated event because it was not previously emitted in
                # the in-loop close path.
                if not backend_done:
                    log.info(
                        "auto.interview.ledger_only_closure",
                        auto_session_id=state.auto_session_id,
                        round_number=round_number,
                        ambiguity_score=turn.ambiguity_score,
                        interview_session_id=state.interview_session_id,
                    )
                state.pending_question = None
                state.interview_completed = True
                state.interview_closure_mode = closure_mode
                state.mark_progress(
                    f"interview closed via {closure_mode} at round "
                    f"{round_number}/{self.max_rounds} "
                    f"(backend_ambiguity={turn.ambiguity_score})",
                    tool_name="interview_driver",
                )
                self._save(state)
                # ``state.current_round`` reflects the number of completed
                # answer-exchange rounds so far. Use it (not ``round_number``,
                # which is the upcoming iteration index that hasn't done any
                # work yet) so the result carries the actual rounds consumed.
                return AutoInterviewResult(
                    "seed_ready", state.interview_session_id, ledger, state.current_round
                )

            state.mark_progress(f"interview round {round_number}/{self.max_rounds}")
            self._save(state)

            if backend_done and not ledger_done:
                backend_ready_defaults = await self._try_close_backend_ready_safe_defaults(
                    state,
                    ledger,
                    turn,
                )
                if backend_ready_defaults is not None:
                    return backend_ready_defaults

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
            # Apply the answer to the in-memory ledger only. Do NOT persist
            # ``state.ledger`` / ``state.current_round`` / ``state.pending_question``
            # / ``state.auto_answer_log`` until ``backend.answer`` acknowledges
            # the round.
            #
            # Why deferred persistence (SSOT #1157 *Closure Policy* — bot review #2
            # BLOCKER on 17703155): persisting a complete ledger before the
            # backend transcript reflects the answer creates a resume-time
            # transcript-sync gap. The previous order saved the complete
            # ledger first, then called ``backend.answer``; if that backend
            # call timed out or raised, resume would see the persisted
            # complete ledger, hit the ledger-primary closure gate above,
            # close as ``ledger_only``, and proceed to SEED_GENERATION —
            # but ``GenerateSeedHandler`` reads the persisted interview
            # transcript, not the ledger, and the backend never accepted
            # the last answer, so the Seed would be generated from stale
            # transcript evidence. Deferring the persistence keeps
            # ``state.ledger`` and the backend transcript monotonically in
            # sync: on backend.answer failure, ``state.ledger`` on disk is
            # the pre-answer state, resume re-enters the loop, the answerer
            # deterministically re-computes the same answer, and the next
            # ``backend.answer`` attempt either succeeds (round completes
            # cleanly) or returns the same blocker.
            self.answerer.apply(answer, ledger, question=question_for_record)

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
                # In-memory ledger has the unsynced answer applied, but
                # ``state.ledger`` on disk does NOT (deferred persistence
                # above). Persist only the blocker context; the next resume
                # re-computes the answer from the pre-answer ledger.
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

            # Backend acknowledged the round — NOW safe to persist all the
            # round's effects in a single ``self._save(state)`` flush. The
            # ledger and backend transcript are guaranteed mirrored at this
            # point, so the next iteration's ledger-primary closure gate
            # can safely short-circuit close.
            state.current_round = round_number
            state.ledger = ledger.to_dict()
            state.interview_session_id = turn.session_id
            state.pending_question = turn.question
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

        # max_rounds exhausted — one final ledger-primary closure check, then
        # safe-default finalization for autonomous ``ooo auto``.  The manual
        # ``ooo interview`` path remains strict/fail-closed in its own driver;
        # this auto driver is allowed to close missing/weak required sections
        # when the goal is local, reversible, and covered by the audited
        # safe-default policy.  This is intentionally after the bounded
        # interview loop: explicit/backend-provided answers always win first,
        # and defaults are only the final escape from a benign stalled dialog.
        #
        # The final ledger-primary check catches the case where the last
        # round's apply / backend exchange filled the ledger after the
        # top-of-loop ledger check fired False for round ``max_rounds``.
        backend_done = turn.seed_ready or turn.completed
        ledger_done = ledger.is_seed_ready()
        if ledger_done:
            closure_mode = "mutual_agreement" if backend_done else "ledger_only"
            if not backend_done:
                log.info(
                    "auto.interview.ledger_only_closure",
                    auto_session_id=state.auto_session_id,
                    round_number=self.max_rounds,
                    ambiguity_score=turn.ambiguity_score,
                    interview_session_id=state.interview_session_id,
                )
            state.pending_question = None
            state.interview_completed = True
            state.interview_closure_mode = closure_mode
            state.mark_progress(
                f"interview closed via {closure_mode} at max_rounds="
                f"{self.max_rounds} (backend_ambiguity={turn.ambiguity_score})",
                tool_name="interview_driver",
            )
            self._save(state)
            return AutoInterviewResult(
                "seed_ready", state.interview_session_id, ledger, self.max_rounds
            )

        open_gaps = ledger.open_gaps()
        log.info(
            "auto.interview.safe_default.entered",
            auto_session_id=state.auto_session_id,
            backend_done=backend_done,
            ledger_done=ledger_done,
            open_gaps=list(open_gaps),
            ambiguity_score=turn.ambiguity_score,
            max_rounds=self.max_rounds,
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
        else:
            log.info(
                "auto.interview.safe_default.skipped_backend_done",
                auto_session_id=state.auto_session_id,
                open_gaps=list(open_gaps),
            )

        if finalization is not None:
            if not finalization.defaulted_sections and not finalization.unsafe_gaps:
                log.info(
                    "auto.interview.safe_default.no_gaps_to_default",
                    auto_session_id=state.auto_session_id,
                    ledger_done=ledger_done,
                )
            if finalization.unsafe_gaps:
                log.info(
                    "auto.interview.safe_default.unsafe_context_observed",
                    auto_session_id=state.auto_session_id,
                    unsafe_gaps=finalization.unsafe_gaps,
                )
                # Issue #1248 — escalate the matcher fire through a
                # lateral persona before letting safe-default closure
                # die in place. ``self.lateral_thinker`` is wired only
                # when the runtime constructed one (MCP complete-product
                # path); when ``None``, the existing BLOCKED branch
                # downstream still applies, preserving pre-issue
                # behaviour for runtime contexts that do not yet have a
                # lateral handler.
                if self.lateral_thinker is not None:
                    finalization = await self._escalate_safe_default_unsafe_context(
                        state,
                        ledger,
                        finalization,
                        pending_question=turn.question,
                    )

        if finalization is not None and finalization.completed and ledger.is_seed_ready():
            synthesis = build_safe_default_synthesis(finalization)
            synthesis_pushed = False
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
                    synthesis_pushed = True
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
                        synthesis_pushed=False,
                    )
                    state.ledger = ledger.to_dict()
                    state.mark_blocked(
                        blocker,
                        tool_name="interview.safe_default_synthesis",
                        error_code=INTERVIEW_SAFE_DEFAULT_SYNTHESIS_STOP_REASON_CODE,
                    )
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
                    log.warning(
                        "auto.interview.safe_default_synthesis_nonclosure",
                        auto_session_id=state.auto_session_id,
                        interview_session_id=state.interview_session_id,
                        defaulted_sections=finalization.defaulted_sections,
                        backend_seed_ready=bool(synthesis_turn.seed_ready),
                        backend_completed=bool(synthesis_turn.completed),
                        synthesis_pushed=synthesis_pushed,
                    )
                    state.ledger = ledger.to_dict()
                    state.mark_blocked(
                        blocker,
                        tool_name="interview.safe_default_synthesis",
                        error_code=INTERVIEW_SAFE_DEFAULT_SYNTHESIS_STOP_REASON_CODE,
                    )
                    record_authoring_backend(state)
                    self._save(state)
                    return AutoInterviewResult(
                        "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
                    )
            log.info(
                "auto.interview.safe_default.closed",
                auto_session_id=state.auto_session_id,
                defaulted_sections=finalization.defaulted_sections,
                synthesis_pushed=synthesis_pushed,
            )
            state.ledger = ledger.to_dict()
            state.pending_question = None
            state.interview_completed = True
            # PR-B2 / #821: tag the envelope so callers can distinguish a
            # safe-default-applied closure from a backend-confirmed close
            # (``mutual_agreement``) and from PR-B1's ``ledger_only`` path.
            state.interview_closure_mode = "safe_default"
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
        # NOTE: PR-B1 (#1148) previously placed a ``ledger_only`` fallback
        # here for the ``ledger_done and not backend_done`` case at
        # max_rounds. That fallback is now unreachable — the ledger-primary
        # check at the top of this max_rounds block (and at the top of each
        # in-loop iteration) closes any ``ledger_done`` case before we get
        # here, regardless of backend state. See SSOT #1157 "Closure Policy"
        # section (formalized 2026-05-27) for the design rationale.
        # PR-B2 / #821: partial safe-default closure — some required gaps were
        # safely defaultable, but at least one remained unsafe at max_rounds.
        # Distinguish from the generic "nothing was defaultable" path with a
        # dedicated structured event and a typed stop_reason_code so callers
        # can resume with the unsafe gap context surfaced. Roll back the
        # partial defaults because synthesis was never pushed to the backend
        # transcript (same invariant as the synthesis-failure rollback above):
        # leaving entries in the ledger that the persisted interview does not
        # mirror would diverge on resume.
        if (
            finalization is not None
            and finalization.defaulted_sections
            and finalization.unsafe_gaps
        ):
            log.info(
                "auto.interview.safe_default_partial_unsafe_gaps",
                auto_session_id=state.auto_session_id,
                defaulted_sections=finalization.defaulted_sections,
                unsafe_gaps=finalization.unsafe_gaps,
                ambiguity_score=turn.ambiguity_score,
                interview_session_id=state.interview_session_id,
            )
            _revert_safe_default_entries(ledger, finalization.defaulted_sections)
            blocker = (
                f"auto interview reached max_rounds={self.max_rounds} with "
                f"partial safe-default closure (rolled back): "
                f"defaultable={list(finalization.defaulted_sections)}, "
                f"unsafe_remaining={list(finalization.unsafe_gaps)}"
            )
            state.ledger = ledger.to_dict()
            # Issue #1248 — when the lateral escalation chain exhausted
            # the available personas before reaching this branch it has
            # already stamped ``UNSTUCK_EXHAUSTED_STOP_REASON_CODE`` on
            # ``state.last_error_code``. Surface that typed L5 terminal
            # instead of the generic ``interview_unsafe_gaps_remain``
            # so the result envelope distinguishes "tried lateral and
            # failed" from "lateral never engaged".
            error_code = (
                UNSTUCK_EXHAUSTED_STOP_REASON_CODE
                if state.last_error_code == UNSTUCK_EXHAUSTED_STOP_REASON_CODE
                else "interview_unsafe_gaps_remain"
            )
            state.mark_blocked(
                blocker,
                tool_name="interview_driver",
                error_code=error_code,
            )
            record_authoring_backend(state)
            self._save(state)
            return AutoInterviewResult(
                "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
            )

        # Defensive last resort: ledger is not done, safe-default could not
        # close, no unsafe-gap partial-rollback fired. Under the ledger-primary
        # policy this means every required section was probed and the
        # ``finalize_safe_defaultable_gaps`` policy found nothing safe to fill.
        # Surface as a typed BLOCKED so callers can resume / sharpen the goal.
        # ``ledger_done`` is guaranteed False here (otherwise the top
        # max_rounds check above would have returned ``seed_ready``).
        blocker = (
            f"auto interview reached max_rounds={self.max_rounds} without closure: "
            f"backend_done={backend_done} ({ambiguity_part}), "
            f"ledger_done={ledger_done} ({gaps_part})"
        )
        # Issue #1248 — same UNSTUCK_EXHAUSTED override as the
        # partial-unsafe BLOCKED branch above. The lateral escalation
        # may have run with no defaulted sections (only unsafe_gaps),
        # in which case the flow lands here after exhausting personas.
        error_code = (
            UNSTUCK_EXHAUSTED_STOP_REASON_CODE
            if state.last_error_code == UNSTUCK_EXHAUSTED_STOP_REASON_CODE
            else "interview_max_rounds_exhausted"
        )
        state.mark_blocked(
            blocker,
            tool_name="interview_driver",
            error_code=error_code,
        )
        record_authoring_backend(state)
        self._save(state)
        return AutoInterviewResult(
            "blocked", state.interview_session_id, ledger, self.max_rounds, blocker
        )

    def _try_close_after_backend_start_failure(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        exc: Exception,
    ) -> AutoInterviewResult | None:
        """Close from deterministic ledger evidence when backend start is unavailable.

        A start-time provider/config failure means the LLM interview never
        produced a question, but it does not invalidate facts already present
        in the initial goal or facts that the audited safe-default policy can
        fill for benign local tasks. When those facts make the ledger complete,
        proceed without a persisted interview transcript; the pipeline's ledger
        Seed generator owns the next phase.
        """
        if not _is_authoring_backend_unavailable(exc):
            return None
        closure_mode = "ledger_only_no_backend"
        if not ledger.is_seed_ready():
            finalization = finalize_safe_defaultable_gaps(
                ledger,
                goal=state.goal,
                provenance=f"auto interview backend start failed: {exc}",
                pending_question=None,
                active_profile=getattr(self.answerer, "active_profile", None),
            )
            if finalization is None or not finalization.completed or not ledger.is_seed_ready():
                return None
            closure_mode = "safe_default_no_backend"

        state.ledger = ledger.to_dict()
        state.pending_question = None
        state.interview_completed = True
        state.interview_closure_mode = closure_mode
        state.mark_progress(
            f"interview closed via {closure_mode} after backend start failure",
            tool_name="interview_driver",
        )
        log.warning(
            "auto.interview.backend_start_failed_ledger_fallback",
            auto_session_id=state.auto_session_id,
            closure_mode=closure_mode,
            error=str(exc),
        )
        self._save(state)
        return AutoInterviewResult(
            "seed_ready", state.interview_session_id, ledger, state.current_round
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

    async def _escalate_safe_default_unsafe_context(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        finalization: SafeDefaultFinalization,
        *,
        pending_question: str,
    ) -> SafeDefaultFinalization:
        """Walk the lateral persona chain to clear a safe-default matcher fire.

        Issue #1248 — when ``_unsafe_context_reason`` fires during a
        safe-default closure attempt, the SSOT #1157 L5 invariant
        requires escalation through bounded lateral persona reframes
        before falling to BLOCKED. ``self.lateral_thinker`` is the same
        callable shape the EVALUATE → UNSTUCK_LATERAL path uses, so the
        reframe inherits its timeout / error / persona-tracking
        contract.

        Behaviour:

        * Pick the next persona via
          :func:`select_persona_for_safe_default_block`, honoring
          ``state.personas_invoked``.
        * Invoke the lateral thinker once.

          - On **chain exhaustion** (selector returns ``None``), stamp
            the typed ``unstuck_exhausted`` reason on
            ``state.last_error_code`` and return the original
            finalization. The caller's BLOCKED branch then surfaces the
            typed terminal.
          - On **timeout** / **handler exception** / **transient
            ``LateralResult.error``**, record the persona attempt for
            audit + chain progression but leave ``state.last_error_code``
            alone so the existing ``interview_unsafe_gaps_remain`` /
            ``interview_max_rounds_exhausted`` code applies. Only the
            chain-exhausted path stamps ``unstuck_exhausted``.

        * On a successful lateral response, persist the persona's text
          on state for audit (``last_lateral_*``), append the persona
          to ``personas_invoked``, and — only if the response carries
          the canonical ``CLEARANCE: lexical_false_positive`` marker
          (see :func:`_lateral_response_authorizes_demotion`) — demote
          every active CONSERVATIVE_DEFAULT ledger entry to ASSUMPTION
          and snapshot the mutated ledger onto ``state.ledger`` before
          checkpointing so a resume after demotion sees the same input
          the runtime path saw. The re-run of
          :func:`finalize_safe_defaultable_gaps` then either clears
          ``unsafe_gaps`` (returning the new finalization) or surfaces
          the same matcher fire to the next persona.

        The function only mutates the caller's path of execution; the
        existing safe-default closure / BLOCKED branches downstream are
        unchanged. When ``self.lateral_thinker is None`` the caller is
        expected to short-circuit before invoking this method.
        """
        assert self.lateral_thinker is not None  # noqa: S101 - caller guards

        while finalization.unsafe_gaps:
            already_tried = tuple(ThinkingPersona(value) for value in state.personas_invoked)
            persona = select_persona_for_safe_default_block(already_tried_personas=already_tried)
            if persona is None:
                log.warning(
                    "auto.interview.safe_default.lateral_chain_exhausted",
                    auto_session_id=state.auto_session_id,
                    personas_invoked=tuple(state.personas_invoked),
                    unsafe_gaps=finalization.unsafe_gaps,
                )
                state.last_error_code = UNSTUCK_EXHAUSTED_STOP_REASON_CODE
                self._save(state)
                return finalization

            qa_differences = tuple(finalization.unsafe_gaps)
            qa_suggestions = (
                "Disambiguate: is this matcher fire a lexical false "
                "positive (e.g. 'contract' inside 'acceptance contract', "
                "'license: MIT', 'compliance check') or does the "
                "surrounding ledger context assert genuine unsafe "
                "scope (production deploys, credential handling, "
                "legal/medical adjudication, etc.)? The auto pipeline "
                "will demote active conservative_default ledger entries "
                "to assumption ONLY when your response includes the "
                "exact line `CLEARANCE: lexical_false_positive` on its "
                "own line. Without that marker the matcher input is "
                "left untouched and the safe-default closure stays "
                "blocked — this is the safety default. If you observe "
                "genuinely unsafe scope, do NOT emit the clearance "
                "marker; instead emit `UNSAFE_CONFIRMED: <one-line "
                "reason>` so the audit trail records why the matcher "
                "fire was not cleared.",
            )
            run_artifact = _build_safe_default_lateral_artifact(ledger, finalization)

            log.info(
                "auto.interview.safe_default.lateral_invoked",
                auto_session_id=state.auto_session_id,
                persona=persona.value,
                unsafe_gaps=qa_differences,
            )
            try:
                lateral_result = await asyncio.wait_for(
                    self.lateral_thinker(
                        persona=persona,
                        qa_differences=qa_differences,
                        qa_suggestions=qa_suggestions,
                        run_artifact=run_artifact,
                    ),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError:
                log.warning(
                    "auto.interview.safe_default.lateral_timeout",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    timeout_seconds=self.timeout_seconds,
                )
                if persona.value not in state.personas_invoked:
                    state.personas_invoked.append(persona.value)
                self._save(state)
                return finalization
            except Exception as exc:  # noqa: BLE001 - any handler error => terminal
                log.warning(
                    "auto.interview.safe_default.lateral_invocation_failed",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    error=str(exc),
                )
                if persona.value not in state.personas_invoked:
                    state.personas_invoked.append(persona.value)
                self._save(state)
                return finalization

            # Track the persona regardless of payload outcome so the
            # next attempt picks a different angle. Mirrors the
            # EVALUATE-side append-then-evaluate ordering.
            if persona.value not in state.personas_invoked:
                state.personas_invoked.append(persona.value)

            if lateral_result.error:
                log.warning(
                    "auto.interview.safe_default.lateral_transient_error",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    error=lateral_result.error,
                )
                self._save(state)
                return finalization

            state.last_lateral_persona = lateral_result.persona or persona.value
            state.last_lateral_approach_summary = lateral_result.approach_summary
            state.last_lateral_text = lateral_result.text

            # Demotion is gated on a machine-checkable clearance marker
            # (see ``_LATERAL_CLEARANCE_MARKER_RE``). A bare prompt-only
            # response from the inline ``ouroboros_lateral_think`` path
            # — which is what the production auto pipeline gets today
            # — does NOT authorize reclassifying ledger entries that may
            # record genuinely unsafe scope. The persona invocation is
            # still recorded above for audit; without clearance the
            # loop falls through to the next persona or exhausts.
            if _lateral_response_authorizes_demotion(lateral_result.text):
                demoted = _demote_conservative_defaults_for_safe_default(ledger)
                log.info(
                    "auto.interview.safe_default.lateral_demoted_entries",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    demoted_entry_count=demoted,
                )
                # The lateral audit (last_lateral_*, personas_invoked) and the
                # ledger mutation must persist atomically: ``AutoStore.save``
                # only writes ``state.to_dict()``, so without this snapshot a
                # crash between the checkpoint here and the next ledger sync
                # later in ``run()`` would replay with the persona "spent" but
                # the matcher input un-demoted — the matcher would re-fire on
                # resume with no fresh persona to clear it. Snapshot the
                # mutated ledger before saving so resume sees what runtime saw.
                state.ledger = ledger.to_dict()
            else:
                log.info(
                    "auto.interview.safe_default.lateral_no_clearance",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    has_text=bool(lateral_result.text),
                )
            self._save(state)

            finalization = finalize_safe_defaultable_gaps(
                ledger,
                goal=state.goal,
                provenance=(
                    f"auto interview max_rounds={self.max_rounds} after lateral={persona.value}"
                ),
                pending_question=pending_question,
                active_profile=getattr(self.answerer, "active_profile", None),
            )
            if not finalization.unsafe_gaps:
                log.info(
                    "auto.interview.safe_default.lateral_resolved",
                    auto_session_id=state.auto_session_id,
                    persona=persona.value,
                    defaulted_sections=finalization.defaulted_sections,
                )
                return finalization

            log.info(
                "auto.interview.safe_default.lateral_retry_required",
                auto_session_id=state.auto_session_id,
                persona=persona.value,
                remaining_unsafe_gaps=finalization.unsafe_gaps,
            )

        return finalization

    async def _try_close_backend_ready_safe_defaults(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        turn: InterviewTurn,
    ) -> AutoInterviewResult | None:
        """Close benign remaining gaps after a low-ambiguity backend completion.

        Backend completion alone is not enough to close auto interviews: the
        ledger still owns structural readiness. When the backend also reports a
        concrete ambiguity score at the seed threshold, any remaining
        missing/weak sections are safe to route through the same audited
        safe-default finalizer used at max_rounds. Unsafe/conflicting gaps keep
        the existing gap-reopen path.
        """
        if turn.ambiguity_score is None or turn.ambiguity_score > BACKEND_READY_AMBIGUITY_THRESHOLD:
            return None
        if ledger.is_seed_ready():
            return None

        finalization = finalize_safe_defaultable_gaps(
            ledger,
            goal=state.goal,
            provenance=(
                "backend readiness "
                f"ambiguity_score={turn.ambiguity_score:.2f} "
                f"round={state.current_round}"
            ),
            pending_question=turn.question,
            active_profile=getattr(self.answerer, "active_profile", None),
        )
        if not finalization.completed or not ledger.is_seed_ready():
            _revert_safe_default_entries(ledger, finalization.defaulted_sections)
            log.info(
                "auto.interview.backend_ready_safe_default.skipped",
                auto_session_id=state.auto_session_id,
                ambiguity_score=turn.ambiguity_score,
                defaulted_sections=finalization.defaulted_sections,
                unsafe_gaps=finalization.unsafe_gaps,
                open_gaps=ledger.open_gaps(),
            )
            return None

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
                                f"backend_ambiguity={turn.ambiguity_score:.2f}]"
                            ),
                        ),
                        state,
                        tool_name="interview.backend_ready_safe_default_synthesis",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - keep ledger/transcript in sync
                _revert_safe_default_entries(ledger, finalization.defaulted_sections)
                blocker = (
                    "backend-ready safe-default synthesis transcript sync failed; "
                    f"rolled back defaulted sections: {', '.join(finalization.defaulted_sections)}; "
                    f"error={exc}"
                )
                state.ledger = ledger.to_dict()
                state.mark_blocked(
                    blocker,
                    tool_name="interview.backend_ready_safe_default_synthesis",
                    error_code=INTERVIEW_SAFE_DEFAULT_SYNTHESIS_STOP_REASON_CODE,
                )
                record_authoring_backend(state)
                self._save(state)
                return AutoInterviewResult(
                    "blocked", state.interview_session_id, ledger, state.current_round, blocker
                )
            state.interview_session_id = synthesis_turn.session_id
            state.pending_question = synthesis_turn.question
            if not (synthesis_turn.seed_ready or synthesis_turn.completed):
                _revert_safe_default_entries(ledger, finalization.defaulted_sections)
                blocker = (
                    "backend-ready safe-default synthesis did not close the persisted interview: "
                    "ledger defaults rolled back"
                )
                state.ledger = ledger.to_dict()
                state.mark_blocked(
                    blocker,
                    tool_name="interview.backend_ready_safe_default_synthesis",
                    error_code=INTERVIEW_SAFE_DEFAULT_SYNTHESIS_STOP_REASON_CODE,
                )
                record_authoring_backend(state)
                self._save(state)
                return AutoInterviewResult(
                    "blocked", state.interview_session_id, ledger, state.current_round, blocker
                )

        log.info(
            "auto.interview.backend_ready_safe_default.closed",
            auto_session_id=state.auto_session_id,
            ambiguity_score=turn.ambiguity_score,
            defaulted_sections=finalization.defaulted_sections,
        )
        state.ledger = ledger.to_dict()
        state.pending_question = None
        state.interview_completed = True
        state.interview_closure_mode = "safe_default"
        state.mark_progress(
            "backend-ready safe-default finalization closed interview gaps: "
            + ", ".join(finalization.defaulted_sections),
            tool_name="interview_driver",
        )
        self._save(state)
        return AutoInterviewResult("seed_ready", state.interview_session_id, ledger, state.current_round)

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


def _demote_conservative_defaults_for_safe_default(ledger: SeedDraftLedger) -> int:
    """Demote active CONSERVATIVE_DEFAULT ledger entries to ASSUMPTION source.

    Issue #1248 — after a lateral persona has been invoked to review a
    matcher fire on the safe-default unsafe-context gate, treat every
    active ``conservative_default`` entry the auto-answerer wrote as
    boundary text (matching the docstring intent of ``ASSUMPTION``)
    rather than as active scope. ``ASSUMPTION`` is in
    ``_SKIP_SOURCES_FOR_UNSAFE_GATE`` (see ``safe_defaults.py``), so a
    subsequent ``finalize_safe_defaultable_gaps`` pass will not re-fire
    the matcher on these entries.

    Demotion is the lateral persona's authoritative effect on the
    ledger: we asked the system to judge, and the audit trail of that
    invocation (``state.last_lateral_*`` fields) is the record that
    justifies treating boundary text as boundary. Inactive entries are
    untouched — they are already excluded from the gate by status.

    Returns the count of demoted entries so the caller can log
    observable evidence of the lateral reframe.
    """
    inactive_statuses: frozenset[LedgerStatus] = frozenset(
        {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    )
    count = 0
    for section in ledger.sections.values():
        for entry in section.entries:
            if entry.status in inactive_statuses:
                continue
            if entry.source != LedgerSource.CONSERVATIVE_DEFAULT:
                continue
            entry.source = LedgerSource.ASSUMPTION
            count += 1
    return count


def _build_safe_default_lateral_artifact(
    ledger: SeedDraftLedger, finalization: SafeDefaultFinalization
) -> str:
    """Compact ledger projection passed as ``run_artifact`` to the lateral persona.

    The lateral handler's contract expects an opaque text blob describing
    the current approach (originally the Run artifact for an EVALUATE-side
    persona; here, the snapshot of the ledger state that triggered the
    matcher fire). We surface the unsafe-gap labels and a per-section
    summary of active ledger entries so the persona can see *which*
    boundary text the matcher reacted to without us pre-deciding whether
    each entry is benign or genuinely unsafe.
    """
    lines: list[str] = [
        "Safe-default unsafe-context matcher fired during auto interview closure.",
        f"Unsafe gaps reported: {', '.join(finalization.unsafe_gaps)}",
        "Active ledger entries (source-tagged):",
    ]
    inactive_statuses: frozenset[LedgerStatus] = frozenset(
        {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    )
    for section_name, section in ledger.sections.items():
        for entry in section.entries:
            if entry.status in inactive_statuses:
                continue
            truncated = entry.value if len(entry.value) <= 200 else f"{entry.value[:200]}…"
            lines.append(f"  - [{section_name}|{entry.source}] {truncated}")
    return "\n".join(lines)


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


def _is_authoring_backend_unavailable(exc: Exception) -> bool:
    """Return True for provider/config failures that deterministic auto can bypass."""
    text = str(exc).casefold()
    markers = (
        "config.toml",
        "profile",
        "codex",
        "provider",
        "timed out",
        "timeout",
        "api key",
        "authentication",
        "rate limit",
        "connection refused",
        "network",
    )
    return any(marker in text for marker in markers)


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
