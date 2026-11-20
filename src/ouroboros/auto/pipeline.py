"""Full-quality AutoPipeline supervisor skeleton."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
import inspect
import re
import time
from typing import Any, Protocol

import structlog

from ouroboros.auto import expectations as auto_expectations
from ouroboros.auto.adapters import EvaluateResult
from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.artifact_state import artifact_state_for_result as _artifact_state_for_result
from ouroboros.auto.blocker_attribution import record_authoring_backend
from ouroboros.auto.checkpoint_commits import checkpoint_final_auto
from ouroboros.auto.degraded_seed import (
    degraded_seed_not_executed_blocker,
    degraded_seed_runs_as_stated,
    degraded_seed_safety_blocker,
    is_degraded_seed_not_executed_stop,
)
from ouroboros.auto.domain_inference import derive_domain_from_ledger
from ouroboros.auto.domain_profile import DEFAULT_REGISTRY
from ouroboros.auto.execution_acceptance import normalize_execution_acceptance
from ouroboros.auto.grading import deterministic_floor
from ouroboros.auto.handoff_contract import (
    IDEMPOTENCY_KEY_FIELD,
    IDEMPOTENCY_KWARG_NAME,
    RETRY_GUIDANCE_PHRASE,
    RUN_HANDOFF_STARTED_STATUS,
    UNKNOWN_HANDOFF_STATUSES,
    UNKNOWN_NO_HANDLE_STATUS,
    UNKNOWN_TIMEOUT_STATUS,
    unknown_handoff_guidance,
)
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import AssumptionRecord, SeedDraftLedger
from ouroboros.auto.ledger_seed import (
    PARTIAL_SEED_GENERATION_MODE,
    brownfield_context_from_cwd,
    partial_seed_from_evidence,
    synthesize_seed_from_ledger,
    with_ledger_carryover,
)
from ouroboros.auto.listeners import RALPH_CANCEL_BLOCKER_REASON
from ouroboros.auto.phase_clock import wait_for_pausable
from ouroboros.auto.progress import AutoProgressCallback, AutoProgressEvent
from ouroboros.auto.ralph_resume import reconcile_ralph_checkpoint
from ouroboros.auto.recovery_plan import (
    AutoRecoveryPlan,
    RecoveryPlanAction,
)
from ouroboros.auto.reference_candidate_bridge import (
    apply_requirement_distillation_to_ledger,
)
from ouroboros.auto.resume_routing import (
    recoverable_phase_for_tool as _recoverable_phase_for_tool,
)
from ouroboros.auto.retired_phases import (
    mark_retired_complete_product_run,
    mark_retired_phase,
)
from ouroboros.auto.state import (
    DEFAULT_TIMEOUT_SECONDS_BY_PHASE,
    AutoCommitPolicy,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
    SeedOrigin,
    utc_now_iso,
)
from ouroboros.auto.task_class_application import apply_default_ac_template
from ouroboros.auto.terminal_events import emit_blocked_session_event
from ouroboros.auto.trace_export import best_effort_export_trace
from ouroboros.core.seed import Seed
from ouroboros.orchestrator.runtime_evidence import RuntimeEvidence
from ouroboros.runtime.watchdog import (
    WATCHDOG_STOP_REASON_CODE,
    Watchdog,
)

# RFC #809 Phase 2.2b — Stack 1 of 2 scope and invariants.
#
# **Scope of this module's recovery wiring.** P2.2b landed in two stacked
# PRs. Stack 1 added the deterministic *guards* and *multi-persona
# advisory* path. Stack 2 consumes the typed ``AutoRecoveryPlan`` produced
# by lateral advice: safe ``ralph_redispatch`` plans are routed into a
# fresh Ralph handoff and then back through EVALUATE, while manual or
# unsafe plans still terminate as BLOCKED. The four guards
# (``MAX_EVALUATE_ROUNDS``, same-fingerprint twice, per-persona-once,
# ``state.deadline_at``) bound that closed loop so redispatch cannot loop
# unboundedly or revisit a stale persona.
#
# **Operator-choice cue.** Suffixed onto every recovery-loop BLOCKED
# message so the operator sees their next move without having to read
# the rest of the surface. Two explicit choices, intentionally avoiding
# any "let the system rewrite the spec" option: keep the spec contract
# under human control.
#
# Earlier drafts of this cue advertised a third path ("relax AC via --resume
# + edited seed"). That guidance was wrong: ``AutoPipeline.run()`` resumes
# late-phase blockers (EVALUATE / UNSTUCK_LATERAL) by reconstructing the
# Seed from ``state.seed_artifact``, which is always populated on the
# normal auto path. Editing the on-disk ``state.seed_path`` file therefore
# has no effect — the next resume re-grades against the same in-state AC
# and loops back to the identical blocker. Until the resume contract is
# changed to honor a freshly-edited seed file on late-phase resume (a
# separate PR with its own end-to-end coverage), only two operator paths
# are actually functional, and the cue must say so.
_RECOVERY_BLOCKED_CHOICES: str = (
    "next: (1) re-interview with a refined goal (the in-state Seed cannot "
    "be edited mid-session); (2) abandon this session"
)


class SeedGenerator(Protocol):
    """Protocol for seed-generator callables.

    Implementations accept an optional ``force`` keyword that bypasses the
    ambiguity-score gate inside ``SeedGenerator.generate`` (see
    ``bigbang/seed_generator.py``). The auto pipeline sets ``force=True``
    when ``state.interview_closure_mode`` indicates the interview closed
    on ledger evidence rather than backend agreement
    (``ledger_only`` / ``safe_default``) — under SSOT #1157 *Closure Policy*
    (2026-05-27), the ledger's structural completeness IS the acceptance
    signal, so the persisted backend ambiguity score is acknowledged-stale
    by design and must not re-block at the SEED_GENERATION boundary.

    Implementations that don't honor ``force`` should still accept it for
    interface compatibility; the kwarg defaults to ``False`` so legacy
    ``mutual_agreement`` closures behave exactly as before.
    """

    async def __call__(self, session_id: str, *, force: bool = False) -> Seed: ...


def _seed_generator_accepts_force(seed_generator: SeedGenerator) -> bool:
    """Return True if ``seed_generator`` advertises a ``force`` kwarg.

    ``AutoPipeline`` uses this to keep the SSOT #1157 ledger-primary force
    contract backwards-compatible with legacy ``async def(session_id)``
    callables (the dominant pattern in tests/unit/). Detection is via
    ``inspect.signature`` rather than EAFP — calling unconditionally and
    catching ``TypeError`` would mask genuine signature errors inside the
    callable's body.
    """
    try:
        signature = inspect.signature(seed_generator)
    except (TypeError, ValueError):  # builtins / non-introspectable wrappers
        return False
    parameters = signature.parameters
    if "force" in parameters:
        return True
    # A callable that accepts arbitrary ``**kwargs`` can also take ``force``.
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _is_authoring_backend_unavailable(exc: Exception) -> bool:
    """Return True for provider/config failures that ledger Seed fallback may bypass."""
    text = str(exc).casefold()
    markers = (
        "config.toml",
        "profile",
        "codex",
        "provider",
        "api key",
        "authentication",
        "rate limit",
        "connection refused",
        "network",
    )
    return any(marker in text for marker in markers)


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
RuntimeProbeRunner = Callable[[AutoPipelineState], Awaitable[tuple[RuntimeEvidence, ...]]]

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
DETACHED_STATUS = "detached"
_RESUME_EXPIRED_MESSAGE = "pipeline_timeout (deadline expired before resume)"
# Mirrors RalphHandler.MIN_MAX_TOTAL_SECONDS. The auto layer checks this before
# dispatch so an insufficient top-level pipeline budget remains a pipeline
# timeout, not a Ralph argument-validation failure.
_MIN_RALPH_MAX_TOTAL_SECONDS = 1.0
# Mirrors RalphHandler per-iteration bounds
# (MIN_/MAX_PER_ITERATION_TIMEOUT_SECONDS). The auto layer sizes
# ``per_iteration_timeout_seconds`` to the *remaining pipeline budget* so a
# single ``evolve_step`` cannot block past ``deadline_at`` (Q00/ouroboros#779)
# — RalphLoopRunner only checks ``max_total_seconds`` at iteration boundaries,
# so without an upper bound the iteration could overshoot the deadline.
#
# It must NOT, however, cap below that budget. A ``complete_product`` gen-1
# iteration legitimately runs both the implementation pass and the evolve
# verification pass in one ``evolve_step``; the standalone Ralph default
# (1800s) is far too small for that and was observed killing a
# still-progressing iteration with ~5400s of pipeline budget left (cli-todo
# live R2: 14/14 ACs complete, then cancelled at 1800s -> failed/iteration_timeout
# despite the working product on disk). The ceiling is therefore the
# Ralph-supported maximum, not the standalone default; ``max_total_seconds``
# still bounds the whole loop.
_MIN_RALPH_PER_ITERATION_SECONDS = 30.0
# Standalone Ralph default — kept for parity with RalphHandler, but NOT used as
# the auto-path per-iteration ceiling (see ``_MAX_RALPH_PER_ITERATION_SECONDS``).
_DEFAULT_RALPH_PER_ITERATION_SECONDS = 1800.0
# Mirrors RalphHandler.MAX_PER_ITERATION_TIMEOUT_SECONDS.
_MAX_RALPH_PER_ITERATION_SECONDS = 7200.0

# Q00/ouroboros#782 review-12 BLOCKING #1: when the top-level deadline has
# already expired but a persisted Ralph job awaits reconciliation, give the
# resume poller a brief grace window so an already-terminal job is detected
# (snapshot returns immediately) before ``_enforce_deadline`` trips
# ``pipeline_timeout``. ``asyncio.wait_for(coro, 0)`` cancels the coroutine
# before it can read the first snapshot, so the inner ``get_snapshot`` would
# never run without this floor — silently demoting a legitimately completed
# Ralph loop to a false ``pipeline_timeout`` BLOCKED.
_RALPH_RESUME_PEEK_SECONDS = 1.0
_SYNCHRONOUS_RUN_COMPLETION_GRACE_SECONDS = 300.0

# RFC #1256 §I4 — bounded composition-root drain budget for typed
# ``auto.interview.*`` lifecycle events scheduled by
# ``AutoInterviewDriver`` as background tasks. The drain runs OUTSIDE
# the interview phase's ``asyncio.wait_for(interview_timeout)``
# boundary (see ``_drain_interview_observer_events``), so a slow
# EventStore cannot weaken phase-deadline or cancellation contracts
# (bot review on commit ``c5549124``, req_1779938459_153, reproduced
# the contract failure when the drain ran inside ``run()``). The
# budget is generous enough to cover the driver's per-event
# fail-open bound (1.0 s) plus a small headroom margin for
# two-event sessions while staying well below any realistic pipeline phase timeout —
# the drain itself is bounded by ``asyncio.wait_for`` and downgrades
# timeouts to a typed ``auto.interview.observer_drain_timed_out``
# structlog warning.
_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS = 1.5


log = structlog.get_logger(__name__)


def _runtime_probe_evidence_from_state(
    state: AutoPipelineState,
) -> tuple[RuntimeEvidence, ...]:
    """Return validated persisted runtime probe evidence."""
    evidence: list[RuntimeEvidence] = []
    for item in state.runtime_probe_evidence:
        evidence.append(RuntimeEvidence.from_dict(item))
    return tuple(evidence)


@dataclass(frozen=True, slots=True)
class AutoPipelineResult:
    """Structured AutoPipeline result for CLI/MCP surfaces."""

    status: str
    auto_session_id: str
    phase: str
    seed_path: str | None = None
    seed_origin: str = SeedOrigin.NONE.value
    interview_session_id: str | None = None
    execution_id: str | None = None
    job_id: str | None = None
    run_session_id: str | None = None
    execution_job_status: str | None = None
    execution_job_error: str | None = None
    execution_job_message: str | None = None
    run_subagent: dict[str, Any] | None = None
    current_round: int = 0
    pending_question: str | None = None
    interview_closure_mode: str | None = None
    # L1-d / L1-e / #1171: active task class derived from the ledger and
    # used to inject default AC templates into the Seed. None when the
    # inference was ambiguous, unmatched, or skipped (legacy paths).
    active_task_class: str | None = None
    last_progress_message: str | None = None
    last_progress_at: str | None = None
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
    open_questions: tuple[str, ...] = ()  # the Seed's, verbatim; never in worker input
    # The Seed's ``sourced_scope.assumptions``: auto's unconfirmed answers the worker sees.
    assumed_answers: tuple[str, ...] = ()
    # PR-C2 / #1157: ``assumptions`` with ledger source and confidence per entry.
    assumption_sources: tuple[AssumptionRecord, ...] = ()
    non_goals: tuple[str, ...] = ()
    defaulted_sections: tuple[str, ...] = ()
    blocker: str | None = None
    stop_reason_code: str | None = None
    runtime_backend: str | None = None
    opencode_mode: str | None = None
    efficiency_mode: str = "adaptive"
    frugality_assurance: str = "observe"
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
    # L3-2 / #1176: runtime acceptance evidence captured by the
    # ``probe_runner`` callback wired on ``AutoPipeline``. Empty when no
    # probe runner was provided (backwards compatibility) or when the
    # caller intentionally had no bound runtime probe. When present,
    # failing evidence blocks PRODUCT_COMPLETE before the result envelope
    # is returned, so this field is both surface evidence and a completion
    # grade input rather than a passive annotation.
    runtime_probe_evidence: tuple[RuntimeEvidence, ...] = ()
    checkpoint_commits: tuple[dict[str, Any], ...] = ()
    # #1257 PR-C — degraded-recovery terminal surface.
    #
    # When the interview-phase deadline rerouted into PR-A's
    # ``partial_seed_from_evidence``, the resulting Seed carries
    # ``metadata.degraded = True`` and a list of unresolved sections.
    # A degraded Seed without the user's goal on record stops before RUN
    # with these typed result fields:
    #
    # * ``partial_product``: True iff the terminal is a deadline-recovery
    #   partial product (not a fully verified run).
    # * ``partial_product_reason``: free-form provenance string mirrored
    #   from ``seed.metadata.recovery_reason`` (e.g.
    #   ``"interview_phase_deadline"``).
    # * ``partial_unresolved_slots``: ledger sections still unresolved at
    #   deadline; surfaced verbatim so MCP/CLI clients can convert them
    #   into next-step hints. Empty for normal-path completions.
    #
    # ``stop_reason_code`` remains an *error-class* code (driven by
    # ``state.last_error_code``) and stays at ``None`` for a partial
    # product — a deadline-recovery completion is a typed alternative
    # success, not a blocker.
    partial_product: bool = False
    partial_product_reason: str | None = None
    partial_unresolved_slots: tuple[str, ...] = ()
    # #1509: final-status surfaces must distinguish orchestration state
    # from artifact state so BLOCKED/FAILED sessions with generated files are
    # not mistaken for verified completion. Values are intentionally stringly
    # typed for stable CLI/MCP wire compatibility.
    artifact_state: str | None = None


@dataclass(slots=True)
class AutoPipeline:
    """Coordinate the interview, Seed generation, and run handoff.

    A written Seed goes straight to the run (#3064): there is no Seed review,
    grade, repair, or pre-run check between them. The run job verifies each
    acceptance criterion after it executes.
    """

    interview_driver: AutoInterviewDriver
    seed_generator: SeedGenerator
    run_starter: RunStarter | None = None
    store: AutoStore | None = None
    seed_saver: SeedSaver | None = None
    seed_loader: SeedLoader | None = None
    skip_run: bool = False
    attach_execution_id: str | None = None
    attach_job_id: str | None = None
    attach_run_session_id: str | None = None
    attach_source: str | None = None
    reconcile_run: bool = False
    reconcile_source: str | None = None
    seed_timeout_seconds: float = float(
        DEFAULT_TIMEOUT_SECONDS_BY_PHASE[AutoPhase.SEED_GENERATION.value]
    )
    run_start_timeout_seconds: float = 60.0
    progress_callback: AutoProgressCallback | None = None
    ralph_resumer: RalphStarter | None = None
    # L2-2 / #1172: wall-clock watchdog for the session. ``None`` means
    # no watchdog (legacy behaviour). When wired, the pipeline checks
    # ``watchdog.check(...)`` at ``run()`` entry; on fire the session
    # transitions to BLOCKED with
    # ``stop_reason_code = "watchdog_wall_clock_exceeded"``. The cancel
    # event has already been appended to the EventStore by the watchdog
    # itself, so the pipeline does no further side effect on fire.
    watchdog: Watchdog | None = None
    expectations_researcher: auto_expectations.ExpectationsResearcher | None = None
    _last_emitted_phase: str | None = field(default=None, init=False, repr=False)
    _last_probe_evidence: tuple[RuntimeEvidence, ...] = field(default=(), init=False, repr=False)
    # Recursive resume paths export the trace exactly once at the outermost
    # terminal return.
    _run_depth: int = field(default=0, init=False, repr=False)
    _active_ledger: SeedDraftLedger | None = field(default=None, init=False, repr=False)

    async def run(self, state: AutoPipelineState) -> AutoPipelineResult:
        """Run the pipeline and, once at the outermost terminal, export a trace.

        Thin re-entrancy-aware wrapper around :meth:`_run_pipeline`. The auto
        pipeline recurses through ``run`` on resume boundaries; the depth
        counter ensures the best-effort A2 interview-trace projection runs a
        single time at the outermost frame when the session is terminal. The
        export never raises into the run (see
        :func:`ouroboros.auto.trace_export.best_effort_export_trace`).
        """
        self._run_depth += 1
        try:
            result = await self._run_pipeline(state)
        finally:
            self._run_depth -= 1
        if self._run_depth == 0 and state.is_terminal():
            await best_effort_export_trace(
                state,
                self._active_ledger
                or (
                    SeedDraftLedger.from_dict(state.ledger)
                    if state.ledger
                    else SeedDraftLedger.from_goal(state.goal)
                ),
                event_store=getattr(self.interview_driver, "event_store", None),
            )
            await emit_blocked_session_event(self._emit_runtime_event, state, result)
        return result

    async def _run_pipeline(self, state: AutoPipelineState) -> AutoPipelineResult:
        """Run a bounded auto pipeline using injected side-effecting dependencies."""
        self._last_emitted_phase = None
        # Prevent a reused pipeline from leaking prior runtime-probe evidence.
        self._last_probe_evidence = _runtime_probe_evidence_from_state(state)
        # Share the progress observer with the long-running interview driver.
        self.interview_driver.progress_callback = self.progress_callback
        ledger = (
            SeedDraftLedger.from_dict(state.ledger)
            if state.ledger
            else SeedDraftLedger.from_goal(state.goal)
        )
        self._active_ledger = ledger
        # Compatibility migration runs before a fired watchdog can
        # must not turn an obsolete persisted phase into an unrelated timeout.
        if mark_retired_phase(state):
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        # L2-2 / #1172: wall-clock watchdog check.
        # Runs once per ``run()`` entry — both fresh sessions whose
        # ``created_at`` is too long ago (resume after budget elapsed)
        # and live sessions whose execution chose to re-enter the loop
        # are caught here. The watchdog appends its ``runtime.watchdog.cancel``
        # event itself; the pipeline only translates the decision into
        # the BLOCKED transition + envelope ``stop_reason_code``.
        watchdog_result = await self._check_watchdog(state, ledger)
        if watchdog_result is not None:
            return watchdog_result
        if self.skip_run and not state.skip_run:
            state.skip_run = True
        # Q00/ouroboros#809 P3 PR-4: active domain profile injection is gated
        # to the interview phases below, where ``interview_driver.answerer`` is
        # actually used.  Later resume/result paths must not depend on the
        # mutable process-local profile registry.
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
        # Preserve a dispatched Ralph checkpoint across an expired deadline;
        # all other resumed work remains subject to the top-level gate.
        if (
            state.deadline_at is not None
            and not state.is_terminal()
            and state.is_deadline_expired()
            and not _has_reconciliable_ralph_resume_checkpoint(state)
        ):
            state.last_tool_name = PIPELINE_DEADLINE_TOOL_NAME
            state.mark_blocked(
                _RESUME_EXPIRED_MESSAGE,
                tool_name=PIPELINE_DEADLINE_TOOL_NAME,
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
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
            resume_phase = (
                AutoPhase.RALPH_HANDOFF
                if state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
                and _has_reconciliable_ralph_resume_checkpoint(state)
                else _recoverable_phase_for_tool(state.last_tool_name)
            )
            if resume_phase is None:
                return self._result(state, ledger, blocker=state.last_error)
            previous_phase = state.phase
            has_ralph_checkpoint_to_reconcile = resume_phase is AutoPhase.RALPH_HANDOFF and (
                state.ralph_dispatch_mode == "plugin"
                or (state.ralph_job_id is not None and self.ralph_resumer is not None)
            )
            retry_ralph_handoff = (
                resume_phase is AutoPhase.RALPH_HANDOFF
                and state.last_error != RALPH_CANCEL_BLOCKER_REASON
                and not has_ralph_checkpoint_to_reconcile
            )
            state.recover(
                resume_phase,
                f"resuming {resume_phase.value} after {previous_phase.value}: {state.last_error or 'no error recorded'}",
            )
            if retry_ralph_handoff:
                # Resumable Ralph blockers (for example iteration_timeout)
                # should retry Ralph after the operator fixes the cause. Do
                # not poll/reuse the terminal job that produced the blocker;
                # otherwise --resume loops back to the same terminal status.
                # User-cancelled jobs are excluded above so their explicit
                # cancellation blocker remains preserved and non-retried.
                state.ralph_job_id = None
                state.ralph_lineage_id = None
                state.ralph_dispatch_mode = None
                state.ralph_job_status = None
                state.ralph_stop_reason = None
                state.ralph_current_generation = None
                state.ralph_last_event_at = None
                state.run_handoff_guidance = None
                state.run_handoff_status = "ralph_retry_after_blocker"
            # Legacy auto sessions saved before #779 had no
            # ``deadline_at_epoch``, and ``from_dict()`` deliberately leaves
            # the deadline unset for terminal phases. After recovering them
            # back to a working phase, arm the deadline so subsequent
            # ``_enforce_deadline`` checks are not silent no-ops for the
            # rest of this resume (#790 review-4). ``arm_deadline`` is
            # idempotent — non-legacy resumes are unaffected.
            state.arm_deadline()
            self._save(state)

        if state.phase is AutoPhase.RALPH_HANDOFF and _has_reconciliable_ralph_resume_checkpoint(
            state
        ):
            return await reconcile_ralph_checkpoint(self, state, ledger)

        interview_result = await self._run_interview_phase(state, ledger)
        if interview_result is not None:
            return interview_result

        seed_result = await self._run_seed_phase(state, ledger)
        if isinstance(seed_result, AutoPipelineResult):
            return seed_result
        seed = seed_result

        terminal_resume_result = await self._run_terminal_resume_phase(state, ledger)
        if terminal_resume_result is not None:
            return terminal_resume_result

        return await self._run_execution_phase(state, ledger, seed)

    async def _run_interview_phase(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult | None:
        # A dispatched Ralph checkpoint is reconciled before deadline blocking.
        if not _has_reconciliable_ralph_resume_checkpoint(state) and self._enforce_deadline(state):
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
                no_backend_closure = state.interview_closure_mode in {
                    "ledger_only_no_backend",
                    "safe_default_no_backend",
                }
                ledger_ready = ledger.is_seed_ready()
                if not state.interview_session_id and not (no_backend_closure and ledger_ready):
                    state.mark_blocked(
                        "Completed interview is missing interview_session_id",
                        tool_name="auto_pipeline",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                if not ledger_ready:
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
                _answerer = getattr(self.interview_driver, "answerer", None)
                if _answerer is not None:
                    try:
                        _apply_active_profile(state, _answerer)
                    except ValueError as exc:
                        state.mark_blocked(str(exc), tool_name="domain_profile_registry")
                        self._save(state)
                        return self._result(state, ledger, blocker=state.last_error)
                interview_phase_timeout = state.phase_timeout_seconds(AutoPhase.INTERVIEW)
                interview_timeout = self._deadline_capped_timeout(state, interview_phase_timeout)
                research = auto_expectations.start_expected_experience(
                    self.expectations_researcher, state, interview_timeout, self._emit_runtime_event
                )
                try:
                    try:
                        # Repository investigation pauses this clock (auto/phase_clock.py).
                        interview = await wait_for_pausable(
                            self.interview_driver.run(state, ledger), interview_timeout
                        )
                        if interview.status != "blocked":
                            auto_expectations.keep(state, await research.result())
                    except TimeoutError:
                        if self._enforce_deadline(state):
                            return self._result(state, ledger, blocker=state.last_error)
                        # PR-B / #1257 closure ladder: the per-phase interview
                        # deadline is no longer a terminal BLOCKED. Instead, the
                        # ledger evidence collected so far is converted into a
                        # Seed (complete → ``synthesize_seed_from_ledger``,
                        # incomplete → ``partial_seed_from_evidence``) and the
                        # pipeline continues at SEED_GENERATION so a partial
                        # product can still surface. ``_enforce_deadline`` above keeps
                        # the global pipeline-deadline contract untouched: only
                        # the per-phase ``interview_phase_deadline`` is rerouted.
                        auto_expectations.keep(state, await research.result())
                        return await self._handle_interview_deadline(
                            state,
                            ledger,
                            timeout_seconds=interview_phase_timeout,
                        )
                finally:
                    research.cancel()  # a path that did not await the research stops it
                    # RFC #1256 §I4: drain the driver's background emit tasks on EVERY exit
                    # path (completion, deadline, any exception out of ``interview_driver.run``)
                    # so ``auto.interview.opened``/``failed`` evidence is not lost when the
                    # composition root unwinds (bot review on ``34fd7ee8``). The drain is
                    # bounded by ``_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS``, cannot change
                    # the phase outcome, and refunds its elapsed time to ``state.deadline_at``
                    # so observability latency never turns a completed interview into a
                    # ``pipeline_timeout`` BLOCKED (bot review on ``769cdfeb``).
                    await self._drain_interview_observer_events(state)
                if interview.status == "blocked":
                    return self._result(state, ledger, blocker=interview.blocker)
                state.interview_completed = True
                state.transition(AutoPhase.SEED_GENERATION, "generating Seed from auto interview")
                self._save(state)
        elif state.phase in {AutoPhase.REVIEW, AutoPhase.REPAIR}:
            # The Seed review stage was removed (#3064). A session saved in
            # it resumes at Seed generation, which writes its persisted Seed
            # and starts the run (or generates the Seed if none was kept).
            state.transition(
                AutoPhase.SEED_GENERATION,
                f"resuming at Seed generation: the {state.phase.value} stage was removed",
            )
            self._save(state)
        elif state.phase not in {
            AutoPhase.SEED_GENERATION,
            AutoPhase.RUN,
            AutoPhase.RALPH_HANDOFF,
            AutoPhase.EVALUATE,
            AutoPhase.UNSTUCK_LATERAL,
        }:
            state.mark_blocked(
                f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
                tool_name="auto_pipeline",
            )
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)

        return None

    async def _run_seed_phase(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult | Seed:
        if not _has_reconciliable_ralph_resume_checkpoint(state) and self._enforce_deadline(state):
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase == AutoPhase.SEED_GENERATION:
            obtained = await self._obtain_seed(state, ledger)
            if isinstance(obtained, AutoPipelineResult):
                return obtained
            return await self._write_seed(state, ledger, obtained)
        if state.seed_artifact:
            try:
                seed = Seed.from_dict(state.seed_artifact)
            except Exception as exc:
                state.mark_failed(
                    f"persisted Seed artifact is invalid: {exc}",
                    tool_name="auto_pipeline",
                )
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
            return self._normalize_execution_seed(state, seed)
        if self.seed_loader is not None and state.seed_path:
            loaded = self._load_seed(state, state.seed_path)
            if loaded is None:
                return self._result(state, ledger, blocker=state.last_error)
            return loaded
        state.mark_blocked(
            f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
            tool_name="auto_pipeline",
        )
        self._save(state)
        return self._result(state, ledger, blocker=state.last_error)

    async def _obtain_seed(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult | Seed:
        """Return the Seed at SEED_GENERATION: the persisted one, else a new one."""
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
            return self._normalize_execution_seed(state, seed)
        if self.seed_loader is not None and state.seed_path:
            # A legacy session kept its written Seed only on disk; run that
            # Seed rather than generating a different one.
            loaded = self._load_seed(state, state.seed_path)
            if loaded is None:
                return self._result(state, ledger, blocker=state.last_error)
            return loaded
        if (
            state.interview_closure_mode in {"ledger_only_no_backend", "safe_default_no_backend"}
            and ledger.is_seed_ready()
        ):
            seed = synthesize_seed_from_ledger(
                ledger,
                interview_id=state.interview_session_id,
                brownfield_context=brownfield_context_from_cwd(state.cwd),
            )
            seed = self._record_generated_seed(state, ledger, seed)
            state.mark_progress(
                "Seed generated from completed ledger without authoring backend",
                tool_name="ledger_seed_generator",
            )
            self._save(state)
            return seed
        if not state.interview_session_id:
            if not ledger.is_seed_ready():
                state.mark_failed(
                    "seed generation cannot resume without interview_session_id",
                    tool_name="seed_generator",
                )
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
            seed = synthesize_seed_from_ledger(
                ledger,
                brownfield_context=brownfield_context_from_cwd(state.cwd),
            )
            seed = self._record_generated_seed(state, ledger, seed)
            state.mark_progress(
                "Seed generated from completed ledger", tool_name="ledger_seed_generator"
            )
            self._save(state)
            return seed
        seed_timeout = self._deadline_capped_timeout(state, self.seed_timeout_seconds)
        # SSOT #1157 *Closure Policy* (2026-05-27): when the interview
        # closed on ledger evidence (``ledger_only`` / ``safe_default``)
        # rather than backend ``mutual_agreement``, the persisted
        # backend ambiguity_score is acknowledged-stale by design —
        # the ledger's structural completeness IS the acceptance
        # signal. Without forcing past the ambiguity gate here the
        # seed generator would re-block ledger-only sessions at
        # exactly the same threshold that the interview driver
        # explicitly chose to ignore, defeating PR-β's purpose for
        # the canonical #1170 R2-diag failure mode.
        force_seed_generation = state.interview_closure_mode in {
            "ledger_only",
            "safe_default",
        }
        # Only forward ``force`` when (a) we actually want to force
        # AND (b) the seed_generator implementation advertises a
        # ``force`` parameter. Legacy callables declare
        # ``async def(session_id)`` without a ``force`` kwarg;
        # passing it unconditionally would raise ``TypeError``.
        # PR-β-aware implementations (``HandlerSeedGenerator``)
        # opt in by declaring ``force``.
        seed_kwargs: dict[str, Any] = {}
        if force_seed_generation and _seed_generator_accepts_force(self.seed_generator):
            seed_kwargs["force"] = True
        try:
            seed = await asyncio.wait_for(
                self.seed_generator(state.interview_session_id, **seed_kwargs),
                timeout=seed_timeout,
            )
            if not isinstance(seed, Seed):
                msg = f"seed generator returned {type(seed).__name__}, expected Seed"
                raise TypeError(msg)
            distillation = getattr(
                self.seed_generator,
                "last_requirement_distillation",
                None,
            )
            if distillation is not None:
                bridge_result = apply_requirement_distillation_to_ledger(
                    distillation,
                    ledger,
                )
                state.ledger = ledger.to_dict()
                if bridge_result.blockers:
                    blocker = bridge_result.blockers[0]
                    state.mark_blocked(
                        "Requirement candidate confirmation is required before "
                        f"auto Seed finalization: {blocker.candidate_id}",
                        tool_name="reference_candidate_bridge",
                        error_code=blocker.code,
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
            seed = self._record_generated_seed(state, ledger, seed)
        except TimeoutError as exc:
            if self._enforce_deadline(state):
                record_authoring_backend(state)
                return self._result(state, ledger, blocker=state.last_error)
            if ledger.is_seed_ready():
                seed = synthesize_seed_from_ledger(
                    ledger,
                    interview_id=state.interview_session_id,
                    brownfield_context=brownfield_context_from_cwd(state.cwd),
                )
                seed = self._record_generated_seed(state, ledger, seed)
                state.mark_progress(
                    "Seed generated from completed ledger after seed generator timeout",
                    tool_name="ledger_seed_generator",
                )
                self._save(state)
                return seed
            state.mark_blocked(
                f"seed generation timed out after {self.seed_timeout_seconds:.0f}s",
                tool_name="seed_generator",
            )
            record_authoring_backend(state)
            self._save(state)
            return self._result(state, ledger, blocker=str(exc) or state.last_error)
        except Exception as exc:
            if ledger.is_seed_ready() and _is_authoring_backend_unavailable(exc):
                seed = synthesize_seed_from_ledger(
                    ledger,
                    interview_id=state.interview_session_id,
                    brownfield_context=brownfield_context_from_cwd(state.cwd),
                )
                seed = self._record_generated_seed(state, ledger, seed)
                state.mark_progress(
                    f"Seed generated from completed ledger after seed generator failure: {exc}",
                    tool_name="ledger_seed_generator",
                )
                self._save(state)
                return seed
            message = f"seed generation failed: {exc}"
            if _is_seed_generation_blocker(exc):
                state.mark_blocked(message, tool_name="seed_generator")
            else:
                state.mark_failed(message, tool_name="seed_generator")
            record_authoring_backend(state)
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)
        state.mark_progress("Seed generated", tool_name="seed_generator")
        self._save(state)
        return seed

    async def _write_seed(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
    ) -> AutoPipelineResult | Seed:
        """Write the Seed and hand it to the run, or stop where nothing runs.

        Seed generation ends here. Nothing reviews, grades, repairs, or
        pre-checks the Seed between this write and the run (#3064); the run job
        verifies each acceptance criterion after it executes. What still stops
        before RUN: ``skip_run``, which completes with the written Seed, and the
        interview's deadline closure rules for a degraded Seed
        (:mod:`ouroboros.auto.degraded_seed`): a section a human must answer
        blocks it, and without the user's goal on record it has no goal to run
        (:meth:`_emit_partial_product_terminal`).
        """
        state.ledger = ledger.to_dict()
        if self.seed_saver is not None:
            try:
                state.seed_path = self.seed_saver(seed)
            except Exception as exc:
                state.mark_failed(f"seed save failed: {exc}", tool_name="seed_saver")
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
        self._save(state)

        degraded = bool(getattr(seed.metadata, "degraded", False))
        if degraded and (terminal := degraded_seed_safety_blocker(ledger)):
            blocker, code = terminal
            state.mark_blocked(blocker, tool_name="interview_deadline_recovery", error_code=code)
            self._save(state)
            return self._result(state, ledger, blocker=blocker)
        runs_degraded = degraded_seed_runs_as_stated(seed)
        if degraded and not runs_degraded:
            return await self._emit_partial_product_terminal(state, ledger, seed)
        if runs_degraded:
            state.mark_progress(
                "Degraded seed runs on the request as stated; "
                f"{len(seed.metadata.unresolved_slots)} unresolved slot(s) stay open",
                tool_name="seed_generator",
            )
            self._save(state)

        if self.skip_run or state.skip_run:
            # The only COMPLETE transition with no deadline check of its
            # own; RUN enforces it at ``_run_execution_phase`` entry.
            if self._enforce_deadline(state):
                return self._result(state, ledger, blocker=state.last_error)
            state.transition(AutoPhase.COMPLETE, "Seed written; skip-run requested")
            self._save(state)
            return self._result(state, ledger)
        return seed

    async def _run_terminal_resume_phase(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult | None:
        if mark_retired_phase(state):
            self._save(state)
            return self._result(state, ledger, blocker=state.last_error)

        return None

    async def _run_execution_phase(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
    ) -> AutoPipelineResult:
        if self._enforce_deadline(state):
            return self._result(state, ledger, blocker=state.last_error)
        if state.phase == AutoPhase.RUN:
            attached = self._attach_run_if_requested(state)
            if attached is not None:
                self._save(state)
                return self._result(state, ledger)
            reconciled, transient_blocker = self._reconcile_run_if_requested(state)
            if reconciled is not None:
                self._save(state)
                blocker = transient_blocker or state.last_error
                return self._result(state, ledger, blocker=blocker)
            if any((state.job_id, state.execution_id, state.run_session_id)):
                state.run_handoff_status = RUN_HANDOFF_STARTED_STATUS
                state.run_handoff_guidance = None
                # A handle proves dispatch, not success (#1590). Reconcile when
                # possible; unpollable legacy handles retain historical trust.
                run_verdict = (
                    await _wait_owned_run_job_terminal(
                        self.run_starter,
                        state.job_id,
                        timeout_seconds=self._deadline_capped_timeout(
                            state, state.phase_timeout_seconds(AutoPhase.RUN)
                        ),
                    )
                    if state.job_id
                    else None
                )
                blocked = self._block_resume_if_run_not_successful(state, run_verdict)
                if blocked is not None:
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                if mark_retired_complete_product_run(state):
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
                state.transition(
                    AutoPhase.COMPLETE, "execution already started; using persisted run handle"
                )
                self._save(state)
                return self._result(state, ledger)
        if self.run_starter is None:
            state.mark_blocked("No run starter configured", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, blocker="No run starter configured")

        if state.phase != AutoPhase.RUN:
            state.run_start_attempted = False
            state.run_handoff_status = None
            state.run_handoff_guidance = None
            state.transition(AutoPhase.RUN, "starting execution for the written Seed")
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
            return self._result(state, ledger, blocker=state.last_error)
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
            run_start_timeout = self._run_start_timeout(state)
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
            except TimeoutError:
                if state.is_deadline_expired():
                    recovered_run_meta = await _recover_timed_out_synchronous_run(
                        self.run_starter,
                        timeout_seconds=5.0,
                    )
                    if recovered_run_meta is not None:
                        run_meta = recovered_run_meta
                    elif self._enforce_deadline(state):
                        return self._result(state, ledger, blocker=state.last_error)
                if run_meta is None:
                    _mark_unknown_run_handoff(state, status=UNKNOWN_TIMEOUT_STATUS)
                if run_meta is None and retried:
                    state.run_handoff_guidance = (
                        f"{state.run_handoff_guidance or ''} "
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}"
                    ).strip()
                    state.mark_blocked(
                        f"run start timed out after {run_start_timeout:.0f}s; "
                        f"{RETRY_GUIDANCE_PHRASE} {idempotency_key}",
                        tool_name="run_starter",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=state.last_error)
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
                    return self._result(state, ledger, blocker=state.last_error)
                # Initial attempt: non-timeout errors are not retried —
                # the contract is to bound retries on *unknown* handoffs
                # only. Reset the attempt flag so the caller can re-invoke
                # after fixing the underlying error (preserves prior
                # behavior).
                state.run_start_attempted = attempted_at_entry
                state.mark_failed(f"run start failed: {exc}", tool_name="run_starter")
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)

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
                    state.run_handoff_status = RUN_HANDOFF_STARTED_STATUS
                    state.run_handoff_guidance = None
                    state.transition(AutoPhase.COMPLETE, "execution started for the written Seed")
                    self._save(state)
                    return self._result(state, ledger, run_subagent=run_subagent)
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
                return self._result(state, ledger, blocker=state.last_error)

            # First attempt landed in an unknown handoff (timeout or
            # no-handle). Persist the unknown status, then retry exactly
            # once with the same idempotency_key so the server-side
            # handler can short-circuit any duplicate enqueue. Both
            # timeout and no-handle paths share this same retry slot.
            self._save(state)
            retried = True

    async def _check_watchdog(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
    ) -> AutoPipelineResult | None:
        """L2-2 / #1172: wall-clock watchdog check at ``run()`` entry.

        Returns:

        - ``None`` if no watchdog is wired, or the watchdog did not
          fire — the pipeline proceeds normally.
        - A BLOCKED :class:`AutoPipelineResult` if the watchdog fired —
          ``state.last_error_code`` is set to
          :data:`WATCHDOG_STOP_REASON_CODE` so the envelope surface
          (already plumbed by L4 / #1151) reports the typed terminal.

        The watchdog itself owns the EventStore-side ``runtime.watchdog.cancel``
        append; this helper only converts a returned ``WatchdogDecision``
        into pipeline state transitions.
        """
        if self.watchdog is None:
            return None
        try:
            started_at = datetime.fromisoformat(state.created_at)
        except (TypeError, ValueError):
            # A malformed ``created_at`` is a pre-watchdog state-validation
            # concern; do not let the watchdog crash the run.
            return None
        decision = await self.watchdog.check(
            session_id=state.auto_session_id,
            session_started_at=started_at,
        )
        if decision is None:
            return None
        blocker = (
            f"runtime watchdog cancelled session after "
            f"{decision.elapsed_seconds}s (budget "
            f"{decision.configured_budget_seconds}s)"
        )
        state.mark_blocked(
            blocker,
            tool_name="runtime_watchdog",
            error_code=WATCHDOG_STOP_REASON_CODE,
        )
        self._save(state)
        return self._result(state, ledger, blocker=state.last_error)

    def _block_resume_if_run_not_successful(
        self, state: AutoPipelineState, run_verdict: dict[str, Any] | None
    ) -> bool | None:
        """Block an observed non-success terminal; preserve ambiguous legacy handles."""
        if run_verdict is None:
            return None
        status = _optional_str(run_verdict.get("status"))
        success = run_verdict.get("success")
        if success is True or status == "completed":
            return None
        if status in {"running", "queued", "cancel_requested"}:
            # Still in flight elsewhere — cannot prove failure; do not cancel or
            # block a run another owner may still complete.
            return None
        if status == "paused":
            state.mark_blocked(
                "resumed run execution paused before completion; resume the "
                "paused run before continuing",
                tool_name="run_starter",
            )
            return True
        detail = status or "unknown"
        state.mark_blocked(
            f"resumed run execution did not reach terminal success: {detail}",
            tool_name="run_starter",
        )
        return True

    def _deadline_capped_timeout(self, state: AutoPipelineState, phase_timeout: float) -> float:
        """Return ``phase_timeout`` capped by the remaining pipeline deadline.

        Without this cap, ``_enforce_deadline`` only fires at phase
        boundaries — a single ``await`` inside the interview / seed-gen /
        run-start path could spend the full per-phase timeout
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

    def _run_start_timeout(self, state: AutoPipelineState) -> float:
        """Return the timeout budget for the run starter invocation.

        Async run starters only enqueue work, so they keep the short
        ``run_start_timeout_seconds`` budget. The CLI complete-product path can
        use a synchronous starter that owns the actual execution; treat that as
        RUN phase work and let the top-level pipeline deadline bound it.
        """
        return self._deadline_capped_timeout(state, self.run_start_timeout_seconds)

    async def _drain_interview_observer_events(
        self, state: AutoPipelineState | None = None
    ) -> None:
        """Bounded composition-root drain of background interview emit tasks.

        Called OUTSIDE the interview phase's
        ``asyncio.wait_for(interview_timeout)`` boundary so the
        EventStore appends scheduled by ``AutoInterviewDriver`` cannot
        weaken phase-deadline or cancellation contracts (bot review
        on commit ``c5549124``, req_1779938459_153, reproduced the
        contract failure when the equivalent drain ran inside
        ``driver.run()``).

        Behaviour:

        * ``asyncio.shield`` protects the in-flight appends from
          outer cancellation. A top-level deadline that fires during
          the drain cancels the await on the shield, but the inner
          ``wait_for_pending_emits`` (and the per-event background
          tasks it tracks) keeps running until completion or until
          the event loop closes — observability is best-effort but
          never the cause of a cancellation.
        * ``asyncio.wait_for`` bounds the caller-visible wait by
          ``_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS`` (1.5 s, large
          enough to cover the driver's per-event 1.0 s fail-open
          bound for a two-event session). Timeouts are downgraded to
          a typed ``auto.interview.observer_drain_timed_out`` warning.
        * Short-circuits when no driver is wired or its pending set
          is empty, so the helper is safe to call unconditionally
          after every ``await self.interview_driver.run(...)``.
        * **Deadline refund (RFC #1256 §I4):** when ``state`` is
          provided and a top-level deadline is armed, the helper
          measures elapsed drain time and shifts ``state.deadline_at``
          / ``state.deadline_at_epoch`` forward by the same amount.
          Without this refund, supplied EventStore latency could
          consume the remaining ``pipeline_timeout_seconds`` budget
          and convert a completed interview into a ``pipeline_timeout``
          BLOCKED at the next ``_enforce_deadline`` gate (bot review
          on commit ``769cdfeb``, req_1779940568_155, reproduced this
          with two 0.2 s appends + a ``deadline_at = now + 0.1`` budget:
          without observability the pipeline advanced; with
          observability it returned BLOCKED). Refunding the drain
          duration makes observability's contribution to elapsed
          wall-clock invisible to the deadline machinery — exactly the
          fail-open semantic the §I4 contract advertises.
        """
        driver = self.interview_driver
        # ``AutoInterviewDriver`` exposes ``_pending_emit_tasks`` /
        # ``wait_for_pending_emits``; test/stub drivers that satisfy
        # only the ``run`` Protocol may not. Treat the missing surface
        # as "nothing to drain" so existing pipeline tests that wire
        # mock interview drivers (e.g. ``test_pipeline_deadline``)
        # continue to work unchanged — the §I4 contract only applies
        # when the production driver is in use.
        pending_tasks = getattr(driver, "_pending_emit_tasks", None)
        wait_for_pending_emits = getattr(driver, "wait_for_pending_emits", None)
        if not pending_tasks or wait_for_pending_emits is None:
            return
        pending = len(pending_tasks)
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_for_pending_emits()),
                timeout=_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "auto.interview.observer_drain_timed_out",
                pending=pending,
                timeout_seconds=_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
            )
        finally:
            elapsed = time.monotonic() - started
            if state is not None and elapsed > 0.0:
                if state.deadline_at is not None:
                    state.deadline_at += elapsed
                if state.deadline_at_epoch is not None:
                    state.deadline_at_epoch += elapsed

    async def _emit_runtime_deadline_interview_fired(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        timeout_seconds: float,
        closure_route: str,
    ) -> None:
        """Persist a ``runtime.deadline.interview.fired`` event, fail-open.

        Reuses the interview driver's ``event_store`` (the only EventStore
        the pipeline holds a handle to today, wired in #1260) so the
        deadline event lands in the same aggregate stream that
        ``ouroboros_query_events(auto_session_id)`` already consumes for
        ``auto.interview.*`` lifecycle records. The event itself is a
        runtime-class event (``runtime.deadline.*``) so the aggregate
        type uses ``auto_session`` rather than the interview-specific
        ``auto_interview``; this matches the pattern used by
        :class:`Watchdog` for ``runtime.watchdog.*`` events.

        Errors and timeouts are downgraded to typed structlog warnings —
        observability must never convert the deadline-recovery path back
        into a terminal BLOCKED. The append is awaited inline so the
        deadline event durably precedes the later
        ``auto.product.partial_emitted`` append (canonical regression
        contract); the awaited write is bounded by
        ``_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS`` so a stuck
        EventStore still cannot consume the post-deadline budget.
        ``_drain_interview_observer_events`` ran *before* this helper,
        so we have no in-flight observer tasks to coordinate with here.
        """
        payload: dict[str, Any] = {
            "auto_session_id": state.auto_session_id,
            "phase": AutoPhase.INTERVIEW.value,
            "timeout_seconds": float(timeout_seconds),
            "ledger_ready": ledger.is_seed_ready(),
            "open_gaps": list(ledger.open_gaps()),
            "rounds_completed": int(state.current_round or 0),
            "closure_route": closure_route,
        }
        await self._emit_runtime_event(
            "runtime.deadline.interview.fired",
            state.auto_session_id,
            payload,
        )

    async def _handle_interview_deadline(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        timeout_seconds: float,
    ) -> AutoPipelineResult:
        """Convert an interview-phase deadline into a Seed-generation entry.

        Implements the #1257 PR-B closure ladder:

        1. Emit ``runtime.deadline.interview.fired`` so post-hoc evidence
           inspection can see *why* the recovery path was taken.
        2. Synthesize a Seed from the evidence collected so far. Either
           branch yields a **degraded** Seed, because a deadline closure
           never obtained backend-confirmed low ambiguity (#1302):

           * complete ledger → :func:`synthesize_seed_from_ledger` with
             ``recovery_reason="interview_phase_deadline"`` (full ledger
             content, no ``unresolved_slots``, but ``degraded=True``);
           * incomplete ledger → :func:`partial_seed_from_evidence` with
             ``reason="interview_phase_deadline"`` so unresolved sections
             become next-step hints on the degraded Seed (PR-A).
        3. Persist the Seed via :meth:`_record_generated_seed`, mark
           ``interview_completed = True``, and continue at
           ``SEED_GENERATION``, which writes the Seed (:meth:`_write_seed`). A
           Seed with the user's goal on record runs on that goal; one without
           it stops as a typed partial product
           (:meth:`_emit_partial_product_terminal`).

        The global ``pipeline_timeout_seconds`` deadline is unaffected:
        callers gate on :meth:`_enforce_deadline` *before* invoking this
        helper, so this method only fires when the per-phase deadline
        tripped while the top-level budget still has time.
        """
        ledger_ready = ledger.is_seed_ready()
        closure_route = "ledger_seed" if ledger_ready else PARTIAL_SEED_GENERATION_MODE
        await self._emit_runtime_deadline_interview_fired(
            state,
            ledger,
            timeout_seconds=timeout_seconds,
            closure_route=closure_route,
        )

        if ledger_ready:
            # #1302: the deadline cut the loop short before the backend could
            # confirm low ambiguity (<= 0.20), so this Seed is stamped degraded
            # (``recovery_reason`` set) and is treated exactly like the
            # incomplete-ledger branch below.
            seed = synthesize_seed_from_ledger(
                ledger,
                interview_id=state.interview_session_id,
                recovery_reason="interview_phase_deadline",
                brownfield_context=brownfield_context_from_cwd(state.cwd),
            )
            progress_message = (
                "Interview phase deadline fired; closed via complete-ledger Seed synthesis "
                "(degraded recovery: backend low ambiguity was never confirmed)"
            )
        else:
            seed = partial_seed_from_evidence(
                ledger,
                reason="interview_phase_deadline",
                interview_id=state.interview_session_id,
            )
            progress_message = (
                "Interview phase deadline fired; closed via partial_seed_from_evidence (degraded)"
            )

        seed = self._record_generated_seed(state, ledger, seed)
        # Persist the ledger so the recursive ``self.run(state)`` below
        # rebuilds the same ledger (including any entries the interview
        # driver added before the deadline cancelled it). Without this, the
        # inner ``SeedDraftLedger.from_dict(state.ledger)`` fallback would
        # use ``SeedDraftLedger.from_goal(state.goal)`` and silently drop
        # every section the driver had collected.
        state.ledger = ledger.to_dict()
        state.interview_completed = True
        state.mark_progress(
            progress_message,
            tool_name="interview_deadline_recovery",
        )
        self._save(state)
        # The Seed is already in ``state.seed_artifact`` from
        # ``_record_generated_seed``; the recursive run resumes at
        # SEED_GENERATION, which writes it and decides whether it runs.
        state.transition(
            AutoPhase.SEED_GENERATION,
            f"interview-deadline closure via {closure_route}",
        )
        self._save(state)
        return await self.run(state)

    async def _emit_partial_product_terminal(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
    ) -> AutoPipelineResult:
        """Stop a degraded Seed without a sourced goal before RUN (#1257 PR-C).

        With no ``sourced_scope`` there is no user goal on record to run, so
        the Seed's unresolved sections stay next-step requirements. We stop
        *before* RUN/RALPH_HANDOFF and emit ``auto.product.partial_emitted``
        so post-hoc evidence inspection can see the typed terminal.

        The event aggregate type is ``auto_session`` (same as the PR-B
        ``runtime.deadline.interview.fired`` event) so a single
        ``ouroboros_query_events(auto_session_id)`` call surfaces both the
        deadline trigger and the terminal recovery.
        """
        unresolved_slots = tuple(getattr(seed.metadata, "unresolved_slots", ()))
        recovery_reason = getattr(seed.metadata, "recovery_reason", None)
        await self._emit_runtime_event(
            "auto.product.partial_emitted",
            state.auto_session_id,
            {
                "auto_session_id": state.auto_session_id,
                "seed_id": seed.metadata.seed_id,
                "recovery_reason": recovery_reason,
                "unresolved_slots": list(unresolved_slots),
            },
        )
        state.mark_progress(
            "Partial product emitted from degraded seed; "
            f"{len(unresolved_slots)} unresolved slot(s) carried forward as next steps",
            tool_name="partial_product_terminal",
        )
        blocker, code = degraded_seed_not_executed_blocker(len(unresolved_slots))
        state.mark_blocked(blocker, tool_name="partial_product_terminal", error_code=code)
        self._save(state)
        return self._result(state, ledger, blocker=blocker)

    async def _emit_product_emitted_terminal(
        self,
        state: AutoPipelineState,
        *,
        stop_reason: str | None,
        resumed: bool = False,
    ) -> None:
        """Emit a typed ``auto.product.emitted`` at a complete-product success terminal.

        RFC #1256 §I4 (#1254). Symmetric to
        :meth:`_emit_partial_product_terminal`'s ``auto.product.partial_emitted``
        on the degraded path. Shared by *every* in-process complete-product
        success terminal so a successful run always leaves at least one
        queryable terminal event under ``ouroboros_query_events(auto_session_id)``
        instead of the auto session aggregate being empty on success:

        * the direct ralph-completed path in :meth:`_evaluate_or_complete`
          (no evaluator wired),
        * the evaluator-pass path in :meth:`_finalize_evaluate` — the branch
          MCP wires for normal non-plugin complete-product runs,
        * the synchronous CLI run-success path (``HandlerSynchronousRunStarter``,
          which completes inline at RUN), and
        * the ralph re-attach success path in :meth:`_reattach_ralph_job`.

        These terminals are mutually exclusive for a single run, so a
        complete-product success emits **exactly one** ``auto.product.emitted``.

        Deliberately *not* emitted on the OpenCode **plugin-delegation**
        terminals ("ralph loop delegated…" / "resumed … plugin Ralph delegation
        checkpoint"): in plugin mode the loop and its product are owned by the
        spawned child session, so this process has not produced a product to
        announce — emitting here would be a false terminal.

        Awaited inline via the same fail-open :meth:`_emit_runtime_event`, so a
        slow/absent EventStore never converts a successful COMPLETE into a
        BLOCKED.
        """
        await self._emit_runtime_event(
            "auto.product.emitted",
            state.auto_session_id,
            {
                "auto_session_id": state.auto_session_id,
                "seed_id": state.seed_id,
                "stop_reason": stop_reason,
                "resumed": resumed,
            },
        )

    async def _emit_runtime_event(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist a runtime event in caller order via the interview driver's EventStore.

        Shared between :meth:`_emit_runtime_deadline_interview_fired` and
        :meth:`_emit_partial_product_terminal` so both #1257 surfaces ride
        the same fail-open path. The append is **awaited inline** so the
        ordering contract pinned by the PR-D canonical regression —
        ``runtime.deadline.interview.fired`` MUST precede
        ``auto.product.partial_emitted`` — is durable under realistic
        EventStore append latency / retry behavior. Fire-and-forget
        scheduling was the previous implementation and was flagged by
        ouroboros-agent[bot] ``supersede-requeue-pr:1272-ce273bf`` as a
        race window: if the deadline event's append was slow while the
        partial event's append was fast, the persisted stream could
        contradict the lifecycle even when the result envelope said
        ``partial_product=True``.

        The append is bounded by
        ``_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS`` and downgrades
        timeouts / exceptions to typed structlog warnings, so a stuck
        EventStore can never convert the deadline-recovery path back
        into a terminal BLOCKED; observability stays fail-open.
        """
        event_store = getattr(self.interview_driver, "event_store", None)
        if event_store is None:
            return
        from ouroboros.events.base import BaseEvent

        try:
            await asyncio.wait_for(
                event_store.append(
                    BaseEvent(
                        type=event_type,
                        aggregate_type="auto_session",
                        aggregate_id=aggregate_id,
                        data=dict(payload),
                    )
                ),
                timeout=_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                f"{event_type}.timed_out",
                auto_session_id=aggregate_id,
                timeout_seconds=_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — observer must not break the loop
            log.warning(
                f"{event_type}.failed",
                auto_session_id=aggregate_id,
                error=str(exc),
            )

    def _can_redispatch_recovery_plan(plan: AutoRecoveryPlan) -> bool:
        """Return True when a persisted plan is safe to consume automatically."""
        return (
            plan.action is RecoveryPlanAction.RALPH_REDISPATCH
            and plan.safe_to_redispatch
            and bool(plan.instruction.strip())
        )

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

    def _normalize_execution_seed(
        self, state: AutoPipelineState, seed: Seed, *, persist: bool = True
    ) -> Seed:
        normalized = normalize_execution_acceptance(seed)
        if normalized is not seed and persist:
            state.seed_artifact = normalized.to_dict()
            self._save(state)
        return normalized

    def _record_generated_seed(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        seed: Seed,
    ) -> Seed:
        """Persist a newly generated Seed and apply deterministic auto enrichments."""
        floor = deterministic_floor(ledger)
        if floor > seed.metadata.ambiguity_score:
            seed = seed.model_copy(
                update={"metadata": seed.metadata.model_copy(update={"ambiguity_score": floor})}
            )
        seed = with_ledger_carryover(seed, ledger, expected=auto_expectations.kept(state))
        seed = normalize_execution_acceptance(seed)
        state.seed_id = seed.metadata.seed_id
        state.seed_artifact = seed.to_dict()
        state.seed_origin = SeedOrigin.AUTO_PIPELINE

        # L1-d / #1171: derive the task class from the standardized ledger.
        # Catalog ACs are a fallback for a genuinely empty contract; silently
        # prepending them to an existing Seed broadens user-authorized scope.
        inference = derive_domain_from_ledger(ledger)
        task_class = next(iter(inference.classes)) if inference.is_single else None
        if task_class is not None:
            if not seed.acceptance_criteria:
                applied = apply_default_ac_template(seed, task_class)
                if applied.injected_ac:
                    seed = normalize_execution_acceptance(applied.seed)
                    state.seed_id = seed.metadata.seed_id
                    state.seed_artifact = seed.to_dict()
            state.active_task_class = task_class.value
        return seed

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
        seed = self._normalize_execution_seed(state, seed, persist=False)
        state.seed_id = seed.metadata.seed_id
        state.seed_artifact = seed.to_dict()
        return seed

    def _result(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        blocker: str | None = None,
        run_subagent: dict[str, Any] | None = None,
        status_override: str | None = None,
    ) -> AutoPipelineResult:
        if state.phase == AutoPhase.COMPLETE:
            self._checkpoint_final_commit(state)
        summary = ledger.summary()
        ledger_provenance = {
            source: tuple(sections) for source, sections in summary.get("provenance", {}).items()
        }
        # #1257 PR-C — surface degraded-Seed recovery metadata.
        #
        # Derived from ``state.seed_artifact`` so every ``_result`` callsite
        # (terminal COMPLETE, BLOCKED with a degraded seed still on disk,
        # mid-flight envelopes) reports consistent partial-product surface.
        seed_artifact = state.seed_artifact or {}
        seed_meta = seed_artifact.get("metadata", {}) if isinstance(seed_artifact, dict) else {}
        seed_degraded = bool(seed_meta.get("degraded"))
        partial_unresolved_slots = (
            tuple(seed_meta.get("unresolved_slots", ())) if seed_degraded else ()
        )
        partial_product_reason = seed_meta.get("recovery_reason") if seed_degraded else None
        # ``partial_product`` is True only at the typed terminal: a degraded
        # Seed that reached :meth:`_emit_partial_product_terminal`, which stops
        # BLOCKED with ``DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE`` because
        # the Seed was never executed. A degraded Seed that BLOCKED for another
        # reason (e.g. a safety blocker) keeps ``partial_product=False``.
        partial_product = seed_degraded and is_degraded_seed_not_executed_stop(state)
        result_status = status_override or state.phase.value
        artifact_state = _artifact_state_for_result(
            status=result_status,
            phase=state.phase,
            seed_path=state.seed_path,
            execution_id=state.execution_id,
            job_id=state.job_id,
            run_session_id=state.run_session_id,
            run_handoff_status=state.run_handoff_status,
            partial_product=partial_product,
            product_verified=_has_verified_product_completion(state),
        )
        return AutoPipelineResult(
            status=result_status,
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            seed_path=state.seed_path,
            seed_origin=state.seed_origin.value,
            interview_session_id=state.interview_session_id,
            execution_id=state.execution_id,
            job_id=state.job_id,
            run_session_id=state.run_session_id,
            run_subagent=run_subagent or state.run_subagent or None,
            current_round=state.current_round,
            pending_question=state.pending_question,
            interview_closure_mode=state.interview_closure_mode,
            active_task_class=state.active_task_class,
            last_progress_message=state.last_progress_message,
            last_progress_at=state.last_progress_at,
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
            open_questions=tuple(seed_artifact.get("open_questions", ())),
            assumed_answers=tuple(
                (seed_artifact.get("sourced_scope") or {}).get("assumptions", ())
            ),
            assumption_sources=tuple(ledger.assumption_sources()),
            non_goals=tuple(ledger.non_goals()),
            defaulted_sections=tuple(ledger.summary().get("defaulted_sections", ())),
            blocker=blocker or state.last_error,
            stop_reason_code=state.last_error_code,
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
            efficiency_mode=state.efficiency_mode,
            frugality_assurance=state.frugality_assurance,
            invoked_by=state.invoked_by(),
            provenance=dict(state.provenance) if state.provenance else None,
            last_authoring_backend=state.last_authoring_backend,
            resume_capability=state.resume_capability(),
            ledger_provenance=ledger_provenance,
            evidence_backed_sections=tuple(summary.get("evidence_backed_sections", ())),
            assumption_only_sections=tuple(summary.get("assumption_only_sections", ())),
            runtime_probe_evidence=self._last_probe_evidence
            or _runtime_probe_evidence_from_state(state),
            checkpoint_commits=tuple(state.checkpoint_commits),
            partial_product=partial_product,
            partial_product_reason=partial_product_reason,
            partial_unresolved_slots=partial_unresolved_slots,
            artifact_state=artifact_state,
        )

    def _checkpoint_final_commit(self, state: AutoPipelineState) -> None:
        """Attempt the one-shot final-only checkpoint at product completion."""
        if state.commit_policy is not AutoCommitPolicy.FINAL_ONLY:
            return
        if state.final_checkpoint_attempted:
            return
        try:
            checkpoint_final_auto(
                state,
                repo_cwd=state.cwd,
                summary=state.last_progress_message or "final verified auto result",
            )
        except RuntimeError as exc:
            log.warning(
                "auto_final_checkpoint_commit_failed",
                auto_session_id=state.auto_session_id,
                cwd=state.cwd,
                error=str(exc),
            )
            state.final_checkpoint_attempted = True
        self._save(state)

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

    def _emit(self, state: AutoPipelineState, kind: str, message: str) -> None:
        if self.progress_callback is None:
            return
        event = AutoProgressEvent(
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            kind=kind,
            message=message,
        )
        try:
            self.progress_callback(event)
        except Exception:
            # Observers must never break the pipeline. Swallow callback errors.
            pass


def _has_verified_product_completion(state: AutoPipelineState) -> bool:
    """Return True when COMPLETE represents a verified product terminal.

    ``run_handoff_status=started`` can persist after a complete-product run moves
    through Ralph/EVALUATE success. Treat those terminal success signals as
    stronger evidence than the stale handoff marker so final surfaces do not
    downgrade verified product completion to handoff-only completion.
    """

    if state.phase is not AutoPhase.COMPLETE:
        return False
    if state.ralph_job_status == "completed":
        return True
    # last_qa_passed is Seed QA, which runs before the product execution.
    # It is not proof that the product or an attached run was verified.
    return state.run_handoff_status == "completed"


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


def _accepts_keyword(func: Callable[..., Any], name: str) -> bool:
    """Return True iff ``func`` declares ``name`` or accepts ``**kwargs``.

    Used to decide whether an optional keyword (the run starter's
    idempotency key) can be passed without breaking older callables that do
    not declare it.
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


def _is_seed_generation_blocker(exc: Exception) -> bool:
    """Classify recoverable authoring validation as blocked, not failed."""
    message = str(exc)
    return "Ambiguity score" in message and "exceeds threshold" in message


def _arm_legacy_missing_deadline(state: AutoPipelineState) -> bool:
    """Arm #779 deadline for legacy resumed sessions already past CREATED."""
    if state.phase in {AutoPhase.CREATED, AutoPhase.COMPLETE}:
        return False
    if state.deadline_at is not None or state.deadline_at_epoch is not None:
        return False
    state.arm_deadline()
    return True


def _allows_synchronous_completion_grace(run_meta: dict[str, Any]) -> bool:
    return bool(run_meta.get("_allow_deadline_completion_grace")) and bool(
        run_meta.get("_recovered_after_timeout")
    )


def _within_synchronous_completion_grace(state: AutoPipelineState) -> bool:
    if state.deadline_at is None:
        return True
    return (time.monotonic() - state.deadline_at) <= _SYNCHRONOUS_RUN_COMPLETION_GRACE_SECONDS


async def _recover_timed_out_synchronous_run(
    adapter: object | None,
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    recover = getattr(adapter, "recover_timed_out_run", None)
    if recover is None:
        return None
    try:
        recovered = await asyncio.wait_for(recover(), timeout=timeout_seconds)
    except Exception:
        return None
    if not isinstance(recovered, dict):
        return None
    return {**recovered, "_recovered_after_timeout": True}


def _has_reconciliable_ralph_resume_checkpoint(state: AutoPipelineState) -> bool:
    """Return True when deadline gating should allow Ralph reconciliation.

    Only persisted job handles and confirmed plugin dispatches qualify. An
    unconfirmed ``plugin_pending`` checkpoint must still obey normal deadline
    enforcement because resume has to retry the side-effecting plugin dispatch.
    """
    if state.phase is not AutoPhase.RALPH_HANDOFF and not (
        state.phase is AutoPhase.BLOCKED and state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    ):
        return False
    return state.ralph_job_id is not None or state.ralph_dispatch_mode == "plugin"


def _is_active_recovery_redispatch(state: AutoPipelineState) -> bool:
    """Return True while a safe lateral plan is driving a Ralph redispatch."""
    if state.run_handoff_status != "ralph_retry_after_blocker":
        return False
    if not isinstance(state.last_recovery_plan, dict):
        return False
    try:
        plan = AutoRecoveryPlan.from_dict(state.last_recovery_plan)
    except ValueError:
        return False
    return AutoPipeline._can_redispatch_recovery_plan(plan)


def _prepare_recovery_redispatch(state: AutoPipelineState) -> None:
    """Clear prior Ralph handles so recovery starts a fresh bounded attempt."""
    state.ralph_job_id = None
    state.ralph_lineage_id = None
    state.ralph_dispatch_mode = None
    state.ralph_job_status = None
    state.ralph_stop_reason = None
    state.ralph_current_generation = None
    state.ralph_last_event_at = None
    state.run_handoff_guidance = None
    state.run_handoff_status = "ralph_retry_after_blocker"


def _seed_with_recovery_constraint(seed: Seed, plan: AutoRecoveryPlan) -> Seed:
    """Return a dispatch-only Seed carrying bounded lateral recovery advice.

    The recovery advice must not relax the Seed's goal or acceptance
    criteria. Encoding it as an additional hard constraint gives Ralph the
    reframing context while preserving the user-approved ACs that EVALUATE
    will grade after redispatch.
    """
    instruction = _clean_seed_qa_repair_text(plan.instruction.strip(), limit=420)
    if not instruction:
        instruction = (
            "Do not copy recovery persona prompts or failed-run transcripts into Seed constraints."
        )
    constraint = (
        "[auto recovery] Previous QA failed. Use this lateral recovery "
        "instruction to change implementation/verification approach without "
        f"relaxing the goal or acceptance criteria: {instruction}"
    )
    return seed.model_copy(update={"constraints": (*seed.constraints, constraint)})


_SEED_QA_DIAGNOSTIC_PREFIX_RE = re.compile(
    r"\[seed qa(?: lateral)? repair attempt [^\]]+\]\s*",
    re.IGNORECASE,
)


def _clean_seed_qa_repair_text(text: str, *, limit: int) -> str:
    text = _SEED_QA_DIAGNOSTIC_PREFIX_RE.sub("", text.strip())
    cleaned_lines: list[str] = []
    skipping_diagnostic_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line:
            continue
        if _starts_seed_qa_recovery_context_block(line):
            break
        if _is_seed_qa_recovery_transcript_line(line):
            skipping_diagnostic_block = True
            continue
        if lowered.startswith("# lateral thinking:"):
            continue
        if lowered.startswith("qa differences:") or lowered.startswith("qa suggestions:"):
            skipping_diagnostic_block = True
            continue
        if skipping_diagnostic_block:
            if line.startswith(("-", "*")) or re.match(r"^\d+[\.)]\s+", line):
                continue
            skipping_diagnostic_block = False
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines)
    cleaned = cleaned.replace("# Lateral Thinking:", "").replace("# lateral thinking:", "")
    return cleaned[:limit].strip()


def _is_seed_qa_recovery_transcript_line(text: str) -> bool:
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "## persona:",
            "# persona:",
            "persona:",
            "current approach (not working)",
            "most recent run artifact",
            "problem context",
            "concrete constraints for the generated ouroboros seed",
            "evaluate failed",
            "failed-run transcript",
            "repair transcript",
            "diagnostic recovery text",
        )
    )


