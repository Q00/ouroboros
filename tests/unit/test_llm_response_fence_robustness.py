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

from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evolution.reflect import ReflectEngine
from ouroboros.evolution.wonder import WonderEngine, WonderOutput
from ouroboros.providers.base import CompletionResponse, UsageInfo
from ouroboros.verification.extractor import AssertionExtractor

_ONTOLOGY = OntologySchema(
    name="login",
    description="Login system ontology",
    fields=(OntologyField(name="user", field_type="entity", description="A user"),),
)

LONG_FENCE_CASES = [(4, "json"), (4, ""), (5, "json"), (5, "")]


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
        return f"```\n{payload}\n```"
    if variant == "long_json_fence":
        return _wrap_long_supported_fence(payload, 4, "json")
    if variant == "long_bare_fence":
        return _wrap_long_supported_fence(payload, 4, "")
    if variant == "longer_json_fence":
        return _wrap_long_supported_fence(payload, 5, "json")
    if variant == "longer_bare_fence":
        return _wrap_long_supported_fence(payload, 5, "")
    if variant == "no_fence":
        return payload
    raise AssertionError(f"unknown variant: {variant}")


def _wrap_long_supported_fence(
    payload: str,
    fence_length: int,
    fence_info: str,
    later_payload: str | None = None,
) -> str:
    delimiter = "`" * fence_length
    content = f"{delimiter}{fence_info}\n{payload}\n{delimiter}"
    if later_payload is not None:
        content += f"\nLater fallback: {later_payload}"
    return content


def _wrap_after_unsupported_fence(example_payload: str, actual_payload: str) -> str:
    return (
        "Do not use this example:\n"
        "```python\n"
        f"EXAMPLE = {example_payload}\n"
        "```\n"
        f"Earlier example: {example_payload}\n"
        "Actual answer:\n"
        "```json\n"
        f"{actual_payload}\n"
        "```"
    )


def _wrap_unclosed_unsupported_fence(stale_payload: str, actual_payload: str) -> str:
    return (
        "Do not use this example:\n"
        "```python\n"
        f"EXAMPLE = {stale_payload}\n"
        "The real answer appears later but the fence never closes:\n"
        f"{actual_payload}"
    )


def _wrap_unsupported_fence_then_prose(stale_payload: str, actual_payload: str) -> str:
    return (
        "Do not use this example:\n"
        "```python\n"
        f"EXAMPLE = {stale_payload}\n"
        "```\n"
        "Actual answer:\n"
        f"{actual_payload}"
    )


def _wrap_invalid_json_fence_then_prose(actual_payload: str) -> str:
    return f"```json\n{{not json}}\n```\n{actual_payload}"


def _wrap_invalid_supported_fence_with_nested_payload(fence_info: str, nested_payload: str) -> str:
    return f"```{fence_info}\ninvalid wrapper {nested_payload}\n```"


