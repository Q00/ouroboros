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
OVERSIZED_JSON_INTEGER = "9" * 5000


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
    if variant == "tilde_json_fence":
        return f"~~~json\n{payload}\n~~~"
    if variant == "tilde_bare_fence":
        return f"~~~\n{payload}\n~~~"
    if variant == "long_json_fence":
        return _wrap_long_supported_fence(payload, 4, "json")
    if variant == "long_bare_fence":
        return _wrap_long_supported_fence(payload, 4, "")
    if variant == "longer_json_fence":
        return _wrap_long_supported_fence(payload, 5, "json")
    if variant == "longer_bare_fence":
        return _wrap_long_supported_fence(payload, 5, "")
    if variant == "inline_before_prose_json":
        return f"Use ``` as prose before the answer: {payload}"
    if variant == "inline_before_indented_crlf_fence":
        return f"Use ``` inline\r\n  ````json\r\n{payload}\r\n  ````"
    if variant == "pretty_raw_spaces":
        return json.dumps(json.loads(payload), indent=4)
    if variant == "pretty_raw_tabs":
        return json.dumps(json.loads(payload), indent="\t")
    if variant == "anthropic_prefill_pretty_blanklines":
        pretty = json.dumps(json.loads(payload), indent=2).replace("\n", "\n \t\n", 1)
        return f"Let me carefully evaluate this response.\n  \n{pretty}\n\t\n"
    if variant == "anthropic_prefill_crlf_trailing_blanks":
        return f"I will analyze this response.\r\n  \r\n{payload}\r\n\t\r\n"
    if variant == "anthropic_prefill_ordinary_punctuation":
        return (
            "Let me review the user's \"goal\": it isn't ambiguous; "
            f"the `draft` [note] is prose.\n\n{payload}"
        )
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


def _wrap_unsupported_tilde_fence_then_prose(stale_payload: str, actual_payload: str) -> str:
    return (
        "Do not use this example:\n"
        "~~~python\n"
        f"EXAMPLE = {stale_payload}\n"
        "~~~\n"
        "Actual answer:\n"
        f"{actual_payload}"
    )


def _wrap_unclosed_unsupported_tilde_fence(stale_payload: str, actual_payload: str) -> str:
    return (
        "Do not use this example:\n"
        "~~~python\n"
        f"EXAMPLE = {stale_payload}\n"
        "The real answer appears later but the fence never closes:\n"
        f"{actual_payload}"
    )


def _wrap_invalid_json_fence_then_prose(actual_payload: str) -> str:
    return f"```json\n{{not json}}\n```\n{actual_payload}"


def _wrap_invalid_supported_fence_with_nested_payload(fence_info: str, nested_payload: str) -> str:
    return f"```{fence_info}\ninvalid wrapper {nested_payload}\n```"


MALFORMED_UNFENCED_WRAPPERS = [
    "Analysis: {draft: <payload>}",
    "Analysis: {draft: <payload>",
    "Analysis: {'draft': <payload>",
    "Analysis: {0: <payload>",
    "Analysis: {draft: stale <payload>",
    'Analysis: {"draft[key]": <payload>',
    "Analysis: {draft: '}', payload: <payload>}",
    "{'draft': stale\n\n<payload>",
    "{0: stale\n\n<payload>",
    "{Analysis: stale\n\n<payload>",
    '{Let me think.\n\n{"example": true}\n\n<payload>',
    "{Let me-json stale\n\n<payload>",
    "{Let me: stale\n\n<payload>",
    '{Let me"draft stale\n\n<payload>',
    "{I will analyze[draft] stale\n\n<payload>",
]


def _wrap_malformed_unfenced_payload(wrapper: str, payload: str) -> str:
    return wrapper.replace("<payload>", payload)


def _wrap_unfenced_example_then_actual(example_payload: str, actual_payload: str) -> str:
    return f"For example: {example_payload}\nActual answer: {actual_payload}"


def _wrap_indented_fence_example_then_actual(
    example_payload: str,
    actual_payload: str,
    *,
    indentation: str | tuple[str, str, str] = "    ",
) -> str:
    if isinstance(indentation, tuple):
        opener_indent, body_indent, closer_indent = indentation
    else:
        opener_indent = body_indent = closer_indent = indentation
    lines = f"```json\n{example_payload}\n```".splitlines()
    indented_example = "\n".join(
        f"{opener_indent if index == 0 else closer_indent if index == len(lines) - 1 else body_indent}{line}"
        for index, line in enumerate(lines)
    )
    return f"Example only:\n{indented_example}\nActual answer: {actual_payload}"


def _wrap_disconnected_indented_closer(
    example_payload: str, actual_payload: str, later_payload: str
) -> str:
    return (
        f"Example:\n    ```json\n    {example_payload}\n"
        f"Actual: {actual_payload}\n    ```\nLater stale: {later_payload}"
    )