def _starts_seed_qa_recovery_context_block(text: str) -> bool:
    lowered = text.casefold()
    return lowered.startswith("## problem context") or lowered.startswith("# problem context")


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


async def _wait_owned_run_job_terminal(
    adapter: object | None,
    job_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    handler = getattr(adapter, "handler", None)
    job_manager = getattr(handler, "_job_manager", None)
    get_snapshot = getattr(job_manager, "get_snapshot", None)
    if get_snapshot is None:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while True:
        try:
            snapshot = await get_snapshot(job_id)
        except Exception:
            return None
        if getattr(snapshot, "is_terminal", False):
            meta = dict(getattr(snapshot, "result_meta", None) or {})
            status = getattr(getattr(snapshot, "status", None), "value", None)
            if isinstance(status, str):
                meta.setdefault("status", status)
            return meta
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {"status": "running"}
        await asyncio.sleep(min(0.1, remaining))


def _artifact_text(value: object) -> str | None:
    """Return ``value`` verbatim when it is a string (including ``""``), else None.

    Distinct from :func:`_optional_str` because an artifact graded by EVALUATE
    is a valid input even when empty: a Ralph job whose output is
    intentionally empty must still be evaluated against the Seed AC. Returning
    None for ``""`` would cause ``_evaluate_or_complete`` to skip EVALUATE
    and silently transition to COMPLETE.
    """
    return value if isinstance(value, str) else None


# -- PR-4 helper: thread domain profile into answerer -----------------------


def _apply_active_profile(state: AutoPipelineState, answerer: AutoAnswerer) -> None:
    """Resolve ``state.active_domain_profile_name`` and inject into ``answerer``.

    ``None`` is the only value that activates the hardcoded safety hatch.  A
    non-empty persisted profile name is durable session intent; if the registry
    cannot resolve it, fail loudly instead of silently downgrading to the coding
    fallback and authoring Seed content under the wrong domain.
    When ``answerer`` does not have an ``active_profile`` attribute (e.g. a
    test double), the call is silently skipped.
    """
    if not hasattr(answerer, "active_profile"):
        return
    name = getattr(state, "active_domain_profile_name", None)
    if name:
        profile = DEFAULT_REGISTRY.get(name)
        if profile is None:
            raise ValueError(f"active domain profile is not registered: {name}")
        answerer.active_profile = profile
    else:
        answerer.active_profile = None
