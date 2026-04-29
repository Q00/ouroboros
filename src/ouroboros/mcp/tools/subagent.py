"""Subagent dispatch helper for Ouroboros MCP tool handlers.

When Ouroboros runs inside OpenCode, LLM-requiring handlers don't call LLMs
directly. Instead they return a structured ``_subagent`` dispatch payload in
``MCPToolResult.meta``. The OpenCode bridge plugin intercepts this payload and
spawns a native OpenCode subagent (visible in TUI) to do the actual LLM work.

Architecture:
    Handler.handle(args)
        → build_*_subagent(args)       # tool-specific builder
        → build_subagent_result(payload)  # wraps in MCPToolResult
        → MCPToolResult(meta={"_subagent": {...}})
        ↓ (MCP transport)
    Bridge plugin reads meta._subagent
        → injects SubtaskPart into parent session
        → OpenCode spawns child session with parentID
        → subagent executes prompt, result flows back

Payload structure:
    {
        "_subagent": {
            "tool_name": str,   # which MCP tool triggered dispatch
            "title": str,       # human-readable for TUI pane title
            "agent": str,       # OpenCode subagent type (default: "general")
            "prompt": str,      # full prompt for subagent LLM
            "model": str|None,  # optional model override hint
            "context": dict,    # original tool args for round-trip
        }
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolResult,
)

log = structlog.get_logger(__name__)

_INTERVIEW_SUBAGENT_MAX_CONTEXT_CHARS = 600
_INTERVIEW_SUBAGENT_MAX_PREVIOUS_TRANSCRIPT_CHARS = 200
_INTERVIEW_SUBAGENT_MAX_TRANSCRIPT_QUESTION_CHARS = 900
_INTERVIEW_SUBAGENT_MAX_TRANSCRIPT_ANSWER_CHARS = 220
_INTERVIEW_SUBAGENT_MAX_ANSWER_CHARS = 300

# ---------------------------------------------------------------------------
# SubagentPayload dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubagentPayload:
    """Structured dispatch payload for OpenCode subagent bridge.

    Frozen + slotted for safety and performance. Immutable after creation.
    """

    tool_name: str
    title: str
    prompt: str
    agent: str = "general"
    model: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dict for JSON transport in MCPToolResult.meta."""
        return {
            "tool_name": self.tool_name,
            "title": self.title,
            "agent": self.agent,
            "prompt": self.prompt,
            "model": self.model,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# Core builders
# ---------------------------------------------------------------------------


def build_subagent_payload(
    *,
    tool_name: str,
    title: str,
    prompt: str,
    agent: str = "general",
    model: str | None = None,
    context: dict[str, Any] | None = None,
) -> SubagentPayload:
    """Build a SubagentPayload with validation.

    Args:
        tool_name: MCP tool name that triggered dispatch (e.g. "ouroboros_qa").
        title: Human-readable title for TUI subagent pane.
        prompt: Full prompt text for the subagent LLM.
        agent: OpenCode subagent type. Default "general".
        model: Optional model override hint for the subagent.
        context: Original tool arguments for bridge round-trip.

    Returns:
        Validated SubagentPayload.

    Raises:
        ValueError: If required string fields are empty.
    """
    if not tool_name:
        raise ValueError("tool_name must not be empty")
    if not title:
        raise ValueError("title must not be empty")
    if not prompt:
        raise ValueError("prompt must not be empty")

    return SubagentPayload(
        tool_name=tool_name,
        title=title,
        prompt=prompt,
        agent=agent,
        model=model,
        context=context or {},
    )


def build_subagent_result(
    payload: SubagentPayload,
    *,
    response_shape: dict[str, Any] | None = None,
) -> Result:
    """Wrap a SubagentPayload into an MCPToolResult for MCP transport.

    The payload is serialized as JSON text in the content field because the
    FastMCP adapter only passes ``text_content`` through to the wire — the
    ``meta`` dict is lost. The bridge plugin parses JSON from the text to
    detect the ``_subagent`` key.

    Public-contract preservation (#442): when ``response_shape`` is provided,
    the natural tool response fields (e.g. ``session_id``, ``job_id``,
    ``status``) are merged into the JSON body ALONGSIDE ``_subagent``. Plugin
    still finds ``_subagent`` via ``JSON.parse``; consumers still find the
    contract fields at top level. When ``response_shape`` is ``None`` the
    legacy ``{"_subagent": {...}}`` shape is emitted unchanged.

    Args:
        payload: The subagent dispatch payload.
        response_shape: Optional mapping of public-contract keys to merge into
            the response body (content JSON + meta). Must NOT contain the
            reserved key ``_subagent``; it is always overwritten by the
            dispatch payload.

    Returns:
        Result.ok(MCPToolResult) with ``_subagent`` present in both content
        JSON and meta, alongside any caller-supplied ``response_shape`` keys.
    """
    body: dict[str, Any] = {}
    if response_shape:
        body.update(response_shape)
    body["_subagent"] = payload.to_dict()

    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text=json.dumps(body)),),
            is_error=False,
            meta=dict(body),
        )
    )