def _wrap_indented_pseudo_closer(
    example_payload: str, nested_payload: str, actual_payload: str
) -> str:
    return (
        f"Example:\n    ```json\n    {example_payload}\n"
        f"        ```\n      {nested_payload}\n     ```\nActual: {actual_payload}"
    )


MALFORMED_INDENTED_FENCE_CASES = (
    "disconnected",
    "unclosed-eof",
    "wrong-marker",
    "dirty-closer",
    "shorter-closer",
)
BLOCKQUOTE_INDENTED_PREFIX_VARIANTS = (
    pytest.param((">     ", ">     ", " >     "), id="mixed-leading-one"),
    pytest.param((" >     ", ">     ", "  >     "), id="mixed-leading-two"),
    pytest.param(("  >     ", " >     ", "   >     "), id="mixed-leading-three"),
    pytest.param((">     ", " >      ", "  >       "), id="mixed-padding"),
    pytest.param((">   \t", "  > \t", ">   \t"), id="tab-padding"),
    pytest.param(("> >     ", " >  >     ", "  >>     "), id="nested-mixed"),
)
QUOTED_LIST_FENCE_VARIANTS = (
    pytest.param(("> - ", ">   ", ">   "), id="exact"),
    pytest.param((" > - ", "  >   ", "   >   "), id="mixed-leading"),
    pytest.param(("> - - ", ">     ", ">     "), id="nested-list"),
    pytest.param(("> 1. ", ">    ", ">    "), id="ordered"),
    pytest.param(("> > - ", "> >   ", " > >   "), id="nested-quote"),
    pytest.param(("> -  ", ">    ", ">    "), id="marker-padding"),
)
PLAIN_LIST_FENCE_VARIANTS = (
    pytest.param(("- ", "  "), id="unordered"),
    pytest.param(("1. ", "   "), id="ordered"),
    pytest.param(("- - ", "    "), id="nested"),
)


def _wrap_malformed_indented_fence(
    shape: str,
    stale_payload: str,
    *,
    quote_prefix: str = "",
    trailing: str = "",
) -> str:
    indent = "    "
    opener = "````json" if shape == "shorter-closer" else "```json"
    lines = [
        "Example:",
        f"{quote_prefix}{indent}{opener}",
        f"{quote_prefix}{indent}{stale_payload}",
    ]
    if shape == "disconnected":
        lines.extend([f"{quote_prefix}Outside block", f"{quote_prefix}{indent}```"])
    elif shape == "wrong-marker":
        lines.append(f"{quote_prefix}{indent}~~~")
    elif shape == "dirty-closer":
        lines.append(f"{quote_prefix}{indent}``` trailing")
    elif shape == "shorter-closer":
        lines.append(f"{quote_prefix}{indent}```")
    elif shape != "unclosed-eof":
        raise AssertionError(f"unknown malformed fence shape: {shape}")
    if trailing:
        lines.append(trailing)
    return "\n".join(lines)


def _wrap_blockquote_indented_example(
    stale_payload: str,
    *,
    prefixes: tuple[str, str, str],
    actual_payload: str = "",
) -> str:
    opener_prefix, body_prefix, closer_prefix = prefixes
    text = f"{opener_prefix}```json\n{body_prefix}{stale_payload}\n{closer_prefix}```"
    if actual_payload:
        text += f"\nActual: {actual_payload}"
    return text


def _wrap_quoted_list_fence_example(
    stale_payload: str,
    *,
    prefixes: tuple[str, str, str],
    actual_payload: str = "",
    opener: str = "```",
    closer: str | None = "```",
) -> str:
    opener_prefix, body_prefix, closer_prefix = prefixes
    text = f"{opener_prefix}{opener}json\n{body_prefix}{stale_payload}"
    if closer is not None:
        text += f"\n{closer_prefix}{closer}"
    if actual_payload:
        text += f"\nActual: {actual_payload}"
    return text


def _wrap_plain_list_fenced_answer(payload: str, *, prefixes: tuple[str, str]) -> str:
    opener_prefix, continuation = prefixes
    return f"{opener_prefix}```json\n{continuation}{payload}\n{continuation}```"


def _wrap_disconnected_quoted_list_closer(
    example_payload: str, actual_payload: str, later_payload: str
) -> str:
    return (
        f"> - ```json\n>   {example_payload}\nActual: {actual_payload}\n"
        f">   ```\nLater: {later_payload}"
    )


def _wrap_overpadded_tab_list_literal_example(stale_payload: str, actual_payload: str = "") -> str:
    text = f"-\t\t```json\n\t    {stale_payload}\n\t    ```"
    if actual_payload:
        text += f"\nActual: {actual_payload}"
    return text