# ``prose_prefix_fence`` and ``fence_trailing_prose`` are the two variants the
# old heuristic got wrong; the remaining variants preserve plain, bare, and
# longer supported-fence behavior.
FENCE_VARIANTS = [
    "prose_prefix_fence",
    "fence_trailing_prose",
    "bare_fence",
    "long_json_fence",
    "long_bare_fence",
    "longer_json_fence",
    "longer_bare_fence",
    "no_fence",
]


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

    @pytest.mark.parametrize(("fence_length", "fence_info"), LONG_FENCE_CASES)
    def test_long_supported_fence_wins_over_later_stale_json(
        self, fence_length: int, fence_info: str
    ) -> None:
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token refresh question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )
        stale_payload = json.dumps(
            {
                "questions": [{"question": "stale example question", "kind": "gap"}],
                "should_continue": False,
                "reasoning": "stale example",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_long_supported_fence(actual_payload, fence_length, fence_info, stale_payload),
            _seed(),
        )

        assert out.reasoning == "actual answer"
        assert out.should_continue is True
        assert out.questions == ("actual token refresh question",)

    @pytest.mark.parametrize("fence_info", ["json", ""])
    def test_supported_fence_rejects_invalid_body_with_stale_nested_json(
        self, fence_info: str
    ) -> None:
        stale_payload = json.dumps(
            {
                "questions": [{"question": "stale example question", "kind": "gap"}],
                "should_continue": False,
                "reasoning": "stale example",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_invalid_supported_fence_with_nested_payload(fence_info, stale_payload),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert out.questions == (
            "What assumptions remain untested for goal: Build a login system?",
        )

    def test_unsupported_fence_pair_does_not_let_later_prose_example_win(self) -> None:
        example_payload = json.dumps(
            {
                "questions": [{"question": "stale example question", "kind": "gap"}],
                "should_continue": False,
                "reasoning": "stale example",
            }
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token refresh question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_after_unsupported_fence(example_payload, actual_payload),
            _seed(),
        )

        assert out.reasoning == "actual answer"
        assert out.should_continue is True
        assert out.questions == ("actual token refresh question",)

    def test_unsupported_fence_body_is_excluded_from_prose_fallback(self) -> None:
        stale_payload = json.dumps(
            {
                "questions": [{"question": "stale example question", "kind": "gap"}],
                "should_continue": False,
                "reasoning": "stale example",
            }
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token refresh question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_unsupported_fence_then_prose(stale_payload, actual_payload),
            _seed(),
        )

        assert out.reasoning == "actual answer"
        assert out.should_continue is True
        assert out.questions == ("actual token refresh question",)

    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            lambda _stale, actual: _wrap_invalid_json_fence_then_prose(actual),
        ],
    )
    def test_malformed_fence_fails_closed_instead_of_accepting_stale_material(
        self, content_factory
    ) -> None:
        stale_payload = json.dumps(
            {
                "questions": [{"question": "stale example question", "kind": "gap"}],
                "should_continue": False,
                "reasoning": "stale example",
            }
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token refresh question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            content_factory(stale_payload, actual_payload),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert out.questions == (
            "What assumptions remain untested for goal: Build a login system?",
        )

    def test_malformed_outer_object_with_nested_array_uses_fallback(self) -> None:
        content = '{"questions": ["What remains unknown?"], }'

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(content, _seed())

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert out.questions == (
            "What assumptions remain untested for goal: Build a login system?",
        )

    @pytest.mark.parametrize(
        "content",
        [
            '["incidental", "array"]',
            '{"questions": null, "should_continue": false, "reasoning": "done"}',
            '{"questions": {"question": "not a list"}, "should_continue": false}',
            '{"questions": [[["not a question object"]]], "ontology_tensions": [], "reasoning": 7}',
            '{"questions": [{"question": ["not", "text"]}], "should_continue": true}',
        ],
    )
    def test_bad_typed_shapes_use_fallback_contract(self, content: str) -> None:
        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(content, _seed())

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert out.questions == (
            "What assumptions remain untested for goal: Build a login system?",
        )


class TestReflectFenceRobustness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", FENCE_VARIANTS)
    async def test_successful_fenced_json_variants_parse_through_public_reflect(
        self, variant: str
    ) -> None:
        payload = json.dumps(
            {
                "refined_goal": "Build a login system with refresh-token clarity",
                "refined_constraints": ["Must use OAuth"],
                "ac_patches": [{"op": "keep", "index": 0, "reason": "still valid"}],
                "ontology_mutations": [
                    {
                        "action": "add",
                        "field_name": "refresh_token",
                        "field_type": "entity",
                        "description": "A token used to renew sessions",
                        "reason": "Wonder asked about token refresh",
                    }
                ],
                "reasoning": "reflect reasoning",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap(variant, payload),
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(1),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What handles token refresh?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_ok
        assert result.value.reasoning == "reflect reasoning"
        assert result.value.ontology_mutations[0].field_name == "refresh_token"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("fence_length", "fence_info"), LONG_FENCE_CASES)
    async def test_long_supported_fence_wins_over_later_stale_json(
        self, fence_length: int, fence_info: str
    ) -> None:
        actual_payload = json.dumps(
            {
                "refined_goal": "Build a login system with refresh-token clarity",
                "refined_constraints": ["Must use OAuth"],
                "ac_patches": [{"op": "keep", "index": 0, "reason": "still valid"}],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        stale_payload = json.dumps(
            {
                "refined_goal": "Stale example",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "stale reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_long_supported_fence(
                    actual_payload, fence_length, fence_info, stale_payload
                ),
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(1),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What handles token refresh?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_ok
        assert result.value.reasoning == "actual reflect"
        assert result.value.refined_goal == "Build a login system with refresh-token clarity"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fence_info", ["json", ""])
    async def test_supported_fence_rejects_invalid_body_with_stale_nested_json(
        self, fence_info: str
    ) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "Stale example",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "stale reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_invalid_supported_fence_with_nested_payload(
                    fence_info, stale_payload
                ),
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(1),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What handles token refresh?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_unsupported_fence_body_is_excluded_from_prose_fallback(self) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "Stale example",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "stale reflect",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "Build a login system with refresh-token clarity",
                "refined_constraints": ["Must use OAuth"],
                "ac_patches": [{"op": "keep", "index": 0, "reason": "still valid"}],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_unsupported_fence_then_prose(stale_payload, actual_payload),
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(1),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What handles token refresh?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_ok
        assert result.value.reasoning == "actual reflect"
        assert result.value.refined_goal == "Build a login system with refresh-token clarity"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            lambda _stale, actual: _wrap_invalid_json_fence_then_prose(actual),
        ],
    )
    async def test_malformed_fence_returns_error_instead_of_accepting_stale_material(
        self, content_factory
    ) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "Stale example",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "stale reflect",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "Build a login system with refresh-token clarity",
                "refined_constraints": ["Must use OAuth"],
                "ac_patches": [{"op": "keep", "index": 0, "reason": "still valid"}],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=content_factory(stale_payload, actual_payload),
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(1),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What handles token refresh?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_malformed_outer_object_with_nested_array_returns_error(self) -> None:
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content='{"ontology_mutations": [], }',
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
        engine = ReflectEngine(llm_adapter=adapter, model="test")

        result = await engine.reflect(
            current_seed=_seed(),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What remains unknown?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            '["incidental", "array"]',
            '{"ontology_mutations": [[["not a mutation object"]]], "reasoning": "r"}',
            '{"ontology_mutations": {"field_name": "token"}, "reasoning": "r"}',
            '{"ontology_mutations": [{"field_name": "token"}], "reasoning": "r"}',
            '{"ontology_mutations": [{"action": "add"}], "reasoning": "r"}',
            '{"ontology_mutations": [{"action": "add", "field_name": ""}], "reasoning": "r"}',
            '{"ontology_mutations": [{"action": "add", "field_name": ["not", "text"]}]}',
            '{"ac_patches": null, "ontology_mutations": []}',
            '{"ac_patches": {"0": {"op": "keep"}}, "ontology_mutations": []}',
            '{"ac_patches": "not-a-list", "ontology_mutations": []}',
            '{"refined_goal": ["not", "text"], "ontology_mutations": []}',
            '{"refined_goal": "   ", "ontology_mutations": []}',
            '{"refined_constraints": "not-a-list", "ontology_mutations": []}',
            '{"refined_acs": "single string is not a list", "ontology_mutations": []}',
            '{"refined_acs": {"0": "mapping is not a list"}, "ontology_mutations": []}',
            '{"refined_acs": [{"description": "object member"}], "ontology_mutations": []}',
            '{"refined_acs": ["valid", 7], "ontology_mutations": []}',
            (
                '{"ontology_mutations": ['
                '{"action": "add", "field_name": "empty_add", "description": " ", "reason": " "}'
                "]}"
            ),
        ],
    )
    async def test_bad_typed_shapes_return_result_error(self, content: str) -> None:
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=content,
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(),
            execution_output="",
            evaluation_summary=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                ac_results=(),
            ),
            wonder_output=WonderOutput(questions=("What remains unknown?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()


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

    @pytest.mark.parametrize(("fence_length", "fence_info"), LONG_FENCE_CASES)
    def test_long_supported_fence_wins_over_later_stale_json(
        self, fence_length: int, fence_info: str
    ) -> None:
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "actual assertion",
                }
            ]
        )
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale example",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_long_supported_fence(actual_payload, fence_length, fence_info, stale_payload),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    @pytest.mark.parametrize("fence_info", ["json", ""])
    def test_supported_fence_rejects_invalid_body_with_stale_nested_json(
        self, fence_info: str
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale example",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_invalid_supported_fence_with_nested_payload(fence_info, stale_payload),
            ("AC number 1",),
        )

        assert assertions == ()

    def test_unsupported_fence_pair_does_not_let_later_prose_example_win(self) -> None:
        example_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale example",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "actual assertion",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_after_unsupported_fence(example_payload, actual_payload),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    def test_unsupported_fence_body_is_excluded_from_prose_fallback(self) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale example",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "actual assertion",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_unsupported_fence_then_prose(stale_payload, actual_payload),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            lambda _stale, actual: _wrap_invalid_json_fence_then_prose(actual),
        ],
    )
    def test_malformed_fence_returns_empty_instead_of_accepting_stale_material(
        self, content_factory
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale example",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "actual assertion",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            content_factory(stale_payload, actual_payload),
            ("AC number 1",),
        )

        assert assertions == ()

    def test_incidental_non_object_array_is_ignored(self) -> None:
        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            "I considered AC indices [0, 1] before answering.",
            ("AC number 1", "AC number 2"),
        )

        assert assertions == ()

    @pytest.mark.parametrize(
        "payload",
        [
            [[{"ac_index": 0, "description": "nested object"}]],
            [{"tier": "t4_unverifiable", "description": "missing explicit index"}],
            [{"ac_index": 0, "description": ["not", "text"]}],
            [{"ac_index": 0, "pattern": ["not", "regex"], "expected_value": 10}],
            [{"ac_index": True, "description": "bool is not an index"}],
            [{"ac_index": 0, "tier": "not_a_tier", "description": "invalid tier"}],
            [{"ac_index": 0, "tier": "t1_constant", "expected_value": "10"}],
            [{"ac_index": 0, "tier": "t2_structural", "expected_value": "Widget"}],
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": "(",
                    "expected_value": "10",
                }
            ],
        ],
    )
    def test_bad_assertion_shapes_do_not_escape(self, payload: object) -> None:
        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            json.dumps(payload), ("AC number 1",)
        )

        assert assertions == ()
