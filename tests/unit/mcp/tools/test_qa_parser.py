"""Unit tests for QA response parsing fallbacks."""

from ouroboros.mcp.tools.qa import _parse_qa_response


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
