"""Parallel AC execution orchestrator with Sub-AC decomposition.

Executes acceptance criteria in parallel groups based on dependency analysis.
Complex ACs are decomposed into Sub-ACs and executed in parallel.

Features:
- Parallel execution within dependency levels
- Claude-driven decomposition of complex ACs into Sub-ACs
- Parallel execution of Sub-ACs (each in separate Claude session)
- Event emission for TUI progress tracking

Example:
    executor = ParallelACExecutor(adapter, event_store, console)
    result = await executor.execute_parallel(
        seed=seed,
        dependency_graph=graph,
        session_id="sess_123",
        tools=["Read", "Write", "Bash"],
        system_prompt="You are an agent...",
    )

    if result.all_succeeded:
        print(f"All {result.success_count} ACs completed!")
    else:
        print(f"Partial: {result.success_count} succeeded, {result.failure_count} failed")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
from typing import TYPE_CHECKING, Any

import anyio
from rich.console import Console

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import LevelCoordinator
from ouroboros.orchestrator.level_context import (
    LevelContext,
    build_context_prompt,
    extract_level_context,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
    from ouroboros.orchestrator.dependency_analyzer import DependencyGraph
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)

# Decomposition constants
MAX_DECOMPOSITION_DEPTH = 2
MIN_SUB_ACS = 2
MAX_SUB_ACS = 5


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class ACExecutionResult:
    """Result of executing a single AC, including Sub-ACs if decomposed.

    Attributes:
        ac_index: 0-based AC index.
        ac_content: AC description.
        success: Whether execution succeeded.
        messages: All agent messages from execution.
        final_message: Final result message content.
        error: Error message if failed.
        duration_seconds: Execution duration.
        session_id: Claude session ID for this AC.
        is_decomposed: Whether this AC was decomposed into Sub-ACs.
        sub_results: Results from Sub-AC parallel executions.
        depth: Depth in decomposition tree (0 = root AC).
    """

    ac_index: int
    ac_content: str
    success: bool
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)
    final_message: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    session_id: str | None = None
    is_decomposed: bool = False
    sub_results: tuple[ACExecutionResult, ...] = field(default_factory=tuple)
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ParallelExecutionResult:
    """Result of parallel AC execution.

    Attributes:
        results: Individual results for each AC.
        success_count: Number of successful ACs.
        failure_count: Number of failed ACs.
        skipped_count: Number of skipped ACs (due to failed dependencies).
        total_messages: Total messages processed across all ACs.
        total_duration_seconds: Total execution time.
    """

    results: tuple[ACExecutionResult, ...]
    success_count: int
    failure_count: int
    skipped_count: int = 0
    total_messages: int = 0
    total_duration_seconds: float = 0.0

    @property
    def all_succeeded(self) -> bool:
        """Return True if all ACs succeeded."""
        return self.failure_count == 0 and self.skipped_count == 0

    @property
    def any_succeeded(self) -> bool:
        """Return True if at least one AC succeeded."""
        return self.success_count > 0


# =============================================================================
# Parallel Executor
# =============================================================================


class ParallelACExecutor:
    """Executes ACs in parallel based on dependency graph."""

    def __init__(
        self,
        adapter: ClaudeAgentAdapter,
        event_store: EventStore,
        console: Console | None = None,
        enable_decomposition: bool = True,
    ):
        """Initialize executor.

        Args:
            adapter: Claude Agent adapter for execution.
            event_store: Event store for progress tracking.
            console: Rich console for output.
            enable_decomposition: Enable Claude to decompose complex ACs.
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        self._enable_decomposition = enable_decomposition
        self._coordinator = LevelCoordinator(adapter)

    async def execute_parallel(
        self,
        seed: Seed,
        dependency_graph: DependencyGraph,
        session_id: str,
        execution_id: str,
        tools: list[str],
        system_prompt: str,
    ) -> ParallelExecutionResult:
        """Execute ACs in parallel according to dependency graph.

        Args:
            seed: Seed specification.
            dependency_graph: Dependency graph defining execution order.
            session_id: Parent session ID for tracking.
            execution_id: Execution ID for event tracking.
            tools: Tools available to agents.
            system_prompt: System prompt for agents.

        Returns:
            ParallelExecutionResult with outcomes for all ACs.
        """
        start_time = datetime.now(UTC)
        all_results: list[ACExecutionResult] = []
        failed_indices: set[int] = set()
        level_contexts: list[LevelContext] = []

        total_levels = dependency_graph.total_levels
        total_acs = len(seed.acceptance_criteria)

        # Track AC statuses for TUI updates
        ac_statuses: dict[int, str] = dict.fromkeys(range(total_acs), "pending")
        completed_count = 0

        # Validation: check all AC indices are present in dependency graph
        expected_indices = set(range(total_acs))
        actual_indices = {idx for level in dependency_graph.execution_levels for idx in level}
        missing_indices = expected_indices - actual_indices
        extra_indices = actual_indices - expected_indices

        if missing_indices:
            log.warning(
                "parallel_executor.missing_ac_indices",
                session_id=session_id,
                missing=sorted(missing_indices),
            )
            # Add missing ACs to results as errors
            for idx in sorted(missing_indices):
                all_results.append(
                    ACExecutionResult(
                        ac_index=idx,
                        ac_content=seed.acceptance_criteria[idx],
                        success=False,
                        error="Not included in dependency graph",
                    )
                )

        if extra_indices:
            log.error(
                "parallel_executor.invalid_ac_indices",
                session_id=session_id,
                extra=sorted(extra_indices),
                max_valid=total_acs - 1,
            )
            # Invalid indices will be skipped in the execution loop below

        log.info(
            "parallel_executor.execution.started",
            session_id=session_id,
            total_acs=total_acs,
            total_levels=total_levels,
            levels=dependency_graph.execution_levels,
        )

        # Emit initial progress for TUI
        await self._emit_workflow_progress(
            session_id=session_id,
            execution_id=execution_id,
            seed=seed,
            ac_statuses=ac_statuses,
            executing_indices=[],
            completed_count=0,
            current_level=1,
            total_levels=total_levels,
            activity="Starting parallel execution",
        )

        # Execute groups sequentially, but ACs within each group in parallel
        for level_idx, level in enumerate(dependency_graph.execution_levels):
            level_num = level_idx + 1

            # Check for skipped ACs (dependencies failed)
            executable: list[int] = []
            skipped: list[int] = []

            for ac_idx in level:
                # Skip invalid indices
                if ac_idx < 0 or ac_idx >= total_acs:
                    continue

                deps = dependency_graph.get_dependencies(ac_idx)
                if any(dep in failed_indices for dep in deps):
                    skipped.append(ac_idx)
                else:
                    executable.append(ac_idx)

            # Add skipped results
            for ac_idx in skipped:
                all_results.append(
                    ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        success=False,
                        error="Skipped: dependency failed",
                    )
                )
                ac_statuses[ac_idx] = "skipped"
                log.info(
                    "parallel_executor.ac.skipped",
                    session_id=session_id,
                    ac_index=ac_idx,
                    reason="dependency_failed",
                )

            if not executable:
                continue

            # Mark ACs as executing
            for ac_idx in executable:
                ac_statuses[ac_idx] = "executing"

            self._console.print(
                f"\n[cyan]Level {level_num}/{total_levels}: "
                f"Executing ACs {executable} in parallel[/cyan]"
            )

            # Emit level started event
            await self._emit_level_started(
                session_id=session_id,
                level=level_num,
                ac_indices=executable,
                total_levels=total_levels,
            )

            # Emit progress with executing status for TUI
            await self._emit_workflow_progress(
                session_id=session_id,
                execution_id=execution_id,
                seed=seed,
                ac_statuses=ac_statuses,
                executing_indices=executable,
                completed_count=completed_count,
                current_level=level_num,
                total_levels=total_levels,
                activity="Executing",
            )

            # Execute level in parallel using anyio task group
            # (anyio manages cancel scopes correctly across concurrent tasks,
            # unlike asyncio.gather which creates separate asyncio Tasks
            # that break the SDK's internal cancel scope tracking)
            level_results: list[ACExecutionResult | BaseException] = [None] * len(executable)

            # Capture current contexts for this level's closure
            current_contexts = list(level_contexts)

            # Build sibling AC descriptions for parallel awareness
            sibling_acs = (
                [seed.acceptance_criteria[i] for i in executable] if len(executable) > 1 else []
            )

            async def _run_ac(idx: int, ac_idx: int) -> None:
                try:
                    level_results[idx] = await self._execute_single_ac(
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        session_id=session_id,
                        tools=tools,
                        system_prompt=system_prompt,
                        seed_goal=seed.goal,
                        depth=0,
                        execution_id=execution_id,
                        level_contexts=current_contexts,
                        sibling_acs=sibling_acs,
                    )
                except BaseException as e:
                    # Never suppress anyio Cancelled — doing so breaks
                    # the task group's cancel-scope propagation and can
                    # cause the entire group to hang indefinitely.
                    if isinstance(e, anyio.get_cancelled_exc_class()):
                        raise
                    level_results[idx] = e

            async with anyio.create_task_group() as tg:
                for i, ac_idx in enumerate(executable):
                    tg.start_soon(_run_ac, i, ac_idx)

            # Process results
            level_success = 0
            level_failed = 0

            for ac_idx, result in zip(executable, level_results, strict=False):
                if isinstance(result, BaseException):
                    # Exception during execution
                    error_msg = str(result)
                    ac_result = ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        success=False,
                        error=error_msg,
                    )
                    failed_indices.add(ac_idx)
                    level_failed += 1
                    ac_statuses[ac_idx] = "failed"

                    log.error(
                        "parallel_executor.ac.exception",
                        session_id=session_id,
                        ac_index=ac_idx,
                        error=error_msg,
                    )
                else:
                    ac_result = result
                    if ac_result.success:
                        level_success += 1
                        ac_statuses[ac_idx] = "completed"
                        completed_count += 1
                    else:
                        failed_indices.add(ac_idx)
                        level_failed += 1
                        ac_statuses[ac_idx] = "failed"

                all_results.append(ac_result)

            # Emit level completed event
            await self._emit_level_completed(
                session_id=session_id,
                level=level_num,
                success_count=level_success,
                failure_count=level_failed,
            )

            # Emit progress after level completes
            await self._emit_workflow_progress(
                session_id=session_id,
                execution_id=execution_id,
                seed=seed,
                ac_statuses=ac_statuses,
                executing_indices=[],
                completed_count=completed_count,
                current_level=level_num,
                total_levels=total_levels,
                activity=f"Level {level_num} complete",
            )

            self._console.print(
                f"[green]Level {level_num} complete: "
                f"{level_success} succeeded, {level_failed} failed[/green]"
            )

            # Extract context from this level for next level's ACs
            if level_success > 0:
                level_ac_data = [
                    (r.ac_index, r.ac_content, r.success, r.messages, r.final_message)
                    for r in all_results
                    if isinstance(r, ACExecutionResult) and r.ac_index in executable
                ]
                level_ctx = extract_level_context(level_ac_data, level_num)

                # Coordinator: detect and resolve file conflicts (Approach A)
                level_ac_results = [
                    r
                    for r in all_results
                    if isinstance(r, ACExecutionResult) and r.ac_index in executable
                ]
                conflicts = self._coordinator.detect_file_conflicts(level_ac_results)

                if conflicts:
                    self._console.print(
                        f"  [yellow]Coordinator: {len(conflicts)} file conflict(s) detected, "
                        f"starting review...[/yellow]"
                    )
                    review = await self._coordinator.run_review(
                        conflicts=conflicts,
                        level_context=level_ctx,
                        level_number=level_num,
                    )
                    # Attach review to the level context
                    level_ctx = LevelContext(
                        level_number=level_ctx.level_number,
                        completed_acs=level_ctx.completed_acs,
                        coordinator_review=review,
                    )
                    self._console.print(
                        f"  [green]Coordinator review complete: "
                        f"{len(review.fixes_applied)} fix(es), "
                        f"{len(review.warnings_for_next_level)} warning(s)[/green]"
                    )

                level_contexts.append(level_ctx)

        # Aggregate results - sort by AC index for consistent ordering
        sorted_results = sorted(all_results, key=lambda r: r.ac_index)
        total_duration = (datetime.now(UTC) - start_time).total_seconds()
        success_count = sum(1 for r in sorted_results if r.success)
        failure_count = sum(
            1
            for r in sorted_results
            if not r.success
            and r.error not in ("Skipped: dependency failed", "Not included in dependency graph")
        )
        skipped_count = sum(
            1
            for r in sorted_results
            if r.error in ("Skipped: dependency failed", "Not included in dependency graph")
        )
        total_messages = sum(len(r.messages) for r in sorted_results)

        log.info(
            "parallel_executor.execution.completed",
            session_id=session_id,
            success_count=success_count,
            failure_count=failure_count,
            skipped_count=skipped_count,
            total_messages=total_messages,
            duration_seconds=total_duration,
        )

        return ParallelExecutionResult(
            results=tuple(sorted_results),
            success_count=success_count,
            failure_count=failure_count,
            skipped_count=skipped_count,
            total_messages=total_messages,
            total_duration_seconds=total_duration,
        )

    async def _execute_single_ac(
        self,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        system_prompt: str,
        seed_goal: str,
        depth: int = 0,
        execution_id: str = "",
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[str] | None = None,
    ) -> ACExecutionResult:
        """Execute a single AC, decomposing into parallel Sub-ACs if complex.

        Flow:
        1. Ask Claude to analyze if AC needs decomposition
        2. If decomposable → get Sub-ACs → execute in parallel
        3. If atomic → execute directly

        Args:
            ac_index: 0-based AC index.
            ac_content: AC description.
            session_id: Parent session ID.
            tools: Tools for the agent.
            system_prompt: System prompt.
            seed_goal: Overall goal from seed.
            depth: Current depth in decomposition tree.
            execution_id: Execution ID for event tracking.
            level_contexts: Context from previously completed levels.
            sibling_acs: Descriptions of ACs running in parallel at this level.

        Returns:
            ACExecutionResult for this AC.
        """
        start_time = datetime.now(UTC)

        log.info(
            "parallel_executor.ac.started",
            parent_session_id=session_id,
            ac_index=ac_index,
            depth=depth,
        )

        # Try decomposition if enabled and not too deep
        if self._enable_decomposition and depth < MAX_DECOMPOSITION_DEPTH:
            sub_acs = await self._try_decompose_ac(
                ac_content=ac_content,
                ac_index=ac_index,
                seed_goal=seed_goal,
                tools=tools,
                system_prompt=system_prompt,
            )

            if sub_acs and len(sub_acs) >= MIN_SUB_ACS:
                # Decomposition successful - execute Sub-ACs in parallel
                self._console.print(
                    f"  [cyan]AC {ac_index + 1} → Decomposed into {len(sub_acs)} Sub-ACs (parallel)[/cyan]"
                )

                # Emit decomposition event for TUI
                for i, sub_ac in enumerate(sub_acs):
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_desc=sub_ac[:50],
                        status="pending",
                    )

                # Execute Sub-ACs in parallel
                sub_results = await self._execute_sub_acs_parallel(
                    parent_ac_index=ac_index,
                    sub_acs=sub_acs,
                    session_id=session_id,
                    tools=tools,
                    system_prompt=system_prompt,
                    seed_goal=seed_goal,
                    depth=depth + 1,
                    execution_id=execution_id,
                    level_contexts=level_contexts,
                )

                # Update TUI with final statuses
                for i, result in enumerate(sub_results):
                    status = "completed" if result.success else "failed"
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_desc=sub_acs[i][:50],
                        status=status,
                    )

                duration = (datetime.now(UTC) - start_time).total_seconds()
                all_success = all(r.success for r in sub_results)

                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=all_success,
                    messages=(),
                    final_message=f"Decomposed into {len(sub_acs)} Sub-ACs",
                    duration_seconds=duration,
                    is_decomposed=True,
                    sub_results=tuple(sub_results),
                    depth=depth,
                )

        # Execute atomic AC directly
        return await self._execute_atomic_ac(
            ac_index=ac_index,
            ac_content=ac_content,
            session_id=session_id,
            tools=tools,
            system_prompt=system_prompt,
            seed_goal=seed_goal,
            depth=depth,
            start_time=start_time,
            level_contexts=level_contexts,
            sibling_acs=sibling_acs,
        )

    async def _try_decompose_ac(
        self,
        ac_content: str,
        ac_index: int,
        seed_goal: str,
        tools: list[str],
        system_prompt: str,
    ) -> list[str] | None:
        """Ask Claude to decompose AC into Sub-ACs if complex.

        Returns:
            List of Sub-AC descriptions, or None if AC is atomic.
        """
        decompose_prompt = f"""Analyze this acceptance criterion and determine if it should be decomposed.

## Goal Context
{seed_goal}

## Acceptance Criterion (AC #{ac_index + 1})
{ac_content}

## Instructions
If this AC is complex (requires multiple distinct steps that could run independently),
decompose it into {MIN_SUB_ACS}-{MAX_SUB_ACS} smaller Sub-ACs.

If the AC is simple/atomic (can be done in one focused task), respond with: ATOMIC

If decomposing, respond with ONLY a JSON array of Sub-AC descriptions:
["Sub-AC 1: description", "Sub-AC 2: description", ...]

Each Sub-AC should be:
- Independently executable
- Specific and focused
- Part of achieving the parent AC
- Targeting distinct files or distinct sections within shared files (avoid overlap)

Respond with either "ATOMIC" or the JSON array only, nothing else.
"""

        try:
            response_text = ""
            # NOTE: Do NOT use `break` or `aclosing` with the SDK generator.
            # The SDK uses anyio cancel scopes internally. If the generator
            # is closed via aclose() (from break or aclosing), the cancel scope
            # cleanup creates background asyncio Tasks that cancel other
            # running tasks. Let the generator complete naturally instead.
            async for message in self._adapter.execute_task(
                prompt=decompose_prompt,
                tools=[],  # No tools for decomposition analysis
                system_prompt="You are a task decomposition expert. Analyze tasks and break them down if needed.",
            ):
                if message.content:
                    response_text = message.content

            # Parse response
            response_text = response_text.strip()

            if "ATOMIC" in response_text.upper():
                log.info(
                    "parallel_executor.decomposition.atomic",
                    ac_index=ac_index,
                )
                return None

            # Try to extract JSON array
            json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
            if json_match:
                sub_acs = json.loads(json_match.group())
                if isinstance(sub_acs, list) and all(isinstance(s, str) for s in sub_acs):
                    if MIN_SUB_ACS <= len(sub_acs) <= MAX_SUB_ACS:
                        log.info(
                            "parallel_executor.decomposition.success",
                            ac_index=ac_index,
                            sub_ac_count=len(sub_acs),
                        )
                        return sub_acs

            log.warning(
                "parallel_executor.decomposition.parse_failed",
                ac_index=ac_index,
                response_preview=response_text[:100],
            )
            return None

        except Exception as e:
            log.warning(
                "parallel_executor.decomposition.error",
                ac_index=ac_index,
                error=str(e),
            )
            return None

    async def _execute_sub_acs_parallel(
        self,
        parent_ac_index: int,
        sub_acs: list[str],
        session_id: str,
        tools: list[str],
        system_prompt: str,
        seed_goal: str,
        depth: int,
        execution_id: str,
        level_contexts: list[LevelContext] | None = None,
    ) -> list[ACExecutionResult]:
        """Execute Sub-ACs in parallel, each in its own Claude session.

        Returns:
            List of ACExecutionResult for each Sub-AC.
        """
        self._console.print(f"    [green]Starting {len(sub_acs)} Sub-ACs in parallel...[/green]")

        # Execute all Sub-ACs in parallel using anyio task group
        # (preserves cancel scope context for SDK calls)
        sub_results: list[ACExecutionResult | BaseException] = [None] * len(sub_acs)

        async def _run_sub_ac(idx: int, sub_ac: str) -> None:
            try:
                # Mark Sub-AC as executing before starting
                await self._emit_subtask_event(
                    execution_id=execution_id,
                    ac_index=parent_ac_index,
                    sub_task_index=idx + 1,
                    sub_task_desc=sub_ac[:50],
                    status="executing",
                )

                sub_results[idx] = await self._execute_atomic_ac(
                    ac_index=parent_ac_index * 100 + idx,
                    ac_content=sub_ac,
                    session_id=session_id,
                    tools=tools,
                    system_prompt=system_prompt,
                    seed_goal=seed_goal,
                    depth=depth,
                    start_time=datetime.now(UTC),
                    is_sub_ac=True,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=idx,
                    level_contexts=level_contexts,
                )
            except BaseException as e:
                if isinstance(e, anyio.get_cancelled_exc_class()):
                    raise
                sub_results[idx] = e

        async with anyio.create_task_group() as tg:
            for i, sub_ac in enumerate(sub_acs):
                tg.start_soon(_run_sub_ac, i, sub_ac)

        # Convert exceptions to failed results
        final_results: list[ACExecutionResult] = []
        for i, result in enumerate(sub_results):
            if isinstance(result, BaseException):
                final_results.append(
                    ACExecutionResult(
                        ac_index=parent_ac_index * 100 + i,
                        ac_content=sub_acs[i],
                        success=False,
                        error=str(result),
                        depth=depth,
                    )
                )
            else:
                final_results.append(result)

        success_count = sum(1 for r in final_results if r.success)
        self._console.print(
            f"    [{'green' if success_count == len(sub_acs) else 'yellow'}]"
            f"Sub-ACs completed: {success_count}/{len(sub_acs)} succeeded[/]"
        )

        return final_results

    @staticmethod
    def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Format tool name with input detail for console output."""
        detail = ""
        if tool_name in ("Read", "Write", "Edit"):
            detail = tool_input.get("file_path", "")
        elif tool_name == "Bash":
            detail = tool_input.get("command", "")
        elif tool_name in ("Glob", "Grep"):
            detail = tool_input.get("pattern", "")
        elif tool_name.startswith("mcp__"):
            for v in tool_input.values():
                if v:
                    detail = str(v)[:50]
                    break
        if detail and len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{tool_name}: {detail}" if detail else tool_name

    async def _execute_atomic_ac(
        self,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        system_prompt: str,
        seed_goal: str,
        depth: int,
        start_time: datetime,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[str] | None = None,
    ) -> ACExecutionResult:
        """Execute an atomic AC directly via Claude Agent.

        Returns:
            ACExecutionResult for this AC.
        """
        ac_session_id: str | None = None

        # Build prompt
        if is_sub_ac:
            label = f"Sub-AC {sub_ac_index + 1} of AC {parent_ac_index + 1}"
            indent = "    "
        else:
            label = f"AC {ac_index + 1}"
            indent = "  "

        # Build context section from previous levels
        context_section = build_context_prompt(level_contexts or [])

        # Build parallel awareness section
        parallel_section = ""
        if sibling_acs and len(sibling_acs) > 1:
            other_acs = [ac for ac in sibling_acs if ac != ac_content]
            if other_acs:
                other_list = "\n".join(f"- {ac[:80]}" for ac in other_acs)
                parallel_section = (
                    "\n## Parallel Execution Notice\n"
                    "Other agents are working on sibling tasks concurrently. "
                    "Avoid modifying files that other agents are likely editing. "
                    "Focus on files directly related to YOUR task.\n\n"
                    f"Sibling tasks in progress:\n{other_list}\n"
                )

        prompt = f"""Execute the following task:

