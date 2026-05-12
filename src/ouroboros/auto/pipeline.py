"""Full-quality AutoPipeline supervisor skeleton."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
import inspect
import threading
import time
from typing import Any, Protocol

from ouroboros.auto.adapters import EvaluateResult, LateralResult
from ouroboros.auto.blocker_attribution import record_authoring_backend
from ouroboros.auto.grading import GradeGate, deterministic_floor
from ouroboros.auto.handoff_contract import (
    IDEMPOTENCY_KEY_FIELD,
    IDEMPOTENCY_KWARG_NAME,
    RETRY_GUIDANCE_PHRASE,
    UNKNOWN_HANDOFF_STATUSES,
    UNKNOWN_NO_HANDLE_STATUS,
    UNKNOWN_TIMEOUT_STATUS,
    unknown_handoff_guidance,
)
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.lateral_routing import select_persona_for_qa_failure
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.listeners import RALPH_CANCEL_BLOCKER_REASON, mirror_ralph_job_events
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
# Evaluator contract: takes a Seed and the run artifact (typically a
# JobSnapshot.result_text from Ralph's terminal snapshot), returns a typed
# EvaluateResult. See HandlerEvaluator for the production implementation.
Evaluator = Callable[[Seed, str], Awaitable[EvaluateResult]]
# LateralThinker contract: invoked as keyword-only call with the persona +
# QA-failure shape + run artifact, returns a typed LateralResult. See
# HandlerLateralThinker for the production implementation.
LateralThinker = Callable[..., Awaitable[LateralResult]]

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

# Q00/ouroboros#782 review-12 BLOCKING #1: when the top-level deadline has
# already expired but a persisted Ralph job awaits reconciliation, give the
# resume poller a brief grace window so an already-terminal job is detected
# (snapshot returns immediately) before ``_enforce_deadline`` trips
# ``pipeline_timeout``. ``asyncio.wait_for(coro, 0)`` cancels the coroutine
# before it can read the first snapshot, so the inner ``get_snapshot`` would
# never run without this floor — silently demoting a legitimately completed
# Ralph loop to a false ``pipeline_timeout`` BLOCKED.
_RALPH_RESUME_PEEK_SECONDS = 1.0


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
    # RFC #809 Phase 2.1 — EVALUATE phase QA verdict surfaced to MCP/CLI.
    last_qa_score: float | None = None
    last_qa_verdict: str | None = None
    last_qa_differences: tuple[str, ...] = ()
    last_qa_suggestions: tuple[str, ...] = ()
    # RFC #809 Phase 2.2 — UNSTUCK_LATERAL persona output surfaced to MCP/CLI.
    last_lateral_persona: str | None = None
    last_lateral_approach_summary: str | None = None
    last_lateral_text: str | None = None
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
    # RFC #809 Phase 2.1 — when set AND ``complete_product`` is True, the
    # pipeline inserts an EVALUATE phase between the Ralph terminal verdict
    # and the COMPLETE transition. The evaluator grades the Ralph artifact
    # against the Seed's acceptance criteria via ``ouroboros_qa``. On QA
    # pass the pipeline still reaches COMPLETE; on QA fail it transitions
    # to BLOCKED with the QA differences/suggestions in ``last_error``.
    # See :class:`HandlerEvaluator` in adapters.py for the production wiring.
    evaluator: Evaluator | None = None
    # RFC #809 Phase 2.2 — when set AND ``complete_product`` is True AND the
    # evaluator reports ``passed=False``, the pipeline inserts an
    # UNSTUCK_LATERAL phase between EVALUATE and the BLOCKED transition.
    # The lateral thinker invokes a persona-driven prompt via
    # ``ouroboros_lateral_think`` so the operator (or a future automated
    # recovery layer) sees a reframing of the verification gap instead of
    # the raw QA differences. See :class:`HandlerLateralThinker` in
    # adapters.py for the production wiring.
    lateral_thinker: LateralThinker | None = None
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
        #
        # Q00/ouroboros#782 review-12 BLOCKING #1: never gate ``RALPH_HANDOFF``
        # resume on the deadline-expired early returns when there is a
        # persisted Ralph job / confirmed plugin dispatch waiting on
        # reconciliation. Falling through to ``_resume_ralph_handoff`` lets
        # the poller (or plugin-confirmed transition) finalize the auto
        # phase if Ralph already finished in the background while the
        # client was disconnected. If the job is still running, the
        # poller's own deadline-aware wait fires the same ``pipeline_timeout``
        # BLOCKED state via ``_enforce_deadline``. Same exception applies
        # to the second ``_enforce_deadline`` gate after the BLOCKED/FAILED
        # recovery branch below.
        ralph_resume_pending = state.phase is AutoPhase.RALPH_HANDOFF and (
            state.ralph_job_id is not None or state.ralph_dispatch_mode == "plugin"
        )
        if (
            state.deadline_at is not None
            and not state.is_terminal()
            and state.is_deadline_expired()
            and not ralph_resume_pending
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
        # Q00/ouroboros#782 review-12 BLOCKING #1: same exception as the
        # early-return above — let RALPH_HANDOFF resume reach
        # ``_resume_ralph_handoff`` so an already-terminal Ralph job can be
        # reconciled. The poller's deadline-aware ``wait_for`` (and
        # subsequent ``_enforce_deadline`` call inside ``_poll_ralph_job``)
        # still fires ``pipeline_timeout`` if the job is genuinely still
        # running after the persisted budget has expired.
        if not ralph_resume_pending and self._enforce_deadline(state):
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
            # RFC #809 Phase 2.1/2.2 — EVALUATE and UNSTUCK_LATERAL are
            # resumable phases. Their dedicated resume handlers below
            # (around lines 505 / 519) re-enter ``_run_evaluate`` /
            # ``_run_lateral`` which are idempotent via persisted artifact
            # hashes. Without these entries in the allowlist, a session
            # recovered to either phase from BLOCKED/FAILED would be
            # immediately re-blocked here before reaching its handler.
            AutoPhase.EVALUATE,
            AutoPhase.UNSTUCK_LATERAL,
        }:
            state.mark_blocked(
                f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
                tool_name="auto_pipeline",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)

        # Q00/ouroboros#782 review-12 BLOCKING #1: same exception — let
        # ``RALPH_HANDOFF`` resume reach ``_resume_ralph_handoff`` so the
        # poller can reconcile an already-terminal Ralph job.
        if not ralph_resume_pending and self._enforce_deadline(state):
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
            return await self._resume_ralph_handoff(state, ledger, review=review, seed=seed)

        if state.phase == AutoPhase.EVALUATE:
            # Re-enter the evaluator. ``_run_evaluate`` is idempotent via the
            # artifact-hash cache, so a resumed session with the same artifact
            # and a persisted verdict short-circuits without re-calling QA.
            #
            # If the current process does not wire an evaluator (e.g. the
            # MCP handler skipped wiring in plugin mode), we cannot re-enter
            # ``_run_evaluate`` — it asserts ``self.evaluator is not None``.
            # Fall back to a Phase-2.1-shaped BLOCKED summary using the
            # persisted QA fields so a session that ran EVALUATE in a
            # previous process can resume without a top-level tool crash.
            # Matches the symmetric guard the UNSTUCK_LATERAL branch below
            # already has for ``self.lateral_thinker``.
            if self.evaluator is None:
                state.mark_blocked(
                    state.last_error
                    or (
                        "EVALUATE resume found no evaluator wired in this process; "
                        "complete-product chains in plugin mode skip the auto-pipeline "
                        "evaluator (the existing Ralph plugin delegation handles QA "
                        "out-of-band). Re-run in non-plugin mode to grade the artifact "
                        "inline."
                    ),
                    tool_name="evaluator",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)
            return await self._run_evaluate(
                state,
                ledger,
                seed,
                review=review,
                run_subagent=None,
                ralph_result_text=None,
                stop_reason=None,
            )

        if state.phase == AutoPhase.UNSTUCK_LATERAL:
            # Re-enter the lateral advisor. ``_run_lateral`` is idempotent
            # via ``lateral_input_hash``: matching hash + cached persona text
            # short-circuits without re-invoking the lateral_think tool.
            if self.lateral_thinker is None:
                # Lateral not wired on this process — fall back to the
                # Phase 2.1 BLOCKED summary using the persisted QA fields.
                state.mark_blocked(
                    state.last_error or "EVALUATE failed; lateral thinker not configured",
                    tool_name="evaluator",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)
            return await self._run_lateral(
                state,
                ledger,
                seed,
                qa_score=state.last_qa_score or 0.0,
                qa_verdict=state.last_qa_verdict or "fail",
                qa_differences=tuple(state.last_qa_differences),
                qa_suggestions=tuple(state.last_qa_suggestions),
                cache_suffix="",
                review=review,
                run_subagent=None,
            )

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
                if self.complete_product and self.ralph_starter is not None:
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
        idempotency_key = getattr(state, IDEMPOTENCY_KEY_FIELD)
        prior_retry_exhausted = (
            state.run_handoff_guidance is not None
            and RETRY_GUIDANCE_PHRASE in state.run_handoff_guidance
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
            and state.run_handoff_status not in UNKNOWN_HANDOFF_STATUSES
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
        retried = (
            bool(state.run_start_attempted) and state.run_handoff_status in UNKNOWN_HANDOFF_STATUSES
        )
        attempted_at_entry = state.run_start_attempted
        while True:
            state.run_start_attempted = True
            self._save(state)
            run_meta: dict[str, Any] | None = None
            run_start_timeout = self._deadline_capped_timeout(state, self.run_start_timeout_seconds)
            try:
                run_kwargs: dict[str, Any] = {}
                if _accepts_keyword(self.run_starter, IDEMPOTENCY_KWARG_NAME):
                    run_kwargs[IDEMPOTENCY_KWARG_NAME] = idempotency_key
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
                _mark_unknown_run_handoff(state, status=UNKNOWN_TIMEOUT_STATUS)
                if retried:
                    state.run_handoff_guidance = (
                        f"{state.run_handoff_guidance or ''} "
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                    ).strip()
                    state.mark_blocked(
                        f"run start timed out after {self.run_start_timeout_seconds:.0f}s; "
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}",
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
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                    ).strip()
                    state.mark_blocked(
                        f"run start failed on retry: {exc}; "
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}",
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
                    if self.complete_product and self.ralph_starter is not None:
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
                    f"{guidance} {RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                ).strip()
                state.mark_blocked(
                    f"{guidance} {RETRY_GUIDANCE_PHRASE} {idempotency_key}",
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
        # Preserve a previously persisted lineage on resume so the re-dispatch
        # remains correlated with prior ``mcp.job.*`` events; only mint a fresh
        # one when this is the first handoff attempt for the session.
        lineage_id = state.ralph_lineage_id or (
            f"ralph-{seed.metadata.seed_id}-{state.auto_session_id[:8]}"
        )
        state.ralph_lineage_id = lineage_id
        if state.phase != AutoPhase.RALPH_HANDOFF:
            state.transition(
                AutoPhase.RALPH_HANDOFF,
                f"handing off grade {state.last_grade or state.required_grade} Seed to Ralph loop",
            )
        else:
            state.mark_progress(
                "re-entering Ralph handoff after resume",
                tool_name="ralph_starter",
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

        ralph_mirror_task: asyncio.Task[None] | None = None

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
            nonlocal ralph_mirror_task
            state.ralph_job_id = _optional_str(envelope.get("job_id"))
            state.ralph_dispatch_mode = _optional_str(envelope.get("dispatch_mode"))
            persisted_lineage = _optional_str(envelope.get("lineage_id"))
            if persisted_lineage:
                state.ralph_lineage_id = persisted_lineage
            state.last_tool_name = "ralph_starter"
            self._save(state)
            if (
                self.store is not None
                and state.ralph_job_id is not None
                and state.ralph_dispatch_mode != "plugin"
                and ralph_mirror_task is None
            ):
                event_store = getattr(self.ralph_starter, "job_event_store", None)
                if event_store is not None:
                    ralph_mirror_task = asyncio.create_task(
                        mirror_ralph_job_events(
                            state,
                            self.store,
                            event_store,
                            state.ralph_job_id,
                        )
                    )

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
            await _cancel_ralph_status_mirror(ralph_mirror_task)
            state.mark_failed(f"ralph handoff failed: {exc}", tool_name="ralph_starter")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        await _drain_ralph_status_mirror(ralph_mirror_task)
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
        current_generation = _ralph_current_generation_from_meta(ralph_meta)
        if terminal_status is not None:
            state.ralph_job_status = terminal_status
        if stop_reason is not None:
            state.ralph_stop_reason = stop_reason
        if current_generation is not None:
            state.ralph_current_generation = current_generation
        # Plugin delegation: nothing to await, transition straight to
        # COMPLETE and surface the OpenCode Task widget guidance.
        if state.ralph_dispatch_mode == "plugin":
            state.run_handoff_guidance = (
                "Ralph loop delegated to the OpenCode plugin child session. "
                "Track progress through the OpenCode Task widget; this auto "
                "session will not block on the loop's completion."
            )
            # Q00/ouroboros#782 review-5 BLOCKING #1: surface Ralph's
            # ``_subagent`` envelope so the OpenCode bridge actually spawns
            # the child session. In ``--complete-product`` plugin mode the
            # Ralph subagent supersedes the run-handoff subagent — the run
            # already kicked off and the loop is what the plugin must own.
            ralph_subagent = (
                ralph_meta.get("_subagent")
                if isinstance(ralph_meta.get("_subagent"), dict)
                else None
            )
            effective_subagent = ralph_subagent or run_subagent
            if ralph_subagent is not None:
                state.run_subagent = ralph_subagent
            state.transition(
                AutoPhase.COMPLETE,
                "ralph loop delegated to OpenCode plugin child session",
            )
            self._save(state)
            return self._result(state, ledger, review=review, run_subagent=effective_subagent)
        if terminal_status == "completed":
            return await self._evaluate_or_complete(
                state,
                ledger,
                seed,
                review=review,
                run_subagent=run_subagent,
                stop_reason=stop_reason,
                ralph_result_text=_artifact_text(ralph_meta.get("result_text")),
            )
        if terminal_status == "cancelled":
            if state.phase is not AutoPhase.BLOCKED:
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

    async def _evaluate_or_complete(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
        *,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
        stop_reason: str | None,
        ralph_result_text: str | None,
        resumed: bool = False,
    ) -> AutoPipelineResult:
        """Branch between EVALUATE and COMPLETE after a Ralph terminal verdict.

        Inserted by RFC #809 Phase 2.1. When ``self.evaluator`` is wired AND
        the session is in complete-product mode AND Ralph produced a
        ``result_text`` artifact, transitions to EVALUATE and invokes
        :meth:`_run_evaluate`. Otherwise falls back to the pre-Phase-2.1
        behaviour: transition to COMPLETE directly.
        """
        if self.evaluator is not None and state.complete_product and ralph_result_text is not None:
            # ``is not None`` not truthiness: an empty-but-valid Ralph
            # artifact is still a graded artifact. Skipping EVALUATE on
            # ``""`` would produce a silent false-pass for runs whose output
            # is intentionally empty, violating the
            # "complete_product + evaluator → graded COMPLETE" contract.
            state.transition(AutoPhase.EVALUATE, "evaluating ralph artifact against seed AC")
            self._save(state)
            return await self._run_evaluate(
                state,
                ledger,
                seed,
                review=review,
                run_subagent=run_subagent,
                ralph_result_text=ralph_result_text,
                stop_reason=stop_reason,
            )
        message_prefix = "resumed ralph loop completed" if resumed else "ralph loop completed"
        state.transition(
            AutoPhase.COMPLETE,
            f"{message_prefix} ({stop_reason or 'qa passed'})",
        )
        self._save(state)
        return self._result(state, ledger, review=review, run_subagent=run_subagent)

    async def _run_evaluate(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
        *,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
        ralph_result_text: str | None,
        stop_reason: str | None,
    ) -> AutoPipelineResult:
        """Run the EVALUATE phase: grade the run artifact against the Seed AC.

        Idempotent on resume — when ``state.evaluate_artifact_hash`` matches a
        freshly-computed hash of the current artifact AND a verdict was
        already persisted, the cached verdict is reused without re-invoking
        the LLM judge. A different artifact (e.g. Ralph re-ran on resume)
        forces re-evaluation.

        On QA pass → COMPLETE with the verdict in the progress message.
        On QA fail → BLOCKED with the verdict + top-3 differences/suggestions
        in the blocker text. On timeout / handler error → BLOCKED with
        ``tool_name="evaluator"`` so the session remains resumable.
        """
        assert self.evaluator is not None  # noqa: S101 — guarded by caller

        # Resolve the artifact:
        # 1. Fresh call from the Ralph terminal path → use ``ralph_result_text``
        #    (``is not None`` so an empty-but-valid artifact still grades)
        # 2. EVALUATE-phase resume after a prior call → use the persisted
        #    ``state.evaluate_artifact`` so a timeout/transient-error path is
        #    genuinely recoverable (without persistence, ``--resume`` had no
        #    artifact to grade and dropped into a permanent BLOCKED).
        import hashlib

        artifact: str | None = None
        if ralph_result_text is not None:
            artifact = ralph_result_text
        elif state.evaluate_artifact is not None:
            artifact = state.evaluate_artifact
        if artifact is None:
            artifact_hash = state.evaluate_artifact_hash
        else:
            artifact_hash = hashlib.sha256(artifact.encode("utf-8")).hexdigest()

        # Cache hit requires the persisted ``last_qa_passed`` boolean — the
        # canonical pass decision derived from ``score >= pass_threshold``
        # by the QA handler. Using ``last_qa_verdict == "pass"`` as the
        # cache key would silently reclassify a ``passed=True`` /
        # ``verdict="revise"`` result as BLOCKED on resume, breaking
        # idempotent resume behaviour.
        cache_hit = (
            artifact_hash is not None
            and state.evaluate_artifact_hash == artifact_hash
            and state.last_qa_passed is not None
        )
        if cache_hit:
            return await self._finalize_evaluate(
                state,
                ledger,
                review=review,
                run_subagent=run_subagent,
                passed=bool(state.last_qa_passed),
                score=state.last_qa_score or 0.0,
                verdict=state.last_qa_verdict or "fail",
                differences=tuple(state.last_qa_differences),
                suggestions=tuple(state.last_qa_suggestions),
                stop_reason=stop_reason,
                from_cache=True,
                seed=seed,
            )

        if artifact is None:
            # Resume in EVALUATE with no cached verdict and no fresh artifact —
            # we cannot move forward deterministically. Mark blocked so an
            # operator can attach or re-supply context.
            state.mark_blocked(
                "EVALUATE resume found no cached verdict and no run artifact to re-grade",
                tool_name="evaluator",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )

        # Persist the artifact + hash BEFORE invoking the evaluator so any
        # subsequent timeout / exception / transient QA error leaves a
        # recoverable trail on disk. The artifact must be stored verbatim
        # (no truncation) so the recomputed hash on resume matches the one
        # persisted here — truncation would silently invalidate the cache.
        #
        # Critical: when the artifact has CHANGED (hash differs from the
        # previously persisted one), the stale verdict from the previous
        # artifact MUST be cleared. Otherwise, if the evaluator times out
        # or transiently errors after persisting the new hash, ``--resume``
        # would see ``hash(new) == hash(new)`` paired with the cached pass
        # flag from ``hash(old)`` and incorrectly take the cache-hit path.
        if state.evaluate_artifact_hash != artifact_hash:
            state.last_qa_score = None
            state.last_qa_verdict = None
            state.last_qa_passed = None
            state.last_qa_differences = []
            state.last_qa_suggestions = []
            # The lateral cache also references this artifact (its
            # ``current_approach`` payload includes the run artifact), so a
            # stale persona suggestion produced for the old artifact must
            # not be reused. Invalidating here keeps the lateral and QA
            # caches in lockstep — a fresh EVALUATE on a new artifact will
            # transition through a fresh UNSTUCK_LATERAL too.
            state.last_lateral_persona = None
            state.last_lateral_approach_summary = None
            state.last_lateral_text = None
            state.lateral_input_hash = None
        state.evaluate_artifact = artifact
        state.evaluate_artifact_hash = artifact_hash
        self._save(state)

        phase_timeout = state.phase_timeout_seconds(AutoPhase.EVALUATE)
        # Cap the per-phase budget by the remaining top-level pipeline
        # deadline (Q00/ouroboros#779). Without this cap a late EVALUATE
        # entry could block past ``deadline_at`` and report
        # ``"evaluator timed out"`` instead of the canonical
        # ``pipeline_timeout`` blocker every other long-running phase
        # produces.
        capped_timeout = self._deadline_capped_timeout(state, phase_timeout)
        try:
            eval_result = await asyncio.wait_for(
                self.evaluator(seed, artifact), timeout=capped_timeout
            )
        except TimeoutError:
            # If the deadline expired during the call, surface the canonical
            # pipeline-timeout blocker so resume / status surfaces see the
            # same shape as every other deadline trip in the pipeline.
            if self._enforce_deadline(state):
                return self._result(
                    state,
                    ledger,
                    review=review,
                    blocker=state.last_error,
                    run_subagent=run_subagent,
                )
            state.mark_blocked(
                f"evaluator timed out after {capped_timeout:.0f}s",
                tool_name="evaluator",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        except Exception as exc:
            state.mark_blocked(f"evaluator raised: {exc}", tool_name="evaluator")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )

        if eval_result.error:
            state.mark_blocked(
                f"evaluator reported transient error: {eval_result.error}",
                tool_name="evaluator",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )

        state.last_qa_score = float(eval_result.score)
        state.last_qa_verdict = str(eval_result.verdict)
        # Persist the canonical pass flag explicitly (score >= threshold
        # per the QA contract). Resume reuses this boolean rather than
        # re-deriving ``passed`` from the verdict string — verdicts and
        # the passed flag can diverge (e.g. score 0.85, threshold 0.80
        # ⇒ passed=True but verdict="revise" when the LLM is conservative).
        state.last_qa_passed = bool(eval_result.passed)
        state.last_qa_differences = list(eval_result.differences)
        state.last_qa_suggestions = list(eval_result.suggestions)
        state.evaluate_artifact_hash = artifact_hash
        self._save(state)

        return await self._finalize_evaluate(
            state,
            ledger,
            review=review,
            run_subagent=run_subagent,
            passed=eval_result.passed,
            score=eval_result.score,
            verdict=eval_result.verdict,
            differences=eval_result.differences,
            suggestions=eval_result.suggestions,
            stop_reason=stop_reason,
            from_cache=False,
            seed=seed,
        )

    async def _finalize_evaluate(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
        passed: bool,
        score: float,
        verdict: str,
        differences: tuple[str, ...],
        suggestions: tuple[str, ...],
        stop_reason: str | None,
        from_cache: bool,
        seed: Seed | None = None,
    ) -> AutoPipelineResult:
        """Transition out of EVALUATE based on the resolved QA verdict.

        On QA pass → COMPLETE. On QA fail, if a lateral thinker is wired
        and ``state.complete_product`` is true, transition to UNSTUCK_LATERAL
        and invoke the persona-driven advisor (RFC #809 Phase 2.2); the
        final transition to BLOCKED carries the persona's summary in
        addition to the raw QA differences. Otherwise fall back to the
        Phase 2.1 behaviour: BLOCKED with QA differences only.
        """
        cache_suffix = " [cached]" if from_cache else ""
        if passed:
            state.transition(
                AutoPhase.COMPLETE,
                f"evaluator passed: {verdict} (score {score:.2f}){cache_suffix}"
                + (f"; ralph stop_reason={stop_reason}" if stop_reason else ""),
            )
            self._save(state)
            return self._result(state, ledger, review=review, run_subagent=run_subagent)

        if self.lateral_thinker is not None and state.complete_product and seed is not None:
            state.transition(
                AutoPhase.UNSTUCK_LATERAL,
                "QA failed; invoking lateral persona for verification reframing",
            )
            self._save(state)
            return await self._run_lateral(
                state,
                ledger,
                seed,
                qa_score=score,
                qa_verdict=verdict,
                qa_differences=differences,
                qa_suggestions=suggestions,
                cache_suffix=cache_suffix,
                review=review,
                run_subagent=run_subagent,
            )

        diff_preview = "; ".join(differences[:3]) if differences else ""
        sug_preview = "; ".join(suggestions[:3]) if suggestions else ""
        summary_parts = [f"evaluator did not pass: {verdict} (score {score:.2f}){cache_suffix}"]
        if diff_preview:
            summary_parts.append(f"differences: {diff_preview}")
        if sug_preview:
            summary_parts.append(f"suggestions: {sug_preview}")
        state.mark_blocked("; ".join(summary_parts), tool_name="evaluator")
        self._save(state)
        return self._result(
            state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
        )

    async def _run_lateral(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
        *,
        qa_score: float,
        qa_verdict: str,
        qa_differences: tuple[str, ...],
        qa_suggestions: tuple[str, ...],
        cache_suffix: str,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
    ) -> AutoPipelineResult:
        """Invoke the persona-driven lateral advisor and finalize as BLOCKED.

        Phase 2.2 advisory layer — when ``ouroboros_qa`` rules the run
        artifact did not satisfy the Seed AC, this method picks a persona
        deterministically from the QA-failure shape (via
        :func:`select_persona_for_qa_failure`) and asks
        ``ouroboros_lateral_think`` for a reframing prompt. The persona's
        output is persisted on :class:`AutoPipelineState` and surfaced in
        the final BLOCKED message so the operator (or a future P2.2b
        automated recovery layer) sees actionable next steps rather than
        the raw QA differences.

        Idempotent on resume: same persona + same QA shape hashes to the
        same ``lateral_input_hash``; a cache hit returns the persisted
        persona text without re-invoking the tool.

        On timeout / handler error / transient adapter error → BLOCKED with
        ``tool_name="lateral_thinker"`` so the resume contract (mapped by
        ``_recoverable_phase_for_tool``) lets ``--resume`` re-enter
        UNSTUCK_LATERAL.
        """
        import hashlib

        assert self.lateral_thinker is not None  # noqa: S101 — guarded by caller

        persona = select_persona_for_qa_failure(qa_differences, qa_suggestions)
        # Include the evaluate artifact hash in the cache key. The lateral
        # prompt's ``current_approach`` payload incorporates the run
        # artifact, so two EVALUATE rounds that grade different artifacts
        # but produce the same QA differences/suggestions must NOT share a
        # lateral cache entry — the persona's advice references the
        # specific artifact, and stale advice for the wrong artifact would
        # mislead the operator. The evaluate-artifact hash is the same one
        # ``_run_evaluate`` already uses to invalidate the QA cache; adding
        # it here keeps the two caches in lockstep.
        cache_key = "|".join(
            (
                persona.value,
                state.evaluate_artifact_hash or "",
                "::".join(qa_differences),
                "::".join(qa_suggestions),
            )
        )
        input_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()

        cache_hit = (
            state.lateral_input_hash == input_hash
            and state.last_lateral_text is not None
            and state.last_lateral_persona == persona.value
        )
        if cache_hit:
            return self._finalize_lateral(
                state,
                ledger,
                review=review,
                run_subagent=run_subagent,
                qa_score=qa_score,
                qa_verdict=qa_verdict,
                qa_differences=qa_differences,
                qa_suggestions=qa_suggestions,
                cache_suffix=cache_suffix,
                from_cache=True,
            )

        # Persist hash + persona BEFORE the call so a timeout/error path
        # leaves a recoverable trail. The actual persona text is filled in
        # after the call returns.
        state.lateral_input_hash = input_hash
        state.last_lateral_persona = persona.value
        state.last_lateral_approach_summary = None
        state.last_lateral_text = None
        self._save(state)

        run_artifact = state.evaluate_artifact or ""
        phase_timeout = state.phase_timeout_seconds(AutoPhase.UNSTUCK_LATERAL)
        capped_timeout = self._deadline_capped_timeout(state, phase_timeout)
        try:
            lateral_result = await asyncio.wait_for(
                self.lateral_thinker(
                    persona=persona,
                    qa_differences=qa_differences,
                    qa_suggestions=qa_suggestions,
                    run_artifact=run_artifact,
                ),
                timeout=capped_timeout,
            )
        except TimeoutError:
            if self._enforce_deadline(state):
                return self._result(
                    state,
                    ledger,
                    review=review,
                    blocker=state.last_error,
                    run_subagent=run_subagent,
                )
            state.mark_blocked(
                f"lateral_thinker timed out after {capped_timeout:.0f}s",
                tool_name="lateral_thinker",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )
        except Exception as exc:
            state.mark_blocked(f"lateral_thinker raised: {exc}", tool_name="lateral_thinker")
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )

        if lateral_result.error:
            state.mark_blocked(
                f"lateral_thinker reported transient error: {lateral_result.error}",
                tool_name="lateral_thinker",
            )
            self._save(state)
            return self._result(
                state, ledger, review=review, blocker=state.last_error, run_subagent=run_subagent
            )

        state.last_lateral_persona = lateral_result.persona or persona.value
        state.last_lateral_approach_summary = lateral_result.approach_summary
        state.last_lateral_text = lateral_result.text
        self._save(state)

        return self._finalize_lateral(
            state,
            ledger,
            review=review,
            run_subagent=run_subagent,
            qa_score=qa_score,
            qa_verdict=qa_verdict,
            qa_differences=qa_differences,
            qa_suggestions=qa_suggestions,
            cache_suffix=cache_suffix,
            from_cache=False,
        )

    def _finalize_lateral(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None,
        run_subagent: dict[str, Any] | None,
        qa_score: float,
        qa_verdict: str,
        qa_differences: tuple[str, ...],
        qa_suggestions: tuple[str, ...],
        cache_suffix: str,
        from_cache: bool,
    ) -> AutoPipelineResult:
        """Build the BLOCKED summary that surfaces the persona's reframing."""
        lateral_suffix = " [lateral cached]" if from_cache else ""
        persona_name = state.last_lateral_persona or "unknown"
        approach = state.last_lateral_approach_summary or ""
        summary_parts = [
            f"evaluator did not pass: {qa_verdict} (score {qa_score:.2f}){cache_suffix}",
            f"lateral persona {persona_name}{lateral_suffix}: {approach}"
            if approach
            else f"lateral persona {persona_name}{lateral_suffix} consulted",
        ]
        diff_preview = "; ".join(qa_differences[:3]) if qa_differences else ""
        if diff_preview:
            summary_parts.append(f"differences: {diff_preview}")
        sug_preview = "; ".join(qa_suggestions[:3]) if qa_suggestions else ""
        if sug_preview:
            summary_parts.append(f"suggestions: {sug_preview}")
        state.mark_blocked("; ".join(summary_parts), tool_name="lateral_thinker")
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
        seed: Seed | None = None,
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

        ``seed`` is required to recover from an unconfirmed plugin dispatch
        (``ralph_dispatch_mode == "plugin_pending"``); the dispatch is
        retried via :meth:`_handoff_to_ralph` so a crash *before* the bridge
        actually received the ``_subagent`` envelope does not falsely
        transition the auto session to COMPLETE
        (Q00/ouroboros#782 review-12 BLOCKING #2).
        """
        # Q00/ouroboros#782 review-12 BLOCKING #2: retry an interrupted
        # plugin dispatch BEFORE trusting the confirmed-plugin marker. A
        # ``"plugin_pending"`` checkpoint means the auto pipeline persisted
        # the dispatch intent but the actual ``ouroboros_ralph`` handler
        # call did not return a delegated_to_plugin response, so the bridge
        # may never have received the child-session envelope. Redispatch
        # with the same persisted lineage so any half-emitted events stay
        # correlated.
        if state.ralph_dispatch_mode == "plugin_pending":
            if seed is not None and self.ralph_starter is not None:
                state.ralph_dispatch_mode = None
                state.ralph_job_id = None
                self._save(state)
                return await self._handoff_to_ralph(
                    state, ledger, seed, review=review, run_subagent=None
                )
            state.mark_blocked(
                "ralph plugin dispatch was interrupted before confirmation; "
                "resume could not retry without a persisted Seed",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        if state.ralph_dispatch_mode == "plugin":
            state.run_handoff_guidance = (
                state.run_handoff_guidance
                or "Ralph loop delegated to the OpenCode plugin child session. "
                "Track progress through the OpenCode Task widget; this auto "
                "session will not block on the loop's completion."
            )
            # Q00/ouroboros#782 review-13 BLOCKING #1: a confirmed plugin
            # dispatch is a one-shot side effect — the bridge already received
            # the ``_subagent`` envelope and may have already spawned the
            # child session. Re-emitting the persisted ``state.run_subagent``
            # on resume can trigger a duplicate OpenCode child session via
            # ``meta["_subagent"]`` in :class:`AutoHandler.handle`. Clear the
            # persisted envelope here so neither this ``_result(...)`` call
            # nor any future re-resume replays it. ``state.run_subagent``
            # is typed as a dict, so we reset to ``{}`` rather than ``None``;
            # ``_result()`` treats the empty dict as falsy and emits ``None``.
            state.run_subagent = {}
            state.transition(
                AutoPhase.COMPLETE,
                "resumed OpenCode plugin Ralph delegation checkpoint",
            )
            self._save(state)
            return self._result(state, ledger, review=review)

        if self.ralph_resumer is not None and state.ralph_job_id:
            return await self._poll_ralph_job(state, ledger, seed, review=review)

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
        seed: Seed,
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
        ralph_mirror_task: asyncio.Task[None] | None = None
        if (
            self.store is not None
            and state.ralph_dispatch_mode != "plugin"
            and state.ralph_job_id is not None
        ):
            event_store = getattr(self.ralph_resumer, "job_event_store", None)
            if event_store is not None:
                ralph_mirror_task = asyncio.create_task(
                    mirror_ralph_job_events(
                        state,
                        self.store,
                        event_store,
                        state.ralph_job_id,
                    )
                )
        try:
            poll_call = self.ralph_resumer(job_id=state.ralph_job_id)
            if state.deadline_at is None:
                ralph_meta = await poll_call
            else:
                # Q00/ouroboros#782 review-12 BLOCKING #1: floor at
                # ``_RALPH_RESUME_PEEK_SECONDS`` so an already-terminal Ralph
                # job can be reconciled even when the top-level deadline has
                # expired. ``asyncio.wait_for`` with timeout=0 cancels the
                # coroutine before it can read the first snapshot, which
                # would silently turn a completed loop into ``pipeline_timeout``.
                remaining = state.deadline_at - time.monotonic()
                poll_timeout = max(remaining, _RALPH_RESUME_PEEK_SECONDS)
                ralph_meta = await asyncio.wait_for(poll_call, timeout=poll_timeout)
        except TimeoutError:
            await _cancel_ralph_status_mirror(ralph_mirror_task)
            if self._enforce_deadline(state):
                return self._result(state, ledger, review=review, blocker=state.last_error)
            state.mark_blocked(
                "ralph resume poll timed out before terminal status",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        except Exception as exc:
            await _cancel_ralph_status_mirror(ralph_mirror_task)
            state.mark_failed(f"ralph resume poll failed: {exc}", tool_name="ralph_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        await _drain_ralph_status_mirror(ralph_mirror_task)
        if not isinstance(ralph_meta, dict):
            state.mark_failed(
                f"ralph resumer returned {type(ralph_meta).__name__}, expected dict",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        terminal_status = _optional_str(ralph_meta.get("terminal_status"))
        stop_reason = _optional_str(ralph_meta.get("stop_reason"))
        current_generation = _ralph_current_generation_from_meta(ralph_meta)
        if terminal_status is not None:
            state.ralph_job_status = terminal_status
        if stop_reason is not None:
            state.ralph_stop_reason = stop_reason
        if current_generation is not None:
            state.ralph_current_generation = current_generation
        if terminal_status == "completed":
            return await self._evaluate_or_complete(
                state,
                ledger,
                seed,
                review=review,
                run_subagent=None,
                stop_reason=stop_reason,
                ralph_result_text=_artifact_text(ralph_meta.get("result_text")),
                resumed=True,
            )
        # Q00/ouroboros#782 review-10 BLOCKING #2: ``terminal_status ==
        # "cancelled"`` must map to BLOCKED with the pinned
        # ``RALPH_CANCEL_BLOCKER_REASON`` — same as the live ``_handoff_to_ralph``
        # path. Falling through to the generic failure branch would mark a
        # user-cancelled session FAILED on resume, regressing the live-path
        # contract for a normal user action.
        if terminal_status == "cancelled":
            if state.phase is not AutoPhase.BLOCKED:
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

    def _remaining_deadline_seconds(self, state: AutoPipelineState) -> float | None:
        """Return remaining pipeline budget in seconds, if a deadline is armed."""
        if state.deadline_at is None or state.is_terminal():
            return None
        return max(0.0, state.deadline_at - time.monotonic())

    def _phase_timeout_with_deadline(self, state: AutoPipelineState, phase_timeout: float) -> float:
        """Cap a phase-local timeout by the remaining top-level pipeline budget."""
        remaining = self._remaining_deadline_seconds(state)
        if remaining is None:
            return phase_timeout
        return max(0.0, min(phase_timeout, remaining))

    def _deadline_timeout_elapsed(self, state: AutoPipelineState) -> bool:
        """Return True when a wait_for timeout should be classified as pipeline_timeout."""
        return state.deadline_at is not None and state.is_deadline_expired()

    def _mark_pipeline_timeout(self, state: AutoPipelineState) -> None:
        """Persist a top-level deadline BLOCKED state after an in-flight await overruns."""
        remaining = (state.deadline_at - time.monotonic()) if state.deadline_at is not None else 0.0
        message = (
            f"pipeline_timeout: deadline exceeded by "
            f"{abs(remaining):.1f}s during {state.phase.value}"
        )
        state.last_tool_name = PIPELINE_DEADLINE_TOOL_NAME
        state.mark_blocked(message, tool_name=PIPELINE_DEADLINE_TOOL_NAME)
        self._save(state)

    async def _reattach_ralph_job(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult:
        """Wait on an already-dispatched Ralph job rather than dispatching a duplicate.

        Q00/ouroboros#782 review-6 BLOCKING #2. Resume after a crash that
        happened between ``persist_started_ralph`` saving ``ralph_job_id``
        and the original ``HandlerRalphStarter`` finishing its terminal
        wait. Calls ``ralph_starter`` with ``attach_job_id=state.ralph_job_id``
        so no fresh ``mcp.subagent.dispatched`` / job-create side effect
        runs; the same terminal-status mapping handles the result.

        Intentionally does NOT call ``_enforce_deadline`` first
        (Q00/ouroboros#782 review-7 BLOCKING #1): re-attach is just
        observing an already-dispatched job's terminal state, so a long
        offline gap that pushed past ``deadline_at`` must NOT strand a
        successfully completed Ralph job as a false ``pipeline_timeout``.
        """
        assert self.ralph_starter is not None  # noqa: S101 - guarded by caller
        ralph_mirror_task: asyncio.Task[None] | None = None
        if (
            self.store is not None
            and state.ralph_dispatch_mode != "plugin"
            and state.ralph_job_id is not None
        ):
            event_store = getattr(self.ralph_starter, "job_event_store", None)
            if event_store is not None:
                ralph_mirror_task = asyncio.create_task(
                    mirror_ralph_job_events(
                        state,
                        self.store,
                        event_store,
                        state.ralph_job_id,
                    )
                )
        try:
            ralph_meta = await self.ralph_starter(
                None,  # type: ignore[arg-type]
                lineage_id=state.ralph_lineage_id or "",
                attach_job_id=state.ralph_job_id,
            )
        except Exception as exc:
            await _cancel_ralph_status_mirror(ralph_mirror_task)
            state.mark_failed(
                f"ralph re-attach failed: {exc}",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        await _drain_ralph_status_mirror(ralph_mirror_task)
        if not isinstance(ralph_meta, dict):
            state.mark_failed(
                f"ralph re-attach returned {type(ralph_meta).__name__}, expected dict",
                tool_name="ralph_starter",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        terminal_status = _optional_str(ralph_meta.get("terminal_status"))
        stop_reason = _optional_str(ralph_meta.get("stop_reason"))
        current_generation = _ralph_current_generation_from_meta(ralph_meta)
        if terminal_status is not None:
            state.ralph_job_status = terminal_status
        if stop_reason is not None:
            state.ralph_stop_reason = stop_reason
        if current_generation is not None:
            state.ralph_current_generation = current_generation
        if terminal_status == "completed":
            state.transition(
                AutoPhase.COMPLETE,
                f"ralph loop completed on re-attach ({stop_reason or 'qa passed'})",
            )
            self._save(state)
            return self._result(state, ledger)
        if terminal_status == "cancelled":
            if state.phase is not AutoPhase.BLOCKED:
                state.mark_blocked(RALPH_CANCEL_BLOCKER_REASON, tool_name="ralph_starter")
                self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        if terminal_status == "failed" and stop_reason in _RALPH_BLOCKED_STOP_REASONS:
            state.mark_blocked(stop_reason, tool_name="ralph_starter")
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        message = (
            f"ralph loop failed on re-attach: {stop_reason}"
            if stop_reason
            else f"ralph loop failed on re-attach: terminal_status={terminal_status or 'unknown'}"
        )
        state.mark_failed(message, tool_name="ralph_starter")
        self._save(state)
        return self._result(state, ledger, blocker=state.last_error)

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
            last_qa_score=state.last_qa_score,
            last_qa_verdict=state.last_qa_verdict,
            last_qa_differences=tuple(state.last_qa_differences),
            last_qa_suggestions=tuple(state.last_qa_suggestions),
            last_lateral_persona=state.last_lateral_persona,
            last_lateral_approach_summary=state.last_lateral_approach_summary,
            last_lateral_text=state.last_lateral_text,
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
        if (
            not state.run_start_attempted
            or state.run_handoff_status not in UNKNOWN_HANDOFF_STATUSES
        ):
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
        if (
            not state.run_start_attempted
            or state.run_handoff_status not in UNKNOWN_HANDOFF_STATUSES
        ):
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
    state: AutoPipelineState, *, status: str = UNKNOWN_NO_HANDLE_STATUS
) -> None:
    if status == UNKNOWN_NO_HANDLE_STATUS and state.run_handoff_status in UNKNOWN_HANDOFF_STATUSES:
        status = state.run_handoff_status
    state.run_handoff_status = status
    state.run_handoff_guidance = unknown_handoff_guidance(status)


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
    if tool_name == "evaluator":
        # RFC #809 Phase 2.1: when the evaluator times out or the QA handler
        # returns a transient infrastructure error, the session is marked
        # BLOCKED with this tool name. ``--resume`` must dispatch back into
        # EVALUATE so the cached verdict (if present) or a fresh evaluator
        # call (if not) can drive the session forward instead of leaving it
        # stranded in a non-resumable BLOCKED state.
        return AutoPhase.EVALUATE
    if tool_name == "lateral_thinker":
        # RFC #809 Phase 2.2: timeout / transient error in the lateral
        # advisor blocks with this tool name. Resume re-enters
        # UNSTUCK_LATERAL where the cached persona suggestion (if any)
        # short-circuits or a fresh lateral_think call retries.
        return AutoPhase.UNSTUCK_LATERAL
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


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _last_int(value: object) -> int | None:
    if not isinstance(value, list):
        return None
    for item in reversed(value):
        found = _optional_int(item)
        if found is not None:
            return found
    return None


def _ralph_current_generation_from_meta(meta: dict[str, Any]) -> int | None:
    current_generation = _optional_int(meta.get("current_generation"))
    if current_generation is not None:
        return current_generation
    generations_generation = _last_int(meta.get("generations"))
    if generations_generation is not None:
        return generations_generation
    return _optional_int(meta.get("iterations"))


async def _drain_ralph_status_mirror(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
    except TimeoutError:
        await _cancel_ralph_status_mirror(task)
    except Exception:
        pass


async def _cancel_ralph_status_mirror(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _artifact_text(value: object) -> str | None:
    """Return ``value`` verbatim when it is a string (including ``""``), else None.

    Distinct from :func:`_optional_str` because an artifact graded by EVALUATE
    is a valid input even when empty: a Ralph job whose output is
    intentionally empty must still be evaluated against the Seed AC. Returning
    None for ``""`` would cause ``_evaluate_or_complete`` to skip EVALUATE
    and silently transition to COMPLETE.
    """
    return value if isinstance(value, str) else None
