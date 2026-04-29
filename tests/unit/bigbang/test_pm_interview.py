"""Unit tests for ouroboros.bigbang.pm_interview module.

Tests the PMInterviewEngine composition pattern, question classification,
PMSeed generation, and brownfield repo management.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    ClassifierOutputType,
    QuestionCategory,
    QuestionClassifier,
)
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionResponse,
    UsageInfo,
)


def _mock_completion(content: str = "What problem does this solve?") -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model="claude-opus-4-6",
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason="stop",
    )


def _make_adapter() -> MagicMock:
    """Create a mock LLM adapter."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion()))
    return adapter


def _make_engine(
    adapter: MagicMock | None = None, tmp_path: Path | None = None
) -> PMInterviewEngine:
    """Create a PMInterviewEngine with mocked dependencies."""
    if adapter is None:
        adapter = _make_adapter()

    state_dir = tmp_path or Path("/tmp/test_pm_interview")
    return PMInterviewEngine.create(
        llm_adapter=adapter,
        state_dir=state_dir,
    )


class TestPMInterviewEngineComposition:
    """Test that PMInterviewEngine wraps InterviewEngine via composition."""

    def test_has_inner_engine(self, tmp_path: Path) -> None:
        """PMInterviewEngine has an inner InterviewEngine attribute."""
        engine = _make_engine(tmp_path=tmp_path)
        assert isinstance(engine.inner, InterviewEngine)

    def test_has_classifier(self, tmp_path: Path) -> None:
        """PMInterviewEngine has a QuestionClassifier."""
        engine = _make_engine(tmp_path=tmp_path)
        assert isinstance(engine.classifier, QuestionClassifier)

    def test_shares_llm_adapter(self, tmp_path: Path) -> None:
        """Inner engine and classifier share the same LLM adapter."""
        adapter = _make_adapter()
        engine = PMInterviewEngine.create(
            llm_adapter=adapter,
            state_dir=tmp_path,
        )
        assert engine.inner.llm_adapter is adapter
        assert engine.classifier.llm_adapter is adapter
        assert engine.llm_adapter is adapter

    def test_does_not_inherit_from_interview_engine(self) -> None:
        """PMInterviewEngine does NOT inherit from InterviewEngine."""
        assert not issubclass(PMInterviewEngine, InterviewEngine)

    def test_create_factory(self, tmp_path: Path) -> None:
        """create() factory method properly wires all components."""
        adapter = _make_adapter()
        engine = PMInterviewEngine.create(
            llm_adapter=adapter,
            model="test-model",
            state_dir=tmp_path,
        )

        assert engine.inner.model == "test-model"
        assert engine.model == "test-model"
        assert engine.inner.state_dir == tmp_path

    def test_initial_state_is_clean(self, tmp_path: Path) -> None:
        """Newly created engine has empty deferred items and classifications."""
        engine = _make_engine(tmp_path=tmp_path)
        assert engine.deferred_items == []
        assert engine.classifications == []
        assert engine.codebase_context == ""
        assert engine._explored is False