# ---------------------------------------------------------------------------
# Runtime dispatch gate
# ---------------------------------------------------------------------------


_OPENCODE_RUNTIMES = frozenset({"opencode", "opencode_cli"})


def should_dispatch_via_plugin(
    runtime_backend: str | None,
    opencode_mode: str | None,
) -> bool:
    """Return True when the OpenCode bridge plugin is expected to intercept.

    The MCP handlers emit a ``_subagent`` envelope only when a bridge plugin
    is loaded inside the calling OpenCode session. In every other runtime
    (claude, codex, opencode subprocess, none) the envelope has no receiver
    and the handler must run the real in-process execution path instead.

    Rules:
        - runtime_backend not OpenCode → False.
        - runtime_backend OpenCode, opencode_mode="plugin" → True.
        - runtime_backend OpenCode, opencode_mode="subprocess" → False.
        - runtime_backend OpenCode, opencode_mode None/empty → False.
            Safe default: upgraded users who haven't re-run ``ouroboros setup``
            will have opencode_mode=None. They don't have the bridge plugin
            installed, so dispatching envelopes would break their flows.
            Require explicit opt-in via ``ouroboros setup --opencode-mode=plugin``.

    Args:
        runtime_backend: Resolved agent runtime backend name.
        opencode_mode: Configured ``orchestrator.opencode_mode`` value.

    Returns:
        True when dispatch envelope should be returned; False otherwise.
    """
    backend = (runtime_backend or "").strip().lower()
    if backend not in _OPENCODE_RUNTIMES:
        return False
    mode = (opencode_mode or "").strip().lower()
    return mode == "plugin"


def _truncate_tail(text: str | None, max_chars: int) -> str:
    """Keep prompt inputs bounded while preserving the most recent context."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return "[truncated]\n" + text[-max_chars:]


def _truncate_head(text: str | None, max_chars: int) -> str:
    """Keep prompt inputs bounded while preserving the opening context."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def _truncate_prompt_line(line: str, max_content_chars: int) -> str:
    """Bound one formatted transcript line without losing its Q/A label."""
    marker = ":** "
    if marker not in line:
        return line if len(line) <= max_content_chars else line[:max_content_chars] + "..."

    prefix, content = line.split(marker, 1)
    prefix = f"{prefix}{marker}"
    if len(content) <= max_content_chars:
        return line
    return f"{prefix}{content[:max_content_chars]}... [truncated]"


_TRANSCRIPT_Q_MARKER_RE = re.compile(r"(?m)^\*\*Q\d+:\*\* ")
_TRANSCRIPT_A_MARKER_RE = re.compile(r"(?m)^\*\*A\d+:\*\* ")


def _compact_transcript_section(section: str, max_content_chars: int) -> str:
    """Compact a marked Q/A section while preserving the marker."""
    lines = section.splitlines()
    if not lines:
        return ""

    marker = ":** "
    first_line = lines[0]
    if marker not in first_line:
        return _truncate_tail(section, max_content_chars)

    prefix, first_content = first_line.split(marker, 1)
    prefix = f"{prefix}{marker}"
    content_parts = [first_content, *lines[1:]]
    content = "\n".join(content_parts).rstrip()
    if len(content) <= max_content_chars:
        return section
    return f"{prefix}{content[:max_content_chars]}... [truncated]"