def _wrap_leading_tab_container_literal_example(
    container: str, stale_payload: str, actual_payload: str = ""
) -> str:
    if container == "quote":
        text = f"\t> ```json\n\t> {stale_payload}\n\t> ```"
    elif container == "list":
        text = f"\t- ```json\n\t\t{stale_payload}\n\t\t```"
    else:
        raise AssertionError(f"unknown leading-tab container: {container}")
    if actual_payload:
        text += f"\nActual: {actual_payload}"
    return text


RAW_NON_ANSWER_CONTEXTS = ("code-span", "double-code-span", "long-code-span", "comment")
HTML_COMMENT_GRAMMAR_CASES = ("escaped", "short", "short-dash", "unclosed", "malformed")


def _wrap_raw_non_answer_context(context: str, stale_payload: str, actual_payload: str = "") -> str:
    if context == "code-span":
        text = f"Example: `{stale_payload}`"
    elif context == "double-code-span":
        text = f"Example: `` `{stale_payload}` ``"
    elif context == "long-code-span":
        text = f"Example: ```` ```{stale_payload}``` ````"
    elif context == "comment":
        text = f"<!-- Example only: {stale_payload} -->"
    else:
        raise AssertionError(f"unknown raw non-answer context: {context}")
    if actual_payload:
        text += f"\nActual: {actual_payload}"
    return text


def _wrap_html_comment_grammar_case(case: str, actual_payload: str, stale_payload: str) -> str:
    if case == "escaped":
        return f"\\<!-- {actual_payload} --> Later: {stale_payload}"
    if case == "short":
        return f"Example: <!--> Actual: {actual_payload}"
    if case == "short-dash":
        return f"Example: <!---> Actual: {actual_payload}"
    if case == "unclosed":
        return f"Example: <!-- {actual_payload}"
    if case == "malformed":
        return f"Example: <!-- ---> Actual: {actual_payload}"
    raise AssertionError(f"unknown HTML comment grammar case: {case}")