class TestOpeningQuestion:
    """Test the initial 'what do you want to build?' question."""

    def test_get_opening_question_returns_string(self, tmp_path: Path) -> None:
        """get_opening_question returns a non-empty question string."""
        engine = _make_engine(tmp_path=tmp_path)
        question = engine.get_opening_question()

        assert isinstance(question, str)
        assert len(question) > 0
        assert "build" in question.lower()

    def test_get_opening_question_is_static(self) -> None:
        """get_opening_question is a static method — callable without instance."""
        question = PMInterviewEngine.get_opening_question()
        assert isinstance(question, str)
        assert "build" in question.lower()

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_creates_interview(self, tmp_path: Path) -> None:
        """ask_opening_and_start creates an interview from the PM's answer."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.ask_opening_and_start(
            user_response="I want to build a task management tool for small teams"
        )

        assert result.is_ok
        state = result.value
        assert state.interview_id
        assert state.status == InterviewStatus.IN_PROGRESS
        # The PM's answer should be included in the initial context
        assert "task management tool" in state.initial_context

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_strips_whitespace(self, tmp_path: Path) -> None:
        """ask_opening_and_start strips leading/trailing whitespace from answer."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.ask_opening_and_start(user_response="  Build a dashboard  \n")

        assert result.is_ok
        assert "Build a dashboard" in result.value.initial_context

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_empty_response_errors(self, tmp_path: Path) -> None:
        """ask_opening_and_start rejects empty responses."""
        engine = _make_engine(tmp_path=tmp_path)

        result = await engine.ask_opening_and_start(user_response="")
        assert result.is_err
        assert "describe" in str(result.error).lower() or "build" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_whitespace_only_errors(self, tmp_path: Path) -> None:
        """ask_opening_and_start rejects whitespace-only responses."""
        engine = _make_engine(tmp_path=tmp_path)

        result = await engine.ask_opening_and_start(user_response="   \n\t  ")
        assert result.is_err

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_passes_brownfield_repos(self, tmp_path: Path) -> None:
        """ask_opening_and_start forwards brownfield_repos to start_interview."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        async def _fake_explore(repos: list[dict[str, str]]) -> str:
            engine.codebase_context = "Python project"
            return "Python project"

        with patch.object(
            engine,
            "explore_codebases",
            new_callable=AsyncMock,
            side_effect=_fake_explore,
        ) as mock_explore:
            result = await engine.ask_opening_and_start(
                user_response="Build a feature on top of existing code",
                brownfield_repos=[{"path": "/code/proj", "name": "proj", "desc": ""}],
            )

            assert result.is_ok
            state = result.value
            assert state.is_brownfield is True
            assert state.codebase_paths == [{"path": "/code/proj", "role": "primary"}]
            mock_explore.assert_called_once()

    @pytest.mark.asyncio
    async def test_ask_opening_and_start_passes_interview_id(self, tmp_path: Path) -> None:
        """ask_opening_and_start forwards custom interview_id."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.ask_opening_and_start(
            user_response="Build a CLI tool",
            interview_id="custom_id_123",
        )

        assert result.is_ok
        assert result.value.interview_id == "custom_id_123"


