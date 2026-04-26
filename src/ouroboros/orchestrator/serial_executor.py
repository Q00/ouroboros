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

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.diff_capture import (
    capture_pre_ac_snapshot,
    compute_diff_summary,
)
from ouroboros.orchestrator.events import (
    create_ac_postmortem_captured_event,
    create_monolithic_resume_adjudicated_event,
    create_postmortem_chain_truncated_event,
    create_sub_postmortem_resume_event,
)
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    ACPostmortem,
    Invariant,
    PostmortemChain,
    PostmortemStatus,
    build_postmortem_chain_prompt,
    deserialize_postmortem_chain,
    extract_invariant_tags,
    extract_level_context,
    serialize_postmortem_chain,
)
from ouroboros.persistence.checkpoint import (
    CheckpointData,
    CompoundingCheckpointState,
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

# Default directory for chain artifact output. Override with OUROBOROS_CHAIN_ARTIFACT_DIR.
_DEFAULT_CHAIN_ARTIFACT_DIR = "docs/brainstorm"

# Q3 (C-plus): Invariant reliability gate defaults.
# OUROBOROS_INVARIANT_MIN_RELIABILITY — minimum score for an invariant to be stored.
_DEFAULT_INVARIANT_MIN_RELIABILITY = 0.7
# Regex to extract a float score from a Haiku response (first 0.0-1.0 match).
_HAIKU_SCORE_RE = re.compile(r"\b(1\.0+|0\.\d+)\b")
# Fallback reliability when the verifier response cannot be parsed.
_HAIKU_SCORE_FALLBACK = 0.5


def _get_min_reliability() -> float:
    """Return the minimum reliability threshold for invariant inclusion.

    Reads ``OUROBOROS_INVARIANT_MIN_RELIABILITY`` env var; defaults to 0.7.
    Invalid values fall back to the default silently.
    """
    raw = os.environ.get("OUROBOROS_INVARIANT_MIN_RELIABILITY", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_INVARIANT_MIN_RELIABILITY


async def _verify_single_tag(
    adapter: Any,
    tag: str,
    *,
    ac_trace: str,
    files_modified: list[str],
    model: str,
) -> float:
    """Ask the Haiku verifier to score one [[INVARIANT]] tag.

    Sends a short prompt asking the model to return a reliability score
    0.0–1.0. Parses the first float in the response. On any error
    (API failure, unparseable response) returns :data:`_HAIKU_SCORE_FALLBACK`.

    Args:
        adapter: LLM adapter with a :meth:`complete` method.
        tag: Invariant text to verify.
        ac_trace: Final message from the AC (work summary / trace).
        files_modified: Files changed during the AC.
        model: Model identifier to use for the call.

    Returns:
        Reliability score in [0.0, 1.0].
    """
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    files_str = ", ".join(files_modified) if files_modified else "(none)"
    trace_preview = (ac_trace or "")[:800]

    user_content = (
        "You are a fact-checking assistant for a software development workflow.\n\n"
        "An AI agent declared the following invariant after completing a task:\n\n"
        f'  Invariant: "{tag}"\n\n'
        "Context:\n"
        f"  Files modified: {files_str}\n"
        f"  Agent trace / final output:\n  {trace_preview}\n\n"
        "Is this invariant actually supported by the evidence above?\n"
        "Reply with ONLY a single number between 0.0 and 1.0, where:\n"
        "  1.0 = definitely supported\n"
        "  0.5 = uncertain\n"
        "  0.0 = not supported or contradicted\n"
        "Be conservative — prefer 0.5 when evidence is ambiguous.\n"
        "Reply with the number only, nothing else."
    )

    config = CompletionConfig(model=model, temperature=0.0, max_tokens=16)
    messages = [Message(role=MessageRole.USER, content=user_content)]

    try:
        result = await adapter.complete(messages, config)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "invariant_verifier.adapter_error",
            tag=tag[:60],
            error=str(exc),
        )
        return _HAIKU_SCORE_FALLBACK

    if result.is_err:
        log.warning(
            "invariant_verifier.llm_failed",
            tag=tag[:60],
            error=str(result.error),
        )
        return _HAIKU_SCORE_FALLBACK

    raw_text = (result.value.content or "").strip()
    match = _HAIKU_SCORE_RE.search(raw_text)
    if match:
        try:
            score = float(match.group(1))
            return max(0.0, min(1.0, score))
        except ValueError:
            pass

    log.warning(
        "invariant_verifier.unparseable_score",
        tag=tag[:60],
        response_preview=raw_text[:80],
    )
    return _HAIKU_SCORE_FALLBACK


async def verify_invariants(
    adapter: Any,
    tags: list[str],
    *,
    ac_trace: str,
    files_modified: list[str],
    model: str | None = None,
) -> list[tuple[str, float]]:
    """Verify ``[[INVARIANT]]`` tags via a Haiku model call per tag.

    Implements the Q3 (C-plus) Haiku verifier gate.  For each tag, a short
    prompt is sent to the ``model`` asking for a reliability score 0.0–1.0.
    Results are returned in input order; errors result in :data:`_HAIKU_SCORE_FALLBACK`.

    This function is intentionally **inline / blocking** — callers must
    ``await`` it before advancing the postmortem chain so the verified
    invariants are visible to the next AC's prompt.

    Args:
        adapter: LLM adapter used for completion calls (must implement
            the :class:`~ouroboros.providers.base.LLMAdapter` protocol).
        tags: Extracted invariant text strings (from :func:`~ouroboros.orchestrator.level_context.extract_invariant_tags`).
        ac_trace: Final message from the AC, used as evidence for verification.
        files_modified: Files changed during the AC, used as evidence.
        model: Override model. When ``None``, resolved via
            :func:`~ouroboros.config.loader.get_invariant_verifier_model`.

    Returns:
        List of ``(tag_text, reliability_score)`` pairs in input order.
        Empty when ``tags`` is empty.

    [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
    [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
    """
    if not tags:
        return []

    if model is None:
        from ouroboros.config.loader import get_invariant_verifier_model

        model = get_invariant_verifier_model()

    results: list[tuple[str, float]] = []
    for tag in tags:
        score = await _verify_single_tag(
            adapter,
            tag,
            ac_trace=ac_trace,
            files_modified=files_modified,
            model=model,
        )
        log.info(
            "invariant_verifier.tag_scored",
            tag=tag[:80],
            score=score,
            model=model,
        )
        results.append((tag, score))
    return results


def _render_chain_as_markdown(
    chain: PostmortemChain,
    session_id: str,
    execution_id: str,
) -> str:
    """Render a PostmortemChain as a human-readable markdown artifact.

    Uses ``serialize_postmortem_chain`` as the single data source so there
    is no second serialization path. Format per AC:

        ## AC <n> [<status>]
        - Files modified: ...
        - Gotchas: ...
        - Public API changes: ...
        - Invariants established: ...   (when non-empty)
        - Diff summary:                 (when non-empty; fenced code block)

    Args:
        chain: The postmortem chain to render.
        session_id: Session ID for the header.
        execution_id: Execution ID for the header.

    Returns:
        Markdown string with one section per AC.
    """
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        "# Postmortem Chain",
        "",
        f"**Session:** `{session_id}`  ",
        f"**Execution:** `{execution_id}`  ",
        f"**Written:** {now_str}  ",
        f"**ACs:** {len(chain.postmortems)}",
        "",
    ]

    serialized = serialize_postmortem_chain(chain)
    for entry in serialized:
        summary = entry.get("summary") or {}
        ac_index = summary.get("ac_index", 0)
        ac_content = summary.get("ac_content", "")
        status = entry.get("status", "pass")
        files_modified = summary.get("files_modified") or []
        public_api = summary.get("public_api") or ""
        gotchas = entry.get("gotchas") or []
        invariants = entry.get("invariants_established") or []
        duration = entry.get("duration_seconds", 0.0)
        retry_attempts = entry.get("retry_attempts", 0)
        diff_summary = entry.get("diff_summary") or ""

        lines.append(f"## AC {ac_index + 1} [{status}]")
        lines.append("")
        lines.append(f"**Task:** {ac_content}")
        if duration:
            lines.append(f"**Duration:** {duration:.1f}s")
        if retry_attempts:
            lines.append(f"**Retries:** {retry_attempts}")

        # Strict CommonMark requires a blank line before a bullet list when
        # the preceding line is a paragraph (e.g. **Retries:** or **Task:**),
        # otherwise the bullet is parsed as paragraph continuation and the
        # rendered HTML loses the list semantics.
        lines.append("")

        if files_modified:
            files_str = ", ".join(str(f) for f in files_modified)
            lines.append(f"- Files modified: {files_str}")
        else:
            lines.append("- Files modified: (none recorded)")

        if gotchas:
            gotchas_str = "; ".join(str(g) for g in gotchas)
            lines.append(f"- Gotchas: {gotchas_str}")
        else:
            lines.append("- Gotchas: (none)")

        if public_api:
            lines.append(f"- Public API changes: {public_api}")
        else:
            lines.append("- Public API changes: (none recorded)")

        if invariants:
            # Invariants are serialized as dicts via dataclasses.asdict(); extract text.
            inv_texts = [
                i.get("text", str(i)) if isinstance(i, dict) else str(i)
                for i in invariants
            ]
            lines.append(f"- Invariants established: {'; '.join(inv_texts)}")

        # Q2: emit `diff_summary` as a fenced code block under the bullet
        # list when populated.  The 2-space indent keeps the fenced block
        # associated with the bullet under CommonMark.  Empty diff_summary
        # is suppressed entirely to keep no-op-AC artifacts terse.
        if diff_summary:
            lines.append("- Diff summary:")
            lines.append("  ```text")
            for stat_line in diff_summary.split("\n"):
                lines.append(f"  {stat_line}")
            lines.append("  ```")

        lines.append("")

    return "\n".join(lines)


