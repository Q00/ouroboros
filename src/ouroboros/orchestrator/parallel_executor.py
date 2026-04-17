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
        execution_plan=graph.to_execution_plan(),
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

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
import platform
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any

import anyio
from rich.console import Console

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
    runtime_handle_tool_catalog,
)
from ouroboros.orchestrator.capabilities import (
    build_capability_graph,
    serialize_capability_graph,
)
from ouroboros.orchestrator.control_plane import (
    build_control_plane_state,
    serialize_control_plane_state,
)
from ouroboros.orchestrator.coordinator import CoordinatorReview, LevelCoordinator
from ouroboros.orchestrator.events import (
    create_ac_stall_detected_event,
    create_heartbeat_event,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    build_ac_runtime_identity,
    build_ac_runtime_scope,
    build_level_coordinator_runtime_scope,
)
from ouroboros.orchestrator.level_context import (
    LevelContext,
    build_context_prompt,
    deserialize_level_contexts,
    extract_level_context,
    serialize_level_contexts,
)
from ouroboros.orchestrator.mcp_tools import serialize_tool_catalog
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
)
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
)
from ouroboros.orchestrator.runtime_message_projection import (
    project_runtime_message,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.mcp.types import MCPToolDefinition
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.orchestrator.dependency_analyzer import (
        DependencyGraph,
        StagedExecutionPlan,
    )
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)

# Decomposition constants
# Depth >= max_decomposition_depth forces atomic execution as a soft safety net.
DEFAULT_MAX_DECOMPOSITION_DEPTH = 2
MAX_DECOMPOSITION_DEPTH = DEFAULT_MAX_DECOMPOSITION_DEPTH
MIN_SUB_ACS = 2
MAX_SUB_ACS = 5
DECOMPOSITION_TIMEOUT_SECONDS = 60.0
_IMPLEMENTATION_SESSION_KIND = "implementation_session"
_REUSABLE_RUNTIME_EVENT_TYPES = frozenset(
    {
        "execution.session.recovered",
        "execution.session.started",
        "execution.session.resumed",
    }
)
_NON_REUSABLE_RUNTIME_EVENT_TYPES = frozenset(
    {
        "execution.session.completed",
        "execution.session.failed",
    }
)
_AC_RUNTIME_OWNERSHIP_METADATA_KEYS = frozenset(
    {
        "ac_id",
        "ac_index",
        "attempt_number",
        "parent_ac_index",
        "retry_attempt",
        "scope",
        "session_attempt_id",
        "session_role",
        "session_scope_id",
        "session_state_path",
        "sub_ac_index",
    }
)
_AC_RUNTIME_SCOPE_METADATA_KEYS = frozenset(
    {
        "ac_id",
        "ac_index",
        "parent_ac_index",
        "scope",
        "session_role",
        "session_scope_id",
        "session_state_path",
        "sub_ac_index",
    }
)
_AC_RUNTIME_RESUME_METADATA_KEYS = frozenset({"runtime_event_type", "server_session_id"})

# Stall detection constants
STALL_TIMEOUT_SECONDS: float = 300.0  # 5 minutes of silence → stall
HEARTBEAT_INTERVAL_SECONDS: float = 30.0  # Heartbeat emission interval
MAX_STALL_RETRIES: int = 2  # Max retries after stall (3 total attempts)
_STALL_SENTINEL = "__STALL_DETECTED__"  # Sentinel error for stall results

# Memory-pressure gate constants
_MIN_FREE_MEMORY_GB = 2.0
_MEMORY_CHECK_INTERVAL_SECONDS = 5.0
_MEMORY_WAIT_MAX_SECONDS = 120.0
_MAX_LEAF_RESULT_CHARS = 1200


def _get_available_memory_gb() -> float | None:
    """Get available memory in GB. Returns None if check fails."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            pages_free = 0
            pages_inactive = 0
            page_size = 4096  # macOS default
            for line in result.stdout.splitlines():
                if "page size of" in line:
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            page_size = int(part)
                elif line.startswith("Pages free:"):
                    pages_free = int(line.split(":")[1].strip().rstrip("."))
                elif line.startswith("Pages inactive:"):
                    pages_inactive = int(line.split(":")[1].strip().rstrip("."))
            return (pages_free + pages_inactive) * page_size / (1024**3)

        elif system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
            return None

        else:
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


# Data models re-exported from parallel_executor_models for backwards compat.
# (ACExecutionOutcome, ACExecutionResult, ParallelExecutionStageResult,
#  ParallelExecutionResult, StageExecutionOutcome are imported above.)


def _normalize_command(command: str) -> str:
    """Normalize Bash commands for stable audit output."""
    return " ".join(command.split())


def _truncate_text(text: str, limit: int = _MAX_LEAF_RESULT_CHARS) -> str:
    """Truncate long evidence blocks while preserving their beginning."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n[TRUNCATED]"


def _extract_leaf_evidence_lines(result: ACExecutionResult) -> list[str]:
    """Extract normalized command, file, and result evidence for a leaf AC."""
    lines: list[str] = []
    seen_commands: set[str] = set()
    seen_file_ops: set[tuple[str, str]] = set()

    for message in result.messages:
        if not message.tool_name:
            continue
        tool_input = message.data.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}

        if message.tool_name == "Bash":
            command = tool_input.get("command")
            if isinstance(command, str):
                normalized = _normalize_command(command)
                if normalized and normalized not in seen_commands:
                    if not lines:
                        lines.append("Commands Run:")
                    lines.append(f"- Bash: {normalized}")
                    seen_commands.add(normalized)
            continue

        if message.tool_name in ("Write", "Edit", "NotebookEdit"):
            path_key = "notebook_path" if message.tool_name == "NotebookEdit" else "file_path"
            file_path = tool_input.get(path_key)
            if isinstance(file_path, str) and file_path:
                file_op = (message.tool_name, file_path)
                if file_op not in seen_file_ops:
                    if "File Changes:" not in lines:
                        lines.append("File Changes:")
                    lines.append(f"- {message.tool_name}: {file_path}")
                    seen_file_ops.add(file_op)

    result_text = result.final_message or (f"Error: {result.error}" if result.error else "")
    if result_text:
        lines.append("Result:")
        lines.append(_truncate_text(result_text))
    return lines


def _render_ac_section(
    result: ACExecutionResult,
    *,
    index_path: tuple[int, ...],
    heading_level: int,
    include_header: bool = True,
) -> list[str]:
    """Render a single AC or Sub-AC section for verification/audit output."""
    lines: list[str] = []
    if include_header:
        status = "PASS" if result.success else "FAIL"
        label = "AC" if len(index_path) == 1 else "Sub-AC"
        lines.append(
            f"{'#' * heading_level} {label} {'.'.join(str(i) for i in index_path)}: "
            f"[{status}] {result.ac_content}"
        )

    if result.is_decomposed and result.sub_results:
        lines.append(f"Decomposed into {len(result.sub_results)} Sub-ACs")
        for idx, sub_result in enumerate(result.sub_results, start=1):
            if lines:
                lines.append("")
            lines.extend(
                _render_ac_section(
                    sub_result,
                    index_path=index_path + (idx,),
                    heading_level=min(heading_level + 1, 6),
                )
            )
        return lines

    evidence_lines = _extract_leaf_evidence_lines(result)
    if evidence_lines:
        lines.extend(evidence_lines)
    else:
        lines.append("Result:")
        lines.append("No final result message captured.")
    return lines


def _collect_decomposition_depth_warning_paths(
    result: ACExecutionResult,
    *,
    index_path: tuple[int, ...],
) -> list[str]:
    """Collect dotted AC paths that hit the soft decomposition depth safety net."""
    warning_paths: list[str] = []
    if result.decomposition_depth_warning:
        warning_paths.append(".".join(str(i) for i in index_path))

    for idx, sub_result in enumerate(result.sub_results, start=1):
        warning_paths.extend(
            _collect_decomposition_depth_warning_paths(
                sub_result,
                index_path=index_path + (idx,),
            )
        )
    return warning_paths


def render_parallel_verification_report(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
    *,
    max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
) -> str:
    """Build the canonical QA artifact for parallel execution results."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Verification Report",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.skipped_count > 0:
        lines.append(f"Skipped: {parallel_result.skipped_count}")

    warning_paths: list[str] = []
    for user_facing_idx, result in enumerate(parallel_result.results, start=1):
        warning_paths.extend(
            _collect_decomposition_depth_warning_paths(
                result,
                index_path=(user_facing_idx,),
            )
        )

    if warning_paths:
        feedback_metadata = {
            "feedback_metadata": [
                {
                    "code": "decomposition_depth_warning",
                    "severity": "warning",
                    "message": (
                        "Recursive decomposition reached the soft depth safety net; "
                        "affected leaves were forced to atomic execution."
                    ),
                    "source": "parallel_executor",
                    "details": {
                        "max_depth": max_decomposition_depth,
                        "affected_count": len(warning_paths),
                        "affected_ac_paths": warning_paths,
                    },
                }
            ]
        }
        lines.append("")
        lines.append("## Feedback Metadata")
        lines.append(f"Feedback Metadata JSON: {json.dumps(feedback_metadata, sort_keys=True)}")

    lines.append("")
    lines.append("## AC Results")
    for result in parallel_result.results:
        lines.append("")
        lines.extend(
            _render_ac_section(
                result,
                index_path=(result.ac_index + 1,),
                heading_level=3,
            )
        )
    return "\n".join(lines)


def render_parallel_completion_message(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
) -> str:
    """Build a concise operator-facing completion summary."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Complete",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.skipped_count > 0:
        lines.append(f"Skipped: {parallel_result.skipped_count}")

    lines.append("")
    lines.append("AC Status:")
    for result in parallel_result.results:
        if result.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY:
            status = "PASS"
            suffix = " (externally satisfied)"
        else:
            status = "PASS" if result.success else "FAIL"
            suffix = f" ({len(result.sub_results)} sub-ACs)" if result.is_decomposed else ""
        lines.append(f"- AC {result.ac_index + 1}: [{status}] {result.ac_content}{suffix}")
    return "\n".join(lines)


# =============================================================================
# Parallel Executor
# =============================================================================


