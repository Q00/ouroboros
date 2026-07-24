"""Interactive interview engine for requirement clarification.

This module implements the interview protocol that refines vague ideas into
clear requirements through iterative questioning. Users control when to stop.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import functools
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.config import get_llm_model_for_role
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.file_lock import file_lock as _file_lock
from ouroboros.core.requirement_candidate import (
    RequirementDistillation,
    compute_requirement_input_fingerprint,
)
from ouroboros.core.security import InputValidator
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    InterviewTurnContext,
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceResolutionStatus,
    build_reference_contrast_question,
    detect_explicit_confusion_terms,
    next_unresolved_reference,
    select_glossary_injection,
)
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

log = structlog.get_logger()

# Interview round constants
# Start scoring after 3 answered rounds. Closing pressure and auto-completion
# are gated separately by ambiguity thresholds and sustained score quality.
MIN_ROUNDS_BEFORE_EARLY_EXIT = 3
DEFAULT_INTERVIEW_ROUNDS = 10  # Reference value for prompts (not enforced)

# Legacy alias for backward compatibility
MAX_INTERVIEW_ROUNDS = DEFAULT_INTERVIEW_ROUNDS
MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS = 3500
INITIAL_CONTEXT_SUMMARY_QUESTION = (
    "Your saved initial context is too long to safely send to the interview "
    "model without risking CLI prompt failure. Please reply with a concise "
    "summary of the full context, including goals, constraints, and success "
    "criteria. I will use that summary for the next interview question."
)
INITIAL_CONTEXT_SUMMARY_REQUIRED = (
    "[Initial context exceeds the prompt-safe size and no user summary has been "
    "recorded yet. Ask the user to provide a concise summary before scoring or "
    "generating a seed.]"
)
PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE = "\n\n[Context truncated for prompt safety.]"
# Empirically, the local Agent SDK CLI path can return empty completions when
# interview question prompts grow beyond roughly this serialized prompt size.
# This is the observed failure ceiling, not a raw ``message.content`` budget:
# CLI adapters add section headers, role prefixes, separators, and final
# response instructions around each message before sending the real prompt.
AGENT_SDK_CLI_EMPIRICAL_EMPTY_RESPONSE_CHARS = 16_000
# Conservative serialization reserves used by interview prompt budgeting. The
# fixed reserve covers adapter section headers/tool/execution instructions; the
# per-message reserve covers role prefixes and separators so long interviews
# with many short turns cannot pass raw-content checks while crossing the
# observed CLI empty-response cliff after serialization.
AGENT_SDK_CLI_FIXED_FRAMING_CHARS = 1_500
AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS = 128
# Keep estimated serialized prompts below the observed empty-response boundary;
# the remaining 2k margin absorbs adapter/provider prompt text that is harder to
# model locally while still tripling the original 4.8k interview budget.
AGENT_SDK_CLI_SAFE_PROMPT_CHARS = 14_000

_TOOLLESS_INTERVIEW_BASE_PROMPT = """## Role Boundaries
- You are only an interviewer.
- Generate exactly one Socratic question that reduces requirements ambiguity.
- Do not explore files, commands, repositories, APIs, tools, or external systems.
- Do not ask to inspect implementation details unless the caller already supplied those details.
- The caller supplies any code or research context in answers.

## Response Format
- Ask one focused question in 1-2 sentences.
- Do not include a preamble.
- End with the question.

## Questioning Strategy
- Target the biggest unresolved decision.
- Prefer scope, non-goal, success criteria, ownership, risk, and verification questions.
- For brownfield work, focus on intent and decisions rather than discovering what exists.
"""


class InterviewPerspective(StrEnum):
    """Internal perspectives used to keep interviews broad and practical."""

    RESEARCHER = "researcher"
    SIMPLIFIER = "simplifier"
    ARCHITECT = "architect"
    BREADTH_KEEPER = "breadth-keeper"
    SEED_CLOSER = "seed-closer"


@dataclass(frozen=True, slots=True)
class InterviewPerspectiveStrategy:
    """Prompt data for one internal interview perspective."""

    perspective: InterviewPerspective
    system_prompt: str
    approach_instructions: tuple[str, ...]
    question_templates: tuple[str, ...]


@functools.lru_cache(maxsize=1)
def _load_interview_perspective_strategies() -> dict[
    InterviewPerspective,
    InterviewPerspectiveStrategy,
]:
    """Lazy-load perspective prompts from agent markdown files."""
    from ouroboros.agents.loader import load_persona_prompt_data

    mapping = {
        InterviewPerspective.RESEARCHER: "researcher",
        InterviewPerspective.SIMPLIFIER: "simplifier",
        InterviewPerspective.ARCHITECT: "architect",
        InterviewPerspective.BREADTH_KEEPER: "breadth-keeper",
        InterviewPerspective.SEED_CLOSER: "seed-closer",
    }

    return {
        perspective: InterviewPerspectiveStrategy(
            perspective=perspective,
            system_prompt=data.system_prompt,
            approach_instructions=data.approach_instructions,
            question_templates=data.question_templates,
        )
        for perspective, filename in mapping.items()
        for data in [load_persona_prompt_data(filename)]
    }


class InterviewStatus(StrEnum):
    """Status of the interview process."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