def write_chain_artifact(
    chain: PostmortemChain,
    session_id: str,
    execution_id: str,
    *,
    artifact_dir: str | None = None,
) -> Path:
    """Write the PostmortemChain to a markdown artifact file.

    The output directory defaults to ``docs/brainstorm`` but can be
    overridden via the ``OUROBOROS_CHAIN_ARTIFACT_DIR`` environment variable
    or the ``artifact_dir`` argument (explicit arg takes precedence over env var).

    The directory is created defensively (``parents=True, exist_ok=True``) so
    callers do not need to pre-create it.

    This function is intentionally synchronous — it is called after the
    serial loop completes and must not introduce async complexity.

    Args:
        chain: The chain to serialize.
        session_id: Session id used in the filename.
        execution_id: Execution id used in the file header.
        artifact_dir: Override directory. Falls back to env var, then default.

    Returns:
        Path of the written artifact file.
    """
    if artifact_dir is None:
        artifact_dir = os.environ.get(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", _DEFAULT_CHAIN_ARTIFACT_DIR
        )

    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"chain-{session_id}-{timestamp}.md"
    artifact_path = out_dir / filename

    content = _render_chain_as_markdown(chain, session_id, execution_id)
    artifact_path.write_text(content, encoding="utf-8")

    log.info(
        "serial_executor.chain_artifact.written",
        path=str(artifact_path),
        session_id=session_id,
        postmortems=len(chain.postmortems),
    )
    return artifact_path


