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
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

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

# --- Invariant tag extraction (Q3 / C-plus) ---
# Canonical regex for [[INVARIANT: <text>]] tags.  Double-bracket delimiter is
# intentionally strict so any accidental single-bracket text is ignored.
_INVARIANT_TAG_RE = re.compile(r"\[\[INVARIANT:\s*([^\]]+)\]\]", re.IGNORECASE)
# Maximum characters per extracted invariant text (matches Q3 spec: ~200 chars).
_MAX_INVARIANT_TEXT_CHARS = 200

# --- Postmortem primitives (serial compounding execution) ---
# Default number of most-recent postmortems rendered in full form.
POSTMORTEM_DEFAULT_K_FULL = 3
# Default token budget for the rendered postmortem chain section.
POSTMORTEM_DEFAULT_TOKEN_BUDGET = 8000
# Rough chars-per-token heuristic used for budget estimation.
_POSTMORTEM_CHARS_PER_TOKEN = 4
# Max chars of AC content shown in a digest line.
_POSTMORTEM_DIGEST_CONTENT_CHARS = 60
# Max chars retained in diff_summary (guards against huge git diff --stat output).
_POSTMORTEM_MAX_DIFF_CHARS = 2000
# Max chars retained in tool_trace_digest (guards against pathological traces).
_POSTMORTEM_MAX_TRACE_CHARS = 1500
# Default minimum reliability score for invariants to appear in the prompt chain.
# Below-threshold invariants are captured and stored in the serialized chain
# but are hidden from downstream ACs' prompt context.
# Override with OUROBOROS_INVARIANT_MIN_RELIABILITY env var.
POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY = 0.7

PostmortemStatus = Literal["pass", "fail", "partial"]


