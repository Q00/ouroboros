"""Tests that the classifier receives full Q&A history, current question, and brownfield context.

AC 18: Classifier receives full Q&A history plus current question plus brownfield context.

These tests verify that when PMInterviewEngine.ask_next_question() calls
classifier.classify(), it passes:
  1. The current question as the `question` parameter
  2. Full Q&A history (all previous rounds) as `interview_context`
  3. Brownfield/codebase context via classifier.codebase_context
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import (
    InterviewRound,
    InterviewState,
)
from ouroboros.bigbang.pm_interview import PMInterviewEngine
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
    state_dir = tmp_path or Path("/tmp/test_classifier_context")
    return PMInterviewEngine.create(
        llm_adapter=adapter,
        state_dir=state_dir,
    )


class TestClassifierReceivesFullContext:
    """Verify classifier.classify() gets full Q&A history, question, and brownfield context."""

    @pytest.mark.asyncio
    async def test_classifier_receives_current_question(self, tmp_path: Path) -> None:
        """The current question is passed as the `question` parameter to classify()."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        current_question = "What is the target market for this product?"

        # Mock inner engine to return the question
        adapter.complete = AsyncMock(
            side_effect=[
                # Question generation
                Result.ok(_mock_completion(current_question)),
                # Classification response
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": current_question,
                                "reasoning": "Market question is PM-answerable",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        # Spy on the classifier's classify method
        original_classify = engine.classifier.classify
        engine.classifier.classify = AsyncMock(wraps=original_classify)

        state = InterviewState(
            interview_id="test_q_param",
            initial_context="Build a CRM tool",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # Verify classify was called with the current question
        engine.classifier.classify.assert_called_once()
        call_kwargs = engine.classifier.classify.call_args
        assert (
            call_kwargs[1]["question"] == current_question or call_kwargs[0][0] == current_question
        )

    @pytest.mark.asyncio
    async def test_classifier_receives_qa_history_from_rounds(self, tmp_path: Path) -> None:
        """Full Q&A history from previous rounds is passed as interview_context."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Set up state with existing Q&A rounds
        state = InterviewState(
            interview_id="test_history",
            initial_context="Build a project tracker",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Who are the primary users?",
                    user_response="Project managers and team leads",
                ),
                InterviewRound(
                    round_number=2,
                    question="What is the main pain point?",
                    user_response="Lack of visibility into project status",
                ),
            ],
        )

        next_question = "What success metrics matter most?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(next_question)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": next_question,
                                "reasoning": "Metrics question",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        original_classify = engine.classifier.classify
        engine.classifier.classify = AsyncMock(wraps=original_classify)

        result = await engine.ask_next_question(state)
        assert result.is_ok

        # Extract the interview_context that was passed
        call_kwargs = engine.classifier.classify.call_args
        interview_context = (
            call_kwargs[1].get("interview_context") or call_kwargs[0][1]
            if len(call_kwargs[0]) > 1
            else call_kwargs[1]["interview_context"]
        )

        # Should contain the initial context
        assert "Build a project tracker" in interview_context

        # Should contain ALL previous Q&A pairs
        assert "Who are the primary users?" in interview_context
        assert "Project managers and team leads" in interview_context
        assert "What is the main pain point?" in interview_context
        assert "Lack of visibility into project status" in interview_context

    @pytest.mark.asyncio
    async def test_classifier_receives_brownfield_codebase_context(self, tmp_path: Path) -> None:
        """Brownfield codebase context is available to the classifier via codebase_context attr."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Simulate codebase exploration having been done
        brownfield_ctx = (
            "## Codebase: my-api\n"
            "Language: Python\n"
            "Framework: FastAPI\n"
            "Structure: src/api/, src/models/, tests/\n"
            "Key patterns: Repository pattern, dependency injection\n"
        )
        engine.codebase_context = brownfield_ctx
        engine.classifier.codebase_context = brownfield_ctx

        next_question = "What data needs to be stored?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(next_question)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": next_question,
                                "reasoning": "Data requirements question",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        state = InterviewState(
            interview_id="test_brownfield_ctx",
            initial_context="Build a new endpoint",
        )

        result = await engine.ask_next_question(state)
        assert result.is_ok

        # Verify the classifier has the brownfield context
        assert engine.classifier.codebase_context == brownfield_ctx
        assert "FastAPI" in engine.classifier.codebase_context
        assert "Repository pattern" in engine.classifier.codebase_context

    @pytest.mark.asyncio
    async def test_classifier_receives_all_three_contexts_together(self, tmp_path: Path) -> None:
        """Classifier receives current question + full Q&A history + brownfield context simultaneously."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Set up brownfield context
        brownfield_ctx = "## Codebase: payments-service\nLanguage: Go\nFramework: gRPC"
        engine.codebase_context = brownfield_ctx
        engine.classifier.codebase_context = brownfield_ctx

        # Set up state with Q&A history
        state = InterviewState(
            interview_id="test_all_three",
            initial_context="Add payment processing to our platform",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What payment methods should be supported?",
                    user_response="Credit cards and bank transfers initially",
                ),
                InterviewRound(
                    round_number=2,
                    question="What geographic regions are in scope?",
                    user_response="US and EU for the first release",
                ),
            ],
        )

        current_question = "What compliance requirements apply?"

        # Capture the actual LLM messages sent by the classifier
        captured_messages = []

        async def capturing_complete(messages, config):
            captured_messages.append(messages)
            # First call: question generation from inner engine
            if len(captured_messages) == 1:
                return Result.ok(_mock_completion(current_question))
            # Second call: classification
            return Result.ok(
                _mock_completion(
                    json.dumps(
                        {
                            "category": "planning",
                            "reframed_question": current_question,
                            "reasoning": "Compliance is PM domain",
                            "defer_to_dev": False,
                        }
                    )
                )
            )

        adapter.complete = AsyncMock(side_effect=capturing_complete)

        result = await engine.ask_next_question(state)
        assert result.is_ok

        # The second set of messages should be the classifier call
        assert len(captured_messages) >= 2
        classifier_messages = captured_messages[1]

        # Find the user message content sent to the classifier
        user_msg_content = None
        for msg in classifier_messages:
            if msg.role == "user":
                user_msg_content = msg.content
                break

        assert user_msg_content is not None

        # 1. Current question is in the classifier input
        assert current_question in user_msg_content

        # 2. Full Q&A history is included
        assert "What payment methods should be supported?" in user_msg_content
        assert "Credit cards and bank transfers initially" in user_msg_content
        assert "What geographic regions are in scope?" in user_msg_content
        assert "US and EU for the first release" in user_msg_content

        # 3. Brownfield context is included
        assert "payments-service" in user_msg_content
        assert "Go" in user_msg_content or "gRPC" in user_msg_content

    @pytest.mark.asyncio
    async def test_classifier_context_includes_initial_context(self, tmp_path: Path) -> None:
        """The initial context (PM's opening answer) is included in the interview_context."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        initial = "I want to build a real-time collaboration tool for remote teams"
        state = InterviewState(
            interview_id="test_initial_ctx",
            initial_context=initial,
        )

        next_q = "How many concurrent users do you expect?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(next_q)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": next_q,
                                "reasoning": "Scale question",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        original_classify = engine.classifier.classify
        engine.classifier.classify = AsyncMock(wraps=original_classify)

        result = await engine.ask_next_question(state)
        assert result.is_ok

        call_kwargs = engine.classifier.classify.call_args
        interview_context = (
            call_kwargs[1].get("interview_context") or call_kwargs[0][1]
            if len(call_kwargs[0]) > 1
            else call_kwargs[1]["interview_context"]
        )

        assert "real-time collaboration tool" in interview_context

    @pytest.mark.asyncio
    async def test_classifier_context_empty_when_no_history(self, tmp_path: Path) -> None:
        """When there are no prior rounds, context still contains initial_context."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_no_history",
            initial_context="Build a note-taking app",
        )

        next_q = "What platforms should it run on?"

        adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(_mock_completion(next_q)),
                Result.ok(
                    _mock_completion(
                        json.dumps(
                            {
                                "category": "planning",
                                "reframed_question": next_q,
                                "reasoning": "Platform scope question",
                                "defer_to_dev": False,
                            }
                        )
                    )
                ),
            ]
        )

        original_classify = engine.classifier.classify
        engine.classifier.classify = AsyncMock(wraps=original_classify)

        result = await engine.ask_next_question(state)
        assert result.is_ok

        call_kwargs = engine.classifier.classify.call_args
        interview_context = (
            call_kwargs[1].get("interview_context") or call_kwargs[0][1]
            if len(call_kwargs[0]) > 1
            else call_kwargs[1]["interview_context"]
        )

        # Initial context should be present even with no rounds
        assert "Build a note-taking app" in interview_context
        # No Q: or A: entries since no rounds
        assert "Q:" not in interview_context.replace("Initial Context:", "")

    @pytest.mark.asyncio
    async def test_explore_codebases_shares_context_with_classifier(self, tmp_path: Path) -> None:
        """explore_codebases() sets codebase_context on both engine and classifier."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        assert engine.classifier.codebase_context == ""
        assert engine.codebase_context == ""

        # Manually set context (simulating what explore_codebases does)
        ctx = "## Codebase scan results\nFiles: 42\nPatterns: MVC"
        engine.codebase_context = ctx
        engine.classifier.codebase_context = ctx

        assert engine.classifier.codebase_context == ctx
        assert engine.codebase_context == ctx

    @pytest.mark.asyncio
    async def test_build_interview_context_includes_all_rounds(self, tmp_path: Path) -> None:
        """_build_interview_context includes initial context and all Q&A rounds."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        state = InterviewState(
            interview_id="test_build_ctx",
            initial_context="Build an analytics dashboard",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What data sources?",
                    user_response="SQL databases and CSV files",
                ),
                InterviewRound(
                    round_number=2,
                    question="Who needs access?",
                    user_response="Executives and data analysts",
                ),
                InterviewRound(
                    round_number=3,
                    question="What refresh frequency?",
                    user_response="Real-time for key metrics, hourly for reports",
                ),
            ],
        )

        context = engine._build_interview_context(state)

        # Initial context
        assert "Build an analytics dashboard" in context

        # All three rounds
        assert "What data sources?" in context
        assert "SQL databases and CSV files" in context
        assert "Who needs access?" in context
        assert "Executives and data analysts" in context
        assert "What refresh frequency?" in context
        assert "Real-time for key metrics" in context

    @pytest.mark.asyncio
    async def test_classifier_user_message_structure(self, tmp_path: Path) -> None:
        """The classifier's user message has question, interview context, and codebase context sections."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Set brownfield context
        engine.codebase_context = "## Existing API\nEndpoints: /users, /tasks"
        engine.classifier.codebase_context = engine.codebase_context

        state = InterviewState(
            interview_id="test_msg_structure",
            initial_context="Extend the task API",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What new capabilities are needed?",
                    user_response="Recurring tasks and task dependencies",
                ),
            ],
        )

        current_q = "What priority levels should tasks have?"

        captured_messages = []

        async def capture_complete(messages, config):
            captured_messages.append(messages)
            if len(captured_messages) == 1:
                return Result.ok(_mock_completion(current_q))
            return Result.ok(
                _mock_completion(
                    json.dumps(
                        {
                            "category": "planning",
                            "reframed_question": current_q,
                            "reasoning": "Priority is a product decision",
                            "defer_to_dev": False,
                        }
                    )
                )
            )

        adapter.complete = AsyncMock(side_effect=capture_complete)

        result = await engine.ask_next_question(state)
        assert result.is_ok

        # Get the classifier's messages (second call)
        assert len(captured_messages) >= 2
        classifier_msgs = captured_messages[1]

        # Find user message
        user_content = None
        for msg in classifier_msgs:
            if msg.role == "user":
                user_content = msg.content
                break

        assert user_content is not None

        # Verify all three context sections are present
        assert "Question to classify:" in user_content
        assert current_q in user_content
        assert "Interview context" in user_content
        assert "Recurring tasks and task dependencies" in user_content
        assert "Codebase context" in user_content
        assert "/users, /tasks" in user_content
