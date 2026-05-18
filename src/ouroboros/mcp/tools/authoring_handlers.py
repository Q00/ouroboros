"""Authoring-phase tool handlers for Ouroboros MCP server.

Contains handlers for interview and seed generation tools:
- GenerateSeedHandler: Converts completed interview sessions into immutable Seeds.
- InterviewHandler: Manages interactive requirement-clarification interviews.
"""

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.backends import backend_supports_tool_envelope
from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AUTO_COMPLETE_STREAK_REQUIRED,
    AmbiguityScore,
    AmbiguityScorer,
    ComponentScore,
    ScoreBreakdown,
    get_completion_floor_failures,
    get_milestone,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS,
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewEngine,
    InterviewState,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.config import get_clarification_model
from ouroboros.core.errors import ValidationError
from ouroboros.core.initial_context import resolve_initial_context_input
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.subagent import (
    build_generate_seed_subagent,
    build_interview_subagent,
    build_subagent_result,
    emit_subagent_dispatched_event,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_runtime_builtin_tool_names,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter, resolve_llm_backend
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)

_DATA_DIR = Path.home() / ".ouroboros" / "data"

# Strict format for caller-supplied ``interview_id`` arguments — matches
# the server's own generation pattern (``f"interview_{uuid4().hex[:16]}"``)
# so external clients cannot inject arbitrary identifiers.  See
# Q00/ouroboros#723 review.
_SUGGESTED_INTERVIEW_ID_RE = re.compile(r"^interview_[a-f0-9]{16}$")

_LIVE_AMBIGUITY_MAX_RETRIES = 3

REQUIRED_CLIENT_GATES: tuple[str, ...] = (
    # TODO(#1008): derive required gate names from the interview skill /
    # backend capability registry once non-skippable gates have a structured
    # source of truth instead of a Markdown-only checklist.
    "seed_ready_acceptance_guard",
    "restate_goal_approved",
)
_REQUIRE_CLIENT_GATES_ENV = "OUROBOROS_REQUIRE_CLIENT_GATES"


def _normalize_client_gates(value: Any) -> frozenset[str]:
    """Normalize caller-reported client-side gate acknowledgements."""
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())
    return frozenset()


