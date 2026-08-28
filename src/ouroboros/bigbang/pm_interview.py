"""PM Interview Engine — composition wrapper around InterviewEngine.

Adds PM-specific behavior on top of the existing InterviewEngine:
- Question classification (planning vs development)
- Reframing technical questions for PM audience
- Deferred item tracking for dev-only questions
- PMSeed generation from completed interview
- Brownfield repo management via the configured runtime database

The engine does not read repositories (RFC Q00/ouroboros#1937 decision 9).
Reading code belongs to the advisory lanes, which run in the session that fans
out and put their findings beside the question; an engine-side summary would be
evidence the person judging never receives.

Composition pattern: PMInterviewEngine *wraps* InterviewEngine without
modifying its internals. The inner engine handles question generation,
state persistence, and round management. The outer engine intercepts
questions for classification and collects PM-specific metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.ambiguity import (
    AmbiguityScore,
    AmbiguityScorer,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.answer_provenance import extraction_rounds
from ouroboros.bigbang.brownfield import (
    load_brownfield_repos_as_dicts as _load_brownfield_dicts,
)
from ouroboros.bigbang.inner_guidance import (
    compose_steered_prompt,
    reserve_steering_extension,
)
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewEngine,
    InterviewState,
    initial_context_summary_missing,
    prompt_safe_initial_context,
)
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    ClassifierOutputType,
    QuestionCategory,
    QuestionClassifier,
    classification_policy_prompt,
)
from ouroboros.bigbang.turn_planner import InterviewTurnPlanner
from ouroboros.config import get_llm_model_for_role
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.owner_only import write_owner_only
from ouroboros.core.pm_snapshot import refresh_pm_snapshot_worktrees
from ouroboros.core.types import Result
from ouroboros.orchestrator.capabilities.question_text import normalize_question_text
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

log = structlog.get_logger()

PM_UNCERTAINTY_GUIDANCE = (
    "If a product question is not settled, do not invent certainty. "
    "Treat uncertain answers as explicit PM signal: record assumptions when "
    "the user is making a tentative claim, or decide-later items when the "
    "answer depends on missing information, a stakeholder decision, or a "
    "future product choice."
)

_SEED_DIR = Path.home() / ".ouroboros" / "seeds"
_PM_SYSTEM_PROMPT_PREFIX = f"""\
You are a Product Requirements interviewer helping a PM define their product.
The PRD will drive autonomous AI planning, implementation, and verification
downstream — elicit decisions precise enough for that.

A PRD is a contract between the PM and the developers: success criteria are the
behavior and policy the PM must observe in the delivered feature to accept it
as built. Post-launch outcomes have no place in this contract.

Focus on: goal, user stories, constraints, success criteria, assumptions.

{PM_UNCERTAINTY_GUIDANCE}

"""

_OPENING_QUESTION = (
    "What do you want to build? Tell me about the product or feature "
    "you have in mind — the problem it solves, who it's for, and any "
    "initial ideas you already have."
)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a requirements extraction engine. Given a PM interview transcript,
extract structured product requirements. Preserve uncertainty explicitly: do not
turn uncertain, stakeholder-dependent, or unknown answers into confirmed
requirements. Put tentative claims in assumptions and unresolved choices in
decide_later_items.

A PRD is a contract between the PM and the developers: success_criteria are the
behavior and policy the PM must observe in the delivered feature to accept it as
built. Post-launch outcomes mentioned in the transcript are the PM's follow-up
work, not contract terms — record them under assumptions or decide_later_items.

Respond ONLY with valid JSON in this exact format:
{
    "product_name": "Short product/feature name",
    "goal": "High-level product goal statement",
    "user_stories": [
        {"persona": "User type", "action": "what they want", "benefit": "why"}
    ],
    "constraints": ["constraint 1", "constraint 2"],
    "success_criteria": ["criterion 1", "criterion 2"],
    "deferred_items": ["deferred item 1"],
    "decide_later_items": ["original question text for items to decide later"],
    "assumptions": ["assumption 1"]
}
"""


# Identifies the PRD-contract paragraph — the policy #1663 exists to
# enforce. It is shed last so tight budgets drop the supporting paragraphs
# before the policy itself.
_PM_CONTRACT_MARKER = "contract between the PM and the developers"

#: Ceiling on questions per PM turn (RFC #2222 decision 1) — a target the
#: generator may stay under, never a quota to pad toward.
MAX_QUESTIONS_PER_TURN = 3

#: Mirrors the planner's closure-mode activation ("Overall ambiguity <= 0.25
#: activates closure mode ... do not open a new topic"). A companion is by
#: definition a second topic, so at or below this score the batch is one.
_CLOSURE_MODE_AMBIGUITY = 0.25


def _decision_only_view(state: InterviewState) -> InterviewState:
    """Project ``state`` as the ambiguity scorer should read it.

    Scoring asks how clear the *requirements* are, and requirements are what
    the person decided.  What they weighed on the way is neither clearer nor
    vaguer for having been consulted, so an observation's content must not
    reach the scorer: a well-researched question would otherwise read as a
    well-decided one, which is the same confusion ``check_completion`` avoids
    by counting decisions rather than rounds.

    The answer is dropped, not the round.  This mirrors the per-role rule in
    ``answer_provenance``: an observation is withheld from the slot that
    carries authority and left alone in the slot that carries the question.
    The scorer then sees the grounded question standing unanswered — which is
    what it is until the person answers it — so the finding raises the
    remaining ambiguity instead of lowering it.

    The initial-context summary round is passed through untouched.
    ``prompt_safe_initial_context`` recovers a long context from that answer,
    and blanking it would make scoring fail closed on exactly the sessions
    that carry the most context.

    This is a PM-local projection on purpose.  The same gap exists in
    ``AmbiguityScorer`` for every other caller, but closing it there is an
    interview-core change this PR does not own; see the Out-of-scope section
    of the PR body.  When the shared projection lands, this collapses into it.
    """
    projected = [
        round_data
        if round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION
        or round_data.provenance != "observation"
        else round_data.model_copy(update={"user_response": None})
        for round_data in state.rounds
    ]
    return state.model_copy(update={"rounds": projected})


