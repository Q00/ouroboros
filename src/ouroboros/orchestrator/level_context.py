"""Context extraction and injection for inter-level AC execution.

Extracts summaries from completed levels and injects them into
subsequent level prompts for continuity. This enables dependent ACs
to understand what previous ACs accomplished without re-discovering
through file system exploration.

Usage:
    from ouroboros.orchestrator.level_context import extract_level_context

    context = extract_level_context(
        results, level_num=0, workspace_root="/path/to/session/workspace"
    )
    prompt_text = context.to_prompt_text()
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import re
from typing import TYPE_CHECKING, Any

from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage
    from ouroboros.orchestrator.coordinator import CoordinatorReview

log = get_logger(__name__)

# Maximum characters for key_output to prevent prompt bloat
_MAX_KEY_OUTPUT_CHARS = 200
# Maximum characters for the entire level context section
_MAX_LEVEL_CONTEXT_CHARS = 2000
# Maximum characters for public API summary per AC
_MAX_PUBLIC_API_CHARS = 500
# Maximum file size (bytes) to read for API extraction (1 MB)
_MAX_FILE_SIZE_BYTES = 1_048_576
# Maximum number of files to process for public API summary
_MAX_FILES_FOR_API = 20


def _extract_public_api(file_path: str, workspace_root: str) -> list[str]:
    """Extract public API signatures from a source file.

    Reads the file and extracts top-level public definitions:
    - Python: class/def signatures (non-underscore prefixed)
    - TypeScript/JS: exported functions/classes/interfaces/types
    - Go: exported (capitalized) func/type signatures

    Args:
        file_path: Path to the source file to scan.
        workspace_root: Required workspace directory. Files whose resolved
            path falls outside this root are rejected (defence in depth against
            callers that forget to pre-filter). May be passed raw or already
            resolved — this function applies realpath() internally.

    Returns list of signature strings like:
        ["class UserService", "def get_user(id: str) -> User"]
    """
    if not workspace_root:
        # Required sentinel — empty root means reject everything.
        return []
    try:
        resolved_root = os.path.realpath(workspace_root)
        # Resolve symlinks to prevent symlink-based path traversal
        resolved = os.path.realpath(file_path)
        # Defence-in-depth: reject paths outside the workspace root even if
        # callers skipped their own containment check.
        if resolved != resolved_root and not resolved.startswith(resolved_root + os.sep):
            return []
        # Check file size before reading to avoid memory exhaustion
        if os.path.getsize(resolved) > _MAX_FILE_SIZE_BYTES:
            return []
        with open(resolved) as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    signatures: list[str] = []

    if file_path.endswith(".py"):
        # Extract top-level (non-indented) class and function signatures.
        # Handles multi-line signatures by accumulating lines until parens balance.
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^(class|def|async\s+def)\s+(\w+)", line)
            if m:
                name = m.group(2)
                if not name.startswith("_"):
                    # Accumulate multi-line signature until parens balance
                    sig_lines = [line.rstrip()]
                    open_parens = line.count("(") - line.count(")")
                    while open_parens > 0 and i + 1 < len(lines):
                        i += 1
                        sig_lines.append(lines[i].strip())
                        open_parens += lines[i].count("(") - lines[i].count(")")
                    sig = " ".join(sig_lines)
                    # Trim trailing colon and everything after
                    sig = re.sub(r":\s*$", "", sig)
                    # Collapse whitespace
                    sig = re.sub(r"\s{2,}", " ", sig).strip()
                    signatures.append(sig)
            i += 1

    elif file_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        for m in re.finditer(
            r"^export\s+(?:default\s+)?((?:function|class|interface|type|const|enum)\s+\w[^\n{;]*)",
            content,
            re.MULTILINE,
        ):
            signatures.append(m.group(1).strip())

    elif file_path.endswith(".go"):
        for m in re.finditer(
            r"^((?:func|type)\s+[A-Z]\w*[^\n{]*)",
            content,
            re.MULTILINE,
        ):
            signatures.append(m.group(1).strip())

    return signatures


def _build_public_api_summary(
    files_modified: tuple[str, ...],
    *,
    workspace_root: str,
) -> str:
    """Build a public API summary across all modified files.

    Args:
        files_modified: Tuple of file paths to scan.
        workspace_root: Required workspace directory. Only files whose
            realpath falls within this directory are processed. Passing an
            empty string or a path outside any expected tree will cause all
            files to be rejected — there is no bypass path.

    Returns a compact string like:
        user_service.py: class UserService, def get_user(id: str) -> User;
        models.py: class User
    """
    if not workspace_root:
        # Explicitly reject empty paths: a missing workspace would be a
        # containment bypass if silently treated as "/" or CWD.
        return ""
    resolved_root = os.path.realpath(workspace_root)

    parts: list[str] = []
    processed = 0
    for file_path in files_modified:
        if processed >= _MAX_FILES_FOR_API:
            break
        if not os.path.isfile(file_path):
            continue
        # Path containment check: skip files outside workspace
        resolved_file = os.path.realpath(file_path)
        if not resolved_file.startswith(resolved_root + os.sep) and resolved_file != resolved_root:
            continue
        sigs = _extract_public_api(file_path, resolved_root)
        processed += 1
        if sigs:
            basename = os.path.basename(file_path)
            parts.append(f"{basename}: {', '.join(sigs)}")

    summary = "; ".join(parts)
    if len(summary) > _MAX_PUBLIC_API_CHARS:
        summary = summary[: _MAX_PUBLIC_API_CHARS - 3] + "..."
    return summary


@dataclass(frozen=True, slots=True)
class ACContextSummary:
    """Summary of a single AC execution for context passing.

    Attributes:
        ac_index: 0-based AC index.
        ac_content: Original AC description text.
        success: Whether the AC completed successfully.
        tools_used: Unique tool names used during execution.
        files_modified: File paths modified via Write/Edit tools.
        key_output: Truncated final message (last N chars).
        public_api: Extracted public API signatures from modified files.
    """

    ac_index: int
    ac_content: str
    success: bool
    tools_used: tuple[str, ...] = field(default_factory=tuple)
    files_modified: tuple[str, ...] = field(default_factory=tuple)
    key_output: str = ""
    public_api: str = ""


@dataclass(frozen=True, slots=True)
class LevelContext:
    """Context from a completed dependency level.

    Attributes:
        level_number: 0-based execution level index.
        completed_acs: Summaries of ACs in this level.
        coordinator_review: Optional review from the Level Coordinator.
    """

    level_number: int
    completed_acs: tuple[ACContextSummary, ...] = field(default_factory=tuple)
    coordinator_review: CoordinatorReview | None = None

    def to_prompt_text(self) -> str:
        """Format context as prompt text for injection into next level.

        Returns:
            Formatted string describing what previous ACs accomplished.
            Empty string if no successful ACs.
        """
        successful = [ac for ac in self.completed_acs if ac.success]
        if not successful:
            return ""

        lines: list[str] = []
        for summary in successful:
            header = f"- AC {summary.ac_index + 1}: {summary.ac_content[:60]}"
            lines.append(header)
            if summary.files_modified:
                files = ", ".join(summary.files_modified[:5])
                if len(summary.files_modified) > 5:
                    files += f" (+{len(summary.files_modified) - 5} more)"
                lines.append(f"  Files modified: {files}")
            if summary.public_api:
                lines.append(f"  Public API: {summary.public_api}")
            if summary.key_output:
                lines.append(f"  Result: {summary.key_output}")

        text = "\n".join(lines)
        if len(text) > _MAX_LEVEL_CONTEXT_CHARS:
            text = text[: _MAX_LEVEL_CONTEXT_CHARS - 3] + "..."
        return text


def build_context_prompt(level_contexts: list[LevelContext]) -> str:
    """Build a complete context section from multiple levels.

    Args:
        level_contexts: Accumulated contexts from previous levels.

    Returns:
        Formatted prompt section, or empty string if no context.
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

    result = ""
    if sections:
        result = (
            "\n## Previous Work Context\n"
            "The following ACs have already been completed. "
            "Use this context to inform your work.\n\n" + "\n\n".join(sections) + "\n"
        )

    # Append coordinator review warnings if present
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
                result += (
                    f"\n## Coordinator Review (Level {review.level_number})\n"
                    + "\n".join(review_lines)
                    + "\n"
                )

    return result