def _write_compounding_checkpoint(
    store: Any,
    *,
    seed_id: str,
    session_id: str,
    ac_index: int,
    chain: PostmortemChain,
) -> None:
    """Write a per-AC compounding checkpoint after successful completion.

    Serializes the current postmortem chain into a
    :class:`~ouroboros.persistence.checkpoint.CompoundingCheckpointState`
    and persists it via ``store.write``.  Failures are caught and logged so
    a checkpoint write error never propagates to the caller.

    This function is synchronous — it is called inside the serial loop
    after each successful AC and must not introduce async complexity.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance (or any object with a ``write`` method that accepts
            a :class:`~ouroboros.persistence.checkpoint.CheckpointData`).
        seed_id: Seed identifier used as the checkpoint key.
        session_id: Session identifier — included in log context only.
        ac_index: 0-based index of the *just-completed* successful AC.
        chain: Current postmortem chain (already includes the postmortem
            for ``ac_index``).

    [[INVARIANT: checkpoints are only written after AC success, never on failure]]
    [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
    """
    try:
        serialized_chain = serialize_postmortem_chain(chain)
        state = CompoundingCheckpointState(
            last_completed_ac_index=ac_index,
            postmortem_chain=serialized_chain,
        )
        checkpoint = CheckpointData.create(
            seed_id=seed_id,
            phase="execution",
            state=state.to_dict(),
        )
        result = store.write(checkpoint)
        if result.is_err:
            log.warning(
                "serial_executor.checkpoint.write_failed",
                session_id=session_id,
                ac_index=ac_index,
                error=str(result.error),
            )
        else:
            log.info(
                "serial_executor.checkpoint.written",
                session_id=session_id,
                ac_index=ac_index,
                seed_id=seed_id,
                postmortems=len(chain.postmortems),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.checkpoint.unexpected_error",
            session_id=session_id,
            ac_index=ac_index,
            error=str(exc),
        )


def _write_partial_sub_ac_checkpoint(
    store: Any,
    *,
    seed_id: str,
    session_id: str,
    ac_index: int,
    sub_postmortems: tuple["ACPostmortem", ...],
    base_chain: "PostmortemChain",
    last_completed_ac_index: int,
) -> None:
    """Write a partial sub-AC state checkpoint after a decomposed AC fails.

    Called when a decomposed AC fails but some sub-ACs completed.  The
    checkpoint advances *neither* ``last_completed_ac_index`` (the failing AC
    is NOT counted as done) nor the ``postmortem_chain`` (the failing AC's
    postmortem is excluded); but it records which sub-ACs completed so a
    subsequent resume can skip them.

    The sub-postmortem context is included in the context_override of the
    resumed AC by :func:`_build_sub_postmortem_resume_context`.

    Failures are caught and logged so a write error never propagates.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance (or any object with a ``write`` method accepting a
            :class:`~ouroboros.persistence.checkpoint.CheckpointData`).
        seed_id: Seed identifier used as the checkpoint key.
        session_id: Session identifier — included in log context only.
        ac_index: 0-based index of the failing AC (partial sub-AC progress).
        sub_postmortems: Sub-postmortems for completed sub-ACs.
        base_chain: The postmortem chain up to (but not including) the
            failing AC.  The chain is NOT advanced in this checkpoint write.
        last_completed_ac_index: The last fully-completed AC index (unchanged
            because the failing AC did not succeed).

    [[INVARIANT: partial sub-AC checkpoint does NOT advance last_completed_ac_index]]
    [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
    """
    from ouroboros.orchestrator.level_context import PostmortemChain

    try:
        # Serialize each sub-postmortem independently using serialize_postmortem_chain
        # on a single-element chain to reuse the existing format.
        serialized_subs: list[dict] = []
        for sub_pm in sub_postmortems:
            sub_chain = PostmortemChain(postmortems=(sub_pm,))
            entries = serialize_postmortem_chain(sub_chain)
            if entries:
                serialized_subs.append(entries[0])

        serialized_base_chain = serialize_postmortem_chain(base_chain)
        state = CompoundingCheckpointState(
            last_completed_ac_index=last_completed_ac_index,
            postmortem_chain=serialized_base_chain,
            partial_failing_ac_index=ac_index,
            partial_failing_ac_sub_postmortems=serialized_subs,
        )
        checkpoint = CheckpointData.create(
            seed_id=seed_id,
            phase="execution",
            state=state.to_dict(),
        )
        result = store.write(checkpoint)
        if result.is_err:
            log.warning(
                "serial_executor.partial_checkpoint.write_failed",
                session_id=session_id,
                ac_index=ac_index,
                error=str(result.error),
            )
        else:
            log.info(
                "serial_executor.partial_checkpoint.written",
                session_id=session_id,
                ac_index=ac_index,
                seed_id=seed_id,
                sub_postmortems_count=len(sub_postmortems),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.partial_checkpoint.unexpected_error",
            session_id=session_id,
            ac_index=ac_index,
            error=str(exc),
        )


def _load_partial_failing_ac_state(
    store: Any,
    *,
    seed_id: str,
) -> tuple[int | None, list[dict] | None]:
    """Load partial failing AC state from a compounding checkpoint.

    Returns the ``partial_failing_ac_index`` and
    ``partial_failing_ac_sub_postmortems`` from the stored checkpoint without
    re-deriving the postmortem chain (which is handled by
    :func:`_load_compounding_checkpoint`).

    On any error (missing checkpoint, wrong mode, parse failure), returns
    ``(None, None)`` so the caller falls back to a fresh AC execution.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance.
        seed_id: Seed identifier — the key used to look up the checkpoint.

    Returns:
        ``(partial_failing_ac_index, partial_failing_ac_sub_postmortems)``
        where both are ``None`` when no partial state is present.

    [[INVARIANT: _load_partial_failing_ac_state returns (None, None) on any failure]]
    """
    try:
        load_result = store.load(seed_id)
    except Exception:  # noqa: BLE001
        return None, None

    if load_result.is_err:
        return None, None

    checkpoint = load_result.value
    try:
        state = CompoundingCheckpointState.from_dict(checkpoint.state)
    except (ValueError, KeyError, TypeError):
        return None, None

    return state.partial_failing_ac_index, state.partial_failing_ac_sub_postmortems


