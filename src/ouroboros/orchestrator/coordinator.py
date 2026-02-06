"""Level Coordinator for inter-level review and conflict resolution.

Detects file conflicts from parallel AC execution results and optionally
invokes a Claude session to auto-resolve them. Acts as an intelligent
review gate between dependency levels.

Architecture: Approach A (Pragmatic)
- Pure Python conflict detection from in-memory ACExecutionResult data
- Claude session only when file conflicts are detected
- Zero cost when no conflicts exist

Usage:
    coordinator = LevelCoordinator(adapter)
    conflicts = coordinator.detect_file_conflicts(level_results)

    if conflicts:
        review = await coordinator.run_review(
            conflicts=conflicts,
            level_context=level_ctx,
            level_number=1,
        )
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage, ClaudeAgentAdapter
    from ouroboros.orchestrator.level_context import LevelContext
    from ouroboros.orchestrator.parallel_executor import ACExecutionResult

log = get_logger(__name__)


# Tools available to the Coordinator Claude session
COORDINATOR_TOOLS: list[str] = ["Read", "Bash", "Edit", "Grep", "Glob"]

# System prompt for the Coordinator agent
COORDINATOR_SYSTEM_PROMPT = (
    "You are a Level Coordinator reviewing parallel AC execution results. "
    "Your job is to detect and resolve file conflicts, then provide actionable "
    "guidance for the next level of execution. Be concise and precise."
)


@dataclass(frozen=True, slots=True)
class FileConflict:
    """A file modified by multiple ACs in the same level.

    Attributes:
        file_path: Path to the conflicting file.
        ac_indices: Which ACs modified this file.
        resolved: Whether the conflict was resolved by the Coordinator.
        resolution_description: How the conflict was resolved.
    """

    file_path: str
    ac_indices: tuple[int, ...]
    resolved: bool = False
    resolution_description: str = ""


@dataclass(frozen=True, slots=True)
class CoordinatorReview:
    """Result of a Coordinator review between dependency levels.

    Attributes:
        level_number: Which level was reviewed.
        conflicts_detected: File conflicts found.
        review_summary: Coordinator's analysis text.
        fixes_applied: Descriptions of fixes made.
        warnings_for_next_level: Injected into next level prompt.
        duration_seconds: Time spent on review.
        session_id: Claude session ID (None if no session was needed).
    """

    level_number: int
    conflicts_detected: tuple[FileConflict, ...] = field(default_factory=tuple)
    review_summary: str = ""
    fixes_applied: tuple[str, ...] = field(default_factory=tuple)
    warnings_for_next_level: tuple[str, ...] = field(default_factory=tuple)
    duration_seconds: float = 0.0
    session_id: str | None = None


class LevelCoordinator:
    """Coordinates between parallel execution levels.

    Detects file conflicts from AC execution results and optionally
    invokes Claude to resolve them.
    """

    def __init__(self, adapter: ClaudeAgentAdapter) -> None:
        """Initialize coordinator.

        Args:
            adapter: Claude Agent adapter for conflict resolution sessions.
        """
        self._adapter = adapter

    @staticmethod
    def detect_file_conflicts(
        level_results: list[ACExecutionResult],
    ) -> list[FileConflict]:
        """Detect files modified by multiple ACs in the same level.

        Scans ACExecutionResult.messages for Write/Edit tool calls and
        identifies files touched by more than one AC.

        Args:
            level_results: Results from ACs executed in the same level.

        Returns:
            List of FileConflict for files modified by 2+ ACs.
        """
        # Map file_path → set of ac_indices that modified it
        file_to_acs: dict[str, set[int]] = defaultdict(set)

        for result in level_results:
            _collect_file_modifications(result, file_to_acs)

        # Filter to files with 2+ writers
        conflicts: list[FileConflict] = []
        for file_path, ac_indices in sorted(file_to_acs.items()):
            if len(ac_indices) >= 2:
                conflicts.append(
                    FileConflict(
                        file_path=file_path,
                        ac_indices=tuple(sorted(ac_indices)),
                    )
                )

        if conflicts:
            log.warning(
                "coordinator.conflicts_detected",
                conflict_count=len(conflicts),
                files=[c.file_path for c in conflicts],
            )
        else:
            log.info("coordinator.no_conflicts")

        return conflicts

    async def run_review(
        self,
        conflicts: list[FileConflict],
        level_context: LevelContext,
        level_number: int,
    ) -> CoordinatorReview:
        """Run a Claude session to review and resolve file conflicts.

        Only called when conflicts are detected (Approach A).

        Args:
            conflicts: Detected file conflicts.
            level_context: Context from the completed level.
            level_number: Which level was just completed.

        Returns:
            CoordinatorReview with resolution details.
        """
        start_time = datetime.now(UTC)

        prompt = _build_review_prompt(conflicts, level_context, level_number)

        log.info(
            "coordinator.review.started",
            level=level_number,
            conflict_count=len(conflicts),
        )

        session_id: str | None = None
        final_text = ""

        try:
            async for message in self._adapter.execute_task(
                prompt=prompt,
                tools=COORDINATOR_TOOLS,
                system_prompt=COORDINATOR_SYSTEM_PROMPT,
            ):
                if message.data.get("session_id"):
                    session_id = message.data["session_id"]
                if message.is_final:
                    final_text = message.content

        except Exception as e:
            log.exception(
                "coordinator.review.failed",
                level=level_number,
                error=str(e),
            )
            duration = (datetime.now(UTC) - start_time).total_seconds()
            return CoordinatorReview(
                level_number=level_number,
                conflicts_detected=tuple(conflicts),
                review_summary=f"Coordinator review failed: {e}",
                duration_seconds=duration,
            )

        duration = (datetime.now(UTC) - start_time).total_seconds()

        # Parse structured response from Claude
        review = _parse_review_response(
            final_text, conflicts, level_number, duration, session_id
        )

        log.info(
            "coordinator.review.completed",
            level=level_number,
            fixes_applied=len(review.fixes_applied),
            warnings=len(review.warnings_for_next_level),
            duration_seconds=duration,
        )

        return review


def _collect_file_modifications(
    result: ACExecutionResult,
    file_to_acs: dict[str, set[int]],
) -> None:
    """Recursively collect file modifications from an AC result.

    Handles both atomic and decomposed (Sub-AC) results.

    Args:
        result: AC execution result to scan.
        file_to_acs: Accumulator mapping file_path → ac_indices.
    """
    # Check direct messages for Write/Edit tool calls
    for msg in result.messages:
        if msg.tool_name in ("Write", "Edit"):
            tool_input = msg.data.get("tool_input", {})
            file_path = tool_input.get("file_path")
            if file_path:
                file_to_acs.setdefault(file_path, set()).add(result.ac_index)

    # Recurse into Sub-AC results
    for sub_result in result.sub_results:
        # Sub-ACs inherit the parent AC index for conflict tracking
        for msg in sub_result.messages:
            if msg.tool_name in ("Write", "Edit"):
                tool_input = msg.data.get("tool_input", {})
                file_path = tool_input.get("file_path")
                if file_path:
                    file_to_acs.setdefault(file_path, set()).add(result.ac_index)


def _build_review_prompt(
    conflicts: list[FileConflict],
    level_context: LevelContext,
    level_number: int,
) -> str:
    """Build the prompt for the Coordinator Claude session.

    Args:
        conflicts: Detected file conflicts.
        level_context: Context from the completed level.
        level_number: Which level was just completed.

    Returns:
        Formatted prompt string.
    """
    context_text = level_context.to_prompt_text()

    conflict_lines: list[str] = []
    for conflict in conflicts:
        ac_list = ", ".join(f"AC {i + 1}" for i in conflict.ac_indices)
        conflict_lines.append(f"- `{conflict.file_path}` modified by: {ac_list}")
    conflict_text = "\n".join(conflict_lines)

    return f"""Review the results of Level {level_number} parallel AC execution.

