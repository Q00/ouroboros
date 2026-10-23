"""Regression coverage for tool-scoped prose at the MCP security boundary."""

from copy import deepcopy
from typing import Any

from pydantic import ValidationError
import pytest

from ouroboros.core.types import Result
from ouroboros.interview_adapters.models import InterviewTurnContext
from ouroboros.mcp.server.security import InputValidator

LATERAL_TOOL = "ouroboros_lateral_think"
INTERVIEW_TOOL = "ouroboros_interview"
HISTORICAL_ATTEMPT = (
    "Platform scope too large for hackathon; do not promise all agents or dot-to-dot API."
)
HISTORICAL_EXCERPT = (
    "dawn-dusk sun-synchronous low Earth orbit; "
    "illustrative satellite constellation mean altitude 650 km."
)


def _arguments(tool_name: str, prose: str) -> dict[str, Any]:
    if tool_name == LATERAL_TOOL:
        return {
            "problem_context": "Keep the project achievable",
            "current_approach": "Bound the demo",
            "failed_attempts": ["An earlier attempt", prose],
        }
    return {
        "initial_context": "Design a small orbit demonstration",
        "references": [
            {
                "reference_id": "orbit-design",
                "label": "Orbit design",
                "origin": "url",
                "url": "https://example.com/orbit-design",
                "excerpt": prose,
            }
        ],
    }


@pytest.mark.parametrize(
    ("tool_name", "prose"),
    [(LATERAL_TOOL, HISTORICAL_ATTEMPT), (INTERVIEW_TOOL, HISTORICAL_EXCERPT)],
)
def test_observed_prose_reaches_validation_unchanged(tool_name: str, prose: str) -> None:
    arguments = _arguments(tool_name, prose)
    original = deepcopy(arguments)

    result = InputValidator().validate(tool_name, arguments)

    assert result.is_ok, result.error
    assert arguments == original


@pytest.mark.parametrize("tool_name", [LATERAL_TOOL, INTERVIEW_TOOL])
@pytest.mark.parametrize(
    "prose",
    [
        "First option; second option",
        "Compare input | output",
        "The documented check is lint && tests",
        "The fallback condition is primary || secondary",
        "The earlier implementation called subprocess.run() and open(path)",
        "The parser used eval(value), exec(code), and compile(source)",
        "The old import came from ../shared/config.py",
        r"The Windows example referenced ..\shared\config.py",
    ],
)
def test_supported_prose_can_quote_punctuation_and_code(tool_name: str, prose: str) -> None:
    result = InputValidator().validate(tool_name, _arguments(tool_name, prose))

    assert result.is_ok, result.error


@pytest.mark.parametrize("source_tool", [LATERAL_TOOL, INTERVIEW_TOOL])
@pytest.mark.parametrize("other_tool", ["unrelated_tool", "ouroboros_pm_interview"])
def test_same_fields_on_other_tools_remain_checked(source_tool: str, other_tool: str) -> None:
    result = InputValidator().validate(other_tool, _arguments(source_tool, "A; B"))

    assert result.is_err
    assert "Shell metacharacter" in str(result.error)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (INTERVIEW_TOOL, {"failed_attempts": ["A; B"]}),
        (LATERAL_TOOL, {"references": [{"excerpt": "A; B"}]}),
        (LATERAL_TOOL, {"wrapper": {"failed_attempts": ["A; B"]}}),
        (INTERVIEW_TOOL, {"wrapper": {"references": [{"excerpt": "A; B"}]}}),
        (LATERAL_TOOL, {"failed_attempts": [{"command": "A; B"}]}),
        (LATERAL_TOOL, {"failed_attempts": [["A; B"]]}),
        (LATERAL_TOOL, {"failed_attempts": {"0": "A; B"}}),
        (INTERVIEW_TOOL, {"references": {"0": {"excerpt": "A; B"}}}),
        (INTERVIEW_TOOL, {"references": [{"excerpt": {"command": "A; B"}}]}),
        (INTERVIEW_TOOL, {"references": [{"excerpt": ["A; B"]}]}),
        (INTERVIEW_TOOL, {"excerpt": "A; B"}),
        (LATERAL_TOOL, {"failed_attempts": "A; B"}),
        (LATERAL_TOOL, {"failed_attempts[0]": "A; B"}),
        (INTERVIEW_TOOL, {"references[0].excerpt": "A; B"}),
        (INTERVIEW_TOOL, {"references[0]": {"excerpt": "A; B"}}),
        (INTERVIEW_TOOL, {"references": [{"excerpt.command": "A; B"}]}),
    ],
)
def test_lookalike_or_deeper_paths_do_not_gain_exemptions(
    tool_name: str, arguments: dict[str, Any]
) -> None:
    result = InputValidator().validate(tool_name, arguments)

    assert result.is_err
    assert "Shell metacharacter" in str(result.error)


