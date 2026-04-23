"""Serial compounding AC executor.

Subclass of :class:`ouroboros.orchestrator.parallel_executor.ParallelACExecutor`
that runs acceptance criteria strictly one at a time, threading a rolling
postmortem chain from each AC into the prompt of the next.

Design (phase 1):
- Reuses ``_execute_single_ac`` from the parallel base class via the
  ``context_override`` kwarg so the ~1150-line prompt+runtime machinery is
  NOT duplicated or extracted.
- Linearizes the dependency plan into a single total order by walking
  stages then AC indices; dependency semantics are respected because
  ``StagedExecutionPlan`` already produces stages in topological order.
- After each AC, builds an :class:`ACPostmortem` from the existing
  :func:`extract_level_context` summarization machinery, appends it to the
  rolling chain, and emits an ``execution.ac.postmortem.captured`` event.
- On failure after retries, the loop halts (fail-fast) matching the
  "atomic" semantics requested by the user. The accumulated postmortems
  are still returned for inspection.

Out of scope for phase 1 (follow-up milestones):
- Per-AC git commits + diff_summary population (M5).
- AC-granular checkpoint/resume (M6).
- Inline QA + retry-with-QA feedback (M7).
- Prompt-cache-friendly structured system blocks (phase 2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.events import create_ac_postmortem_captured_event
from ouroboros.orchestrator.level_context import (
    ACPostmortem,
    PostmortemChain,
    PostmortemStatus,
    build_postmortem_chain_prompt,
    extract_level_context,
)
from ouroboros.orchestrator.parallel_executor import (
    ParallelACExecutor,
    _STALL_SENTINEL,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
)
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.dependency_analyzer import (
        DependencyGraph,
        StagedExecutionPlan,
    )
    from ouroboros.orchestrator.mcp_config import MCPToolDefinition

log = get_logger(__name__)


def linearize_execution_plan(execution_plan: "StagedExecutionPlan") -> tuple[int, ...]:
    """Flatten a staged execution plan into a total AC order.

    Walks stages in order (which are already topologically sorted by the
    planner), emitting AC indices within each stage in sorted order so the
    result is deterministic given the same plan.

    Returns:
        Tuple of AC indices in the order serial execution should visit them.
    """
    ordered: list[int] = []
    for stage in execution_plan.stages:
        for ac_index in sorted(stage.ac_indices):
            if ac_index not in ordered:
                ordered.append(ac_index)
    return tuple(ordered)


class SerialCompoundingExecutor(ParallelACExecutor):
    """Run ACs one at a time, compounding context via postmortems.

    Extends :class:`ParallelACExecutor` to reuse the per-AC runtime, retry,
    decomposition, and event-emission machinery without extracting it.
    Only the outer orchestration (linearization + postmortem threading)
    differs.
    """

    async def execute_serial(
        self,
        seed: "Seed",
        *,
        session_id: str,
        execution_id: str,
        tools: list[str],
        system_prompt: str,
        tool_catalog: "tuple[MCPToolDefinition, ...] | None" = None,
        dependency_graph: "DependencyGraph | None" = None,
        execution_plan: "StagedExecutionPlan | None" = None,
        fail_fast: bool = True,
        externally_satisfied_acs: "dict[int, dict[str, Any]] | None" = None,
    ) -> ParallelExecutionResult:
        """Execute ACs strictly serially with compounding postmortems.

        Args:
            seed: Seed specification whose ACs are being executed.
            session_id: Parent session id for tracking and event aggregation.
            execution_id: Execution id for event tracking.
            tools: Tool names available to the agent.
            system_prompt: System prompt used for every AC (pinned for the
                whole run to keep the prefix stable for prompt-cache hits
                when the adapter supports them).
            tool_catalog: Optional tool metadata catalog.
            dependency_graph: Dependency graph; used only when
                ``execution_plan`` is not supplied.
            execution_plan: Pre-built staged plan. When absent,
                ``dependency_graph.to_execution_plan()`` is used.
            fail_fast: When True (default), halt at the first AC that
                fails after retries. The compounding chain up to that
                point is still returned. When False, continue to the
                next AC with a failed postmortem recorded.
            externally_satisfied_acs: Map of AC indices already satisfied
                externally. When provided, those ACs will be skipped and
                recorded with SATISFIED_EXTERNALLY outcome.

        Returns:
            ParallelExecutionResult with one stage per AC so downstream
            progress tooling sees a structurally similar shape to the
            parallel path.
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        ac_order = linearize_execution_plan(execution_plan)
        start_time = datetime.now(UTC)

        chain = PostmortemChain()
        results: list[ACExecutionResult] = []
        stages: list[ParallelExecutionStageResult] = []
        execution_counters = {"messages_count": 0, "tool_calls_count": 0}
        external_completed = externally_satisfied_acs or {}

        log.info(
            "serial_executor.started",
            session_id=session_id,
            execution_id=execution_id,
            total_acs=len(ac_order),
            fail_fast=fail_fast,
        )

        halted = False
        for position, ac_index in enumerate(ac_order):
            if halted:
                # Record remaining ACs as blocked so downstream tooling sees
                # a complete picture without the serial loop running them.
                blocked = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=False,
                    error="blocked: serial loop halted after upstream AC failure",
                    outcome=ACExecutionOutcome.BLOCKED,
                )
                results.append(blocked)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(blocked,),
                        started=False,
                    )
                )
                continue

            # Check if AC is externally satisfied; skip execution if so.
            if ac_index in external_completed:
                metadata = external_completed.get(ac_index, {})
                reason = metadata.get("reason")
                commit = metadata.get("commit")
                notes: list[str] = [
                    "Skipped via --skip-completed; existing working tree state is treated as satisfied."
                ]
                if isinstance(reason, str) and reason.strip():
                    notes.append(f"Reason: {reason.strip()}")
                if isinstance(commit, str) and commit.strip():
                    notes.append(f"Commit: {commit.strip()}")

                satisfied_result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=True,
                    final_message="\n".join(notes),
                    retry_attempt=0,
                    outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                )
                results.append(satisfied_result)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(satisfied_result,),
                        started=False,
                    )
                )
                log.info(
                    "serial_executor.ac.satisfied_externally",
                    session_id=session_id,
                    ac_index=ac_index,
                    reason=reason,
                    commit=commit,
                )
                # Still add to postmortem chain to provide context
                postmortem = self._build_postmortem_from_result(
                    satisfied_result, workspace_root=self._task_cwd
                )
                chain = chain.append(postmortem)
                continue

            # Compose the compounding-context section from the current chain.
            context_section = build_postmortem_chain_prompt(chain)

            ac_content = seed.acceptance_criteria[ac_index]

            self._console.print(
                f"[bold cyan]Serial AC {ac_index + 1}/{len(ac_order)}[/bold cyan]"
                f" [{len(chain.postmortems)} postmortems in chain]"
            )
            self._flush_console()

            try:
                result = await self._execute_single_ac(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    session_id=session_id,
                    tools=tools,
                    tool_catalog=tool_catalog,
                    system_prompt=system_prompt,
                    seed_goal=seed.goal,
                    depth=0,
                    execution_id=execution_id,
                    level_contexts=None,
                    sibling_acs=None,  # serial: no siblings
                    retry_attempt=0,
                    execution_counters=execution_counters,
                    context_override=context_section,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "serial_executor.ac.unexpected_error",
                    session_id=session_id,
                    ac_index=ac_index,
                    error=str(exc),
                )
                result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error=f"unexpected executor error: {exc}",
                    outcome=ACExecutionOutcome.FAILED,
                )

            results.append(result)

            postmortem = self._build_postmortem_from_result(
                result, workspace_root=self._task_cwd
            )
            chain = chain.append(postmortem)

            await self._safe_emit_event(
                create_ac_postmortem_captured_event(
                    session_id=session_id,
                    ac_index=ac_index,
                    ac_id=f"ac_{ac_index}",
                    postmortem=postmortem,
                    execution_id=execution_id,
                    retry_attempt=result.retry_attempt,
                )
            )

            stages.append(
                ParallelExecutionStageResult(
                    stage_index=position,
                    ac_indices=(ac_index,),
                    results=(result,),
                    started=True,
                )
            )

            if not result.success and fail_fast:
                log.warning(
                    "serial_executor.halting_on_failure",
                    session_id=session_id,
                    ac_index=ac_index,
                    error=result.error,
                )
                halted = True

        total_duration = (datetime.now(UTC) - start_time).total_seconds()
        success_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.SUCCEEDED
        )
        externally_satisfied_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        )
        failure_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.FAILED
        )
        blocked_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.BLOCKED
        )
        # Serial execution has no INVALID outcomes (all ACs are in the linearized plan),
        # so skipped_count equals blocked_count.
        skipped_count = blocked_count

        log.info(
            "serial_executor.completed",
            session_id=session_id,
            total_acs=len(ac_order),
            success=success_count,
            externally_satisfied=externally_satisfied_count,
            failed=failure_count,
            blocked=blocked_count,
            skipped=skipped_count,
            duration_seconds=total_duration,
            postmortems_captured=len(chain.postmortems),
        )

        return ParallelExecutionResult(
            results=tuple(results),
            success_count=success_count,
            failure_count=failure_count,
            externally_satisfied_count=externally_satisfied_count,
            blocked_count=blocked_count,
            skipped_count=skipped_count,
            stages=tuple(stages),
            total_messages=execution_counters.get("messages_count", 0),
            total_duration_seconds=total_duration,
        )

    @staticmethod
    def _build_postmortem_from_result(
        result: ACExecutionResult,
        *,
        workspace_root: str | None,
    ) -> ACPostmortem:
        """Derive an ACPostmortem from an ACExecutionResult.

        Uses the existing :func:`extract_level_context` summarization
        (which already folds tool-use events into files_modified, tools_used,
        key_output, and public_api) for a deterministic reconstruction of
        the factual half of the postmortem. The compounding-specific fields
        (diff_summary, gotchas, qa_suggestions, invariants_established)
        remain empty in phase 1 — populated by later milestones.
        """
        # extract_level_context expects a list[tuple[idx, content, success, msgs, final_msg]]
        level_ctx = extract_level_context(
            ac_results=[
                (
                    result.ac_index,
                    result.ac_content,
                    result.success,
                    result.messages,
                    result.final_message,
                )
            ],
            level_num=0,
            workspace_root=workspace_root or "",
        )
        if level_ctx.completed_acs:
            summary = level_ctx.completed_acs[0]
        else:  # pragma: no cover — extract_level_context always returns one summary per input
            from ouroboros.orchestrator.level_context import ACContextSummary

            summary = ACContextSummary(
                ac_index=result.ac_index,
                ac_content=result.ac_content,
                success=result.success,
            )

        status: PostmortemStatus
        if result.success:
            status = "pass"
        elif result.outcome == ACExecutionOutcome.BLOCKED:
            status = "partial"
        elif (
            result.error == _STALL_SENTINEL
            or result.outcome == ACExecutionOutcome.FAILED
        ):
            status = "fail"
        else:
            status = "fail"

        gotchas: tuple[str, ...] = ()
        if not result.success and result.error:
            gotchas = (result.error,)

        return ACPostmortem(
            summary=summary,
            status=status,
            retry_attempts=result.retry_attempt,
            duration_seconds=result.duration_seconds,
            ac_native_session_id=result.session_id,
            gotchas=gotchas,
        )


__all__ = [
    "SerialCompoundingExecutor",
    "linearize_execution_plan",
]