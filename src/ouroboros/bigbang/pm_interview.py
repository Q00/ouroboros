"""PM Interview Engine — composition wrapper around InterviewEngine.

Adds PM-specific behavior on top of the existing InterviewEngine:
- Question classification (planning vs development)
- Reframing technical questions for PM audience
- Deferred item tracking for dev-only questions
- PMSeed generation from completed interview
- PM document generation (pm.md)
- Brownfield repo management via ~/.ouroboros/ouroboros.db
- CodebaseExplorer scan-once semantics (shared context)

Composition pattern: PMInterviewEngine *wraps* InterviewEngine without
modifying its internals. The inner engine handles question generation,
state persistence, and round management. The outer engine intercepts
questions for classification and collects PM-specific metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import structlog
import yaml

from ouroboros.bigbang.brownfield import (
    load_brownfield_repos_as_dicts as _load_brownfield_dicts,
)
from ouroboros.bigbang.explore import CodebaseExplorer, format_explore_results
from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.bigbang.pm_document import save_pm_document
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    ClassifierOutputType,
    QuestionClassifier,
)
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

log = structlog.get_logger()

_SEED_DIR = Path.home() / ".ouroboros" / "seeds"
_PM_SYSTEM_PROMPT_PREFIX = """\
You are a Product Requirements interviewer helping a PM define their product.

Focus on PRODUCT-LEVEL questions:
- What problem does this solve and for whom?
- What are the business goals and success metrics?
- What are the user stories and workflows?
- What constraints exist (timeline, budget, compliance)?
- What is in scope vs out of scope?
- What are the acceptance criteria?

Do NOT ask about:
- Implementation details (databases, frameworks, APIs)
- Architecture decisions (microservices, deployment)
- Code-level patterns or testing strategies