class ParallelACExecutor:
    """Executes ACs in parallel based on dependency graph."""

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        console: Console | None = None,
        enable_decomposition: bool = True,
        max_concurrent: int = 3,
        max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
        checkpoint_store: Any | None = None,
        inherited_runtime_handle: RuntimeHandle | None = None,
        task_cwd: str | None = None,
    ):
        """Initialize executor.

        Args:
            adapter: Agent runtime for execution.
            event_store: Event store for progress tracking.
            console: Rich console for output.
            enable_decomposition: Enable Claude to decompose complex ACs.
            max_concurrent: Maximum number of concurrent AC executions.
            max_decomposition_depth: Maximum recursive decomposition depth.
            checkpoint_store: Optional CheckpointStore for state recovery (RC3).
            inherited_runtime_handle: Optional parent Claude runtime handle for
                        delegated child executions.
            task_cwd: Explicit working directory override for task execution metadata.
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        self._enable_decomposition = enable_decomposition
        self._max_decomposition_depth = max(0, max_decomposition_depth)
        self._inherited_runtime_handle = inherited_runtime_handle
        self._task_cwd = task_cwd
        self._coordinator = LevelCoordinator(
            adapter,
            inherited_runtime_handle=inherited_runtime_handle,
            task_cwd=task_cwd,
        )
        self._semaphore = anyio.Semaphore(max_concurrent)
        self._ac_runtime_handles: dict[str, RuntimeHandle] = {}
        self._checkpoint_store = checkpoint_store
        self._execution_counters_lock = asyncio.Lock()

    def _flush_console(self) -> None:
        """Flush console output to ensure progress is visible immediately."""
        if hasattr(self._console, "file") and hasattr(self._console.file, "flush"):
            try:
                self._console.file.flush()
            except (OSError, ValueError):
                pass

    async def _safe_emit_event(self, event: Any, max_retries: int = 3) -> bool:
        """Emit event with retry on failure (RC5).

        Retries with exponential backoff to handle transient DB lock errors.
        On permanent failure, logs error AND prints a console warning so the
        operator is aware of event persistence degradation.

        Args:
            event: BaseEvent to persist.
            max_retries: Maximum number of attempts.

        Returns:
            True if event was written, False if all retries failed.
        """
        for attempt in range(max_retries):
            try:
                await self._event_store.append(event)
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = min(1.0 * (2**attempt), 5.0)
                    log.warning(
                        "parallel_executor.event_write.retry",
                        event_type=event.type,
                        attempt=attempt + 1,
                        error=str(e),
                    )
                    await anyio.sleep(wait)
                else:
                    log.error(
                        "parallel_executor.event_write.failed",
                        event_type=event.type,
                        attempts=max_retries,
                        error=str(e),
                    )
                    self._console.print(
                        f"  [yellow]Event persistence degraded: "
                        f"{event.type} dropped after {max_retries} retries[/yellow]"
                    )
        return False

    @staticmethod
    def _build_expected_ac_runtime_metadata(
        runtime_scope: Any,
        *,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        retry_attempt: int,  # noqa: ARG004
    ) -> dict[str, Any]:
        """Build metadata that binds a runtime handle to a single AC execution scope."""
        return ACRuntimeIdentity(
            runtime_scope=runtime_scope,
            ac_index=None if is_sub_ac else ac_index,
            parent_ac_index=parent_ac_index if is_sub_ac else None,
            sub_ac_index=sub_ac_index if is_sub_ac else None,
        ).to_metadata()

    @staticmethod
    def _runtime_handle_claims_foreign_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        """Return True when the handle explicitly belongs to another AC scope."""
        if runtime_handle is None:
            return False

        metadata = runtime_handle.metadata
        for key in _AC_RUNTIME_SCOPE_METADATA_KEYS:
            expected_value = expected_metadata.get(key)
            if key in metadata and metadata.get(key) != expected_value:
                return True

        if is_sub_ac:
            return metadata.get("ac_index") is not None

        return (
            metadata.get("parent_ac_index") is not None or metadata.get("sub_ac_index") is not None
        )

    @classmethod
    def _runtime_handle_matches_ac_scope_for_resume(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        """Return True when a resumable handle is fully owned by the current AC scope."""
        if runtime_handle is None or cls._runtime_resume_session_id(runtime_handle) is None:
            return False

        metadata = runtime_handle.metadata
        matched_scope_key = False
        for key in _AC_RUNTIME_SCOPE_METADATA_KEYS:
            if key not in metadata:
                continue
            matched_scope_key = True
            if metadata.get(key) != expected_metadata.get(key):
                return False

        if not matched_scope_key:
            return False

        if is_sub_ac:
            return (
                metadata.get("parent_ac_index") == expected_metadata.get("parent_ac_index")
                and metadata.get("sub_ac_index") == expected_metadata.get("sub_ac_index")
                and metadata.get("ac_index") is None
            )

        return (
            metadata.get("ac_index") == expected_metadata.get("ac_index")
            and metadata.get("parent_ac_index") is None
            and metadata.get("sub_ac_index") is None
        )

    @staticmethod
    def _bind_runtime_handle_to_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        scrub_resume_state: bool = False,
    ) -> RuntimeHandle | None:
        """Overlay normalized AC ownership metadata onto a runtime handle."""
        if runtime_handle is None:
            return None

        metadata = dict(runtime_handle.metadata)
        for key in _AC_RUNTIME_OWNERSHIP_METADATA_KEYS:
            metadata.pop(key, None)
        if scrub_resume_state:
            for key in _AC_RUNTIME_RESUME_METADATA_KEYS:
                metadata.pop(key, None)
        metadata.update(expected_metadata)

        return replace(
            runtime_handle,
            native_session_id=None if scrub_resume_state else runtime_handle.native_session_id,
            conversation_id=None if scrub_resume_state else runtime_handle.conversation_id,
            previous_response_id=None
            if scrub_resume_state
            else runtime_handle.previous_response_id,
            transcript_path=None if scrub_resume_state else runtime_handle.transcript_path,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    def _normalize_ac_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope: Any,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        retry_attempt: int,
        source: str,
        require_resume_scope_match: bool,
    ) -> RuntimeHandle | None:
        """Bind a runtime handle to the active AC scope and reject foreign resumes."""
        if runtime_handle is None:
            return None

        expected_metadata = self._build_expected_ac_runtime_metadata(
            runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )

        if require_resume_scope_match and self._is_resumable_runtime_handle(runtime_handle):
            if not self._runtime_handle_matches_ac_scope_for_resume(
                runtime_handle,
                expected_metadata=expected_metadata,
                is_sub_ac=is_sub_ac,
            ):
                log.warning(
                    "parallel_executor.ac.runtime_handle_scope_rejected",
                    source=source,
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    retry_attempt=retry_attempt,
                    expected_session_scope_id=runtime_scope.aggregate_id,
                    observed_session_scope_id=runtime_handle.metadata.get("session_scope_id"),
                    observed_ac_index=runtime_handle.metadata.get("ac_index"),
                    observed_parent_ac_index=runtime_handle.metadata.get("parent_ac_index"),
                    observed_sub_ac_index=runtime_handle.metadata.get("sub_ac_index"),
                )
                return None

        scrub_resume_state = self._runtime_handle_claims_foreign_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            is_sub_ac=is_sub_ac,
        )
        if scrub_resume_state:
            log.warning(
                "parallel_executor.ac.runtime_handle_scope_scrubbed",
                source=source,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
                expected_session_scope_id=runtime_scope.aggregate_id,
                observed_session_scope_id=runtime_handle.metadata.get("session_scope_id"),
                observed_ac_index=runtime_handle.metadata.get("ac_index"),
                observed_parent_ac_index=runtime_handle.metadata.get("parent_ac_index"),
                observed_sub_ac_index=runtime_handle.metadata.get("sub_ac_index"),
            )

        return self._bind_runtime_handle_to_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            scrub_resume_state=scrub_resume_state,
        )

    def _build_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        retry_attempt: int = 0,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
    ) -> RuntimeHandle | None:
        """Build an AC-scoped runtime handle for implementation work.

        Handles are cached per AC scope so reconnect/resume stays inside the
        current AC retry/fix loop and never crosses into another AC execution.
        """
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        cached_seeded_handle = self._ac_runtime_handles.get(runtime_identity.cache_key)
        seeded_handle = self._normalize_ac_runtime_handle(
            cached_seeded_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=True,
        )
        if cached_seeded_handle is not None and seeded_handle is None:
            self._ac_runtime_handles.pop(runtime_identity.cache_key, None)
        backend = self._adapter.runtime_backend
        if not backend:
            return None

        cwd = self._task_cwd or self._adapter.working_directory
        approval_mode = self._adapter.permission_mode
        metadata: dict[str, Any] = dict(seeded_handle.metadata) if seeded_handle is not None else {}
        metadata.update(runtime_identity.to_metadata())
        metadata.setdefault("turn_number", 1)
        metadata.setdefault(
            "turn_id",
            self._default_turn_id(runtime_identity, int(metadata["turn_number"])),
        )
        if tool_catalog is not None:
            metadata["tool_catalog"] = serialize_tool_catalog(tool_catalog)
            capability_graph = build_capability_graph(tool_catalog)
            policy_context = PolicyContext(
                runtime_backend=backend,
                session_role=PolicySessionRole.IMPLEMENTATION,
                execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
            )
            metadata["capability_graph"] = serialize_capability_graph(capability_graph)
            metadata["control_plane"] = serialize_control_plane_state(
                build_control_plane_state(
                    capability_graph,
                    evaluate_capability_policy(capability_graph, policy_context),
                )
            )

        if seeded_handle is not None:
            return replace(
                seeded_handle,
                backend=backend,
                kind=seeded_handle.kind or _IMPLEMENTATION_SESSION_KIND,
                cwd=seeded_handle.cwd
                if seeded_handle.cwd
                else cwd
                if isinstance(cwd, str) and cwd
                else None,
                approval_mode=(
                    seeded_handle.approval_mode
                    if seeded_handle.approval_mode
                    else approval_mode
                    if isinstance(approval_mode, str) and approval_mode
                    else None
                ),
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        return RuntimeHandle(
            backend=backend,
            kind=_IMPLEMENTATION_SESSION_KIND,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            approval_mode=approval_mode
            if isinstance(approval_mode, str) and approval_mode
            else None,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    async def _load_persisted_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        retry_attempt: int = 0,
    ) -> RuntimeHandle | None:
        """Load the latest reusable AC-scoped runtime handle from execution events."""
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        cached_runtime_handle = self._ac_runtime_handles.get(runtime_identity.cache_key)
        cached_handle = self._normalize_ac_runtime_handle(
            cached_runtime_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=True,
        )
        if cached_runtime_handle is not None and cached_handle is None:
            self._ac_runtime_handles.pop(runtime_identity.cache_key, None)
        if cached_handle is not None:
            return cached_handle

        try:
            events = await self._event_store.replay(
                runtime_identity.runtime_scope.aggregate_type,
                runtime_identity.session_scope_id,
            )
        except Exception:
            log.exception(
                "parallel_executor.ac.runtime_handle_load_failed",
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
                session_scope_id=runtime_identity.session_scope_id,
            )
            return None

        for event in reversed(events):
            event_data = event.data if isinstance(event.data, dict) else {}
            if not self._event_matches_ac_runtime_identity(event_data, runtime_identity):
                continue

            if event.type in _NON_REUSABLE_RUNTIME_EVENT_TYPES:
                self._forget_ac_runtime_handle(
                    ac_index,
                    execution_context_id=execution_context_id,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    retry_attempt=retry_attempt,
                )
                return None
            if event.type not in _REUSABLE_RUNTIME_EVENT_TYPES:
                continue

            runtime_payload = event_data.get("runtime")
            try:
                runtime_handle = RuntimeHandle.from_dict(runtime_payload)
            except ValueError as exc:
                log.warning(
                    "parallel_executor.persisted_runtime_handle_invalid",
                    aggregate_id=event.aggregate_id,
                    event_type=event.type,
                    error=str(exc),
                    runtime_keys=sorted(runtime_payload)
                    if isinstance(runtime_payload, dict)
                    else None,
                )
                continue
            if runtime_handle is None:
                continue
            runtime_handle = self._normalize_ac_runtime_handle(
                runtime_handle,
                runtime_scope=runtime_identity.runtime_scope,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
                source="persisted_event",
                require_resume_scope_match=True,
            )
            if runtime_handle is None:
                continue

            self._ac_runtime_handles[runtime_identity.cache_key] = runtime_handle
            return runtime_handle

        return None

    def _remember_ac_runtime_handle(
        self,
        ac_index: int,
        runtime_handle: RuntimeHandle | None,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        retry_attempt: int = 0,
    ) -> RuntimeHandle | None:
        """Cache the latest reusable AC-scoped runtime handle."""
        if runtime_handle is None:
            return None

        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        normalized_handle = self._normalize_ac_runtime_handle(
            runtime_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
            source="runtime",
            require_resume_scope_match=False,
        )
        if normalized_handle is None:
            return None

        previous_handle = self._ac_runtime_handles.get(runtime_identity.cache_key)
        normalized_previous_handle = self._normalize_ac_runtime_handle(
            previous_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=False,
        )
        normalized_handle = self._augment_ac_runtime_handle(
            normalized_handle,
            runtime_identity=runtime_identity,
            previous_handle=normalized_previous_handle,
        )
        self._ac_runtime_handles[runtime_identity.cache_key] = normalized_handle
        return normalized_handle

    def _forget_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        retry_attempt: int = 0,
    ) -> None:
        """Drop live cached handle state once an AC scope is no longer resumable."""
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        self._ac_runtime_handles.pop(runtime_identity.cache_key, None)

    async def _terminate_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope_id: str,
    ) -> None:
        """Best-effort termination for live AC-scoped runtimes."""
        if runtime_handle is None or not runtime_handle.can_terminate:
            return

        try:
            terminated = await runtime_handle.terminate()
        except Exception as exc:
            log.warning(
                "parallel_executor.runtime_handle_terminate_failed",
                runtime_scope_id=runtime_scope_id,
                backend=runtime_handle.backend,
                error=str(exc),
            )
            return

        if terminated:
            log.info(
                "parallel_executor.runtime_handle_terminated",
                runtime_scope_id=runtime_scope_id,
                backend=runtime_handle.backend,
            )

    @staticmethod
    def _resolve_ac_runtime_identity(
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        retry_attempt: int = 0,
    ) -> ACRuntimeIdentity:
        """Return the normalized AC runtime identity for one implementation attempt."""
        return build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )

    @staticmethod
    def _event_matches_ac_runtime_identity(
        event_data: dict[str, Any],
        runtime_identity: ACRuntimeIdentity,
    ) -> bool:
        """Return True when an event belongs to the requested AC attempt."""
        runtime_payload = event_data.get("runtime")
        runtime_metadata: dict[str, Any] = {}
        if isinstance(runtime_payload, dict):
            raw_metadata = runtime_payload.get("metadata")
            if isinstance(raw_metadata, dict):
                runtime_metadata = raw_metadata

        expected_metadata = runtime_identity.to_metadata()
        matched_identity_key = False
        for key in _AC_RUNTIME_OWNERSHIP_METADATA_KEYS:
            if key in event_data:
                observed_value = event_data.get(key)
            elif key in runtime_metadata:
                observed_value = runtime_metadata.get(key)
            else:
                continue

            matched_identity_key = True
            if observed_value != expected_metadata.get(key):
                return False

        return matched_identity_key

    @staticmethod
    def _default_turn_id(
        runtime_identity: ACRuntimeIdentity,
        turn_number: int,
    ) -> str:
        """Build a stable logical turn identifier within one AC session attempt."""
        return f"{runtime_identity.session_attempt_id}:turn_{turn_number}"

    @staticmethod
    def _runtime_turn_number(runtime_handle: RuntimeHandle | None) -> int:
        """Return the 1-based logical turn number carried by a runtime handle."""
        if runtime_handle is None:
            return 1

        value = runtime_handle.metadata.get("turn_number")
        if isinstance(value, int) and value > 0:
            return value
        return 1

    @classmethod
    def _runtime_turn_id(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_identity: ACRuntimeIdentity,
    ) -> str:
        """Return the stable logical turn identifier for a runtime handle."""
        if runtime_handle is not None:
            value = runtime_handle.metadata.get("turn_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return cls._default_turn_id(
            runtime_identity,
            cls._runtime_turn_number(runtime_handle),
        )

    @staticmethod
    def _runtime_recovery_discontinuity(
        runtime_handle: RuntimeHandle | None,
    ) -> dict[str, Any] | None:
        """Return persisted recovery discontinuity metadata when present."""
        if runtime_handle is None:
            return None

        value = runtime_handle.metadata.get("recovery_discontinuity")
        return dict(value) if isinstance(value, dict) else None

    @classmethod
    def _runtime_handle_same_session(
        cls,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle | None,
    ) -> bool:
        """Return True when two runtime handles identify the same backend session."""
        if previous_handle is None or current_handle is None:
            return False

        previous_native = previous_handle.native_session_id
        current_native = current_handle.native_session_id
        if previous_native and current_native:
            return previous_native == current_native

        previous_server = previous_handle.server_session_id
        current_server = current_handle.server_session_id
        if previous_server and current_server:
            return previous_server == current_server

        previous_resume = previous_handle.resume_session_id
        current_resume = current_handle.resume_session_id
        if previous_resume and current_resume:
            return previous_resume == current_resume

        return False

    @classmethod
    def _build_recovery_discontinuity(
        cls,
        *,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle,
        runtime_identity: ACRuntimeIdentity,
    ) -> dict[str, Any] | None:
        """Build failed-to-replacement session/turn linkage for soft recovery."""
        if previous_handle is None or previous_handle.resume_session_id is None:
            return None
        if cls._runtime_handle_same_session(previous_handle, current_handle):
            return None

        current_event_type = current_handle.metadata.get("runtime_event_type")
        replacement_event = isinstance(
            current_event_type, str
        ) and current_event_type.strip().lower() in {"session.started", "thread.started"}
        previous_native = previous_handle.native_session_id
        current_native = current_handle.native_session_id
        previous_server = previous_handle.server_session_id
        current_server = current_handle.server_session_id
        native_changed = bool(
            previous_native and current_native and previous_native != current_native
        )
        server_changed = bool(
            previous_server and current_server and previous_server != current_server
        )
        if not replacement_event and not native_changed and not server_changed:
            return None

        failed_turn_number = cls._runtime_turn_number(previous_handle)
        replacement_turn_number = max(
            cls._runtime_turn_number(current_handle),
            failed_turn_number + 1,
        )

        return {
            "reason": "replacement_session",
            "failed": {
                "session_id": previous_native,
                "server_session_id": previous_server,
                "resume_session_id": previous_handle.resume_session_id,
                "turn_id": cls._runtime_turn_id(
                    previous_handle,
                    runtime_identity=runtime_identity,
                ),
                "turn_number": failed_turn_number,
            },
            "replacement": {
                "session_id": current_native,
                "server_session_id": current_server,
                "resume_session_id": current_handle.resume_session_id,
                "turn_id": cls._default_turn_id(runtime_identity, replacement_turn_number),
                "turn_number": replacement_turn_number,
            },
        }

    @classmethod
    def _augment_ac_runtime_handle(
        cls,
        runtime_handle: RuntimeHandle,
        *,
        runtime_identity: ACRuntimeIdentity,
        previous_handle: RuntimeHandle | None,
    ) -> RuntimeHandle:
        """Carry forward logical turn state and record same-attempt recovery linkage."""
        metadata = dict(runtime_handle.metadata)
        metadata.setdefault("turn_number", cls._runtime_turn_number(runtime_handle))
        metadata.setdefault(
            "turn_id",
            cls._runtime_turn_id(runtime_handle, runtime_identity=runtime_identity),
        )

        if previous_handle is not None and cls._runtime_handle_same_session(
            previous_handle,
            runtime_handle,
        ):
            previous_turn_number = cls._runtime_turn_number(previous_handle)
            if previous_turn_number > cls._runtime_turn_number(runtime_handle):
                metadata["turn_number"] = previous_turn_number
                metadata["turn_id"] = cls._runtime_turn_id(
                    previous_handle,
                    runtime_identity=runtime_identity,
                )

            previous_recovery_discontinuity = cls._runtime_recovery_discontinuity(previous_handle)
            if previous_recovery_discontinuity is not None:
                metadata.setdefault(
                    "recovery_discontinuity",
                    previous_recovery_discontinuity,
                )

        recovery_discontinuity = cls._build_recovery_discontinuity(
            previous_handle=previous_handle,
            current_handle=runtime_handle,
            runtime_identity=runtime_identity,
        )
        if recovery_discontinuity is not None:
            replacement = recovery_discontinuity["replacement"]
            metadata["turn_number"] = replacement["turn_number"]
            metadata["turn_id"] = replacement["turn_id"]
            metadata["recovery_discontinuity"] = recovery_discontinuity

        if metadata == runtime_handle.metadata:
            return runtime_handle

        return replace(
            runtime_handle,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    @staticmethod
    def _with_native_session_id(
        runtime_handle: RuntimeHandle | None,
        native_session_id: str | None,
    ) -> RuntimeHandle | None:
        """Attach a discovered native session id to an existing runtime handle."""
        if runtime_handle is None or not native_session_id:
            return runtime_handle
        if runtime_handle.native_session_id == native_session_id:
            return runtime_handle

        return replace(
            runtime_handle,
            native_session_id=native_session_id,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=dict(runtime_handle.metadata),
        )

    @staticmethod
    def _is_resumable_runtime_handle(runtime_handle: RuntimeHandle | None) -> bool:
        """Return True when the handle can reconnect to an existing backend session."""
        return ParallelACExecutor._runtime_resume_session_id(runtime_handle) is not None

    @staticmethod
    def _runtime_resume_session_id(runtime_handle: RuntimeHandle | None) -> str | None:
        """Return the minimal persisted session identifier used for reconnect/resume."""
        if runtime_handle is None:
            return None
        return runtime_handle.resume_session_id

    async def _emit_ac_runtime_event(
        self,
        *,
        event_type: str,
        runtime_identity: ACRuntimeIdentity,
        ac_content: str,
        runtime_handle: RuntimeHandle | None,
        session_id: str | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Persist AC-scoped runtime lifecycle events using normalized metadata."""
        from ouroboros.events.base import BaseEvent

        effective_session_id = session_id or self._runtime_resume_session_id(runtime_handle)
        server_session_id = runtime_handle.server_session_id if runtime_handle is not None else None

        event = BaseEvent(
            type=event_type,
            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
            aggregate_id=runtime_identity.session_scope_id,
            data={
                "ac_id": runtime_identity.ac_id,
                "acceptance_criterion": ac_content,
                "scope": runtime_identity.scope,
                "session_role": runtime_identity.session_role,
                "retry_attempt": runtime_identity.retry_attempt,
                "attempt_number": runtime_identity.attempt_number,
                "session_scope_id": runtime_identity.session_scope_id,
                "session_attempt_id": runtime_identity.session_attempt_id,
                "session_state_path": runtime_identity.session_state_path,
                "runtime_backend": (runtime_handle.backend if runtime_handle is not None else None),
                "runtime": (
                    runtime_handle.to_persisted_dict() if runtime_handle is not None else None
                ),
                "session_id": effective_session_id,
                "server_session_id": server_session_id,
                "success": success,
                "result_summary": result_summary,
                "error": error,
            },
        )
        if runtime_handle is not None:
            turn_id = runtime_handle.metadata.get("turn_id")
            if isinstance(turn_id, str) and turn_id.strip():
                event.data["turn_id"] = turn_id.strip()

            turn_number = runtime_handle.metadata.get("turn_number")
            if isinstance(turn_number, int) and turn_number > 0:
                event.data["turn_number"] = turn_number

            recovery_discontinuity = self._runtime_recovery_discontinuity(runtime_handle)
            if recovery_discontinuity is not None:
                event.data["recovery_discontinuity"] = recovery_discontinuity
        tool_catalog = runtime_handle_tool_catalog(runtime_handle)
        if tool_catalog is not None:
            event.data["tool_catalog"] = tool_catalog
        await self._event_store.append(event)

    @staticmethod
    def _coerce_ac_indices(raw_indices: Any) -> tuple[int, ...]:
        """Normalize a stage or batch AC index payload into an ordered tuple."""
        if raw_indices is None:
            return ()
        if isinstance(raw_indices, int):
            return (raw_indices,)

        indices: list[int] = []
        for candidate in raw_indices:
            if isinstance(candidate, int):
                indices.append(candidate)
        return tuple(indices)

    def _get_stage_batches(self, stage: Any) -> tuple[tuple[int, ...], ...]:
        """Return normalized batch AC groupings for a stage."""
        raw_batches = getattr(stage, "batches", None)
        if raw_batches:
            batches = tuple(
                batch_indices
                for batch_indices in (
                    self._coerce_ac_indices(getattr(batch, "ac_indices", batch))
                    for batch in raw_batches
                )
                if batch_indices
            )
            if batches:
                return batches

        ac_indices = self._coerce_ac_indices(getattr(stage, "ac_indices", ()))
        return (ac_indices,) if ac_indices else ()

    def _get_stage_ac_indices(self, stage: Any) -> tuple[int, ...]:
        """Return the ordered AC indices covered by a stage."""
        ac_indices = self._coerce_ac_indices(getattr(stage, "ac_indices", ()))
        if ac_indices:
            return ac_indices

        ordered_indices: list[int] = []
        seen_indices: set[int] = set()
        for batch in self._get_stage_batches(stage):
            for ac_index in batch:
                if ac_index in seen_indices:
                    continue
                seen_indices.add(ac_index)
                ordered_indices.append(ac_index)
        return tuple(ordered_indices)

    async def _execute_ac_batch(
        self,
        *,
        seed: Seed,
        batch_indices: list[int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        ac_retry_attempts: dict[int, int],
        execution_counters: dict[str, int] | None = None,
    ) -> list[ACExecutionResult | BaseException]:
        """Execute one batch of stage-ready ACs using the shared worker pool."""
        batch_results: list[ACExecutionResult | BaseException] = [None] * len(batch_indices)
        sibling_acs = (
            [seed.acceptance_criteria[i] for i in batch_indices] if len(batch_indices) > 1 else []
        )

        async def _run_ac(idx: int, ac_idx: int) -> None:
            async with self._semaphore:
                try:
                    batch_results[idx] = await self._execute_single_ac(
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        session_id=session_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        seed_goal=seed.goal,
                        depth=0,
                        execution_id=execution_id,
                        level_contexts=level_contexts,
                        sibling_acs=sibling_acs,
                        retry_attempt=ac_retry_attempts[ac_idx],
                        execution_counters=execution_counters,
                    )
                except BaseException as e:
                    # Never suppress anyio Cancelled — doing so breaks
                    # the task group's cancel-scope propagation and can
                    # cause the entire group to hang indefinitely.
                    if isinstance(e, anyio.get_cancelled_exc_class()):
                        raise
                    batch_results[idx] = e

        async with anyio.create_task_group() as tg:
            for idx, ac_idx in enumerate(batch_indices):
                tg.start_soon(_run_ac, idx, ac_idx)

        return batch_results

    async def execute_parallel(
        self,
        seed: Seed,
        *,
        session_id: str,
        execution_id: str,
        tools: list[str],
        system_prompt: str,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
        dependency_graph: DependencyGraph | None = None,
        execution_plan: StagedExecutionPlan | None = None,
        reconciled_level_contexts: list[LevelContext] | None = None,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
    ) -> ParallelExecutionResult:
        """Execute ACs according to a staged execution plan.

        Args:
            seed: Seed specification.
            execution_plan: Staged execution plan defining serial stages.
            session_id: Parent session ID for tracking.
            execution_id: Execution ID for event tracking.
            tools: Tools available to agents.
            system_prompt: System prompt for agents.
            dependency_graph: Legacy fallback used to derive ``execution_plan``.
            reconciled_level_contexts: Existing post-reconcile stage contexts
                from a previous execution attempt. Reopened ACs receive these
                as prompt context so they continue from the current shared
                workspace state instead of the original failed-attempt state.
            externally_satisfied_acs: Top-level ACs already satisfied by the
                current working tree and therefore skipped for re-execution.

        Returns:
            ParallelExecutionResult with outcomes for all ACs.
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        start_time = datetime.now(UTC)
        all_results: list[ACExecutionResult] = []
        failed_indices: set[int] = set()
        blocked_indices: set[int] = set()
        stage_results: list[ParallelExecutionStageResult] = []
        level_contexts = list(reconciled_level_contexts or [])

        total_levels = execution_plan.total_stages
        total_acs = len(seed.acceptance_criteria)
        external_completed = externally_satisfied_acs or {}
        execution_counters = {
            "messages_count": 0,
            "tool_calls_count": 0,
        }

        # Track AC statuses for TUI updates
        ac_statuses: dict[int, str] = dict.fromkeys(range(total_acs), "pending")
        ac_retry_attempts: dict[int, int] = dict.fromkeys(range(total_acs), 0)
        completed_count = 0
        resume_from_level = 0

        # RC3: Attempt to recover from checkpoint
        if self._checkpoint_store:
            try:
                seed_id = getattr(seed, "id", session_id)
                load_result = self._checkpoint_store.load(seed_id)
                if hasattr(load_result, "is_ok") and load_result.is_ok and load_result.value:
                    cp = load_result.value
                    if cp.phase == "parallel_execution":
                        resume_from_level = cp.state.get("completed_levels", 0)
                        for idx, status in cp.state.get("ac_statuses", {}).items():
                            ac_statuses[int(idx)] = status
                        for idx in cp.state.get("failed_indices", []):
                            failed_indices.add(int(idx))
                        completed_count = cp.state.get("completed_count", 0)
                        # Restore level contexts so subsequent levels
                        # have access to completed levels' output
                        saved_contexts = cp.state.get("level_contexts", [])
                        if saved_contexts:
                            level_contexts = deserialize_level_contexts(saved_contexts)
                        log.info(
                            "parallel_executor.recovery.resuming",
                            from_level=resume_from_level,
                            seed_id=seed_id,
                            restored_contexts=len(level_contexts),
                        )
                        # Reconstruct all_results for completed/failed/skipped ACs.
                        for prev_stage in execution_plan.stages[:resume_from_level]:
                            for ac_idx in self._get_stage_ac_indices(prev_stage):
                                if ac_idx >= total_acs:
                                    continue
                                status = ac_statuses.get(ac_idx, "pending")
                                is_completed = status == "completed"
                                is_skipped = status == "skipped"
                                all_results.append(
                                    ACExecutionResult(
                                        ac_index=ac_idx,
                                        ac_content=seed.acceptance_criteria[ac_idx],
                                        success=is_completed,
                                        final_message=(
                                            "[Restored from checkpoint]" if is_completed else ""
                                        ),
                                        error=(
                                            "Skipped: dependency failed"
                                            if is_skipped
                                            else None
                                            if is_completed
                                            else "Failed (restored from checkpoint)"
                                        ),
                                        retry_attempt=ac_retry_attempts.get(ac_idx, 0),
                                    )
                                )
                        self._console.print(
                            f"[cyan]Resuming from level {resume_from_level + 1} "
                            f"(checkpoint recovered, "
                            f"{len(level_contexts)} level context(s) restored)[/cyan]"
                        )
            except Exception as e:
                log.warning(
                    "parallel_executor.recovery.failed",
                    error=str(e),
                )

        # Validation: check all AC indices are present in dependency graph
        expected_indices = set(range(total_acs))
        actual_indices = {
            idx for stage in execution_plan.stages for idx in self._get_stage_ac_indices(stage)
        }
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
                        retry_attempt=ac_retry_attempts[idx],
                        outcome=ACExecutionOutcome.INVALID,
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
            levels=execution_plan.execution_levels,
        )

        # Emit initial progress for TUI
        await self._emit_workflow_progress(
            session_id=session_id,
            execution_id=execution_id,
            seed=seed,
            ac_statuses=ac_statuses,
            ac_retry_attempts=ac_retry_attempts,
            executing_indices=[],
            completed_count=completed_count,
            current_level=resume_from_level + 1,
            total_levels=total_levels,
            activity="Starting parallel execution",
            messages_count=execution_counters["messages_count"],
            tool_calls_count=execution_counters["tool_calls_count"],
        )

        # RC2+RC4: Shared state for resilient progress emitter
        progress_state: dict[str, int] = {
            "current_level": resume_from_level + 1,
            "total_levels": total_levels,
        }

        # Execute groups sequentially, but ACs within each group in parallel.
        # The resilient progress emitter runs as a sibling background task
        # and is automatically cancelled when the execution loop finishes.
        async with anyio.create_task_group() as outer_tg:
            outer_tg.start_soon(
                self._resilient_progress_emitter,
                session_id,
                execution_id,
                seed,
                ac_statuses,
                progress_state,
            )

            for stage in execution_plan.stages:
                level_idx = stage.index
                level = self._get_stage_ac_indices(stage)
                stage_batches = self._get_stage_batches(stage)
                level_num = level_idx + 1

                # RC3: Skip already-completed levels on recovery
                if level_idx < resume_from_level:
                    log.info(
                        "parallel_executor.recovery.skipping_level",
                        level=level_num,
                    )
                    continue

                # Update shared progress state for background emitter
                progress_state["current_level"] = level_num

                # Check for blocked ACs (dependencies failed or were blocked upstream)
                executable: list[int] = []
                blocked: list[int] = []
                externally_satisfied: list[int] = []
                stage_ac_results: list[ACExecutionResult] = []

                for ac_idx in level:
                    # Skip invalid indices
                    if ac_idx < 0 or ac_idx >= total_acs:
                        continue

                    # Always validate dependencies first — even externally
                    # satisfied ACs must be blocked if their upstream
                    # dependencies failed, because the "satisfied" state may
                    # be stale relative to the current execution.
                    deps = execution_plan.get_dependencies(ac_idx)
                    if any(dep in failed_indices or dep in blocked_indices for dep in deps):
                        blocked.append(ac_idx)
                    elif ac_idx in external_completed:
                        externally_satisfied.append(ac_idx)
                    else:
                        executable.append(ac_idx)

                level_success = 0
                level_failed = 0

                for ac_idx in externally_satisfied:
                    metadata = external_completed.get(ac_idx, {})
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
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        success=True,
                        final_message="\n".join(notes),
                        retry_attempt=ac_retry_attempts[ac_idx],
                        outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                    )
                    all_results.append(satisfied_result)
                    stage_ac_results.append(satisfied_result)
                    ac_statuses[ac_idx] = "completed"
                    completed_count += 1
                    level_success += 1
                    log.info(
                        "parallel_executor.ac.satisfied_externally",
                        session_id=session_id,
                        ac_index=ac_idx,
                        reason=reason,
                        commit=commit,
                    )

                # Add blocked results
                for ac_idx in blocked:
                    blocked_result = ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=seed.acceptance_criteria[ac_idx],
                        success=False,
                        error="Skipped: dependency failed",
                        retry_attempt=ac_retry_attempts[ac_idx],
                        outcome=ACExecutionOutcome.BLOCKED,
                    )
                    all_results.append(blocked_result)
                    stage_ac_results.append(blocked_result)
                    blocked_indices.add(ac_idx)
                    ac_statuses[ac_idx] = "skipped"
                    log.info(
                        "parallel_executor.ac.skipped",
                        session_id=session_id,
                        ac_index=ac_idx,
                        reason="dependency_failed",
                    )

                if not executable:
                    stage_started = bool(externally_satisfied)
                    stage_result = ParallelExecutionStageResult(
                        stage_index=level_idx,
                        ac_indices=tuple(level),
                        results=tuple(sorted(stage_ac_results, key=lambda result: result.ac_index)),
                        started=stage_started,
                    )
                    stage_results.append(stage_result)
                    await self._emit_level_completed(
                        session_id=session_id,
                        level=level_num,
                        success_count=stage_result.success_count,
                        failure_count=stage_result.failure_count,
                        blocked_count=stage_result.blocked_count,
                        started=stage_started,
                        outcome=stage_result.outcome.value,
                    )
                    continue

                # Mark ACs as executing
                for ac_idx in executable:
                    ac_statuses[ac_idx] = "executing"

                self._console.print(
                    f"\n[cyan]Level {level_num}/{total_levels}: "
                    f"Executing ACs {[idx + 1 for idx in executable]} in parallel[/cyan]"
                )
                self._flush_console()

                # Emit level started event
                await self._emit_level_started(
                    session_id=session_id,
                    level=level_num,
                    ac_indices=executable,
                    total_levels=total_levels,
                )

                # Capture current contexts for this level's closure
                current_contexts = list(level_contexts)

                for batch_index, batch in enumerate(stage_batches, start=1):
                    batch_executable = [ac_idx for ac_idx in batch if ac_idx in executable]
                    if not batch_executable:
                        continue

                    for ac_idx in batch_executable:
                        ac_statuses[ac_idx] = "executing"

                    if len(stage_batches) > 1:
                        self._console.print(
                            f"  [cyan]Batch {batch_index}/{len(stage_batches)}: "
                            f"ACs {[idx + 1 for idx in batch_executable]}[/cyan]"
                        )
                        self._flush_console()

                    await self._emit_workflow_progress(
                        session_id=session_id,
                        execution_id=execution_id,
                        seed=seed,
                        ac_statuses=ac_statuses,
                        ac_retry_attempts=ac_retry_attempts,
                        executing_indices=batch_executable,
                        completed_count=completed_count,
                        current_level=level_num,
                        total_levels=total_levels,
                        activity="Executing",
                        messages_count=execution_counters["messages_count"],
                        tool_calls_count=execution_counters["tool_calls_count"],
                    )

                    batch_results = await self._execute_ac_batch(
                        seed=seed,
                        batch_indices=batch_executable,
                        session_id=session_id,
                        execution_id=execution_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        level_contexts=current_contexts,
                        ac_retry_attempts=ac_retry_attempts,
                        execution_counters=execution_counters,
                    )

                    for ac_idx, result in zip(batch_executable, batch_results, strict=False):
                        if isinstance(result, BaseException):
                            # Exception during execution
                            error_msg = str(result)
                            ac_result = ACExecutionResult(
                                ac_index=ac_idx,
                                ac_content=seed.acceptance_criteria[ac_idx],
                                success=False,
                                error=error_msg,
                                retry_attempt=ac_retry_attempts[ac_idx],
                                outcome=ACExecutionOutcome.FAILED,
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
                        elif (
                            isinstance(result, ACExecutionResult)
                            and result.error == _STALL_SENTINEL
                        ):
                            # Stalled AC — treat as permanent failure at batch level
                            ac_id = f"ac_{ac_idx}"
                            await self._safe_emit_event(
                                create_ac_stall_detected_event(
                                    session_id=session_id,
                                    ac_index=ac_idx,
                                    ac_id=ac_id,
                                    silent_seconds=STALL_TIMEOUT_SECONDS,
                                    attempt=1,
                                    max_attempts=1,
                                    action="abandon",
                                )
                            )
                            ac_result = ACExecutionResult(
                                ac_index=ac_idx,
                                ac_content=seed.acceptance_criteria[ac_idx],
                                success=False,
                                error=(f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)"),
                                retry_attempt=ac_retry_attempts[ac_idx],
                                outcome=ACExecutionOutcome.FAILED,
                            )
                            failed_indices.add(ac_idx)
                            level_failed += 1
                            ac_statuses[ac_idx] = "failed"
                            log.error(
                                "parallel_executor.ac.stall_abandoned",
                                session_id=session_id,
                                ac_index=ac_idx,
                            )
                        else:
                            ac_result = result
                            if ac_result.success:
                                level_success += 1
                                ac_statuses[ac_idx] = "completed"
                                completed_count += 1
                            elif ac_result.is_blocked:
                                blocked_indices.add(ac_idx)
                                ac_statuses[ac_idx] = "skipped"
                            else:
                                failed_indices.add(ac_idx)
                                level_failed += 1
                                ac_statuses[ac_idx] = "failed"

                        all_results.append(ac_result)
                        stage_ac_results.append(ac_result)

                stage_result = ParallelExecutionStageResult(
                    stage_index=level_idx,
                    ac_indices=tuple(level),
                    results=tuple(sorted(stage_ac_results, key=lambda result: result.ac_index)),
                    started=True,
                )

                # Emit level completed event
                await self._emit_level_completed(
                    session_id=session_id,
                    level=level_num,
                    success_count=level_success,
                    failure_count=level_failed,
                    blocked_count=stage_result.blocked_count,
                    started=True,
                    outcome=stage_result.outcome.value,
                )

                # Emit progress after level completes
                await self._emit_workflow_progress(
                    session_id=session_id,
                    execution_id=execution_id,
                    seed=seed,
                    ac_statuses=ac_statuses,
                    ac_retry_attempts=ac_retry_attempts,
                    executing_indices=[],
                    completed_count=completed_count,
                    current_level=level_num,
                    total_levels=total_levels,
                    activity=f"Level {level_num} complete",
                    messages_count=execution_counters["messages_count"],
                    tool_calls_count=execution_counters["tool_calls_count"],
                )

                self._console.print(
                    f"[green]Level {level_num} complete: "
                    f"{level_success} succeeded, {level_failed} failed[/green]"
                )
                self._flush_console()

                # Extract context from this level for next level's ACs
                if executable and level_success > 0:
                    level_ac_data = [
                        (r.ac_index, r.ac_content, r.success, r.messages, r.final_message)
                        for r in stage_ac_results
                        if r.ac_index in executable
                    ]
                    # workspace_root is required: fall back through
                    # adapter working directory, then process cwd. Never None.
                    workspace_root = (
                        self._task_cwd or self._adapter.working_directory or os.getcwd()
                    )
                    level_ctx = extract_level_context(
                        level_ac_data,
                        level_num,
                        workspace_root=workspace_root,
                    )

                    # Coordinator: detect and resolve file conflicts (Approach A)
                    level_ac_results = [r for r in stage_ac_results if r.ac_index in executable]
                    conflicts = self._coordinator.detect_file_conflicts(level_ac_results)

                    if conflicts:
                        self._console.print(
                            f"  [yellow]Coordinator: {len(conflicts)} file conflict(s) detected, "
                            f"starting review...[/yellow]"
                        )
                        await self._emit_coordinator_started(
                            execution_id=execution_id,
                            session_id=session_id,
                            level=level_num,
                            conflicts=conflicts,
                        )
                        review = await self._coordinator.run_review(
                            execution_id=execution_id,
                            conflicts=conflicts,
                            level_context=level_ctx,
                            level_number=level_num,
                        )
                        await self._emit_coordinator_runtime_events(
                            execution_id=execution_id,
                            session_id=session_id,
                            review=review,
                        )
                        await self._emit_coordinator_completed(
                            execution_id=execution_id,
                            session_id=session_id,
                            review=review,
                        )
                        # Attach review to the level context
                        level_ctx = LevelContext(
                            level_number=level_ctx.level_number,
                            completed_acs=level_ctx.completed_acs,
                            coordinator_review=review,
                        )
                        stage_result = replace(stage_result, coordinator_review=review)
                        self._console.print(
                            f"  [green]Coordinator review complete: "
                            f"{len(review.fixes_applied)} fix(es), "
                            f"{len(review.warnings_for_next_level)} warning(s)[/green]"
                        )

                    level_contexts.append(level_ctx)
                stage_results.append(stage_result)

                # RC3: Save checkpoint after each level completion
                if self._checkpoint_store:
                    try:
                        from ouroboros.persistence.checkpoint import CheckpointData

                        seed_id = getattr(seed, "id", session_id)
                        checkpoint = CheckpointData.create(
                            seed_id=seed_id,
                            phase="parallel_execution",
                            state={
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "completed_levels": level_idx + 1,
                                "ac_statuses": {str(k): v for k, v in ac_statuses.items()},
                                "failed_indices": sorted(failed_indices),
                                "completed_count": completed_count,
                                "level_contexts": serialize_level_contexts(level_contexts),
                            },
                        )
                        save_result = self._checkpoint_store.save(checkpoint)
                        if hasattr(save_result, "is_ok") and save_result.is_ok:
                            log.info(
                                "parallel_executor.checkpoint.saved",
                                level=level_num,
                                seed_id=seed_id,
                            )
                        else:
                            err_msg = (
                                str(save_result.error)
                                if hasattr(save_result, "error")
                                else "unknown error"
                            )
                            log.warning(
                                "parallel_executor.checkpoint.save_failed",
                                level=level_num,
                                seed_id=seed_id,
                                error=err_msg,
                            )
                            self._console.print(
                                f"  [yellow]Checkpoint save failed for level "
                                f"{level_num}: {err_msg}[/yellow]"
                            )
                    except Exception as e:
                        log.warning(
                            "parallel_executor.checkpoint.save_failed",
                            level=level_num,
                            error=str(e),
                        )

            # All levels done — cancel the background progress emitter
            outer_tg.cancel_scope.cancel()

        # Aggregate results - sort by AC index for consistent ordering
        sorted_results = sorted(all_results, key=lambda r: r.ac_index)
        total_duration = (datetime.now(UTC) - start_time).total_seconds()
        success_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.SUCCEEDED)
        externally_satisfied_count = sum(
            1 for r in sorted_results if r.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        )
        failure_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.FAILED)
        blocked_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.BLOCKED)
        invalid_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.INVALID)
        skipped_count = blocked_count + invalid_count
        total_messages = execution_counters["messages_count"]

        log.info(
            "parallel_executor.execution.completed",
            session_id=session_id,
            success_count=success_count,
            externally_satisfied_count=externally_satisfied_count,
            failure_count=failure_count,
            blocked_count=blocked_count,
            invalid_count=invalid_count,
            skipped_count=skipped_count,
            total_messages=total_messages,
            duration_seconds=total_duration,
        )

        return ParallelExecutionResult(
            results=tuple(sorted_results),
            success_count=success_count,
            failure_count=failure_count,
            externally_satisfied_count=externally_satisfied_count,
            skipped_count=skipped_count,
            blocked_count=blocked_count,
            invalid_count=invalid_count,
            stages=tuple(stage_results),
            reconciled_level_contexts=tuple(level_contexts),
            total_messages=total_messages,
            total_duration_seconds=total_duration,
        )

    async def _execute_single_ac(
        self,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        seed_goal: str,
        depth: int = 0,
        execution_id: str = "",
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[str] | None = None,
        retry_attempt: int = 0,
        execution_counters: dict[str, int] | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
    ) -> ACExecutionResult:
        """Execute a single AC via the sole recursive AC execution entry point.

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
        if self._enable_decomposition and depth < self._max_decomposition_depth:
            self._console.print(f"  [dim]AC {ac_index + 1}: Analyzing complexity...[/dim]")
            self._flush_console()
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
                self._flush_console()

                # Emit decomposition event for TUI
                for i, sub_ac in enumerate(sub_acs):
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_desc=sub_ac[:50],
                        status="pending",
                    )

                # Execute Sub-ACs sequentially (memory optimization) while
                # re-entering this same method for both composite and atomic children.
                self._console.print(
                    f"    [green]Starting {len(sub_acs)} Sub-ACs sequentially...[/green]"
                )

                sub_results: list[ACExecutionResult | BaseException] = [None] * len(sub_acs)
                sub_depth = depth + 1

                for idx, sub_ac in enumerate(sub_acs):
                    try:
                        # Only depth-0 → depth-1 transitions flag children as sub-ACs
                        # with parent_ac_index/sub_ac_index. Deeper descendants
                        # (depth>=2) retain their own ac_index and are treated as
                        # independent nodes for runtime identity tracking; downstream
                        # tooling that needs full ancestry should traverse via
                        # decomposition events rather than parent_ac_index metadata.
                        child_is_sub_ac = depth == 0 and not is_sub_ac
                        await self._emit_subtask_event(
                            execution_id=execution_id,
                            ac_index=ac_index,
                            sub_task_index=idx + 1,
                            sub_task_desc=sub_ac[:50],
                            status="executing",
                        )

                        sub_results[idx] = await self._execute_single_ac(
                            ac_index=ac_index * 100 + idx,
                            ac_content=sub_ac,
                            session_id=session_id,
                            tools=tools,
                            tool_catalog=tool_catalog,
                            system_prompt=system_prompt,
                            seed_goal=seed_goal,
                            depth=sub_depth,
                            execution_id=execution_id,
                            level_contexts=level_contexts,
                            retry_attempt=retry_attempt,
                            execution_counters=execution_counters,
                            is_sub_ac=child_is_sub_ac,
                            parent_ac_index=ac_index if child_is_sub_ac else None,
                            sub_ac_index=idx if child_is_sub_ac else None,
                        )
                    except BaseException as e:
                        if isinstance(e, anyio.get_cancelled_exc_class()):
                            raise
                        sub_results[idx] = e

                # Convert exceptions and None sentinels to failed results
                final_sub_results: list[ACExecutionResult] = []
                for i, result in enumerate(sub_results):
                    if isinstance(result, BaseException) or result is None:
                        final_sub_results.append(
                            ACExecutionResult(
                                ac_index=ac_index * 100 + i,
                                ac_content=sub_acs[i],
                                success=False,
                                error=str(result)
                                if isinstance(result, BaseException)
                                else "Task cancelled or produced no result",
                                retry_attempt=retry_attempt,
                                depth=sub_depth,
                            )
                        )
                    else:
                        final_sub_results.append(result)

                success_count = sum(1 for result in final_sub_results if result.success)
                self._console.print(
                    f"    [{'green' if success_count == len(sub_acs) else 'yellow'}]"
                    f"Sub-ACs completed: {success_count}/{len(sub_acs)} succeeded[/]"
                )

                # Update TUI with final statuses
                for i, result in enumerate(final_sub_results):
                    status = "completed" if result.success else "failed"
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_desc=sub_acs[i][:50],
                        status=status,
                    )

                duration = (datetime.now(UTC) - start_time).total_seconds()
                all_success = all(result.success for result in final_sub_results)

                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=all_success,
                    messages=(),
                    final_message="\n".join(
                        _render_ac_section(
                            ACExecutionResult(
                                ac_index=ac_index,
                                ac_content=ac_content,
                                success=all_success,
                                messages=(),
                                duration_seconds=duration,
                                is_decomposed=True,
                                sub_results=tuple(final_sub_results),
                                depth=depth,
                            ),
                            index_path=(ac_index + 1,),
                            heading_level=3,
                            include_header=False,
                        )
                    ),
                    duration_seconds=duration,
                    retry_attempt=retry_attempt,
                    is_decomposed=True,
                    sub_results=tuple(final_sub_results),
                    depth=depth,
                )

        # Depth-limit canary: execution is forced atomic once the soft recursion
        # safety net is reached, so downstream stages can detect decomposition pressure.
        decomposition_depth_warning = (
            self._enable_decomposition and depth >= self._max_decomposition_depth
        )

        # Stall recovery belongs to atomic leaves only. Once this method decides
        # to execute atomically, it can retry the leaf without re-running the
        # decomposition/dispatch branch above.
        atomic_retry_attempt = retry_attempt
        max_attempts = retry_attempt + MAX_STALL_RETRIES + 1
        execution_context_id = execution_id or session_id

        while True:
            atomic_result = await self._execute_atomic_ac(
                ac_index=ac_index,
                ac_content=ac_content,
                session_id=session_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                seed_goal=seed_goal,
                depth=depth,
                start_time=start_time,
                execution_id=execution_id,
                level_contexts=level_contexts,
                sibling_acs=sibling_acs,
                retry_attempt=atomic_retry_attempt,
                execution_counters=execution_counters,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
            )
            if atomic_result.error != _STALL_SENTINEL:
                if decomposition_depth_warning:
                    return replace(atomic_result, decomposition_depth_warning=True)
                return atomic_result

            runtime_identity = build_ac_runtime_identity(
                ac_index,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=atomic_retry_attempt,
            )
            should_retry = atomic_retry_attempt - retry_attempt < MAX_STALL_RETRIES
            await self._safe_emit_event(
                create_ac_stall_detected_event(
                    session_id=session_id,
                    ac_index=ac_index,
                    ac_id=runtime_identity.ac_id,
                    silent_seconds=STALL_TIMEOUT_SECONDS,
                    attempt=runtime_identity.attempt_number,
                    max_attempts=max_attempts,
                    action="restart" if should_retry else "abandon",
                )
            )

            if not should_retry:
                log.error(
                    "parallel_executor.ac.stall_abandoned",
                    session_id=session_id,
                    ac_index=ac_index,
                    depth=depth,
                    retry_attempt=atomic_retry_attempt,
                )
                failed_result = replace(
                    atomic_result,
                    error=f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)",
                )
                if decomposition_depth_warning:
                    return replace(failed_result, decomposition_depth_warning=True)
                return failed_result

            atomic_retry_attempt += 1

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
            async with asyncio.timeout(DECOMPOSITION_TIMEOUT_SECONDS):
                async for message in self._adapter.execute_task(
                    prompt=decompose_prompt,
                    tools=[],  # No tools for decomposition analysis
                    system_prompt="You are a task decomposition expert. Analyze tasks and break them down if needed.",
                    resume_handle=self._inherited_runtime_handle,
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

        except TimeoutError:
            log.warning(
                "parallel_executor.decomposition.timeout",
                ac_index=ac_index,
                timeout_seconds=DECOMPOSITION_TIMEOUT_SECONDS,
            )
            return None
        except Exception as e:
            log.warning(
                "parallel_executor.decomposition.error",
                ac_index=ac_index,
                error=str(e),
            )
            return None

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

    async def _wait_for_memory(self, label: str) -> None:
        """Block until system has enough free memory to spawn a subprocess."""
        requires_memory_gate = getattr(self._adapter, "_requires_memory_gate", None)
        if not isinstance(requires_memory_gate, bool):
            requires_memory_gate = False
        if not requires_memory_gate:
            return

        elapsed = 0.0
        while elapsed < _MEMORY_WAIT_MAX_SECONDS:
            available_gb = _get_available_memory_gb()
            if available_gb is None or available_gb >= _MIN_FREE_MEMORY_GB:
                return
            log.warning(
                "memory_pressure.waiting",
                available_gb=round(available_gb, 2),
                label=label,
            )
            await asyncio.sleep(_MEMORY_CHECK_INTERVAL_SECONDS)
            elapsed += _MEMORY_CHECK_INTERVAL_SECONDS
        log.warning("memory_pressure.timeout", label=label)

    @staticmethod
    def _runtime_event_metadata(message: AgentMessage) -> dict[str, Any]:
        """Serialize shared runtime/tool metadata for execution-scoped events."""
        projected = project_runtime_message(message)
        return dict(projected.runtime_metadata)

    @staticmethod
    def _message_tool_input_preview(tool_input: dict[str, Any]) -> str | None:
        """Build a compact preview string for shared session tool-call events."""
        if not tool_input:
            return None

        parts: list[str] = []
        for key, value in tool_input.items():
            rendered = str(value).strip()
            if rendered:
                parts.append(f"{key}: {rendered}")
        preview = ", ".join(parts)
        return preview[:100] if preview else None

    @staticmethod
    def _should_emit_session_progress_event(
        message: AgentMessage,
        *,
        projected: Any,
        messages_processed: int,
    ) -> bool:
        """Reuse the shared progress-emission policy for AC session messages."""
        runtime_backend = message.resume_handle.backend if message.resume_handle else None
        return (
            message.is_final
            or messages_processed % 10 == 0
            or projected.is_tool_call
            or projected.thinking is not None
            or message.type == "system"
            or runtime_backend == "opencode"
            or projected.is_tool_result
        )

    def _build_session_progress_event(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        projected: Any,
    ):
        """Create a shared session progress event from an AC runtime message."""
        from ouroboros.orchestrator.events import create_progress_event
        from ouroboros.orchestrator.workflow_state import coerce_ac_marker_update

        message_type = projected.message_type
        event = create_progress_event(
            session_id=session_id,
            message_type=message_type,
            content_preview=projected.content,
            tool_name=projected.tool_name if message_type in {"tool", "tool_result"} else None,
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
            "progress": {
                "last_message_type": message_type,
                "last_content_preview": projected.content[:200],
            },
        }
        runtime = event_data.get("runtime")
        if isinstance(runtime, dict):
            event_data["progress"]["runtime"] = runtime
        runtime_event_type = event_data.get("runtime_event_type")
        if isinstance(runtime_event_type, str) and runtime_event_type:
            event_data["progress"]["runtime_event_type"] = runtime_event_type
        runtime_signal = event_data.get("runtime_signal")
        if isinstance(runtime_signal, str) and runtime_signal:
            event_data["progress"]["runtime_signal"] = runtime_signal
        runtime_status = event_data.get("runtime_status")
        if isinstance(runtime_status, str) and runtime_status:
            event_data["progress"]["runtime_status"] = runtime_status
        thinking = event_data.get("thinking")
        if isinstance(thinking, str) and thinking:
            event_data["progress"]["thinking"] = thinking
        ac_tracking = coerce_ac_marker_update(event_data.get("ac_tracking"))
        if not ac_tracking.is_empty:
            event_data["progress"]["ac_tracking"] = ac_tracking.to_dict()
        return event.model_copy(update={"data": event_data})

    def _build_session_tool_called_event(
        self,
        session_id: str,
        *,
        projected: Any,
    ):
        """Create a shared session tool-call event from an AC runtime message."""
        from ouroboros.orchestrator.events import create_tool_called_event

        if projected.tool_name is None:
            return None

        event = create_tool_called_event(
            session_id=session_id,
            tool_name=projected.tool_name,
            tool_input_preview=self._message_tool_input_preview(projected.tool_input),
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
        }
        return event.model_copy(update={"data": event_data})

    @staticmethod
    def _coordinator_aggregate_id(execution_id: str, level: int) -> str:
        """Build a deterministic level-scoped aggregate ID for coordinator work."""
        return f"{execution_id}:l{level - 1}:coord"

    async def _emit_coordinator_started(
        self,
        execution_id: str,
        session_id: str,
        level: int,
        conflicts: list[Any],
    ) -> None:
        """Emit a level-scoped event when coordinator reconciliation starts."""
        from ouroboros.events.base import BaseEvent

        runtime_scope = build_level_coordinator_runtime_scope(execution_id, level)
        event = BaseEvent(
            type="execution.coordinator.started",
            aggregate_type="execution",
            aggregate_id=self._coordinator_aggregate_id(execution_id, level),
            data={
                "execution_id": execution_id,
                "session_id": session_id,
                "scope": "level",
                "session_role": "coordinator",
                "stage_index": level - 1,
                "level_number": level,
                "session_scope_id": runtime_scope.aggregate_id,
                "session_state_path": runtime_scope.state_path,
                "conflict_count": len(conflicts),
                "conflicts": [
                    {
                        "file_path": conflict.file_path,
                        "ac_indices": list(conflict.ac_indices),
                    }
                    for conflict in conflicts
                ],
            },
        )
        await self._event_store.append(event)

    async def _emit_coordinator_runtime_events(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
    ) -> None:
        """Persist normalized coordinator runtime audit events at level scope."""
        from ouroboros.events.base import BaseEvent

        aggregate_id = self._coordinator_aggregate_id(execution_id, review.level_number)
        base_data = {
            "execution_id": execution_id,
            "session_id": session_id,
            "coordinator_session_id": review.session_id,
            "scope": review.scope,
            "session_role": review.session_role,
            "stage_index": review.stage_index,
            "level_number": review.level_number,
            "session_scope_id": review.artifact_owner_id,
            "session_state_path": review.artifact_state_path,
        }

        for message in review.messages:
            projected = project_runtime_message(message)

            if projected.is_tool_call and projected.tool_name is not None:
                tool_input = projected.tool_input
                tool_event = BaseEvent(
                    type="execution.coordinator.tool.started",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "tool_name": projected.tool_name,
                        "tool_detail": self._format_tool_detail(projected.tool_name, tool_input),
                        "tool_input": tool_input,
                        **self._runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(tool_event)

            if projected.is_tool_result and projected.tool_name is not None:
                tool_result_event = BaseEvent(
                    type="execution.coordinator.tool.completed",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "tool_name": projected.tool_name,
                        "tool_result_text": projected.content,
                        **self._runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(tool_result_event)

            if projected.thinking:
                thinking_event = BaseEvent(
                    type="execution.coordinator.thinking",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "thinking_text": projected.thinking,
                        **self._runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(thinking_event)

    async def _emit_coordinator_completed(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
    ) -> None:
        """Persist the coordinator reconciliation result as a level-scoped artifact."""
        from ouroboros.events.base import BaseEvent

        event = BaseEvent(
            type="execution.coordinator.completed",
            aggregate_type="execution",
            aggregate_id=self._coordinator_aggregate_id(execution_id, review.level_number),
            data={
                "execution_id": execution_id,
                "session_id": session_id,
                "coordinator_session_id": review.session_id,
                **review.to_artifact_payload(),
                "conflicts_detected": [
                    {
                        "file_path": conflict.file_path,
                        "ac_indices": list(conflict.ac_indices),
                        "resolved": conflict.resolved,
                        "resolution_description": conflict.resolution_description,
                    }
                    for conflict in review.conflicts_detected
                ],
                "review_summary": review.review_summary,
                "fixes_applied": list(review.fixes_applied),
                "warnings_for_next_level": list(review.warnings_for_next_level),
                "duration_seconds": review.duration_seconds,
            },
        )
        await self._event_store.append(event)

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
        execution_id: str = "",
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[str] | None = None,
        retry_attempt: int = 0,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
        execution_counters: dict[str, int] | None = None,
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

        retry_section = ""
        if retry_attempt > 0:
            retry_section = (
                "\n## Retry Context\n"
                f"This is retry attempt {retry_attempt} for this acceptance criterion.\n"
                "Resume from the current shared workspace state, including any "
                "coordinator-reconciled changes already applied.\n"
            )

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

        # Scan the requested runtime workspace so prompts stay aligned with the actual task cwd.
        import os

        cwd = self._task_cwd or self._adapter.working_directory
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        try:
            entries = sorted(os.listdir(cwd))
            file_listing = "\n".join(f"- {e}" for e in entries if not e.startswith("."))
        except OSError:
            file_listing = "(unable to list)"

        prompt = f"""Execute the following task:

## Working Directory
`{cwd}`

Files present:
{file_listing}

**Important**: Use Glob to discover files. Never guess absolute paths.

## Goal Context
{seed_goal}

## Your Task ({label})
{ac_content}
{context_section}{retry_section}{parallel_section}
Use the available tools to accomplish this task. Report your progress clearly.
When complete, explicitly state: [TASK_COMPLETE]
"""

        messages: list[AgentMessage] = []
        final_message = ""
        success = False
        clear_cached_runtime_handle = False
        execution_context_id = execution_id or session_id
        persisted_runtime_handle = await self._load_persisted_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        if persisted_runtime_handle is not None:
            self._remember_ac_runtime_handle(
                ac_index,
                persisted_runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
            )
        runtime_handle = self._build_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
            tool_catalog=tool_catalog,
        )
        runtime_identity = build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            retry_attempt=retry_attempt,
        )
        lifecycle_event_type = (
            "execution.session.resumed"
            if self._is_resumable_runtime_handle(runtime_handle)
            else "execution.session.started"
        )
        lifecycle_emitted = False
        emitted_recovery_turn_ids: set[str] = set()

        # Stall detection: CancelScope with resettable deadline (RC6)
        message_count = 0
        last_heartbeat = time.monotonic()
        exec_start = time.monotonic()

        await self._wait_for_memory(label)

        try:
            with anyio.CancelScope(
                deadline=anyio.current_time() + STALL_TIMEOUT_SECONDS,
            ) as stall_scope:
                async for message in self._adapter.execute_task(
                    prompt=prompt,
                    tools=tools,
                    system_prompt=system_prompt,
                    resume_handle=runtime_handle,
                ):
                    # Reset stall deadline on every message (RC6 core)
                    stall_scope.deadline = anyio.current_time() + STALL_TIMEOUT_SECONDS
                    if message.resume_handle is not None:
                        runtime_handle = self._remember_ac_runtime_handle(
                            ac_index,
                            message.resume_handle,
                            execution_context_id=execution_context_id,
                            is_sub_ac=is_sub_ac,
                            parent_ac_index=parent_ac_index,
                            sub_ac_index=sub_ac_index,
                            retry_attempt=retry_attempt,
                        )

                    if runtime_handle is not None and runtime_handle.native_session_id:
                        ac_session_id = runtime_handle.native_session_id
                    elif (
                        message.resume_handle is None
                        and isinstance(message.data.get("session_id"), str)
                        and message.data["session_id"]
                    ):
                        ac_session_id = message.data["session_id"]

                    runtime_handle = self._with_native_session_id(runtime_handle, ac_session_id)
                    if runtime_handle is not None and message.resume_handle is not None:
                        message = replace(message, resume_handle=runtime_handle)

                    recovery_discontinuity = self._runtime_recovery_discontinuity(runtime_handle)
                    if recovery_discontinuity is not None:
                        replacement = recovery_discontinuity.get("replacement", {})
                        replacement_turn_id = replacement.get("turn_id")
                        if isinstance(replacement_turn_id, str) and replacement_turn_id:
                            if replacement_turn_id not in emitted_recovery_turn_ids:
                                await self._emit_ac_runtime_event(
                                    event_type="execution.session.recovered",
                                    runtime_identity=runtime_identity,
                                    ac_content=ac_content,
                                    runtime_handle=runtime_handle,
                                    session_id=ac_session_id,
                                )
                                emitted_recovery_turn_ids.add(replacement_turn_id)

                    messages.append(message)
                    message_count += 1
                    if execution_counters is not None:
                        async with self._execution_counters_lock:
                            execution_counters["messages_count"] = (
                                execution_counters.get("messages_count", 0) + 1
                            )

                    # RC1: Emit heartbeat piggybacking on message flow
                    now = time.monotonic()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        ac_id = runtime_identity.ac_id
                        await self._safe_emit_event(
                            create_heartbeat_event(
                                session_id=session_id,
                                ac_index=ac_index,
                                ac_id=ac_id,
                                elapsed_seconds=now - exec_start,
                                message_count=message_count,
                            )
                        )
                        last_heartbeat = now

                    projected = project_runtime_message(message)

                    persisted_session_id = self._runtime_resume_session_id(runtime_handle)
                    if not lifecycle_emitted and persisted_session_id:
                        await self._emit_ac_runtime_event(
                            event_type=lifecycle_event_type,
                            runtime_identity=runtime_identity,
                            ac_content=ac_content,
                            runtime_handle=runtime_handle,
                            session_id=persisted_session_id,
                        )
                        lifecycle_emitted = True
                        self._remember_ac_runtime_handle(
                            ac_index,
                            runtime_handle,
                            execution_context_id=execution_context_id,
                            is_sub_ac=is_sub_ac,
                            parent_ac_index=parent_ac_index,
                            sub_ac_index=sub_ac_index,
                            retry_attempt=retry_attempt,
                        )

                    session_tool_event = self._build_session_tool_called_event(
                        session_id,
                        projected=projected,
                    )
                    if session_tool_event is not None:
                        await self._event_store.append(session_tool_event)

                    if self._should_emit_session_progress_event(
                        message,
                        projected=projected,
                        messages_processed=len(messages),
                    ):
                        session_progress_event = self._build_session_progress_event(
                            session_id,
                            message,
                            projected=projected,
                        )
                        await self._event_store.append(session_progress_event)

                    if projected.is_tool_call and projected.tool_name is not None:
                        # RC6: Tool invocations prove liveness — reset stall
                        # deadline so long-running tools (Bash, external APIs)
                        # are not falsely detected as stalls.
                        stall_scope.deadline = anyio.current_time() + STALL_TIMEOUT_SECONDS
                        if execution_counters is not None:
                            async with self._execution_counters_lock:
                                execution_counters["tool_calls_count"] = (
                                    execution_counters.get("tool_calls_count", 0) + 1
                                )
                        tool_input = projected.tool_input
                        tool_detail = self._format_tool_detail(projected.tool_name, tool_input)
                        self._console.print(f"{indent}[yellow]{label} → {tool_detail}[/yellow]")
                        self._flush_console()

                        # Emit tool started event for TUI
                        from ouroboros.events.base import BaseEvent as _BaseEvent

                        tool_event = _BaseEvent(
                            type="execution.tool.started",
                            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                            aggregate_id=runtime_identity.session_scope_id,
                            data={
                                "ac_id": runtime_identity.ac_id,
                                "retry_attempt": runtime_identity.retry_attempt,
                                "attempt_number": runtime_identity.attempt_number,
                                "session_scope_id": runtime_identity.session_scope_id,
                                "session_attempt_id": runtime_identity.session_attempt_id,
                                "tool_name": projected.tool_name,
                                "tool_detail": tool_detail,
                                "tool_input": tool_input,
                                **self._runtime_event_metadata(message),
                            },
                        )
                        await self._event_store.append(tool_event)

                    if projected.is_tool_result and projected.tool_name is not None:
                        from ouroboros.events.base import BaseEvent as _BaseEvent

                        tool_result_event = _BaseEvent(
                            type="execution.tool.completed",
                            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                            aggregate_id=runtime_identity.session_scope_id,
                            data={
                                "ac_id": runtime_identity.ac_id,
                                "retry_attempt": runtime_identity.retry_attempt,
                                "attempt_number": runtime_identity.attempt_number,
                                "session_scope_id": runtime_identity.session_scope_id,
                                "session_attempt_id": runtime_identity.session_attempt_id,
                                "tool_name": projected.tool_name,
                                "tool_result_text": projected.content,
                                **self._runtime_event_metadata(message),
                            },
                        )
                        await self._event_store.append(tool_result_event)

                    if projected.thinking:
                        from ouroboros.events.base import BaseEvent as _BaseEvent

                        thinking_event = _BaseEvent(
                            type="execution.agent.thinking",
                            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                            aggregate_id=runtime_identity.session_scope_id,
                            data={
                                "ac_id": runtime_identity.ac_id,
                                "retry_attempt": runtime_identity.retry_attempt,
                                "attempt_number": runtime_identity.attempt_number,
                                "session_scope_id": runtime_identity.session_scope_id,
                                "session_attempt_id": runtime_identity.session_attempt_id,
                                "thinking_text": projected.thinking,
                                **self._runtime_event_metadata(message),
                            },
                        )
                        await self._event_store.append(thinking_event)

                    if message.is_final:
                        final_message = message.content
                        success = not message.is_error

            # Check if stall was detected (CancelScope ate the Cancelled)
            if stall_scope.cancelled_caught:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                log.warning(
                    "parallel_executor.ac.stall_detected",
                    ac_index=ac_index,
                    depth=depth,
                    silent_seconds=STALL_TIMEOUT_SECONDS,
                    message_count=message_count,
                )
                clear_cached_runtime_handle = True
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    messages=tuple(messages),
                    error=_STALL_SENTINEL,
                    duration_seconds=duration,
                    session_id=ac_session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                )

            self._remember_ac_runtime_handle(
                ac_index,
                runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
            )

            duration = (datetime.now(UTC) - start_time).total_seconds()

            await self._emit_ac_runtime_event(
                event_type=(
                    "execution.session.completed" if success else "execution.session.failed"
                ),
                runtime_identity=runtime_identity,
                ac_content=ac_content,
                runtime_handle=runtime_handle,
                session_id=ac_session_id,
                result_summary=final_message or None,
                success=success,
                error=None if success else final_message or "Implementation session failed",
            )
            clear_cached_runtime_handle = True

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
                retry_attempt=retry_attempt,
                depth=depth,
                runtime_handle=runtime_handle,
            )

        except Exception as e:
            duration = (datetime.now(UTC) - start_time).total_seconds()

            self._remember_ac_runtime_handle(
                ac_index,
                runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
            )
            await self._emit_ac_runtime_event(
                event_type="execution.session.failed",
                runtime_identity=runtime_identity,
                ac_content=ac_content,
                runtime_handle=runtime_handle,
                session_id=ac_session_id,
                success=False,
                error=str(e),
            )
            clear_cached_runtime_handle = True

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
                retry_attempt=retry_attempt,
                depth=depth,
                runtime_handle=runtime_handle,
            )
        finally:
            if clear_cached_runtime_handle:
                await self._terminate_runtime_handle(
                    runtime_handle,
                    runtime_scope_id=runtime_identity.session_scope_id,
                )
                self._forget_ac_runtime_handle(
                    ac_index,
                    execution_context_id=execution_context_id,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    retry_attempt=retry_attempt,
                )

    async def _emit_subtask_event(
        self,
        execution_id: str,
        ac_index: int,
        sub_task_index: int,
        sub_task_desc: str,
        status: str,
    ) -> None:
        """Emit sub-task event for TUI tree updates.

        ``ac_index`` arrives 0-based from the executor loop but the TUI
        tree keys AC nodes as ``ac_{1-based}``, so we convert here.
        """
        from ouroboros.events.base import BaseEvent

        ac_index_1 = ac_index + 1  # 0-based → 1-based for TUI node keys
        event = BaseEvent(
            type="execution.subtask.updated",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "ac_index": ac_index_1,
                "sub_task_index": sub_task_index,
                "sub_task_id": f"ac_{ac_index_1}_sub_{sub_task_index}",
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
        blocked_count: int = 0,
        started: bool = True,
        outcome: str | None = None,
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
                "failed": failure_count,
                "blocked": blocked_count,
                "started": started,
                "outcome": outcome or StageExecutionOutcome.SUCCEEDED.value,
                "total": success_count + failure_count + blocked_count,
            },
        )
        await self._event_store.append(event)

    async def _resilient_progress_emitter(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        progress_state: dict[str, int],
        interval: float = 15.0,
        max_consecutive_errors: int = 5,
    ) -> None:
        """Periodically emit workflow progress with error resilience (RC2 + RC4).

        Runs as a background task inside a task group. Terminates when:
        - All ACs are in terminal state (RC4: no stale monitoring)
        - Consecutive errors exceed threshold (RC2: graceful degradation)
        - Task group cancel scope triggers (execution loop finished)

        Args:
            session_id: Session ID.
            execution_id: Execution ID.
            seed: Seed specification.
            ac_statuses: Shared dict of AC statuses (mutated externally).
            progress_state: Shared dict with ``current_level`` and ``total_levels``
                keys, mutated by the main execution loop.
            interval: Seconds between emissions.
            max_consecutive_errors: Stop after this many consecutive failures.
        """
        consecutive_errors = 0
        terminal_states = {"completed", "failed", "skipped"}

        while True:
            await anyio.sleep(interval)

            # RC4: Stop when all ACs are done
            if all(s in terminal_states for s in ac_statuses.values()):
                log.info("parallel_executor.progress_emitter.all_done")
                return

            try:
                await self._emit_workflow_progress(
                    session_id=session_id,
                    execution_id=execution_id,
                    seed=seed,
                    ac_statuses=ac_statuses,
                    ac_retry_attempts=None,
                    executing_indices=[i for i, s in ac_statuses.items() if s == "executing"],
                    completed_count=sum(1 for s in ac_statuses.values() if s == "completed"),
                    current_level=progress_state.get("current_level", 0),
                    total_levels=progress_state.get("total_levels", 0),
                    activity="Monitoring",
                )
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                wait = min(2.0**consecutive_errors, 30.0)
                log.warning(
                    "parallel_executor.progress_emitter.error",
                    error=str(e),
                    consecutive_errors=consecutive_errors,
                )
                if consecutive_errors >= max_consecutive_errors:
                    log.error(
                        "parallel_executor.progress_emitter.giving_up",
                        consecutive_errors=consecutive_errors,
                    )
                    return
                await anyio.sleep(wait)

    async def _emit_workflow_progress(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        ac_retry_attempts: dict[int, int] | None,
        executing_indices: list[int],
        completed_count: int,
        current_level: int,
        total_levels: int,
        activity: str = "Executing",
        messages_count: int = 0,
        tool_calls_count: int = 0,
    ) -> None:
        """Emit workflow progress event for TUI updates.

        Args:
            session_id: Session ID.
            execution_id: Execution ID.
            seed: Seed specification.
            ac_statuses: Dict mapping AC index to status string.
            ac_retry_attempts: Dict mapping AC index to reopen retry count.
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
            retry_attempt = (ac_retry_attempts or {}).get(i, 0)
            runtime_scope = build_ac_runtime_scope(
                i,
                execution_context_id=execution_id or session_id,
                retry_attempt=retry_attempt,
            )
            acceptance_criteria.append(
                {
                    "index": i + 1,
                    "ac_id": runtime_scope.aggregate_id,
                    "content": ac_content,
                    "status": status,
                    "retry_attempt": retry_attempt,
                    "attempt_number": runtime_scope.attempt_number,
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
            messages_count=messages_count,
            tool_calls_count=tool_calls_count,
        )
        await self._event_store.append(event)


__all__ = [
    "ACExecutionOutcome",
    "ACExecutionResult",
    "ParallelExecutionStageResult",
    "StageExecutionOutcome",
    "ParallelExecutionResult",
    "ParallelACExecutor",
]
