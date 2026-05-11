"""Full-quality AutoPipeline supervisor skeleton."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
import inspect
import threading
import time
from typing import Any, Protocol

from ouroboros.auto.blocker_attribution import record_authoring_backend
from ouroboros.auto.grading import GradeGate, deterministic_floor
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.listeners import RALPH_CANCEL_BLOCKER_REASON
from ouroboros.auto.progress import AutoProgressCallback, AutoProgressEvent
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
    SeedOrigin,
    utc_now_iso,
)
from ouroboros.core.seed import Seed

SeedGenerator = Callable[[str], Awaitable[Seed]]


class RunStarter(Protocol):
    """Protocol for run-starter callables.

    Implementations accept an optional ``idempotency_key`` so the auto
    pipeline can safely retry a single run-start attempt without enqueuing
    a duplicate execution server-side. The key is populated from
    ``state.auto_session_id`` by ``AutoPipeline.run``.
    """

    async def __call__(self, seed: Seed, *, idempotency_key: str = "") -> dict[str, Any]: ...


RalphStarter = Callable[..., Awaitable[dict[str, Any]]]
SeedSaver = Callable[[Seed], str]
SeedLoader = Callable[[str], Seed]

# Ralph stop_reason values that map to a recoverable BLOCKED auto phase
# rather than a hard FAILED. Pinned by Q00/ouroboros#773 and asserted by
# tests/unit/auto/test_pipeline_ralph_handoff.py so silent drift surfaces
# as test failure.
_RALPH_BLOCKED_STOP_REASONS: frozenset[str] = frozenset(
    {
        "iteration_timeout",
        "wall_clock_exhausted",
        "oscillation_detected",
        "grade_regressing",
        "max_generations reached",
    }
)

# Tool-name marker recorded on ``state.last_tool_name`` whenever the top-level
# pipeline deadline (#779) trips. Distinct from per-phase tool names so that
# recovery decisions and surfaces can detect "deadline-expired" vs ordinary
# per-tool blockers without scanning the error message.
PIPELINE_DEADLINE_TOOL_NAME = "pipeline_deadline"
_RESUME_EXPIRED_MESSAGE = "pipeline_timeout (deadline expired before resume)"
# Mirrors RalphHandler.MIN_MAX_TOTAL_SECONDS. The auto layer checks this before
# dispatch so an insufficient top-level pipeline budget remains a pipeline
# timeout, not a Ralph argument-validation failure.
_MIN_RALPH_MAX_TOTAL_SECONDS = 1.0
# Mirrors RalphHandler.MIN_PER_ITERATION_TIMEOUT_SECONDS / DEFAULT_PER_ITERATION_TIMEOUT_SECONDS.
# When the remaining pipeline budget is shorter than the Ralph default
# per-iteration timeout, the auto layer caps ``per_iteration_timeout_seconds``
# so a single ``evolve_step`` cannot block past ``deadline_at``. RalphLoopRunner
# only checks ``max_total_seconds`` at iteration boundaries, so without this cap
# the first iteration could still run for the full default 1800s — violating
# the top-level pipeline deadline contract pinned by Q00/ouroboros#779.
_MIN_RALPH_PER_ITERATION_SECONDS = 30.0
_DEFAULT_RALPH_PER_ITERATION_SECONDS = 1800.0


_RETRY_GUIDANCE_PHRASE = "retried once with idempotency key"


@dataclass(frozen=True, slots=True)
class AutoPipelineResult:
    """Structured AutoPipeline result for CLI/MCP surfaces."""

    status: str
    auto_session_id: str
    phase: str
    grade: str | None = None
    seed_path: str | None = None
    seed_origin: str = SeedOrigin.NONE.value
    interview_session_id: str | None = None
    execution_id: str | None = None
    job_id: str | None = None
    run_session_id: str | None = None
    run_subagent: dict[str, Any] | None = None
    current_round: int = 0
    pending_question: str | None = None
    last_progress_message: str | None = None
    last_progress_at: str | None = None
    last_grade: str | None = None
    run_handoff_status: str | None = None
    run_handoff_guidance: str | None = None
    attached_run_handle: str | None = None
    attached_run_source: str | None = None
    attached_at: str | None = None
    run_reconciliation_status: str | None = None
    run_reconciliation_source: str | None = None
    run_reconciled_at: str | None = None
    ralph_job_id: str | None = None
    ralph_lineage_id: str | None = None
    ralph_dispatch_mode: str | None = None
    assumptions: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    blocker: str | None = None
    runtime_backend: str | None = None
    opencode_mode: str | None = None
    invoked_by: str = "direct"
    provenance: dict[str, Any] | None = None
    last_authoring_backend: str | None = None
    resume_capability: AutoResumeCapability = AutoResumeCapability.RESUME
    """Typed :class:`AutoResumeCapability` value. Defaults to
    :attr:`AutoResumeCapability.RESUME` so existing test constructions of
    ``AutoPipelineResult(...)`` keep their historical behavior.
    ``AutoPipeline._result()`` overrides it from the persisted state's
    :meth:`AutoPipelineState.resume_capability`."""
    ledger_provenance: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence_backed_sections: tuple[str, ...] = ()
    assumption_only_sections: tuple[str, ...] = ()


@dataclass(slots=True)
class AutoPipeline:
    """Coordinate interview, Seed generation, review, repair, and run handoff."""

    interview_driver: AutoInterviewDriver
    seed_generator: SeedGenerator
    run_starter: RunStarter | None = None
    store: AutoStore | None = None
    reviewer: SeedReviewer | None = None
    repairer: SeedRepairer | None = None
    grade_gate: GradeGate | None = None
    seed_saver: SeedSaver | None = None
    seed_loader: SeedLoader | None = None
    skip_run: bool = False
    attach_execution_id: str | None = None
    attach_job_id: str | None = None
    attach_run_session_id: str | None = None
    attach_source: str | None = None
    reconcile_run: bool = False
    reconcile_source: str | None = None
    seed_timeout_seconds: float = 120.0
    run_start_timeout_seconds: float = 60.0
    progress_callback: AutoProgressCallback | None = None
    # Q00/ouroboros#773: chain RUN → RALPH_HANDOFF when ``complete_product``
    # is true and a ``ralph_starter`` is configured. ``complete_product``
    # defaults to False (opt-in safety) so existing callers see no behavior
    # change.
    ralph_starter: RalphStarter | None = None
    # Q00/ouroboros#773 (review-5): poller invoked by ``_resume_ralph_handoff``
    # to translate a persisted ``ralph_job_id`` back into a terminal status
    # dict so a session interrupted in ``RALPH_HANDOFF`` (e.g. MCP client
    # disconnects while the background Ralph job keeps running) actually
    # reconciles to ``COMPLETE`` / ``BLOCKED`` / ``FAILED`` on resume instead
    # of staying stranded in the non-terminal handoff state. Defaults to
    # None for backward compatibility — when unset, resume falls back to
    # the legacy guidance-only behavior.
    ralph_resumer: RalphStarter | None = None
    complete_product: bool = False
    _last_emitted_phase: str | None = field(default=None, init=False, repr=False)
    _last_emitted_grade: str | None = field(default=None, init=False, repr=False)
    _last_emitted_repair: int | None = field(default=None, init=False, repr=False)

    async def run(self, state: AutoPipelineState) -> AutoPipelineResult:
        """Run a bounded auto pipeline using injected side-effecting dependencies."""
        self._last_emitted_phase = None
        self._last_emitted_grade = None
        self._last_emitted_repair = None
        # Push the same progress callback down into the interview driver so
        # the longest-running phase (auto interview rounds) emits live
        # snapshots through the same observer contract instead of forcing
        # consumers to scrape persisted state for per-round updates.
        self.interview_driver.progress_callback = self.progress_callback
        ledger = (
            SeedDraftLedger.from_dict(state.ledger)
            if state.ledger
            else SeedDraftLedger.from_goal(state.goal)
        )
        if self.skip_run and not state.skip_run:
            state.skip_run = True
        # Q00/ouroboros#773 (review-3): ``complete_product`` is durable session
        # intent, not a per-invocation flag. On a fresh session the constructor
        # writes the operator's choice into the state; on resume we honor the
        # persisted value even when the caller forgot to re-pass the flag, so
        # a session originally started with ``--complete-product`` keeps
        # chaining RUN → RALPH_HANDOFF after restart. Lowering is intentional:
        # explicit ``complete_product=True`` raises the bound; absence keeps
        # the persisted truth.
        if self.complete_product and not state.complete_product:
            state.complete_product = True
        elif state.complete_product and not self.complete_product:
            self.complete_product = True
        if state.phase is AutoPhase.RALPH_HANDOFF and not state.complete_product:
            state.complete_product = True
            self.complete_product = True
        # Validate the persisted Seed artifact BEFORE any other path can
        # trigger a state-validating save. ``AutoStore.save`` re-validates the
        # full state, so a malformed ``seed_artifact`` would otherwise raise a
        # raw ``ValueError`` from the very first save (e.g. the legacy
        # deadline-arm or resume-expired branches below) instead of being
        # converted into a clean ``FAILED`` outcome by
        # ``_mark_invalid_seed_artifact``.
        if state.seed_artifact:
            try:
                Seed.from_dict(state.seed_artifact)
            except Exception as exc:
                _mark_invalid_seed_artifact(state, f"persisted Seed artifact is invalid: {exc}")
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
            # Backfill legacy resumed sessions: pre-PR auto pipelines were the
            # only writer of state.seed_artifact, so a valid persisted Seed
            # paired with seed_origin=none can only have come from this
            # pipeline. Inferring it once on resume keeps the new contract
            # accurate for sessions created before this field existed.
            if state.seed_origin is SeedOrigin.NONE:
                state.seed_origin = SeedOrigin.AUTO_PIPELINE
        _arm_legacy_missing_deadline(state)
        # Top-level deadline check on resume (#779). When ``deadline_at`` is
        # already set and has passed before this process even starts work,
        # immediately transition to BLOCKED so no phase work is invoked. The
        # message is the literal one the issue contract requires so external
        # surfaces can distinguish a resume-expired session from a freshly
        # tripped deadline mid-run.
        if (
            state.deadline_at is not None
            and not state.is_terminal()
            and state.is_deadline_expired()
        ):
            state.last_tool_name = PIPELINE_DEADLINE_TOOL_NAME
            state.mark_blocked(
                _RESUME_EXPIRED_MESSAGE,
                tool_name=PIPELINE_DEADLINE_TOOL_NAME,
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        resume_tool_name = state.last_tool_name
        self._save(state)

        if self.reconcile_run and state.phase == AutoPhase.COMPLETE:
            reconciled, transient_blocker = self._reconcile_run_if_requested(state)
            if reconciled is not None:
                self._save(state)
                if reconciled is False:
                    blocker = transient_blocker or state.last_error
                else:
                    blocker = None
                status_override = "blocked" if reconciled is False else None
                return self._result(
                    state,
                    ledger,
                    blocker=blocker,
                    status_override=status_override,
                )
        if state.phase == AutoPhase.COMPLETE:
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase in {AutoPhase.BLOCKED, AutoPhase.FAILED}:
            resume_phase = _recoverable_phase_for_tool(state.last_tool_name)
            if resume_phase is None:
                return self._result(state, ledger, blocker=state.last_error)
            previous_phase = state.phase
            state.recover(
                resume_phase,
                f"resuming {resume_phase.value} after {previous_phase.value}: {state.last_error or 'no error recorded'}",
            )
            # Legacy auto sessions saved before #779 had no
            # ``deadline_at_epoch``, and ``from_dict()`` deliberately leaves
            # the deadline unset for terminal phases. After recovering them
            # back to a working phase, arm the deadline so subsequent
            # ``_enforce_deadline`` checks are not silent no-ops for the
            # rest of this resume (#790 review-4). ``arm_deadline`` is
            # idempotent — non-legacy resumes are unaffected.
            state.arm_deadline()
            self._save(state)

        review: SeedReview | None = None
        if self._enforce_deadline(state):
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase in {AutoPhase.CREATED, AutoPhase.INTERVIEW}:
            # Arm the top-level pipeline deadline (#779) on the first
            # CREATED → INTERVIEW transition so every later phase entry can
            # compare ``time.monotonic()`` against a stable absolute target.
            # Idempotent for resumed sessions whose deadline already armed.
            # Persist immediately so a crash during the first
            # ``interview_driver.run()`` cannot leave the saved state
            # without ``deadline_at_epoch`` — otherwise a resumed session
            # would silently extend the pipeline by re-arming a fresh 2h
            # window and break the "preserved across process restarts"
            # contract (#790 review-5).
            if state.phase == AutoPhase.CREATED:
                state.arm_deadline()
                self._save(state)
            if state.phase == AutoPhase.INTERVIEW and state.interview_completed:
                if not state.interview_session_id:
                    state.mark_blocked(
                        "Completed interview is missing interview_session_id",
                        tool_name="auto_pipeline",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                if not ledger.is_seed_ready():
                    gaps = ", ".join(ledger.open_gaps())
                    state.mark_blocked(
                        f"Completed interview has unresolved ledger gaps: {gaps}",
                        tool_name="auto_pipeline",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                state.transition(
                    AutoPhase.SEED_GENERATION, "resuming Seed generation after completed interview"
                )
                self._save(state)
            else:
                interview_phase_timeout = state.phase_timeout_seconds(AutoPhase.INTERVIEW)
                interview_timeout = self._deadline_capped_timeout(state, interview_phase_timeout)
                try:
                    interview = await asyncio.wait_for(
                        self.interview_driver.run(state, ledger),
                        timeout=interview_timeout,
                    )
                except TimeoutError:
                    if self._enforce_deadline(state):
                        return self._result(state, ledger, blocker=state.last_error)
                    state.mark_blocked(
                        f"interview phase exceeded {interview_phase_timeout:.0f}s",
                        tool_name="interview_driver",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                if interview.status == "blocked":
                    return self._result(state, ledger, blocker=interview.blocker)
                state.interview_completed = True
                state.transition(AutoPhase.SEED_GENERATION, "generating Seed from auto interview")
                self._save(state)
        elif state.phase == AutoPhase.REPAIR:
            state.transition(AutoPhase.REVIEW, "resuming review after repair checkpoint")
            self._save(state)
        elif state.phase not in {
            AutoPhase.SEED_GENERATION,
            AutoPhase.REVIEW,
            AutoPhase.RUN,
            AutoPhase.RALPH_HANDOFF,
        }:
            state.mark_blocked(
                f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
                tool_name="auto_pipeline",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)

        if self._enforce_deadline(state):
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase == AutoPhase.SEED_GENERATION:
            if state.seed_artifact:
                try:
                    seed = Seed.from_dict(state.seed_artifact)
                except Exception as exc:
                    state.mark_failed(
                        f"persisted Seed artifact is invalid: {exc}",
                        tool_name="seed_generator",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                state.transition(AutoPhase.REVIEW, "resuming review from persisted Seed")
                self._save(state)
            else:
                if not state.interview_session_id:
                    state.mark_failed(
                        "seed generation cannot resume without interview_session_id",
                        tool_name="seed_generator",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                seed_timeout = self._deadline_capped_timeout(state, self.seed_timeout_seconds)
                try:
                    seed = await asyncio.wait_for(
                        self.seed_generator(state.interview_session_id),
                        timeout=seed_timeout,
                    )
                    if not isinstance(seed, Seed):
                        msg = f"seed generator returned {type(seed).__name__}, expected Seed"
                        raise TypeError(msg)
                    # Apply deterministic floor: the LLM-derived ambiguity_score
                    # cannot fall below what code can objectively measure from the
                    # ledger (open gaps, conflicting entries, assumption-only
                    # sections). Seals self-rationalization at the A-grade gate.
                    floor = deterministic_floor(ledger)
                    if floor > seed.metadata.ambiguity_score:
                        seed = seed.model_copy(
                            update={
                                "metadata": seed.metadata.model_copy(
                                    update={"ambiguity_score": floor}
                                ),
                            }
                        )
                    state.seed_id = seed.metadata.seed_id
                    state.seed_artifact = seed.to_dict()
                    state.seed_origin = SeedOrigin.AUTO_PIPELINE
                except TimeoutError as exc:
                    if self._enforce_deadline(state):
                        record_authoring_backend(state)
                        return self._result(state, ledger, blocker=state.last_error)
                    state.mark_blocked(
                        f"seed generation timed out after {self.seed_timeout_seconds:.0f}s",
                        tool_name="seed_generator",
                    )
                    record_authoring_backend(state)
                    self._save(state)
                    return self._result(state, ledger, blocker=str(exc) or state.last_error)
                except Exception as exc:
                    state.mark_failed(
                        f"seed generation failed: {exc}",
                        tool_name="seed_generator",
                    )
                    record_authoring_backend(state)
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                state.mark_progress("Seed generated", tool_name="seed_generator")
                self._save(state)
                state.transition(
                    AutoPhase.REVIEW, f"reviewing Seed for required grade {state.required_grade}"
                )
                self._save(state)
        elif (
            state.phase == AutoPhase.REVIEW
            and resume_tool_name in {"grade_gate", "seed_loader"}
            and self.seed_loader is not None
            and state.seed_path
        ):
            seed = self._load_seed(state, state.seed_path)
            if seed is None:
                return self._result(state, ledger, blocker=state.last_error)
        elif state.seed_artifact:
            try:
                seed = Seed.from_dict(state.seed_artifact)
            except Exception as exc:
                state.mark_failed(
                    f"persisted Seed artifact is invalid: {exc}",
                    tool_name="auto_pipeline",
                )
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
        elif self.seed_loader is not None and state.seed_path:
            seed = self._load_seed(state, state.seed_path)
            if seed is None:
                return self._result(state, ledger, blocker=state.last_error)
        else:
            state.mark_blocked(
                f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
                tool_name="auto_pipeline",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)

        if state.phase == AutoPhase.RALPH_HANDOFF:
            return await self._resume_ralph_handoff(state, ledger, review=review)

        if self._enforce_deadline(state):
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase == AutoPhase.REVIEW:
            reviewer = self.reviewer or SeedReviewer(self.grade_gate)
            repairer = self.repairer or SeedRepairer(reviewer=reviewer)
            repair_timeout = state.phase_timeout_seconds(AutoPhase.REPAIR)
            # ``asyncio.wait_for`` only releases the awaiting coroutine; it
            # cannot interrupt synchronous reviewer work running in the
            # ``to_thread`` worker. Pass an explicit cancel signal so the
            # repairer exits at the next iteration boundary instead of
            # continuing to consume LLM calls after the budget expired
            # (PR #785 review-3).
            cancel_event = threading.Event()
            converge_kwargs: dict[str, Any] = {"ledger": ledger}
            # Older test stubs / external implementations of ``converge`` may
            # not accept ``cancel_event``; only pass it when the callable
            # actually declares it (or accepts ``**kwargs``). Real
            # ``SeedRepairer.converge`` does declare it.
            if _accepts_keyword(repairer.converge, "cancel_event"):
                converge_kwargs["cancel_event"] = cancel_event
            bounded_repair_timeout = self._deadline_capped_timeout(state, repair_timeout)
            try:
                seed, review, repairs = await asyncio.wait_for(
                    asyncio.to_thread(repairer.converge, seed, **converge_kwargs),
                    timeout=bounded_repair_timeout,
                )
            except TimeoutError:
                cancel_event.set()
                if self._enforce_deadline(state):
                    return self._result(state, ledger, blocker=state.last_error)
                state.mark_blocked(
                    f"repair phase exceeded {repair_timeout:.0f}s",
                    tool_name="seed_repairer",
                )
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
            state.seed_artifact = seed.to_dict()
            state.repair_round = len(repairs)
            state.last_grade = review.grade_result.grade.value
            state.findings = [asdict(finding) for finding in review.findings]
            state.ledger = ledger.to_dict()
            self._maybe_emit_repair(state)
            self._maybe_emit_grade(state)
            if self.seed_saver is not None:
                try:
                    state.seed_path = self.seed_saver(seed)
                except Exception as exc:
                    state.mark_failed(f"seed save failed: {exc}", tool_name="seed_saver")
                    self._save(state)
                    return self._result(state, ledger, review=review, blocker=state.last_error)
            self._save(state)

            if not _grade_meets_required(review.grade_result.grade.value, state.required_grade):
                blocker = (
                    f"Seed grade {review.grade_result.grade.value} did not meet "
                    f"required grade {state.required_grade}"
                )
                state.mark_blocked(blocker, tool_name="grade_gate")
                self._save(state)
                return self._result(state, ledger, review=review, blocker=blocker)

            if not review.may_run and not (self.skip_run or state.skip_run):
                blocker = "Seed review did not clear the Seed for execution"
                state.mark_blocked(blocker, tool_name="grade_gate")
                self._save(state)
                return self._result(state, ledger, review=review, blocker=blocker)

            if self.skip_run or state.skip_run:
                state.transition(
                    AutoPhase.COMPLETE,
                    f"Seed grade {review.grade_result.grade.value} ready; skip-run requested",
                )
                self._save(state)
                return self._result(state, ledger, review=review)

        if self._enforce_deadline(state):
            return self._result(state, ledger, review=review, blocker=state.last_error)
        if state.phase == AutoPhase.RUN:
            attached = self._attach_run_if_requested(state)
            if attached is not None:
                self._save(state)
                return self._result(state, ledger, review=review)
            reconciled, transient_blocker = self._reconcile_run_if_requested(state)
            if reconciled is not None:
                self._save(state)
                blocker = transient_blocker or state.last_error
                return self._result(state, ledger, review=review, blocker=blocker)
            if any((state.job_id, state.execution_id, state.run_session_id)):
                state.run_handoff_status = "started"
                state.run_handoff_guidance = None
                # Q00/ouroboros#773 (review-5 finding 2): honor the durable
                # ``complete_product`` intent on RUN resume. Without this
                # branch, a crash between run handoff and ``_handoff_to_ralph``
                # would silently bypass Ralph on resume even though the
                # operator explicitly opted into RUN → RALPH_HANDOFF — a
                # regression of the persisted-session contract added in
                # this PR.
                if self.complete_product:
                    if self.ralph_starter is None:
                        state.mark_blocked(
                            "Cannot resume complete-product Ralph handoff without ralph starter configured",
                            tool_name="ralph_starter",
                        )
                        self._save(state)
                        return self._result(state, ledger, review=review, blocker=state.last_error)
                    return await self._handoff_to_ralph(
                        state, ledger, seed, review, run_subagent=None
                    )
                state.transition(
                    AutoPhase.COMPLETE, "execution already started; using persisted run handle"
                )
                self._save(state)
                return self._result(state, ledger, review=review)
            if not _grade_meets_required(state.last_grade, state.required_grade):
                state.mark_blocked(
                    f"Cannot start execution without a persisted grade meeting {state.required_grade}",
                    tool_name="grade_gate",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)
            if review is None:
                reviewer = self.reviewer or SeedReviewer(self.grade_gate)
                review_timeout = self._deadline_capped_timeout(
                    state, state.phase_timeout_seconds(AutoPhase.REVIEW)
                )
                try:
                    review = await asyncio.wait_for(
                        asyncio.to_thread(reviewer.review, seed, ledger=ledger),
                        timeout=review_timeout,
                    )
                except TimeoutError:
                    if self._enforce_deadline(state):
                        return self._result(state, ledger, blocker=state.last_error)
                    state.mark_blocked(
                        "review timed out before run could be started",
                        tool_name="seed_reviewer",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                state.last_grade = review.grade_result.grade.value
                state.findings = [asdict(finding) for finding in review.findings]
                self._maybe_emit_grade(state)
                self._save(state)
            if not review.may_run:
                state.mark_blocked(
                    "Seed review did not clear the Seed for execution",
                    tool_name="grade_gate",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)

        if self.run_starter is None:
            state.mark_blocked("No run starter configured", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker="No run starter configured")

        if state.phase != AutoPhase.RUN:
            state.run_start_attempted = False
            state.run_handoff_status = None
            state.run_handoff_guidance = None
            state.transition(
                AutoPhase.RUN,
                f"starting execution for grade {state.last_grade or state.required_grade} Seed",
            )
            self._save(state)
        # The run starter is invoked at most twice per session lifetime:
        # once for the initial attempt, and once on retry if the first
        # attempt timed out or returned no durable tracking handle. Both
        # calls share the same idempotency_key (state.auto_session_id) so
        # the server-side handler returns the same execution metadata
        # rather than enqueuing a duplicate. See Q00/ouroboros#774.
        #
        # If a previous pipeline.run() already exhausted the bounded
        # retry (state.last_error carries the documented retry phrase),
        # do NOT call the run starter a third time — the in-process
        # idempotency map cannot rule out a duplicate enqueue past two
        # attempts on the same session.
        idempotency_key = state.auto_session_id
        prior_retry_exhausted = (
            state.run_handoff_guidance is not None
            and _RETRY_GUIDANCE_PHRASE in state.run_handoff_guidance
        ) or (
            # Conservative non-retryable guard. Covers two cases:
            #   1. Pre-#787 sessions persisted before ``run_handoff_status``
            #      existed: ``AutoPipelineState.from_dict`` defaults the
            #      field to ``None`` on load. Such a session resumed with
            #      ``run_start_attempted=True`` cannot prove which retry
            #      slot is still safe, so the conservative pre-#787
            #      behavior is preserved (block instead of dispatching a
            #      duplicate enqueue).
            #   2. Mid-call crash before ``_mark_unknown_run_handoff`` ran
            #      (loop sets ``run_start_attempted=True`` and saves before
            #      calling ``run_starter``).
            #   3. Symmetric guard for the non-timeout retry-exception
            #      path: ``unknown_retry_failed`` lands here too because
            #      it's not a retryable status.
            bool(state.run_start_attempted)
            and state.run_handoff_status not in {"unknown_no_handle", "unknown_timeout"}
        )
        if prior_retry_exhausted:
            blocker_text = state.last_error or state.run_handoff_guidance
            state.mark_blocked(
                blocker_text or "run starter retry already exhausted", tool_name="run_starter"
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        # If we resume into RUN with a persisted unknown handoff
        # (``run_start_attempted=True`` plus an ``unknown_*`` status), the
        # first iteration of this loop *is* the retry — the prior
        # pipeline.run() call already used the initial attempt slot.
        retried = bool(state.run_start_attempted) and state.run_handoff_status in {
            "unknown_no_handle",
            "unknown_timeout",
        }
        attempted_at_entry = state.run_start_attempted
        while True:
            state.run_start_attempted = True
            self._save(state)
            run_meta: dict[str, Any] | None = None
            run_start_timeout = self._deadline_capped_timeout(state, self.run_start_timeout_seconds)
            try:
                run_kwargs: dict[str, Any] = {}
                if _accepts_keyword(self.run_starter, "idempotency_key"):
                    run_kwargs["idempotency_key"] = idempotency_key
                run_meta = await asyncio.wait_for(
                    self.run_starter(seed, **run_kwargs),
                    timeout=run_start_timeout,
                )
                if not isinstance(run_meta, dict):
                    msg = f"run starter returned {type(run_meta).__name__}, expected dict"
                    raise TypeError(msg)
            except TimeoutError as exc:
                if self._enforce_deadline(state):
                    return self._result(state, ledger, review=review, blocker=state.last_error)
                _mark_unknown_run_handoff(state, status="unknown_timeout")
                if retried:
                    state.run_handoff_guidance = (
                        f"{state.run_handoff_guidance or ''} "
                        f"{_RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                    ).strip()
                    state.mark_blocked(
                        f"run start timed out after {self.run_start_timeout_seconds:.0f}s; "
                        f"{_RETRY_GUIDANCE_PHRASE} {idempotency_key}",
                        tool_name="run_starter",
                    )
                    self._save(state)
                    return self._result(
                        state,
                        ledger,
                        review=review,
                        blocker=state.last_error or str(exc),
                    )
            except Exception as exc:
                if retried:
                    # Retry attempt itself raised — bound is exhausted. The
                    # initial attempt may have already enqueued execution on
                    # the server, so we MUST NOT call run_starter a third
                    # time on a later resume. Persist an exhausted-retry
                    # marker so the symmetric guard above re-blocks instead
                    # of re-entering the run-start branch. ``last_error``
                    # carries the documented retry phrase so callers can
                    # detect this specific terminal state.
                    state.run_handoff_status = "unknown_retry_failed"
                    state.run_handoff_guidance = (
                        f"{state.run_handoff_guidance or 'Run starter retry raised an exception'} "
                        f"{_RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                    ).strip()
                    state.mark_blocked(
                        f"run start failed on retry: {exc}; "
                        f"{_RETRY_GUIDANCE_PHRASE} {idempotency_key}",
                        tool_name="run_starter",
                    )
                    # Leave state.run_start_attempted=True so the caller's
                    # next pipeline.run() short-circuits at the symmetric
                    # guard rather than starting a third attempt.
                    self._save(state)
                    return self._result(state, ledger, review=review, blocker=state.last_error)
                # Initial attempt: non-timeout errors are not retried —
                # the contract is to bound retries on *unknown* handoffs
                # only. Reset the attempt flag so the caller can re-invoke
                # after fixing the underlying error (preserves prior
                # behavior).
                state.run_start_attempted = attempted_at_entry
                state.mark_failed(f"run start failed: {exc}", tool_name="run_starter")
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)

            if run_meta is not None:
                state.job_id = _optional_str(run_meta.get("job_id"))
                state.execution_id = _optional_str(run_meta.get("execution_id"))
                state.run_session_id = _optional_str(run_meta.get("session_id"))
                run_subagent = (
                    run_meta.get("_subagent")
                    if isinstance(run_meta.get("_subagent"), dict)
                    else None
                )
                state.run_subagent = run_subagent or {}
                if any((state.job_id, state.execution_id, state.run_session_id)):
                    state.run_handoff_status = "started"
                    state.run_handoff_guidance = None
                    # Q00/ouroboros#773: when ``--complete-product`` is set
                    # and a ralph starter is configured, chain RUN →
                    # RALPH_HANDOFF instead of going straight to COMPLETE.
                    if self.complete_product:
                        if self.ralph_starter is None:
                            state.mark_blocked(
                                "Cannot continue complete-product run without ralph starter configured",
                                tool_name="ralph_starter",
                            )
                            self._save(state)
                            return self._result(
                                state,
                                ledger,
                                review=review,
                                blocker=state.last_error,
                                run_subagent=run_subagent,
                            )
                        return await self._handoff_to_ralph(
                            state, ledger, seed, review, run_subagent
                        )
                    state.transition(
                        AutoPhase.COMPLETE,
                        f"execution started for grade "
                        f"{state.last_grade or state.required_grade} Seed",
                    )
                    self._save(state)
                    return self._result(state, ledger, review=review, run_subagent=run_subagent)
                # No durable handle surfaced — treat as unknown handoff.
                _mark_unknown_run_handoff(state)

            if retried:
                # Retry exhausted on no-handle path (timed_out path returned
                # earlier). Block with the documented retry phrase and
                # persist it onto run_handoff_guidance so a later resume
                # can detect that the bound is already spent.
                guidance = state.run_handoff_guidance or "Run starter returned no tracking handle"
                state.run_handoff_guidance = (
                    f"{guidance} {_RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                ).strip()
                state.mark_blocked(
                    f"{guidance} {_RETRY_GUIDANCE_PHRASE} {idempotency_key}",
                    tool_name="run_starter",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)

            # First attempt landed in an unknown handoff (timeout or
            # no-handle). Persist the unknown status, then retry exactly
            # once with the same idempotency_key so the server-side
            # handler can short-circuit any duplicate enqueue. Both
            # timeout and no-handle paths share this same retry slot.
            self._save(state)
            retried = True

    def _deadline_capped_timeout(self, state: AutoPipelineState, phase_timeout: float) -> float:
        """Return ``phase_timeout`` capped by the remaining pipeline deadline.

        Without this cap, ``_enforce_deadline`` only fires at phase
        boundaries — a single ``await`` inside the interview / seed-gen /
        repair / run-start path could spend the full per-phase timeout
        even after the top-level deadline expired, breaking the public
        ``pipeline_timeout`` contract (#790 review-6). Returns
        ``phase_timeout`` unchanged when no deadline is armed; returns a
        near-zero floor when the deadline is already past so the next
        ``asyncio.wait_for`` trips immediately and routes the failure into
        ``_enforce_deadline``.
        """
        if state.deadline_at is None:
            return float(phase_timeout)
        remaining = state.deadline_at - time.monotonic()
        if remaining <= 0:
            return 0.0
        return float(min(float(phase_timeout), remaining))

    async def _handoff_to_ralph(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
    ) -> AutoPipelineResult:
        """Run the RUN → RALPH_HANDOFF → terminal-phase chain.

        Builds a deterministic ``lineage_id``, forwards the remaining
        pipeline budget as ``max_total_seconds``, and maps the ralph
        terminal status back into one of ``COMPLETE`` / ``BLOCKED`` /
        ``FAILED`` per the contract pinned by
        :data:`_RALPH_BLOCKED_STOP_REASONS`. Plugin-mode dispatches
        transition to COMPLETE immediately and surface the OpenCode Task
        widget guidance to the operator.
        """
        assert self.ralph_starter is not None  # noqa: S101 - guarded by caller
        lineage_id = f"ralph-{seed.metadata.seed_id}-{state.auto_session_id[:8]}"
        state.ralph_lineage_id = lineage_id
        state.transition(
            AutoPhase.RALPH_HANDOFF,
            f"handing off grade {state.last_grade or state.required_grade} Seed to Ralph loop",
        )
        self._save(state)
        max_total_seconds: float | None = None
        per_iteration_timeout_seconds: float | None = None
        if state.deadline_at is not None:
            remaining = state.deadline_at - time.monotonic()
            if remaining < _MIN_RALPH_MAX_TOTAL_SECONDS:
                message = (
                    "pipeline_timeout: remaining deadline budget "
                    f"{max(0.0, remaining):.1f}s is below Ralph minimum "
                    f"{_MIN_RALPH_MAX_TOTAL_SECONDS:.0f}s during {state.phase.value}"
                )
                state.mark_blocked(message, tool_name=PIPELINE_DEADLINE_TOOL_NAME)
                self._save(state)
                return self._result(
                    state,
                    ledger,
                    review=review,
                    blocker=state.last_error,
                    run_subagent=run_subagent,
                )
            max_total_seconds = remaining
            # Cap per-iteration so a single ``evolve_step`` cannot block past
            # the remaining deadline. ``RalphLoopRunner`` checks
            # ``max_total_seconds`` only at the top of each iteration, so
            # without this cap the first iteration could still run for the
            # full default 1800s after the deadline expired. Floored at the
            # Ralph minimum (30s) — when the remaining budget is itself below
            # that floor we still cap at 30s rather than rejecting at the
            # auto layer, since the pre-dispatch ``MIN_RALPH_MAX_TOTAL_SECONDS``
            # check already protects against a sub-second budget. The
            # ``max_total_seconds`` cap then aborts the loop before any
            # follow-up iteration starts, so the worst-case overshoot is one
            # iteration of up to 30 seconds.
            per_iteration_timeout_seconds = max(
                _MIN_RALPH_PER_ITERATION_SECONDS,
                min(_DEFAULT_RALPH_PER_ITERATION_SECONDS, remaining),
            )

        # Q00/ouroboros#773 (review-6): persist the Ralph dispatch handle as
        # soon as the background job exists, BEFORE we await terminal
        # completion. Without this checkpoint, a process restart, deadline
        # trip, or client disconnect after dispatch but before terminal
        # would leave the persisted state with only ``ralph_lineage_id`` —
        # ``_resume_ralph_handoff`` then cannot call ``ralph_resumer``
        # (which keys off ``ralph_job_id``) and falls back to guidance-only
        # text, reintroducing the stranded-resume bug this PR is meant to
        # solve. The starter callable invokes this hook BEFORE blocking on
        # the terminal-status poll.
        def _checkpoint_dispatch(envelope: dict[str, Any]) -> None:
            state.ralph_job_id = _optional_str(envelope.get("job_id"))
            state.ralph_dispatch_mode = _optional_str(envelope.get("dispatch_mode"))
            persisted_lineage = _optional_str(envelope.get("lineage_id"))
            if persisted_lineage:
                state.ralph_lineage_id = persisted_lineage
            state.last_tool_name = "ralph_starter"
            self._save(state)

        # Q00/ouroboros#773 (review-7): decide compatibility BEFORE invocation,
        # never by retrying on a post-dispatch ``TypeError``. ``RalphHandler``
        # creates a brand-new background job on every call and has no
        # idempotency key (unlike the run starter's ``idempotency_key`` path),
        # so a second invocation after a real ``TypeError`` thrown post-dispatch
        # would create a duplicate Ralph loop mutating the same lineage.
        # Inspect the callable's signature once and route the kwargs through
        # the right shape on a single attempt.
        starter_kwargs: dict[str, Any] = {
            "lineage_id": lineage_id,
            "max_total_seconds": max_total_seconds,
            "per_iteration_timeout_seconds": per_iteration_timeout_seconds,
        }
        if _accepts_keyword(self.ralph_starter, "on_dispatched"):
            starter_kwargs["on_dispatched"] = _checkpoint_dispatch
        try:
            ralph_call = self.ralph_starter(seed, **starter_kwargs)
            if state.deadline_at is None:
                ralph_meta = await ralph_call
            else:
                ralph_timeout = max(0.0, state.deadline_at - time.monotonic())
                ralph_meta = await asyncio.wait_for(ralph_call, timeout=ralph_timeout)
        except TimeoutError:
            # Even if the deadline trips, the Ralph job may already exist
            # server-side (the dispatch checkpoint above persisted its
            # handle). Resume will then poll it via ``_resume_ralph_handoff``
            # rather than treating the session as terminally lost.
            if self._enforce_deadline(state):
                return self._result(
                    state,
                    ledger,
                    review=review,
                    blocker=state.last_error,
                    run_subagent=run_subagent,
                )
            state.mark_blocked(
                "ralph handoff timed out before terminal status",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        except Exception as exc:
            state.mark_failed(f"ralph handoff failed: {exc}", tool_name="ralph_starter")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        if not isinstance(ralph_meta, dict):
            state.mark_failed(
                f"ralph starter returned {type(ralph_meta).__name__}, expected dict",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        state.ralph_job_id = _optional_str(ralph_meta.get("job_id"))
        state.ralph_dispatch_mode = _optional_str(ralph_meta.get("dispatch_mode"))
        terminal_status = _optional_str(ralph_meta.get("terminal_status"))
        stop_reason = _optional_str(ralph_meta.get("stop_reason"))
        # Plugin delegation: nothing to await, transition straight to
        # COMPLETE and surface the OpenCode Task widget guidance.
        if state.ralph_dispatch_mode == "plugin":
            state.run_handoff_guidance = (
                "Ralph loop delegated to the OpenCode plugin child session. "
                "Track progress through the OpenCode Task widget; this auto "
                "session will not block on the loop's completion."
            )
            state.transition(
                AutoPhase.COMPLETE,
                "ralph loop delegated to OpenCode plugin child session",
            )
            self._save(state)
            return self._result(state, ledger, review=review, run_subagent=run_subagent)
        if terminal_status == "completed":
            state.transition(
                AutoPhase.COMPLETE,
                f"ralph loop completed ({stop_reason or 'qa passed'})",
            )
            self._save(state)
            return self._result(state, ledger, review=review, run_subagent=run_subagent)
        if terminal_status == "cancelled":
            state.mark_blocked(RALPH_CANCEL_BLOCKER_REASON, tool_name="ralph_starter")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        if terminal_status == "failed" and stop_reason in _RALPH_BLOCKED_STOP_REASONS:
            state.mark_blocked(stop_reason, tool_name="ralph_starter")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        # Any other failure (terminal failure action, exception bubbled up,
        # or an unrecognized status) is a hard FAILED.
        message = (
            f"ralph loop failed: {stop_reason}"
            if stop_reason
            else f"ralph loop failed: terminal_status={terminal_status or 'unknown'}"
        )
        state.mark_failed(message, tool_name="ralph_starter")
        self._save(state)
        return self._result(
            state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
        )

    async def _resume_ralph_handoff(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None,
    ) -> AutoPipelineResult:
        """Resume a persisted Ralph handoff checkpoint.

        Plugin-mode dispatches transition straight to COMPLETE (the plugin
        child session is fire-and-forget — there is no in-process job to
        await). Job-mode dispatches with a configured ``ralph_resumer``
        poll the persisted ``ralph_job_id`` and map the terminal status
        back onto the same auto phase as :meth:`_handoff_to_ralph`, so a
        session interrupted between dispatch and terminal status (review-5
        finding 1) is actually reconciled instead of stranded in
        ``RALPH_HANDOFF`` forever. When no ``ralph_resumer`` is wired (or
        no ``ralph_job_id`` was persisted) the method falls back to
        guidance-only behavior so callers without a job-manager handle
        still get a coherent message instead of a polling failure.
        """
        if state.ralph_dispatch_mode == "plugin":
            state.run_handoff_guidance = (
                state.run_handoff_guidance
                or "Ralph loop delegated to the OpenCode plugin child session. "
                "Track progress through the OpenCode Task widget; this auto "
                "session will not block on the loop's completion."
            )
            state.transition(
                AutoPhase.COMPLETE,
                "resumed OpenCode plugin Ralph delegation checkpoint",
            )
            self._save(state)
            return self._result(state, ledger, review=review)

        if self.ralph_resumer is not None and state.ralph_job_id:
            return await self._poll_ralph_job(state, ledger, review=review)

        handle = state.ralph_job_id or state.ralph_lineage_id
        if handle:
            state.run_handoff_guidance = (
                "Ralph handoff already has a persisted tracking handle; resume did "
                "not start duplicate run or Ralph work. Track the existing Ralph "
                f"lineage/job: {handle}."
            )
        else:
            state.run_handoff_guidance = (
                "Ralph handoff checkpoint has no persisted Ralph job handle; resume "
                "did not start duplicate run or Ralph work. Inspect the Ralph runtime "
                "before dispatching manually."
            )
        state.mark_progress(state.run_handoff_guidance, tool_name="ralph_starter")
        self._save(state)
        return self._result(state, ledger, review=review)

    async def _poll_ralph_job(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None,
    ) -> AutoPipelineResult:
        """Poll a persisted Ralph job and map its terminal status onto an auto phase.

        Used only by :meth:`_resume_ralph_handoff` when ``ralph_resumer`` is
        wired and ``state.ralph_job_id`` is present. The polling shape mirrors
        :meth:`_handoff_to_ralph` so the COMPLETE / BLOCKED / FAILED contract
        pinned by :data:`_RALPH_BLOCKED_STOP_REASONS` stays single-sourced.
        """
        assert self.ralph_resumer is not None  # noqa: S101 - guarded by caller
        assert state.ralph_job_id is not None  # noqa: S101 - guarded by caller
        try:
            poll_call = self.ralph_resumer(job_id=state.ralph_job_id)
            if state.deadline_at is None:
                ralph_meta = await poll_call
            else:
                poll_timeout = max(0.0, state.deadline_at - time.monotonic())
                ralph_meta = await asyncio.wait_for(poll_call, timeout=poll_timeout)
        except TimeoutError:
            if self._enforce_deadline(state):
                return self._result(state, ledger, review=review, blocker=state.last_error)
            state.mark_blocked(
                "ralph resume poll timed out before terminal status",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        except Exception as exc:
            state.mark_failed(f"ralph resume poll failed: {exc}", tool_name="ralph_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        if not isinstance(ralph_meta, dict):
            state.mark_failed(
                f"ralph resumer returned {type(ralph_meta).__name__}, expected dict",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        terminal_status = _optional_str(ralph_meta.get("terminal_status"))
        stop_reason = _optional_str(ralph_meta.get("stop_reason"))
        if terminal_status == "completed":
            state.transition(
                AutoPhase.COMPLETE,
                f"resumed ralph loop completed ({stop_reason or 'qa passed'})",
            )
            self._save(state)
            return self._result(state, ledger, review=review)
        if terminal_status == "cancelled":
            state.mark_blocked(RALPH_CANCEL_BLOCKER_REASON, tool_name="ralph_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        if terminal_status == "failed" and stop_reason in _RALPH_BLOCKED_STOP_REASONS:
            state.mark_blocked(stop_reason, tool_name="ralph_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        # Any other failure is a hard FAILED, mirroring _handoff_to_ralph.
        message = (
            f"ralph loop failed: {stop_reason}"
            if stop_reason
            else f"ralph loop failed: terminal_status={terminal_status or 'unknown'}"
        )
        state.mark_failed(message, tool_name="ralph_starter")
        self._save(state)
        return self._result(state, ledger, review=review, blocker=state.last_error)

    def _enforce_deadline(self, state: AutoPipelineState) -> bool:
        """Return True when the pipeline must abort because the deadline expired.

        Mutates ``state`` to ``BLOCKED`` with ``tool_name=pipeline_deadline``
        and a ``pipeline_timeout`` error message, then persists. Callers must
        return immediately when this returns True. No-op when the deadline is
        unset or the state is already terminal.
        """
        if state.is_terminal() or state.deadline_at is None:
            return False
        if not state.is_deadline_expired():
            return False
        remaining = state.deadline_at - time.monotonic()
        message = (
            f"pipeline_timeout: deadline exceeded by "
            f"{abs(remaining):.1f}s during {state.phase.value}"
        )
        state.last_tool_name = PIPELINE_DEADLINE_TOOL_NAME
        state.mark_blocked(message, tool_name=PIPELINE_DEADLINE_TOOL_NAME)
        self._save(state)
        return True

    def _load_seed(self, state: AutoPipelineState, seed_path: str) -> Seed | None:
        if self.seed_loader is None:
            state.mark_failed("seed loader is not configured", tool_name="seed_loader")
            self._save(state)
            return None
        try:
            seed = self.seed_loader(seed_path)
        except Exception as exc:
            state.mark_failed(f"seed load failed: {exc}", tool_name="seed_loader")
            self._save(state)
            return None
        if not isinstance(seed, Seed):
            state.mark_failed(
                f"seed loader returned {type(seed).__name__}, expected Seed",
                tool_name="seed_loader",
            )
            self._save(state)
            return None
        # Loader-based resume paths previously left ``seed_origin`` at the
        # legacy default ``none`` even though a Seed had clearly been
        # persisted by an earlier auto pipeline run (the Seed file at
        # ``seed_path`` was written by ``seed_saver``). Backfill the
        # provenance once on first post-PR resume so the new CLI/MCP
        # surfaces don't keep reporting an inaccurate ``none`` for valid
        # resumed sessions. Existing non-default values are preserved.
        if state.seed_origin is SeedOrigin.NONE:
            state.seed_origin = SeedOrigin.AUTO_PIPELINE
        return seed

    def _result(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None = None,
        blocker: str | None = None,
        run_subagent: dict[str, Any] | None = None,
        status_override: str | None = None,
    ) -> AutoPipelineResult:
        summary = ledger.summary()
        ledger_provenance = {
            source: tuple(sections) for source, sections in summary.get("provenance", {}).items()
        }
        return AutoPipelineResult(
            status=status_override or state.phase.value,
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            grade=review.grade_result.grade.value if review else state.last_grade,
            seed_path=state.seed_path,
            seed_origin=state.seed_origin.value,
            interview_session_id=state.interview_session_id,
            execution_id=state.execution_id,
            job_id=state.job_id,
            run_session_id=state.run_session_id,
            run_subagent=run_subagent or state.run_subagent or None,
            current_round=state.current_round,
            pending_question=state.pending_question,
            last_progress_message=state.last_progress_message,
            last_progress_at=state.last_progress_at,
            last_grade=state.last_grade,
            run_handoff_status=state.run_handoff_status,
            run_handoff_guidance=state.run_handoff_guidance,
            attached_run_handle=state.attached_run_handle,
            attached_run_source=state.attached_run_source,
            attached_at=state.attached_at,
            run_reconciliation_status=state.run_reconciliation_status,
            run_reconciliation_source=state.run_reconciliation_source,
            run_reconciled_at=state.run_reconciled_at,
            ralph_job_id=state.ralph_job_id,
            ralph_lineage_id=state.ralph_lineage_id,
            ralph_dispatch_mode=state.ralph_dispatch_mode,
            assumptions=tuple(ledger.assumptions()),
            non_goals=tuple(ledger.non_goals()),
            blocker=blocker or state.last_error,
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
            invoked_by=state.invoked_by(),
            provenance=dict(state.provenance) if state.provenance else None,
            last_authoring_backend=state.last_authoring_backend,
            resume_capability=state.resume_capability(),
            ledger_provenance=ledger_provenance,
            evidence_backed_sections=tuple(summary.get("evidence_backed_sections", ())),
            assumption_only_sections=tuple(summary.get("assumption_only_sections", ())),
        )

    def _attach_run_if_requested(self, state: AutoPipelineState) -> bool | None:
        handle = _first_nonempty(
            self.attach_execution_id, self.attach_job_id, self.attach_run_session_id
        )
        if handle is None:
            return None
        if not state.run_start_attempted or state.run_handoff_status not in {
            "unknown_no_handle",
            "unknown_timeout",
        }:
            msg = (
                "Attach requires an auto session with unknown run handoff status "
                "after a prior run start attempt"
            )
            state.mark_blocked(msg, tool_name="run_starter")
            return False
        state.execution_id = _optional_str(self.attach_execution_id)
        state.job_id = _optional_str(self.attach_job_id)
        state.run_session_id = _optional_str(self.attach_run_session_id)
        state.attached_run_handle = handle
        state.attached_run_source = _optional_str(self.attach_source) or "manual"
        state.attached_at = utc_now_iso()
        state.run_handoff_status = "attached"
        state.run_handoff_guidance = (
            "Attached an externally verified execution handle to this auto session; "
            "resume will use the attached handle and will not start a duplicate run."
        )
        # Successful attach supersedes any prior reconciliation outcome on the
        # same unknown handoff, so clear stale reconciliation metadata to avoid
        # surfacing contradictory state (attached + previous reconciliation failure).
        state.run_reconciliation_status = None
        state.run_reconciliation_source = None
        state.run_reconciled_at = None
        state.transition(AutoPhase.COMPLETE, "attached existing execution handle")
        return True

    def _reconcile_run_if_requested(
        self, state: AutoPipelineState
    ) -> tuple[bool | None, str | None]:
        """Run the generic reconciliation contract.

        Returns ``(outcome, transient_blocker)``:

        - ``outcome`` is ``None`` when reconcile was not requested, ``True`` for
          a successful reconciliation, and ``False`` when the request fails.
        - ``transient_blocker`` carries an invocation-only error message that
          must be surfaced to the caller for the current call only. It is used
          for failure paths (notably invalid-context against a terminal complete
          session) where mutating ``state.last_error`` durably would leak the
          error into every later plain ``--resume``/``--status`` response.
        """
        if not self.reconcile_run:
            return None, None
        if state.run_handoff_status == "attached" and state.attached_run_handle:
            state.run_reconciliation_status = "attached"
            state.run_reconciliation_source = _optional_str(self.reconcile_source) or "attached_run"
            state.run_reconciled_at = utc_now_iso()
            state.run_handoff_guidance = (
                "Reconciliation confirmed the session already has an attached run handle; "
                "resume will not start a duplicate run."
            )
            if state.phase == AutoPhase.COMPLETE:
                state.mark_progress(
                    "reconciled existing attached execution handle",
                    tool_name="run_starter",
                )
            else:
                state.transition(
                    AutoPhase.COMPLETE, "reconciled existing attached execution handle"
                )
            return True, None
        if not state.run_start_attempted or state.run_handoff_status not in {
            "unknown_no_handle",
            "unknown_timeout",
        }:
            msg = (
                "Reconciliation requires an auto session with unknown run handoff "
                "status after a prior run start attempt"
            )
            state.run_reconciliation_status = "invalid_context"
            state.run_reconciliation_source = _optional_str(self.reconcile_source) or "generic"
            state.run_reconciled_at = utc_now_iso()
            state.run_handoff_guidance = msg
            if state.phase == AutoPhase.COMPLETE:
                # Keep the terminal phase intact and avoid corrupting durable
                # state.last_error: future plain --resume/--status calls must
                # not report this per-invocation misuse as a steady-state
                # blocker. The message is returned as a transient blocker so
                # the current call still surfaces it via the result.
                state.last_tool_name = "run_starter"
                state.mark_progress(msg, tool_name="run_starter")
                return False, msg
            state.mark_blocked(msg, tool_name="run_starter")
            return False, None
        state.run_reconciliation_status = "unsupported"
        state.run_reconciliation_source = _optional_str(self.reconcile_source) or "generic"
        state.run_reconciled_at = utc_now_iso()
        state.run_handoff_guidance = (
            "Generic reconciliation has no runtime-specific discovery adapter for this "
            "unknown handoff. No duplicate run was started. Attach a verified execution, "
            "job, or run session handle, or add a runtime-specific reconciler that returns "
            "attached, not_found, ambiguous, or unsupported."
        )
        state.mark_blocked(state.run_handoff_guidance, tool_name="run_starter")
        return False, None

    def _save(self, state: AutoPipelineState) -> None:
        if self.store is not None:
            self.store.save(state)
        self._maybe_emit_phase(state)

    def _maybe_emit_phase(self, state: AutoPipelineState) -> None:
        phase = state.phase.value
        if phase == self._last_emitted_phase:
            return
        self._last_emitted_phase = phase
        self._emit(state, "phase", state.last_progress_message)

    def _maybe_emit_grade(self, state: AutoPipelineState) -> None:
        grade = state.last_grade
        if grade is None or grade == self._last_emitted_grade:
            return
        self._last_emitted_grade = grade
        self._emit(state, "grade", f"Seed grade {grade}", grade=grade)

    def _maybe_emit_repair(self, state: AutoPipelineState) -> None:
        rounds = state.repair_round
        if rounds <= 0 or rounds == self._last_emitted_repair:
            return
        self._last_emitted_repair = rounds
        self._emit(state, "repair", f"repair round {rounds}", round=rounds)

    def _emit(
        self,
        state: AutoPipelineState,
        kind: str,
        message: str,
        *,
        round: int | None = None,
        grade: str | None = None,
    ) -> None:
        if self.progress_callback is None:
            return
        event = AutoProgressEvent(
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            kind=kind,
            message=message,
            round=round,
            grade=grade,
        )
        try:
            self.progress_callback(event)
        except Exception:
            # Observers must never break the pipeline. Swallow callback errors.
            pass


def _mark_invalid_seed_artifact(state: AutoPipelineState, message: str) -> None:
    state.seed_artifact = {}
    # Keep seed_origin consistent with the now-empty seed_artifact: the
    # session no longer has a persisted Seed of any provenance, so the
    # publicly surfaced "auto_pipeline" / "external_authoring" claim
    # would otherwise become a misleading orphan attribution.
    state.seed_origin = SeedOrigin.NONE
    if state.phase in {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED}:
        now = utc_now_iso()
        state.phase = AutoPhase.FAILED
        state.phase_started_at = now
        state.last_progress_at = now
        state.updated_at = now
        state.last_tool_name = "auto_pipeline"
        state.last_progress_message = message
        state.last_error = message
        return
    state.mark_failed(message, tool_name="auto_pipeline")


def _mark_unknown_run_handoff(
    state: AutoPipelineState, *, status: str = "unknown_no_handle"
) -> None:
    if status == "unknown_no_handle" and state.run_handoff_status in {
        "unknown_no_handle",
        "unknown_timeout",
    }:
        status = state.run_handoff_status
    state.run_handoff_status = status
    if status == "unknown_timeout":
        state.run_handoff_guidance = (
            "Run starter timed out before a durable tracking handle was captured. "
            "The runtime may still have created an execution. Resume will attempt "
            "exactly one automatic retry reusing the same idempotency key (state."
            "auto_session_id) so the server-side handler can short-circuit a "
            "duplicate enqueue. After that retry budget is exhausted the pipeline "
            "blocks and any further resume requires manual inspection."
        )
        return
    state.run_handoff_guidance = (
        "Run starter was attempted, but no durable tracking handle was captured. "
        "Resume will attempt exactly one automatic retry reusing the same "
        "idempotency key (state.auto_session_id) so the server-side handler can "
        "short-circuit a duplicate enqueue. After that retry budget is exhausted "
        "the pipeline blocks and any further resume requires manual inspection."
    )


def _grade_meets_required(actual: str | None, required: str) -> bool:
    rank = {"A": 0, "B": 1, "C": 2}
    if actual not in rank or required not in rank:
        return False
    return rank[actual] <= rank[required]


def _accepts_keyword(func: Callable[..., Any], name: str) -> bool:
    """Return True iff ``func`` declares ``name`` or accepts ``**kwargs``.

    Used to decide whether the repair-phase cancel signal can be threaded
    into a ``converge``-shaped callable without breaking older test stubs
    that only declare ``(seed, *, ledger)``.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == name:
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _recoverable_phase_for_tool(tool_name: str | None) -> AutoPhase | None:
    if tool_name in {
        "interview.start",
        "interview.resume",
        "interview.answer",
        "auto_answerer",
        "interview_driver",
    }:
        return AutoPhase.INTERVIEW
    if tool_name == "seed_generator":
        return AutoPhase.SEED_GENERATION
    if tool_name in {"seed_saver", "grade_gate", "seed_loader", "seed_repairer"}:
        # ``seed_repairer`` joins this set so a repair-phase timeout (the
        # outer ``asyncio.wait_for`` around ``repairer.converge`` inside
        # AutoPipeline.run) is recoverable on ``--resume``: the only sensible
        # restart is the REVIEW phase, which re-invokes the bounded repairer.
        # Without this entry a transient timeout becomes a permanent dead end.
        return AutoPhase.REVIEW
    if tool_name == "run_starter":
        return AutoPhase.RUN
    if tool_name == "ralph_starter":
        return AutoPhase.RALPH_HANDOFF
    return None


def _arm_legacy_missing_deadline(state: AutoPipelineState) -> bool:
    """Arm #779 deadline for legacy resumed sessions already past CREATED."""
    if state.phase in {AutoPhase.CREATED, AutoPhase.COMPLETE}:
        return False
    if state.deadline_at is not None or state.deadline_at_epoch is not None:
        return False
    state.arm_deadline()
    return True


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        normalized = _optional_str(value)
        if normalized is not None:
            return normalized
    return None


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