@pytest.mark.parametrize("tool_name", [LATERAL_TOOL, INTERVIEW_TOOL])
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "session; next", "Shell metacharacter"),
        ("cwd", "../../outside", "Path traversal"),
        ("session_id", "__import__('os')", "Potentially dangerous"),
    ],
)
def test_control_fields_remain_checked_beside_allowed_prose(
    tool_name: str, field: str, value: str, error: str
) -> None:
    arguments = _arguments(tool_name, "A; B")
    arguments[field] = value

    result = InputValidator().validate(tool_name, arguments)

    assert result.is_err
    assert error in str(result.error)
    assert field in str(result.error)


@pytest.mark.parametrize("field", ["reference_id", "label", "origin", "url"])
def test_reference_siblings_keep_existing_lexical_checks(field: str) -> None:
    arguments = _arguments(INTERVIEW_TOOL, HISTORICAL_EXCERPT)
    arguments["references"][0][field] = "value; next"

    result = InputValidator().validate(INTERVIEW_TOOL, arguments)

    assert result.is_err
    assert f"Shell metacharacter detected in references[0].{field}" in str(result.error)


@pytest.mark.parametrize("tool_name", [LATERAL_TOOL, INTERVIEW_TOOL])
def test_registered_validator_still_runs_after_prose_exemption(tool_name: str) -> None:
    arguments = _arguments(tool_name, "A; B")
    received: list[dict[str, Any]] = []

    def reject(value: dict[str, Any]) -> Result[None, str]:
        received.append(value)
        return Result.err("existing tool constraint")

    validator = InputValidator()
    validator.register_validator(tool_name, reject)

    result = validator.validate(tool_name, arguments)

    assert received == [arguments]
    assert received[0] is arguments
    assert result.is_err
    assert f"Validation failed for {tool_name}: existing tool constraint" in str(result.error)


@pytest.mark.parametrize(
    "invalid_change",
    [
        {"origin": "unsupported"},
        {"reference_id": "invalid id"},
        {"excerpt": "x" * 2001},
        {"excerpt": "   "},
        {"unexpected_field": "ordinary text"},
    ],
)
def test_reference_model_constraints_still_apply(invalid_change: dict[str, str]) -> None:
    arguments = _arguments(INTERVIEW_TOOL, HISTORICAL_EXCERPT)
    arguments["references"][0].update(invalid_change)

    assert InputValidator().validate(INTERVIEW_TOOL, arguments).is_ok
    with pytest.raises(ValidationError):
        InterviewTurnContext.model_validate({"references": arguments["references"]})


def test_reference_normalization_and_duplicate_checks_are_unchanged() -> None:
    arguments = _arguments(INTERVIEW_TOOL, f"  {HISTORICAL_EXCERPT}  ")
    assert InputValidator().validate(INTERVIEW_TOOL, arguments).is_ok

    context = InterviewTurnContext.model_validate({"references": arguments["references"]})

    assert context.references[0].excerpt == HISTORICAL_EXCERPT
    with pytest.raises(ValidationError, match="duplicate reference_id"):
        InterviewTurnContext.model_validate({"references": arguments["references"] * 2})