class InterviewRound(BaseModel):
    """A single round of interview questions and responses.

    Attributes:
        round_number: 1-based round number (no upper limit - user controls).
        question: The question asked by the system.
        user_response: The user's response (None if not yet answered).
        timestamp: When this round was created.
    """

    round_number: int = Field(ge=1)  # No upper limit - user decides when to stop
    question: str
    user_response: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InterviewState(BaseModel):
    """Persistent state of an interview session.

    Attributes:
        interview_id: Unique identifier for this interview.
        status: Current status of the interview.
        rounds: List of completed and current rounds.
        initial_context: The initial context provided by the user.
        created_at: When the interview was created.
        updated_at: When the interview was last updated.
        is_brownfield: Whether this is a brownfield project.
        codebase_paths: Directories to explore for brownfield context.
        codebase_context: Summary from auto-explore phase.
        explore_completed: Whether exploration has been completed.
    """

    interview_id: str
    status: InterviewStatus = InterviewStatus.IN_PROGRESS
    rounds: list[InterviewRound] = Field(default_factory=list)
    initial_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_brownfield: bool = False
    codebase_paths: list[dict[str, str]] = Field(default_factory=list)
    codebase_context: str = ""
    explore_completed: bool = False
    ambiguity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguity_breakdown: dict[str, Any] | None = None
    completion_candidate_streak: int = Field(default=0, ge=0)
    lateral_review_advised_milestones: list[str] = Field(default_factory=list)
    reference_cues: tuple[ReferenceCue, ...] = Field(default_factory=tuple)
    reference_resolutions: tuple[ReferenceContrastResolution, ...] = Field(default_factory=tuple)
    pending_confused_terms: tuple[str, ...] = Field(default_factory=tuple)
    requirement_input_revision: int = Field(default=0, ge=0)
    requirement_distillation: RequirementDistillation | None = None

    @property
    def current_round_number(self) -> int:
        """Get the current round number (1-based)."""
        return len(self.rounds) + 1

    @property
    def is_complete(self) -> bool:
        """Check if interview is marked complete (user-controlled)."""
        return self.status == InterviewStatus.COMPLETED

    @property
    def needs_initial_context_summary(self) -> bool:
        """True when oversized initial context has no recorded summary."""
        if len(self.initial_context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
            return False
        return not any(
            round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION
            and bool(round_data.user_response)
            for round_data in self.rounds
        )

    @property
    def can_reopen(self) -> bool:
        """True when a completed interview should be reopenable.

        Any completed interview is reopenable: the main session is the final
        gate on seed-ready (see Seed-ready Acceptance Guard in skills/interview/
        SKILL.md). When it sends another answer, it is explicitly challenging
        the prior closure — the stored ambiguity score is no longer trustworthy
        and must be re-evaluated against the extended round history.
        """
        return self.is_complete

    def mark_updated(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(UTC)

    def store_ambiguity(
        self,
        *,
        score: float,
        breakdown: dict[str, Any],
    ) -> None:
        """Persist the latest ambiguity evaluation on the interview state."""
        self.ambiguity_score = score
        self.ambiguity_breakdown = breakdown
        self.mark_updated()

    def note_lateral_review_advisory(self, milestone: str) -> None:
        """Persist that a milestone transition advisory has been emitted."""
        if milestone in self.lateral_review_advised_milestones:
            return
        self.lateral_review_advised_milestones.append(milestone)
        self.mark_updated()

    def clear_stored_ambiguity(self) -> None:
        """Invalidate any persisted ambiguity snapshot after interview changes."""
        if self.ambiguity_score is None and self.ambiguity_breakdown is None:
            return

        self.ambiguity_score = None
        self.ambiguity_breakdown = None
        self.mark_updated()

    def requirement_input_fingerprint(self) -> str:
        """Fingerprint canonical inputs used by the derived distillation cache."""
        return compute_requirement_input_fingerprint(
            {
                "initial_context": self.initial_context,
                "rounds": [
                    {
                        "round_number": round_data.round_number,
                        "question": round_data.question,
                        "user_response": round_data.user_response,
                    }
                    for round_data in self.rounds
                ],
                "reference_cues": self.reference_cues,
                "reference_resolutions": self.reference_resolutions,
                "codebase_context": self.codebase_context,
                "codebase_paths": self.codebase_paths,
            }
        )

    def invalidate_requirement_distillation(self) -> None:
        """Advance canonical-input revision and clear derived cached output."""
        self.requirement_input_revision += 1
        self.requirement_distillation = None
        self.mark_updated()

    def discard_stale_requirement_distillation(self) -> bool:
        """Discard a persisted cache that no longer matches canonical inputs."""
        cached = self.requirement_distillation
        if cached is None:
            return False
        if cached.is_current(
            input_revision=self.requirement_input_revision,
            input_fingerprint=self.requirement_input_fingerprint(),
        ):
            return False
        self.requirement_distillation = None
        self.mark_updated()
        return True

    def merge_turn_context(self, context: InterviewTurnContext | None) -> bool:
        """Merge bounded adapter input and invalidate only requirement changes."""
        if context is None:
            return False

        pending = list(self.pending_confused_terms)
        seen_terms = {term.casefold() for term in pending}
        for term in context.confused_terms:
            if term.casefold() not in seen_terms:
                pending.append(term)
                seen_terms.add(term.casefold())
        self.pending_confused_terms = tuple(pending)

        cues_by_id = {cue.reference_id: cue for cue in self.reference_cues}
        cue_order = [cue.reference_id for cue in self.reference_cues]
        references_changed = False
        changed_ids: set[str] = set()
        for cue in context.references:
            previous = cues_by_id.get(cue.reference_id)
            if previous == cue:
                continue
            if previous is None:
                cue_order.append(cue.reference_id)
            cues_by_id[cue.reference_id] = cue
            changed_ids.add(cue.reference_id)
            references_changed = True

        if references_changed:
            self.reference_cues = tuple(cues_by_id[reference_id] for reference_id in cue_order)
            self.reference_resolutions = tuple(
                resolution
                for resolution in self.reference_resolutions
                if resolution.reference_id not in changed_ids
            )
            self.invalidate_requirement_distillation()
        elif context.confused_terms:
            self.mark_updated()
        return references_changed or bool(context.confused_terms)

    def next_adapter_question(self) -> str | None:
        """Return one deterministic post-base-frame adapter question."""
        base_question_answered = any(
            round_data.question != INITIAL_CONTEXT_SUMMARY_QUESTION
            and bool(round_data.user_response)
            for round_data in self.rounds
        )
        if not base_question_answered:
            return None

        if self.pending_confused_terms:
            context = InterviewTurnContext(confused_terms=self.pending_confused_terms)
            injection = select_glossary_injection(
                context=context,
                base_question_answered=True,
            )
            self.pending_confused_terms = ()
            if injection is not None:
                self.mark_updated()
                return (
                    f"{injection.render()}\n\n"
                    "Using your own words, what does that term need to mean for this project?"
                )

        cue = next_unresolved_reference(self.reference_cues, self.reference_resolutions)
        if cue is None:
            return None
        question = build_reference_contrast_question(cue)
        remaining = tuple(
            resolution
            for resolution in self.reference_resolutions
            if resolution.reference_id != cue.reference_id
        )
        self.reference_resolutions = (
            *remaining,
            ReferenceContrastResolution(
                reference_id=cue.reference_id,
                status=ReferenceResolutionStatus.ASKED,
                asked_question=question,
            ),
        )
        self.mark_updated()
        return question

    def record_adapter_answer(self, question: str, answer: str) -> None:
        """Resolve a persisted reference contrast when its exact question is answered."""
        updated: list[ReferenceContrastResolution] = []
        changed = False
        for resolution in self.reference_resolutions:
            if (
                resolution.status is ReferenceResolutionStatus.ASKED
                and resolution.asked_question == question
            ):
                updated.append(
                    ReferenceContrastResolution(
                        reference_id=resolution.reference_id,
                        status=ReferenceResolutionStatus.RESOLVED,
                        asked_question=question,
                        answer=answer,
                    )
                )
                changed = True
            else:
                updated.append(resolution)
        if changed:
            self.reference_resolutions = tuple(updated)


def prompt_safe_initial_context(state: InterviewState) -> str:
    """Return initial context safe for LLM prompts across interview consumers."""
    if len(state.initial_context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
        return state.initial_context
    for round_data in reversed(state.rounds):
        if round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION and round_data.user_response:
            return _truncate_prompt_safe_context(round_data.user_response)
    return INITIAL_CONTEXT_SUMMARY_REQUIRED


def _truncate_prompt_safe_context(context: str) -> str:
    """Cap prompt context while leaving an explicit truncation marker."""
    if len(context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
        return context

    content_budget = MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS - len(
        PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE
    )
    if content_budget <= 0:
        return context[:MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS]
    return context[:content_budget] + PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE


def initial_context_summary_missing(state: InterviewState) -> bool:
    """Return True when a long initial context still needs a user summary."""
    return prompt_safe_initial_context(state) == INITIAL_CONTEXT_SUMMARY_REQUIRED


# ---------------------------------------------------------------------------
# Question candidate panel (K2)
# ---------------------------------------------------------------------------
#
# Instead of one question per round, three persona candidates
# (contrarian / architect / researcher) each propose a question aimed at a
# specific ambiguity dimension. Selection is deterministic (no LLM judge): pick
# the candidate targeting the WORST-scoring dimension; ties break by persona
# priority contrarian > architect > researcher.

# Persona priority order, most-preferred first.
QUESTION_CANDIDATE_PERSONAS: tuple[str, ...] = ("contrarian", "architect", "researcher")
_QUESTION_CANDIDATE_PRIORITY: dict[str, int] = {
    persona: index for index, persona in enumerate(QUESTION_CANDIDATE_PERSONAS)
}

# Ambiguity dimension keys a candidate may target (same keys ScoreBreakdown
# uses, so K1's per-dimension output selects K2's question directly).
QUESTION_CANDIDATE_DIMENSIONS: tuple[str, ...] = (
    "goal_clarity",
    "constraint_clarity",
    "success_criteria_clarity",
    "context_clarity",
)


@dataclass(frozen=True, slots=True)
class QuestionCandidate:
    """One persona-proposed interview question aimed at a dimension."""

    persona: str
    question: str
    target_dimension: str


def select_question_candidate(
    candidates: list[QuestionCandidate],
    dimension_clarity: dict[str, float],
) -> QuestionCandidate:
    """Deterministically select the best question candidate.

    Selection rule (no LLM judge):

    1. Prefer the candidate targeting the dimension with the WORST (lowest)
       current clarity score. A candidate whose ``target_dimension`` is absent
       from ``dimension_clarity`` is treated as least urgent.
    2. Ties (candidates targeting the same worst dimension) break by persona
       priority: contrarian > architect > researcher.

    Args:
        candidates: Non-empty candidate list.
        dimension_clarity: Mapping of dimension key -> clarity score (0..1).

    Returns:
        The selected candidate.

    Raises:
        ValueError: If ``candidates`` is empty.
    """
    if not candidates:
        raise ValueError("candidates must not be empty")

    def _sort_key(candidate: QuestionCandidate) -> tuple[float, int]:
        clarity = dimension_clarity.get(candidate.target_dimension)
        # Lower clarity = worse = more urgent = preferred. Unknown dimension is
        # least urgent (treated as maximally clear).
        clarity_rank = clarity if clarity is not None else float("inf")
        priority = _QUESTION_CANDIDATE_PRIORITY.get(
            candidate.persona, len(_QUESTION_CANDIDATE_PRIORITY)
        )
        return (clarity_rank, priority)

    return min(candidates, key=_sort_key)


def _dimension_clarity_from_breakdown(breakdown: dict[str, Any] | None) -> dict[str, float]:
    """Extract ``{dimension_key: clarity_score}`` from a stored breakdown dict.

    Accepts the ``ScoreBreakdown.model_dump`` shape persisted by
    ``InterviewState.store_ambiguity``. Non-numeric / missing entries are
    skipped so selection only sees usable per-dimension scores.
    """
    clarity: dict[str, float] = {}
    if not isinstance(breakdown, dict):
        return clarity
    for key, payload in breakdown.items():
        if key not in QUESTION_CANDIDATE_DIMENSIONS or not isinstance(payload, dict):
            continue
        value = payload.get("clarity_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            clarity[key] = float(value)
    return clarity


def _question_candidate_persona_roles() -> dict[str, str]:
    """Return persona -> role, sourced from the lateral persona metadata.

    Reads ``orchestrator/capabilities`` persona metadata (read-only) so the
    candidate lens descriptions reuse the same persona vocabulary as the lateral
    system rather than duplicating role strings here.
    """
    try:
        from ouroboros.orchestrator.capabilities.lateral_personas import (
            _lateral_persona_panel_metadata,
        )

        metadata = _lateral_persona_panel_metadata()
        roles = {persona.persona_id: persona.role for persona in metadata.personas}
    except Exception:  # noqa: BLE001 - metadata is advisory; fall back to bare ids
        roles = {}
    for persona in QUESTION_CANDIDATE_PERSONAS:
        roles.setdefault(persona, persona)
    return roles


def _parse_question_candidate(persona: str, response: str) -> QuestionCandidate | None:
    """Parse a persona candidate JSON ``{question, target_dimension}``.

    Returns ``None`` on any parse failure or missing question so a single bad
    candidate never breaks the panel (the others still select).
    """
    import json
    import re

    text = response.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    question = str(data.get("question") or "").strip()
    if not question:
        return None
    target = str(data.get("target_dimension") or "").strip()
    if target not in QUESTION_CANDIDATE_DIMENSIONS:
        # Unknown/blank target is tolerated: it simply ranks least-urgent in
        # selection rather than dropping an otherwise valid question.
        target = ""
    return QuestionCandidate(persona=persona, question=question, target_dimension=target)


@dataclass
class InterviewEngine:
    """Engine for conducting interactive requirement interviews.

    This engine orchestrates the interview process:
    1. Generates questions based on current context and ambiguity
    2. Collects user responses
    3. Persists state between sessions
    4. Tracks progress through rounds

    Example:
        engine = InterviewEngine(
            llm_adapter=LiteLLMAdapter(),
            state_dir=Path.home() / ".ouroboros" / "data",
        )

        # Start new interview
        result = await engine.start_interview(
            initial_context="I want to build a CLI tool for task management"
        )

        # Ask questions in rounds
        while not state.is_complete:
            question_result = await engine.ask_next_question(state)
            if question_result.is_ok:
                question = question_result.value
                user_response = input(question)
                await engine.record_response(state, user_response)

        # Generate final seed (not implemented in this story)

    Note:
        The model can be configured via OuroborosConfig.clarification.default_model
        or passed directly to the constructor.
    """

    llm_adapter: LLMAdapter
    state_dir: Path = field(default_factory=lambda: Path.home() / ".ouroboros" / "data")
    model: str | None = None
    model_is_explicit: bool = field(default=False, init=False)
    temperature: float = 0.7
    max_tokens: int = 2048
    _MAX_TOTAL_PROMPT_CHARS = AGENT_SDK_CLI_SAFE_PROMPT_CHARS
    _MAX_SYSTEM_PROMPT_CHARS = 3500
    _MIN_SYSTEM_PROMPT_CHARS = 1200
    _MAX_INITIAL_CONTEXT_SYSTEM_CHARS = 1800
    _MAX_INITIAL_CONTEXT_TOTAL_CHARS = MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS
    _INITIAL_CONTEXT_SUMMARY_QUESTION = INITIAL_CONTEXT_SUMMARY_QUESTION
    suppress_tool_use_prompt_cues: bool = False
    # When True, ``ask_next_question`` generates three persona candidates
    # (contrarian / architect / researcher) concurrently and deterministically
    # selects the one targeting the worst-scoring ambiguity dimension. Defaults
    # to False so the single-call path — and every existing test — is preserved.
    # Requires a stored per-dimension ambiguity breakdown; falls back to the
    # single call when no breakdown or no valid candidate is available.
    question_candidate_panel: bool = False

    def __post_init__(self) -> None:
        """Ensure state directory exists."""
        self.model_is_explicit = self.model is not None
        if self.model is None:
            self.model = get_llm_model_for_role("interview")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state_file_path(self, interview_id: str) -> Path:
        """Get the path to the state file for an interview.

        Args:
            interview_id: The interview ID.

        Returns:
            Path to the state file.
        """
        return self.state_dir / f"interview_{interview_id}.json"

    async def start_interview(
        self, initial_context: str, interview_id: str | None = None, cwd: str | None = None
    ) -> Result[InterviewState, ValidationError]:
        """Start a new interview session.

        Args:
            initial_context: The initial context or idea provided by the user.
            interview_id: Optional interview ID (generated if not provided).
            cwd: Optional working directory. When provided, auto-detects
                brownfield projects and runs codebase exploration before the
                first question.

        Returns:
            Result containing the new InterviewState or ValidationError.
        """
        # Validate initial context with security limits
        is_valid, error_msg = InputValidator.validate_initial_context(initial_context)
        if not is_valid:
            return Result.err(ValidationError(error_msg, field="initial_context"))

        if interview_id is None:
            interview_id = f"interview_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

        state = InterviewState(
            interview_id=interview_id,
            initial_context=initial_context,
        )

        # Auto-detect brownfield projects from CWD.
        # codebase_paths is informational only — the main session (not MCP)
        # handles codebase exploration directly via Read/Glob/Grep.
        if cwd:
            from ouroboros.bigbang.explore import detect_brownfield

            if detect_brownfield(cwd):
                state.is_brownfield = True
                state.codebase_paths = [{"path": cwd, "role": "primary"}]

        log.info(
            "interview.started",
            interview_id=interview_id,
            initial_context_length=len(initial_context),
            is_brownfield=state.is_brownfield,
        )

        # Persist the freshly-created state immediately so that downstream
        # failures (e.g. a question-generation timeout) still leave a
        # resumable handle on disk.  Hard-fail on save errors: the
        # recovery contract downstream (recoverable ``MCPToolResult`` with
        # ``meta.session_id`` returned from ``InterviewHandler`` on a
        # first-question failure) assumes the on-disk state file exists.
        # Returning ``Result.ok`` after a failed save would silently lie
        # to callers that the session is resumable.  See Q00/ouroboros#687.
        save_result = await self.save_state(state)
        if save_result.is_err:
            log.error(
                "interview.start_save_failed",
                interview_id=interview_id,
                error=str(save_result.error),
            )
            return Result.err(
                ValidationError(
                    f"Failed to persist initial interview state: {save_result.error}",
                    field="interview_id",
                    value=interview_id,
                )
            )

        return Result.ok(state)

    async def ask_next_question(
        self, state: InterviewState
    ) -> Result[str, ProviderError | ValidationError]:
        """Generate the next question based on current state.

        Args:
            state: Current interview state.

        Returns:
            Result containing the next question or error.
        """
        if state.is_complete and state.needs_initial_context_summary:
            return Result.ok(self._INITIAL_CONTEXT_SUMMARY_QUESTION)

        if state.is_complete:
            return Result.err(
                ValidationError(
                    "Interview is already complete",
                    field="status",
                    value=state.status,
                )
            )
        effective_initial_context = self._effective_initial_context(state)
        if effective_initial_context is None:
            return Result.ok(self._INITIAL_CONTEXT_SUMMARY_QUESTION)

        adapter_question = state.next_adapter_question()
        if adapter_question is not None:
            return Result.ok(adapter_question)

        # Build the context from previous rounds
        conversation_history = self._build_conversation_history(
            state,
            initial_context=effective_initial_context,
        )
        preserve_prefix_messages = (
            1 if len(effective_initial_context) > self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS else 0
        )
        history_budget = (
            self._MAX_TOTAL_PROMPT_CHARS
            - self._MIN_SYSTEM_PROMPT_CHARS
            - AGENT_SDK_CLI_FIXED_FRAMING_CHARS
            - AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
        )
        conversation_history = self._trim_messages_to_budget(
            conversation_history,
            max_chars=history_budget,
            preserve_prefix_messages=preserve_prefix_messages,
        )

        # Generate next question. Budget against estimated serialized CLI
        # prompt cost, not raw message content, because CLI adapters add
        # framing around every message before sending prompts to the model.
        history_cost = self._message_budget_cost(conversation_history)
        system_prompt_budget = min(
            self._MAX_SYSTEM_PROMPT_CHARS,
            self._MAX_TOTAL_PROMPT_CHARS
            - AGENT_SDK_CLI_FIXED_FRAMING_CHARS
            - AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
            - history_cost,
        )
        system_prompt = self._build_system_prompt(
            state,
            initial_context=effective_initial_context,
            max_chars=system_prompt_budget,
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            *conversation_history,
        ]

        assert self.model is not None
        config = CompletionConfig(
            model=self.model,
            role="clarification",
            model_is_explicit=self.model_is_explicit,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        log.debug(
            "interview.generating_question",
            interview_id=state.interview_id,
            round_number=state.current_round_number,
            message_count=len(messages),
        )

        # Question candidate panel (K2): three persona candidates + deterministic
        # selection. Falls through to the single call when disabled, when no
        # per-dimension breakdown is stored, or when no valid candidate is
        # produced — so existing behavior is always reachable.
        if self.question_candidate_panel:
            candidate = await self._select_question_from_candidates(
                state,
                system_prompt=system_prompt,
                conversation_history=conversation_history,
                config=config,
            )
            if candidate is not None:
                return Result.ok(candidate)

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "interview.question_generation_failed",
                interview_id=state.interview_id,
                round_number=state.current_round_number,
                error=str(result.error),
            )
            return Result.err(result.error)

        question = result.value.content.strip()

        log.info(
            "interview.question_generated",
            interview_id=state.interview_id,
            round_number=state.current_round_number,
            question_length=len(question),
        )

        return Result.ok(question)

    async def _select_question_from_candidates(
        self,
        state: InterviewState,
        *,
        system_prompt: str,
        conversation_history: list[Message],
        config: CompletionConfig,
    ) -> str | None:
        """Generate persona candidates and deterministically select one.

        Returns the selected question, or ``None`` to signal the caller should
        fall back to the single-call path (no stored breakdown, or no valid
        candidate produced).
        """
        dimension_clarity = _dimension_clarity_from_breakdown(state.ambiguity_breakdown)
        if not dimension_clarity:
            return None

        candidates = await self._generate_question_candidates(
            state,
            system_prompt=system_prompt,
            conversation_history=conversation_history,
            config=config,
        )
        if not candidates:
            return None

        selected = select_question_candidate(candidates, dimension_clarity)
        log.info(
            "interview.question_candidate_selected",
            interview_id=state.interview_id,
            persona=selected.persona,
            target_dimension=selected.target_dimension,
            candidate_count=len(candidates),
        )
        return selected.question

    async def _generate_question_candidates(
        self,
        state: InterviewState,
        *,
        system_prompt: str,
        conversation_history: list[Message],
        config: CompletionConfig,
    ) -> list[QuestionCandidate]:
        """Generate one persona question candidate per persona, concurrently."""
        persona_roles = _question_candidate_persona_roles()

        async def _one(persona: str) -> QuestionCandidate | None:
            role = persona_roles.get(persona, persona)
            persona_prompt = (
                f"{system_prompt}\n\n"
                f"## Persona Lens: {persona} — {role}\n"
                "Propose ONE clarifying question through this persona's lens, "
                "aimed at the single ambiguity dimension you judge weakest.\n\n"
                "Respond ONLY with JSON, no other text:\n"
                '{"question": "string", "target_dimension": "goal_clarity" | '
                '"constraint_clarity" | "success_criteria_clarity" | '
                '"context_clarity"}'
            )
            messages = [
                Message(role=MessageRole.SYSTEM, content=persona_prompt),
                *conversation_history,
            ]
            try:
                result = await self.llm_adapter.complete(messages, config)
            except Exception as exc:  # noqa: BLE001 - candidate is best-effort
                log.warning(
                    "interview.question_candidate_failed",
                    interview_id=state.interview_id,
                    persona=persona,
                    error=str(exc),
                )
                return None
            if result.is_err:
                return None
            return _parse_question_candidate(persona, result.value.content)

        results = await asyncio.gather(*(_one(persona) for persona in QUESTION_CANDIDATE_PERSONAS))
        return [candidate for candidate in results if candidate is not None]

    async def record_response(
        self, state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, ValidationError]:
        """Record the user's response to the current question.

        Args:
            state: Current interview state.
            user_response: The user's response.
            question: The question that was asked.

        Returns:
            Result containing updated state or ValidationError.
        """
        # Validate user response with security limits
        is_valid, error_msg = InputValidator.validate_user_response(user_response)
        if not is_valid:
            return Result.err(ValidationError(error_msg, field="user_response"))

        if state.is_complete:
            if not state.can_reopen:
                return Result.err(
                    ValidationError(
                        "Cannot record response - interview is complete",
                        field="status",
                        value=state.status,
                    )
                )
            prior_ambiguity = state.ambiguity_score
            prior_streak = state.completion_candidate_streak
            state.status = InterviewStatus.IN_PROGRESS
            state.clear_stored_ambiguity()
            # The completion-candidate streak is the other half of the cached
            # closure decision (authoring_handlers auto-completes when
            # streak >= AUTO_COMPLETE_STREAK_REQUIRED). Leaving it intact would
            # let the reopened session auto-close after a single qualifying
            # score instead of rebuilding the required two-signal stability.
            state.completion_candidate_streak = 0
            log.info(
                "interview.reopened",
                interview_id=state.interview_id,
                prior_ambiguity_score=prior_ambiguity,
                prior_completion_candidate_streak=prior_streak,
            )

        state.record_adapter_answer(question, user_response)

        # Create new round
        round_data = InterviewRound(
            round_number=state.current_round_number,
            question=question,
            user_response=user_response,
        )

        state.rounds.append(round_data)
        detected_terms = detect_explicit_confusion_terms(user_response)
        if detected_terms:
            state.merge_turn_context(InterviewTurnContext(confused_terms=detected_terms))
        state.invalidate_requirement_distillation()
        state.mark_updated()

        log.info(
            "interview.response_recorded",
            interview_id=state.interview_id,
            round_number=round_data.round_number,
            response_length=len(user_response),
        )

        # Note: No auto-complete on round limit. User controls when to stop.
        # CLI handles prompting user to continue after each round.

        return Result.ok(state)

    async def save_state(self, state: InterviewState) -> Result[Path, ValidationError]:
        """Persist interview state to disk.

        Uses file locking to prevent race conditions during concurrent access.
        The blocking file I/O is offloaded to a thread to avoid stalling the
        asyncio event loop.

        Args:
            state: The interview state to save.

        Returns:
            Result containing path to saved file or ValidationError.
        """
        try:
            file_path = self._state_file_path(state.interview_id)
            state.mark_updated()
            # Serialize while still on the event-loop (CPU-bound, not I/O)
            content = state.model_dump_json(indent=2)

            def _sync_write() -> None:
                with _file_lock(file_path, exclusive=True):
                    file_path.write_text(content, encoding="utf-8")

            await asyncio.to_thread(_sync_write)

            log.info(
                "interview.state_saved",
                interview_id=state.interview_id,
                file_path=str(file_path),
            )

            return Result.ok(file_path)
        except (OSError, ValueError) as e:
            log.exception(
                "interview.state_save_failed",
                interview_id=state.interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to save interview state: {e}",
                    details={"interview_id": state.interview_id},
                )
            )

    async def load_state(self, interview_id: str) -> Result[InterviewState, ValidationError]:
        """Load interview state from disk.

        Uses file locking to prevent race conditions during concurrent access.
        The blocking file I/O is offloaded to a thread to avoid stalling the
        asyncio event loop.

        Args:
            interview_id: The interview ID to load.

        Returns:
            Result containing loaded state or ValidationError.
        """
        file_path = self._state_file_path(interview_id)

        if not file_path.exists():
            return Result.err(
                ValidationError(
                    f"Interview state not found: {interview_id}",
                    field="interview_id",
                    value=interview_id,
                )
            )

        try:

            def _sync_read() -> str:
                with _file_lock(file_path, exclusive=False):
                    return file_path.read_text(encoding="utf-8")

            content = await asyncio.to_thread(_sync_read)

            state = InterviewState.model_validate_json(content)
            state.discard_stale_requirement_distillation()

            log.info(
                "interview.state_loaded",
                interview_id=interview_id,
                rounds=len(state.rounds),
            )

            return Result.ok(state)
        except (OSError, ValueError) as e:
            log.exception(
                "interview.state_load_failed",
                interview_id=interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to load interview state: {e}",
                    field="interview_id",
                    value=interview_id,
                    details={"file_path": str(file_path)},
                )
            )

    def _build_system_prompt(
        self,
        state: InterviewState,
        initial_context: str | None = None,
        max_chars: int | None = None,
    ) -> str:
        """Build the system prompt for question generation.

        Args:
            state: Current interview state.
            initial_context: Optional prompt-safe context to use instead of
                ``state.initial_context``.
            max_chars: Optional cap for the returned system prompt. When omitted,
                uses the standard system-prompt cap.

        Returns:
            The system prompt.
        """
        from ouroboros.agents.loader import load_agent_prompt

        max_prompt_chars = max_chars or self._MAX_SYSTEM_PROMPT_CHARS
        effective_round_number = self._next_conversation_round_number(state)
        round_info = f"Round {effective_round_number}"

        base_prompt = (
            _TOOLLESS_INTERVIEW_BASE_PROMPT
            if self.suppress_tool_use_prompt_cues
            else load_agent_prompt("socratic-interviewer")
        )

        context_for_prompt = (
            initial_context if initial_context is not None else state.initial_context
        )
        prompt_initial_context = self._initial_context_for_system_prompt(context_for_prompt)

        # For first round, add explicit instruction to start directly with a question
        if effective_round_number == 1:
            dynamic_header = (
                f"You are an expert requirements engineer conducting a Socratic interview.\n\n"
                f"CRITICAL: Start your FIRST response with a DIRECT QUESTION about the project. "
                f'Do NOT introduce yourself. Do NOT say "I\'ll conduct" or "Let me ask". '
                f"Just ask a specific, clarifying question immediately.\n\n"
                f"This is {round_info}. Your ONLY job is to ask questions that reduce ambiguity.\n\n"
                f"Initial context: {prompt_initial_context}\n"
            )
        else:
            dynamic_header = (
                f"You are an expert requirements engineer conducting a Socratic interview.\n\n"
                f"This is {round_info}. Your ONLY job is to ask questions that reduce ambiguity.\n\n"
                f"Initial context: {prompt_initial_context}\n"
            )

        # Answer prefix hints — always present so the question generator
        # can interpret enriched answers regardless of brownfield status.
        # Under budget pressure the FULL glossary is swapped for the compact
        # one-liner below (bot-review round-29): hard truncation used to cut
        # the last entry mid-token ("- [from-d"), silently dropping the
        # [from-data] semantics while its answers stayed in history. The
        # compact form preserves EVERY prefix meaning; the retained initial
        # context is never the victim.
        if self.suppress_tool_use_prompt_cues:
            answer_prefix_hints = (
                "Answer prefixes the caller may use:\n"
                "- [from-code]: Caller-supplied existing-system context (factual).\n"
                "- [from-user]: Human decisions/judgments.\n"
                "- [from-research]: Caller-supplied external context.\n"
                "- [from-data]: Caller-supplied data evidence "
                "(factual at query time, may be stale)."
            )
        else:
            answer_prefix_hints = (
                "Answer prefixes the caller may use:\n"
                "- [from-code]: Existing codebase state (factual, read from files).\n"
                "- [from-user]: Human decisions/judgments.\n"
                "- [from-research]: Externally researched information (API docs, pricing, compatibility).\n"
                "- [from-data]: Data evidence from metrics/DB/warehouse queries "
                "(factual at query time, may be stale — treat as description, not decision)."
            )
        self._answer_prefix_hints_full = answer_prefix_hints
        self._answer_prefix_hints_compact = (
            "Prefixes: [from-code]=existing-system fact; [from-user]=human "
            "decision; [from-research]=external fact; [from-data]=point-in-time "
            "data description (not a decision, may be stale)."
        )
        dynamic_header += f"\n\n{answer_prefix_hints}"
        # Brownfield hint: main session handles code reading, MCP just asks questions
        if state.is_brownfield:
            if self.suppress_tool_use_prompt_cues:
                dynamic_header += (
                    "\n\nThis is a BROWNFIELD project. The caller will enrich answers "
                    "with existing-system context. Focus your questions on INTENT and "
                    "DECISIONS, not on discovering what exists."
                )
            else:
                dynamic_header += (
                    "\n\nThis is a BROWNFIELD project. The caller (main session) has direct "
                    "codebase access and will enrich answers with code context. Focus your "
                    "questions on INTENT and DECISIONS, not on discovering what exists."
                )

        ambiguity_snapshot = self._build_ambiguity_snapshot_prompt(state)
        if ambiguity_snapshot:
            dynamic_header += f"\n\n{ambiguity_snapshot}"

        perspective_panel = self._build_perspective_panel_prompt(state)

        _OVERHEAD = 20  # newlines, ellipsis, separators

        # Preserve the dynamic header first; it contains the capped initial
        # context and first-turn instructions. Trim the optional panel/base
        # prompt before falling back to hard-truncating the header.
        available_after_header = max_prompt_chars - len(dynamic_header) - _OVERHEAD
        if available_after_header <= 0:
            # Swap the full prefix glossary for its compact one-liner BEFORE
            # any hard cut (round-29): every prefix keeps its semantics and
            # the retained initial context keeps priority.
            full_hints = getattr(self, "_answer_prefix_hints_full", None)
            compact_hints = getattr(self, "_answer_prefix_hints_compact", None)
            if full_hints and compact_hints and full_hints in dynamic_header:
                dynamic_header = dynamic_header.replace(full_hints, compact_hints, 1)
                available_after_header = max_prompt_chars - len(dynamic_header) - _OVERHEAD
        if available_after_header <= 0:
            dynamic_header = dynamic_header[: max_prompt_chars - _OVERHEAD]
            perspective_panel = ""
            base_budget = 0
        elif len(perspective_panel) > available_after_header:
            perspective_panel = perspective_panel[:available_after_header]
            base_budget = 0
        else:
            base_budget = available_after_header - len(perspective_panel)

        trimmed_base = base_prompt[:base_budget] if base_budget < len(base_prompt) else base_prompt
        full_prompt = f"{dynamic_header}\n{trimmed_base}\n\n{perspective_panel}"

        # Hard-truncate as final safety net
        if len(full_prompt) > max_prompt_chars:
            full_prompt = full_prompt[:max_prompt_chars]

        return full_prompt

    def _initial_context_for_system_prompt(self, initial_context: str) -> str:
        """Return the initial context portion safe to embed in system prompt."""
        if len(initial_context) <= self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS:
            return initial_context
        return (
            initial_context[: self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS]
            + "\n\n[Initial context continues in the first user message.]"
        )

    def _initial_context_overflow_message(self, initial_context: str) -> str:
        """Return overflow initial context as durable user-message content."""
        if len(initial_context) <= self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS:
            return ""
        overflow = initial_context[self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS :]
        return f"Additional initial context omitted from the system prompt:\n{overflow}"

    def _effective_initial_context(self, state: InterviewState) -> str | None:
        """Return prompt-safe initial context, or None when a summary is needed."""
        context = prompt_safe_initial_context(state)
        if context == INITIAL_CONTEXT_SUMMARY_REQUIRED:
            return None
        return context

    def _build_ambiguity_snapshot_prompt(self, state: InterviewState) -> str:
        """Build prompt context from the latest ambiguity snapshot."""
        if state.ambiguity_score is None:
            return ""

        from pydantic import ValidationError as PydanticValidationError

        from ouroboros.bigbang.ambiguity import (
            AmbiguityScore,
            ScoreBreakdown,
            get_completion_floor_failures,
            get_milestone,
        )

        milestone, milestone_desc = get_milestone(state.ambiguity_score)

        lines = [
            "## Current Ambiguity Snapshot",
            f"- Overall ambiguity: {state.ambiguity_score:.2f}",
            f"- Milestone: **{milestone.value.upper()}** — {milestone_desc}",
        ]

        reconstructed_score: AmbiguityScore | None = None
        if isinstance(state.ambiguity_breakdown, dict):
            try:
                reconstructed_score = AmbiguityScore(
                    overall_score=state.ambiguity_score,
                    breakdown=ScoreBreakdown.model_validate(state.ambiguity_breakdown),
                )
            except PydanticValidationError:
                reconstructed_score = None

            weakest_components: list[tuple[float, str, str]] = []
            for payload in state.ambiguity_breakdown.values():
                if not isinstance(payload, dict):
                    continue
                clarity = payload.get("clarity_score")
                if clarity is None:
                    continue
                weakest_components.append(
                    (
                        float(clarity),
                        str(payload.get("name", "Unknown")),
                        str(payload.get("justification", "")),
                    )
                )

            weakest_components.sort(key=lambda item: item[0])
            for clarity, name, justification in weakest_components[:2]:
                lines.append(f"- Weakest area: {name} ({clarity:.2f} clarity)")
                if justification:
                    lines.append(f"  Reason: {justification}")

        if reconstructed_score is not None:
            floor_failures = get_completion_floor_failures(
                reconstructed_score,
                is_brownfield=state.is_brownfield,
            )
            if floor_failures:
                lines.append(f"- Per-dimension gaps: {'; '.join(floor_failures)}")
                lines.append(
                    "- Keep drilling those dimensions before asking a closure-style question, "
                    "even when overall ambiguity reads low."
                )
            else:
                lines.append("- Per-dimension gaps: none")

        lines.append("- Drill into the weakest area with a concrete, scenario-grounded question.")
        return "\n".join(lines)

    def _select_perspectives(self, state: InterviewState) -> tuple[InterviewPerspective, ...]:
        """Choose the active perspective panel for the current round."""
        from ouroboros.bigbang.ambiguity import SEED_CLOSER_ACTIVATION_THRESHOLD

        perspectives: list[InterviewPerspective] = [InterviewPerspective.BREADTH_KEEPER]

        effective_round_number = self._next_conversation_round_number(state)
        if effective_round_number <= 2:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                ]
            )
        elif effective_round_number <= 5:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                    InterviewPerspective.ARCHITECT,
                ]
            )
        else:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                    InterviewPerspective.ARCHITECT,
                ]
            )

        if (
            state.ambiguity_score is not None
            and state.ambiguity_score <= SEED_CLOSER_ACTIVATION_THRESHOLD
        ):
            perspectives.append(InterviewPerspective.SEED_CLOSER)

        if state.is_brownfield and InterviewPerspective.ARCHITECT not in perspectives:
            perspectives.append(InterviewPerspective.ARCHITECT)

        # Preserve declaration order while removing duplicates.
        return tuple(dict.fromkeys(perspectives))

    def _build_perspective_panel_prompt(self, state: InterviewState) -> str:
        """Build instructions for the internal perspective panel."""
        if self.suppress_tool_use_prompt_cues:
            return "\n".join(
                [
                    "## Perspective Panel",
                    "Silently check breadth, simplicity, architecture, and closure readiness.",
                    "Use those perspectives only to choose one clarifying question.",
                    "",
                    "## Panel Synthesis Rules",
                    "- Keep independent ambiguity tracks visible instead of collapsing onto one favorite subtopic.",
                    "- Preserve both implementation and written-output requirements when the user asked for both.",
                    "- Prefer breadth recap questions when multiple unresolved tracks still exist.",
                    "- Only ask a closure question when closure mode is active; otherwise keep drilling into the weakest area.",
                    "- Even when the score is seed-ready, do not end the interview on the first low-ambiguity turn.",
                ]
            )
        strategies = _load_interview_perspective_strategies()
        sections = [
            "## Perspective Panel",
            "Before asking the next question, silently consult these internal agents.",
            "They are planning aids only. Emit exactly one final question to the user.",
            "",
        ]

        for perspective in self._select_perspectives(state):
            strategy = strategies[perspective]
            sections.append(f"### {perspective.value}")
            sections.append(f"Focus: {strategy.system_prompt}")
            if strategy.approach_instructions:
                sections.append("Approach cues:")
                sections.extend(f"- {item}" for item in strategy.approach_instructions[:3])
            if strategy.question_templates:
                sections.append("Question patterns:")
                sections.extend(f"- {item}" for item in strategy.question_templates[:2])
            sections.append("")

        sections.extend(
            [
                "## Panel Synthesis Rules",
                "- Keep independent ambiguity tracks visible instead of collapsing onto one favorite subtopic.",
                "- If one file, abstraction, or bug has dominated several rounds, zoom back out before going deeper.",
                "- Preserve both implementation and written-output requirements when the user asked for both.",
                "- Prefer breadth recap questions when multiple unresolved tracks still exist.",
                "- Only ask a closure question when closure mode is active; otherwise keep drilling into the weakest area.",
                "- Even when the score is seed-ready, do not end the interview on the first low-ambiguity turn.",
            ]
        )

        return "\n".join(sections)

    def _next_conversation_round_number(self, state: InterviewState) -> int:
        """Return the next real interview round, ignoring summary sentinels."""
        return (
            sum(
                1
                for round_data in state.rounds
                if round_data.question != self._INITIAL_CONTEXT_SUMMARY_QUESTION
            )
            + 1
        )

    # Agent SDK CLI can return empty responses when the combined prompt
    # (system_prompt + conversation history) exceeds an internal threshold.
    # Cap each user response to keep the total prompt within safe limits.
    _MAX_USER_RESPONSE_CHARS = 4000

    def _build_conversation_history(
        self,
        state: InterviewState,
        initial_context: str | None = None,
    ) -> list[Message]:
        """Build conversation history from completed rounds.

        Long user responses are truncated to prevent Agent SDK CLI from
        returning empty responses due to prompt size.

        Args:
            state: Current interview state.
            initial_context: Prompt-safe initial context to use for overflow
                instead of ``state.initial_context``.

        Returns:
            List of messages representing the conversation.
        """
        messages: list[Message] = []
        context_for_prompt = (
            initial_context if initial_context is not None else state.initial_context
        )

        has_conversation_rounds = self._next_conversation_round_number(state) > 1
        overflow = self._initial_context_overflow_message(context_for_prompt)
        if overflow:
            messages.append(Message(role=MessageRole.USER, content=overflow))
        elif not has_conversation_rounds and context_for_prompt:
            # Some chat providers reject a first request that contains only a
            # system message. Mirror the user's initial context as the first
            # user turn so provider adapters always receive a non-system
            # conversation message on round one. Summary-recovery sentinel
            # rounds do not count as conversation because they are skipped
            # below. Long contexts keep using the overflow path above to
            # preserve prompt-budget caps.
            user_content = context_for_prompt
            if len(user_content) > self._MAX_USER_RESPONSE_CHARS:
                user_content = user_content[: self._MAX_USER_RESPONSE_CHARS] + "..."
            messages.append(Message(role=MessageRole.USER, content=user_content))

        for round_data in state.rounds:
            if round_data.question == self._INITIAL_CONTEXT_SUMMARY_QUESTION:
                continue
            messages.append(Message(role=MessageRole.ASSISTANT, content=round_data.question))
            if round_data.user_response:
                response = round_data.user_response
                if len(response) > self._MAX_USER_RESPONSE_CHARS:
                    response = response[: self._MAX_USER_RESPONSE_CHARS] + "..."
                messages.append(Message(role=MessageRole.USER, content=response))

        return messages

    def _message_budget_cost(self, messages: list[Message]) -> int:
        """Estimate serialized CLI prompt cost for message content and framing."""
        return sum(
            len(message.content) + AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS for message in messages
        )

    def _trim_messages_to_budget(
        self,
        messages: list[Message],
        *,
        max_chars: int,
        preserve_prefix_messages: int = 0,
    ) -> list[Message]:
        """Keep durable prefix messages plus newest conversation within a CLI budget."""
        if self._message_budget_cost(messages) <= max_chars:
            return messages

        prefix = messages[:preserve_prefix_messages]
        remaining_messages = messages[preserve_prefix_messages:]
        prefix_chars = self._message_budget_cost(prefix)
        if prefix_chars >= max_chars:
            retained_prefix: list[Message] = []
            used_prefix_chars = 0
            for message in prefix:
                remaining = max_chars - used_prefix_chars - AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
                if remaining <= 0:
                    break
                if len(message.content) <= remaining:
                    retained_prefix.append(message)
                    used_prefix_chars += (
                        len(message.content) + AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
                    )
                else:
                    retained_prefix.append(
                        Message(role=message.role, content=message.content[:remaining])
                    )
                    break
            return retained_prefix

        retained: list[Message] = []
        used_chars = prefix_chars
        for message in reversed(remaining_messages):
            remaining = max_chars - used_chars - AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
            if remaining <= 0:
                break
            if len(message.content) <= remaining:
                retained.append(message)
                used_chars += len(message.content) + AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS
            else:
                retained.append(Message(role=message.role, content=message.content[-remaining:]))
                break
        return [*prefix, *reversed(retained)]

    async def complete_interview(
        self, state: InterviewState
    ) -> Result[InterviewState, ValidationError]:
        """Mark the interview as completed.

        Args:
            state: Current interview state.

        Returns:
            Result containing updated state or ValidationError.
        """
        if state.status == InterviewStatus.COMPLETED:
            return Result.ok(state)

        state.status = InterviewStatus.COMPLETED
        state.mark_updated()

        log.info(
            "interview.completed",
            interview_id=state.interview_id,
            total_rounds=len(state.rounds),
        )

        return Result.ok(state)

    async def list_interviews(self) -> list[dict[str, Any]]:
        """List all interview sessions in the state directory.

        Returns:
            List of interview metadata dictionaries.
        """
        interviews = []

        for file_path in self.state_dir.glob("interview_*.json"):
            try:
                content = file_path.read_text(encoding="utf-8")
                state = InterviewState.model_validate_json(content)
                interviews.append(
                    {
                        "interview_id": state.interview_id,
                        "status": state.status,
                        "rounds": len(state.rounds),
                        "created_at": state.created_at,
                        "updated_at": state.updated_at,
                    }
                )
            except (OSError, ValueError) as e:
                log.warning(
                    "interview.list_failed_for_file",
                    file_path=str(file_path),
                    error=str(e),
                )
                continue

        return sorted(interviews, key=lambda x: x["updated_at"], reverse=True)