class TestStartInterview:
    """Test PM interview start."""

    @pytest.mark.asyncio
    async def test_start_delegates_to_inner(self, tmp_path: Path) -> None:
        """start_interview delegates to inner engine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview("Build a task manager")

        assert result.is_ok
        state = result.value
        assert state.interview_id
        assert state.status == InterviewStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_start_stores_user_context_without_pm_steering(self, tmp_path: Path) -> None:
        """start_interview persists only user context, not PM steering prefix."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview("Build a task manager")

        state = result.value
        # Persisted initial_context should contain user input only
        assert "Build a task manager" in state.initial_context
        # PM steering prefix should NOT leak into persisted state
        assert "Product Requirements" not in state.initial_context
        # Engine holds steering separately
        assert hasattr(engine, "_pm_steering")
        assert "Product Requirements" in engine._pm_steering

    @pytest.mark.asyncio
    async def test_start_merges_codebase_context_and_user_answer(self, tmp_path: Path) -> None:
        """start_interview merges CodebaseExplorer scan results plus user answer
        into initial_context for the inner InterviewEngine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        codebase_summary = (
            "### [PRIMARY] /code/my-app\n"
            "Tech: Python\n"
            "Deps: fastapi, sqlalchemy\n"
            "Python project using FastAPI with SQLAlchemy ORM.\n"
        )

        with patch("ouroboros.bigbang.pm_interview.CodebaseExplorer") as MockExplorer:
            mock_explorer = MagicMock()
            mock_explorer.explore = AsyncMock(return_value=[])
            MockExplorer.return_value = mock_explorer

            with patch(
                "ouroboros.bigbang.pm_interview.format_explore_results",
                return_value=codebase_summary,
            ):
                result = await engine.start_interview(
                    initial_context="Add a notifications feature for users",
                    brownfield_repos=[
                        {"path": "/code/my-app", "name": "my-app", "desc": "Main app"}
                    ],
                )

        assert result.is_ok
        state = result.value
        ctx = state.initial_context

        # User answer must be present
        assert "Add a notifications feature for users" in ctx
        # PM steering prefix should NOT be in persisted state
        assert "Product Requirements" not in ctx
        # Codebase exploration context must be present
        assert "Existing Codebase Context (BROWNFIELD)" in ctx
        assert "Python project using FastAPI" in ctx
        assert "fastapi, sqlalchemy" in ctx
        # User answer appears BEFORE the codebase context section
        user_pos = ctx.index("Add a notifications feature")
        codebase_pos = ctx.index("Existing Codebase Context")
        assert user_pos < codebase_pos

    @pytest.mark.asyncio
    async def test_start_without_brownfield_has_no_codebase_section(self, tmp_path: Path) -> None:
        """start_interview without brownfield repos does not include codebase section."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview("Build a new greenfield app")

        assert result.is_ok
        ctx = result.value.initial_context
        assert "Build a new greenfield app" in ctx
        assert "Existing Codebase Context" not in ctx

    @pytest.mark.asyncio
    async def test_ask_opening_merges_codebase_and_answer_into_initial_context(
        self, tmp_path: Path
    ) -> None:
        """ask_opening_and_start merges codebase scan results plus the PM's
        opening answer into initial_context for the inner InterviewEngine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        codebase_summary = "### [PRIMARY] /proj\nTech: Go\nGo monorepo with gRPC services."

        with patch("ouroboros.bigbang.pm_interview.CodebaseExplorer") as MockExplorer:
            mock_explorer = MagicMock()
            mock_explorer.explore = AsyncMock(return_value=[])
            MockExplorer.return_value = mock_explorer

            with patch(
                "ouroboros.bigbang.pm_interview.format_explore_results",
                return_value=codebase_summary,
            ):
                result = await engine.ask_opening_and_start(
                    user_response="I want to add a billing module to our platform",
                    brownfield_repos=[{"path": "/proj", "name": "proj", "desc": "Platform"}],
                )

        assert result.is_ok
        ctx = result.value.initial_context

        # Both the user's answer and codebase context must be merged
        assert "billing module" in ctx
        assert "Go monorepo with gRPC" in ctx
        assert "BROWNFIELD" in ctx


class TestAskNextQuestion:
    """Test question generation with classification."""

    @pytest.mark.asyncio
    async def test_planning_question_passes_through(self, tmp_path: Path) -> None:
        """Planning questions are returned unchanged."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Mock inner engine to return a planning question
        planning_q = "Who are the target users for this product?"

        # First call: inner engine generates question
        # Second call: classifier classifies it as planning
        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(planning_q)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": planning_q,
                                "reasoning": "Business question about users",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == planning_q
        assert len(engine.classifications) == 1
        assert engine.classifications[0].category == QuestionCategory.PLANNING

    @pytest.mark.asyncio
    async def test_dev_question_gets_reframed(self, tmp_path: Path) -> None:
        """Development questions are reframed for PM audience."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        dev_q = "Which database engine should we use — PostgreSQL or MongoDB?"
        reframed_q = (
            "What are your data storage needs — structured or flexible data, and how much volume?"
        )

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(dev_q)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "development",
                                "reframed_question": reframed_q,
                                "reasoning": "Database choice is dev concern, reframed to business need",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == reframed_q
        assert engine.classifications[0].category == QuestionCategory.DEVELOPMENT

    @pytest.mark.asyncio
    async def test_pm_steering_wrapper_accepts_prompt_budget_kwargs(self, tmp_path: Path) -> None:
        """PM prompt wrapper remains compatible with InterviewEngine prompt budgeting."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        planning_q = "Who are the target users?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(planning_q)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": planning_q,
                                "reasoning": "Target users are a PM concern",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_pm_budget_kwargs",
            initial_context=("A" * 3_489) + "TAIL_MARKER",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == planning_q

    @pytest.mark.asyncio
    async def test_initial_context_summary_question_bypasses_classification(
        self, tmp_path: Path
    ) -> None:
        """Long-context recovery prompt is returned verbatim, not classified."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        state = InterviewState(
            interview_id="test_pm_summary_recovery",
            initial_context=("A" * 4_000) + "RAW_TAIL",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == INITIAL_CONTEXT_SUMMARY_QUESTION
        adapter.complete.assert_not_called()
        assert engine.classifications == []

    @pytest.mark.asyncio
    async def test_deferred_question_returned_to_user(self, tmp_path: Path) -> None:
        """DEV-only questions marked as defer_to_dev are returned to the user."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        dev_q = "Should we use gRPC or REST for inter-service communication?"

        adapter.complete = AsyncMock(
            side_effect=[
                # Question generation: dev question
                Result.ok(_mock_completion(dev_q)),
                # Classification: defer to dev
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "development",
                                "reframed_question": dev_q,
                                "reasoning": "Purely technical protocol choice",
                                "defer_to_dev": True,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # The user sees the deferred question directly
        assert result.value == dev_q
        # deferred_items NOT populated yet (user hasn't chosen to skip)
        assert dev_q not in engine.deferred_items
        # No rounds auto-recorded
        assert len(state.rounds) == 0

    @pytest.mark.asyncio
    async def test_user_can_skip_as_deferred(self, tmp_path: Path) -> None:
        """User can defer a technical question via skip_as_deferred()."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        dev_q = "What container orchestration platform should we use — Kubernetes or ECS?"

        state = InterviewState(
            interview_id="test_auto_response",
            initial_context="Build a SaaS platform",
        )

        # User chooses to defer
        adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion("ok")))
        result = await engine.skip_as_deferred(state, dev_q)

        assert result.is_ok
        assert dev_q in engine.deferred_items
        # Verify the deferral response was properly recorded
        assert len(state.rounds) == 1
        assert "[Deferred to development phase]" in state.rounds[0].user_response

    @pytest.mark.asyncio
    async def test_deferred_question_returned_not_auto_skipped(self, tmp_path: Path) -> None:
        """DEFERRED questions are returned to user, not auto-skipped."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        dev_q1 = "Should we use gRPC or REST?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(dev_q1)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "development",
                                "reframed_question": dev_q1,
                                "reasoning": "Protocol choice",
                                "defer_to_dev": True,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_multi_defer",
            initial_context="Build a platform",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # First deferred question returned directly — no recursion
        assert result.value == dev_q1
        # Not yet in deferred_items (user hasn't chosen to skip)
        assert dev_q1 not in engine.deferred_items
        assert len(state.rounds) == 0

    @pytest.mark.asyncio
    async def test_classification_failure_returns_original(self, tmp_path: Path) -> None:
        """If classification fails, original question is returned."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        question = "What problem does this solve?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(question)),
                Result.err(ProviderError("rate limit")),
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == question

    @pytest.mark.asyncio
    async def test_decide_later_returns_question_without_auto_answering(
        self, tmp_path: Path
    ) -> None:
        """Decide-later questions are returned to the caller for user decision.

        The engine no longer auto-answers with a placeholder or recurses.
        The caller (main session) detects classification == "decide_later"
        and presents the user with a decide-later option.
        """
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        decide_later_q = "How should we handle scaling when we reach 1M users?"
        placeholder = "This will be determined after MVP launch and initial user metrics. Marking as a decision point for later."

        adapter.complete = AsyncMock(
            side_effect=[
                # Question generation: decide-later question
                Result.ok(_mock_completion(decide_later_q)),
                # Classification: decide_later
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "decide_later",
                                "reframed_question": decide_later_q,
                                "reasoning": "Scaling is a post-MVP concern",
                                "defer_to_dev": False,
                                "decide_later": True,
                                "placeholder_response": placeholder,
                            }
                        )
                    )
                ),
                # No second question generation — no recursion
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # The decide-later question is returned to the caller
        assert result.value == decide_later_q
        # decide_later_items is NOT populated — caller handles that
        assert engine.decide_later_items == []
        # No auto-response recorded — state has no rounds
        assert len(state.rounds) == 0
        # Classification is recorded for caller to detect
        assert engine.get_last_classification() == "decide_later"

    @pytest.mark.asyncio
    async def test_decide_later_classification_result_properties(self) -> None:
        """ClassificationResult with decide_later has correct output_type and question_for_pm."""
        result = ClassificationResult(
            original_question="How should we handle scaling?",
            category=QuestionCategory.DECIDE_LATER,
            reframed_question="How should we handle scaling?",
            reasoning="Post-MVP concern",
            decide_later=True,
            placeholder_response="TBD after MVP launch.",
        )

        assert result.output_type == ClassifierOutputType.DECIDE_LATER
        # Returned to user so they can choose to answer or defer
        assert result.question_for_pm == "How should we handle scaling?"


class TestPMInterviewContext:
    """Test PM interview context construction."""

    def test_context_uses_prompt_safe_initial_context_and_skips_summary_round(
        self, tmp_path: Path
    ) -> None:
        """PM contexts avoid raw oversized initial context and synthetic summary Q&A."""
        engine = _make_engine(tmp_path=tmp_path)
        state = InterviewState(
            interview_id="test_pm_large_context",
            initial_context=("A" * 4_000) + "RAW_TAIL",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                    user_response=("B" * 4_000) + "SUMMARY_TAIL",
                ),
                InterviewRound(
                    round_number=2,
                    question="Who are the target users?",
                    user_response="Small teams",
                ),
            ],
        )

        context = engine._build_interview_context(state)

        assert "Context truncated for prompt safety" in context
        assert "RAW_TAIL" not in context
        assert "SUMMARY_TAIL" not in context
        assert INITIAL_CONTEXT_SUMMARY_QUESTION not in context
        assert "Who are the target users?" in context
        assert "Small teams" in context


class TestCheckCompletion:
    """Test PM interview completion checks."""

    @pytest.mark.asyncio
    async def test_summary_round_does_not_count_toward_minimum_rounds(self, tmp_path: Path) -> None:
        """Initial-context summary recovery is not a substantive PM answer."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        state = InterviewState(
            interview_id="test_pm_summary_round_count",
            initial_context=("A" * 4_000) + "RAW_TAIL",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                    user_response="Concise product summary",
                ),
                InterviewRound(
                    round_number=2,
                    question="Who are the users?",
                    user_response="Small teams",
                ),
                InterviewRound(
                    round_number=3,
                    question="What problem do they have?",
                    user_response="Tracking work",
                ),
            ],
        )

        result = await engine.check_completion(state)

        assert result is None
        adapter.complete.assert_not_called()