def _compact_latest_transcript_round(round_text: str) -> str:
    """Preserve the latest transcript round as Q/A sections while bounding content."""
    answer_match = _TRANSCRIPT_A_MARKER_RE.search(round_text)
    if answer_match is None:
        return _compact_transcript_section(
            round_text,
            _INTERVIEW_SUBAGENT_MAX_TRANSCRIPT_QUESTION_CHARS,
        )

    question_section = round_text[: answer_match.start()].rstrip()
    answer_section = round_text[answer_match.start() :].rstrip()
    compacted_question = _compact_transcript_section(
        question_section,
        _INTERVIEW_SUBAGENT_MAX_TRANSCRIPT_QUESTION_CHARS,
    )
    compacted_answer = _compact_transcript_section(
        answer_section,
        _INTERVIEW_SUBAGENT_MAX_TRANSCRIPT_ANSWER_CHARS,
    )
    return f"{compacted_question}\n{compacted_answer}"


def _compact_interview_transcript(transcript: str) -> str:
    """Compact transcript history without splitting the latest Q/A block."""
    question_matches = list(_TRANSCRIPT_Q_MARKER_RE.finditer(transcript))
    if not question_matches:
        return _truncate_tail(transcript, _INTERVIEW_SUBAGENT_MAX_PREVIOUS_TRANSCRIPT_CHARS)

    latest_start = question_matches[-1].start()
    latest_round = transcript[latest_start:].strip()
    if not latest_round:
        return ""

    compacted_latest_round = _compact_latest_transcript_round(latest_round)
    previous = transcript[:latest_start].strip()
    if not previous:
        return compacted_latest_round

    previous_tail = _truncate_tail(
        previous,
        _INTERVIEW_SUBAGENT_MAX_PREVIOUS_TRANSCRIPT_CHARS,
    )
    return f"{previous_tail}\n\n{compacted_latest_round}"


def _load_seed_closer_summary() -> str:
    """Load the compact Seed Closer guard, tolerating older custom prompt overrides."""
    from ouroboros.agents.loader import load_agent_section

    try:
        return load_agent_section("seed-closer", "CLOSURE GATE SUMMARY")
    except (FileNotFoundError, KeyError):
        try:
            return _truncate_tail(load_agent_section("seed-closer", "YOUR APPROACH"), 900)
        except (FileNotFoundError, KeyError):
            return (
                "- Do not treat ambiguity <= 0.2 as sufficient for closure.\n"
                "- Do not close if unresolved decisions would materially change implementation.\n"
                "- Ask the highest-impact follow-up question when a material gap remains."
            )


