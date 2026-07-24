"""Regression tests: LLM-response parsers must tolerate markdown code fences
*and* surrounding prose.

Before this fix, ``WonderEngine``, ``ReflectEngine`` and ``AssertionExtractor``
stripped fences with a fragile ``content.startswith("```")`` + ``lines[1:-1]``
heuristic. That heuristic fails whenever the model emits prose *before* the
fence (``Here is the analysis:\\n```json ...``) or trailing text *after* the
closing fence — both extremely common with Gemini-family models — silently
degrading Wonder to its parse-error fallback and Reflect/extractor to empty
output. All three now delegate to the shared ``extract_json_payload`` helper,
which already handled these cases for the semantic/consensus evaluators.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.evolution.wonder import WonderEngine
from ouroboros.verification.extractor import AssertionExtractor

_ONTOLOGY = OntologySchema(
    name="login",
    description="Login system ontology",
    fields=(OntologyField(name="user", field_type="entity", description="A user"),),
)


def _seed(num_acs: int = 3) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a login system",
        constraints=("Must use OAuth",),
        acceptance_criteria=tuple(f"AC number {i}" for i in range(1, num_acs + 1)),
        ontology_schema=_ONTOLOGY,
    )


def _wrap(variant: str, payload: str) -> str:
    """Wrap a JSON payload the way real model completions arrive."""
    if variant == "prose_prefix_fence":
        return f"Here is the analysis:\n```json\n{payload}\n```"
    if variant == "fence_trailing_prose":
        return f"```json\n{payload}\n```\nLet me know if you need anything else."
    if variant == "bare_fence":
        return f"```json\n{payload}\n```"
    if variant == "no_fence":
        return payload
    raise AssertionError(f"unknown variant: {variant}")


# ``prose_prefix_fence`` and ``fence_trailing_prose`` are the two variants the
# old heuristic got wrong; ``bare_fence`` and ``no_fence`` guard against
# regressions on the paths it did handle.
FENCE_VARIANTS = ["prose_prefix_fence", "fence_trailing_prose", "bare_fence", "no_fence"]


class TestWonderFenceRobustness:
    @pytest.mark.parametrize("variant", FENCE_VARIANTS)
    def test_parse_response_recovers_wrapped_json(self, variant: str) -> None:
        payload = json.dumps(
            {
                "questions": [{"question": "What handles token refresh?", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "grounded reasoning",
            }
        )
        content = _wrap(variant, payload)

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(content, _seed(3))

        # On the real payload, ``reasoning`` is the model's text; the parse-error
        # fallback would instead start with "Parse error, ...".
        assert out.reasoning == "grounded reasoning"
        assert out.should_continue is True
        assert any("token refresh" in q for q in out.questions)


class TestAssertionExtractorFenceRobustness:
    @pytest.mark.parametrize("variant", FENCE_VARIANTS)
    def test_parse_response_recovers_wrapped_json_array(self, variant: str) -> None:
        payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "build passes",
                }
            ]
        )
        content = _wrap(variant, payload)

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            content, ("AC number 1",)
        )

        assert len(assertions) == 1
        assert assertions[0].ac_index == 0
        assert assertions[0].description == "build passes"