def _build_sub_postmortem_resume_context(
    sub_postmortem_dicts: list[dict],
) -> str:
    """Build a context section listing completed sub-ACs for the resumed AC.

    When a decomposed AC is resumed after a partial failure, the agent needs
    to know which sub-ACs were already completed so it can avoid re-running
    them.  This function formats the serialized sub-postmortems as a compact
    markdown section suitable for inclusion in the AC's context_override.

    Args:
        sub_postmortem_dicts: Serialized sub-postmortem dicts (each entry
            from :func:`~ouroboros.orchestrator.level_context.serialize_postmortem_chain`).

    Returns:
        Markdown string with one section per completed sub-AC, or empty
        string if ``sub_postmortem_dicts`` is empty.

    [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
    """
    if not sub_postmortem_dicts:
        return ""

    lines: list[str] = [
        "",
        "---",
        "## Sub-AC Resume Context",
        "",
        "The following sub-ACs were ALREADY COMPLETED in the prior partial run.",
        "Do NOT re-execute them.  Resume from the next incomplete sub-AC.",
        "",
    ]
    for i, entry in enumerate(sub_postmortem_dicts):
        summary = entry.get("summary") or {}
        ac_content = summary.get("ac_content", f"Sub-AC {i}")
        status = entry.get("status", "pass")
        files_modified = summary.get("files_modified") or []
        gotchas = entry.get("gotchas") or []

        lines.append(f"### Completed Sub-AC {i} [{status}]")
        lines.append(f"**Task:** {ac_content}")
        if files_modified:
            lines.append(f"**Files:** {', '.join(str(f) for f in files_modified)}")
        if gotchas:
            lines.append(f"**Gotchas:** {'; '.join(str(g) for g in gotchas)}")
        lines.append("")

    return "\n".join(lines)


def _build_monolithic_resume_decision_prompt(
    ac_content: str,
    context_section: str,
) -> str:
    """Build the DECISION prompt for monolithic AC resume adjudication.

    Packages the original AC text and the available context (prior AC
    postmortems) into a prompt asking the agent to decide whether to
    *continue* from the interrupted state or *restart* from scratch.

    The decision instruction is on the last line, asking for a first-line
    response of exactly ``DECISION: continue`` or ``DECISION: restart``.

    Args:
        ac_content: Original acceptance criterion text (the full task).
        context_section: Postmortem chain context rendered for the prompt
            (used as the best available "pre-crash trace" since per-message
            traces are not stored in the checkpoint for monolithic ACs).

    Returns:
        Prompt string for the adjudication LLM call.

    [[INVARIANT: monolithic resume prompt always includes the literal text DECISION: continue and DECISION: restart as options]]
    """
    stripped = context_section.strip() if context_section else ""
    context_block = stripped if stripped else "(No prior context recorded)"
    return (
        "You are resuming an interrupted AI workflow task.\n\n"
        "## Original Task (Acceptance Criterion)\n\n"
        f"{ac_content}\n\n"
        "## Prior Work Context\n\n"
        f"{context_block}\n\n"
        "## Decision Required\n\n"
        "This task was interrupted before completion in a prior run.\n"
        "Based on the context above, decide whether to:\n"
        "  (a) continue — resume from the interrupted state if there is "
        "evidence of substantial partial work that should not be repeated\n"
        "  (b) restart — start this task fresh from the beginning if there "
        "is little or no partial work, or if a clean start is safer\n\n"
        "State your decision on the FIRST LINE as exactly one of:\n"
        "  DECISION: continue\n"
        "or\n"
        "  DECISION: restart\n\n"
        "Then briefly explain your reasoning on subsequent lines."
    )


def _parse_monolithic_resume_decision(response_text: str) -> str:
    """Parse DECISION: continue|restart from the first line of a response.

    Looks for ``DECISION:`` (case-insensitive) in the first non-empty line
    and extracts ``continue`` or ``restart``.  Falls back to ``"restart"``
    on any parse failure — the conservative default keeps the workflow safe.

    Args:
        response_text: Raw text response from the adjudication call.

    Returns:
        ``"continue"`` or ``"restart"``.

    [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
    """
    if not response_text or not response_text.strip():
        return "restart"

    for line in response_text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if "decision:" in lower:
            # Extract the part after the colon.
            after_colon = lower.split("decision:", 1)[1].strip()
            if after_colon.startswith("continue"):
                return "continue"
            if after_colon.startswith("restart"):
                return "restart"
        # Only examine the first non-empty line.
        break

    log.info(
        "serial_executor.monolithic_resume.unparseable_decision",
        response_preview=response_text[:100],
    )
    return "restart"