def get_client_gate_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return required/accepted/missing client gate metadata for seed generation."""
    accepted = _normalize_client_gates(arguments.get("client_gates"))
    missing = tuple(gate for gate in REQUIRED_CLIENT_GATES if gate not in accepted)
    status: dict[str, Any] = {
        "required_client_gates": REQUIRED_CLIENT_GATES,
        "accepted_client_gates": tuple(sorted(accepted)),
        "missing_client_gates": missing,
    }
    if missing:
        status["client_gate_warning"] = (
            "Seed generation was requested without all client-side interview gates "
            "being acknowledged. The client should run the Seed-ready Acceptance Guard "
            "and Restate gate, then pass client_gates with the acknowledged gate names."
        )
    return status


def _require_client_gates_enabled() -> bool:
    """Return True when missing client gates should hard-block generation."""
    return os.environ.get(_REQUIRE_CLIENT_GATES_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _client_gate_error(gate_status: dict[str, Any]) -> MCPToolError | None:
    missing = gate_status.get("missing_client_gates")
    if not missing or not _require_client_gates_enabled():
        return None
    missing_display = ", ".join(str(item) for item in missing)
    return MCPToolError(
        "Seed generation requires acknowledged client-side interview gates "
        f"when {_REQUIRE_CLIENT_GATES_ENV}=1. Missing: {missing_display}.",
        tool_name="ouroboros_generate_seed",
    )


def _client_gate_warning_text(gate_status: dict[str, Any]) -> str:
    warning = gate_status.get("client_gate_warning")
    missing = gate_status.get("missing_client_gates")
    if not isinstance(warning, str) or not missing:
        return ""
    missing_display = ", ".join(str(item) for item in missing)
    return f"Client Gate Warning: {warning} Missing: {missing_display}.\n\n"


_INTERVIEW_COMPLETION_SIGNALS = {
    "done",
    "complete",
    "stop",
    "enough",
    "generate seed",
    "create seed",
    "seed",
}

_INTERVIEW_COMPLETION_PHRASES = (
    "close the interview",
    "close interview",
    "close now",
    "mark the interview complete",
    "mark interview complete",
    "generate the seed",
    "create the seed",
    "seed generation",
    "ready for seed generation",
    "hand off for seed generation",
    "no remaining ambiguity",
    "no ambiguity remains",
    "no ambiguity left",
)


def _interview_allowed_tools(runtime_backend: str | None) -> list[str] | None:
    """Return the policy-derived read-only tool envelope for interviews."""
    effective_backend = resolve_llm_backend(runtime_backend)
    if not backend_supports_tool_envelope(effective_backend):
        return None
    return allowed_runtime_builtin_tool_names(
        PolicyContext(
            runtime_backend=effective_backend,
            session_role=PolicySessionRole.INTERVIEW,
            execution_phase=PolicyExecutionPhase.INTERVIEW,
        )
    )


_INTERVIEW_COMPLETION_NEGATIONS = (
    "not done",
    "not complete",
    "not enough",
    "not ready",
    "do not close",
    "dont close",
    "don't close",
)


def _normalize_interview_answer(answer: str) -> str:
    """Normalize interview answers for lightweight intent matching."""
    return " ".join(re.findall(r"[a-z0-9']+", answer.lower()))


def _is_interview_completion_signal(answer: str | None) -> bool:
    """Return True when the answer explicitly asks to end the interview.

    Only ``[from-user]`` and prefix-less answers represent human intent to close.
    The one deterministic non-human exception is the auto driver's
    ``[from-auto][safe-default-synthesis]`` payload, which is emitted only after
    the driver has already accepted auditable safe defaults for the remaining
    required gaps and must close the persisted interview in the same turn.

    The auto driver's ordinary ``_feature_acceptance_answer`` echoes the LLM question into
    its answer text, which can accidentally include phrases like "no remaining
    ambiguity" and trip the shortfall branch. Gate the heuristic by prefix so
    ``[from-auto]`` / ``[from-code]`` / ``[from-research]`` answers — which carry
    facts or auto-generated fillers, not user intent — never enter completion.

    If a new ``[from-<source>]`` prefix is introduced in
    ``skills/interview/SKILL.md`` (or the LLM-facing prompt at
    ``bigbang/interview.py``), audit whether that source represents direct human
    intent and update the guard below — otherwise it is silently default-denied,
    which is safe (the user can still close with a prefix-less ``"done"``) but
    may surprise callers.
    """
    if answer is None:
        return False

    stripped = answer.lstrip().lower()
    # Auto-generated answers normally must not express user intent to close, but
    # the safe-default finalizer is the one deterministic exception: the auto
    # driver has already decided the remaining required gaps are conservative
    # defaults and now needs the persisted interview session to close in the
    # same turn that records those defaults.  Keep this allowance tied to the
    # explicit synthesis tag so ordinary ``[from-auto]`` answers remain guarded.
    allow_auto_safe_default_completion = stripped.startswith("[from-auto][safe-default-synthesis]")
    if (
        stripped.startswith("[from-")
        and not stripped.startswith("[from-user]")
        and not allow_auto_safe_default_completion
    ):
        return False

    normalized = _normalize_interview_answer(answer)
    if not normalized:
        return False

    if normalized in _INTERVIEW_COMPLETION_SIGNALS:
        return True

    if any(phrase in normalized for phrase in _INTERVIEW_COMPLETION_NEGATIONS):
        return False

    if any(phrase in normalized for phrase in _INTERVIEW_COMPLETION_PHRASES):
        return True

    tokens = set(normalized.split())
    if {"close", "interview"} <= tokens:
        return True
    if "seed" in tokens and tokens.intersection({"generate", "create", "ready"}):
        return True
    if "ambiguity" in tokens and "no" in tokens and tokens.intersection({"remaining", "left"}):
        return True
    return normalized.endswith(" done") or normalized == "done"


def _count_answered_rounds(state: InterviewState) -> int:
    """Return the number of completed interview rounds."""
    return sum(1 for round_data in state.rounds if round_data.user_response is not None)


def _reset_stale_completion_streak(state: InterviewState) -> None:
    """Invalidate any accrued completion streak.

    Single source of truth for the "stale streak invalidation" rule
    shared by every path that observes a non-qualifying signal (scorer
    error, weak live rescore, non-qualifying normal answer). Keeping the
    reset in one helper makes the two-signal completion contract
    stateless across flows: a stored streak only survives a signal that
    was itself qualifying.
    """
    if state.completion_candidate_streak != 0:
        state.completion_candidate_streak = 0
        state.mark_updated()


def _update_completion_candidate_streak(
    state: InterviewState,
    score: AmbiguityScore,
) -> bool:
    """Update and return whether the current score qualifies for auto-completion."""
    qualifies = qualifies_for_seed_completion(score, is_brownfield=state.is_brownfield)
    if qualifies:
        state.completion_candidate_streak += 1
    else:
        _reset_stale_completion_streak(state)
    return qualifies


def _completion_gate_reason(
    score: AmbiguityScore | None,
    *,
    is_brownfield: bool,
) -> str:
    """Describe the strongest reason interview completion is still blocked."""
    if score is None:
        return f"ambiguity score could not be confirmed against threshold {AMBIGUITY_THRESHOLD:.2f}"
    if score.overall_score > AMBIGUITY_THRESHOLD:
        return (
            f"ambiguity score {score.overall_score:.2f} exceeds threshold {AMBIGUITY_THRESHOLD:.2f}"
        )

    floor_failures = get_completion_floor_failures(score, is_brownfield=is_brownfield)
    if floor_failures:
        return f"completion floors are unmet ({'; '.join(floor_failures)})"

    return "requirements are not stable enough to close yet"


def _milestone_for_score(score: AmbiguityScore | None) -> str | None:
    """Return the milestone label for an ambiguity score, or None."""
    if score is None:
        return None
    milestone, _ = get_milestone(score.overall_score)
    return milestone.value


_MILESTONE_RANKS = {
    "initial": 0,
    "progress": 1,
    "refined": 2,
    "ready": 3,
}


def _maybe_record_lateral_review_advisory(
    state: InterviewState,
    *,
    previous_milestone: str | None,
    score: AmbiguityScore | None,
) -> dict[str, Any] | None:
    """Return advisory meta for a first-time forward milestone transition.

    This helper is intentionally deterministic and side-effect limited: it
    records that an advisory was emitted for the target milestone, but it does
    not invoke lateral thinking, block question generation, or alter the
    interview answer/Seed contract.
    """
    current_milestone = _milestone_for_score(score)
    if previous_milestone is None or current_milestone is None:
        return None

    previous_rank = _MILESTONE_RANKS.get(previous_milestone)
    current_rank = _MILESTONE_RANKS.get(current_milestone)
    if previous_rank is None or current_rank is None or current_rank <= previous_rank:
        return None

    if current_milestone in state.lateral_review_advised_milestones:
        return None

    state.note_lateral_review_advisory(current_milestone)
    return {
        "lateral_review_recommended": True,
        "lateral_review_milestone": current_milestone,
        "lateral_review_from_milestone": previous_milestone,
        "lateral_review_reason": "first_forward_milestone_transition",
    }


def _compute_transcript_chars(state: InterviewState) -> int:
    """Sum question + user_response length over every round in ``state``.

    Used by the response-shape diagnostic event (Q00/ouroboros#831) so we
    can correlate hang reports with cumulative interview context size.
    """
    total = 0
    for round_data in state.rounds:
        total += len(round_data.question or "")
        total += len(round_data.user_response or "")
    return total


def _format_question_with_ambiguity(question: str, score: AmbiguityScore | None) -> str:
    """Attach the current ambiguity score to a question for display.

    The text format uses ``(ambiguity: <score>)`` without the milestone
    label to preserve backward compatibility with downstream consumers
    that parse the score via regex.  Milestone data is available in the
    structured ``meta.milestone`` field of the MCP response.
    """
    if score is None:
        return question
    return f"(ambiguity: {score.overall_score:.2f}) {question}"


def _is_initial_context_length_guard_question(question: str) -> bool:
    """Return True when ``question`` is the length-guard meta-directive.

    The interview engine surfaces a fixed string as the "next question" when
    ``initial_context`` exceeds ``MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS``.
    That string is not a real interview question — it asks the *caller* to
    re-send a shorter summary — so MCP responses carrying it must carry a
    distinguishing meta signal.  See Q00/ouroboros#831.
    """
    return question == INITIAL_CONTEXT_SUMMARY_QUESTION


def _length_guard_meta_fields() -> dict[str, Any]:
    """Return the meta keys that mark a length-guard response.

    The handler merges these into the existing ``meta`` dict (``session_id``,
    ``ambiguity_score``, etc.) when the returned question is the length-guard
    meta-directive.  Clients can branch on ``meta.reason`` to handle the case
    programmatically instead of mis-routing the question to a human via
    AskUserQuestion.
    """
    return {
        "recoverable": True,
        "reason": "initial_context_too_large",
        "expected_action": "resend_with_summary",
        "max_chars": MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS,
    }


def _ambiguity_warning_for_failed_question(
    score: AmbiguityScore | None,
    *,
    is_brownfield: bool = False,
) -> str:
    """Build an explicit ambiguity warning for question-generation failures.

    When question generation fails mid-interview, the main session must NOT
    assume the interview is complete.
    See: https://github.com/Q00/ouroboros/issues/210
    """
    if score is None:
        return (
            "\n\nWARNING: Ambiguity score is unknown. "
            "The interview is NOT complete — do NOT generate a Seed. "
            "Resume the interview to continue clarifying requirements."
        )
    if not score.is_ready_for_seed:
        return (
            f"\n\nWARNING: Current ambiguity is {score.overall_score:.2f} "
            f"(threshold: {AMBIGUITY_THRESHOLD}). "
            f"The interview is NOT complete — do NOT generate a Seed. "
            f"Resume the interview to continue clarifying requirements."
        )
    floor_failures = get_completion_floor_failures(score, is_brownfield=is_brownfield)
    if floor_failures:
        return (
            "\n\nWARNING: Ambiguity is low, but completion floors are unmet "
            f"({'; '.join(floor_failures)}). "
            "The interview is NOT complete — do NOT generate a Seed. "
            "Resume the interview to continue clarifying requirements."
        )
    return ""


_INTERVIEW_EVENT_ERROR_DETAIL_KEYS = (
    "error_type",
    "session_id",
    "failure_category",
    "auth_plane",
    "openai_responses_endpoint_seen",
    "returncode",
    "subtype",
    "stop_reason",
    "partial_rejected",
    "content_length",
    "timeout_seconds",
    "attempt",
    "depth",
)

_INTERVIEW_EVENT_POSIX_PATH_RE = re.compile(
    r"(^|[\s,;:='\"`(<{\[])"
    r"("
    r"~[/\\][^\s,;'\"\]}]+"
    r"|/(?!api(?:/|$))[^\s,;'\"\]}]+"
    r")"
)
_INTERVIEW_EVENT_URL_RE = re.compile(r"https?://[^\s,;:'\")\]}]+")
_INTERVIEW_EVENT_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
_INTERVIEW_EVENT_PATH_REDACTION = "[redacted path]"


def _redact_interview_event_posix_path(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_INTERVIEW_EVENT_PATH_REDACTION}"


def _redact_interview_event_windows_paths(text: str) -> str:
    """Redact Windows drive paths without consuming unrelated sentence tails."""
    redacted: list[str] = []
    cursor = 0
    while match := _INTERVIEW_EVENT_WINDOWS_DRIVE_RE.search(text, cursor):
        redacted.append(text[cursor : match.start()])
        index = match.end()
        while index < len(text):
            char = text[index]
            if char in "\r\n,;:'\")]}":
                break
            if char.isspace():
                next_space = index + 1
                while next_space < len(text) and not text[next_space].isspace():
                    if text[next_space] in "\r\n,;:'\")]}":
                        break
                    next_space += 1
                if "\\" not in text[index + 1 : next_space]:
                    break
            index += 1
        redacted.append(_INTERVIEW_EVENT_PATH_REDACTION)
        cursor = index
    redacted.append(text[cursor:])
    return "".join(redacted)


def _redact_interview_event_error_text(text: str) -> str:
    """Remove local path-shaped substrings from persisted interview event text."""
    urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"__INTERVIEW_EVENT_URL_{len(urls) - 1}__"

    protected = _INTERVIEW_EVENT_URL_RE.sub(protect_url, text)
    redacted = _redact_interview_event_windows_paths(protected)
    redacted = _INTERVIEW_EVENT_POSIX_PATH_RE.sub(_redact_interview_event_posix_path, redacted)
    for index, url in enumerate(urls):
        redacted = redacted.replace(f"__INTERVIEW_EVENT_URL_{index}__", url)
    return redacted


def _format_interview_failure_event_error(error: Any) -> str:
    """Return an event-safe error string for interview failure events.

    Provider errors can carry rich machine diagnostics in ``details``.  Those
    details are intentionally useful for structured callers, but event text is a
    persisted/user-adjacent surface.  Render only the provider message plus an
    explicit allowlist of scalar, non-path diagnostic fields; keep path-bearing
    fields such as ``cwd`` and ``configured_cli_path`` in the original error
    object for internal diagnostics only.
    """
    message = getattr(error, "message", None)
    if not isinstance(message, str) or not message:
        message = str(error)
    rendered = [_redact_interview_event_error_text(message)]

    details = getattr(error, "details", None)
    if isinstance(details, dict):
        for key in _INTERVIEW_EVENT_ERROR_DETAIL_KEYS:
            value = details.get(key)
            if value is None or isinstance(value, dict | list | tuple | set):
                continue
            rendered.append(f"{key}: {_redact_interview_event_error_text(str(value))}")

    provider = getattr(error, "provider", None)
    if provider:
        rendered.append(f"provider: {_redact_interview_event_error_text(str(provider))}")
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        rendered.append(f"status_code: {status_code}")

    return "\n".join(rendered)


def _load_state_ambiguity_score(state: InterviewState) -> AmbiguityScore | None:
    """Rebuild a stored ambiguity snapshot from interview state."""
    if state.ambiguity_score is None:
        return None

    if isinstance(state.ambiguity_breakdown, dict):
        try:
            breakdown = ScoreBreakdown.model_validate(state.ambiguity_breakdown)
        except PydanticValidationError:
            log.warning(
                "mcp.tool.interview.invalid_stored_ambiguity_breakdown",
                session_id=state.interview_id,
            )
        else:
            return AmbiguityScore(
                overall_score=state.ambiguity_score,
                breakdown=breakdown,
            )

    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="goal_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.40,
            justification="Loaded from stored interview ambiguity score",
        ),
        constraint_clarity=ComponentScore(
            name="constraint_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.30,
            justification="Loaded from stored interview ambiguity score",
        ),
        success_criteria_clarity=ComponentScore(
            name="success_criteria_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.30,
            justification="Loaded from stored interview ambiguity score",
        ),
    )
    return AmbiguityScore(
        overall_score=state.ambiguity_score,
        breakdown=breakdown,
    )


def _stored_ambiguity_snapshot_is_degraded(state: InterviewState) -> bool:
    """Return True when stored ambiguity data lacks a parseable breakdown."""
    if state.ambiguity_score is None:
        return True
    if not isinstance(state.ambiguity_breakdown, dict):
        return True

    try:
        ScoreBreakdown.model_validate(state.ambiguity_breakdown)
    except PydanticValidationError:
        return True

    return False


def _format_interview_transcript(state: InterviewState) -> str:
    """Format persisted interview rounds as a readable transcript for subagent context."""
    if not state.rounds:
        return ""
    lines: list[str] = []
    if state.initial_context:
        lines.append(f"**Initial Context:** {state.initial_context}")
        lines.append("")
    for r in state.rounds:
        lines.append(f"**Q{r.round_number}:** {r.question}")
        if r.user_response:
            lines.append(f"**A{r.round_number}:** {r.user_response}")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _plugin_save_state(state_dir: Path, state: InterviewState) -> Result[Path, str]:
    """Persist interview state without needing InterviewEngine (no LLM dep).

    Used exclusively in the plugin dispatch path where litellm may not be
    installed. Returns Result so callers can propagate persistence failures.
    """
    try:
        file_path = state_dir / f"interview_{state.interview_id}.json"
        state.mark_updated()
        content = state.model_dump_json(indent=2)

        def _sync_write() -> None:
            file_path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_sync_write)
        return Result.ok(file_path)
    except (OSError, ValueError) as e:
        return Result.err(f"Failed to save interview state: {e}")


async def _plugin_load_state(state_dir: Path, interview_id: str) -> Result[InterviewState, str]:
    """Load interview state without needing InterviewEngine (no LLM dep).

    Used exclusively in the plugin dispatch path.
    """
    file_path = state_dir / f"interview_{interview_id}.json"
    if not file_path.exists():
        return Result.err(f"Interview state not found: {interview_id}")
    try:

        def _sync_read() -> str:
            return file_path.read_text(encoding="utf-8")

        content = await asyncio.to_thread(_sync_read)
        state = InterviewState.model_validate_json(content)
        return Result.ok(state)
    except (OSError, ValueError) as e:
        return Result.err(f"Failed to load interview state: {e}")


@dataclass
class GenerateSeedHandler:
    """Handler for the ouroboros_generate_seed tool.

    Converts a completed interview session into an immutable Seed specification.
    The seed generation gates on ambiguity score (must be <= 0.2).
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    seed_generator: SeedGenerator | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    def _build_ambiguity_score_from_value(self, ambiguity_score_value: float) -> AmbiguityScore:
        """Build an ambiguity score object from an explicit numeric override."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="goal_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.40,
                justification="Provided as input parameter",
            ),
            constraint_clarity=ComponentScore(
                name="constraint_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.30,
                justification="Provided as input parameter",
            ),
            success_criteria_clarity=ComponentScore(
                name="success_criteria_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.30,
                justification="Provided as input parameter",
            ),
        )
        return AmbiguityScore(
            overall_score=ambiguity_score_value,
            breakdown=breakdown,
        )

    def _load_stored_ambiguity_score(self, state: InterviewState) -> AmbiguityScore | None:
        """Load a persisted ambiguity score snapshot from interview state."""
        if state.ambiguity_score is None:
            return None

        if isinstance(state.ambiguity_breakdown, dict):
            try:
                breakdown = ScoreBreakdown.model_validate(state.ambiguity_breakdown)
            except PydanticValidationError:
                log.warning(
                    "mcp.tool.generate_seed.invalid_stored_ambiguity_breakdown",
                    session_id=state.interview_id,
                )
            else:
                return AmbiguityScore(
                    overall_score=state.ambiguity_score,
                    breakdown=breakdown,
                )

        return self._build_ambiguity_score_from_value(state.ambiguity_score)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_generate_seed",
            description=(
                "Generate an immutable Seed from a completed interview session. "
                "The seed contains structured requirements (goal, constraints, acceptance criteria) "
                "extracted from the interview conversation. Generation requires ambiguity_score <= 0.2."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Interview session ID to convert to a seed",
                    required=True,
                ),
                MCPToolParameter(
                    name="ambiguity_score",
                    type=ToolInputType.NUMBER,
                    description=(
                        "Ambiguity score for the interview (0.0 = clear, 1.0 = ambiguous). "
                        "Required if interview didn't calculate it. Generation fails if > 0.2."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="client_gates",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Client-side interview gates acknowledged before seed generation. "
                        "Expected values include seed_ready_acceptance_guard and "
                        "restate_goal_approved."
                    ),
                    required=False,
                    items={"type": "string"},
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a seed generation request.

        Args:
            arguments: Tool arguments including session_id and optional ambiguity_score.

        Returns:
            Result containing generated Seed YAML or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_generate_seed",
                )
            )

        ambiguity_score_value = arguments.get("ambiguity_score")
        client_gate_status = get_client_gate_status(arguments)
        client_gate_error = _client_gate_error(client_gate_status)
        if client_gate_error is not None:
            return Result.err(client_gate_error)

        log.info(
            "mcp.tool.generate_seed",
            session_id=session_id,
            ambiguity_score=ambiguity_score_value,
        )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Plugin mode: validate interview readiness server-side before
            # delegating.  The subprocess path loads state + computes/checks
            # ambiguity via litellm.  Plugin can't compute ambiguity (no
            # litellm), so we accept three evidence sources:
            #   1. Caller-supplied ambiguity_score (from parent LLM output)
            #   2. Persisted score on state (set by prior subprocess run)
            #   3. Round count — at least one answered round means the
            #      interview happened; the child validates completeness.
            state_dir = self.data_dir or _DATA_DIR
            load_result = await _plugin_load_state(state_dir, session_id)
            if load_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to load interview state: {load_result.error}",
                        tool_name="ouroboros_generate_seed",
                    )
                )
            interview_state = load_result.value

            # Determine best available ambiguity score for the gate.
            _THRESHOLD = 0.2
            effective_score = ambiguity_score_value  # caller-supplied
            if effective_score is None:
                effective_score = interview_state.ambiguity_score  # persisted

            if not interview_state.is_complete:
                answered_rounds = [r for r in interview_state.rounds if r.user_response is not None]
                if effective_score is not None:
                    # Have a score — enforce threshold
                    if effective_score > _THRESHOLD:
                        return Result.err(
                            MCPToolError(
                                f"Ambiguity score {effective_score:.2f} exceeds "
                                f"threshold {_THRESHOLD}. Continue interviewing "
                                f"to reduce ambiguity before seed generation.",
                                tool_name="ouroboros_generate_seed",
                            )
                        )
                elif not answered_rounds:
                    # No score AND no rounds — nothing to generate from
                    return Result.err(
                        MCPToolError(
                            "Interview has no answered rounds and no ambiguity "
                            "score. Complete at least one interview round before "
                            "generating a seed.",
                            tool_name="ouroboros_generate_seed",
                        )
                    )

            transcript = _format_interview_transcript(interview_state)

            payload = build_generate_seed_subagent(
                session_id=session_id,
                ambiguity_score=effective_score,
                transcript=transcript,
                client_gates=client_gate_status["accepted_client_gates"],
            )
            await emit_subagent_dispatched_event(
                self.event_store,
                session_id=session_id,
                payload=payload,
            )
            return build_subagent_result(
                payload,
                response_shape={
                    "session_id": session_id,
                    "status": "delegated_to_subagent",
                    "dispatch_mode": "plugin",
                    **client_gate_status,
                },
            )

        # Fall-through: real in-process seed generation (subprocess / non-opencode runtimes).

        try:
            # Use injected or create services.
            # ``allowed_tools=[]`` paired with ``max_turns=1``: any tool-use
            # block emitted by the model would consume the only allowed turn
            # and the SDK then raises ``Reached maximum number of turns (1)``
            # before a final text response can stream. See issue #781.
            llm_adapter = self.llm_adapter or create_llm_adapter(
                backend=self.llm_backend,
                max_turns=1,
                allowed_tools=[]
                if backend_supports_tool_envelope(resolve_llm_backend(self.llm_backend))
                else None,
            )
            interview_engine = self.interview_engine or InterviewEngine(
                llm_adapter=llm_adapter,
                model=get_clarification_model(self.llm_backend),
            )

            # Load interview state
            state_result = await interview_engine.load_state(session_id)

            if state_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to load interview state: {state_result.error}",
                        tool_name="ouroboros_generate_seed",
                    )
                )

            state: InterviewState = state_result.value

            # Always use a trusted ambiguity score: persisted snapshot or
            # freshly computed.  The caller-supplied ``ambiguity_score``
            # parameter is intentionally ignored to prevent LLM callers
            # from overriding the gate with an arbitrary low value.
            # See: https://github.com/Q00/ouroboros/issues/210
            if ambiguity_score_value is not None:
                log.warning(
                    "mcp.tool.generate_seed.ignoring_caller_ambiguity_score",
                    session_id=session_id,
                    caller_value=ambiguity_score_value,
                )

            ambiguity_score = self._load_stored_ambiguity_score(state)
            if ambiguity_score is None:
                scorer = AmbiguityScorer(
                    llm_adapter=llm_adapter,
                )
                score_result = await scorer.score(state)
                if score_result.is_err:
                    return Result.err(
                        MCPToolError(
                            f"Failed to calculate ambiguity: {score_result.error}",
                            tool_name="ouroboros_generate_seed",
                        )
                    )

                ambiguity_score = score_result.value
                state.store_ambiguity(
                    score=ambiguity_score.overall_score,
                    breakdown=ambiguity_score.breakdown.model_dump(mode="json"),
                )
                save_result = await interview_engine.save_state(state)
                if save_result.is_err:
                    log.warning(
                        "mcp.tool.generate_seed.persist_ambiguity_failed",
                        session_id=session_id,
                        error=str(save_result.error),
                    )

            # Use injected or create seed generator
            generator = self.seed_generator or SeedGenerator(
                llm_adapter=llm_adapter,
                model=get_clarification_model(self.llm_backend),
            )

            # Generate seed
            seed_result = await generator.generate(state, ambiguity_score)

            if seed_result.is_err:
                error = seed_result.error
                if isinstance(error, ValidationError):
                    return Result.err(
                        MCPToolError(
                            f"Validation error: {error}",
                            tool_name="ouroboros_generate_seed",
                        )
                    )
                return Result.err(
                    MCPToolError(
                        f"Failed to generate seed: {error}",
                        tool_name="ouroboros_generate_seed",
                    )
                )

            seed = seed_result.value

            # Convert seed to YAML
            seed_dict = seed.to_dict()
            seed_yaml = yaml.dump(
                seed_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

            result_text = (
                _client_gate_warning_text(client_gate_status) + f"Seed Generated Successfully\n"
                f"=========================\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Interview ID: {seed.metadata.interview_id}\n"
                f"Ambiguity Score: {seed.metadata.ambiguity_score:.2f}\n"
                f"Goal: {seed.goal}\n\n"
                f"--- Seed YAML ---\n"
                f"{seed_yaml}"
            )

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta={
                        "seed_id": seed.metadata.seed_id,
                        "interview_id": seed.metadata.interview_id,
                        "ambiguity_score": seed.metadata.ambiguity_score,
                        **client_gate_status,
                    },
                )
            )

        except Exception as e:
            log.error("mcp.tool.generate_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed generation failed: {e}",
                    tool_name="ouroboros_generate_seed",
                )
            )


@dataclass
class InterviewHandler:
    """Handler for the ouroboros_interview tool.

    Manages interactive interviews for requirement clarification.
    Supports starting new interviews, resuming existing sessions,
    and recording responses to questions.
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)
    suppress_tool_use_prompt_cues: bool = False

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._owns_event_store = self.event_store is None
        self._event_store = self.event_store or EventStore()
        self._initialized = False
        self._closed = False
        self._bg_tasks: set[asyncio.Task] = set()

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def _drain_bg_tasks(self, timeout: float = 5.0) -> None:
        """Await all pending background event tasks before shutdown."""
        if not self._bg_tasks:
            return
        tasks = list(self._bg_tasks)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        # Await cancelled tasks so CancelledError propagates cleanly
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._bg_tasks.clear()

    async def close(self) -> None:
        """Drain pending event tasks, then close the event store if owned."""
        self._closed = True
        await self._drain_bg_tasks()
        if self._owns_event_store:
            await self._event_store.close()
            self._initialized = False

    def resolved_state_dir(self) -> Path:
        """Return the directory the handler actually writes interview state to.

        Single source of truth for the interview persistence location used
        by collision checks, the auto-driver persistence probe, and the
        plugin/subprocess save paths.  When an ``InterviewEngine`` is
        injected (e.g. by ``create_ouroboros_server`` with a custom
        ``state_dir``), the engine's directory wins — the handler's own
        ``data_dir`` may be stale or unset.  See Q00/ouroboros#723 review.
        """
        if self.interview_engine is not None:
            return self.interview_engine.state_dir
        return self.data_dir or _DATA_DIR

    async def _emit_event(self, event: Any) -> None:
        """Emit event to store. Swallows errors to not break interview flow."""
        try:
            await self._ensure_initialized()
            await self._event_store.append(event)
        except Exception as e:
            log.warning("mcp.tool.interview.event_emission_failed", error=str(e))

    def _emit_event_bg(self, event: Any) -> None:
        """Fire-and-forget event emission — non-blocking on the hot path."""
        if self._closed:
            return
        task = asyncio.create_task(self._emit_event(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _score_interview_state(
        self,
        llm_adapter: LLMAdapter,
        state: InterviewState,
        *,
        advance_streak: bool = True,
        reset_on_failure: bool = True,
    ) -> AmbiguityScore | None:
        """Calculate and cache the latest ambiguity snapshot for interview routing.

        The streak update is controlled by two orthogonal flags so callers
        can disable the qualifying-score increment without also disabling
        the failure/non-qualifying invalidation — a requirement of the
        explicit ``done`` branch, which owns the increment itself but still
        relies on the scorer to reset a stale streak when the live rescore
        is weak or fails.

        Args:
            advance_streak: When ``True`` (default) a qualifying score
                bumps ``state.completion_candidate_streak`` by one. Set to
                ``False`` on paths that own the increment themselves (the
                explicit ``done`` branch) so the streak is not double-
                bumped between this helper and the caller.
            reset_on_failure: When ``True`` (default) a scorer error or a
                non-qualifying score clears any existing streak — the
                single shared "stale-streak invalidation" rule across all
                explicit-done and normal-answer flows. Only pass ``False``
                if a caller needs to observe the current streak across a
                transient scoring failure without losing it; in practice
                no live caller does.
        """
        scorer = AmbiguityScorer(
            llm_adapter=llm_adapter,
            model=get_clarification_model(self.llm_backend),
            max_retries=_LIVE_AMBIGUITY_MAX_RETRIES,
        )
        score_result = await scorer.score(state)
        if score_result.is_err:
            state.clear_stored_ambiguity()
            if reset_on_failure:
                _reset_stale_completion_streak(state)
            log.warning(
                "mcp.tool.interview.live_ambiguity_failed",
                interview_id=state.interview_id,
                error=str(score_result.error),
            )
            return None

        score = score_result.value
        qualifies = qualifies_for_seed_completion(
            score,
            is_brownfield=state.is_brownfield,
        )
        if advance_streak:
            # Standard routing: single helper owns both bump-on-qualify
            # and reset-on-fail so the two are never out of sync.
            _update_completion_candidate_streak(state, score)
        elif reset_on_failure and not qualifies:
            # Explicit-done path (or any caller that owns the increment):
            # we still MUST share the stale-streak reset contract so a
            # weak rescore cannot let a stored streak survive.
            _reset_stale_completion_streak(state)
        state.store_ambiguity(
            score=score.overall_score,
            breakdown=score.breakdown.model_dump(mode="json"),
        )
        return score

    @staticmethod
    def _ambiguity_gate_response(
        session_id: str,
        score: AmbiguityScore | None,
        *,
        is_brownfield: bool,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Build an MCP response refusing premature interview completion."""
        score_display = f"{score.overall_score:.2f}" if score is not None else "unknown"
        gate_reason = _completion_gate_reason(score, is_brownfield=is_brownfield)
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Cannot complete yet — {gate_reason}. "
                            f"Current ambiguity: {score_display}. "
                            f"Please answer a few more questions to "
                            f"clarify remaining areas."
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "ambiguity_score": (score.overall_score if score is not None else None),
                    "milestone": _milestone_for_score(score),
                    "seed_ready": False,
                },
            )
        )

    async def _complete_interview_response(
        self,
        engine: InterviewEngine,
        state: InterviewState,
        session_id: str,
        score: AmbiguityScore | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Complete the interview and return a Seed-ready MCP response."""
        complete_result = await engine.complete_interview(state)
        if complete_result.is_err:
            return Result.err(
                MCPToolError(
                    str(complete_result.error),
                    tool_name="ouroboros_interview",
                )
            )

        state = complete_result.value
        save_result = await engine.save_state(state)
        if save_result.is_err:
            log.warning(
                "mcp.tool.interview.save_failed_on_complete",
                error=str(save_result.error),
            )

        from ouroboros.events.interview import interview_completed

        self._emit_event_bg(
            interview_completed(
                interview_id=session_id,
                total_rounds=len(state.rounds),
            )
        )

        score_line = ""
        if score is not None:
            score_line = f"(ambiguity: {score.overall_score:.2f}) Ready for Seed generation.\n"

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Interview completed. Session ID: {session_id}\n\n"
                            f"{score_line}"
                            f'Generate a Seed with: session_id="{session_id}"'
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "completed": True,
                    "ambiguity_score": score.overall_score if score is not None else None,
                    "milestone": _milestone_for_score(score),
                    "seed_ready": score.is_ready_for_seed if score is not None else None,
                    "required_client_gates": REQUIRED_CLIENT_GATES,
                },
            )
        )

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_interview",
            description=(
                "Interactive interview for requirement clarification. "
                "Start a new interview with initial_context, resume with session_id, "
                "or record an answer to the current question. "
                "In plugin mode, returns a delegation receipt "
                "(status=delegated_to_subagent) and the interview executes in an "
                "OpenCode Task pane — the real session_id is returned there."
            ),
            parameters=(
                MCPToolParameter(
                    name="initial_context",
                    type=ToolInputType.STRING,
                    description="Initial context to start a new interview session",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Session ID to resume an existing interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="answer",
                    type=ToolInputType.STRING,
                    description="Response to the current interview question",
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description=(
                        "Working directory for brownfield auto-detection. "
                        "Defaults to the current working directory if not provided."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="last_question",
                    type=ToolInputType.STRING,
                    description=(
                        "The question text from the previous child session's response. "
                        "In plugin mode each dispatch creates a new child session whose "
                        "questions are not automatically persisted server-side. Pass the "
                        "child's last question here when submitting an answer so the "
                        "interview transcript preserves the real question text instead "
                        "of a placeholder."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="interview_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional caller-supplied id for a brand-new interview. "
                        "Must match the server format 'interview_<16 lowercase hex>' "
                        "and must NOT collide with an existing interview file. "
                        "Only valid for the start action — supplying it together with "
                        "session_id (resume) or answer is rejected with an error to "
                        "prevent silent identifier hijacking; do not preserve this "
                        "argument across turns. "
                        "Used by the bounded auto driver to pre-allocate the id so a "
                        "driver-level cancel cannot leave auto state out of sync with "
                        "the persisted interview file (see Q00/ouroboros#687)."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an interview request.

        Args:
            arguments: Tool arguments including initial_context, session_id, or answer.

        Returns:
            Result containing interview question and session_id or error.
        """
        initial_context = arguments.get("initial_context")
        session_id = arguments.get("session_id")
        answer = arguments.get("answer")
        # Optional caller-supplied id for new interviews (Q00/ouroboros#687).
        # Only honoured for the ``start`` action; ignored otherwise.
        suggested_interview_id_arg = arguments.get("interview_id")
        suggested_interview_id = (
            suggested_interview_id_arg
            if isinstance(suggested_interview_id_arg, str) and suggested_interview_id_arg
            else None
        )
        last_question = arguments.get("last_question")

        # --- Argument validation (before any dispatch) ---
        # Determine action from arguments
        if initial_context:
            action = "start"
        elif answer:
            action = "answer"
        else:
            action = "resume"

        # Reject invalid combos early — applies to both plugin and subprocess paths.
        if action != "start" and not session_id:
            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start or session_id to resume",
                    tool_name="ouroboros_interview",
                )
            )

        # Validate caller-supplied ``interview_id`` strictly to keep the
        # MCP contract narrow.  The id must match the server's own
        # ``interview_<16 hex>`` format (matching ``uuid4().hex[:16]``)
        # and must not collide with an existing on-disk interview file —
        # this prevents cross-client spoofing/hijacking and accidental
        # reuse of an active session.  Q00/ouroboros#723 review.
        if suggested_interview_id is not None:
            if action != "start":
                return Result.err(
                    MCPToolError(
                        "interview_id is only valid for new interviews; resume via session_id",
                        tool_name="ouroboros_interview",
                    )
                )
            if not _SUGGESTED_INTERVIEW_ID_RE.fullmatch(suggested_interview_id):
                return Result.err(
                    MCPToolError(
                        "interview_id must match the server format 'interview_<16 hex>'",
                        tool_name="ouroboros_interview",
                    )
                )
            collision_path = self.resolved_state_dir() / f"interview_{suggested_interview_id}.json"
            if collision_path.exists():
                return Result.err(
                    MCPToolError(
                        "interview_id collides with an existing interview; pick a fresh id",
                        tool_name="ouroboros_interview",
                    )
                )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Plugin mode: persist state server-side WITHOUT creating an LLM adapter.
            # Only state I/O is needed here — the subagent handles all LLM work.
            # This avoids importing litellm (optional dep) on plugin-only installs.
            # Route through ``resolved_state_dir`` so an injected
            # ``InterviewEngine`` with a custom ``state_dir`` keeps plugin
            # writes and the collision check on the same directory
            # (Q00/ouroboros#723 review).
            state_dir = self.resolved_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)

            transcript = ""
            real_session_id = session_id

            if action == "start" and initial_context:
                cwd = arguments.get("cwd") or os.getcwd()
                resolved_context = resolve_initial_context_input(initial_context, cwd=cwd)
                if resolved_context.is_err:
                    return Result.err(
                        MCPToolError(
                            str(resolved_context.error),
                            tool_name="ouroboros_interview",
                        )
                    )
                # Pure state creation — mirrors InterviewEngine.start_interview()
                from ouroboros.core.security import InputValidator

                is_valid, error_msg = InputValidator.validate_initial_context(
                    resolved_context.value
                )
                if not is_valid:
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))
                from uuid import uuid4

                # Honour caller-supplied id when present (auto driver
                # pre-allocates the id for Q00/ouroboros#687).  Fall back
                # to a fresh uuid otherwise.
                interview_id = suggested_interview_id or f"interview_{uuid4().hex[:16]}"
                state = InterviewState(
                    interview_id=interview_id,
                    initial_context=resolved_context.value,
                )
                # Detect brownfield
                if cwd:
                    from ouroboros.bigbang.explore import detect_brownfield

                    if detect_brownfield(cwd):
                        state.is_brownfield = True
                        state.codebase_paths = [{"path": cwd, "role": "primary"}]

                # Persist — propagate failure instead of silently ignoring
                save_result = await _plugin_save_state(state_dir, state)
                if save_result.is_err:
                    return Result.err(
                        MCPToolError(str(save_result.error), tool_name="ouroboros_interview")
                    )
                real_session_id = state.interview_id

            elif session_id:
                load_result = await _plugin_load_state(state_dir, session_id)
                if load_result.is_err:
                    return Result.err(
                        MCPToolError(str(load_result.error), tool_name="ouroboros_interview")
                    )
                state = load_result.value
                # Record answer into persisted state.
                # In plugin mode each dispatch = new child session. The child
                # generates questions but can't write back to server-side state.
                # We must always persist user answers for transcript continuity.
                #
                # The ``last_question`` parameter solves the question-text gap:
                # the parent LLM sees the child's response (which contains the
                # question) and passes it back here so we can persist the real
                # question text instead of a placeholder.
                if answer:
                    if state.rounds and state.rounds[-1].user_response is None:
                        # Round exists with question but no answer yet — fill it.
                        # If last_question was provided, update the question text
                        # in case the existing one is a stale placeholder from a
                        # previous partial persistence.
                        if last_question:
                            state.rounds[-1].question = last_question
                        state.rounds[-1].user_response = answer
                    else:
                        # No rounds yet or all answered — append new round.
                        # Use last_question when available; fall back to a
                        # descriptive placeholder for backward compatibility
                        # (callers that don't supply last_question yet).
                        from ouroboros.bigbang.interview import InterviewRound

                        question_text = (
                            last_question if last_question else "(continued from subagent)"
                        )
                        state.rounds.append(
                            InterviewRound(
                                round_number=len(state.rounds) + 1,
                                question=question_text,
                                user_response=answer,
                            )
                        )
                    state.mark_updated()
                    save_result = await _plugin_save_state(state_dir, state)
                    if save_result.is_err:
                        return Result.err(
                            MCPToolError(str(save_result.error), tool_name="ouroboros_interview")
                        )
                # Build transcript from persisted rounds
                transcript = _format_interview_transcript(state)

            payload = build_interview_subagent(
                session_id=real_session_id or "new",
                action=action,
                initial_context=initial_context,
                answer=answer,
                cwd=arguments.get("cwd"),
                transcript=transcript,
            )
            await emit_subagent_dispatched_event(
                self.event_store,
                session_id=real_session_id,
                payload=payload,
            )
            return build_subagent_result(
                payload,
                response_shape={
                    "session_id": real_session_id,
                    "action": action,
                    "status": "delegated_to_subagent",
                    "dispatch_mode": "plugin",
                    "next_turn_hint": (
                        "When the user answers, pass the child session's "
                        "question text as 'last_question' alongside 'answer' "
                        "to preserve interview transcript fidelity."
                    ),
                },
            )

        # Fall-through: real in-process interview engine (subprocess / non-opencode runtimes).

        # Use injected or create interview engine
        # max_turns=1: MCP is a pure question generator. No tool use needed.
        # Main session handles codebase exploration and answering.
        #
        # ``allowed_tools=[]`` is paired with ``max_turns=1``: any tool-use
        # block emitted by the model would consume the only allowed turn
        # and the SDK then raises ``Reached maximum number of turns (1)``
        # before a final text response can stream.  The read-only policy
        # envelope (``_interview_allowed_tools``) is intentionally bypassed
        # here — interview is a single-shot question generator, not an
        # agentic explorer, and matches ``PMInterviewHandler._get_engine``
        # which closes the envelope the same way (``pm_handler.py``).
        # See: https://github.com/Q00/ouroboros/issues/765
        #
        # ``strict_mcp_config=True`` is opt-in here — and ONLY here — so the
        # subprocess spawned for question generation cannot rediscover the
        # plugin-provided ouroboros MCP server when this handler runs as a
        # child of Claude Code's MCP host (where ``mcp__plugin_ouroboros_*``
        # tools are auto-registered).  Without this, the subprocess
        # recurses on ``ouroboros_interview`` and exits at ``--max-turns 1``.
        # CLI interview entrypoints (``ooo init`` / ``ooo pm``) do NOT pass
        # this flag, so they keep plugin/project ``.mcp.json`` servers.
        llm_adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=1,
            use_case="interview",
            allowed_tools=(
                []
                if backend_supports_tool_envelope(resolve_llm_backend(self.llm_backend))
                else None
            ),
            strict_mcp_config=True,
        )
        # Build a per-call InterviewEngine when a real engine is supplied, to
        # avoid mutating shared engine state in place. Mutating a shared
        # engine's llm_adapter and suppress_tool_use_prompt_cues is race-prone:
        # under concurrent MCP requests, request A's `finally` restoration can
        # clobber request B's in-flight adapter and leave the shared engine
        # pointing at the wrong adapter or stale prompt mode. InterviewEngine
        # itself is config-only (state lives on disk via state_dir), so
        # cloning the config fields into a fresh per-call engine is both
        # correct and concurrency-safe.
        #
        # Test fakes that do NOT subclass InterviewEngine are passed through
        # unchanged: they cannot leak under concurrency because they are not
        # shared across production requests, and tests inject them to observe
        # the question-generation flow.
        # NOTE: ``isinstance(..., InterviewEngine)`` must NOT be reached when
        # ``InterviewEngine`` has been monkey-patched in tests to a non-type
        # (e.g. ``patch("authoring_handlers.InterviewEngine", return_value=...)``
        # replaces the name with a MagicMock instance). The previous short-
        # circuit only guarded the left operand: once ``template`` was non-None,
        # ``isinstance(template, InterviewEngine)`` still ran and raised
        # ``TypeError: isinstance() arg 2 must be a type``, turning a supported
        # dependency-injection / test-harness path into a hard failure.
        # Add an ``isinstance(InterviewEngine, type)`` guard so the clone arm
        # is only taken when the bound name is still a real class, and any
        # patched non-type value falls through to the ``elif template is not
        # None`` passthrough branch.
        template = self.interview_engine
        if (
            template is not None
            and isinstance(InterviewEngine, type)
            and isinstance(template, InterviewEngine)
        ):
            engine = InterviewEngine(
                llm_adapter=llm_adapter,
                state_dir=template.state_dir,
                model=template.model or get_clarification_model(self.llm_backend),
                suppress_tool_use_prompt_cues=self.suppress_tool_use_prompt_cues,
            )
            engine.temperature = template.temperature
            engine.max_tokens = template.max_tokens
        elif template is not None:
            engine = template
        else:
            engine = InterviewEngine(
                llm_adapter=llm_adapter,
                state_dir=self.data_dir or _DATA_DIR,
                model=get_clarification_model(self.llm_backend),
                suppress_tool_use_prompt_cues=self.suppress_tool_use_prompt_cues,
            )

        _interview_id: str | None = None  # Track for error event emission

        try:
            # Start new interview
            if initial_context:
                cwd = arguments.get("cwd") or os.getcwd()
                resolved_context = resolve_initial_context_input(initial_context, cwd=cwd)
                if resolved_context.is_err:
                    return Result.err(
                        MCPToolError(
                            str(resolved_context.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                result = await engine.start_interview(
                    resolved_context.value,
                    cwd=cwd,
                    interview_id=suggested_interview_id,
                )
                if result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(result.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                state = result.value
                _interview_id = state.interview_id
                # No answers exist yet — scoring cannot trigger completion
                # and would waste an LLM call (~3-8s). The PM handler
                # already skips scoring before MIN_ROUNDS_BEFORE_EARLY_EXIT
                # (pm_interview.py:889); apply the same optimisation here.
                live_score = None
                question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    event_error_msg = _format_interview_failure_event_error(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    self._emit_event_bg(
                        interview_failed(
                            state.interview_id,
                            event_error_msg,
                            phase="question_generation",
                        )
                    )
                    # ``InterviewEngine.start_interview`` already persisted
                    # the initial state on disk (Q00/ouroboros#687), so the
                    # ``session_id`` returned below is guaranteed resumable.
                    # Return recoverable result with session ID for retry
                    if "empty response" in error_msg.lower():
                        amb_warning = _ambiguity_warning_for_failed_question(
                            live_score,
                            is_brownfield=state.is_brownfield,
                        )
                        stderr_info = ""
                        err = question_result.error
                        if hasattr(err, "details") and isinstance(err.details, dict):
                            stderr = err.details.get("stderr", "")
                            if stderr:
                                stderr_info = f"\n\nDiagnostics (stderr):\n{stderr}"
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Question generation failed (empty response from Agent SDK). "
                                            f"Session ID: {state.interview_id}\n\n"
                                            f'Resume with: session_id="{state.interview_id}"'
                                            f"{amb_warning}"
                                            f"{stderr_info}"
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": state.interview_id, "recoverable": True},
                            )
                        )
                    # Generic question-generation failure (timeout etc.):
                    # return a recoverable result so callers can resume
                    # using the persisted ``session_id`` instead of losing
                    # the interview handle (Q00/ouroboros#687).  Truncate
                    # ``error_msg`` to avoid leaking provider internals into
                    # the user-facing envelope.  The lifecycle event uses the
                    # provider's compact formatter for the same boundary.
                    safe_error = error_msg[:200] if error_msg else "unknown error"
                    return Result.ok(
                        MCPToolResult(
                            content=(
                                MCPContentItem(
                                    type=ContentType.TEXT,
                                    text=(
                                        f"Question generation failed: {safe_error}. "
                                        f"Session ID: {state.interview_id}\n\n"
                                        f'Resume with: session_id="{state.interview_id}"'
                                    ),
                                ),
                            ),
                            is_error=True,
                            meta={"session_id": state.interview_id, "recoverable": True},
                        )
                    )

                question = question_result.value
                display_question = _format_question_with_ambiguity(question, live_score)

                # Record the question as an unanswered round so resume can find it
                from ouroboros.bigbang.interview import InterviewRound

                state.rounds.append(
                    InterviewRound(
                        round_number=1,
                        question=question,
                        user_response=None,
                    )
                )
                state.mark_updated()

                # Persist state to disk so subsequent calls can resume
                save_result = await engine.save_state(state)
                if save_result.is_err:
                    log.warning(
                        "mcp.tool.interview.save_failed_on_start",
                        error=str(save_result.error),
                    )

                # Emit interview started event
                from ouroboros.events.interview import interview_started

                self._emit_event_bg(
                    interview_started(
                        state.interview_id,
                        resolved_context.value,
                    )
                )

                log.info(
                    "mcp.tool.interview.started",
                    session_id=state.interview_id,
                )

                is_length_guard = _is_initial_context_length_guard_question(question)
                start_meta: dict[str, Any] = {
                    "session_id": state.interview_id,
                    "ambiguity_score": (
                        live_score.overall_score if live_score is not None else None
                    ),
                    "milestone": _milestone_for_score(live_score),
                    "seed_ready": (
                        live_score.is_ready_for_seed if live_score is not None else None
                    ),
                }
                if is_length_guard:
                    # Q00/ouroboros#831 (Direction A): surface the length-guard
                    # meta-directive via structured meta keys so clients can
                    # branch on ``meta.reason`` instead of mis-routing the text
                    # body to a human via AskUserQuestion.  ``is_error`` is
                    # intentionally left ``False`` -- the wire success/failure
                    # axis must not flip or ``HandlerInterviewBackend.start``
                    # would raise on every oversized ``initial_context``.
                    start_meta.update(_length_guard_meta_fields())

                start_response_text = (
                    f"Interview started. Session ID: {state.interview_id}\n\n{display_question}"
                )
                # Q00/ouroboros#831 (diagnostics): capture the shape of every
                # MCP question-bearing response so future hang reports can be
                # correlated with response size / transcript pressure.
                from ouroboros.events.interview import interview_response_emitted

                self._emit_event_bg(
                    interview_response_emitted(
                        state.interview_id,
                        response_kind="start",
                        round_number=len(state.rounds),
                        payload_chars=len(start_response_text),
                        transcript_chars=_compute_transcript_chars(state),
                        ambiguity_prefix_present=start_response_text.startswith("(ambiguity:"),
                        is_length_guard=is_length_guard,
                    )
                )
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=start_response_text,
                            ),
                        ),
                        is_error=False,
                        meta=start_meta,
                    )
                )

            # Resume existing interview
            if session_id:
                load_result = await engine.load_state(session_id)
                if load_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(load_result.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                state = load_result.value
                _interview_id = session_id

                if not answer and state.rounds and state.rounds[-1].user_response is None:
                    pending_question = state.rounds[-1].question
                    display_question = _format_question_with_ambiguity(
                        pending_question,
                        _load_state_ambiguity_score(state),
                    )
                    resume_is_length_guard = _is_initial_context_length_guard_question(
                        pending_question
                    )
                    resume_meta: dict[str, Any] = {
                        "session_id": session_id,
                        "ambiguity_score": state.ambiguity_score,
                        "milestone": (
                            get_milestone(state.ambiguity_score)[0].value
                            if state.ambiguity_score is not None
                            else None
                        ),
                        "seed_ready": (
                            state.ambiguity_score is not None
                            and state.ambiguity_score <= AMBIGUITY_THRESHOLD
                        ),
                    }
                    if resume_is_length_guard:
                        # Q00/ouroboros#831 (Direction A): structured signal
                        # when resuming an interview whose pending round is
                        # the length-guard meta-directive.  ``is_error`` stays
                        # ``False`` so the auto driver does not treat the
                        # summarize prompt as a hard failure.
                        resume_meta.update(_length_guard_meta_fields())

                    resume_response_text = f"Session {session_id}\n\n{display_question}"
                    # Q00/ouroboros#831 (diagnostics): response-shape event for
                    # the resume-pending branch.  Pure observability.
                    from ouroboros.events.interview import interview_response_emitted

                    self._emit_event_bg(
                        interview_response_emitted(
                            session_id,
                            response_kind="resume_pending",
                            round_number=len(state.rounds),
                            payload_chars=len(resume_response_text),
                            transcript_chars=_compute_transcript_chars(state),
                            ambiguity_prefix_present=resume_response_text.startswith("(ambiguity:"),
                            is_length_guard=resume_is_length_guard,
                        )
                    )
                    return Result.ok(
                        MCPToolResult(
                            content=(
                                MCPContentItem(
                                    type=ContentType.TEXT,
                                    text=resume_response_text,
                                ),
                            ),
                            is_error=False,
                            meta=resume_meta,
                        )
                    )

                lateral_review_meta: dict[str, Any] | None = None

                # If answer provided, record it first
                if answer:
                    if _is_interview_completion_signal(answer):
                        # Remember whether a round is awaiting an answer so we
                        # can pop it only on the branches that actually end
                        # the interview. Shortfall/refusal paths keep the
                        # pending question around so the user's next plain
                        # answer lands on a live round instead of either
                        # crashing ("no questions have been asked yet") or
                        # attaching to a stale, already-answered round.
                        has_pending_round = bool(
                            state.rounds and state.rounds[-1].user_response is None
                        )
                        # Gate: check ambiguity before completing.
                        # Stored score first; live scoring as fallback.
                        exit_score = _load_state_ambiguity_score(state)
                        if (
                            exit_score is None
                            or _stored_ambiguity_snapshot_is_degraded(state)
                            or not qualifies_for_seed_completion(
                                exit_score,
                                is_brownfield=state.is_brownfield,
                            )
                        ):
                            # Own the streak advance in this branch; the
                            # scorer must not double-bump it. See #405.
                            # ``reset_on_failure=True`` keeps the shared
                            # stale-streak invalidation contract even
                            # though this branch disables the qualifying-
                            # score increment.
                            exit_score = await self._score_interview_state(
                                llm_adapter,
                                state,
                                advance_streak=False,
                                reset_on_failure=True,
                            )
                        if exit_score is not None and qualifies_for_seed_completion(
                            exit_score,
                            is_brownfield=state.is_brownfield,
                        ):
                            # Explicit 'done' with a qualifying score counts
                            # as an implicit stability signal — advance the
                            # streak so repeated 'done' inputs can progress
                            # instead of looping on the same message forever.
                            # See: https://github.com/Q00/ouroboros/issues/405
                            if state.completion_candidate_streak < AUTO_COMPLETE_STREAK_REQUIRED:
                                state.completion_candidate_streak += 1
                                state.mark_updated()
                            if state.completion_candidate_streak >= AUTO_COMPLETE_STREAK_REQUIRED:
                                # We are about to finalize the interview —
                                # drop the pending 'done' round so it does
                                # not leak into the saved transcript.
                                if has_pending_round:
                                    state.rounds.pop()
                                return await self._complete_interview_response(
                                    engine,
                                    state,
                                    session_id,
                                    exit_score,
                                )
                            # Streak advanced but still short of the
                            # threshold — persist the advance and invite the
                            # user to confirm again or answer the pending
                            # question. The pending round is intentionally
                            # preserved so "answer another question to
                            # update the score" remains truthful.
                            #
                            # Persistence is load-bearing on this branch:
                            # the next 'done' must see the advanced streak
                            # or the user is stuck looping from 0 forever.
                            # Treat a save failure as a hard error rather
                            # than returning the "almost there" success
                            # message with an un-persisted streak.
                            # See #405 follow-up design note on 3c2531d.
                            shortfall_save_result = await engine.save_state(state)
                            if shortfall_save_result.is_err:
                                log.error(
                                    "mcp.tool.interview.save_failed_on_shortfall",
                                    session_id=session_id,
                                    error=str(shortfall_save_result.error),
                                )
                                return Result.err(
                                    MCPToolError(
                                        "Failed to persist completion streak "
                                        f"advance: {shortfall_save_result.error}",
                                        tool_name="ouroboros_interview",
                                    )
                                )
                            streak_shortfall = (
                                AUTO_COMPLETE_STREAK_REQUIRED - state.completion_candidate_streak
                            )
                            answer_hint = (
                                "or answer the pending question to update the score."
                                if has_pending_round
                                else "or resume without an answer to receive another question."
                            )
                            return Result.ok(
                                MCPToolResult(
                                    content=(
                                        MCPContentItem(
                                            type=ContentType.TEXT,
                                            text=(
                                                f"Ambiguity looks low "
                                                f"(score={exit_score.overall_score:.2f}). "
                                                f"Stability check: "
                                                f"{state.completion_candidate_streak}"
                                                f"/{AUTO_COMPLETE_STREAK_REQUIRED}. "
                                                f"Type 'done' once more to confirm "
                                                f"({streak_shortfall} more signal(s) needed), "
                                                f"{answer_hint}"
                                            ),
                                        ),
                                    ),
                                    is_error=False,
                                    meta={
                                        "session_id": session_id,
                                        "ambiguity_score": exit_score.overall_score,
                                        "seed_ready": False,
                                        "completion_candidate_streak": (
                                            state.completion_candidate_streak
                                        ),
                                        "streak_required": AUTO_COMPLETE_STREAK_REQUIRED,
                                    },
                                )
                            )
                        # Ambiguity too high — refuse completion. Keep any
                        # pending round in place so the user can still
                        # answer it directly.
                        #
                        # Persistence is load-bearing on this branch too:
                        # ``_score_interview_state(reset_on_failure=True)``
                        # may have just cleared a stale ``completion_candidate_streak``
                        # in memory. If the save silently fails, the next
                        # request reloads the pre-reset streak from disk
                        # and a single qualifying signal can finalize the
                        # interview, violating the two-signal contract #405
                        # was opened to enforce. Treat a save failure as a
                        # hard error, mirroring the shortfall branch.
                        refuse_save_result = await engine.save_state(state)
                        if refuse_save_result.is_err:
                            log.error(
                                "mcp.tool.interview.save_failed_on_ambiguity_gate",
                                session_id=session_id,
                                error=str(refuse_save_result.error),
                            )
                            return Result.err(
                                MCPToolError(
                                    "Failed to persist stale-streak reset: "
                                    f"{refuse_save_result.error}",
                                    tool_name="ouroboros_interview",
                                )
                            )
                        return self._ambiguity_gate_response(
                            session_id,
                            exit_score,
                            is_brownfield=state.is_brownfield,
                        )

                    if not state.rounds:
                        return Result.err(
                            MCPToolError(
                                "Cannot record answer - no questions have been asked yet",
                                tool_name="ouroboros_interview",
                            )
                        )

                    # Resolve the question text for this round.
                    #
                    # Case A — last round is unanswered (the normal flow): the
                    # caller is answering the pending MCP-generated question.
                    # Pop the unanswered round; record_response re-creates it
                    # with the same question and the user's answer. A
                    # caller-provided ``last_question`` overrides the stored
                    # text to repair stale placeholders.
                    #
                    # Case B — last round is already answered (post-seed-ready
                    # challenge per the Seed-ready Acceptance Guard, or any
                    # reopen with no pending question): MCP did not generate
                    # the probe — the main session did. Reusing
                    # state.rounds[-1].question would bind the caller's new
                    # answer to the previously-answered question, corrupting
                    # the transcript. Require the caller to supply the
                    # question via ``last_question``.
                    # If this is the first live ambiguity score, the interview
                    # is crossing from the implicit starting milestone.  Treat
                    # the absent stored score as ``initial`` so the normal first
                    # ``initial -> progress/refined/ready`` transition can
                    # surface the advisory instead of being skipped forever.
                    previous_milestone = (
                        get_milestone(state.ambiguity_score)[0].value
                        if state.ambiguity_score is not None
                        else "initial"
                    )

                    if state.rounds[-1].user_response is None:
                        pending_question = last_question or state.rounds[-1].question
                        state.rounds.pop()
                    else:
                        if not last_question:
                            return Result.err(
                                MCPToolError(
                                    "Cannot record answer - the previous round is "
                                    "already answered and no follow-up question was "
                                    "provided. When reopening a completed interview "
                                    "(Seed-ready challenge), pass the new probe "
                                    "question as 'last_question' alongside 'answer'.",
                                    tool_name="ouroboros_interview",
                                )
                            )
                        pending_question = last_question

                    record_result = await engine.record_response(state, answer, pending_question)
                    if record_result.is_err:
                        return Result.err(
                            MCPToolError(
                                str(record_result.error),
                                tool_name="ouroboros_interview",
                            )
                        )
                    state = record_result.value
                    state.clear_stored_ambiguity()

                    # Emit response recorded event
                    from ouroboros.events.interview import interview_response_recorded

                    self._emit_event_bg(
                        interview_response_recorded(
                            interview_id=session_id,
                            round_number=len(state.rounds),
                            question_preview=pending_question,
                            response_preview=answer,
                        )
                    )

                    log.info(
                        "mcp.tool.interview.response_recorded",
                        session_id=session_id,
                    )

                    # Persist recorded answer immediately so it survives
                    # question generation failures downstream
                    await engine.save_state(state)

                    # Only score ambiguity when completion is actually
                    # possible. Before MIN_ROUNDS_BEFORE_EARLY_EXIT the
                    # result cannot trigger early exit, so the LLM call
                    # (~3-8 s) is pure waste. Once scoring starts, run it
                    # before question generation so the next prompt sees
                    # the latest ambiguity snapshot, closure threshold,
                    # and completion-candidate streak.
                    answered = _count_answered_rounds(state)
                    if answered >= MIN_ROUNDS_BEFORE_EARLY_EXIT:
                        # Scoring must complete before question generation:
                        # _score_interview_state mutates state.ambiguity_score,
                        # completion_candidate_streak, and ambiguity_breakdown.
                        # ask_next_question reads those fields to build the
                        # system prompt (closure mode, seed-ready, streak).
                        # Running them in parallel would give the question
                        # generator stale routing context.
                        live_score = await self._score_interview_state(llm_adapter, state)
                        lateral_review_meta = _maybe_record_lateral_review_advisory(
                            state,
                            previous_milestone=previous_milestone,
                            score=live_score,
                        )
                        if lateral_review_meta is not None and live_score is not None:
                            from ouroboros.events.interview import (
                                interview_lateral_review_recommended,
                            )

                            self._emit_event_bg(
                                interview_lateral_review_recommended(
                                    session_id,
                                    from_milestone=lateral_review_meta[
                                        "lateral_review_from_milestone"
                                    ],
                                    to_milestone=lateral_review_meta["lateral_review_milestone"],
                                    ambiguity_score=live_score.overall_score,
                                    round_number=len(state.rounds),
                                )
                            )
                        if (
                            live_score is not None
                            and qualifies_for_seed_completion(
                                live_score,
                                is_brownfield=state.is_brownfield,
                            )
                            and state.completion_candidate_streak >= AUTO_COMPLETE_STREAK_REQUIRED
                        ):
                            return await self._complete_interview_response(
                                engine,
                                state,
                                session_id,
                                live_score,
                            )
                        question_result = await engine.ask_next_question(state)
                    else:
                        live_score = None
                        question_result = await engine.ask_next_question(state)
                else:
                    live_score = _load_state_ambiguity_score(state)
                    question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    event_error_msg = _format_interview_failure_event_error(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    self._emit_event_bg(
                        interview_failed(
                            session_id,
                            event_error_msg,
                            phase="question_generation",
                        )
                    )
                    if "empty response" in error_msg.lower():
                        amb_warning = _ambiguity_warning_for_failed_question(
                            live_score,
                            is_brownfield=state.is_brownfield,
                        )
                        # Extract stderr from ProviderError details for diagnostics
                        stderr_info = ""
                        err = question_result.error
                        if hasattr(err, "details") and isinstance(err.details, dict):
                            stderr = err.details.get("stderr", "")
                            if stderr:
                                stderr_info = f"\n\nDiagnostics (stderr):\n{stderr}"
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Question generation failed (empty response from Agent SDK). "
                                            f"Session ID: {session_id}\n\n"
                                            f'Resume with: session_id="{session_id}"'
                                            f"{amb_warning}"
                                            f"{stderr_info}"
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": session_id, "recoverable": True},
                            )
                        )
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))

                question = question_result.value
                display_question = _format_question_with_ambiguity(question, live_score)

                # Save pending question as unanswered round for next resume
                from ouroboros.bigbang.interview import InterviewRound

                state.rounds.append(
                    InterviewRound(
                        round_number=state.current_round_number,
                        question=question,
                        user_response=None,
                    )
                )
                state.mark_updated()

                save_result = await engine.save_state(state)
                if save_result.is_err:
                    log.warning(
                        "mcp.tool.interview.save_failed",
                        error=str(save_result.error),
                    )

                log.info(
                    "mcp.tool.interview.question_asked",
                    session_id=session_id,
                )

                answer_is_length_guard = _is_initial_context_length_guard_question(question)
                answer_meta: dict[str, Any] = {
                    "session_id": session_id,
                    "ambiguity_score": (
                        live_score.overall_score if live_score is not None else None
                    ),
                    "milestone": _milestone_for_score(live_score),
                    "seed_ready": (
                        live_score.is_ready_for_seed if live_score is not None else None
                    ),
                }
                if answer_is_length_guard:
                    # Q00/ouroboros#831 (Direction A): structured signal when
                    # the next question after an answer is again the length-
                    # guard meta-directive.  ``is_error`` stays ``False`` so
                    # the auto driver's ``answer()`` path is not raised on.
                    answer_meta.update(_length_guard_meta_fields())

                if lateral_review_meta is not None:
                    answer_meta.update(lateral_review_meta)

                answer_response_text = f"Session {session_id}\n\n{display_question}"
                # Q00/ouroboros#831 (diagnostics): response-shape event for
                # the answer branch.  Pure observability.
                from ouroboros.events.interview import interview_response_emitted

                self._emit_event_bg(
                    interview_response_emitted(
                        session_id,
                        response_kind="answer",
                        round_number=len(state.rounds),
                        payload_chars=len(answer_response_text),
                        transcript_chars=_compute_transcript_chars(state),
                        ambiguity_prefix_present=answer_response_text.startswith("(ambiguity:"),
                        is_length_guard=answer_is_length_guard,
                    )
                )
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=answer_response_text,
                            ),
                        ),
                        is_error=False,
                        meta=answer_meta,
                    )
                )

            # No valid parameters provided
            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start or session_id to resume",
                    tool_name="ouroboros_interview",
                )
            )

        except Exception as e:
            log.error("mcp.tool.interview.error", error=str(e))
            if _interview_id:
                from ouroboros.events.interview import interview_failed

                self._emit_event_bg(
                    interview_failed(
                        _interview_id,
                        _format_interview_failure_event_error(e),
                        phase="unexpected_error",
                    )
                )
            return Result.err(
                MCPToolError(
                    f"Interview failed: {e}",
                    tool_name="ouroboros_interview",
                )
            )
        finally:
            if self._owns_event_store:
                await self.close()