async def emit_subagent_dispatched_event(
    event_store: Any | None,
    *,
    session_id: str | None,
    payload: SubagentPayload,
) -> None:
    """Persist a ``subagent.dispatched`` audit event for the plugin path.

    Real execution path already records its own lifecycle events via the
    orchestrator. The plugin path hands control to a foreign process, so we
    record the dispatch here so audit / resume can see it happened.

    Failure to emit is non-fatal: logged and swallowed. The dispatch envelope
    is the user-visible result; losing the audit row must not break the
    call.

    Args:
        event_store: Optional EventStore. If None, emission is skipped.
        session_id: Session the dispatch is scoped to (may be None).
        payload: The dispatch payload being returned to the caller.
    """
    if event_store is None:
        return
    try:
        from ouroboros.events.base import BaseEvent

        aggregate_id = session_id or f"subagent-{payload.tool_name}"
        await event_store.append(
            BaseEvent(
                type="subagent.dispatched",
                aggregate_type="subagent",
                aggregate_id=aggregate_id,
                data={
                    "tool_name": payload.tool_name,
                    "title": payload.title,
                    "agent": payload.agent,
                    "model": payload.model,
                    "prompt_len": len(payload.prompt),
                    "context_keys": sorted(payload.context.keys()),
                    "session_id": session_id,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 — audit miss must not break dispatch
        log.warning(
            "subagent.dispatched.emit_failed",
            tool_name=payload.tool_name,
            session_id=session_id,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Tool-specific builders
# ---------------------------------------------------------------------------


def build_qa_subagent(
    *,
    artifact: str,
    quality_bar: str,
    artifact_type: str = "code",
    reference: str | None = None,
    pass_threshold: float = 0.80,
    qa_session_id: str | None = None,
    iteration_history: list[dict[str, Any]] | None = None,
    seed_content: str | None = None,
) -> SubagentPayload:
    """Build subagent payload for QA evaluation.

    Constructs a prompt that includes the QA judge role, artifact to evaluate,
    quality bar criteria, and instructs JSON verdict output.
    """
    from ouroboros.agents.loader import load_agent_prompt

    system_prompt = load_agent_prompt("qa-judge")

    # Build reference section
    reference_section = ""
    if reference:
        reference_section = f"\n## Reference\n```\n{reference}\n```\n"

    # Build history section
    history_section = ""
    if iteration_history:
        lines = []
        for entry in iteration_history:
            lines.append(
                f"  - Iteration {entry.get('iteration', '?')}: "
                f"score={entry.get('score', '?')}, "
                f"verdict={entry.get('verdict', '?')}"
            )
        history_section = "\n## Previous Iterations\n" + "\n".join(lines) + "\n"

    # Build seed section
    seed_section = ""
    if seed_content:
        seed_section = f"\n## Seed Specification\n```yaml\n{seed_content}\n```\n"

    prompt = f"""{system_prompt}

---

## Your Task

Evaluate the following artifact against the quality bar. Return your evaluation
as a JSON object with these exact fields:
- score (float 0.0-1.0)
- verdict ("pass", "revise", or "fail")
- dimensions (object with per-dimension float scores)
- differences (array of specific differences found)
- suggestions (array of actionable improvement suggestions)
- reasoning (string explaining your assessment)

## Quality Bar
{quality_bar}

## Pass Threshold
{pass_threshold}

## Artifact Type
{artifact_type}

## Artifact Content
```
{artifact}
```
{reference_section}{history_section}{seed_section}
Return ONLY the JSON verdict object. No other text."""

    context: dict[str, Any] = {
        "artifact": artifact,
        "quality_bar": quality_bar,
        "artifact_type": artifact_type,
        "reference": reference,
        "pass_threshold": pass_threshold,
        "qa_session_id": qa_session_id,
        "iteration_history": iteration_history,
        "seed_content": seed_content,
    }

    return build_subagent_payload(
        tool_name="ouroboros_qa",
        title="QA: evaluate artifact",
        prompt=prompt,
        context=context,
    )


def build_interview_subagent(
    *,
    session_id: str,
    action: str = "start",
    initial_context: str | None = None,
    answer: str | None = None,
    cwd: str | None = None,
    transcript: str = "",
) -> SubagentPayload:
    """Build subagent payload for Socratic interview.

    Supports start (with initial_context), answer (with user answer),
    and resume (session_id only) actions.

    Args:
        transcript: Full conversation history (Q&A pairs) for context
            continuity across subagent invocations.
    """
    from ouroboros.agents.loader import load_agent_prompt

    system_prompt = load_agent_prompt("socratic-interviewer")
    seed_closer_summary = _load_seed_closer_summary()

    transcript_section = ""
    if transcript:
        bounded_transcript = _compact_interview_transcript(transcript)
        transcript_section = f"\n## Conversation History\n{bounded_transcript}\n"

    bounded_initial_context = _truncate_head(
        initial_context,
        _INTERVIEW_SUBAGENT_MAX_CONTEXT_CHARS,
    )
    bounded_answer = _truncate_tail(answer, _INTERVIEW_SUBAGENT_MAX_ANSWER_CHARS)

    seed_ready_guard = f"""
## Seed-ready Guard
Before declaring ready, apply the canonical Seed Closer closure gate summary.
Do not treat ambiguity <= 0.2 as sufficient for closure.

{seed_closer_summary}"""

    if action == "start" and initial_context:
        prompt = f"""{system_prompt}

---

## Your Task

Start a Socratic interview to clarify requirements for the following project idea.
Ask probing questions to reduce ambiguity. Score ambiguity after each exchange.
{seed_ready_guard}

## Initial Context
{bounded_initial_context}

## Session ID
{session_id}

Begin the interview. Ask your first clarifying question."""

    elif action == "answer" and answer:
        prompt = f"""{system_prompt}

---

## Your Task

Continue the Socratic interview. The user has answered your previous question.
Analyze their answer, update your understanding, score current ambiguity,
and ask the next clarifying question or declare ready only after the Seed-ready Guard passes.
{seed_ready_guard}

## Session ID
{session_id}
{transcript_section}
## User's Latest Answer
{bounded_answer}

Continue the interview."""

    else:
        prompt = f"""{system_prompt}

---

## Your Task

Resume the Socratic interview for session {session_id}.
Review the conversation history and continue from where we left off.
{transcript_section}
{seed_ready_guard}

## Action: {action}

Continue the interview."""

    context: dict[str, Any] = {
        "session_id": session_id,
        "action": action,
        "initial_context": initial_context,
        "answer": answer,
        "cwd": cwd,
    }

    return build_subagent_payload(
        tool_name="ouroboros_interview",
        title=f"Interview: {action}",
        prompt=prompt,
        context=context,
    )


def build_generate_seed_subagent(
    *,
    session_id: str,
    ambiguity_score: float | None = None,
    transcript: str = "",
) -> SubagentPayload:
    """Build subagent payload for seed generation from interview."""
    from ouroboros.agents.loader import load_agent_prompt

    system_prompt = load_agent_prompt("seed-architect")

    ambiguity_note = ""
    if ambiguity_score is not None:
        ambiguity_note = f"\n## Current Ambiguity Score\n{ambiguity_score}\n"

    transcript_section = ""
    if transcript:
        transcript_section = f"\n## Interview Transcript\n{transcript}\n"

    prompt = f"""{system_prompt}

---

## Your Task

Generate an immutable Seed specification from the completed interview session.
The seed must contain structured requirements: goal, constraints, acceptance
criteria, ontology schema, evaluation principles, and exit conditions.

## Session ID
{session_id}
{ambiguity_note}{transcript_section}
Extract all requirements from the interview conversation and produce a
complete YAML seed specification. The seed should be precise enough for
autonomous execution."""

    context: dict[str, Any] = {
        "session_id": session_id,
        "ambiguity_score": ambiguity_score,
    }

    return build_subagent_payload(
        tool_name="ouroboros_generate_seed",
        title="Generate seed from interview",
        prompt=prompt,
        context=context,
    )


def build_evaluate_subagent(
    *,
    session_id: str,
    artifact: str,
    artifact_type: str | None = "code",
    seed_content: str | None = None,
    acceptance_criterion: str | None = None,
    working_dir: str | None = None,
    trigger_consensus: bool = False,
) -> SubagentPayload:
    """Build subagent payload for evaluation pipeline."""
    from ouroboros.agents.loader import load_agent_prompt

    system_prompt = load_agent_prompt("evaluator")

    seed_section = ""
    if seed_content:
        seed_section = f"\n## Seed Specification\n```yaml\n{seed_content}\n```\n"

    ac_section = ""
    if acceptance_criterion:
        ac_section = f"\n## Acceptance Criterion\n{acceptance_criterion}\n"

    consensus_note = ""
    if trigger_consensus:
        consensus_note = (
            "\n## Consensus Mode\n"
            "This evaluation requires multi-model consensus. "
            "Be especially rigorous and detailed in your assessment.\n"
        )

    prompt = f"""{system_prompt}

---

## Your Task

Evaluate the following artifact for compliance with acceptance criteria
and goal alignment. Provide a detailed semantic evaluation.

## Session ID
{session_id}
{seed_section}{ac_section}{consensus_note}
## Artifact Type
{artifact_type or "code"}

## Artifact
```
{artifact}
```

Provide your evaluation with pass/fail verdict and detailed reasoning."""

    context: dict[str, Any] = {
        "session_id": session_id,
        "artifact": artifact,
        "artifact_type": artifact_type,
        "seed_content": seed_content,
        "acceptance_criterion": acceptance_criterion,
        "working_dir": working_dir,
        "trigger_consensus": trigger_consensus,
    }

    return build_subagent_payload(
        tool_name="ouroboros_evaluate",
        title="Evaluate: semantic analysis",
        prompt=prompt,
        context=context,
    )


def build_execute_subagent(
    *,
    seed_content: str,
    session_id: str | None = None,
    seed_path: str | None = None,
    cwd: str | None = None,
    max_iterations: int = 10,
    skip_qa: bool = False,
    model_tier: str | None = "medium",
) -> SubagentPayload:
    """Build subagent payload for seed execution."""
    seed_path_note = ""
    if seed_path:
        seed_path_note = f"\n## Seed File Path\n{seed_path}\n"

    cwd_note = ""
    if cwd:
        cwd_note = f"\n## Working Directory\n{cwd}\n"

    qa_note = ""
    if skip_qa:
        qa_note = "\n## QA\nSkip QA after execution.\n"
    else:
        qa_note = "\n## QA\nRun QA evaluation after execution completes.\n"

    prompt = f"""## Your Task

Execute the following seed specification. Implement all requirements defined
in the seed, respecting constraints and acceptance criteria.

## Session ID
{session_id or "new"}

## Max Iterations
{max_iterations}
{seed_path_note}{cwd_note}{qa_note}
## Seed Specification
```yaml
{seed_content}
```

Implement the seed requirements. Work iteratively, testing as you go.
Stop when all acceptance criteria are met or max iterations reached."""

    context: dict[str, Any] = {
        "seed_content": seed_content,
        "session_id": session_id,
        "seed_path": seed_path,
        "cwd": cwd,
        "max_iterations": max_iterations,
        "skip_qa": skip_qa,
        "model_tier": model_tier,
    }

    return build_subagent_payload(
        tool_name="ouroboros_execute_seed",
        title="Execute: seed implementation",
        prompt=prompt,
        context=context,
    )


def build_pm_interview_subagent(
    *,
    session_id: str,
    action: str = "start",
    initial_context: str | None = None,
    answer: str | None = None,
    cwd: str | None = None,
    selected_repos: list[str] | None = None,
    transcript: str = "",
) -> SubagentPayload:
    """Build subagent payload for PM interview.

    Supports start, answer, and generate actions.

    Args:
        transcript: Full conversation history for context continuity.
    """
    from ouroboros.agents.loader import load_agent_prompt

    system_prompt = load_agent_prompt("socratic-interviewer")

    repos_section = ""
    if selected_repos:
        repos_section = (
            "\n## Selected Repositories\n" + "\n".join(f"- {r}" for r in selected_repos) + "\n"
        )

    transcript_section = ""
    if transcript:
        transcript_section = f"\n## Conversation History\n{transcript}\n"

    if action == "start" and initial_context:
        prompt = f"""{system_prompt}

---

## Your Task (PM Interview)

Start a product management interview to gather requirements for the following
project idea. Focus on user stories, priorities, MVP scope, and technical
constraints.

## Initial Context
{initial_context}
{repos_section}
## Session ID
{session_id}

Begin the PM interview. Ask your first question about product requirements."""

    elif (action == "answer" or action == "resume") and answer:
        prompt = f"""{system_prompt}

---

## Your Task (PM Interview)

Continue the PM interview. The user has answered your question.
Analyze their answer, classify requirements, and ask the next question.

## Session ID
{session_id}
{transcript_section}
## User's Latest Answer
{answer}
{repos_section}
Continue the PM interview."""

    elif action == "generate":
        prompt = f"""{system_prompt}

---

## Your Task (PM Interview - Generate Seed)

The PM interview is complete. Generate a seed specification from the
gathered requirements. Include all user stories, constraints, and
acceptance criteria discussed.

## Session ID
{session_id}
{transcript_section}{repos_section}
Generate the complete seed YAML specification."""

    else:
        prompt = f"""{system_prompt}

---

## Your Task (PM Interview)

Resume PM interview for session {session_id}.
Action: {action}
{repos_section}
Continue the PM interview."""

    context: dict[str, Any] = {
        "session_id": session_id,
        "action": action,
        "initial_context": initial_context,
        "answer": answer,
        "cwd": cwd,
        "selected_repos": selected_repos,
    }

    return build_subagent_payload(
        tool_name="ouroboros_pm_interview",
        title=f"PM Interview: {action}",
        prompt=prompt,
        context=context,
    )


# ---------------------------------------------------------------------------
# Multi-subagent (parallel) builders
# ---------------------------------------------------------------------------


def build_multi_subagent_result(
    payloads: list[SubagentPayload],
    *,
    response_shape: dict[str, Any] | None = None,
) -> Result:
    """Wrap a list of SubagentPayloads into a single MCPToolResult for parallel dispatch.

    The bridge plugin recognizes the ``_subagents`` key (plural, array) and fires
    one ``promptAsync`` per payload, resulting in N Task panes opening in
    parallel in the parent session.

    Dedupe happens at the plugin layer per-payload via prompt hash, so identical
    payloads in the same call are handled safely.

    Public-contract preservation (#442): when ``response_shape`` is provided,
    the natural tool response fields are merged into the JSON body ALONGSIDE
    ``_subagents``. Plugin still finds ``_subagents`` via ``JSON.parse``;
    consumers still find the contract fields at top level.

    Args:
        payloads: Non-empty list of SubagentPayload. Empty list is rejected.
        response_shape: Optional mapping of public-contract keys to merge into
            the response body. Must NOT contain the reserved key ``_subagents``;
            it is always overwritten by the dispatch list.

    Returns:
        Result.ok(MCPToolResult) with ``_subagents`` present in both content
        JSON and meta, alongside any caller-supplied ``response_shape`` keys.

    Raises:
        ValueError: If payloads list is empty.
    """
    if not payloads:
        raise ValueError("payloads must not be empty")

    dispatch_list = [p.to_dict() for p in payloads]
    body: dict[str, Any] = {}
    if response_shape:
        body.update(response_shape)
    body["_subagents"] = dispatch_list

    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text=json.dumps(body)),),
            is_error=False,
            meta=dict(body),
        )
    )


