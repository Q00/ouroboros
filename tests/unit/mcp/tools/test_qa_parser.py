"""Unit tests for QA response parsing and score-based verdicts."""

import json

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.qa import QAHandler, _parse_qa_response
from ouroboros.providers.base import CompletionResponse, UsageInfo


class TestParseQAResponse:
    def test_parses_plain_text_score_and_verdict_when_json_missing(self) -> None:
        response = """Quality review complete.

Score: 0.84 / 1.00
Verdict: pass
Reasoning: Acceptance criteria are clear and testable.
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.84
        assert result.value.verdict == "pass"
        assert result.value.reasoning == "Acceptance criteria are clear and testable."

    def test_derives_verdict_from_plain_text_score_when_verdict_missing(self) -> None:
        response = """Review summary:
score = 0.55
The artifact is close but needs revision.
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.55
        assert result.value.verdict == "revise"

    def test_parses_bulleted_score_and_verdict(self) -> None:
        response = """Review:

- Score: 0.91
- Verdict: pass
- Reasoning: All criteria met.
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.91
        assert result.value.verdict == "pass"
        assert result.value.reasoning == "All criteria met."

    def test_parses_markdown_bold_score_and_verdict(self) -> None:
        response = """## QA Result

**Score**: 0.72
**Verdict**: revise
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.72
        assert result.value.verdict == "revise"

    def test_parses_dash_separated_score_and_verdict(self) -> None:
        response = """Assessment complete.

Score - 0.88
Verdict - pass
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.88
        assert result.value.verdict == "pass"

    def test_dash_separated_verdict_is_overridden_by_score(self) -> None:
        """A provider's contradictory verdict cannot override the score."""
        response = """Assessment complete.

Score - 0.88
Verdict - fail
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.score == 0.88
        assert result.value.verdict == "pass"

    @pytest.mark.parametrize(
        ("score", "provider_verdict", "pass_threshold", "expected"),
        [
            (0.39, "pass", 0.8, "fail"),
            (0.40, "pass", 0.8, "revise"),
            (0.42, "pass", 0.8, "revise"),
            (0.79, "fail", 0.8, "revise"),
            (0.80, "fail", 0.8, "pass"),
            (0.30, "fail", 0.2, "pass"),
        ],
    )
    @pytest.mark.parametrize("response_format", ["json", "plain_text"])
    def test_verdict_follows_score_across_formats_and_boundaries(
        self,
        score: float,
        provider_verdict: str,
        pass_threshold: float,
        expected: str,
        response_format: str,
    ) -> None:
        if response_format == "json":
            response = json.dumps({"score": score, "verdict": provider_verdict})
        else:
            response = f"Score: {score}\nVerdict: {provider_verdict}"

        result = _parse_qa_response(response, pass_threshold=pass_threshold)

        assert result.is_ok
        assert result.value.verdict == expected

    def test_parses_suggestions_section(self) -> None:
        response = """Score: 0.75
Verdict: revise

Differences:
- Missing error handling for edge cases
- No input validation

Suggestions:
- Add try/except blocks around API calls
- Validate user input at entry points
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.differences == [
            "Missing error handling for edge cases",
            "No input validation",
        ]
        assert result.value.suggestions == [
            "Add try/except blocks around API calls",
            "Validate user input at entry points",
        ]

    def test_parses_dimensions_section(self) -> None:
        response = """Score: 0.82
Verdict: pass

Dimensions:
- accuracy: 0.9
- clarity: 0.8
- completeness: 0.75
"""

        result = _parse_qa_response(response, pass_threshold=0.8)

        assert result.is_ok
        assert result.value.dimensions == {
            "accuracy": 0.9,
            "clarity": 0.8,
            "completeness": 0.75,
        }


def test_out_of_range_large_integer_score_returns_parse_error() -> None:
    result = _parse_qa_response(json.dumps({"score": 10**1000, "verdict": "pass"}))

    assert result.is_err
    assert "score must be a float between 0.0 and 1.0" in result.error


@pytest.mark.parametrize(
    ("score", "provider_verdict", "expected_verdict", "expected_action", "passed"),
    [
        (0.42, "pass", "revise", "continue", False),
        (0.95, "fail", "pass", "done", True),
        (0.39, "pass", "fail", "escalate", False),
    ],
)
async def test_handler_publishes_consistent_score_based_result(
    score: float,
    provider_verdict: str,
    expected_verdict: str,
    expected_action: str,
    passed: bool,
) -> None:
    class FakeLLM:
        async def complete(
            self, messages: object, config: object
        ) -> Result[CompletionResponse, str]:
            return Result.ok(
                CompletionResponse(
                    content=json.dumps(
                        {
                            "score": score,
                            "verdict": provider_verdict,
                            "dimensions": {},
                            "differences": [],
                            "suggestions": [],
                            "reasoning": "test",
                        }
                    ),
                    model="fake",
                    usage=UsageInfo(0, 0, 0),
                )
            )

    result = await QAHandler(llm_adapter=FakeLLM(), llm_backend="claude_code").handle(
        {
            "artifact": "example",
            "quality_bar": "meets requirements",
            "qa_session_id": "qa-test",
            "iteration_history": [{"iteration": 1, "score": 0.1, "verdict": "fail"}],
        }
    )

    assert result.is_ok
    assert result.value.meta is not None
    meta = result.value.meta
    assert meta["score"] == score
    assert meta["verdict"] == expected_verdict
    assert meta["loop_action"] == expected_action
    assert meta["passed"] is passed
    assert meta["iteration_entry"] == {
        "iteration": 2,
        "score": score,
        "verdict": expected_verdict,
        "loop_action": expected_action,
    }
    text = result.value.content[0].text
    assert f"Score: {score:.2f} / 1.00 [{expected_verdict.upper()}]" in text
    assert f"Verdict: {expected_verdict}" in text
    assert f"Loop Action: {expected_action}" in text