async def _adjudicate_monolithic_resume(
    adapter: Any,
    ac_content: str,
    context_section: str,
    *,
    model: str | None = None,
) -> tuple[str, str]:
    """Invoke the agent to decide continue vs restart for a monolithic AC resume.

    Constructs the DECISION prompt, calls ``adapter.complete()``, and parses
    the first line for ``DECISION: continue`` or ``DECISION: restart``.

    Falls back to ``"restart"`` on any adapter or parse error — the safe
    conservative default that ensures a clean execution from the top.

    This call is **inline-blocking**: the caller must await it before
    proceeding to ``_execute_single_ac`` so the decision is known before
    the AC runs.

    Args:
        adapter: LLM adapter with a ``complete`` method.
        ac_content: Original AC text (the full task description).
        context_section: Postmortem chain context (acts as the pre-crash trace).
        model: Model override.  When ``None``, resolved via
            :func:`~ouroboros.config.loader.get_invariant_verifier_model`
            (same helper used by the Haiku verifier — a cheap model is fine
            for a binary continue/restart decision).

    Returns:
        ``(decision, raw_response)`` where ``decision`` is ``"continue"`` or
        ``"restart"`` and ``raw_response`` is the full raw text from the
        adapter (may be empty on error).

    [[INVARIANT: _adjudicate_monolithic_resume always returns ("continue"|"restart", str)]]
    [[INVARIANT: adapter error in monolithic adjudication defaults to "restart"]]
    """
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    prompt = _build_monolithic_resume_decision_prompt(ac_content, context_section)

    if model is None:
        try:
            from ouroboros.config.loader import get_invariant_verifier_model

            model = get_invariant_verifier_model()
        except Exception as exc:  # noqa: BLE001
            # Fall back to the same resolution path get_invariant_verifier_model
            # uses internally (assertion-extraction model). Avoids drifting onto
            # an unversioned model name that may not match adapter routing.
            try:
                from ouroboros.config.loader import get_assertion_extraction_model

                model = get_assertion_extraction_model()
            except Exception:  # noqa: BLE001
                model = "claude-haiku-4-5-20251001"
            log.warning(
                "serial_executor.monolithic_resume.verifier_model_lookup_failed",
                error=str(exc),
                fallback_model=model,
            )

    config = CompletionConfig(model=model, temperature=0.0, max_tokens=256)
    messages = [Message(role=MessageRole.USER, content=prompt)]

    try:
        result = await adapter.complete(messages, config)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.monolithic_resume.adapter_error",
            error=str(exc),
        )
        return "restart", ""

    if result.is_err:
        log.warning(
            "serial_executor.monolithic_resume.llm_failed",
            error=str(result.error),
        )
        return "restart", ""

    raw_text = (result.value.content or "").strip()
    decision = _parse_monolithic_resume_decision(raw_text)

    log.info(
        "serial_executor.monolithic_resume.decision_made",
        decision=decision,
        raw_preview=raw_text[:100],
    )
    return decision, raw_text


