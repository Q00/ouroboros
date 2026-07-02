"""Evidence and verification report rendering helpers."""

from __future__ import annotations

from ouroboros.orchestrator.evidence.common import _normalize_command, _truncate_text
from ouroboros.orchestrator.level_context import LevelContext
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult


def _subtask_event_label(content: str, *, max_length: int = 50) -> str:
    """Return compact display text without losing full event content."""
    normalized = " ".join(content.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[:max_length]


def _build_governed_parent_summary(level_contexts: list[LevelContext] | None) -> str:
    """Render level context for governed dispatch without nested H2 headings.

    ``build_context_prompt()`` is also used by the legacy prompt path, where its
    top-level ``##`` sections are appropriate.  Governed dispatch already wraps
    that text in ``## Parent context`` via ``compose_context()``, so preserving
    those headings creates a hard-to-scan nested hierarchy.  Build the governed
    variant from structured ``LevelContext`` data so only orchestrator-owned
    section wrappers become compact labels; embedded markdown from AC outputs
    remains byte-for-byte content inside its original summary text.
    """
    if not level_contexts:
        return ""

    sections: list[str] = []
    for ctx in level_contexts:
        text = ctx.to_prompt_text()
        if text:
            sections.append(text)

    has_reviews = any(ctx.coordinator_review for ctx in level_contexts)
    if not sections and not has_reviews:
        return ""

    lines: list[str] = []
    if sections:
        lines.extend(
            (
                "Previous Work Context:",
                "The following ACs have already been completed. "
                "Use this context to inform your work.",
                "",
                "\n\n".join(sections),
            )
        )

    for ctx in level_contexts:
        if ctx.coordinator_review:
            review = ctx.coordinator_review
            review_lines: list[str] = []

            if review.review_summary:
                review_lines.append(f"**Review**: {review.review_summary}")

            if review.fixes_applied:
                fixes = "; ".join(review.fixes_applied)
                review_lines.append(f"**Fixes applied**: {fixes}")

            if review.warnings_for_next_level:
                for warning in review.warnings_for_next_level:
                    review_lines.append(f"- WARNING: {warning}")

            if review_lines:
                if lines:
                    lines.append("")
                lines.append(f"Coordinator Review (Level {review.level_number}):")
                lines.extend(review_lines)

    return "\n".join(lines).strip()


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
    """Render a single Task or Subtask section for verification/audit output."""
    lines: list[str] = []
    if include_header:
        status = "COMPLETED" if result.success else "FAILED"
        label = "Task" if len(index_path) == 1 else "Subtask"
        lines.append(
            f"{'#' * heading_level} {label} {'.'.join(str(i) for i in index_path)}: "
            f"[{status}] {result.ac_content}"
        )

    if result.is_decomposed and result.sub_results:
        lines.append(f"Decomposed into {len(result.sub_results)} Subtasks")
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