def decision_round_count(state: InterviewState) -> int:
    """Count authoritative PM decisions eligible for completion."""
    return sum(
        1
        for round_data in state.rounds
        if round_data.user_response is not None
        and round_data.question != INITIAL_CONTEXT_SUMMARY_QUESTION
        and round_data.provenance == "user"
    )


@dataclass(frozen=True, slots=True)
class PMInterviewTurnPlan:
    """Atomic PM question, ambiguity result, and routing classification."""

    question: str
    ambiguity: AmbiguityScore | None
    classification: ClassificationResult
    raw_payload: dict[str, Any]


#: What a round holds when the user asked to leave the decision open.
#:
#: A skip is recorded, not skipped: the round exists so the question is not
#: asked again, and it says the decision is open rather than answered. These
#: are named because two runtimes write them — the engine below, and the
#: plugin path that has no engine to reach — and a second spelling of one
#: sentence is a transcript whose meaning depends on which runtime took the
#: call.
DECIDE_LATER_PLACEHOLDER = "[Decide later] To be determined — user chose to decide later."
DEFERRED_PLACEHOLDER = (
    "[Deferred to development phase] This technical decision will be addressed "
    "during the development interview."
)


@dataclass
class PMInterviewEngine:
    """PM interview engine — wraps InterviewEngine via composition.

    This engine adds a PM-specific layer on top of the standard
    InterviewEngine. It intercepts generated questions, classifies them
    as planning vs development, reframes technical questions for PMs,
    and tracks deferred items.

    The inner InterviewEngine is fully responsible for:
    - Question generation via LLM
    - State management and persistence
    - Round tracking
    - Brownfield codebase exploration (delegated to inner engine)

    The PMInterviewEngine adds:
    - Question classification via QuestionClassifier
    - Deferred item tracking (dev-only questions)
    - PMSeed extraction from completed interviews
    - Brownfield repo registration (configured runtime database)
    - Scan-once codebase context sharing

    Attributes:
        inner: The wrapped InterviewEngine instance.
        classifier: Question classifier for planning/dev distinction.
        llm_adapter: LLM adapter (shared with inner engine).
        model: Model for PM-specific LLM calls.
        deferred_items: Questions deferred to development phase.
        classifications: History of question classifications.
        codebase_context: Shared codebase exploration context.
        _explored: Whether codebase has been explored (scan-once guard).

    Example:
        adapter = LiteLLMAdapter()
        engine = PMInterviewEngine.create(llm_adapter=adapter)

        state_result = await engine.start_interview("Build a task manager")
        state = state_result.value

        while not state.is_complete:
            q_result = await engine.ask_next_question(state)
            question = q_result.value
            # question is already PM-friendly (classified + reframed)
            response = input(question)
            await engine.record_response(state, response, question)

        pm_seed = await engine.generate_pm_seed(state)
        engine.save_pm_seed(pm_seed)
    """

    supports_atomic_turn = True

    inner: InterviewEngine
    classifier: QuestionClassifier
    llm_adapter: LLMAdapter
    model: str | None = None
    model_is_explicit: bool = field(default=False, init=False)
    deferred_items: list[str] = field(default_factory=list)
    decide_later_items: list[str] = field(default_factory=list)
    """Original question text for questions classified as DECIDE_LATER.

    These are questions that are premature or unknowable at the PM stage.
    The main session presents the question to the user with a "decide later"
    option; when chosen, the caller records the item here so the PMSeed
    and PM document can surface them as explicit "decide later" decisions.
    """
    classifications: list[ClassificationResult] = field(default_factory=list)
    codebase_context: str = ""
    _explored: bool = False
    _reframe_map: dict[str, str] = field(default_factory=dict)
    """Maps reframed question text → original technical question text.

    When a DEVELOPMENT question is reframed for the PM, we track the mapping
    so that record_response can bundle the original technical question with
    the PM's answer before passing it to the inner InterviewEngine.
    """
    _selected_brownfield_repos: list[dict[str, str]] = field(default_factory=list)
    """Brownfield repos actually used in this session.

    Stored during :meth:`start_interview` so that :meth:`generate_pm_seed`
    can reference the same repos without querying the DB (which may have
    changed since the interview started).
    """

    @classmethod
    def create(
        cls,
        llm_adapter: LLMAdapter,
        model: str | None = None,
        state_dir: Path | None = None,
    ) -> PMInterviewEngine:
        """Factory method to create a PMInterviewEngine with proper wiring.

        Creates the inner InterviewEngine and QuestionClassifier with
        shared LLM adapter.

        Args:
            llm_adapter: LLM adapter for all LLM calls.
            model: Model for interview question generation.
            state_dir: Custom state directory for interview persistence.

        Returns:
            Configured PMInterviewEngine instance.
        """
        if state_dir is None:
            state_dir = Path.home() / ".ouroboros" / "data"

        inner = InterviewEngine(
            llm_adapter=llm_adapter,
            state_dir=state_dir,
            model=model,
        )

        classifier = QuestionClassifier(
            llm_adapter=llm_adapter,
            implicit_model=model,
        )

        return cls(
            inner=inner,
            classifier=classifier,
            llm_adapter=llm_adapter,
            model=model,
        )

    def __post_init__(self) -> None:
        """Resolve implicit default model while preserving explicit caller pins."""
        self.model_is_explicit = self.model is not None
        if self.model is None:
            self.model = get_llm_model_for_role("pm_interview")

    # ──────────────────────────────────────────────────────────────
    # Brownfield repo management
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def load_brownfield_repos() -> list[dict[str, str]]:
        """Load registered brownfield repositories from the DB.

        Delegates to :func:`ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts`.

        Returns:
            List of repo dicts with keys: path, name, desc.
        """
        return _load_brownfield_dicts()

    # ──────────────────────────────────────────────────────────────
    # Codebase exploration (scan-once)
    # ──────────────────────────────────────────────────────────────

    async def explore_codebases(
        self,
        repos: list[dict[str, str]] | None = None,
    ) -> str:
        """Return the empty codebase context. The engine no longer reads code.

        Retained as a no-op rather than deleted because callers outside this
        engine still invoke it, and because the empty string it returns is the
        correct value now: RFC #1937 moved repository reading to the advisory
        lanes in the host session, which is where the regular interview has
        always had it -- that engine's prompt forbids exploring files or
        repositories outright, and its brownfield marking says in a comment
        that the main session, not MCP, does the exploring.

        What the summary used to do, and where it went:

        * *Deciding brownfield.* It gated ``is_brownfield`` and
          ``codebase_paths``, so a failed summarization silently produced a
          greenfield Seed. The roster now decides that, which is what
          registration always meant.
        * *Priming the question generator and classifier.* Both received a
          truncated whole-repo blob. The ``code_context`` lane answers the same
          need per question, with evidence the PM can actually see.
        * *Filling Seed extraction.* It never reached there from the regular
          interview either -- that field is empty for every interview-originated
          Seed -- so the branch reading it is one PM no longer takes.
        """
        self._explored = True
        return ""

    # ──────────────────────────────────────────────────────────────
    # Opening question — asked before the interview loop
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_opening_question() -> str:
        """Return the initial "what do you want to build?" question.

        This question is asked *before* the interview loop begins. The PM's
        answer becomes the ``initial_context`` for :meth:`start_interview`.

        Returns:
            The opening question string.
        """
        return _OPENING_QUESTION

    async def ask_opening_and_start(
        self,
        user_response: str,
        interview_id: str | None = None,
        brownfield_repos: list[dict[str, str]] | None = None,
    ) -> Result[InterviewState, ValidationError]:
        """Process the PM's answer to the opening question and start the interview.

        This is a convenience method that takes the PM's answer to the opening
        question (``get_opening_question()``) and feeds it as
        ``initial_context`` into :meth:`start_interview`.

        Args:
            user_response: The PM's answer to "What do you want to build?".
            interview_id: Optional interview ID.
            brownfield_repos: Optional brownfield repos to explore.

        Returns:
            Result containing the new InterviewState or ValidationError.
        """
        if not user_response or not user_response.strip():
            return Result.err(
                ValidationError(
                    "Please describe what you want to build.",
                    field="initial_context",
                )
            )

        log.info(
            "pm.opening_response_received",
            response_length=len(user_response),
        )

        return await self.start_interview(
            initial_context=user_response.strip(),
            interview_id=interview_id,
            brownfield_repos=brownfield_repos,
        )

    # ──────────────────────────────────────────────────────────────
    # Interview lifecycle — delegates to inner engine
    # ──────────────────────────────────────────────────────────────

    async def start_interview(
        self,
        initial_context: str,
        interview_id: str | None = None,
        brownfield_repos: list[dict[str, str]] | None = None,
    ) -> Result[InterviewState, ValidationError]:
        """Start a new PM interview session.

        Optionally explores brownfield codebases before starting.
        Delegates interview creation to the inner InterviewEngine.

        Args:
            initial_context: Initial product idea or context.
            interview_id: Optional interview ID.
            brownfield_repos: Optional brownfield repos to explore.

        Returns:
            Result containing the new InterviewState or ValidationError.
        """
        # Always reset all PM state for a fresh interview
        self._selected_brownfield_repos = []
        self.codebase_context = ""
        self._explored = False
        self.classifier.codebase_context = ""
        self.deferred_items = []
        self.decide_later_items = []
        self.classifications = []
        self._reframe_map = {}

        # Redirect the repositories at persistent snapshot worktrees pinned to
        # the remote default branch (created once, then fetch + hard-reset) so a
        # stale local checkout never leaks into PRD context. The engine does not
        # read them -- the advisory lanes do, in the host session (RFC #1937) --
        # but where they read is still decided here, because the snapshot
        # machinery is engine-side and the roster handed to the lanes is built
        # from these records.
        if brownfield_repos:
            brownfield_repos = await asyncio.to_thread(
                refresh_pm_snapshot_worktrees, list(brownfield_repos)
            )
            self._selected_brownfield_repos = list(brownfield_repos)

        # Store the raw user context for extraction; PM steering goes
        # only into the interview system prompt, not into persisted state.
        self._initial_context = initial_context
        user_context = initial_context

        # Keep PM steering prefix in memory for interview rounds but
        # do NOT persist it as initial_context so extraction sees only
        # user-provided content.
        self._pm_steering = _PM_SYSTEM_PROMPT_PREFIX

        # Install PM-scoped system prompt wrapper
        self._install_pm_steering()

        result = await self.inner.start_interview(
            initial_context=user_context,
            interview_id=interview_id,
        )

        if result.is_ok:
            # Mark brownfield state on the returned InterviewState.
            #
            # The roster is the whole condition. It used to be the roster *and*
            # a non-empty engine summary, which made a fact the registration
            # already states depend on a summarization step succeeding -- and
            # that step failed silently, so one failed call turned a brownfield
            # project into a greenfield Seed with nothing to notice.
            state = result.value
            if brownfield_repos:
                state.is_brownfield = True
                # Persist the durable source checkout, not an ephemeral
                # snapshot worktree path, into interview state.
                state.codebase_paths = [
                    {"path": r.get("source_path") or r["path"], "role": "primary"}
                    for r in brownfield_repos
                    if "path" in r
                ]
            log.info(
                "pm.interview_started",
                interview_id=state.interview_id,
                is_brownfield=state.is_brownfield,
            )

        return result

    def _install_pm_steering(self) -> None:
        """Install PM steering into the inner engine's system prompt builder.

        Idempotent — if already installed, replaces previous wrapper to prevent
        stacking across multiple start/resume calls on the same engine instance.

        Budget-extension contract: the PM-owned inner instance gets its
        system-prompt budgets widened by exactly the steering length (instance
        attributes only — ``interview.py`` and dev-interview instances are
        untouched). ``ask_next_question`` therefore computes budgets and trims
        history against the widened numbers, the wire ceiling
        (``_MAX_TOTAL_PROMPT_CHARS``) stays enforced by the inner engine
        itself, and on the normal path the steering rides entirely inside the
        reserved extension: the inner build keeps the engine's *designed*
        budget, byte-identical to what a dev interview would produce.

        The interview layer always has priority. When a caller supplies a cap
        smaller than designed-budget-plus-extension (tests, wire pressure in
        the history-decline zone), steering falls back to fit-and-shed:
        paragraphs are included atomically (contract paragraph first, shed
        last) and dropped one by one until every ``INNER_GUIDANCE_INVARIANTS``
        marker retained by the unwrapped baseline build also survives the
        steered build — or no steering remains.
        """
        self._pm_steering = getattr(self, "_pm_steering", _PM_SYSTEM_PROMPT_PREFIX)

        # Reserve the steering extension on the PM-owned inner instance
        # (idempotent — derived from class attributes, never stacks).
        reserve_steering_extension(self.inner, self._pm_steering)

        # Store the original (unwrapped) build method on first install
        if not hasattr(self, "_original_build_system_prompt"):
            self._original_build_system_prompt = self.inner._build_system_prompt

        original_build = self._original_build_system_prompt

        def _pm_build_system_prompt(
            state: InterviewState,
            initial_context: str | None = None,
            max_chars: int | None = None,
        ) -> str:
            return compose_steered_prompt(
                inner=self.inner,
                build=original_build,
                steering=self._pm_steering,
                state=state,
                initial_context=initial_context,
                max_chars=max_chars,
                shed_last_marker=_PM_CONTRACT_MARKER,
            )

        self.inner._build_system_prompt = _pm_build_system_prompt  # type: ignore[assignment]

    def _apply_classification(self, classification: ClassificationResult) -> str:
        """Persist one PM routing decision and return the user-facing question."""
        self.classifications.append(classification)
        output_type = classification.output_type
        if output_type == ClassifierOutputType.DEFERRED:
            log.info(
                "pm.question_deferred_candidate",
                question=classification.original_question[:100],
                reasoning=classification.reasoning,
                output_type=output_type,
            )
            return classification.original_question
        if output_type == ClassifierOutputType.DECIDE_LATER:
            log.info(
                "pm.question_decide_later",
                question=classification.original_question[:100],
                reasoning=classification.reasoning,
            )
            return classification.original_question
        if output_type == ClassifierOutputType.REFRAMED:
            reframed = classification.question_for_pm
            self._reframe_map[reframed] = classification.original_question
            log.info(
                "pm.question_reframed",
                original=classification.original_question[:100],
                reframed=reframed[:100],
                output_type=output_type,
            )
            return reframed
        log.debug(
            "pm.question_passthrough",
            question=classification.original_question[:100],
            output_type=output_type,
        )
        return classification.question_for_pm

    async def plan_next_turn(
        self,
        state: InterviewState,
    ) -> Result[PMInterviewTurnPlan, ProviderError | ValidationError]:
        """Plan PM scoring, question generation, and classification in one call."""
        additional_context = ""
        if self.decide_later_items:
            additional_context = "\n".join(f"- {item}" for item in self.decide_later_items)
        scorer = AmbiguityScorer(llm_adapter=self.llm_adapter, model=self.model)
        planner = InterviewTurnPlanner(engine=self.inner, scorer=scorer)
        response_contract = f"""
Also include these PM routing fields in the same JSON object:
"category": "planning"|"development"|"decide_later",
"reframed_question": "string", "reasoning": "string",
"defer_to_dev": false|true, "decide_later": false|true,
"placeholder_response": "string".

Optionally add "companion_questions": up to {MAX_QUESTIONS_PER_TURN - 1} extra
questions asked in the same turn, each an object with the same routing fields
plus "question". Include one only when it targets a different unresolved
clarity dimension and no answer to another question in this turn could change
how it should be asked. In closure mode, or when unsure, include none. Never
rephrase the primary question as a companion.

Apply this canonical PM routing policy:
{classification_policy_prompt()}
"""
        turn_result = await planner.plan(
            state,
            scoring_state=_decision_only_view(state),
            additional_scoring_context=additional_context,
            extra_response_contract=response_contract,
            additional_untrusted_context=self.classifier.codebase_context[:2000],
        )
        if turn_result.is_err:
            return Result.err(turn_result.error)
        turn = turn_result.value
        try:
            classification = self.classifier._parse_response(
                json.dumps(turn.raw_payload),
                turn.question,
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("pm.atomic_classification_failed", error=str(exc))
            classification = ClassificationResult(
                original_question=turn.question,
                category=QuestionCategory.PLANNING,
                reframed_question=turn.question,
                reasoning="Atomic PM classification unavailable; defaulting to planning.",
            )
        question = self._apply_classification(classification)
        return Result.ok(
            PMInterviewTurnPlan(
                question=question,
                ambiguity=turn.ambiguity,
                classification=classification,
                raw_payload=turn.raw_payload,
            )
        )

    async def plan_next_turns(
        self,
        state: InterviewState,
    ) -> Result[list[PMInterviewTurnPlan], ProviderError | ValidationError]:
        """Plan one PM turn of one to three questions (RFC #2222 decision 1).

        One planner call: the primary question travels exactly as
        :meth:`plan_next_turn` produces it, and companions ride the same JSON
        payload under ``companion_questions``. Each companion is classified and
        applied through the same ``_apply_classification`` path as the primary,
        so reframe tracking and skip routing do not know batch members apart.

        What is enforced here rather than trusted to the generator:

        * **Closure mode is single-question.** At or below the planner's
          closure threshold the batch is the primary alone — a companion is a
          second topic, and closure mode forbids opening one.
        * **No duplicate question identities in one batch.** Fan-out isolation
          keys on the normalized question text, so a companion that normalizes
          to an already-included question is dropped, not dispatched.
        * **The ceiling.** At most ``MAX_QUESTIONS_PER_TURN`` questions leave,
          however many the payload carried.

        A malformed companion entry is dropped silently: the primary is the
        turn, and companions are an optimization the turn must not fail on.
        What counts as malformed is not decided here — the companion's routing
        fields go through the primary's own classification parser, so a
        wrong-typed ``decide_later`` is refused by the same rule in both. What
        follows the refusal differs, because the two questions do: the primary
        falls back to a plain planning question, since the turn must have one,
        while a companion is dropped, since the turn is whole without it.
        """
        self._begin_turn()
        turn_result = await self.plan_next_turn(state)
        if turn_result.is_err:
            return Result.err(turn_result.error)
        primary = turn_result.value
        plans = [primary]

        ambiguity = primary.ambiguity
        if ambiguity is not None and ambiguity.overall_score <= _CLOSURE_MODE_AMBIGUITY:
            return Result.ok(plans)

        seen_identities = {
            normalize_question_text(primary.question),
            normalize_question_text(primary.classification.original_question),
        }
        raw_companions = primary.raw_payload.get("companion_questions")
        if not isinstance(raw_companions, list):
            return Result.ok(plans)

        for raw in raw_companions:
            if len(plans) >= MAX_QUESTIONS_PER_TURN:
                break
            if not isinstance(raw, dict):
                continue
            question_text = str(raw.get("question") or "").strip()
            if not question_text:
                continue
            if normalize_question_text(question_text) in seen_identities:
                continue
            try:
                classification = self.classifier._parse_response(json.dumps(raw), question_text)
            except (KeyError, TypeError, ValueError) as exc:
                # Same parser, same strictness as the primary: a routing field
                # of the wrong type is malformed, not a value to coerce.
                # ``bool("false")`` is True, and a companion routed that way
                # would offer a skip that discards a question the PM must
                # answer. Dropping loses one companion; coercing loses an answer.
                log.warning("pm.companion_classification_rejected", error=str(exc))
                continue
            reframes_before = dict(self._reframe_map)
            shown_question = self._apply_classification(classification)
            shown_identity = normalize_question_text(shown_question)
            if shown_identity in seen_identities:
                # _apply_classification already recorded routing state for a
                # question this batch will not carry — undo both traces. The
                # map is restored, not popped: a companion whose own text
                # differs but which reframes onto the primary's shown question
                # overwrites the primary's entry, and popping that key would
                # take the primary's original question with it — leaving the
                # PM's answer to a reframed question with nothing to bundle.
                self.classifications.pop()
                self._reframe_map = reframes_before
                continue
            seen_identities.add(normalize_question_text(question_text))
            seen_identities.add(shown_identity)
            plans.append(
                PMInterviewTurnPlan(
                    question=shown_question,
                    ambiguity=None,
                    classification=classification,
                    raw_payload=dict(raw),
                )
            )

        log.info(
            "pm.turn_batch_planned",
            interview_id=state.interview_id,
            batch_size=len(plans),
        )
        return Result.ok(plans)

    def _begin_turn(self) -> None:
        """Drop the previous turn's reframe routing before a new one is planned.

        A reframe maps a *shown* question back to the technical one it came
        from, and that mapping means something only while the turn that
        produced it is the turn on the wire. A host abandons a turn simply by
        not answering it (RFC #2222 revision 4) and the next call plans a fresh
        one — so a mapping that outlived its turn would attach itself to
        whatever later question happens to be displayed with the same text, and
        that decision would be recorded under a technical question nobody was
        asked.

        This is the same removal the pending list got, at the one address it
        had left: what is persisted describes the turn being planned now, and
        planning replaces it rather than adding to it.
        """
        self._reframe_map = {}

    async def ask_next_question(
        self,
        state: InterviewState,
    ) -> Result[str, ProviderError | ValidationError]:
        """Generate and classify the next question using the legacy two-call path."""
        self._begin_turn()
        question_result = await self.inner.ask_next_question(state)
        if question_result.is_err:
            return question_result
        question = question_result.value
        if question == INITIAL_CONTEXT_SUMMARY_QUESTION:
            return Result.ok(question)
        classify_result = await self.classifier.classify(
            question=question,
            interview_context=self._build_interview_context(state),
        )
        if classify_result.is_err:
            log.warning("pm.classification_failed", question=question[:100])
            return question_result
        return Result.ok(self._apply_classification(classify_result.value))

    async def record_response(
        self,
        state: InterviewState,
        user_response: str,
        question: str,
    ) -> Result[InterviewState, ValidationError]:
        """Record the PM's response to the current question.

        If the question was reframed from a technical question, bundles the
        original technical question with the PM's answer so the inner
        InterviewEngine retains full context for follow-up generation.

        The bundled question recorded in the inner engine is::

            [Original technical question: <original>]
            [PM was asked (reframed): <reframed>]

        The answer itself is preserved byte-for-byte.  In particular, a
        leading provenance marker such as ``[from-code]`` must remain leading
        so ``InterviewState.record_answer`` and persisted-state validation do
        not accidentally promote an observation into a user decision.

        This ensures the LLM generating follow-up questions sees both
        the underlying technical concern and the PM's product-level answer.

        Args:
            state: Current interview state.
            user_response: The PM's response.
            question: The question that was asked (possibly reframed).

        Returns:
            Result containing updated state or ValidationError.
        """
        original_question = self._reframe_map.pop(question, None)

        if original_question is not None:
            # Bundle the original and reframed questions, but do not decorate
            # the response: provenance is encoded by a leading caller marker.
            bundled_question = (
                f"[Original technical question: {original_question}]\n"
                f"[PM was asked (reframed): {question}]"
            )

            log.info(
                "pm.response_bundled",
                original_question=original_question[:100],
                reframed_question=question[:100],
            )

            return await self.inner.record_response(state, user_response, bundled_question)

        return await self.inner.record_response(state, user_response, question)

    async def skip_as_decide_later(
        self,
        state: InterviewState,
        question: str,
    ) -> Result[InterviewState, ValidationError]:
        """Skip a question as "decide later" at the user's explicit request.

        Records the question in ``decide_later_items`` and feeds a placeholder
        response to the inner InterviewEngine so the round is properly recorded
        and the engine advances.

        This is called when the main session detects that the user chose the
        "decide later" option for a DECIDE_LATER-classified question, instead
        of the old auto-skip behavior inside ``ask_next_question``.

        Args:
            state: Current interview state.
            question: The question the user chose to decide later.

        Returns:
            Result containing updated state or ValidationError.
        """
        if question not in self.decide_later_items:
            self.decide_later_items.append(question)

        log.info(
            "pm.question_decide_later_by_user",
            question=question[:100],
        )

        return await self.record_response(
            state,
            user_response=DECIDE_LATER_PLACEHOLDER,
            question=question,
        )

    async def skip_as_deferred(
        self,
        state: InterviewState,
        question: str,
    ) -> Result[InterviewState, ValidationError]:
        """Skip a question as "deferred to dev" at the user's explicit request.

        Records the question in ``deferred_items`` and feeds a deferral
        response to the inner InterviewEngine so the round is properly recorded
        and the engine advances.

        Args:
            state: Current interview state.
            question: The question the user chose to defer.

        Returns:
            Result containing updated state or ValidationError.
        """
        if question not in self.deferred_items:
            self.deferred_items.append(question)

        log.info(
            "pm.question_deferred_by_user",
            question=question[:100],
        )

        return await self.record_response(
            state,
            user_response=DEFERRED_PLACEHOLDER,
            question=question,
        )

    async def complete_interview(
        self,
        state: InterviewState,
    ) -> Result[InterviewState, ValidationError]:
        """Mark the PM interview as completed.

        Delegates to the inner InterviewEngine.

        Args:
            state: Current interview state.

        Returns:
            Result containing updated state or ValidationError.
        """
        return await self.inner.complete_interview(state)

    def get_decide_later_summary(self) -> list[str]:
        """Return the combined list of deferred + decide-later items.

        Merges runtime ``deferred_items`` (technical questions deferred to
        dev phase) with ``decide_later_items`` (premature/unknowable questions)
        into one canonical list for display and artifact generation.

        Returns:
            List of original question text strings. Empty if none were deferred.
        """
        combined = list(self.decide_later_items)
        for item in self.deferred_items:
            if item not in combined:
                combined.append(item)
        return combined

    def format_decide_later_summary(self) -> str:
        """Format decide-later items as a human-readable summary string.

        Returns a numbered list of decide-later items suitable for display
        at the end of the interview. Returns an empty string if there are
        no decide-later items.

        Returns:
            Formatted summary string, or empty string if no items.
        """
        items = self.get_decide_later_summary()
        if not items:
            return ""

        lines = ["Items to decide later:"]
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {item}")

        return "\n".join(lines)

    async def save_state(
        self,
        state: InterviewState,
    ) -> Result[Path, ValidationError]:
        """Persist interview state to disk.

        Delegates to the inner InterviewEngine.

        Args:
            state: The interview state to save.

        Returns:
            Result containing path to saved file or ValidationError.
        """
        return await self.inner.save_state(state)

    async def load_state(
        self,
        interview_id: str,
    ) -> Result[InterviewState, ValidationError]:
        """Load interview state from disk.

        Delegates to the inner InterviewEngine.

        Args:
            interview_id: The interview ID to load.

        Returns:
            Result containing loaded state or ValidationError.
        """
        return await self.inner.load_state(interview_id)

    def restore_meta(self, meta: dict[str, Any]) -> None:
        """Restore PM-specific metadata into this engine from a loaded dict.

        Sets ``decide_later_items``, ``codebase_context``,
        ``pending_reframe`` (via ``_reframe_map``), and syncs the classifier's
        ``codebase_context`` so that subsequent classification calls use the
        brownfield context.

        This is the inverse of the meta dict produced by
        :func:`pm_handler._save_pm_meta`.

        Args:
            meta: Dictionary previously persisted as ``pm_meta_{session_id}.json``.
                  Expected keys: ``decide_later_items``,
                  ``codebase_context``, ``pending_reframe``.
                  Legacy key ``deferred_items`` is merged into
                  ``decide_later_items`` for backward compatibility.
        """
        # Full state reset — clear all session-scoped fields before restoring.
        # Legacy metadata may still have separate deferred_items; merge them
        # into decide_later_items (canonical field since v0.25).
        self.decide_later_items = list(meta.get("decide_later_items", []))
        for item in meta.get("deferred_items", []):
            if item not in self.decide_later_items:
                self.decide_later_items.append(item)
        self.deferred_items = []
        self.codebase_context = meta.get("codebase_context", "") or ""
        self.classifications = []  # Reset before restoring
        self._reframe_map = {}  # Reset before restoring
        # Sync classifier so brownfield context is available for classification
        self.classifier.codebase_context = self.codebase_context
        # Restore brownfield repo selection
        self._selected_brownfield_repos = list(meta.get("brownfield_repos", []))
        # Restore classification history
        saved_classifications = meta.get("classifications", [])
        if saved_classifications:
            # Map ClassifierOutputType values back to a minimal ClassificationResult
            _OUTPUT_TO_CATEGORY = {
                ClassifierOutputType.PASSTHROUGH: QuestionCategory.PLANNING,
                ClassifierOutputType.REFRAMED: QuestionCategory.DEVELOPMENT,
                ClassifierOutputType.DEFERRED: QuestionCategory.DEVELOPMENT,
                ClassifierOutputType.DECIDE_LATER: QuestionCategory.DECIDE_LATER,
            }

            for c_val in saved_classifications:
                try:
                    output_type = ClassifierOutputType(c_val)
                    category = _OUTPUT_TO_CATEGORY.get(output_type, QuestionCategory.PLANNING)
                    self.classifications.append(
                        ClassificationResult(
                            original_question="",
                            category=category,
                            reframed_question="",
                            reasoning="restored",
                            defer_to_dev=(output_type == ClassifierOutputType.DEFERRED),
                            decide_later=(output_type == ClassifierOutputType.DECIDE_LATER),
                        )
                    )
                except ValueError:
                    pass
        # Restore the reframe map from pending_reframe if present
        pending = meta.get("pending_reframe")
        if pending and isinstance(pending, dict):
            self._reframe_map[pending["reframed"]] = pending["original"]
        # A batched turn can hold several pending reframes at once (RFC #2222);
        # the full map is persisted beside the legacy single entry.
        pending_map = meta.get("pending_reframes")
        if isinstance(pending_map, dict):
            for reframed, original in pending_map.items():
                if isinstance(reframed, str) and isinstance(original, str):
                    self._reframe_map[reframed] = original

        # Reinstall PM steering wrapper for resumed sessions
        self._install_pm_steering()

    # ──────────────────────────────────────────────────────────────
    # Public accessors for handler delegation
    # ──────────────────────────────────────────────────────────────

    def compute_deferred_diff(
        self,
        deferred_len_before: int,
        decide_later_len_before: int,
    ) -> dict[str, Any]:
        """Compute the diff of deferred/decide-later items after ask_next_question.

        Compares list lengths before and after the call to determine which
        new items were added during classification.  Returns a dict with:
            new_deferred: list of newly deferred question texts
            new_decide_later: list of newly decide-later question texts
            deferred_count: always 0 (deprecated; merged into decide_later_count)
            decide_later_count: combined total of deferred + decide-later items

        Args:
            deferred_len_before: Length of deferred_items before the call.
            decide_later_len_before: Length of decide_later_items before the call.

        Returns:
            Dict with new_deferred, new_decide_later, deferred_count, decide_later_count.
        """
        new_deferred = self.deferred_items[deferred_len_before:]
        new_decide_later = self.decide_later_items[decide_later_len_before:]

        return {
            "new_deferred": list(new_deferred),
            "new_decide_later": list(new_decide_later),
            "deferred_count": 0,
            "decide_later_count": len(self.deferred_items) + len(self.decide_later_items),
        }

    def get_pending_reframe(self) -> dict[str, str] | None:
        """Return the most recent pending reframe as {reframed, original}, or None."""
        if not self._reframe_map:
            return None
        reframed = next(reversed(self._reframe_map))
        return {
            "reframed": reframed,
            "original": self._reframe_map[reframed],
        }

    def get_last_classification(self) -> str | None:
        """Return the output_type string of the last classification, or None.

        Returns:
            The output_type value string (e.g. 'passthrough', 'reframed',
            'deferred', 'decide_later'), or None if no classifications exist.
        """
        if self.classifications:
            return self.classifications[-1].output_type.value
        return None

    async def check_completion(
        self,
        state: InterviewState,
    ) -> dict[str, Any] | None:
        """Check whether the interview should complete based on ambiguity.

        After at least ``MIN_ROUNDS_BEFORE_EARLY_EXIT`` authoritative PM
        decisions, the scorer evaluates requirement clarity. A score at or below
        the threshold makes the interview ready for PM generation.
        """
        answered_rounds = decision_round_count(state)

        # ── Ambiguity check (only after minimum rounds) ────────────────
        if answered_rounds < MIN_ROUNDS_BEFORE_EARLY_EXIT:
            return None

        try:
            # Build additional context for scorer: decide-later items are
            # intentional deferrals that should not penalise clarity.
            additional_context = ""
            if self.decide_later_items:
                additional_context = "Decide-later items (intentional deferrals):\n"
                additional_context += "\n".join(f"- {item}" for item in self.decide_later_items)

            scorer = AmbiguityScorer(
                llm_adapter=self.llm_adapter,
                model=self.model,
            )
            score_result = await scorer.score(
                _decision_only_view(state),
                is_brownfield=state.is_brownfield,
                additional_context=additional_context,
            )

            if score_result.is_err:
                log.warning(
                    "pm.completion.scoring_failed",
                    session_id=state.interview_id,
                    error=str(score_result.error),
                )
                # Scoring failed — continue the interview rather than blocking
                return None

            ambiguity = score_result.value

            # Persist score on state for downstream use
            state.store_ambiguity(
                score=ambiguity.overall_score,
                breakdown=ambiguity.breakdown.model_dump(mode="json"),
            )

            if qualifies_for_seed_completion(ambiguity, is_brownfield=state.is_brownfield):
                log.info(
                    "pm.completion.ambiguity_resolved",
                    session_id=state.interview_id,
                    ambiguity_score=ambiguity.overall_score,
                    rounds=answered_rounds,
                )
                return {
                    "interview_complete": True,
                    "completion_reason": "ambiguity_resolved",
                    "rounds_completed": answered_rounds,
                    "ambiguity_score": ambiguity.overall_score,
                }

            log.debug(
                "pm.completion.continuing",
                session_id=state.interview_id,
                ambiguity_score=ambiguity.overall_score,
                rounds=answered_rounds,
            )

        except Exception as e:
            log.warning(
                "pm.completion.check_error",
                session_id=state.interview_id,
                error=str(e),
            )

        return None

    # ──────────────────────────────────────────────────────────────
    # PMSeed extraction
    # ──────────────────────────────────────────────────────────────

    async def generate_pm_seed(
        self,
        state: InterviewState,
    ) -> Result[PMSeed, ProviderError | ValidationError]:
        """Extract PMSeed from completed interview.

        Uses LLM to extract structured product requirements from the
        interview transcript, including any deferred items.

        Args:
            state: Completed interview state.

        Returns:
            Result containing PMSeed or error.
        """
        substantive_rounds = [
            round_data
            for round_data in state.rounds
            if round_data.question != INITIAL_CONTEXT_SUMMARY_QUESTION and round_data.user_response
        ]
        if not substantive_rounds:
            return Result.err(
                ValidationError(
                    "Cannot generate PM seed from empty interview",
                    field="rounds",
                )
            )

        if not state.is_complete:
            return Result.err(
                ValidationError(
                    "Cannot generate PM seed from incomplete interview — complete the interview first",
                    field="is_complete",
                )
            )

        if initial_context_summary_missing(state):
            return Result.err(
                ValidationError(
                    "Initial context summary required before PM seed generation",
                    field="initial_context",
                    details={"interview_id": state.interview_id},
                )
            )

        context = self._build_interview_context(state, withhold_observations=True)

        messages = [
            Message(role=MessageRole.SYSTEM, content=_EXTRACTION_SYSTEM_PROMPT),
            Message(
                role=MessageRole.USER,
                content=self._build_extraction_prompt(context),
            ),
        ]

        assert self.model is not None
        config = CompletionConfig(
            model=self.model,
            role="pm_interview",
            model_is_explicit=self.model_is_explicit,
            temperature=0.2,
            max_tokens=4096,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            return Result.err(result.error)

        try:
            seed = self._parse_pm_seed(
                result.value.content,
                interview_id=state.interview_id,
            )
            log.info(
                "pm.seed_generated",
                pm_id=seed.pm_id,
                product_name=seed.product_name,
                story_count=len(seed.user_stories),
                decide_later_count=len(seed.decide_later_items),
            )
            return Result.ok(seed)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return Result.err(
                ProviderError(
                    f"Failed to parse PM seed: {e}",
                    details={"response_preview": result.value.content[:200]},
                )
            )

    def save_pm_seed(
        self,
        seed: PMSeed,
        output_dir: Path | None = None,
    ) -> Path:
        """Save PMSeed to JSON file.

        Saves to ~/.ouroboros/seeds/pm_seed_{id}.json.

        Args:
            seed: The PMSeed to save.
            output_dir: Custom output directory (defaults to ~/.ouroboros/seeds/).

        Returns:
            Path to the saved JSON file.
        """
        if output_dir is None:
            output_dir = _SEED_DIR

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{seed.pm_id}.json"
        filepath = output_dir / filename

        json_content = json.dumps(
            seed.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        durability_confirmed = write_owner_only(filepath, json_content)

        if not durability_confirmed:
            log.warning(
                "pm.seed_save_durability_uncertain",
                path=str(filepath),
                pm_id=seed.pm_id,
            )

        log.info(
            "pm.seed_saved",
            path=str(filepath),
            pm_id=seed.pm_id,
        )

        return filepath

    # ──────────────────────────────────────────────────────────────
    # Dev interview handoff
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def pm_seed_to_dev_context(seed: PMSeed) -> str:
        """Serialize PMSeed to initial_context string for dev interview.

        This is the CLI-level handoff: the PMSeed YAML is passed as the
        initial_context string to a standard InterviewEngine session.

        Args:
            seed: The PMSeed to serialize.

        Returns:
            YAML string suitable for initial_context.
        """
        return seed.to_initial_context()

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _build_interview_context(
        self, state: InterviewState, *, withhold_observations: bool = False
    ) -> str:
        """Build interview context string from state.

        Args:
            state: Current interview state.
            withhold_observations: Render observation answers as a fixed note
                instead of their content. True only for the requirement
                extraction prompt — the question classifier reads the same
                context and has to see observations in full, since informing
                the next question is what they were collected for (#1755).

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {prompt_safe_initial_context(state)}"]

        # Question lines are unchanged either way: an observation reaching a
        # later question is where it was collected to arrive.
        if withhold_observations:
            rendered = [(item.question, item.answer) for item in extraction_rounds(state)]
        else:
            rendered = [(r.question, r.user_response) for r in state.rounds]

        for question, answer in rendered:
            if question == INITIAL_CONTEXT_SUMMARY_QUESTION:
                continue
            parts.append(f"\nQ: {question}")
            if answer:
                parts.append(f"A: {answer}")

        return "\n".join(parts)

    def _build_extraction_prompt(self, context: str) -> str:
        """Build extraction prompt with interview context and deferred items.

        Args:
            context: Formatted interview context.

        Returns:
            User prompt for PM seed extraction.
        """
        prompt = f"""Extract structured product requirements from this PM interview:

---
{context}
---
"""

        # Combine deferred and decide-later items under one canonical key
        # so the LLM output schema matches PMSeed (which only has
        # decide_later_items).
        all_decide_later: list[str] = list(self.deferred_items)
        for item in self.decide_later_items:
            if item not in all_decide_later:
                all_decide_later.append(item)

        if all_decide_later:
            items_text = "\n".join(f"- {item}" for item in all_decide_later)
            prompt += f"""

The following questions were deferred or identified as premature during the interview.
Include them as original question text in "decide_later_items":
{items_text}
"""

        # Note: brownfield codebase context is already included in
        # initial_context (via _build_interview_context), so we don't
        # duplicate it here.

        return prompt

    def _parse_pm_seed(
        self,
        response: str,
        interview_id: str,
    ) -> PMSeed:
        """Parse LLM response into PMSeed.

        Args:
            response: Raw LLM response text.
            interview_id: Source interview ID.

        Returns:
            Parsed PMSeed.

        Raises:
            ValueError: If response cannot be parsed.
        """

        # One authoritative payload or nothing: an echoed schema example
        # must never become the saved PMSeed (#1838).
        text = extract_json_payload(response.strip())
        if text is None:
            raise ValueError("no unambiguous JSON payload in PM seed response")

        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("PM seed payload must be a JSON object")

        # Parse user stories
        stories = tuple(
            UserStory(
                persona=s.get("persona", "User"),
                action=s.get("action", ""),
                benefit=s.get("benefit", ""),
            )
            for s in data.get("user_stories", [])
        )

        # Merge LLM-extracted items with engine-tracked items, deduplicating.
        # The extraction prompt includes raw items as context so the LLM may
        # already emit them, but engine-tracked items are authoritative and
        # must survive even if the extractor omits them.
        # Both deferred_items (from LLM) and engine.deferred_items are merged
        # into decide_later_items on the PMSeed.
        all_decide_later = list(data.get("decide_later_items", []))
        for item in data.get("deferred_items", []):
            if item not in all_decide_later:
                all_decide_later.append(item)
        for item in self.deferred_items:
            if item not in all_decide_later:
                all_decide_later.append(item)
        for item in self.decide_later_items:
            if item not in all_decide_later:
                all_decide_later.append(item)

        # Include brownfield repos — use session-stored repos, not DB.
        # Snapshot worktree paths are working locations only; the seed must
        # record the durable source checkout (``source_path``) instead.
        def _durable_repo(repo: dict[str, str]) -> dict[str, str]:
            entry = dict(repo)
            source = entry.pop("source_path", None)
            if source:
                entry["path"] = source
            return entry

        brownfield_repos = tuple(_durable_repo(r) for r in self._selected_brownfield_repos)

        return PMSeed(
            pm_id=f"pm_seed_{interview_id}",
            product_name=data.get("product_name", ""),
            goal=data.get("goal", ""),
            user_stories=stories,
            constraints=tuple(data.get("constraints", [])),
            success_criteria=tuple(data.get("success_criteria", [])),
            decide_later_items=tuple(all_decide_later),
            assumptions=tuple(data.get("assumptions", [])),
            interview_id=interview_id,
            codebase_context=self.codebase_context,
            brownfield_repos=brownfield_repos,
        )