## Goal Context
{seed_goal}

## Your Task ({label})
{ac_content}
{context_section}{parallel_section}
Use the available tools to accomplish this task. Report your progress clearly.
When complete, explicitly state: [TASK_COMPLETE]
"""

        messages: list[AgentMessage] = []
        final_message = ""
        success = False

        try:
            async for message in self._adapter.execute_task(
                prompt=prompt,
                tools=tools,
                system_prompt=system_prompt,
            ):
                messages.append(message)

                if ac_session_id is None and message.data.get("session_id"):
                    ac_session_id = message.data["session_id"]

                if message.tool_name:
                    tool_input = message.data.get("tool_input", {})
                    tool_detail = self._format_tool_detail(message.tool_name, tool_input)
                    self._console.print(f"{indent}[yellow]{label} → {tool_detail}[/yellow]")

                    # Emit tool started event for TUI
                    ac_id = (
                        f"ac_{ac_index}"
                        if not is_sub_ac
                        else f"sub_ac_{parent_ac_index}_{sub_ac_index}"
                    )
                    from ouroboros.events.base import BaseEvent as _BaseEvent

                    tool_event = _BaseEvent(
                        type="execution.tool.started",
                        aggregate_type="execution",
                        aggregate_id=ac_id,
                        data={
                            "ac_id": ac_id,
                            "tool_name": message.tool_name,
                            "tool_detail": tool_detail,
                            "tool_input": tool_input,
                        },
                    )
                    await self._event_store.append(tool_event)

                if message.data.get("thinking"):
                    ac_id = (
                        f"ac_{ac_index}"
                        if not is_sub_ac
                        else f"sub_ac_{parent_ac_index}_{sub_ac_index}"
                    )
                    from ouroboros.events.base import BaseEvent as _BaseEvent

                    thinking_event = _BaseEvent(
                        type="execution.agent.thinking",
                        aggregate_type="execution",
                        aggregate_id=ac_id,
                        data={
                            "ac_id": ac_id,
                            "thinking_text": message.data["thinking"],
                        },
                    )
                    await self._event_store.append(thinking_event)

                if message.is_final:
                    final_message = message.content
                    success = not message.is_error

            duration = (datetime.now(UTC) - start_time).total_seconds()

            log.info(
                "parallel_executor.ac.completed",
                ac_index=ac_index,
                depth=depth,
                success=success,
                is_sub_ac=is_sub_ac,
                duration_seconds=duration,
            )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=success,
                messages=tuple(messages),
                final_message=final_message,
                duration_seconds=duration,
                session_id=ac_session_id,
                depth=depth,
            )

        except Exception as e:
            duration = (datetime.now(UTC) - start_time).total_seconds()

            log.exception(
                "parallel_executor.ac.failed",
                ac_index=ac_index,
                depth=depth,
                error=str(e),
            )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=tuple(messages),
                error=str(e),
                duration_seconds=duration,
                session_id=ac_session_id,
                depth=depth,
            )

    async def _emit_subtask_event(
        self,
        execution_id: str,
        ac_index: int,
        sub_task_index: int,
        sub_task_desc: str,
        status: str,
    ) -> None:
        """Emit sub-task event for TUI tree updates."""
        from ouroboros.events.base import BaseEvent

        event = BaseEvent(
            type="execution.subtask.updated",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "ac_index": ac_index,
                "sub_task_index": sub_task_index,
                "sub_task_id": f"ac_{ac_index}_sub_{sub_task_index}",
                "content": sub_task_desc,
                "status": status,
            },
        )
        await self._event_store.append(event)

    async def _emit_level_started(
        self,
        session_id: str,
        level: int,
        ac_indices: list[int],
        total_levels: int,
    ) -> None:
        """Emit event when a parallel level starts."""
        from ouroboros.events.base import BaseEvent

        event = BaseEvent(
            type="execution.decomposition.level_started",
            aggregate_type="execution",
            aggregate_id=session_id,
            data={
                "level": level - 1,  # TUI expects 0-based index
                "total_levels": total_levels,
                "child_indices": ac_indices,  # TUI expects this field name
                "ac_count": len(ac_indices),
            },
        )
        await self._event_store.append(event)

    async def _emit_level_completed(
        self,
        session_id: str,
        level: int,
        success_count: int,
        failure_count: int,
    ) -> None:
        """Emit event when a parallel level completes."""
        from ouroboros.events.base import BaseEvent

        event = BaseEvent(
            type="execution.decomposition.level_completed",
            aggregate_type="execution",
            aggregate_id=session_id,
            data={
                "level": level - 1,  # TUI expects 0-based index
                "successful": success_count,
                "total": success_count + failure_count,
            },
        )
        await self._event_store.append(event)

    async def _emit_workflow_progress(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        executing_indices: list[int],
        completed_count: int,
        current_level: int,
        total_levels: int,
        activity: str = "Executing",
    ) -> None:
        """Emit workflow progress event for TUI updates.

        Args:
            session_id: Session ID.
            execution_id: Execution ID.
            seed: Seed specification.
            ac_statuses: Dict mapping AC index to status string.
            executing_indices: Currently executing AC indices.
            completed_count: Number of completed ACs.
            current_level: Current execution level.
            total_levels: Total execution levels.
            activity: Current activity description.
        """
        from ouroboros.orchestrator.events import create_workflow_progress_event

        # Build AC list for TUI
        acceptance_criteria = []
        for i, ac_content in enumerate(seed.acceptance_criteria):
            status = ac_statuses.get(i, "pending")
            acceptance_criteria.append(
                {
                    "index": i,
                    "content": ac_content,
                    "status": status,
                    "elapsed": "",
                }
            )

        # Determine current AC index (first executing one, or None)
        current_ac_index = executing_indices[0] if executing_indices else None

        # Build activity detail
        if executing_indices:
            activity_detail = (
                f"Level {current_level}/{total_levels}: ACs {[i + 1 for i in executing_indices]}"
            )
        else:
            activity_detail = f"Level {current_level}/{total_levels}"

        event = create_workflow_progress_event(
            execution_id=execution_id,
            session_id=session_id,
            acceptance_criteria=acceptance_criteria,
            completed_count=completed_count,
            total_count=len(seed.acceptance_criteria),
            current_ac_index=current_ac_index,
            current_phase="Deliver",  # Parallel execution is in Deliver phase
            activity=activity,
            activity_detail=activity_detail,
        )
        await self._event_store.append(event)


__all__ = [
    "ACExecutionResult",
    "ParallelExecutionResult",
    "ParallelACExecutor",
]
