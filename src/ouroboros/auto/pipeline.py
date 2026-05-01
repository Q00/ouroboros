"""Full-quality AutoPipeline supervisor skeleton."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from ouroboros.auto.grading import GradeGate
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import Seed

SeedGenerator = Callable[[str], Awaitable[Seed]]
RunStarter = Callable[[Seed], Awaitable[dict[str, str | None]]]


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
    skip_run: bool = False

    async def run(self, state: AutoPipelineState) -> AutoPipelineResult:
        """Run a bounded auto pipeline using injected side-effecting dependencies."""
        ledger = (
            SeedDraftLedger.from_dict(state.ledger)
            if state.ledger
            else SeedDraftLedger.from_goal(state.goal)
        )
        self._save(state)

        if state.phase in {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED}:
            return self._result(state, ledger, blocker=state.last_error)

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
            try:
                seed = await self.seed_generator(state.interview_session_id or "")
                if not isinstance(seed, Seed):
                    msg = f"seed generator returned {type(seed).__name__}, expected Seed"
                    raise TypeError(msg)
                state.seed_id = seed.metadata.seed_id
                state.seed_artifact = seed.to_dict()
            except Exception as exc:
                state.mark_failed(f"seed generation failed: {exc}", tool_name="seed_generator")
                self._save(state)
                return self._result(state, ledger, blocker=state.last_error)
            state.mark_progress("Seed generated", tool_name="seed_generator")
            self._save(state)
            state.transition(AutoPhase.REVIEW, "reviewing Seed for A-grade")
            self._save(state)
        elif state.seed_artifact:
            seed = Seed.from_dict(state.seed_artifact)
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
            self._save(state)

            if not review.may_run:
                state.mark_blocked("Seed did not reach A-grade", tool_name="grade_gate")
                self._save(state)
                return self._result(
                    state, ledger, review=review, blocker="Seed did not reach A-grade"
                )

            if self.skip_run:
                state.transition(AutoPhase.COMPLETE, "A-grade Seed ready; skip-run requested")
                self._save(state)
                return self._result(state, ledger, review=review)
        else:
            review = None

        if state.phase == AutoPhase.RUN:
            if any((state.job_id, state.execution_id)):
                state.transition(
                    AutoPhase.COMPLETE, "execution already started; using persisted run handle"
                )
                self._save(state)
                return self._result(state, ledger, review=review)
            state.mark_blocked(
                "Run start status is unknown; refusing to start a duplicate execution",
                tool_name="run_starter",
            )
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)

        if self.run_starter is None:
            state.mark_blocked("No run starter configured", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker="No run starter configured")

        if state.phase != AutoPhase.RUN:
            state.transition(AutoPhase.RUN, "starting execution for A-grade Seed")
            self._save(state)
        try:
            run_meta = await self.run_starter(seed)
            if not isinstance(run_meta, dict):
                msg = f"run starter returned {type(run_meta).__name__}, expected dict"
                raise TypeError(msg)
            state.job_id = _optional_str(run_meta.get("job_id"))
            state.execution_id = _optional_str(run_meta.get("execution_id"))
        except Exception as exc:
            state.mark_failed(f"run start failed: {exc}", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        if not any((state.job_id, state.execution_id)):
            state.mark_blocked("Run starter returned no tracking handle", tool_name="run_starter")
            self._save(state)
            return self._result(state, ledger, review=review, blocker=state.last_error)
        state.transition(AutoPhase.COMPLETE, "execution started for A-grade Seed")
        self._save(state)
        return self._result(state, ledger, review=review)

    def _result(
        self,
        state: AutoPipelineState,
        ledger: SeedDraftLedger,
        *,
        review: SeedReview | None = None,
        blocker: str | None = None,
    ) -> AutoPipelineResult:
        return AutoPipelineResult(
            status=state.phase.value,
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            grade=review.grade_result.grade.value if review else state.last_grade,
            seed_path=state.seed_path,
            interview_session_id=state.interview_session_id,
            execution_id=state.execution_id,
            job_id=state.job_id,
            assumptions=tuple(ledger.assumptions()),
            non_goals=tuple(ledger.non_goals()),
            blocker=blocker or state.last_error,
        )

    def _save(self, state: AutoPipelineState) -> None:
        if self.store is not None:
            self.store.save(state)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
