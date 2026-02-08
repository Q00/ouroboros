"""Tests for the Gemini Context Analyzer."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.dashboard.gemini_analyzer import (
    GeminiContextAnalyzer,
    IterationData,
    AnalysisInsight,
    ProgressTrajectory,
    FullHistoryAnalysis,
)
from ouroboros.core.types import Result
from ouroboros.core.errors import ProviderError


class TestIterationData:
    """Tests for IterationData class."""

    def test_iteration_data_creation(self) -> None:
        """Test creating iteration data."""
        data = IterationData(
            iteration_id=1,
            timestamp=datetime.now(),
            phase="Discover",
            action="Explore area",
            result="Found path",
            state={"position": [0, 0]},
            metrics={"efficiency": 0.5},
            reasoning="Based on previous data",
        )

        assert data.iteration_id == 1
        assert data.phase == "Discover"
        assert data.action == "Explore area"

    def test_iteration_data_frozen(self) -> None:
        """Test that iteration data is immutable."""
        data = IterationData(
            iteration_id=1,
            timestamp=datetime.now(),
            phase="Discover",
            action="Test",
            result="Success",
            state={},
        )

        with pytest.raises(AttributeError):
            data.iteration_id = 2  # type: ignore


class TestAnalysisInsight:
    """Tests for AnalysisInsight class."""

    def test_insight_creation(self) -> None:
        """Test creating an insight."""
        insight = AnalysisInsight(
            insight_type="pattern",
            title="Test Pattern",
            description="A test pattern was found",
            confidence=0.85,
            affected_iterations=[1, 5, 10],
            evidence=["Evidence 1", "Evidence 2"],
        )

        assert insight.insight_type == "pattern"
        assert insight.confidence == 0.85
        assert len(insight.affected_iterations) == 3


class TestProgressTrajectory:
    """Tests for ProgressTrajectory class."""

    def test_trajectory_creation(self) -> None:
        """Test creating a trajectory."""
        trajectory = ProgressTrajectory(
            dimension="efficiency",
            values=[(1, 0.5), (2, 0.6), (3, 0.7)],
            trend="improving",
            inflection_points=[2],
        )

        assert trajectory.dimension == "efficiency"
        assert trajectory.trend == "improving"
        assert len(trajectory.values) == 3


class TestGeminiContextAnalyzer:
    """Tests for GeminiContextAnalyzer class."""

    def test_token_estimation(self) -> None:
        """Test token count estimation."""
        analyzer = GeminiContextAnalyzer()

        # ~4 chars per token
        text = "a" * 400
        assert analyzer._estimate_tokens(text) == 100

    def test_prompt_building(self) -> None:
        """Test prompt construction."""
        analyzer = GeminiContextAnalyzer()

        iterations = [
            IterationData(
                iteration_id=i,
                timestamp=datetime.now(),
                phase="Discover",
                action=f"Action {i}",
                result=f"Result {i}",
                state={"step": i},
            )
            for i in range(3)
        ]

        prompt = analyzer._build_history_prompt(
            iterations,
            "Test maze problem",
        )

        assert "Test maze problem" in prompt
        assert "Iteration 0" in prompt
        assert "Iteration 1" in prompt
        assert "Iteration 2" in prompt
        assert "DEVIL'S ADVOCATE" in prompt

    @pytest.mark.asyncio
    async def test_analyze_empty_history(self) -> None:
        """Test analyzing empty iteration history."""
        analyzer = GeminiContextAnalyzer()

        result = await analyzer.analyze_full_history([])

        assert result.is_err
        assert "No iterations" in result.error.message

    @pytest.mark.asyncio
    async def test_analyze_full_history_success(self) -> None:
        """Test successful full history analysis."""
        analyzer = GeminiContextAnalyzer()

        iterations = [
            IterationData(
                iteration_id=i,
                timestamp=datetime.now(),
                phase="Develop",
                action=f"Action {i}",
                result="Success",
                state={"step": i},
            )
            for i in range(5)
        ]

        # Mock the API response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '''{
            "insights": [
                {
                    "insight_type": "pattern",
                    "title": "Test Pattern",
                    "description": "Found a pattern",
                    "confidence": 0.9,
                    "affected_iterations": [1, 2, 3],
                    "evidence": ["Evidence"]
                }
            ],
            "trajectories": [
                {
                    "dimension": "efficiency",
                    "values": [[1, 0.5], [2, 0.6]],
                    "trend": "improving",
                    "inflection_points": []
                }
            ],
            "devil_advocate_critique": "The solution is sound",
            "summary": "Analysis complete"
        }'''
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 1000
        mock_response.usage.completion_tokens = 500
        mock_response.usage.total_tokens = 1500

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = mock_response

            result = await analyzer.analyze_full_history(iterations)

            assert result.is_ok
            analysis = result.value
            assert analysis.total_iterations == 5
            assert len(analysis.insights) == 1
            assert analysis.insights[0].title == "Test Pattern"
            assert len(analysis.trajectories) == 1
            assert analysis.summary == "Analysis complete"

    @pytest.mark.asyncio
    async def test_analyze_full_history_api_error(self) -> None:
        """Test handling API errors."""
        analyzer = GeminiContextAnalyzer()

        iterations = [
            IterationData(
                iteration_id=1,
                timestamp=datetime.now(),
                phase="Test",
                action="Test",
                result="Test",
                state={},
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_api:
            mock_api.side_effect = Exception("API Error")

            result = await analyzer.analyze_full_history(iterations)

            assert result.is_err
            assert "API Error" in result.error.message

    @pytest.mark.asyncio
    async def test_analyze_full_history_json_error(self) -> None:
        """Test handling invalid JSON response."""
        analyzer = GeminiContextAnalyzer()

        iterations = [
            IterationData(
                iteration_id=1,
                timestamp=datetime.now(),
                phase="Test",
                action="Test",
                result="Test",
                state={},
            )
        ]

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "not valid json"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = mock_response

            result = await analyzer.analyze_full_history(iterations)

            assert result.is_err
            assert "JSON" in result.error.message

    def test_model_constant(self) -> None:
        """Test that the model constant is set correctly."""
        assert GeminiContextAnalyzer.MODEL == "gemini-2.5-pro-preview-05-06"

    def test_max_input_tokens(self) -> None:
        """Test that max input tokens allows for large context."""
        assert GeminiContextAnalyzer.MAX_INPUT_TOKENS == 800_000