def build_lateral_multi_subagent(
    *,
    personas: list[str],
    problem_context: str,
    current_approach: str,
    failed_attempts: tuple[str, ...] = (),
) -> list[SubagentPayload]:
    """Build N subagent payloads — one per lateral-thinking persona.

    Each payload targets a different persona so main LLM sees N Task panes
    running in true parallel (independent LLM contexts, no anchoring bias).

    Args:
        personas: List of persona names. Duplicates are deduped (preserving
                  first-seen order). Unknown personas raise ValueError.
                  Empty list raises ValueError.
        problem_context: Description of the stuck situation.
        current_approach: What has been tried and isn't working.
        failed_attempts: Previous failed approaches shared across all panes.

    Returns:
        List of SubagentPayload, one per unique persona.

    Raises:
        ValueError: If personas empty, unknown, or required fields missing.
    """
    from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona

    if not personas:
        raise ValueError("personas must not be empty")
    if not problem_context:
        raise ValueError("problem_context must not be empty")
    if not current_approach:
        raise ValueError("current_approach must not be empty")

    # Dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in personas:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)

    # Validate + convert to enum
    enum_personas: list[ThinkingPersona] = []
    for name in unique:
        try:
            enum_personas.append(ThinkingPersona(name))
        except ValueError as e:
            raise ValueError(
                f"Unknown persona '{name}'. Valid: "
                "hacker, researcher, simplifier, architect, contrarian"
            ) from e

    thinker = LateralThinker()
    payloads: list[SubagentPayload] = []

    for persona in enum_personas:
        try:
            result = thinker.generate_alternative(
                persona=persona,
                problem_context=problem_context,
                current_approach=current_approach,
                failed_attempts=failed_attempts,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lateral_multi_subagent.persona_exception",
                persona=persona.value,
                error=str(exc),
            )
            continue

        if result.is_err:
            log.warning(
                "lateral_multi_subagent.persona_skipped",
                persona=persona.value,
                error=str(result.error),
            )
            continue

        lateral = result.unwrap()
        # Wrap the persona prompt with an explicit instruction for the
        # subagent to produce a concrete alternative plan, not just restate.
        prompt = (
            f"{lateral.prompt}\n\n"
            "---\n\n"
            "## Task for you (subagent)\n"
            f"You are thinking as the **{persona.value}** persona. Apply the "
            "instructions above to this specific problem. Produce:\n"
            "1. A concrete alternative plan (3-5 bullet steps).\n"
            "2. The single biggest assumption you challenge.\n"
            "3. A one-line verdict: would this plan work? why/why not?\n\n"
            "Keep it tight. Your output will be compared with 4 other personas "
            "thinking in parallel. Be distinctive — lean hard into your persona."
        )

        context = {
            "persona": persona.value,
            "problem_context": problem_context,
            "current_approach": current_approach,
            "failed_attempts": list(failed_attempts),
        }

        payloads.append(
            build_subagent_payload(
                tool_name="ouroboros_lateral_think",
                title=f"Lateral ({persona.value})",
                prompt=prompt,
                context=context,
            )
        )

    if not payloads:
        raise ValueError("all personas failed to generate prompts")

    return payloads