def _ensure_tuple_or_none(value: Any) -> tuple[Any, ...]:
    """Convert a value to a tuple, handling strings specially to avoid char-tuples.

    Args:
        value: The value to convert to a tuple.

    Returns:
        - Empty tuple if value is None
        - tuple(value) if value is already a list or tuple
        - (value,) if value is a string (wraps the whole string)
        - (value,) otherwise
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, str):
        return (value,)
    return (value,)


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
                        ac_indices=_ensure_tuple_or_none(fc.get("ac_indices", ())),
                        resolved=fc.get("resolved", False),
                        resolution_description=fc.get("resolution_description", ""),
                    )
                    for fc in rd.get("conflicts_detected", ())
                )
                review = CoordinatorReview(
                    level_number=rd.get("level_number", 0),
                    conflicts_detected=conflicts,
                    review_summary=rd.get("review_summary", ""),
                    fixes_applied=_ensure_tuple_or_none(rd.get("fixes_applied", ())),
                    warnings_for_next_level=_ensure_tuple_or_none(rd.get("warnings_for_next_level", ())),
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
                        tools_used=_ensure_tuple_or_none(ac.get("tools_used", ())),
                        files_modified=_ensure_tuple_or_none(ac.get("files_modified", ())),
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


# --- Serial compounding: per-AC postmortem artifact and rolling chain ---


@dataclass(frozen=True, slots=True)
class Invariant:
    """A fact established by an AC that compounds into future ACs.

    Attributes:
        text: The invariant claim (≤200 chars).
        reliability: Score 0.0–1.0 from the Haiku verifier (default 1.0 until
            the verifier runs; set to 1.0 for manually-emitted tags that have
            not yet been verified).
        occurrences: How many times this invariant has been (re-)declared
            across ACs. Bumped on each re-declaration; used to rank invariants.
        first_seen_ac_id: The AC id string where this invariant was first
            established. Empty string when the source AC has no id.
        is_contradicted: True when a literal NOT-prefix contradiction was
            detected during ``merge_invariants``.  A contradicted invariant
            gets ``reliability=0.0`` and is excluded from trusted invariant
            summaries.  See :meth:`PostmortemChain.merge_invariants`.
    """

    text: str
    reliability: float = 1.0
    occurrences: int = 1
    first_seen_ac_id: str = ""
    is_contradicted: bool = False

    def __str__(self) -> str:  # noqa: D105
        return self.text


def _deserialize_invariant(item: Any) -> "Invariant":
    """Reconstruct an Invariant from a serialized form.

    Handles two formats for backward compatibility:
    - Legacy string: ``"AUTH_HEADER required"`` → ``Invariant(text=...)``
    - New dict: ``{"text": "...", "reliability": 0.9, ...}``
    """
    if isinstance(item, str):
        return Invariant(text=item)
    if isinstance(item, dict):
        return Invariant(
            text=item.get("text", ""),
            reliability=float(item.get("reliability", 1.0)),
            occurrences=int(item.get("occurrences", 1)),
            first_seen_ac_id=item.get("first_seen_ac_id", ""),
            is_contradicted=bool(item.get("is_contradicted", False)),
        )
    # Fallback: coerce unknown types to string
    return Invariant(text=str(item))


def _contradiction_counterpart_key(key: str) -> str | None:
    """Return the normalized key that would contradict ``key``, or None.

    A contradiction pair consists of a claim and its literal NOT-prefix
    negation (case-insensitive, whitespace-collapsed):

    - ``"x holds"`` ↔ ``"not x holds"``
    - ``"not auth required"`` ↔ ``"auth required"``

    Only the "NOT " (four-character) prefix is recognized as the negation
    marker.  Returns the counterpart key string if the pair exists; returns
    ``None`` when no counterpart can be formed.

    [[INVARIANT: NOT-prefix contradiction uses four-char "not " sentinel only]]
    """
    _NOT_PREFIX = "not "
    if key.startswith(_NOT_PREFIX):
        base = key[len(_NOT_PREFIX):]
        return base if base else None
    return _NOT_PREFIX + key


@dataclass(frozen=True, slots=True)
class ACPostmortem:
    """Per-AC postmortem carried forward as compounding context.

    Composition over inheritance: wraps an ``ACContextSummary`` (the facts
    extractable from tool events) and adds fields specific to compounding
    execution (diff, gotchas, QA suggestions, invariants, retry count,
    status, duration, native session id).

    Keeping this distinct from ``ACContextSummary`` means the parallel-mode
    prompt and checkpoint paths are byte-identical when serial mode is not
    in use — only the serial executor constructs these.
    """

    summary: ACContextSummary
    diff_summary: str = ""
    tool_trace_digest: str = ""
    gotchas: tuple[str, ...] = field(default_factory=tuple)
    qa_suggestions: tuple[str, ...] = field(default_factory=tuple)
    invariants_established: tuple[Invariant, ...] = field(default_factory=tuple)
    retry_attempts: int = 0
    status: PostmortemStatus = "pass"
    duration_seconds: float = 0.0
    ac_native_session_id: str | None = None
    sub_postmortems: tuple["ACPostmortem", ...] = field(default_factory=tuple)

    def to_digest(
        self,
        *,
        min_reliability: float = 0.0,
        contradicted_keys: "frozenset[str] | None" = None,
    ) -> str:
        """Render a one-line digest for compressed display in the chain.

        Format: ``AC {n} [{status}]: {content} — files: a,b (+K more) | invariants: X, Y``
        For non-passing ACs, the first gotcha is appended instead of invariants
        when present, since gotchas are the anti-repeat signal.

        Args:
            min_reliability: Reliability gate applied to invariants in the
                digest line.  Contradicted invariants
                (``is_contradicted=True``) are always excluded.  Defaults to
                0.0 (no filtering) when called standalone; the chain caller
                passes the chain-level threshold for consistency with
                :meth:`to_full_text` and :meth:`PostmortemChain.to_prompt_text`.
            contradicted_keys: Optional set of normalized invariant keys
                contradicted *somewhere in the enclosing chain*.  Per-AC
                ``invariants_established`` only knows about contradiction
                state at the AC's own creation time; passing this set lets
                the chain renderer hide invariants that were later
                contradicted by a downstream AC.
        """
        s = self.summary
        content = s.ac_content[:_POSTMORTEM_DIGEST_CONTENT_CHARS].rstrip()
        if len(s.ac_content) > _POSTMORTEM_DIGEST_CONTENT_CHARS:
            content += "..."
        parts = [f"AC {s.ac_index + 1} [{self.status}]: {content}"]

        if s.files_modified:
            head = ", ".join(s.files_modified[:3])
            extra = len(s.files_modified) - 3
            files = head + (f" (+{extra} more)" if extra > 0 else "")
            parts.append(f"files: {files}")

        if self.status != "pass" and self.gotchas:
            parts.append(f"gotcha: {self.gotchas[0]}")
        else:
            # Apply the same reliability gate as to_full_text/to_prompt_text.
            # Older entries render via to_digest, so without this filter
            # below-threshold or contradicted invariants would leak back into
            # the chain context — defeating the render gate.
            _contradicted = contradicted_keys or frozenset()
            visible_invs = tuple(
                inv
                for inv in self.invariants_established
                if (
                    not inv.is_contradicted
                    and inv.reliability >= min_reliability
                    and " ".join(inv.text.lower().split()) not in _contradicted
                )
            )
            if visible_invs:
                inv_head = "; ".join(inv.text for inv in visible_invs[:2])
                extra_inv = len(visible_invs) - 2
                inv_str = inv_head + (f" (+{extra_inv} more)" if extra_inv > 0 else "")
                parts.append(f"invariants: {inv_str}")

        return " | ".join(parts)

    def to_full_text(
        self,
        *,
        min_reliability: float = 0.0,
        contradicted_keys: "frozenset[str] | None" = None,
    ) -> str:
        """Render the full postmortem for the in-prompt 'recent' window.

        Args:
            min_reliability: Reliability gate applied to per-AC invariants in
                the "Invariants established" sub-section.  Contradicted
                invariants (``is_contradicted=True``) are always excluded.
                Defaults to 0.0 (no filtering) when called standalone; callers
                should pass the chain-level threshold for consistent behaviour.
            contradicted_keys: Optional set of normalized invariant keys
                contradicted *somewhere in the enclosing chain*.  An entry's
                ``is_contradicted`` flag only reflects state at the AC's
                creation time; this set lets the chain renderer also suppress
                invariants that were contradicted by a *later* AC, so the
                trusted claim is hidden everywhere in the rendered output.
        """
        s = self.summary
        lines: list[str] = [
            f"### AC {s.ac_index + 1} [{self.status}]"
            f"{' (retried ' + str(self.retry_attempts) + 'x)' if self.retry_attempts else ''}"
            f"{' (' + f'{self.duration_seconds:.1f}' + 's)' if self.duration_seconds else ''}",
            f"**Task:** {s.ac_content}",
        ]
        if s.files_modified:
            files = ", ".join(s.files_modified[:10])
            if len(s.files_modified) > 10:
                files += f" (+{len(s.files_modified) - 10} more)"
            lines.append(f"**Files modified:** {files}")
        if s.tools_used:
            lines.append(f"**Tools used:** {', '.join(s.tools_used)}")
        if self.diff_summary:
            lines.append("**Diff summary:**")
            lines.append("```")
            lines.append(self.diff_summary[:_POSTMORTEM_MAX_DIFF_CHARS])
            lines.append("```")
        if self.tool_trace_digest:
            lines.append(
                f"**Tool trace:** {self.tool_trace_digest[:_POSTMORTEM_MAX_TRACE_CHARS]}"
            )
        # Apply reliability gate to per-AC invariants in the full-form render.
        # Also drop invariants whose normalized key is in the chain-level
        # contradicted set so a downstream contradiction hides the (then-trusted)
        # original claim everywhere — not just in the cumulative section.
        _contradicted = contradicted_keys or frozenset()
        visible_invs = tuple(
            inv
            for inv in self.invariants_established
            if (
                not inv.is_contradicted
                and inv.reliability >= min_reliability
                and " ".join(inv.text.lower().split()) not in _contradicted
            )
        )
        if visible_invs:
            lines.append("**Invariants established:**")
            lines.extend(f"- {inv.text}" for inv in visible_invs)
        if self.gotchas:
            lines.append("**Gotchas:**")
            lines.extend(f"- {g}" for g in self.gotchas)
        if self.qa_suggestions:
            lines.append("**QA suggestions:**")
            lines.extend(f"- {q}" for q in self.qa_suggestions)
        if s.public_api:
            lines.append(f"**Public API:** {s.public_api}")
        if s.key_output:
            lines.append(f"**Result:** {s.key_output}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PostmortemChain:
    """Rolling chain of per-AC postmortems for compounding execution.

    Rendering policy: the most recent ``k_full`` entries render in full; older
    entries render as one-line digests. An always-kept "Established Invariants"
    block accumulates every AC's invariants (deduplicated, insertion order).
    Under ``token_budget`` pressure, oldest digest lines are dropped first;
    full forms and the invariants block are preserved.
    """

    postmortems: tuple[ACPostmortem, ...] = field(default_factory=tuple)

    def append(self, postmortem: ACPostmortem) -> "PostmortemChain":
        """Return a new chain with ``postmortem`` appended (immutable)."""
        return PostmortemChain(postmortems=self.postmortems + (postmortem,))

    def cumulative_invariants(self) -> tuple[Invariant, ...]:
        """Deduplicated invariants across all ACs, in insertion order.

        Deduplication is by normalized text (lower-cased, collapsed whitespace).
        The **last** occurrence of each key is used so that
        :meth:`merge_invariants` results — which carry up-to-date occurrence
        counts and blended reliability scores — take precedence over earlier,
        lower-count versions.

        Insertion order is preserved: the first time a key is seen determines
        its position in the returned tuple; subsequent re-declarations only
        update the stored :class:`Invariant` object.
        """
        order: list[str] = []
        seen: dict[str, Invariant] = {}
        for pm in self.postmortems:
            for inv in pm.invariants_established:
                key = " ".join(inv.text.lower().split())
                if key not in seen:
                    order.append(key)
                seen[key] = inv  # last wins — most recent count / reliability
        return tuple(seen[k] for k in order)

    def merge_invariants(
        self,
        new: "list[tuple[str, float]]",
        source_ac_id: str,
    ) -> "tuple[Invariant, ...]":
        """Merge new invariant claims into the running chain's cumulative set.

        Called after each AC completes to produce the ``invariants_established``
        tuple for the new :class:`ACPostmortem` being appended to the chain.

        Algorithm (per invariant in ``new``):

        1. **Normalize** the text: lower-case + collapsed whitespace for lookup.
        2. **Contradiction check** (NOT-prefix): if the normalized key and an
           existing key are literal NOT-prefix negations of each other (e.g.
           ``"auth required"`` vs ``"not auth required"``), the new invariant
           is marked ``is_contradicted=True`` and receives ``reliability=0.0``.
           A warning is logged.  The contradicted invariant is still included in
           the returned tuple so it is visible in the chain for inspection.
        3. **Match** against the chain's cumulative invariants (same key).
        4. **Re-declaration** (match found): bump ``occurrences`` by 1, blend the
           reliability score as a weighted mean::

               (prior.reliability × prior.occurrences + new_reliability) / new_occurrences

           The *canonical text* is preserved from the first-seen invariant so
           the rendered output stays stable across re-declarations with minor
           wording variations.
        5. **New** (no match, no contradiction): insert as
           ``Invariant(text, reliability, 1, source_ac_id)``.

        Deduplication within ``new`` itself (same normalized key appearing
        twice in one AC) keeps only the first occurrence.

        Args:
            new: Pairs of ``(text, reliability_score)`` produced by the Haiku
                verifier (Q3 / C-plus).  Pass an empty list when no invariants
                were extracted or the verifier returned nothing.
            source_ac_id: AC id string used as ``first_seen_ac_id`` for
                genuinely new invariants.  Pass an empty string when the
                calling AC has no id.

        Returns:
            A tuple of :class:`Invariant` objects, one per unique entry in
            ``new``, in the order they appeared.  Re-declared invariants carry
            updated occurrence counts and blended reliability scores; new ones
            carry ``occurrences=1`` and ``first_seen_ac_id=source_ac_id``.
            Contradicted invariants carry ``is_contradicted=True`` and
            ``reliability=0.0``.

        [[INVARIANT: PostmortemChain.merge_invariants bumps occurrences and averages reliability on re-declaration]]
        [[INVARIANT: first_seen_ac_id is set on new invariants and preserved on re-declarations]]
        [[INVARIANT: NOT-prefix contradictions set is_contradicted=True and reliability=0.0 on the new invariant]]
        """
        # Build lookup from accumulated history; last occurrence wins so that
        # previously-merged invariants (with bumped counts) serve as the base.
        existing: dict[str, Invariant] = {}
        for pm in self.postmortems:
            for inv in pm.invariants_established:
                key = " ".join(inv.text.lower().split())
                existing[key] = inv

        result: list[Invariant] = []
        seen_keys: set[str] = set()

        for text, reliability in new:
            # Apply 200-char cap — mirrors extract_invariant_tags behaviour.
            text = text[:_MAX_INVARIANT_TEXT_CHARS]
            key = " ".join(text.lower().split())
            if not key:
                continue
            if key in seen_keys:
                # Duplicate within this AC's ``new`` list — skip.
                continue
            seen_keys.add(key)

            # --- NOT-prefix contradiction detection (AC-2 / B-prime extension) ---
            # Check if the new claim contradicts an already-established invariant.
            # A contradiction exists when the new key and an existing key are
            # literal NOT-prefix negations of each other (e.g. "auth required"
            # vs "not auth required").  Contradicted invariants are marked with
            # is_contradicted=True and receive reliability=0.0 to signal that
            # the pair cannot both be trusted.
            counterpart_key = _contradiction_counterpart_key(key)
            is_contradicted = counterpart_key is not None and counterpart_key in existing

            if is_contradicted:
                log.warning(
                    "invariant.contradiction_detected",
                    new_key=key,
                    counterpart_key=counterpart_key,
                    source_ac_id=source_ac_id,
                )
                result.append(
                    Invariant(
                        text=text,
                        reliability=0.0,
                        occurrences=1,
                        first_seen_ac_id=source_ac_id,
                        is_contradicted=True,
                    )
                )
                # Symmetric contradiction marking: the counterpart living on a
                # prior postmortem cannot be mutated (frozen dataclass), so we
                # emit a fresh contradicted Invariant under the COUNTERPART's
                # key on this batch's result.  ``cumulative_invariants`` walks
                # the chain with last-wins-by-key semantics, so the older
                # trusted entry is overridden by this contradicted one and
                # filtered at the render gate alongside its negation.  Without
                # this, the prior claim continues to surface in downstream
                # ACs' compounding context even though it is now provably
                # contradicted.
                if counterpart_key is not None and counterpart_key not in seen_keys:
                    prior_counterpart = existing[counterpart_key]
                    result.append(
                        Invariant(
                            text=prior_counterpart.text,
                            reliability=0.0,
                            occurrences=prior_counterpart.occurrences,
                            first_seen_ac_id=prior_counterpart.first_seen_ac_id,
                            is_contradicted=True,
                        )
                    )
                    seen_keys.add(counterpart_key)
            elif key in existing:
                prior = existing[key]
                new_occ = prior.occurrences + 1
                # Weighted mean: weight by prior occurrence count.
                blended = (prior.reliability * prior.occurrences + reliability) / new_occ
                result.append(
                    Invariant(
                        text=prior.text,  # canonical text from first occurrence
                        reliability=round(blended, 6),
                        occurrences=new_occ,
                        first_seen_ac_id=prior.first_seen_ac_id,
                    )
                )
            else:
                result.append(
                    Invariant(
                        text=text,
                        reliability=reliability,
                        occurrences=1,
                        first_seen_ac_id=source_ac_id,
                    )
                )

        return tuple(result)

    def to_prompt_text(
        self,
        *,
        k_full: int = POSTMORTEM_DEFAULT_K_FULL,
        token_budget: int = POSTMORTEM_DEFAULT_TOKEN_BUDGET,
        min_reliability: float | None = None,
        on_truncated: Callable[[int, int, int, int, int], None] | None = None,
    ) -> str:
        """Render the chain as a markdown section for user-turn injection.

        Args:
            k_full: Number of most-recent postmortems to render in full form.
                Remaining older postmortems render as one-line digests.
            token_budget: Approximate token budget for the section. When the
                rendered text exceeds ``token_budget * 4`` characters, oldest
                digest lines are progressively dropped. Full forms and the
                invariants block are always preserved.
            min_reliability: Reliability gate for the "Established Invariants"
                block. Only invariants whose ``reliability`` score is at or
                above this threshold are rendered; contradicted invariants
                (``is_contradicted=True``) are always excluded regardless of
                score. When ``None`` (default), the value is resolved from
                the ``OUROBOROS_INVARIANT_MIN_RELIABILITY`` env var, falling
                back to :data:`POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY`
                (0.7). Pass ``0.0`` explicitly to show all non-contradicted
                invariants.
            on_truncated: Optional callback invoked when the rendered text
                still exceeds the character budget after dropping all digest
                entries.  Called with positional args:
                ``(dropped_count, char_budget, rendered_chars,
                full_forms_preserved, cumulative_invariants_preserved)``.
                Used by :func:`build_postmortem_chain_prompt` to emit
                the Q7 ``"execution.postmortem_chain.truncated"`` event.

        Returns:
            The formatted section, or an empty string if the chain is empty.

        [[INVARIANT: to_prompt_text render gate uses OUROBOROS_INVARIANT_MIN_RELIABILITY env var (default 0.7)]]
        [[INVARIANT: contradicted invariants are always excluded from to_prompt_text regardless of min_reliability]]
        """
        if not self.postmortems:
            return ""

        # --- Resolve min_reliability threshold (arg > env var > built-in default) ---
        if min_reliability is None:
            raw = os.environ.get("OUROBOROS_INVARIANT_MIN_RELIABILITY", "").strip()
            try:
                min_reliability = (
                    float(raw) if raw else POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY
                )
            except ValueError:
                log.warning(
                    "postmortem_chain.invalid_min_reliability_env",
                    raw_value=raw,
                    fallback=POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY,
                )
                min_reliability = POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY

        char_budget = max(0, token_budget) * _POSTMORTEM_CHARS_PER_TOKEN

        # Full form: last k_full entries. Digests: everything older.
        if k_full <= 0:
            full_entries: tuple[ACPostmortem, ...] = ()
            digest_entries: tuple[ACPostmortem, ...] = self.postmortems
        else:
            split = max(0, len(self.postmortems) - k_full)
            digest_entries = self.postmortems[:split]
            full_entries = self.postmortems[split:]

        all_invariants = self.cumulative_invariants()
        # Apply render gate: filter contradicted invariants and those below the
        # reliability threshold.  Contradicted invariants (reliability=0.0) are
        # always excluded; the threshold check also catches them but the explicit
        # ``is_contradicted`` guard makes intent clear.
        invariants = tuple(
            inv
            for inv in all_invariants
            if not inv.is_contradicted and inv.reliability >= min_reliability
        )
        hidden_count = len(all_invariants) - len(invariants)
        # Build the chain-level contradicted-keys set so per-AC renderers can
        # also hide invariants contradicted by a downstream AC. Without this
        # set, AC-N's ``to_full_text`` would still render a claim that AC-N+M
        # later proved false (since the per-AC ``is_contradicted`` is fixed at
        # the AC's creation time).
        contradicted_keys: frozenset[str] = frozenset(
            " ".join(inv.text.lower().split())
            for inv in all_invariants
            if inv.is_contradicted
        )
        if hidden_count > 0:
            log.debug(
                "postmortem_chain.invariants.render_gate_filtered",
                hidden_count=hidden_count,
                total_count=len(all_invariants),
                min_reliability=min_reliability,
            )

        def _render(digests: tuple[ACPostmortem, ...]) -> str:
            sections: list[str] = ["## Prior AC Postmortems (Compounding Context)"]
            if invariants:
                sections.append("### Established Invariants (cumulative)")
                sections.extend(f"- {inv.text}" for inv in invariants)
            if digests:
                sections.append("### Earlier ACs (digests)")
                sections.extend(
                    f"- {pm.to_digest(min_reliability=min_reliability, contradicted_keys=contradicted_keys)}"
                    for pm in digests
                )
            if full_entries:
                sections.append("### Recent ACs (full postmortems)")
                # Pass min_reliability AND contradicted_keys so individual
                # full-form sections apply the same gate as the cumulative
                # invariants block above (including chain-level contradictions
                # introduced after the AC's creation).
                sections.extend(
                    pm.to_full_text(
                        min_reliability=min_reliability,
                        contradicted_keys=contradicted_keys,
                    )
                    for pm in full_entries
                )
            return "\n".join(sections)

        text = _render(digest_entries)
        if char_budget <= 0 or len(text) <= char_budget:
            return text

        # Over budget: drop oldest digests progressively until we fit or run out.
        remaining = list(digest_entries)
        while remaining and len(text) > char_budget:
            remaining.pop(0)
            text = _render(tuple(remaining))

        # Compute truncation outside the over-budget branch: when the loop
        # successfully shrunk the chain below budget by dropping entries, the
        # caller still needs to know — otherwise telemetry only fires on the
        # rare case where truncation FAILS to fit, which inverts the intent.
        dropped_count = len(digest_entries) - len(remaining)
        if dropped_count > 0:
            log.warning(
                "postmortem_chain.over_budget",
                rendered_chars=len(text),
                char_budget=char_budget,
                dropped_count=dropped_count,
                full_count=len(full_entries),
                invariants_count=len(invariants),
            )
            # Q7: Notify caller so a structured event can be emitted alongside
            # this log line.  Callback is intentionally synchronous — callers
            # that need async event emission collect the info and emit afterward.
            if on_truncated is not None:
                on_truncated(
                    dropped_count,
                    char_budget,
                    len(text),
                    len(full_entries),
                    len(invariants),
                )
        return text


def build_postmortem_chain_prompt(
    chain: PostmortemChain,
    *,
    k_full: int | None = None,
    token_budget: int | None = None,
    on_truncated: Callable[[int, int, int, int, int], None] | None = None,
) -> str:
    """Build the "Prior AC Postmortems" section for the user-turn prompt.

    Thin wrapper around :meth:`PostmortemChain.to_prompt_text` that honors the
    ``OUROBOROS_POSTMORTEM_FULL_K`` and ``OUROBOROS_POSTMORTEM_TOKEN_BUDGET``
    env overrides when arguments are not supplied. Returns an empty string for
    an empty chain so callers can concatenate unconditionally.

    Args:
        chain: Postmortem chain to render.
        k_full: Override for number of most-recent full-form entries.
            Defaults to ``OUROBOROS_POSTMORTEM_FULL_K`` env var or
            :data:`POSTMORTEM_DEFAULT_K_FULL`.
        token_budget: Override for token budget. Defaults to
            ``OUROBOROS_POSTMORTEM_TOKEN_BUDGET`` env var or
            :data:`POSTMORTEM_DEFAULT_TOKEN_BUDGET`.
        on_truncated: Optional callback forwarded to
            :meth:`PostmortemChain.to_prompt_text` for Q7 truncation event
            emission. See that method's docstring for callback signature.
    """
    if k_full is None:
        env_k = os.environ.get("OUROBOROS_POSTMORTEM_FULL_K")
        try:
            k_full = int(env_k) if env_k is not None else POSTMORTEM_DEFAULT_K_FULL
        except ValueError:
            k_full = POSTMORTEM_DEFAULT_K_FULL
    if token_budget is None:
        env_b = os.environ.get("OUROBOROS_POSTMORTEM_TOKEN_BUDGET")
        try:
            token_budget = (
                int(env_b) if env_b is not None else POSTMORTEM_DEFAULT_TOKEN_BUDGET
            )
        except ValueError:
            token_budget = POSTMORTEM_DEFAULT_TOKEN_BUDGET
    return chain.to_prompt_text(
        k_full=k_full,
        token_budget=token_budget,
        on_truncated=on_truncated,
    )


def serialize_postmortem_chain(chain: PostmortemChain) -> list[dict[str, Any]]:
    """Serialize a postmortem chain for checkpoint / event storage.

    Uses ``dataclasses.asdict`` for the full nested tree; tuples become lists
    via standard asdict behavior. Safe to JSON-encode.
    """
    return [asdict(pm) for pm in chain.postmortems]


def _deserialize_postmortem(d: dict[str, Any]) -> ACPostmortem:
    """Reconstruct an ACPostmortem from a dict, tolerating missing fields."""
    summary_dict = d.get("summary") or {}
    summary = ACContextSummary(
        ac_index=summary_dict.get("ac_index", 0),
        ac_content=summary_dict.get("ac_content", ""),
        success=summary_dict.get("success", False),
        tools_used=_ensure_tuple_or_none(summary_dict.get("tools_used", ())),
        files_modified=_ensure_tuple_or_none(summary_dict.get("files_modified", ())),
        key_output=summary_dict.get("key_output", ""),
        public_api=summary_dict.get("public_api", ""),
    )
    status_value = d.get("status", "pass")
    if status_value not in ("pass", "fail", "partial"):
        status_value = "pass"
    sub_raw = d.get("sub_postmortems") or ()
    sub: list[ACPostmortem] = []
    for entry in sub_raw:
        if isinstance(entry, dict):
            try:
                sub.append(_deserialize_postmortem(entry))
            except Exception as e:
                log.warning("postmortem.deserialize.sub_skipped", error=str(e))
    return ACPostmortem(
        summary=summary,
        diff_summary=d.get("diff_summary", ""),
        tool_trace_digest=d.get("tool_trace_digest", ""),
        gotchas=_ensure_tuple_or_none(d.get("gotchas", ())),
        qa_suggestions=_ensure_tuple_or_none(d.get("qa_suggestions", ())),
        invariants_established=tuple(
            _deserialize_invariant(item)
            for item in (d.get("invariants_established") or ())
        ),
        retry_attempts=d.get("retry_attempts", 0),
        status=status_value,  # type: ignore[arg-type]
        duration_seconds=d.get("duration_seconds", 0.0),
        ac_native_session_id=d.get("ac_native_session_id"),
        sub_postmortems=tuple(sub),
    )


def deserialize_postmortem_chain(data: list[dict[str, Any]]) -> PostmortemChain:
    """Reconstruct a postmortem chain from serialized form.

    Tolerant of missing/extra fields for forward/backward schema compatibility,
    mirroring the pattern in :func:`deserialize_level_contexts`.
    """
    postmortems: list[ACPostmortem] = []
    for d in data or ():
        try:
            postmortems.append(_deserialize_postmortem(d))
        except Exception as e:
            log.warning("postmortem_chain.deserialize.entry_skipped", error=str(e))
    return PostmortemChain(postmortems=tuple(postmortems))


def extract_invariant_tags(messages: "Sequence[AgentMessage] | str") -> list[str]:
    """Extract ``[[INVARIANT: ...]]`` tags from agent messages or a plain string.

    Implements the Q3 (C-plus) tag-parsing step.  Tags are parsed with the
    canonical regex ``\\[\\[INVARIANT:\\s*([^\\]]+)\\]\\]`` (case-insensitive),
    whitespace-stripped, and capped at :data:`_MAX_INVARIANT_TEXT_CHARS` (200)
    characters.  Duplicates are removed in insertion order (normalization:
    lower-case + collapsed whitespace).

    Args:
        messages: Either a sequence of :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            objects — the ``content`` field of each is scanned — or a plain
            string scanned directly.  Pass ``result.final_message`` to scan
            only the last assistant turn; pass ``result.messages`` to scan
            the full conversation.

    Returns:
        Deduplicated list of invariant text strings in the order first seen.
        Returns an empty list when no valid tags are found.

    Examples::

        >>> extract_invariant_tags("Done. [[INVARIANT: X is always true]]")
        ['X is always true']
        >>> extract_invariant_tags("[[INVARIANT: A]] [[INVARIANT: A]]")
        ['A']
    """
    if isinstance(messages, str):
        text = messages
    else:
        text = "\n".join(m.content for m in messages)

    results: list[str] = []
    seen: set[str] = set()
    for match in _INVARIANT_TAG_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        # Apply 200-char cap *before* deduplication so callers see the capped form.
        raw = raw[:_MAX_INVARIANT_TEXT_CHARS]
        # Normalize for deduplication: lower-case + collapsed whitespace.
        key = " ".join(raw.lower().split())
        if key not in seen:
            seen.add(key)
            results.append(raw)
    return results


__all__ = [
    "ACContextSummary",
    "ACPostmortem",
    "Invariant",
    "LevelContext",
    "POSTMORTEM_DEFAULT_K_FULL",
    "POSTMORTEM_DEFAULT_TOKEN_BUDGET",
    "PostmortemChain",
    "PostmortemStatus",
    "build_context_prompt",
    "build_postmortem_chain_prompt",
    "deserialize_level_contexts",
    "deserialize_postmortem_chain",
    "extract_invariant_tags",
    "extract_level_context",
    "serialize_level_contexts",
    "serialize_postmortem_chain",
]