def extract_level_context(
    ac_results: list[tuple[int, str, bool, tuple[AgentMessage, ...], str]],
    level_num: int,
    *,
    workspace_root: str,
) -> LevelContext:
    """Extract context from completed AC results in a level.

    Args:
        ac_results: List of (ac_index, ac_content, success, messages, final_message)
            tuples from the completed level.
        level_num: Level number for tracking.
        workspace_root: Required workspace directory that bounds all file
            reads performed while building public-API summaries. Callers that
            do not have a session workspace MUST pass an explicit sentinel
            (e.g., ``os.getcwd()`` or a temp dir) — there is no silent bypass.

    Returns:
        LevelContext with summaries of completed work.
    """
    summaries: list[ACContextSummary] = []

    for ac_index, ac_content, success, messages, final_message in ac_results:
        tools_used: set[str] = set()
        files_modified: set[str] = set()

        for msg in messages:
            if msg.tool_name:
                tools_used.add(msg.tool_name)
                # Extract file paths from Write/Edit tool inputs
                if msg.tool_name in ("Write", "Edit", "NotebookEdit"):
                    tool_input = msg.data.get("tool_input", {})
                    file_path = tool_input.get("file_path")
                    if file_path:
                        files_modified.add(file_path)

        key_output = ""
        if final_message:
            key_output = final_message[-_MAX_KEY_OUTPUT_CHARS:].strip()

        sorted_files = tuple(sorted(files_modified))
        public_api = (
            _build_public_api_summary(sorted_files, workspace_root=workspace_root)
            if success
            else ""
        )

        summaries.append(
            ACContextSummary(
                ac_index=ac_index,
                ac_content=ac_content,
                success=success,
                tools_used=tuple(sorted(tools_used)),
                files_modified=sorted_files,
                key_output=key_output,
                public_api=public_api,
            )
        )

    log.info(
        "level_context.extracted",
        level=level_num,
        ac_count=len(summaries),
        successful=sum(1 for s in summaries if s.success),
        total_files=sum(len(s.files_modified) for s in summaries),
    )

    return LevelContext(
        level_number=level_num,
        completed_acs=tuple(summaries),
    )


