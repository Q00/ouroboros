"""Tests for extract_json_payload — the shared JSON extractor."""

import json

import pytest

from ouroboros.evaluation.json_utils import extract_json_payload

LONG_FENCE_CASES = [(4, "json"), (4, ""), (5, "json"), (5, "")]
MALFORMED_INDENTED_FENCE_CASES = (
    "disconnected",
    "unclosed-eof",
    "wrong-marker",
    "dirty-closer",
    "shorter-closer",
)
BLOCKQUOTE_INDENTED_PREFIX_VARIANTS = (
    pytest.param(">     ", ">     ", " >     ", id="mixed-leading-one"),
    pytest.param(" >     ", ">     ", "  >     ", id="mixed-leading-two"),
    pytest.param("  >     ", " >     ", "   >     ", id="mixed-leading-three"),
    pytest.param(">     ", " >      ", "  >       ", id="mixed-padding"),
    pytest.param(">   \t", "  > \t", ">   \t", id="tab-padding"),
    pytest.param("> >     ", " >  >     ", "  >>     ", id="nested-mixed"),
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
    pytest.param("- ", "  ", id="unordered"),
    pytest.param("1. ", "   ", id="ordered"),
    pytest.param("- - ", "    ", id="nested"),
)


def _malformed_indented_fence(
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


class TestExtractJsonPayload:
    """extract_json_payload must find the first *valid* JSON object."""

    def test_pure_json(self):
        text = '{"score": 0.85, "verdict": "pass"}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.85' in result

    @pytest.mark.parametrize(
        ("payload", "indent"),
        [
            ({"outer": {"inner": 42}, "items": [1, 2]}, 4),
            ([{"assertion": {"verified": True}}], "\t"),
        ],
        ids=["pretty-object-spaces", "pretty-array-tabs"],
    )
    def test_pretty_raw_json_preserves_nested_indentation(
        self, payload: object, indent: int | str
    ) -> None:
        text = json.dumps(payload, indent=indent)

        assert extract_json_payload(text) == text

    @pytest.mark.parametrize("indentation", ["    ", "\t"], ids=["spaces", "tab"])
    def test_indented_literal_fence_is_excluded_without_rewriting_pretty_answer(
        self, indentation: str
    ) -> None:
        stale = json.dumps({"stale": {"nested": True}}, indent=4)
        indented_example = "\n".join(
            f"{indentation}{line}" for line in f"```json\n{stale}\n```".splitlines()
        )
        actual = json.dumps({"actual": {"nested": True}}, indent=4)
        text = f"Example only:\n{indented_example}\nActual answer:\n{actual}"

        assert extract_json_payload(text) == actual

    def test_indented_literal_fence_allows_varying_legal_line_indentation(self) -> None:
        stale = '{"stale": true}'
        actual = '{"actual": true}'
        text = f"Example only:\n    ```json\n      {stale}\n     ```\nActual answer: {actual}"

        assert extract_json_payload(text) == actual

    def test_indented_literal_fence_cannot_close_across_unindented_content(self) -> None:
        text = (
            'Example:\n    ```json\n    {"example": true}\n'
            'Actual: {"actual": true}\n    ```\nLater stale: {"stale": true}'
        )

        assert extract_json_payload(text) is None

    def test_eight_space_pseudo_closer_remains_inside_indented_block(self) -> None:
        text = (
            'Example:\n    ```json\n    {"example": true}\n'
            '        ```\n      {"nested_stale": true}\n     ```\n'
            'Actual: {"actual": true}'
        )

        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize("shape", MALFORMED_INDENTED_FENCE_CASES)
    @pytest.mark.parametrize("quote_prefix", ["", "> "], ids=["plain", "blockquote"])
    def test_malformed_indented_literal_fence_stale_only_fails_closed(
        self, shape: str, quote_prefix: str
    ) -> None:
        text = _malformed_indented_fence(
            shape,
            '{"stale": true}',
            quote_prefix=quote_prefix,
        )

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize("shape", MALFORMED_INDENTED_FENCE_CASES)
    @pytest.mark.parametrize("quote_prefix", ["", "> "], ids=["plain", "blockquote"])
    @pytest.mark.parametrize(
        "trailing",
        [
            'Actual: {"actual": true}',
            'Actual: {"actual": true}\nLater: {"later": true}',
        ],
        ids=["actual", "actual-and-later"],
    )
    def test_malformed_indented_literal_fence_owns_later_payloads(
        self, shape: str, quote_prefix: str, trailing: str
    ) -> None:
        text = _malformed_indented_fence(
            shape,
            '{"stale": true}',
            quote_prefix=quote_prefix,
            trailing=trailing,
        )

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize(
        ("opener_prefix", "body_prefix", "closer_prefix"),
        BLOCKQUOTE_INDENTED_PREFIX_VARIANTS,
    )
    def test_blockquote_nested_indented_example_releases_real_payload(
        self, opener_prefix: str, body_prefix: str, closer_prefix: str
    ) -> None:
        text = (
            f'{opener_prefix}```json\n{body_prefix}{{"stale": true}}\n{closer_prefix}```\n'
            'Actual: {"actual": true}'
        )

        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(
        ("opener_prefix", "body_prefix", "closer_prefix"),
        BLOCKQUOTE_INDENTED_PREFIX_VARIANTS,
    )
    def test_blockquote_nested_indented_example_stale_only_is_excluded(
        self, opener_prefix: str, body_prefix: str, closer_prefix: str
    ) -> None:
        text = f'{opener_prefix}```json\n{body_prefix}{{"stale": true}}\n{closer_prefix}```'

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize("prefixes", QUOTED_LIST_FENCE_VARIANTS)
    def test_quoted_list_fence_example_stale_only_is_excluded(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        opener_prefix, body_prefix, closer_prefix = prefixes
        text = f'{opener_prefix}```json\n{body_prefix}{{"stale": true}}\n{closer_prefix}```'

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize("prefixes", QUOTED_LIST_FENCE_VARIANTS)
    def test_quoted_list_fence_example_releases_actual_payload(
        self, prefixes: tuple[str, str, str]
    ) -> None:
        opener_prefix, body_prefix, closer_prefix = prefixes
        text = (
            f'{opener_prefix}```json\n{body_prefix}{{"stale": true}}\n'
            f'{closer_prefix}```\nActual: {{"actual": true}}'
        )

        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(("opener_prefix", "continuation"), PLAIN_LIST_FENCE_VARIANTS)
    def test_plain_list_fenced_answer_is_authoritative(
        self, opener_prefix: str, continuation: str
    ) -> None:
        text = f'{opener_prefix}```json\n{continuation}{{"actual": true}}\n{continuation}```'

        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(
        ("opener", "closer"),
        [
            ("```", None),
            ("```", "~~~"),
            ("````", "```"),
            ("```", "``` trailing"),
        ],
        ids=["eof", "wrong-marker", "shorter", "dirty"],
    )
    def test_malformed_quoted_list_fence_owns_stale_and_later_payloads(
        self, opener: str, closer: str | None
    ) -> None:
        text = f'> - {opener}json\n>   {{"stale": true}}'
        if closer is not None:
            text += f"\n>   {closer}"
        text += '\nActual: {"actual": true}'

        assert extract_json_payload(text) is None

    def test_disconnected_quoted_list_closer_cannot_release_later_stale(self) -> None:
        text = (
            'Example:\n> - ```json\n>   {"example": true}\n'
            'Actual: {"actual": true}\n>   ```\nLater: {"stale": true}'
        )

        assert extract_json_payload(text) is None

    def test_tab_continued_plain_list_fence_is_authoritative(self) -> None:
        text = '- ```json\n\t{"actual": true}\n\t```'

        assert extract_json_payload(text) == '{"actual": true}'

    def test_tab_continued_quoted_list_example_is_excluded_and_releases_actual(
        self,
    ) -> None:
        stale_only = '> - ```json\n>\t{"stale": true}\n>\t```'
        with_actual = f'{stale_only}\nActual: {{"actual": true}}'

        assert extract_json_payload(stale_only) is None
        assert extract_json_payload(with_actual) == '{"actual": true}'

    def test_overpadded_tab_list_is_literal_example_and_releases_actual(self) -> None:
        stale_only = '-\t\t```json\n\t    {"stale": true}\n\t    ```'
        with_actual = f'{stale_only}\nActual: {{"actual": true}}'

        assert extract_json_payload(stale_only) is None
        assert extract_json_payload(with_actual) == '{"actual": true}'

    @pytest.mark.parametrize(
        "literal_example",
        [
            '\t> ```json\n\t> {"stale": true}\n\t> ```',
            '\t- ```json\n\t\t{"stale": true}\n\t\t```',
        ],
        ids=["leading-tab-quote", "leading-tab-list"],
    )
    def test_leading_tab_container_is_literal_example_and_releases_actual(
        self, literal_example: str
    ) -> None:
        with_actual = f'{literal_example}\nActual: {{"actual": true}}'

        assert extract_json_payload(literal_example) is None
        assert extract_json_payload(with_actual) == '{"actual": true}'

    def test_json_in_code_fence(self):
        text = '```json\n{"score": 0.85}\n```'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.85' in result

    @pytest.mark.parametrize("fence_info", ["json", ""])
    def test_json_in_tilde_code_fence(self, fence_info: str) -> None:
        text = f'~~~{fence_info}\n{{"score": 0.85}}\n~~~'
        assert extract_json_payload(text) == '{"score": 0.85}'

    def test_json_fence_with_literal_backticks_after_brace_in_string(self):
        text = (
            '```json\n{"message": "literal }``` marker", "questions": ["keep outer object"]}\n```'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert result.startswith("{")
        assert '"message": "literal }``` marker"' in result
        assert '"questions": ["keep outer object"]' in result

    @pytest.mark.parametrize(("fence_length", "fence_info"), LONG_FENCE_CASES)
    def test_long_supported_fence_parses_json_and_bare_payloads(
        self, fence_length: int, fence_info: str
    ) -> None:
        delimiter = "`" * fence_length
        text = f'{delimiter}{fence_info}\n{{"actual": true}}\n{delimiter}'
        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(("fence_length", "fence_info"), LONG_FENCE_CASES)
    def test_long_supported_fence_wins_over_later_prose_json(
        self, fence_length: int, fence_info: str
    ) -> None:
        delimiter = "`" * fence_length
        text = f'{delimiter}{fence_info}\n{{"actual": true}}\n{delimiter}\nLater: {{"stale": true}}'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_long_fence_allows_embedded_triple_backticks_in_json_string(self) -> None:
        text = '````json\n{"marker": "```", "actual": true}\n````\nLater: {"stale": true}'
        assert extract_json_payload(text) == '{"marker": "```", "actual": true}'

    def test_long_fence_accepts_longer_clean_closer(self) -> None:
        text = '````json\n{"actual": true}\n`````'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_long_fence_rejects_closer_with_trailing_text(self) -> None:
        text = '````json\n{"actual": true}\n```` trailing\nLater: {"stale": true}'
        assert extract_json_payload(text) is None

    def test_inline_backticks_do_not_suppress_prose_json_fallback(self) -> None:
        text = 'Use ``` as prose, then {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(
        "non_answer",
        [
            'Example: `{"stale": true}`',
            'Example: `` `{"stale": true}` ``',
            'Example: ```` ```{"stale": true}``` ````',
            '<!-- {"stale": true} -->',
            '<!-- multiline\n{"stale": true}\nexample -->',
        ],
        ids=["code-span", "double-code-span", "long-code-span", "comment", "multiline-comment"],
    )
    def test_raw_non_answer_context_is_excluded_and_releases_actual(self, non_answer: str) -> None:
        with_actual = f'{non_answer}\nActual: {{"actual": true}}'

        assert extract_json_payload(non_answer) is None
        assert extract_json_payload(with_actual) == '{"actual": true}'

    def test_escaped_html_comment_opener_keeps_both_payloads_ambiguous(self) -> None:
        text = r'\<!-- {"actual": true} --> Later: {"stale": true}'

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize("comment", ["<!-->", "<!--->"])
    def test_short_html_comment_releases_actual(self, comment: str) -> None:
        assert (
            extract_json_payload(f'Example: {comment} Actual: {{"actual": true}}')
            == '{"actual": true}'
        )

    def test_unclosed_html_comment_opener_remains_ordinary_text(self) -> None:
        assert extract_json_payload('Example: <!-- {"actual": true}') == '{"actual": true}'

    def test_malformed_html_comment_remains_ordinary_text(self) -> None:
        assert (
            extract_json_payload('Example: <!-- ---> Actual: {"actual": true}')
            == '{"actual": true}'
        )

    def test_inline_tildes_do_not_suppress_prose_json_fallback(self) -> None:
        text = 'Use ~~~ as prose, then {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_inline_backticks_do_not_hide_later_indented_crlf_fence(self) -> None:
        text = (
            'Use ``` inline\r\n  ````json\r\n{"actual": true}\r\n  ````\r\nLater: {"stale": true}'
        )
        assert extract_json_payload(text) == '{"actual": true}'

    def test_prose_before_json(self):
        """Generic raw prose may contain braces before one JSON answer."""
        text = (
            "I will analyze this artifact carefully.\n\n"
            "The {complexity} is moderate.\n\n"
            '{"score": 0.90, "verdict": "pass"}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.90' in result

    def test_prose_with_curly_braces_before_json(self):
        """Stray braces in prose should be skipped."""
        text = (
            "Let me evaluate the {artifact} quality.\n"
            "Based on {criteria} analysis:\n\n"
            '{"score": 0.75, "verdict": "revise", "reasoning": "needs work"}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.75' in result

    def test_nested_json(self):
        text = '{"outer": {"inner": 42}, "key": "value"}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"inner": 42' in result

    def test_escaped_braces_in_strings(self):
        text = '{"msg": "use \\"{key}\\" syntax", "ok": true}'
        result = extract_json_payload(text)
        assert result is not None

    def test_no_json(self):
        text = "This is plain text with no JSON at all."
        assert extract_json_payload(text) is None

    def test_unbalanced_braces(self):
        text = '{"key": "value"'
        assert extract_json_payload(text) is None

    def test_empty_object(self):
        text = "prefix {} suffix"
        result = extract_json_payload(text)
        assert result == "{}"

    def test_anthropic_prefill_happy(self):
        """Anthropic prefill success: response is just continuation."""
        text = '{"score": 0.84, "verdict": "revise", "dimensions": {"correctness": 0.85}}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.84' in result

    def test_raw_provider_prose_before_json(self):
        """A provider-preserved prose response exposes its unique JSON answer."""
        text = (
            "Let me carefully evaluate this document.\n\n"
            "Based on the quality bar provided:\n\n"
            '{"score": 0.88, "verdict": "pass", '
            '"dimensions": {"correctness": 0.90, "completeness": 0.85}}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.88' in result

    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_raw_prose_preserves_pretty_json_blank_lines_and_trailing_space(
        self, line_ending: str
    ) -> None:
        payload = line_ending.join(
            [
                "{",
                '  "outer": {',
                '    "inner": true',
                "  },",
                " \t",
                '  "items": [1, 2]',
                "}",
            ]
        )
        text = (
            f"Let me carefully evaluate this response.{line_ending}  {line_ending}"
            f"{payload}{line_ending}\t{line_ending}"
        )

        assert extract_json_payload(text) == payload

    def test_raw_prose_rejects_multiple_paragraph_payloads(self) -> None:
        text = 'Let me compare the answers.\n\n{"a": 1}\n\n{"b": 2}'

        assert extract_json_payload(text) is None

    def test_raw_prose_allows_ordinary_punctuation(self) -> None:
        text = (
            "Let me review the user's \"goal\": it isn't ambiguous; "
            "the `draft` [note] is prose.\n  \n"
            '{"actual": true}'
        )

        assert extract_json_payload(text) == '{"actual": true}'

    @pytest.mark.parametrize(
        "prefix",
        ["{Let me-json", "{Let me:", '{Let me"draft', "{I will analyze[draft]"],
        ids=["hyphen", "colon", "quote", "mixed-bracket"],
    )
    def test_unbalanced_phrase_lookalike_fails_closed(self, prefix: str) -> None:
        assert extract_json_payload(f'{prefix} stale\n\n{{"stale": true}}') is None

    def test_multiple_unfenced_json_objects_fail_closed(self):
        text = 'prefix {"a": 1} middle {"b": 2} suffix'
        assert extract_json_payload(text) is None

    @pytest.mark.parametrize("prefix", ["> ", ">> "])
    def test_blockquoted_json_fence_strips_exact_quote_prefix(self, prefix: str) -> None:
        text = (
            f'{prefix}```json\n{prefix}{{"questions": [], "should_continue": false}}\n{prefix}```'
        )
        assert extract_json_payload(text) == '{"questions": [], "should_continue": false}'

    def test_blockquoted_json_fence_rejects_unquoted_body_line(self) -> None:
        text = '> ```json\n{"actual": true}\n> ```\nLater: {"stale": true}'
        assert extract_json_payload(text) is None

    def test_blockquoted_json_fence_requires_matching_closer_prefix(self) -> None:
        text = '> ```json\n> {"actual": true}\n>> ```\nLater: {"stale": true}'
        assert extract_json_payload(text) is None

    def test_multiple_supported_json_fences_fail_closed(self) -> None:
        text = '```json\n{"example": true}\n```\n```json\n{"actual": true}\n```'
        assert extract_json_payload(text) is None

    def test_invalid_json_with_valid_later(self):
        """First brace-balanced block is not valid JSON, second is."""
        text = '{not json at all} {"valid": true}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"valid": true' in result

    @pytest.mark.parametrize(
        "nested_payload",
        [
            '{"questions": [], "should_continue": false}',
            '[{"ac_index": 0, "tier": "t4_unverifiable"}]',
        ],
        ids=["nested-object", "nested-array"],
    )
    def test_balanced_invalid_wrapper_does_not_promote_nested_payload(
        self, nested_payload: str
    ) -> None:
        text = f"Analysis: {{draft: {nested_payload}}}"

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize(
        "nested_payload",
        [
            '{"questions": [], "should_continue": false}',
            '[{"ac_index": 0, "tier": "t4_unverifiable"}]',
        ],
        ids=["nested-object", "nested-array"],
    )
    def test_unclosed_invalid_wrapper_does_not_promote_nested_payload(
        self, nested_payload: str
    ) -> None:
        text = f"Analysis: {{draft: {nested_payload}"

        assert extract_json_payload(text) is None

    @pytest.mark.parametrize(
        "wrapper",
        [
            "Analysis: {'draft': <payload>",
            "Analysis: {0: <payload>",
            "Analysis: {draft: stale <payload>",
            'Analysis: {"draft[key]": <payload>',
            "Analysis: {draft: '}', payload: <payload>}",
        ],
        ids=[
            "single-quoted-key",
            "numeric-key",
            "prose-before-payload",
            "delimiter-in-quoted-key",
            "single-quoted-early-closer",
        ],
    )
    def test_malformed_structured_opener_does_not_promote_nested_payload(
        self, wrapper: str
    ) -> None:
        text = wrapper.replace("<payload>", '{"stale": true}')

        assert extract_json_payload(text) is None

    def test_unclosed_human_heading_is_not_anthropic_prefill(self) -> None:
        assert extract_json_payload('{Analysis: {"actual": true}') is None

    @pytest.mark.parametrize(
        "prefix",
        ["{'draft': stale", "{0: stale", "{Analysis: stale"],
        ids=["single-quoted-key", "numeric-key", "arbitrary-heading"],
    )
    def test_blank_line_does_not_turn_wrapper_into_anthropic_prefill(self, prefix: str) -> None:
        text = f'{prefix}\n\n{{"stale": true}}'

        assert extract_json_payload(text) is None

    def test_balanced_invalid_wrapper_preserves_later_independent_payload(self) -> None:
        text = 'Analysis: {draft: {"stale": true}} Actual: {"valid": true}'

        assert extract_json_payload(text) == '{"valid": true}'

    def test_unclosed_unsupported_fence_fails_closed(self):
        text = '```python\nEXAMPLE = {"stale": true}\n{"actual": true}'
        assert extract_json_payload(text) is None

    def test_invalid_supported_fence_fails_closed_instead_of_later_prose_json(self):
        text = '```json\n{not json}\n```\n{"actual": true}'
        assert extract_json_payload(text) is None

    @pytest.mark.parametrize(
        ("fence_info", "nested_payload"),
        [("json", '{"stale": true}'), ("", '[{"stale": true}]')],
    )
    def test_supported_fence_rejects_invalid_body_with_nested_json(
        self, fence_info: str, nested_payload: str
    ) -> None:
        text = f"```{fence_info}\ninvalid wrapper {nested_payload}\n```"
        assert extract_json_payload(text) is None

    def test_supported_fallback_excludes_well_formed_unsupported_fence_body(self):
        text = '```python\nEXAMPLE = {"stale": true}\n```\nActual: {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_fallback_excludes_blockquoted_unsupported_fence_body(self) -> None:
        text = '> ```python\n> EXAMPLE = {"stale": true}\n> ```\nActual: {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_fallback_excludes_well_formed_unsupported_tilde_fence_body(self) -> None:
        text = '~~~python\nEXAMPLE = {"stale": true}\n~~~\nActual: {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'

    def test_unclosed_unsupported_tilde_fence_fails_closed(self) -> None:
        text = '~~~python\nEXAMPLE = {"stale": true}\nActual: {"actual": true}'
        assert extract_json_payload(text) is None

    def test_fallback_excludes_multiple_well_formed_unsupported_fence_bodies(self):
        text = (
            '```python\nEXAMPLE = {"stale": 1}\n```\n'
            '```yaml\nstale: {"stale": 2}\n```\n'
            'Actual: {"actual": true}'
        )
        assert extract_json_payload(text) == '{"actual": true}'

    def test_long_unsupported_fence_body_is_excluded_from_fallback(self) -> None:
        text = '````python\n{"stale": true}\n`````\nActual: {"actual": true}'
        assert extract_json_payload(text) == '{"actual": true}'


class TestExtractJsonArray:
    """extract_json_payload must also handle top-level JSON arrays."""

    def test_pure_array(self):
        text = '[{"a": 1}, {"b": 2}]'
        result = extract_json_payload(text)
        assert result is not None
        assert '"a": 1' in result
        assert result.startswith("[")

    def test_array_in_code_fence(self):
        text = '```json\n[{"item": "value"}]\n```'
        result = extract_json_payload(text)
        assert result is not None
        assert result.startswith("[")

    def test_prose_before_array(self):
        text = 'Here are the results:\n[{"score": 0.9}, {"score": 0.8}]'
        result = extract_json_payload(text)
        assert result is not None
        assert result.startswith("[")
        assert '"score": 0.9' in result

    def test_object_before_array_is_ambiguous(self):
        text = '{"first": true} [1, 2, 3]'
        assert extract_json_payload(text) is None

    def test_array_before_object_is_ambiguous(self):
        text = '[1, 2] {"second": true}'
        assert extract_json_payload(text) is None

    def test_empty_array(self):
        text = "prefix [] suffix"
        result = extract_json_payload(text)
        assert result == "[]"

    def test_nested_array(self):
        text = "[[1, 2], [3, 4]]"
        result = extract_json_payload(text)
        assert result is not None
        assert result == "[[1, 2], [3, 4]]"