class TestRecordResponse:
    """Test response recording delegation."""

    @pytest.mark.asyncio
    async def test_delegates_to_inner_engine(self, tmp_path: Path) -> None:
        """record_response delegates to inner InterviewEngine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.record_response(state, "Small teams", "Who are the users?")

        assert result.is_ok
        assert len(state.rounds) == 1
        assert state.rounds[0].user_response == "Small teams"

    @pytest.mark.asyncio
    async def test_bundles_reframed_question_with_original(self, tmp_path: Path) -> None:
        """When a question was reframed, record_response bundles the original
        technical question with the PM's answer for the inner engine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        original_q = "Which database engine should we use — PostgreSQL or MongoDB?"
        reframed_q = "What are your data storage needs — structured or flexible data?"

        # Simulate ask_next_question having populated the reframe map
        engine._reframe_map[reframed_q] = original_q

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.record_response(
            state, "We need structured data with lots of relationships", reframed_q
        )

        assert result.is_ok
        assert len(state.rounds) == 1

        # The inner engine should have received the bundled question
        recorded_question = state.rounds[0].question
        assert original_q in recorded_question
        assert reframed_q in recorded_question
        assert "[Original technical question:" in recorded_question
        assert "[PM was asked (reframed):" in recorded_question

        # The inner engine should have received the bundled response
        recorded_response = state.rounds[0].user_response
        assert "PM answer:" in recorded_response
        assert "structured data with lots of relationships" in recorded_response

    @pytest.mark.asyncio
    async def test_reframe_map_cleared_after_recording(self, tmp_path: Path) -> None:
        """After recording a response, the reframe mapping is consumed (popped)."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        reframed_q = "What are your data storage needs?"
        engine._reframe_map[reframed_q] = "Which database?"

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        await engine.record_response(state, "Structured data", reframed_q)

        # Mapping should be consumed
        assert reframed_q not in engine._reframe_map

    @pytest.mark.asyncio
    async def test_non_reframed_question_passes_through(self, tmp_path: Path) -> None:
        """Non-reframed (planning) questions pass through without bundling."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        planning_q = "Who are the target users?"

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.record_response(state, "Small teams", planning_q)

        assert result.is_ok
        assert state.rounds[0].question == planning_q
        assert state.rounds[0].user_response == "Small teams"

    @pytest.mark.asyncio
    async def test_ask_then_record_reframed_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end: ask_next_question reframes, record_response bundles."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        dev_q = "Which database engine should we use?"
        reframed_q = "What are your data storage needs?"

        adapter.complete = AsyncMock(
            side_effect=[
                # Inner engine generates dev question
                Result.ok(_mock_completion(dev_q)),
                # Classifier reframes it
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "development",
                                "reframed_question": reframed_q,
                                "reasoning": "Database choice is dev concern",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        # Ask — should get reframed question
        q_result = await engine.ask_next_question(state)
        assert q_result.is_ok
        assert q_result.value == reframed_q

        # Verify reframe map was populated
        assert reframed_q in engine._reframe_map
        assert engine._reframe_map[reframed_q] == dev_q

        # Record response — should bundle
        r_result = await engine.record_response(state, "Structured relational data", reframed_q)
        assert r_result.is_ok

        # Verify bundled content in the round
        round_data = state.rounds[0]
        assert dev_q in round_data.question
        assert reframed_q in round_data.question
        assert "PM answer:" in round_data.user_response
        assert "Structured relational data" in round_data.user_response

        # Reframe map should be consumed
        assert reframed_q not in engine._reframe_map


class TestCompleteInterview:
    """Test interview completion."""

    @pytest.mark.asyncio
    async def test_delegates_to_inner_engine(self, tmp_path: Path) -> None:
        """complete_interview delegates to inner InterviewEngine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.complete_interview(state)

        assert result.is_ok
        assert state.status == InterviewStatus.COMPLETED


class TestPMSeedGeneration:
    """Test PMSeed generation from completed interview."""

    @pytest.mark.asyncio
    async def test_generates_seed_from_interview(self, tmp_path: Path) -> None:
        """generate_pm_seed extracts PMSeed from interview state."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        extraction_response = json.dumps(
            {
                "product_name": "TaskFlow",
                "goal": "Help small teams manage tasks efficiently",
                "user_stories": [
                    {
                        "persona": "Team Lead",
                        "action": "create and assign tasks",
                        "benefit": "I can track team progress",
                    }
                ],
                "constraints": ["Must work offline", "Budget under $10k"],
                "success_criteria": ["Users can create tasks in under 10 seconds"],
                "deferred_items": [],
                "assumptions": ["Teams have internet for sync"],
            }
        )

        adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion(extraction_response)))

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Who are the users?",
                    user_response="Small teams of 5-10 people",
                ),
            ],
        )

        result = await engine.generate_pm_seed(state)

        assert result.is_ok
        seed = result.value
        assert seed.product_name == "TaskFlow"
        assert seed.goal == "Help small teams manage tasks efficiently"
        assert len(seed.user_stories) == 1
        assert seed.user_stories[0].persona == "Team Lead"
        assert len(seed.constraints) == 2
        assert seed.interview_id == "test_001"

    @pytest.mark.asyncio
    async def test_includes_deferred_items_in_decide_later(self, tmp_path: Path) -> None:
        """LLM-extracted deferred items are merged into decide_later_items on PMSeed."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        # Raw engine items are passed to the extraction prompt as context,
        # so the LLM should summarise them.  Both LLM-extracted deferred_items
        # and engine-tracked deferred_items are merged into decide_later_items
        # on the PMSeed.
        engine.deferred_items = ["Should we use gRPC or REST?"]

        extraction_response = json.dumps(
            {
                "product_name": "TaskFlow",
                "goal": "Task management",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": ["Database selection"],
                "assumptions": [],
            }
        )

        adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion(extraction_response)))

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(round_number=1, question="Q?", user_response="A"),
            ],
        )

        result = await engine.generate_pm_seed(state)

        assert result.is_ok
        seed = result.value
        assert "Database selection" in seed.decide_later_items
        # Engine-tracked item is merged back to prevent data loss
        assert "Should we use gRPC or REST?" in seed.decide_later_items

    @pytest.mark.asyncio
    async def test_empty_interview_returns_error(self, tmp_path: Path) -> None:
        """Generating seed from empty interview returns error."""
        engine = _make_engine(tmp_path=tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.generate_pm_seed(state)
        assert result.is_err

    @pytest.mark.asyncio
    async def test_summary_only_interview_returns_empty_error(self, tmp_path: Path) -> None:
        """Synthetic summary recovery alone is not substantive PM interview content."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        state = InterviewState(
            interview_id="test_summary_only_pm_seed",
            initial_context=("A" * 4_000) + "RAW_TAIL",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                    user_response="Concise product summary",
                ),
            ],
        )

        result = await engine.generate_pm_seed(state)

        assert result.is_err
        assert "empty interview" in result.error.message
        adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_large_context_without_summary_returns_summary_required(
        self, tmp_path: Path
    ) -> None:
        """PM seed generation enforces the long-context summary requirement."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)
        state = InterviewState(
            interview_id="test_pm_seed_missing_summary",
            initial_context=("A" * 4_000) + "RAW_TAIL",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Who are the target users?",
                    user_response="Small teams",
                ),
            ],
        )

        result = await engine.generate_pm_seed(state)

        assert result.is_err
        assert "summary required" in result.error.message
        adapter.complete.assert_not_called()


class TestSavePMSeed:
    """Test PMSeed persistence."""

    def test_saves_yaml_to_seeds_dir(self, tmp_path: Path) -> None:
        """save_pm_seed writes YAML to output directory."""
        engine = _make_engine(tmp_path=tmp_path)

        seed = PMSeed(
            product_name="TaskFlow",
            goal="Task management for small teams",
            user_stories=(
                UserStory(persona="PM", action="create tasks", benefit="track progress"),
            ),
            constraints=("Must work offline",),
            success_criteria=("Create task in 10s",),
        )

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")

        assert filepath.exists()
        assert filepath.suffix == ".json"

        loaded = json.loads(filepath.read_text())
        assert loaded["product_name"] == "TaskFlow"
        assert loaded["goal"] == "Task management for small teams"
        assert len(loaded["user_stories"]) == 1


class TestPMSeed:
    """Test PMSeed frozen dataclass."""

    def test_frozen(self) -> None:
        """PMSeed is frozen — attributes cannot be changed."""
        seed = PMSeed(product_name="Test")

        with pytest.raises(AttributeError):
            seed.product_name = "Changed"  # type: ignore[misc]

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        """PMSeed can roundtrip through dict serialization."""
        original = PMSeed(
            product_name="TaskFlow",
            goal="Manage tasks",
            user_stories=(UserStory(persona="PM", action="create tasks", benefit="efficiency"),),
            constraints=("offline",),
            success_criteria=("fast creation",),
            decide_later_items=("db choice",),
            assumptions=("internet for sync",),
        )

        data = original.to_dict()
        restored = PMSeed.from_dict(data)

        assert restored.product_name == original.product_name
        assert restored.goal == original.goal
        assert len(restored.user_stories) == 1
        assert restored.user_stories[0].persona == "PM"
        assert restored.constraints == original.constraints

    def test_to_initial_context_produces_yaml(self) -> None:
        """to_initial_context produces valid YAML string."""
        seed = PMSeed(
            product_name="TaskFlow",
            goal="Manage tasks",
        )

        context = seed.to_initial_context()

        # Should be valid YAML
        parsed = yaml.safe_load(context)
        assert parsed["product_name"] == "TaskFlow"
        assert parsed["goal"] == "Manage tasks"


class TestBrownfieldRepoManagement:
    """Test DB-based brownfield repo management."""

    def test_load_brownfield_repos_delegates_to_db(self, tmp_path: Path) -> None:
        """load_brownfield_repos delegates to load_brownfield_repos_as_dicts."""
        expected = [{"path": "/code/my-project", "name": "My Project", "desc": "Main app"}]

        with patch(
            "ouroboros.bigbang.pm_interview._load_brownfield_dicts",
            return_value=expected,
        ):
            repos = PMInterviewEngine.load_brownfield_repos()

        assert len(repos) == 1
        assert repos[0]["path"] == "/code/my-project"
        assert repos[0]["name"] == "My Project"

    def test_load_empty_returns_empty_list(self, tmp_path: Path) -> None:
        """Loading when DB is empty returns empty list."""
        with patch(
            "ouroboros.bigbang.pm_interview._load_brownfield_dicts",
            return_value=[],
        ):
            repos = PMInterviewEngine.load_brownfield_repos()
            assert repos == []


class TestCodebaseExploration:
    """Test scan-once codebase exploration."""

    @pytest.mark.asyncio
    async def test_explores_once(self, tmp_path: Path) -> None:
        """explore_codebases only scans once — subsequent calls return cached."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        with patch("ouroboros.bigbang.pm_interview.CodebaseExplorer") as MockExplorer:
            mock_explorer = MagicMock()
            mock_explorer.explore = AsyncMock(return_value=[])
            MockExplorer.return_value = mock_explorer

            repos = [{"path": "/code/proj", "name": "proj"}]

            # First call — scans
            await engine.explore_codebases(repos)
            assert mock_explorer.explore.call_count == 1

            # Second call — cached
            await engine.explore_codebases(repos)
            assert mock_explorer.explore.call_count == 1  # No additional call

    @pytest.mark.asyncio
    async def test_shares_context_with_classifier(self, tmp_path: Path) -> None:
        """Exploration context is shared with the classifier."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        with patch("ouroboros.bigbang.pm_interview.CodebaseExplorer") as MockExplorer:
            mock_explorer = MagicMock()
            mock_explorer.explore = AsyncMock(return_value=[])
            MockExplorer.return_value = mock_explorer

            with patch(
                "ouroboros.bigbang.pm_interview.format_explore_results",
                return_value="Python project with FastAPI",
            ):
                await engine.explore_codebases([{"path": "/code/proj", "name": "proj"}])

                assert engine.codebase_context == "Python project with FastAPI"
                assert engine.classifier.codebase_context == "Python project with FastAPI"


class TestDevInterviewHandoff:
    """Test PMSeed to dev interview handoff."""

    def test_pm_seed_to_dev_context(self) -> None:
        """pm_seed_to_dev_context produces YAML for initial_context."""
        seed = PMSeed(
            product_name="TaskFlow",
            goal="Manage tasks for small teams",
            constraints=("offline support",),
        )

        context = PMInterviewEngine.pm_seed_to_dev_context(seed)

        parsed = yaml.safe_load(context)
        assert parsed["product_name"] == "TaskFlow"
        assert parsed["goal"] == "Manage tasks for small teams"
        assert "offline support" in parsed["constraints"]


class TestSaveAndLoadState:
    """Test state persistence delegation."""

    @pytest.mark.asyncio
    async def test_save_delegates(self, tmp_path: Path) -> None:
        """save_state delegates to inner engine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        result = await engine.save_state(state)
        assert result.is_ok

    @pytest.mark.asyncio
    async def test_load_delegates(self, tmp_path: Path) -> None:
        """load_state delegates to inner engine."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        # Save first
        await engine.save_state(state)

        # Load
        result = await engine.load_state("test_001")
        assert result.is_ok
        assert result.value.interview_id == "test_001"


# ──────────────────────────────────────────────────────────────
# restore_meta tests
# ──────────────────────────────────────────────────────────────


class TestRestoreMeta:
    """Tests for PMInterviewEngine.restore_meta()."""

    def test_restore_meta_sets_all_fields(self) -> None:
        engine = _make_engine()
        meta = {
            "deferred_items": ["item1", "item2"],
            "decide_later_items": ["dl1"],
            "codebase_context": "some context",
            "pending_reframe": {"reframed": "q_reframed", "original": "q_original"},
        }

        engine.restore_meta(meta)

        # Legacy deferred_items are merged into decide_later_items on restore
        assert engine.deferred_items == []
        assert engine.decide_later_items == ["dl1", "item1", "item2"]
        assert engine.codebase_context == "some context"
        assert engine._reframe_map["q_reframed"] == "q_original"

    def test_restore_meta_syncs_classifier_codebase_context(self) -> None:
        engine = _make_engine()
        meta = {
            "codebase_context": "brownfield info here",
        }

        engine.restore_meta(meta)

        assert engine.classifier.codebase_context == "brownfield info here"

    def test_restore_meta_defaults_on_empty_dict(self) -> None:
        engine = _make_engine()
        # Pre-populate to verify reset
        engine.deferred_items = ["old"]
        engine.decide_later_items = ["old_dl"]
        engine.codebase_context = "old context"

        engine.restore_meta({})

        assert engine.deferred_items == []
        assert engine.decide_later_items == []
        assert engine.codebase_context == ""
        assert engine.classifier.codebase_context == ""

    def test_restore_meta_skips_pending_reframe_when_none(self) -> None:
        engine = _make_engine()
        meta: dict[str, object] = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
        }

        engine.restore_meta(meta)

        assert engine._reframe_map == {}

    def test_restore_meta_handles_none_codebase_context(self) -> None:
        engine = _make_engine()
        meta = {"codebase_context": None}

        engine.restore_meta(meta)

        assert engine.codebase_context == ""
        assert engine.classifier.codebase_context == ""
