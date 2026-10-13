"""``ouroboros_qa`` must not mint a pass out of a malformed verdict shape.

Two shapes used to reach ``passed=True`` through ``QAHandler.handle``:

* an outer verdict with no top-level ``score`` whose *nested* object carried one
  (a dimension written as ``{"score": 0.95}``, or a named wrapper sitting next to
  the model's own ``verdict: "fail"``) — the nested score was promoted and the
  outer verdict discarded;
* a JSON boolean as ``score`` — ``bool`` is an ``int``, so ``true`` became 1.0.

Both are now rejected at the public entry point, and the two consumers that
read ``passed`` (Ralph's stop decision, the auto pipeline's Seed QA gate) see a
failure rather than a pass.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ouroboros.auto.adapters import HandlerSeedQAEvaluator
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo
from ouroboros.ralph_loop import _qa_authoritative_failure, _qa_passed


class _FakeLLM:
    def __init__(self, body: str) -> None:
        self.body = body

    async def complete(self, messages: Any, config: Any) -> Result[CompletionResponse, Any]:
        return Result.ok(
            CompletionResponse(content=self.body, model="fake", usage=UsageInfo(0, 0, 0))
        )


def _handler(body: str) -> QAHandler:
    return QAHandler(llm_adapter=_FakeLLM(body), llm_backend="claude_code")  # type: ignore[arg-type]


_ARGS = {"artifact": "def main(): pass", "quality_bar": "CLI works and is tested"}

# Each payload is one the model could emit; none carries a usable top-level score.
_REJECTED: dict[str, str] = {
    # outer verdict says fail; a dimension written as an object carries a high score
    "nested-dimension-object": json.dumps(
        {
            "verdict": "fail",
            "reasoning": "Core feature missing; tests absent.",
            "differences": ["no CLI entrypoint"],
            "suggestions": [],
            "security": {"score": 0.95, "note": "no secrets found"},
        }
    ),
    "nested-object-revise": json.dumps(
        {"verdict": "revise", "reasoning": "Half done", "best_case": {"score": 0.9}}
    ),
    # a named wrapper next to the model's own string verdict is not an envelope
    "named-wrapper-beside-verdict": json.dumps(
        {"verdict": "fail", "result": {"score": 0.95, "verdict": "pass"}}
    ),
    "named-wrapper-beside-reasoning": json.dumps(
        {"qa_verdict": {"score": 0.9, "verdict": "pass"}, "reasoning": "AC2 unmet"}
    ),
    # two candidate envelopes: no way to tell which one is the verdict
    "two-wrappers": json.dumps({"result": {"score": 0.9}, "evaluation": {"score": 0.2}}),
    # a scoreless sibling verdict competes with the scored wrapper; it is not metadata
    "scoreless-sibling-verdict": json.dumps(
        {
            "qa_verdict": {"verdict": "fail", "reasoning": "CLI missing"},
            "result": {"score": 0.95, "verdict": "pass"},
        }
    ),
    "unnamed-sibling-verdict": json.dumps(
        {
            "assessment": {"verdict": "fail", "reasoning": "AC2 unmet"},
            "result": {"score": 0.95, "verdict": "pass"},
        }
    ),
    # JSON booleans are not scores
    "score-true-verdict-fail": json.dumps(
        {"score": True, "verdict": "fail", "differences": ["broken"], "reasoning": "fails"}
    ),
    "score-true-no-verdict": json.dumps({"score": True, "differences": [], "reasoning": ""}),
    "score-false": json.dumps({"score": False, "verdict": "fail", "reasoning": "fails"}),
    # json.loads accepts NaN; it slips past a range check (every comparison is False)
    "score-nan": '{"score": NaN, "verdict": "pass", "reasoning": "?"}',
}

# The subset that used to come out of QAHandler as ``passed=True``. ``score-false``
# and ``score-nan`` already produced ``passed=False``, so a consumer test over them
# would pass with or without the fix and prove nothing.
_FAIL_OPEN = {k: v for k, v in _REJECTED.items() if k not in {"score-false", "score-nan"}}


@pytest.mark.parametrize("body", list(_REJECTED.values()), ids=list(_REJECTED))
async def test_malformed_verdict_shape_is_rejected(body: str) -> None:
    result = await _handler(body).handle(dict(_ARGS))

    assert result.is_err
    assert result.error.tool_name == "ouroboros_qa"
    assert "score must be a float between 0.0 and 1.0" in str(result.error)


@pytest.mark.parametrize("body", list(_FAIL_OPEN.values()), ids=list(_FAIL_OPEN))
async def test_ralph_does_not_stop_on_malformed_verdict(body: str) -> None:
    result = await _handler(body).handle(dict(_ARGS))
    # Mirrors evolution_handlers' evolve_step: ``qa`` is published only when
    # QAHandler returned ok, alongside ``qa_attempted=True``.
    step_meta: dict[str, Any] = {"action": "converged", "qa_attempted": True}
    if result.is_ok:
        step_meta["qa"] = result.value.meta

    assert _qa_passed(step_meta) is False
    assert _qa_authoritative_failure(step_meta) is True


@pytest.mark.parametrize("body", list(_FAIL_OPEN.values()), ids=list(_FAIL_OPEN))
async def test_seed_qa_gate_fails_on_malformed_verdict(body: str) -> None:
    seed = Seed(
        goal="Build a CLI todo app",
        ontology_schema=OntologySchema(name="todo", description="tasks"),
        acceptance_criteria=("`todo add x` prints 'added x'",),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    res = await HandlerSeedQAEvaluator(_handler(body))(seed, {"sections": {}})

    assert res.passed is False
    assert res.verdict == "fail"
    assert res.score == 0.0
    assert res.error is not None and "score must be a float" in res.error


# A pure envelope (no verdict field of its own beside the wrapper) is still unwrapped.
@pytest.mark.parametrize(
    ("body", "score", "verdict", "passed", "loop_action"),
    [
        (
            json.dumps({"qa_verdict": {"score": 0.3, "verdict": "fail", "reasoning": "x"}}),
            0.3,
            "fail",
            False,
            "escalate",
        ),
        (
            json.dumps({"verdict": {"score": 0.9, "verdict": "pass", "reasoning": "ok"}}),
            0.9,
            "pass",
            True,
            "done",
        ),
        (
            json.dumps({"result": {"score": 0.6}, "model_note": "envelope extra"}),
            0.6,
            "revise",
            False,
            "continue",
        ),
        (
            json.dumps({"score": 0.85, "verdict": "pass", "dimensions": {}, "reasoning": "ok"}),
            0.85,
            "pass",
            True,
            "done",
        ),
    ],
    ids=["qa_verdict-envelope", "verdict-envelope", "result-envelope-extra-key", "flat"],
)
async def test_well_formed_verdicts_still_parse(
    body: str, score: float, verdict: str, passed: bool, loop_action: str
) -> None:
    result = await _handler(body).handle(dict(_ARGS))

    assert result.is_ok
    meta = result.value.meta
    assert meta["score"] == score
    assert meta["verdict"] == verdict
    assert meta["passed"] is passed
    assert meta["loop_action"] == loop_action


async def test_non_numeric_dimension_scores_are_dropped() -> None:
    body = (
        '{"score": 0.5, "verdict": "revise", "reasoning": "x",'
        ' "dimensions": {"bool_dim": true, "nan_dim": NaN, "inf_dim": Infinity, "ok": 0.7}}'
    )
    result = await _handler(body).handle(dict(_ARGS))

    assert result.is_ok
    assert result.value.meta["dimensions"] == {"ok": 0.7}
    assert result.value.meta["passed"] is False
    assert result.value.meta["loop_action"] == "continue"


# JSON that is present but not one authoritative object must not fall through to
# the plain-text reader, which would take ``Score: 0.95`` from an example.
_AMBIGUOUS_JSON: dict[str, str] = {
    "two-fences-then-example": (
        '```json\n{"verdict": "fail"}\n```\n```json\n{"verdict": "fail"}\n```\n'
        "Example:\nScore: 0.95\nVerdict: pass"
    ),
    "two-objects-then-example": (
        '{"verdict": "fail"} and {"verdict": "fail"}\nScore: 0.95\nVerdict: pass'
    ),
    "tilde-fences-then-example": (
        '~~~json\n{"score": 0.2}\n~~~\n~~~json\n{"score": 0.3}\n~~~\nScore: 0.95\nVerdict: pass'
    ),
}


@pytest.mark.parametrize("body", list(_AMBIGUOUS_JSON.values()), ids=list(_AMBIGUOUS_JSON))
async def test_ambiguous_json_does_not_fall_back_to_prose(body: str) -> None:
    result = await _handler(body).handle(dict(_ARGS))

    assert result.is_err
    assert "no single unambiguous verdict object" in str(result.error)

    step_meta: dict[str, Any] = {"action": "converged", "qa_attempted": True}
    assert _qa_passed(step_meta) is False


async def test_prose_without_json_still_uses_the_fallback() -> None:
    result = await _handler("Score: 0.9\nVerdict: pass\nReasoning: all ACs met").handle(dict(_ARGS))

    assert result.is_ok
    assert result.value.meta["score"] == 0.9
    assert result.value.meta["passed"] is True