def build_evolve_subagent(
    *,
    lineage_id: str,
    seed_content: str | None = None,
    execute: bool = True,
    parallel: bool = True,
    skip_qa: bool = False,
    project_dir: str | None = None,
) -> SubagentPayload:
    """Build subagent payload for one generation of the evolutionary loop.

    Mirrors ``build_execute_subagent``: the subagent runs the generation
    end-to-end (Gen 1 = Execute → Evaluate; Gen 2+ = Wonder → Reflect →
    Execute → Evaluate) and returns a generation report.
    """
    seed_note = ""
    if seed_content:
        seed_note = f"\n## Seed Specification (Gen 1)\n```yaml\n{seed_content}\n```\n"

    project_dir_note = ""
    if project_dir:
        project_dir_note = f"\n## Project Directory\n{project_dir}\n"

    parallel_note = (
        "\n## Parallel\nExecute acceptance criteria in parallel.\n"
        if parallel
        else "\n## Parallel\nExecute acceptance criteria sequentially.\n"
    )

    qa_note = ""
    if skip_qa:
        qa_note = "\n## QA\nSkip QA after the generation completes.\n"
    else:
        qa_note = "\n## QA\nRun QA evaluation after the generation completes.\n"

    if execute:
        mode_note = "\n## Mode\nFull pipeline: Execute the seed, then Evaluate the output.\n"
    else:
        mode_note = (
            "\n## Mode\nOntology-only: skip execution and evaluation. Perform "
            "Wonder → Reflect to evolve the ontology from prior generation "
            "state.\n"
        )

    prompt = f"""## Your Task

Run exactly ONE generation of the evolutionary loop for the given lineage.

Gen 1 lifecycle (seed provided):
1. Execute(Seed) → execution_output
2. Evaluate(execution_output) → evaluation summary
3. Record generation, report convergence signal.

Gen 2+ lifecycle (no seed — reconstruct from prior generation):
1. Wonder(ontology, evaluation) → open questions
2. Reflect(seed, output, evaluation, wonder) → ontology mutations
3. Generate next Seed from reflect output
4. Execute(Seed) → execution_output
5. Evaluate(execution_output) → evaluation summary
6. Record generation, report convergence signal.

## Lineage ID
{lineage_id}
{seed_note}{mode_note}{parallel_note}{project_dir_note}{qa_note}
Return a generation report containing: generation number, phase, action
(continue / converged / stagnated / exhausted / failed), ontology similarity,
evaluation verdict, and any ontology delta (added / removed / modified
fields). Stop after one generation — the orchestrator decides whether to
call you again."""

    context: dict[str, Any] = {
        "lineage_id": lineage_id,
        "seed_content": seed_content,
        "execute": execute,
        "parallel": parallel,
        "skip_qa": skip_qa,
        "project_dir": project_dir,
    }

    return build_subagent_payload(
        tool_name="ouroboros_evolve_step",
        title="Evolve: one generation",
        prompt=prompt,
        context=context,
    )