async def _reflect_wrapped_content(content: str):
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(
            content=content,
            model="test",
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
    )
    return await ReflectEngine(llm_adapter=adapter, model="test").reflect(
        current_seed=_seed(1),
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


# ``prose_prefix_fence`` and ``fence_trailing_prose`` are the two variants the
# old heuristic got wrong; the remaining variants preserve plain, bare, and
# longer supported-fence behavior.
FENCE_VARIANTS = [
    "prose_prefix_fence",
    "fence_trailing_prose",
    "bare_fence",
    "tilde_json_fence",
    "tilde_bare_fence",
    "long_json_fence",
    "long_bare_fence",
    "longer_json_fence",
    "longer_bare_fence",
    "inline_before_prose_json",
    "inline_before_indented_crlf_fence",
    "pretty_raw_spaces",
    "pretty_raw_tabs",
    "anthropic_prefill_pretty_blanklines",
    "anthropic_prefill_crlf_trailing_blanks",
    "anthropic_prefill_ordinary_punctuation",
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

    def test_unfenced_example_before_actual_answer_fails_closed(self) -> None:
        example_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale example"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_unfenced_example_then_actual(example_payload, actual_payload),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert out.questions == (
            "What assumptions remain untested for goal: Build a login system?",
        )

    @pytest.mark.parametrize(
        "indentation",
        ["    ", "\t", ("    ", "      ", "     ")],
        ids=["spaces", "tab", "varying-spaces"],
    )
    def test_indented_fence_example_cannot_override_unfenced_answer(
        self, indentation: str | tuple[str, str, str]
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale example"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual token question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual answer",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_indented_fence_example_then_actual(
                stale_payload, actual_payload, indentation=indentation
            ),
            _seed(),
        )

        assert out.reasoning == "actual answer"
        assert out.should_continue is True
        assert out.questions == ("actual token question",)

    def test_disconnected_indented_closer_fails_closed(self) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_disconnected_indented_closer(stale_payload, actual_payload, stale_payload),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")

    @pytest.mark.parametrize("shape", MALFORMED_INDENTED_FENCE_CASES)
    @pytest.mark.parametrize("quote_prefix", ["", "> "], ids=["plain", "blockquote"])
    def test_malformed_indented_fence_stale_only_fails_closed(
        self, shape: str, quote_prefix: str
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_malformed_indented_fence(
                shape,
                stale_payload,
                quote_prefix=quote_prefix,
            ),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")

    @pytest.mark.parametrize("prefixes", BLOCKQUOTE_INDENTED_PREFIX_VARIANTS)
    def test_blockquote_indented_example_is_excluded_and_releases_actual(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_blockquote_indented_example(stale_payload, prefixes=prefixes),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_blockquote_indented_example(
                stale_payload,
                prefixes=prefixes,
                actual_payload=actual_payload,
            ),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "actual"
        assert actual.questions == ("actual question",)

    @pytest.mark.parametrize("prefixes", QUOTED_LIST_FENCE_VARIANTS)
    def test_quoted_list_example_is_excluded_and_releases_actual(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_quoted_list_fence_example(stale_payload, prefixes=prefixes),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=prefixes,
                actual_payload=actual_payload,
            ),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "actual"
        assert actual.questions == ("actual question",)

    @pytest.mark.parametrize("prefixes", PLAIN_LIST_FENCE_VARIANTS)
    def test_plain_list_fenced_answer_is_authoritative(self, prefixes: tuple[str, str]) -> None:
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=prefixes),
            _seed(),
        )

        assert out.reasoning == "actual"
        assert out.questions == ("actual question",)

    @pytest.mark.parametrize(
        ("opener", "closer"),
        [("```", None), ("```", "~~~"), ("````", "```"), ("```", "``` trailing")],
        ids=["eof", "wrong-marker", "shorter", "dirty"],
    )
    def test_malformed_quoted_list_fence_fails_closed(
        self, opener: str, closer: str | None
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">   ", ">   "),
                actual_payload=actual_payload,
                opener=opener,
                closer=closer,
            ),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")

    def test_disconnected_quoted_list_closer_cannot_release_later_stale(self) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_disconnected_quoted_list_closer(
                stale_payload,
                actual_payload,
                stale_payload,
            ),
            _seed(),
        )

        assert out.reasoning.startswith("Parse error, using seed-scoped fallback")

    def test_tab_continued_plain_list_fence_is_authoritative(self) -> None:
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=("- ", "\t")),
            _seed(),
        )

        assert out.reasoning == "actual"
        assert out.questions == ("actual question",)

    def test_tab_continued_quoted_list_example_is_excluded_and_releases_actual(
        self,
    ) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
            ),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
                actual_payload=actual_payload,
            ),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "actual"

    def test_overpadded_tab_list_literal_example_releases_actual(self) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "STALE_WONDER"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual?", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "ACTUAL_WONDER",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_overpadded_tab_list_literal_example(stale_payload),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_overpadded_tab_list_literal_example(stale_payload, actual_payload),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "ACTUAL_WONDER"
        assert actual.questions == ("actual?",)

    @pytest.mark.parametrize("container", ["quote", "list"])
    def test_leading_tab_container_literal_example_releases_actual(self, container: str) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "STALE_WONDER"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual?", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "ACTUAL_WONDER",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_leading_tab_container_literal_example(container, stale_payload),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_leading_tab_container_literal_example(
                container,
                stale_payload,
                actual_payload,
            ),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "ACTUAL_WONDER"
        assert actual.questions == ("actual?",)

    @pytest.mark.parametrize("context", RAW_NON_ANSWER_CONTEXTS)
    def test_raw_non_answer_context_releases_actual(self, context: str) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "STALE_WONDER"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual?", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "ACTUAL_WONDER",
            }
        )
        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        stale_only = engine._parse_response(
            _wrap_raw_non_answer_context(context, stale_payload),
            _seed(),
        )
        actual = engine._parse_response(
            _wrap_raw_non_answer_context(context, stale_payload, actual_payload),
            _seed(),
        )

        assert stale_only.reasoning.startswith("Parse error, using seed-scoped fallback")
        assert actual.reasoning == "ACTUAL_WONDER"
        assert actual.questions == ("actual?",)

    @pytest.mark.parametrize("case", HTML_COMMENT_GRAMMAR_CASES)
    def test_html_comment_grammar_preserves_authoritative_boundaries(self, case: str) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "STALE_WONDER"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual?", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "ACTUAL_WONDER",
            }
        )

        result = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_html_comment_grammar_case(case, actual_payload, stale_payload),
            _seed(),
        )

        if case == "escaped":
            assert result.reasoning.startswith("Parse error, using seed-scoped fallback")
        else:
            assert result.reasoning == "ACTUAL_WONDER"
            assert result.questions == ("actual?",)

    def test_eight_space_pseudo_closer_does_not_release_nested_example(self) -> None:
        stale_payload = json.dumps(
            {"questions": [], "should_continue": False, "reasoning": "stale"}
        )
        actual_payload = json.dumps(
            {
                "questions": [{"question": "actual question", "kind": "gap"}],
                "should_continue": True,
                "reasoning": "actual",
            }
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(
            _wrap_indented_pseudo_closer(stale_payload, stale_payload, actual_payload),
            _seed(),
        )

        assert out.reasoning == "actual"
        assert out.questions == ("actual question",)

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

    @pytest.mark.parametrize(
        "content_factory",
        [_wrap_unsupported_fence_then_prose, _wrap_unsupported_tilde_fence_then_prose],
    )
    def test_unsupported_fence_body_is_excluded_from_prose_fallback(self, content_factory) -> None:
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

        assert out.reasoning == "actual answer"
        assert out.should_continue is True
        assert out.questions == ("actual token refresh question",)

    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            _wrap_unclosed_unsupported_tilde_fence,
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

    def test_oversized_json_integer_uses_seed_scoped_fallback(self) -> None:
        content = (
            '{"questions": [], "should_continue": true, "reasoning": "ignored", '
            f'"oversized": {OVERSIZED_JSON_INTEGER}}}'
        )

        out = WonderEngine(llm_adapter=AsyncMock(), model="test")._parse_response(content, _seed())

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

    @pytest.mark.parametrize("wrapper", MALFORMED_UNFENCED_WRAPPERS)
    def test_invalid_wrapper_with_nested_answer_uses_fallback(self, wrapper: str) -> None:
        content = _wrap_malformed_unfenced_payload(
            wrapper,
            '{"questions": [], "should_continue": false, "reasoning": "stale convergence"}',
        )

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
    @pytest.mark.parametrize(
        ("boundary_case", "expect_success"),
        [("disconnected", False), ("pseudo-closer", True)],
    )
    async def test_indented_fence_block_boundaries_are_structural(
        self, boundary_case: str, expect_success: bool
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
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        content = (
            _wrap_disconnected_indented_closer(stale_payload, actual_payload, stale_payload)
            if boundary_case == "disconnected"
            else _wrap_indented_pseudo_closer(stale_payload, stale_payload, actual_payload)
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=content,
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
            wonder_output=WonderOutput(questions=("What remains unknown?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        if expect_success:
            assert result.is_ok
            assert result.value.reasoning == "actual reflect"
        else:
            assert result.is_err
            assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", MALFORMED_INDENTED_FENCE_CASES)
    @pytest.mark.parametrize("quote_prefix", ["", "> "], ids=["plain", "blockquote"])
    async def test_malformed_indented_fence_stale_only_fails_closed(
        self, shape: str, quote_prefix: str
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
                content=_wrap_malformed_indented_fence(
                    shape,
                    stale_payload,
                    quote_prefix=quote_prefix,
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
            wonder_output=WonderOutput(questions=("What remains unknown?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_disconnected_quoted_list_closer_cannot_release_later_stale(
        self,
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
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        result = await _reflect_wrapped_content(
            _wrap_disconnected_quoted_list_closer(
                stale_payload,
                actual_payload,
                stale_payload,
            )
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_tab_continued_plain_list_fence_is_authoritative(self) -> None:
        actual_payload = json.dumps(
            {
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        result = await _reflect_wrapped_content(
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=("- ", "\t"))
        )

        assert result.is_ok
        assert result.value.reasoning == "actual reflect"

    @pytest.mark.asyncio
    async def test_tab_continued_quoted_list_example_is_excluded_and_releases_actual(
        self,
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
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        stale_only = await _reflect_wrapped_content(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
            )
        )
        actual = await _reflect_wrapped_content(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
                actual_payload=actual_payload,
            )
        )

        assert stale_only.is_err
        assert actual.is_ok
        assert actual.value.reasoning == "actual reflect"

    @pytest.mark.asyncio
    async def test_overpadded_tab_list_literal_example_releases_actual(self) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "STALE_GOAL",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "STALE_REFLECT",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "ACTUAL_GOAL",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "ACTUAL_REFLECT",
            }
        )

        stale_only = await _reflect_wrapped_content(
            _wrap_overpadded_tab_list_literal_example(stale_payload)
        )
        actual = await _reflect_wrapped_content(
            _wrap_overpadded_tab_list_literal_example(stale_payload, actual_payload)
        )

        assert stale_only.is_err
        assert actual.is_ok
        assert actual.value.reasoning == "ACTUAL_REFLECT"
        assert actual.value.refined_goal == "ACTUAL_GOAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("container", ["quote", "list"])
    async def test_leading_tab_container_literal_example_releases_actual(
        self, container: str
    ) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "STALE_GOAL",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "STALE_REFLECT",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "ACTUAL_GOAL",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "ACTUAL_REFLECT",
            }
        )

        stale_only = await _reflect_wrapped_content(
            _wrap_leading_tab_container_literal_example(container, stale_payload)
        )
        actual = await _reflect_wrapped_content(
            _wrap_leading_tab_container_literal_example(
                container,
                stale_payload,
                actual_payload,
            )
        )

        assert stale_only.is_err
        assert actual.is_ok
        assert actual.value.reasoning == "ACTUAL_REFLECT"
        assert actual.value.refined_goal == "ACTUAL_GOAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", RAW_NON_ANSWER_CONTEXTS)
    async def test_raw_non_answer_context_releases_actual(self, context: str) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "STALE_GOAL",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "STALE_REFLECT",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "ACTUAL_GOAL",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "ACTUAL_REFLECT",
            }
        )

        stale_only = await _reflect_wrapped_content(
            _wrap_raw_non_answer_context(context, stale_payload)
        )
        actual = await _reflect_wrapped_content(
            _wrap_raw_non_answer_context(context, stale_payload, actual_payload)
        )

        assert stale_only.is_err
        assert actual.is_ok
        assert actual.value.reasoning == "ACTUAL_REFLECT"
        assert actual.value.refined_goal == "ACTUAL_GOAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", HTML_COMMENT_GRAMMAR_CASES)
    async def test_html_comment_grammar_preserves_authoritative_boundaries(self, case: str) -> None:
        stale_payload = json.dumps(
            {
                "refined_goal": "STALE_GOAL",
                "refined_constraints": ["stale"],
                "ontology_mutations": [],
                "reasoning": "STALE_REFLECT",
            }
        )
        actual_payload = json.dumps(
            {
                "refined_goal": "ACTUAL_GOAL",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "ACTUAL_REFLECT",
            }
        )

        result = await _reflect_wrapped_content(
            _wrap_html_comment_grammar_case(case, actual_payload, stale_payload)
        )

        if case == "escaped":
            assert result.is_err
        else:
            assert result.is_ok
            assert result.value.reasoning == "ACTUAL_REFLECT"
            assert result.value.refined_goal == "ACTUAL_GOAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefixes", QUOTED_LIST_FENCE_VARIANTS)
    async def test_quoted_list_example_is_excluded_and_releases_actual(
        self, prefixes: tuple[str, str, str]
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
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        stale_only = await _reflect_wrapped_content(
            _wrap_quoted_list_fence_example(stale_payload, prefixes=prefixes)
        )
        actual = await _reflect_wrapped_content(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=prefixes,
                actual_payload=actual_payload,
            )
        )

        assert stale_only.is_err
        assert actual.is_ok
        assert actual.value.reasoning == "actual reflect"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefixes", PLAIN_LIST_FENCE_VARIANTS)
    async def test_plain_list_fenced_answer_is_authoritative(
        self, prefixes: tuple[str, str]
    ) -> None:
        actual_payload = json.dumps(
            {
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        result = await _reflect_wrapped_content(
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=prefixes)
        )

        assert result.is_ok
        assert result.value.reasoning == "actual reflect"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("opener", "closer"),
        [("```", None), ("```", "~~~"), ("````", "```"), ("```", "``` trailing")],
        ids=["eof", "wrong-marker", "shorter", "dirty"],
    )
    async def test_malformed_quoted_list_fence_fails_closed(
        self, opener: str, closer: str | None
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
                "refined_goal": "Actual goal",
                "refined_constraints": ["actual"],
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )

        result = await _reflect_wrapped_content(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">   ", ">   "),
                actual_payload=actual_payload,
                opener=opener,
                closer=closer,
            )
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefixes", BLOCKQUOTE_INDENTED_PREFIX_VARIANTS)
    async def test_blockquote_indented_example_stale_only_fails_closed(
        self, prefixes: tuple[str, str, str]
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
                content=_wrap_blockquote_indented_example(
                    stale_payload,
                    prefixes=prefixes,
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
            wonder_output=WonderOutput(questions=("What remains unknown?",)),
            lineage=OntologyLineage(lineage_id="lineage", goal="Build a login system"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

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
    async def test_unfenced_example_before_actual_answer_returns_parse_error(self) -> None:
        example_payload = json.dumps(
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
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_unfenced_example_then_actual(example_payload, actual_payload),
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
    @pytest.mark.parametrize(
        "indentation",
        ["    ", "\t", ("    ", "      ", "     ")],
        ids=["spaces", "tab", "varying-spaces"],
    )
    async def test_indented_fence_example_cannot_override_unfenced_answer(
        self, indentation: str | tuple[str, str, str]
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
                "ontology_mutations": [],
                "reasoning": "actual reflect",
            }
        )
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_indented_fence_example_then_actual(
                    stale_payload, actual_payload, indentation=indentation
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
    @pytest.mark.parametrize(
        "content_factory",
        [_wrap_unsupported_fence_then_prose, _wrap_unsupported_tilde_fence_then_prose],
    )
    async def test_unsupported_fence_body_is_excluded_from_prose_fallback(
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

        assert result.is_ok
        assert result.value.reasoning == "actual reflect"
        assert result.value.refined_goal == "Build a login system with refresh-token clarity"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            _wrap_unclosed_unsupported_tilde_fence,
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
    async def test_oversized_json_integer_returns_parse_error(self) -> None:
        content = (
            '{"refined_goal": "ignored", "refined_constraints": [], '
            '"ontology_mutations": [], "reasoning": "ignored", '
            f'"oversized": {OVERSIZED_JSON_INTEGER}}}'
        )
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
    @pytest.mark.parametrize("wrapper", MALFORMED_UNFENCED_WRAPPERS)
    async def test_invalid_wrapper_with_nested_answer_returns_error(self, wrapper: str) -> None:
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content=_wrap_malformed_unfenced_payload(
                    wrapper,
                    '{"refined_goal": "stale replacement", '
                    '"refined_constraints": ["stale constraint"], '
                    '"ontology_mutations": [], "reasoning": "stale"}',
                ),
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

    @pytest.mark.parametrize(
        ("boundary_case", "expected_description"),
        [("disconnected", None), ("pseudo-closer", "actual assertion")],
    )
    def test_indented_fence_block_boundaries_are_structural(
        self, boundary_case: str, expected_description: str | None
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
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
        content = (
            _wrap_disconnected_indented_closer(stale_payload, actual_payload, stale_payload)
            if boundary_case == "disconnected"
            else _wrap_indented_pseudo_closer(stale_payload, stale_payload, actual_payload)
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            content, ("AC number 1",)
        )

        if expected_description is None:
            assert assertions is None
        else:
            assert len(assertions) == 1
            assert assertions[0].description == expected_description

    @pytest.mark.parametrize("shape", MALFORMED_INDENTED_FENCE_CASES)
    @pytest.mark.parametrize("quote_prefix", ["", "> "], ids=["plain", "blockquote"])
    def test_malformed_indented_fence_stale_only_fails_closed(
        self, shape: str, quote_prefix: str
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_malformed_indented_fence(
                shape,
                stale_payload,
                quote_prefix=quote_prefix,
            ),
            ("AC number 1",),
        )

        assert assertions is None

    @pytest.mark.parametrize("prefixes", BLOCKQUOTE_INDENTED_PREFIX_VARIANTS)
    def test_blockquote_indented_example_stale_only_is_excluded(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
                }
            ]
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_blockquote_indented_example(stale_payload, prefixes=prefixes),
            ("AC number 1",),
        )

        assert assertions is None

    @pytest.mark.parametrize("prefixes", QUOTED_LIST_FENCE_VARIANTS)
    def test_quoted_list_example_is_excluded_and_releases_actual(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
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
        extractor = AssertionExtractor(llm_adapter=AsyncMock())

        stale_only = extractor._parse_response(
            _wrap_quoted_list_fence_example(stale_payload, prefixes=prefixes),
            ("AC number 1",),
        )
        actual = extractor._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=prefixes,
                actual_payload=actual_payload,
            ),
            ("AC number 1",),
        )

        assert stale_only is None
        assert len(actual) == 1
        assert actual[0].description == "actual assertion"

    @pytest.mark.parametrize("prefixes", PLAIN_LIST_FENCE_VARIANTS)
    def test_plain_list_fenced_answer_is_authoritative(self, prefixes: tuple[str, str]) -> None:
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
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=prefixes),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    @pytest.mark.parametrize(
        ("opener", "closer"),
        [("```", None), ("```", "~~~"), ("````", "```"), ("```", "``` trailing")],
        ids=["eof", "wrong-marker", "shorter", "dirty"],
    )
    def test_malformed_quoted_list_fence_fails_closed(
        self, opener: str, closer: str | None
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
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
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">   ", ">   "),
                actual_payload=actual_payload,
                opener=opener,
                closer=closer,
            ),
            ("AC number 1",),
        )

        assert assertions is None

    def test_disconnected_quoted_list_closer_cannot_release_later_stale(self) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
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
            _wrap_disconnected_quoted_list_closer(
                stale_payload,
                actual_payload,
                stale_payload,
            ),
            ("AC number 1",),
        )

        assert assertions is None

    def test_tab_continued_plain_list_fence_is_authoritative(self) -> None:
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
            _wrap_plain_list_fenced_answer(actual_payload, prefixes=("- ", "\t")),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    def test_tab_continued_quoted_list_example_is_excluded_and_releases_actual(
        self,
    ) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "stale assertion",
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
        extractor = AssertionExtractor(llm_adapter=AsyncMock())

        stale_only = extractor._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
            ),
            ("AC number 1",),
        )
        actual = extractor._parse_response(
            _wrap_quoted_list_fence_example(
                stale_payload,
                prefixes=("> - ", ">\t", ">\t"),
                actual_payload=actual_payload,
            ),
            ("AC number 1",),
        )

        assert stale_only is None
        assert len(actual) == 1
        assert actual[0].description == "actual assertion"

    def test_overpadded_tab_list_literal_example_releases_actual(self) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "STALE_ASSERTION",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "ACTUAL_ASSERTION",
                }
            ]
        )
        extractor = AssertionExtractor(llm_adapter=AsyncMock())

        stale_only = extractor._parse_response(
            _wrap_overpadded_tab_list_literal_example(stale_payload),
            ("AC number 1",),
        )
        actual = extractor._parse_response(
            _wrap_overpadded_tab_list_literal_example(stale_payload, actual_payload),
            ("AC number 1",),
        )

        assert stale_only is None
        assert len(actual) == 1
        assert actual[0].description == "ACTUAL_ASSERTION"

    @pytest.mark.parametrize("container", ["quote", "list"])
    def test_leading_tab_container_literal_example_releases_actual(self, container: str) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "STALE_ASSERTION",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "ACTUAL_ASSERTION",
                }
            ]
        )
        extractor = AssertionExtractor(llm_adapter=AsyncMock())

        stale_only = extractor._parse_response(
            _wrap_leading_tab_container_literal_example(container, stale_payload),
            ("AC number 1",),
        )
        actual = extractor._parse_response(
            _wrap_leading_tab_container_literal_example(
                container,
                stale_payload,
                actual_payload,
            ),
            ("AC number 1",),
        )

        assert stale_only is None
        assert len(actual) == 1
        assert actual[0].description == "ACTUAL_ASSERTION"

    @pytest.mark.parametrize("context", RAW_NON_ANSWER_CONTEXTS)
    def test_raw_non_answer_context_releases_actual(self, context: str) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "STALE_ASSERTION",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "ACTUAL_ASSERTION",
                }
            ]
        )
        extractor = AssertionExtractor(llm_adapter=AsyncMock())

        stale_only = extractor._parse_response(
            _wrap_raw_non_answer_context(context, stale_payload),
            ("AC number 1",),
        )
        actual = extractor._parse_response(
            _wrap_raw_non_answer_context(context, stale_payload, actual_payload),
            ("AC number 1",),
        )

        assert stale_only is None
        assert len(actual) == 1
        assert actual[0].description == "ACTUAL_ASSERTION"

    @pytest.mark.parametrize("case", HTML_COMMENT_GRAMMAR_CASES)
    def test_html_comment_grammar_preserves_authoritative_boundaries(self, case: str) -> None:
        stale_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "STALE_ASSERTION",
                }
            ]
        )
        actual_payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t4_unverifiable",
                    "pattern": "",
                    "description": "ACTUAL_ASSERTION",
                }
            ]
        )

        result = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            _wrap_html_comment_grammar_case(case, actual_payload, stale_payload),
            ("AC number 1",),
        )

        if case == "escaped":
            assert result is None
        else:
            assert len(result) == 1
            assert result[0].description == "ACTUAL_ASSERTION"

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

        assert assertions is None

    def test_unfenced_example_before_actual_answer_returns_empty(self) -> None:
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
            _wrap_unfenced_example_then_actual(example_payload, actual_payload),
            ("AC number 1",),
        )

        assert assertions is None

    @pytest.mark.parametrize(
        "indentation",
        ["    ", "\t", ("    ", "      ", "     ")],
        ids=["spaces", "tab", "varying-spaces"],
    )
    def test_indented_fence_example_cannot_override_unfenced_answer(
        self, indentation: str | tuple[str, str, str]
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
            _wrap_indented_fence_example_then_actual(
                stale_payload, actual_payload, indentation=indentation
            ),
            ("AC number 1",),
        )

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

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

    @pytest.mark.parametrize(
        "content_factory",
        [_wrap_unsupported_fence_then_prose, _wrap_unsupported_tilde_fence_then_prose],
    )
    def test_unsupported_fence_body_is_excluded_from_prose_fallback(self, content_factory) -> None:
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

        assert len(assertions) == 1
        assert assertions[0].description == "actual assertion"

    @pytest.mark.parametrize(
        "content_factory",
        [
            _wrap_unclosed_unsupported_fence,
            _wrap_unclosed_unsupported_tilde_fence,
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

        assert assertions is None

    def test_oversized_json_integer_returns_empty(self) -> None:
        content = (
            '[{"ac_index": 0, "tier": "t4_unverifiable", '
            '"description": "ignored", '
            f'"oversized": {OVERSIZED_JSON_INTEGER}}}]'
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            content, ("AC number 1",)
        )

        assert assertions is None

    def test_incidental_non_object_array_is_ignored(self) -> None:
        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            "I considered AC indices [0, 1] before answering.",
            ("AC number 1", "AC number 2"),
        )

        assert assertions is None

    @pytest.mark.parametrize("wrapper", MALFORMED_UNFENCED_WRAPPERS)
    def test_invalid_wrapper_does_not_create_nested_assertion(self, wrapper: str) -> None:
        content = _wrap_malformed_unfenced_payload(
            wrapper,
            '[{"ac_index": 0, "tier": "t4_unverifiable", "description": "stale assertion"}]',
        )

        assertions = AssertionExtractor(llm_adapter=AsyncMock())._parse_response(
            content, ("AC number 1",)
        )

        assert assertions is None

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

        assert assertions is None