"""

_OPENING_QUESTION = (
    "What do you want to build? Tell me about the product or feature "
    "you have in mind — the problem it solves, who it's for, and any "
    "initial ideas you already have."
)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a requirements extraction engine. Given a PM interview transcript,
extract structured product requirements.

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

# Model for extraction (uses same as interview for consistency)
_FALLBACK_MODEL = "claude-opus-4-6"


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
    - PM document generation (pm.md)
    - Brownfield repo registration (~/.ouroboros/ouroboros.db)
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
        engine.save_pm_document(pm_seed)
    """

    inner: InterviewEngine
    classifier: QuestionClassifier
    llm_adapter: LLMAdapter
    model: str = _FALLBACK_MODEL
    deferred_items: list[str] = field(default_factory=list)
    decide_later_items: list[str] = field(default_factory=list)
    """Original question text for questions classified as DECIDE_LATER.

    These are questions that are premature or unknowable at the PM stage.
    They are auto-answered with a placeholder and stored here so the PMSeed
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
        model: str = _FALLBACK_MODEL,
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
        )

        return cls(
            inner=inner,
            classifier=classifier,
            llm_adapter=llm_adapter,
            model=model,
        )

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
        """Explore brownfield codebases exactly once.

        Scans selected repositories and stores the context for sharing
        between the interviewer and classifier. Subsequent calls return
        the cached result.

        Args:
            repos: Repos to explore. Defaults to registered brownfield repos.

        Returns:
            Formatted codebase context string.
        """
        if self._explored:
            return self.codebase_context

        if repos is None:
            repos = self.load_brownfield_repos()

        if not repos:
            self._explored = True
            return ""

        paths = [{"path": r["path"], "role": r.get("role", "primary")} for r in repos]

        try:
            explorer = CodebaseExplorer(
                llm_adapter=self.llm_adapter,
                model=self.model,
            )
            results = await explorer.explore(paths)
            self.codebase_context = format_explore_results(results)

            # Share context with classifier
            self.classifier.codebase_context = self.codebase_context

            log.info(
                "pm.explore_completed",
                repos_explored=len(results),
                context_length=len(self.codebase_context),
            )
        except (ProviderError, OSError) as e:
            log.warning("pm.explore_failed", error=str(e), exc_info=e)

        self._explored = True
        return self.codebase_context

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

        # Explore codebases if brownfield repos are provided
        if brownfield_repos:
            self._selected_brownfield_repos = list(brownfield_repos)
            await self.explore_codebases(brownfield_repos)

        # Prepend PM context to the initial context
        pm_context = _PM_SYSTEM_PROMPT_PREFIX + initial_context

        if self.codebase_context:
            pm_context += f"\n\n## Existing Codebase Context (BROWNFIELD)\n{self.codebase_context}"

        result = await self.inner.start_interview(
            initial_context=pm_context,
            interview_id=interview_id,
        )

        if result.is_ok:
            log.info(
                "pm.interview_started",
                interview_id=result.value.interview_id,
                has_brownfield=bool(self.codebase_context),
            )

        return result

    async def ask_next_question(
        self,
        state: InterviewState,
    ) -> Result[str, ProviderError | ValidationError]:
        """Generate and classify the next question.

        Delegates question generation to the inner engine, then classifies
        the question. Planning questions pass through unchanged. Development
        questions are reframed for PM audience or deferred.

        Args:
            state: Current interview state.

        Returns:
            Result containing the (possibly reframed) question or error.
        """
        # Generate question via inner engine
        question_result = await self.inner.ask_next_question(state)

        if question_result.is_err:
            return question_result

        question = question_result.value

        # Classify the question
        context = self._build_interview_context(state)
        classify_result = await self.classifier.classify(
            question=question,
            interview_context=context,
        )

        if classify_result.is_err:
            # Classification failed — return original question (safe fallback)
            log.warning("pm.classification_failed", question=question[:100])
            return question_result

        classification = classify_result.value
        self.classifications.append(classification)

        output_type = classification.output_type

        if output_type == ClassifierOutputType.DEFERRED:
            # Track as deferred item and generate a new question
            self.deferred_items.append(classification.original_question)
            log.info(
                "pm.question_deferred",
                question=classification.original_question[:100],
                reasoning=classification.reasoning,
                output_type=output_type,
            )
            # Feed an automatic response back to the inner InterviewEngine
            # so the round is properly recorded and the engine advances.
            # This prevents the inner engine from re-generating similar
            # technical questions it doesn't know were already handled.
            await self.record_response(
                state,
                user_response="[Deferred to development phase] "
                "This technical decision will be addressed during the "
                "development interview.",
                question=classification.original_question,
            )
            # Recursively ask for the next real question
            return await self.ask_next_question(state)

        if output_type == ClassifierOutputType.DECIDE_LATER:
            # Auto-answer with placeholder — no PM interaction needed
            placeholder = classification.placeholder_response
            self.decide_later_items.append(classification.original_question)
            log.info(
                "pm.question_decide_later",
                question=classification.original_question[:100],
                placeholder=placeholder[:100],
                reasoning=classification.reasoning,
            )
            # Record the placeholder as the response so the interview
            # engine advances its round count
            await self.record_response(
                state,
                user_response=f"[Decide later] {placeholder}",
                question=classification.original_question,
            )
            # Recursively ask for the next real question
            return await self.ask_next_question(state)

        if output_type == ClassifierOutputType.REFRAMED:
            # Use the reframed version and track the mapping
            reframed = classification.question_for_pm
            self._reframe_map[reframed] = classification.original_question
            log.info(
                "pm.question_reframed",
                original=classification.original_question[:100],
                reframed=reframed[:100],
                output_type=output_type,
            )
            return Result.ok(reframed)

        # PASSTHROUGH — planning question forwarded unchanged to the PM
        log.debug(
            "pm.question_passthrough",
            question=classification.original_question[:100],
            output_type=output_type,
        )
        return Result.ok(classification.question_for_pm)

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

        The bundled format recorded in the inner engine is::

            [Original technical question: <original>]
            [PM was asked (reframed): <reframed>]
            PM answer: <response>

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
            # Bundle the original technical question with the PM's answer
            bundled_question = (
                f"[Original technical question: {original_question}]\n"
                f"[PM was asked (reframed): {question}]"
            )
            bundled_response = f"PM answer: {user_response}"

            log.info(
                "pm.response_bundled",
                original_question=original_question[:100],
                reframed_question=question[:100],
            )

            return await self.inner.record_response(state, bundled_response, bundled_question)

        return await self.inner.record_response(state, user_response, question)

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
        """Return the list of decide-later items collected during the interview.

        These are the original question texts for questions classified as
        DECIDE_LATER — premature or unknowable at the PM stage. Shown to
        the PM at interview end so they have a clear record of open items.

        Returns:
            List of original question text strings. Empty if none were deferred.
        """
        return list(self.decide_later_items)

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

        Sets ``deferred_items``, ``decide_later_items``, ``codebase_context``,
        ``pending_reframe`` (via ``_reframe_map``), and syncs the classifier's
        ``codebase_context`` so that subsequent classification calls use the
        brownfield context.

        This is the inverse of the meta dict produced by
        :func:`pm_handler._save_pm_meta`.

        Args:
            meta: Dictionary previously persisted as ``pm_meta_{session_id}.json``.
                  Expected keys: ``deferred_items``, ``decide_later_items``,
                  ``codebase_context``, ``pending_reframe``.
        """
        self.deferred_items = list(meta.get("deferred_items", []))
        self.decide_later_items = list(meta.get("decide_later_items", []))
        self.codebase_context = meta.get("codebase_context", "") or ""
        # Sync classifier so brownfield context is available for classification
        self.classifier.codebase_context = self.codebase_context
        # Restore brownfield repo selection
        self._selected_brownfield_repos = list(meta.get("brownfield_repos", []))
        # Restore classification history (string values from ClassificationOutput)
        saved_classifications = meta.get("classifications", [])
        if saved_classifications and not self.classifications:
            from ouroboros.bigbang.question_classifier import (
                ClassificationOutput,
                ClassificationResult,
            )

            for c_val in saved_classifications:
                try:
                    output_type = ClassificationOutput(c_val)
                    self.classifications.append(
                        ClassificationResult(
                            question="", output_type=output_type, reasoning="restored"
                        )
                    )
                except ValueError:
                    pass
        # Restore the reframe map from pending_reframe if present
        pending = meta.get("pending_reframe")
        if pending and isinstance(pending, dict):
            self._reframe_map[pending["reframed"]] = pending["original"]

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
        if not state.rounds:
            return Result.err(
                ValidationError(
                    "Cannot generate PM seed from empty interview",
                    field="rounds",
                )
            )

        context = self._build_interview_context(state)

        messages = [
            Message(role=MessageRole.SYSTEM, content=_EXTRACTION_SYSTEM_PROMPT),
            Message(
                role=MessageRole.USER,
                content=self._build_extraction_prompt(context),
            ),
        ]

        config = CompletionConfig(
            model=self.model,
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
                deferred_count=len(seed.deferred_items),
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
        """Save PMSeed to YAML file.

        Saves to ~/.ouroboros/seeds/pm_seed_{id}.yaml.

        Args:
            seed: The PMSeed to save.
            output_dir: Custom output directory (defaults to ~/.ouroboros/seeds/).

        Returns:
            Path to the saved YAML file.
        """
        if output_dir is None:
            output_dir = _SEED_DIR

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{seed.pm_id}.yaml"
        filepath = output_dir / filename

        yaml_content = yaml.dump(
            seed.to_dict(),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        filepath.write_text(yaml_content, encoding="utf-8")

        log.info(
            "pm.seed_saved",
            path=str(filepath),
            pm_id=seed.pm_id,
        )

        return filepath

    def save_pm_document(
        self,
        seed: PMSeed,
        output_dir: str | Path | None = None,
        *,
        output_path: str | Path | None = None,
    ) -> Path:
        """Generate and save PM document (pm.md).

        Args:
            seed: The PMSeed to generate document from.
            output_dir: Directory to save in. Defaults to .ouroboros/.
                Ignored when *output_path* is provided.
            output_path: Full file path (directory + filename) for the PM
                document. When given, *output_dir* is ignored.

        Returns:
            Path to the saved pm.md.
        """
        return save_pm_document(seed, output_dir, output_path=output_path)

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

    def _build_interview_context(self, state: InterviewState) -> str:
        """Build interview context string from state.

        Args:
            state: Current interview state.

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {state.initial_context}"]

        for round_data in state.rounds:
            parts.append(f"\nQ: {round_data.question}")
            if round_data.user_response:
                parts.append(f"A: {round_data.user_response}")

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

        if self.deferred_items:
            deferred_text = "\n".join(f"- {item}" for item in self.deferred_items)
            prompt += f"""

The following technical questions were deferred during the interview.
Include them in "deferred_items":
{deferred_text}
"""

        if self.decide_later_items:
            decide_later_text = "\n".join(f"- {item}" for item in self.decide_later_items)
            prompt += f"""

The following questions were identified as premature or unknowable at this stage.
Include them as original question text in "decide_later_items":
{decide_later_text}
"""

        if self.codebase_context:
            prompt += f"""

Brownfield codebase context:
{self.codebase_context[:2000]}
"""

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
        import re

        text = response.strip()

        # Extract JSON from markdown code blocks if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                text = json_match.group(0)

        data = json.loads(text)

        # Parse user stories
        stories = tuple(
            UserStory(
                persona=s.get("persona", "User"),
                action=s.get("action", ""),
                benefit=s.get("benefit", ""),
            )
            for s in data.get("user_stories", [])
        )

        # Merge deferred items from classifier with extraction
        all_deferred = list(data.get("deferred_items", []))
        for item in self.deferred_items:
            if item not in all_deferred:
                all_deferred.append(item)

        # Merge decide-later items — stored as original question text
        all_decide_later = list(data.get("decide_later_items", []))
        for item in self.decide_later_items:
            if item not in all_decide_later:
                all_decide_later.append(item)

        # Include brownfield repos — use session-stored repos, not DB
        brownfield_repos = tuple(dict(r) for r in self._selected_brownfield_repos)

        return PMSeed(
            product_name=data.get("product_name", ""),
            goal=data.get("goal", ""),
            user_stories=stories,
            constraints=tuple(data.get("constraints", [])),
            success_criteria=tuple(data.get("success_criteria", [])),
            deferred_items=tuple(all_deferred),
            decide_later_items=tuple(all_decide_later),
            assumptions=tuple(data.get("assumptions", [])),
            interview_id=interview_id,
            codebase_context=self.codebase_context,
            brownfield_repos=brownfield_repos,
        )