## Level {level_number} Results
{context_text}

## File Conflicts Detected
{conflict_text}

## Your Tasks
1. Read the conflicting files using the Read tool
2. Run `git diff` if needed to understand changes
3. If edits from different ACs conflict, resolve them using the Edit tool
4. Provide your review as a structured JSON response:

```json
{{
  "review_summary": "Brief analysis of the level results",
  "fixes_applied": ["Description of fix 1", "..."],
  "warnings_for_next_level": ["Warning 1 for next ACs", "..."],
  "conflicts_resolved": ["{conflicts[0].file_path if conflicts else ''}"]
}}
```

Respond with the JSON block after completing your review and any fixes.
"""


def _parse_review_response(
    response_text: str,
    original_conflicts: list[FileConflict],
    level_number: int,
    duration: float,
    session_id: str | None,
) -> CoordinatorReview:
    """Parse the Coordinator's structured JSON response.

    Falls back to using the raw response as review_summary if JSON parsing fails.

    Args:
        response_text: Raw response from the Coordinator Claude session.
        original_conflicts: Original conflict list for resolution tracking.
        level_number: Level that was reviewed.
        duration: Time spent on review.
        session_id: Claude session ID.

    Returns:
        CoordinatorReview populated from the parsed response.
    """
    review_summary = ""
    fixes_applied: list[str] = []
    warnings: list[str] = []
    resolved_files: set[str] = set()

    # Try to extract JSON from the response
    json_match = re.search(r"```json\s*\n(.*?)\n```", response_text, re.DOTALL)
    if not json_match:
        # Try bare JSON object
        json_match = re.search(r"\{[^{}]*\}", response_text, re.DOTALL)

    if json_match:
        try:
            data = json.loads(json_match.group(1) if "```" in json_match.group() else json_match.group())
            review_summary = data.get("review_summary", "")
            fixes_applied = data.get("fixes_applied", [])
            warnings = data.get("warnings_for_next_level", [])
            resolved_files = set(data.get("conflicts_resolved", []))
        except (json.JSONDecodeError, IndexError):
            log.warning(
                "coordinator.parse_failed",
                response_preview=response_text[:200],
            )

    # Fallback: use raw text as summary
    if not review_summary:
        review_summary = response_text[:500].strip() if response_text else "No review output"

    # Mark conflicts as resolved based on Coordinator's report
    updated_conflicts: list[FileConflict] = []
    for conflict in original_conflicts:
        is_resolved = conflict.file_path in resolved_files
        updated_conflicts.append(
            FileConflict(
                file_path=conflict.file_path,
                ac_indices=conflict.ac_indices,
                resolved=is_resolved,
                resolution_description="Resolved by Coordinator" if is_resolved else "",
            )
        )

    return CoordinatorReview(
        level_number=level_number,
        conflicts_detected=tuple(updated_conflicts),
        review_summary=review_summary,
        fixes_applied=tuple(fixes_applied),
        warnings_for_next_level=tuple(warnings),
        duration_seconds=duration,
        session_id=session_id,
    )


__all__ = [
    "CoordinatorReview",
    "FileConflict",
    "LevelCoordinator",
    "COORDINATOR_TOOLS",
]
