"""Full-quality AutoPipeline supervisor skeleton."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from ouroboros.auto.grading import GradeGate
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore, utc_now_iso
from ouroboros.core.seed import Seed

SeedGenerator = Callable[[str], Awaitable[Seed]]
RunStarter = Callable[[Seed], Awaitable[dict[str, Any]]]
SeedSaver = Callable[[Seed], str]
SeedLoader = Callable[[str], Seed]


@dataclass(frozen=True, slots=True)
class AutoPipelineResult:
    """Structured AutoPipeline result for CLI/MCP surfaces."""

    status: str
    auto_session_id: str
    phase: str
    grade: str | None = None
    seed_path: str | None = None
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
    assumptions: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    blocker: str | None = None


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

    async def run(self, state: AutoPipelineState) -> AutoPipelineResult:
        """Run a bounded auto pipeline using injected side-effecting dependencies."""
        ledger = (
            SeedDraftLedger.from_dict(state.ledger)
            if state.ledger
            else SeedDraftLedger.from_goal(state.goal)
        )
        if self.skip_run and not state.skip_run:
            state.skip_run = True
        resume_tool_name = state.last_tool_name
        if state.seed_artifact:
            try:
                Seed.from_dict(state.seed_artifact)
            except Exception as exc:
                _mark_invalid_seed_artifact(state, f"persisted Seed artifact is invalid: {exc}")
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
        self._save(state)

        if state.phase == AutoPhase.COMPLETE and (
            self.attach_execution_id or self.attach_job_id or self.attach_run_session_id
        ):
            attached, attach_transient = self._attach_run_if_requested(state)
            if attached is not None:
                self._save(state)
                if attached is False:
                    blocker = attach_transient or state.last_error
                else:
                    blocker = None
                status_override = "blocked" if attached is False else None
                return self._result(
                    state,
                    ledger,
                    blocker=blocker,
                    status_override=status_override,
                )
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
            self._save(state)

        review: SeedReview | None = None
        if state.phase in {AutoPhase.CREATED, AutoPhase.INTERVIEW}:
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
                interview = await self.interview_driver.run(state, ledger)
                if interview.status == "blocked":
                    return self._result(state, ledger, blocker=interview.blocker)
                state.interview_completed = True
                state.transition(AutoPhase.SEED_GENERATION, "generating Seed from auto interview")
                self._save(state)
        elif state.phase == AutoPhase.REPAIR:
            state.transition(AutoPhase.REVIEW, "resuming review after repair checkpoint")
            self._save(state)
        elif state.phase not in {AutoPhase.SEED_GENERATION, AutoPhase.REVIEW, AutoPhase.RUN}:
            state.mark_blocked(
                f"Cannot resume auto pipeline from {state.phase.value} without persisted Seed artifact",
                tool_name="auto_pipeline",
            )
            self._save(state)
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
                try:
                    seed = await asyncio.wait_for(
                        self.seed_generator(state.interview_session_id),
                        timeout=self.seed_timeout_seconds,
                    )
                    if not isinstance(seed, Seed):
                        msg = f"seed generator returned {type(seed).__name__}, expected Seed"
                        raise TypeError(msg)
                    state.seed_id = seed.metadata.seed_id
                    state.seed_artifact = seed.to_dict()
                except TimeoutError as exc:
                    state.mark_blocked(
                        f"seed generation timed out after {self.seed_timeout_seconds:.0f}s",
                        tool_name="seed_generator",
                    )
                    self._save(state)
                    return self._result(state, ledger, blocker=str(exc) or state.last_error)
                except Exception as exc:
                    state.mark_failed(f"seed generation failed: {exc}", tool_name="seed_generator")
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

        if state.phase == AutoPhase.REVIEW:
            reviewer = self.reviewer or SeedReviewer(self.grade_gate)
            repairer = self.repairer or SeedRepairer(reviewer=reviewer)
            seed, review, repairs = repairer.converge(seed, ledger=ledger)
            state.seed_artifact = seed.to_dict()
            state.repair_round = len(repairs)
            state.last_grade = review.grade_result.grade.value
            state.findings = [asdict(finding) for finding in review.findings]
            state.ledger = ledger.to_dict()
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

        if state.phase == AutoPhase.RUN:
            attached, _attach_transient = self._attach_run_if_requested(state)
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
                state.transition(
                    AutoPhase.COMPLETE, "execution already started; using persisted run handle"
                )
                self._save(state)
                return self._result(state, ledger, review=review)
            if state.run_start_attempted:
                _mark_unknown_run_handoff(state)
                state.mark_blocked(
                    state.run_handoff_guidance
                    or "Run start status is unknown; refusing to start a duplicate execution",
                    tool_name="run_starter",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)
            if not _grade_meets_required(state.last_grade, state.required_grade):
                state.mark_blocked(
                    f"Cannot start execution without a persisted grade meeting {state.required_grade}",
                    tool_name="grade_gate",
                )
                self._save(state)
                return self._result(state, ledger, review=review, blocker=state.last_error)
            if review is None:
                reviewer = self.reviewer or SeedReviewer(self.grade_gate)
                review = reviewer.review(seed, ledger=ledger)
                state.last_grade = review.grade_result.grade.value
                state.findings = [asdict(finding) for finding in review.findings]
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
        state.run_start_attempted = True
        self._save(state)
        try:
            run_meta = await asyncio.wait_for(
                self.run_starter(seed), timeout=self.run_start_timeout_seconds
            )
            if not isinstance(run_meta, dict):
                msg = f"run starter returned {type(run_meta).__name__}, expected dict"
                raise TypeError(msg)
            state.job_id = _optional_str(run_meta.get("job_id"))
            state.execution_id = _optional_str(run_meta.get("execution_id"))
        except TimeoutError as exc:
            _mark_unknown_run_handoff(state, status="unknown_timeout")
            state.mark_blocked(
                f"run start timed out after {self.run_start_timeout_seconds:.0f}s",
                tool_name="run_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=str(exc) or state.last_error)
        except Exception as exc:
            state.run_start_attempted = False
            state.mark_failed(f"run start failed: {exc}", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        state.run_session_id = _optional_str(run_meta.get("session_id"))
        run_subagent = (
            run_meta.get("_subagent") if isinstance(run_meta.get("_subagent"), dict) else None
        )
        state.run_subagent = run_subagent or {}
        if not any((state.job_id, state.execution_id, state.run_session_id)):
            _mark_unknown_run_handoff(state)
            state.mark_blocked(
                state.run_handoff_guidance or "Run starter returned no tracking handle",
                tool_name="run_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        state.run_handoff_status = "started"
        state.run_handoff_guidance = None
        state.transition(
            AutoPhase.COMPLETE,
            f"execution started for grade {state.last_grade or state.required_grade} Seed",
        )
        self._save(state)
        return self._result(state, ledger, review=review, run_subagent=run_subagent)

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
        return AutoPipelineResult(
            status=status_override or state.phase.value,
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            grade=review.grade_result.grade.value if review else state.last_grade,
            seed_path=state.seed_path,
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
            assumptions=tuple(ledger.assumptions()),
            non_goals=tuple(ledger.non_goals()),
            blocker=blocker or state.last_error,
        )

    def _attach_run_if_requested(self, state: AutoPipelineState) -> tuple[bool | None, str | None]:
        """Apply an attach request, returning ``(outcome, transient_blocker)``.

        Mirrors :meth:`_reconcile_run_if_requested`:

        - ``outcome`` is ``None`` when no attach was requested, ``True`` for a
          successful attach, and ``False`` when the request fails validation.
        - ``transient_blocker`` carries an invocation-only error message that
          must surface to the caller for the current call only. It is used for
          the invalid-context-on-complete case where mutating
          ``state.last_error`` durably would leak the misuse into every later
          plain ``--resume``/``--status`` response.
        """
        handle = _first_nonempty(
            self.attach_execution_id, self.attach_job_id, self.attach_run_session_id
        )
        if handle is None:
            return None, None
        if not state.run_start_attempted or state.run_handoff_status not in {
            "unknown_no_handle",
            "unknown_timeout",
        }:
            msg = (
                "--attach-execution requires an auto session with unknown run handoff "
                "status after a prior run start attempt"
            )
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
        return True, None

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
                "--reconcile-run requires an auto session with unknown run handoff "
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
            "unknown handoff. No duplicate run was started. Attach a verified handle with "
            "--attach-execution/--attach-job/--attach-session, or add a runtime-specific "
            "reconciler that returns attached, not_found, ambiguous, or unsupported."
        )
        state.mark_blocked(state.run_handoff_guidance, tool_name="run_starter")
        return False, None

    def _save(self, state: AutoPipelineState) -> None:
        if self.store is not None:
            self.store.save(state)


def _mark_invalid_seed_artifact(state: AutoPipelineState, message: str) -> None:
    state.seed_artifact = {}
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
            "The runtime may still have created an execution. Resume will not start "
            "another run automatically or risk duplicate execution; inspect the "
            "runtime for an existing execution before rerunning manually."
        )
        return
    state.run_handoff_guidance = (
        "Run starter was attempted, but no durable tracking handle was captured. "
        "Resume will not start another run automatically or risk duplicate execution; "
        "inspect the runtime for an existing execution before rerunning manually."
    )


def _grade_meets_required(actual: str | None, required: str) -> bool:
    rank = {"A": 0, "B": 1, "C": 2}
    if actual not in rank or required not in rank:
        return False
    return rank[actual] <= rank[required]


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
    if tool_name in {"seed_saver", "grade_gate", "seed_loader"}:
        return AutoPhase.REVIEW
    if tool_name == "run_starter":
        return AutoPhase.RUN
    return None


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        normalized = _optional_str(value)
        if normalized is not None:
            return normalized
    return None


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