def serialize_level_contexts(contexts: list[LevelContext]) -> list[dict[str, Any]]:
    """Serialize level contexts for checkpoint storage.

    Uses dataclasses.asdict() for complete, field-addition-safe serialization.
    All nested types (ACContextSummary, CoordinatorReview, FileConflict) are
    frozen dataclasses composed of primitives and tuples, so asdict() produces
    a fully JSON-serializable dict tree (tuples become lists).
    """
    return [asdict(ctx) for ctx in contexts]


def deserialize_level_contexts(data: list[dict[str, Any]]) -> list[LevelContext]:
    """Deserialize level contexts from checkpoint data.

    Reconstructs the typed dataclass tree, converting lists back to tuples
    where the frozen dataclasses expect them. Tolerates missing/extra fields
    from older/newer checkpoint schemas by using explicit field extraction
    with defaults rather than dict-splatting.
    """
    from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict

    result: list[LevelContext] = []
    for d in data:
        review = None
        if d.get("coordinator_review"):
            rd = d["coordinator_review"]
            try:
                conflicts = tuple(
                    FileConflict(
                        file_path=fc.get("file_path", ""),
                        ac_indices=tuple(fc.get("ac_indices", ())),
                        resolved=fc.get("resolved", False),
                        resolution_description=fc.get("resolution_description", ""),
                    )
                    for fc in rd.get("conflicts_detected", ())
                )
                review = CoordinatorReview(
                    level_number=rd.get("level_number", 0),
                    conflicts_detected=conflicts,
                    review_summary=rd.get("review_summary", ""),
                    fixes_applied=tuple(rd.get("fixes_applied", ())),
                    warnings_for_next_level=tuple(rd.get("warnings_for_next_level", ())),
                    duration_seconds=rd.get("duration_seconds", 0.0),
                    session_id=rd.get("session_id"),
                )
            except Exception as e:
                log.warning(
                    "level_context.deserialize.review_skipped",
                    error=str(e),
                )
                review = None

        completed_acs: list[ACContextSummary] = []
        for ac in d.get("completed_acs", ()):
            try:
                completed_acs.append(
                    ACContextSummary(
                        ac_index=ac.get("ac_index", 0),
                        ac_content=ac.get("ac_content", ""),
                        success=ac.get("success", False),
                        tools_used=tuple(ac.get("tools_used", ())),
                        files_modified=tuple(ac.get("files_modified", ())),
                        key_output=ac.get("key_output", ""),
                        public_api=ac.get("public_api", ""),
                    )
                )
            except Exception as e:
                log.warning(
                    "level_context.deserialize.ac_skipped",
                    error=str(e),
                )

        result.append(
            LevelContext(
                level_number=d.get("level_number", 0),
                completed_acs=tuple(completed_acs),
                coordinator_review=review,
            )
        )
    return result


__all__ = [
    "ACContextSummary",
    "LevelContext",
    "build_context_prompt",
    "deserialize_level_contexts",
    "extract_level_context",
    "serialize_level_contexts",
]