def _load_compounding_checkpoint(
    store: Any,
    *,
    seed_id: str,
    session_id: str,
    resume_session_id: str,
) -> tuple[PostmortemChain, int]:
    """Load a compounding checkpoint and deserialize the postmortem chain.

    Attempts to load the checkpoint stored under ``seed_id`` from ``store``.
    On success, deserializes the saved :class:`PostmortemChain` and returns
    it along with the ``last_completed_ac_index`` from the checkpoint state.

    On any failure (missing checkpoint, wrong mode, deserialization error),
    logs a warning and returns an empty chain with ``last_completed_ac_index=-1``
    so the caller falls back to a fresh run.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance (or any object with a ``load`` method).
        seed_id: Seed identifier — the key used to look up the checkpoint.
        session_id: Current session id (used for log context only).
        resume_session_id: Session id of the run being resumed (log context).

    Returns:
        ``(chain, last_completed_ac_index)`` where ``chain`` is the
        deserialized :class:`PostmortemChain` (empty on failure) and
        ``last_completed_ac_index`` is the 0-based index of the last
        successfully completed AC (-1 if nothing was found).

    [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
    [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
    """
    try:
        load_result = store.load(seed_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.resume.store_error",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    if load_result.is_err:
        log.warning(
            "serial_executor.resume.no_checkpoint",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(load_result.error),
        )
        return PostmortemChain(), -1

    checkpoint = load_result.value

    try:
        state = CompoundingCheckpointState.from_dict(checkpoint.state)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning(
            "serial_executor.resume.invalid_checkpoint",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    try:
        chain = deserialize_postmortem_chain(state.postmortem_chain)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.resume.deserialize_error",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    log.info(
        "serial_executor.resume.checkpoint_loaded",
        session_id=session_id,
        seed_id=seed_id,
        resume_session_id=resume_session_id,
        last_completed_ac_index=state.last_completed_ac_index,
        postmortems_in_chain=len(chain.postmortems),
    )
    return chain, state.last_completed_ac_index


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
        resume_session_id: str | None = None,
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
            resume_session_id: When provided, attempt to load a saved
                compounding checkpoint for this seed and deserialize the
                postmortem chain so that already-completed ACs are skipped.
                The checkpoint is keyed by ``seed.metadata.seed_id`` —
                ``resume_session_id`` is used for logging/diagnostics only
                and does not change the storage key.  If no checkpoint is
                found or the checkpoint is not a valid compounding checkpoint,
                a warning is logged and execution continues from the beginning.

        Returns:
            ParallelExecutionResult with one stage per AC so downstream
            progress tooling sees a structurally similar shape to the
            parallel path.

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        ac_order = linearize_execution_plan(execution_plan)
        start_time = datetime.now(UTC)

        # Q6.2: Checkpoint loading for resume.
        # When resume_session_id is supplied and a checkpoint store is available,
        # attempt to load the persisted compounding state so prior ACs are skipped
        # and their postmortems are injected into the rolling chain immediately.
        chain = PostmortemChain()
        last_completed_ac_index: int = -1  # -1 means "nothing completed yet"
        # Distinct from last_completed_ac_index: tracks the most recently
        # persisted AC index across the loop body.  last_completed_ac_index is
        # the resume baseline (immutable for the run); current_persisted_ac_index
        # advances on each successful checkpoint write and is what gets passed
        # to _write_partial_sub_ac_checkpoint so partial writes record the real
        # boundary instead of regressing to the baseline.
        current_persisted_ac_index: int = -1
        # Sub-AC resume: partial state for a failing decomposed AC.
        # Set when the checkpoint records a partially-completed decomposed AC.
        _partial_failing_ac_index: int | None = None
        _partial_failing_ac_sub_pms: list[dict] | None = None

        if resume_session_id is not None and self._checkpoint_store is not None:
            chain, last_completed_ac_index = _load_compounding_checkpoint(
                store=self._checkpoint_store,
                seed_id=seed.metadata.seed_id,
                session_id=session_id,
                resume_session_id=resume_session_id,
            )
            # Resuming: the persisted cursor starts at the resume baseline.
            current_persisted_ac_index = last_completed_ac_index
            # Also load partial sub-AC state (separate load to avoid breaking
            # _load_compounding_checkpoint's return type contract).
            _partial_failing_ac_index, _partial_failing_ac_sub_pms = (
                _load_partial_failing_ac_state(
                    store=self._checkpoint_store,
                    seed_id=seed.metadata.seed_id,
                )
            )
        elif resume_session_id is not None:
            # User asked to resume but no checkpoint store is configured. Without
            # this warning, the resume request is silently ignored and the run
            # starts fresh — surprising to anyone debugging why their --resume
            # produced an unrelated execution.
            log.warning(
                "serial_executor.resume.checkpoint_store_unavailable",
                resume_session_id=resume_session_id,
                seed_id=seed.metadata.seed_id,
                detail="--resume requested but checkpoint store is not configured; starting fresh",
            )

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

            # Q6.2: Skip ACs that were already completed in a prior run (checkpoint resume).
            # The chain is already seeded with their postmortems from deserialization,
            # so we do NOT add to the chain here — just record the skipped result.
            if ac_index <= last_completed_ac_index:
                resumed_result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=True,
                    final_message=(
                        f"Skipped via checkpoint resume (session {resume_session_id}); "
                        f"this AC (index {ac_index}) was already completed in the prior run."
                    ),
                    retry_attempt=0,
                    outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                )
                results.append(resumed_result)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(resumed_result,),
                        started=False,
                    )
                )
                log.info(
                    "serial_executor.ac.skipped_via_resume",
                    session_id=session_id,
                    ac_index=ac_index,
                    resume_session_id=resume_session_id,
                    last_completed_ac_index=last_completed_ac_index,
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
            # Q7: Capture truncation info synchronously; emit event afterward.
            _truncation_info: list[dict] = []

            def _on_truncated(
                dropped: int,
                budget: int,
                rendered: int,
                full_forms: int,
                invariants_ct: int,
            ) -> None:
                _truncation_info.append(
                    {
                        "dropped_count": dropped,
                        "char_budget": budget,
                        "rendered_chars": rendered,
                        "full_forms_preserved": full_forms,
                        "cumulative_invariants_preserved": invariants_ct,
                    }
                )

            context_section = build_postmortem_chain_prompt(
                chain, on_truncated=_on_truncated
            )

            # Emit Q7 truncation event if the chain was over budget.
            for _trunc in _truncation_info:
                await self._safe_emit_event(
                    create_postmortem_chain_truncated_event(
                        session_id=session_id,
                        execution_id=execution_id,
                        **_trunc,
                    )
                )

            # Q6.2 sub-postmortem resume: if this AC matches the partial failing
            # AC recorded in the checkpoint, append the completed sub-AC context
            # to context_section so the agent knows what was already done and
            # where to resume.  Emit a structured event for observability.
            if (
                _partial_failing_ac_index is not None
                and ac_index == _partial_failing_ac_index
                and _partial_failing_ac_sub_pms
            ):
                sub_resume_ctx = _build_sub_postmortem_resume_context(
                    _partial_failing_ac_sub_pms
                )
                context_section = context_section + sub_resume_ctx
                _n_sub_completed = len(_partial_failing_ac_sub_pms)
                log.info(
                    "serial_executor.resume.sub_postmortem_boundary",
                    session_id=session_id,
                    ac_index=ac_index,
                    sub_acs_completed=_n_sub_completed,
                    resume_from_sub_ac=_n_sub_completed,
                )
                await self._safe_emit_event(
                    create_sub_postmortem_resume_event(
                        session_id=session_id,
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_acs_completed=_n_sub_completed,
                        resume_from_sub_ac=_n_sub_completed,
                    )
                )

            ac_content = seed.acceptance_criteria[ac_index]

            # Q6.2 monolithic resume adjudication:
            # When resuming from a checkpoint and this is the first AC after
            # the last completed one, and the sub-AC boundary resume path was
            # NOT taken (no sub_postmortems), invoke the agent to decide
            # whether to continue from the interrupted state or restart fresh.
            #
            # Trigger conditions:
            #   1. resume_session_id is set (explicit resume)
            #   2. last_completed_ac_index >= 0 (real checkpoint loaded)
            #   3. ac_index == last_completed_ac_index + 1 (this is the failing AC)
            #   4. Sub-AC boundary path was not taken for this AC
            _is_monolithic_failing_ac = (
                resume_session_id is not None
                and last_completed_ac_index >= 0
                and ac_index == last_completed_ac_index + 1
            )
            _sub_ac_boundary_taken = (
                _partial_failing_ac_index is not None
                and ac_index == _partial_failing_ac_index
                and bool(_partial_failing_ac_sub_pms)
            )

            if _is_monolithic_failing_ac and not _sub_ac_boundary_taken:
                _adj_decision, _adj_raw = await _adjudicate_monolithic_resume(
                    self._adapter,
                    ac_content,
                    context_section,
                )
                log.info(
                    "serial_executor.resume.monolithic_adjudication",
                    session_id=session_id,
                    ac_index=ac_index,
                    decision=_adj_decision,
                )
                await self._safe_emit_event(
                    create_monolithic_resume_adjudicated_event(
                        session_id=session_id,
                        execution_id=execution_id,
                        ac_index=ac_index,
                        decision=_adj_decision,
                        raw_response_preview=_adj_raw,
                    )
                )
                if _adj_decision == "continue":
                    # Append a continuation hint so the agent knows to resume
                    # rather than restart from scratch.
                    context_section = (
                        context_section
                        + "\n\n---\n"
                        + "## Resume Instruction\n\n"
                        + "This AC was interrupted in a prior run. "
                        + "Based on context above, you decided to CONTINUE "
                        + "from where you left off. Resume the task rather "
                        + "than restarting from scratch.\n"
                    )
                # If decision == "restart": no modification — agent runs the
                # AC fresh without any continuation hint.

            self._console.print(
                f"[bold cyan]Serial AC {ac_index + 1}/{len(ac_order)}[/bold cyan]"
                f" [{len(chain.postmortems)} postmortems in chain]"
            )
            self._flush_console()

            # Q2: Capture pre-AC stash SHA so post-AC `git diff --stat` has a
            # baseline.  Wrapped defensively — diff capture must NEVER fail
            # the AC.  All known failure modes already return None inside
            # capture_pre_ac_snapshot; the broad except guards against
            # unforeseen errors and monkeypatched test stubs that may raise.
            workspace_for_diff = Path(self._task_cwd or ".")
            try:
                pre_sha = capture_pre_ac_snapshot(workspace_for_diff)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "serial_executor.diff_capture.failed",
                    reason="unexpected_exception",
                    phase="pre_snapshot",
                    error=str(exc),
                )
                pre_sha = None

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

            # Q2: Compute diff_summary against the post-AC snapshot.  Any
            # failure inside compute_diff_summary already returns "";
            # the broad except is a final safety net.
            try:
                diff_summary = compute_diff_summary(pre_sha, workspace_for_diff)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "serial_executor.diff_capture.failed",
                    reason="unexpected_exception",
                    phase="diff",
                    error=str(exc),
                )
                diff_summary = ""

            postmortem = self._build_postmortem_from_result(
                result, workspace_root=self._task_cwd, diff_summary=diff_summary
            )

            # Q3 (C-plus): Extract [[INVARIANT: ...]] tags inline-blocking before
            # chain advance so the next AC's prompt sees verified invariants.
            # Scan BOTH final_message and the full message stream — tags emitted
            # mid-execution must not be dropped just because the final message
            # also has tags.  extract_invariant_tags dedups internally; we then
            # union the two sources by normalized key to preserve insertion
            # order without re-rendering duplicates.
            final_tags = extract_invariant_tags(result.final_message or "")
            inv_tags: list[str] = list(final_tags)
            if result.messages:
                stream_tags = extract_invariant_tags(result.messages)
                seen_norm = {" ".join(t.lower().split()) for t in inv_tags}
                for tag in stream_tags:
                    norm = " ".join(tag.lower().split())
                    if norm and norm not in seen_norm:
                        inv_tags.append(tag)
                        seen_norm.add(norm)

            if inv_tags or postmortem.sub_postmortems:
                verified_pairs: list[tuple[str, float]] = []
                if inv_tags:
                    verified_pairs = list(
                        await verify_invariants(
                            self._adapter,
                            inv_tags,
                            ac_trace=result.final_message or "",
                            files_modified=list(postmortem.summary.files_modified),
                        )
                    )

                # Sub-AC invariants flatten upward (AC-2 B-prime extension):
                # include each non-contradicted sub-postmortem invariant in the
                # merge input so the parent's invariants_established reflects
                # work done by decomposed sub-ACs.  merge_invariants handles
                # re-declaration semantics (occurrence bumps, reliability blend).
                sub_pairs: list[tuple[str, float]] = []
                for sub_pm in postmortem.sub_postmortems:
                    for sub_inv in sub_pm.invariants_established:
                        if not sub_inv.is_contradicted:
                            sub_pairs.append((sub_inv.text, sub_inv.reliability))

                # Route through PostmortemChain.merge_invariants so re-declarations
                # bump occurrences, blended reliability accumulates across ACs,
                # and NOT-prefix contradictions are detected.  Below-threshold
                # invariants are STILL stored — the render-gate in to_prompt_text
                # filters them at display time per the seed's design.
                merged = chain.merge_invariants(
                    verified_pairs + sub_pairs,
                    source_ac_id=f"ac_{ac_index}",
                )
                if merged:
                    postmortem = ACPostmortem(
                        summary=postmortem.summary,
                        diff_summary=postmortem.diff_summary,
                        tool_trace_digest=postmortem.tool_trace_digest,
                        gotchas=postmortem.gotchas,
                        qa_suggestions=postmortem.qa_suggestions,
                        invariants_established=merged,
                        retry_attempts=postmortem.retry_attempts,
                        status=postmortem.status,
                        duration_seconds=postmortem.duration_seconds,
                        ac_native_session_id=postmortem.ac_native_session_id,
                        sub_postmortems=postmortem.sub_postmortems,
                    )
                    min_rel = _get_min_reliability()
                    above_threshold = sum(
                        1 for inv in merged
                        if not inv.is_contradicted and inv.reliability >= min_rel
                    )
                    log.info(
                        "serial_executor.invariants.captured",
                        session_id=session_id,
                        ac_index=ac_index,
                        total_tags=len(inv_tags),
                        sub_invariants_merged=len(sub_pairs),
                        merged_count=len(merged),
                        above_threshold=above_threshold,
                        min_reliability=min_rel,
                    )

            # Capture chain BEFORE appending the new postmortem so the partial
            # checkpoint (for a failing decomposed AC) can reference the base chain
            # without the failing AC's postmortem included.
            chain_before_append = chain
            chain = chain.append(postmortem)

            # Q6.2: Write per-AC checkpoint after successful completion.
            # Checkpoints are ONLY written on success — failed ACs do NOT advance
            # the checkpoint cursor, so a resume will retry the failing AC.
            if result.success and self._checkpoint_store is not None:
                _write_compounding_checkpoint(
                    store=self._checkpoint_store,
                    seed_id=seed.metadata.seed_id,
                    session_id=session_id,
                    ac_index=ac_index,
                    chain=chain,
                )
                # Track the persisted progress in-process so a later partial
                # checkpoint (for a subsequent failing decomposed AC) records
                # the correct boundary instead of regressing to the resume
                # baseline.  Kept distinct from last_completed_ac_index so the
                # monolithic resume adjudication trigger (which uses
                # last_completed_ac_index as a resume baseline, not a moving
                # cursor) keeps firing only for the actual failing AC.
                current_persisted_ac_index = ac_index
            elif (
                not result.success
                and result.sub_results
                and postmortem.sub_postmortems
                and self._checkpoint_store is not None
            ):
                # Q6.2 sub-postmortem path: decomposed AC failed but some sub-ACs
                # completed.  Write a partial checkpoint so a future resume can
                # detect the sub-AC boundary and skip already-completed sub-ACs.
                # Does NOT advance last_completed_ac_index.
                _write_partial_sub_ac_checkpoint(
                    store=self._checkpoint_store,
                    seed_id=seed.metadata.seed_id,
                    session_id=session_id,
                    ac_index=ac_index,
                    sub_postmortems=postmortem.sub_postmortems,
                    base_chain=chain_before_append,
                    last_completed_ac_index=current_persisted_ac_index,
                )

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

        # AC-1 (Q6.1): Write end-of-run chain artifact. Always produced — even
        # for failed/partial runs — so crashed runs leave an inspectable chain.
        # Failures here are logged but never propagate to the caller.
        if chain.postmortems:
            try:
                write_chain_artifact(
                    chain,
                    session_id=session_id,
                    execution_id=execution_id,
                )
            except Exception as artifact_exc:  # noqa: BLE001
                log.warning(
                    "serial_executor.chain_artifact.write_failed",
                    session_id=session_id,
                    error=str(artifact_exc),
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
        diff_summary: str = "",
    ) -> ACPostmortem:
        """Derive an ACPostmortem from an ACExecutionResult.

        Uses the existing :func:`extract_level_context` summarization
        (which already folds tool-use events into files_modified, tools_used,
        key_output, and public_api) for a deterministic reconstruction of
        the factual half of the postmortem.

        Args:
            result: The AC execution result to derive the postmortem from.
            workspace_root: Working-directory anchor used by
                :func:`extract_level_context` for public-API summarization.
            diff_summary: Truncated ``git diff --stat`` for the AC, computed
                by :func:`compute_diff_summary` at the call site.  Defaults
                to ``""`` so the recursive sub-postmortem call below does
                not require per-sub-AC capture (top-AC diff already covers
                the union — sub-AC granular capture is out of scope).
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

        # B-prime: if the result has sub-results (decomposed AC), recursively build
        # sub-postmortems and flatten their data into the parent postmortem.
        sub_pms: tuple[ACPostmortem, ...] = ()
        if result.sub_results:
            sub_pms = tuple(
                SerialCompoundingExecutor._build_postmortem_from_result(
                    sub_result,
                    workspace_root=workspace_root,
                )
                for sub_result in result.sub_results
            )

            # Flatten files_modified: union of parent + all sub-postmortems (order-preserving, dedup).
            seen_files: dict[str, None] = dict.fromkeys(summary.files_modified)
            for sub_pm in sub_pms:
                for f in sub_pm.summary.files_modified:
                    seen_files.setdefault(f, None)
            flat_files: tuple[str, ...] = tuple(seen_files)

            # Flatten gotchas: parent's + all sub-postmortems' gotchas.
            flat_gotchas: tuple[str, ...] = gotchas + tuple(
                g for sub_pm in sub_pms for g in sub_pm.gotchas
            )

            # Flatten public_api: join non-empty strings, order-preserving, dedup.
            api_parts: list[str] = []
            if summary.public_api:
                api_parts.append(summary.public_api)
            for sub_pm in sub_pms:
                if sub_pm.summary.public_api and sub_pm.summary.public_api not in api_parts:
                    api_parts.append(sub_pm.summary.public_api)
            flat_public_api = "; ".join(api_parts)

            # Replace summary with the flattened version (frozen dataclass — create new).
            summary = ACContextSummary(
                ac_index=summary.ac_index,
                ac_content=summary.ac_content,
                success=summary.success,
                tools_used=summary.tools_used,
                files_modified=flat_files,
                key_output=summary.key_output,
                public_api=flat_public_api,
            )
            gotchas = flat_gotchas

        return ACPostmortem(
            summary=summary,
            diff_summary=diff_summary,
            status=status,
            retry_attempts=result.retry_attempt,
            duration_seconds=result.duration_seconds,
            ac_native_session_id=result.session_id,
            gotchas=gotchas,
            sub_postmortems=sub_pms,
        )


__all__ = [
    "SerialCompoundingExecutor",
    "linearize_execution_plan",
    "verify_invariants",
    "write_chain_artifact",
    "_adjudicate_monolithic_resume",
    "_build_monolithic_resume_decision_prompt",
    "_load_compounding_checkpoint",
    "_load_partial_failing_ac_state",
    "_parse_monolithic_resume_decision",
    "_write_compounding_checkpoint",
    "_write_partial_sub_ac_checkpoint",
    "_build_sub_postmortem_resume_context",
    "create_postmortem_chain_truncated_event",
]