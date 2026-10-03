"""Tests for ouroboros.orchestrator.evidence_schema (RFC v2 #830, PR 2)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.evidence_schema import (
    EvidenceError,
    EvidenceRecord,
    ProfileEvidenceConfigError,
    extract_evidence,
    validate_evidence,
)
from ouroboros.orchestrator.profile_loader import load_profile


@pytest.fixture
def code_profile():
    return load_profile("code")


@pytest.fixture
def research_profile():
    return load_profile("research")


@pytest.fixture
def analysis_profile():
    return load_profile("analysis")


class TestExtractEvidence:
    def test_bare_json_object(self) -> None:
        record = extract_evidence('{"files_touched": ["a.py"]}')
        assert record.data == {"files_touched": ["a.py"]}

    def test_fenced_json_block(self) -> None:
        text = 'summary line\n```json\n{"x": 1}\n```\ntrailing\n'
        record = extract_evidence(text)
        assert record.data == {"x": 1}

    def test_prefers_json_evidence_fence_after_non_json_code_fence(self) -> None:
        text = (
            "Implemented `hello.py`:\n\n"
            "```python\n"
            "def hello():\n"
            '    return "hello"\n'
            "```\n\n"
            "Validation evidence:\n\n"
            "```json\n"
            "{\n"
            '  "files_touched": ["hello.py", "test_hello.py"],\n'
            '  "commands_run": ["pytest test_hello.py"],\n'
            '  "tests_passed": ["test_hello.py::test_hello"]\n'
            "}\n"
            "```\n"
        )

        record = extract_evidence(text)

        assert record.data == {
            "files_touched": ["hello.py", "test_hello.py"],
            "commands_run": ["pytest test_hello.py"],
            "tests_passed": ["test_hello.py::test_hello"],
        }

    def test_ignores_json_fence_literal_inside_earlier_code_block(self) -> None:
        text = (
            "Implemented markdown emitter:\n\n"
            "```python\n"
            "def example():\n"
            '    return "```json"\n'
            "```\n\n"
            "Validation evidence:\n\n"
            "```json\n"
            "{\n"
            '  "files_touched": ["emitter.py"],\n'
            '  "commands_run": ["pytest tests/test_emitter.py"],\n'
            '  "tests_passed": ["tests/test_emitter.py::test_example"]\n'
            "}\n"
            "```\n"
        )

        record = extract_evidence(text)

        assert record.data == {
            "files_touched": ["emitter.py"],
            "commands_run": ["pytest tests/test_emitter.py"],
            "tests_passed": ["tests/test_emitter.py::test_example"],
        }

    def test_matches_closing_fence_length_before_later_json_evidence(self) -> None:
        text = (
            "Documented an embedded markdown example:\n\n"
            "````markdown\n"
            "Example evidence shape:\n"
            "```json\n"
            '{"not": "top-level evidence"}\n'
            "```\n"
            "````\n\n"
            "Validation evidence:\n\n"
            "```json\n"
            "{\n"
            '  "files_touched": ["docs/example.md"],\n'
            '  "commands_run": ["pytest tests/test_docs.py"],\n'
            '  "tests_passed": ["tests/test_docs.py::test_example"]\n'
            "}\n"
            "```\n"
        )

        record = extract_evidence(text)

        assert record.data == {
            "files_touched": ["docs/example.md"],
            "commands_run": ["pytest tests/test_docs.py"],
            "tests_passed": ["tests/test_docs.py::test_example"],
        }

    def test_accepts_crlf_closing_fence_before_later_json_evidence(self) -> None:
        text = (
            "Implemented Windows output:\r\n\r\n"
            "```python\r\n"
            "print('hello')\r\n"
            "```\r\n\r\n"
            "Validation evidence:\r\n\r\n"
            "```json\r\n"
            "{\r\n"
            '  "files_touched": ["windows.py"],\r\n'
            '  "commands_run": ["pytest tests/test_windows.py"],\r\n'
            '  "tests_passed": ["tests/test_windows.py::test_example"]\r\n'
            "}\r\n"
            "```\r\n"
        )

        record = extract_evidence(text)

        assert record.data == {
            "files_touched": ["windows.py"],
            "commands_run": ["pytest tests/test_windows.py"],
            "tests_passed": ["tests/test_windows.py::test_example"],
        }

    def test_fenced_block_without_lang_tag(self) -> None:
        record = extract_evidence('prelude\n```\n{"y": 2}\n```\n')
        assert record.data == {"y": 2}

    def test_bare_non_json_fence_still_rejected_without_later_json_fence(self) -> None:
        text = 'summary\n```python\ndef hello():\n    return "hello"\n```\n'

        with pytest.raises(
            EvidenceError,
            match="Leaf output contains no JSON object and no fenced evidence block",
        ):
            extract_evidence(text)

    def test_json_shaped_content_in_non_json_fence_is_not_evidence(self) -> None:
        text = 'summary\n```python\n{"files_touched": ["example.py"]}\n```\n'

        with pytest.raises(
            EvidenceError,
            match="Leaf output contains no JSON object and no fenced evidence block",
        ):
            extract_evidence(text)

    @pytest.mark.parametrize("code_fence_first", [True, False])
    def test_non_json_fence_cannot_displace_real_evidence(self, code_fence_first: bool) -> None:
        code_fence = '```python\n{"files_touched": ["example.py"]}\n```'
        evidence = '{"files_touched": ["actual.py"], "tests_passed": ["test_actual"]}'
        text = f"{code_fence}\n{evidence}" if code_fence_first else f"{evidence}\n{code_fence}"

        record = extract_evidence(text)

        assert record.data["files_touched"] == ["actual.py"]

    @pytest.mark.parametrize(
        "example",
        [
            '> {"files_touched": ["quoted-example.py"]}\n',
            'Example:\n\n    {"files_touched": ["indented-example.py"]}\n',
            '~~~python\n{"files_touched": ["tilde-example.py"]}\n~~~\n',
        ],
    )
    def test_markdown_examples_are_not_recovered_as_evidence(self, example: str) -> None:
        with pytest.raises(
            EvidenceError,
            match="Leaf output contains no JSON object and no fenced evidence block",
        ):
            extract_evidence(example)

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="empty"):
            extract_evidence("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="empty"):
            extract_evidence("   \n\t  ")

    def test_malformed_json(self) -> None:
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence("{not: json}")

    def test_prose_before_json_fallback_recovered(self) -> None:
        """Models running on smaller tiers (adaptive mode) sometimes emit
        prose markers like ``[AC_COMPLETE: 6]`` or a summary paragraph
        *before* the final evidence JSON block. The extractor must skip
        that prose and still parse the JSON."""
        text = (
            "[AC_COMPLETE: 6]\n"
            "All tests pass, graceful failure handling confirmed.\n\n"
            '{"files_touched": ["src/graceful.ts"],'
            ' "commands_run": ["npx jest"],'
            ' "tests_passed": ["graceful.test.ts::test_basic"]}\n'
        )
        record = extract_evidence(text)
        assert record.data == {
            "files_touched": ["src/graceful.ts"],
            "commands_run": ["npx jest"],
            "tests_passed": ["graceful.test.ts::test_basic"],
        }

    @pytest.mark.parametrize(
        "trailing_prose",
        [
            "[AC_COMPLETE: 1]",
            "[done]",
            "[status: done]",
            "[ac_complete: 1]",
            "See [issue #1] for context.",
            "Configuration remains at {config.host}.",
            "Configuration remains at { config.host }.",
            "All requested work is complete.",
        ],
    )
    def test_prose_after_recovered_evidence_is_ignored(self, trailing_prose: str) -> None:
        text = (
            "Summary before evidence.\n"
            '{"files_touched": ["main.py"], "tests_passed": ["test_main"]}\n'
            f"{trailing_prose}\n"
        )

        record = extract_evidence(text)

        assert record.data["files_touched"] == ["main.py"]

    def test_unfenced_json_after_prose_only_brace_fallback(self) -> None:
        """Fallback should also work when there's no fence at all, just
        prose before a bare JSON object starting with ``{``."""
        text = (
            "All done. Here's the evidence:\n"
            '{"files_touched": ["a.ts"], "commands_run": ["npm test"], '
            '"tests_passed": ["a.test.ts"]}\n'
        )
        record = extract_evidence(text)
        assert record.data["files_touched"] == ["a.ts"]

    def test_non_json_brace_before_evidence_still_recovered(self) -> None:
        """A stray non-JSON ``{`` in the prose (e.g. a code snippet) must
        not stop the fallback from finding the real evidence object later."""
        text = (
            "Applied patch to {config.host} placeholder.\n"
            '{"files_touched": ["b.ts"], "commands_run": ["npm test"], '
            '"tests_passed": ["b.test.ts"]}\n'
        )
        record = extract_evidence(text)
        assert record.data["files_touched"] == ["b.ts"]

    def test_list_payload_not_rescued_by_inner_object(self) -> None:
        """A top-level list parses successfully at the trusted position, so
        its inner objects must never be adopted as evidence."""
        with pytest.raises(EvidenceError, match="must be a JSON object"):
            extract_evidence('[{"files_touched": ["a.ts"]}]')

    def test_non_object_payload(self) -> None:
        with pytest.raises(EvidenceError, match="must be a JSON object"):
            extract_evidence("[1, 2, 3]")

    def test_prose_prefixed_list_does_not_leak_inner_object(self) -> None:
        """Recovery must not extract an object nested inside a top-level list.

        Regression: `Summary\n[{"files_touched":["wrong.py"]}]` previously
        caused recovery to accept the inner object, contradicting the
        requirement that list payloads cannot be rescued.
        """
        text = 'Summary\n[{"files_touched": ["wrong.py"]}]'
        with pytest.raises(EvidenceError):
            extract_evidence(text)

    @pytest.mark.parametrize("earlier_form", ["bare", "fenced"])
    @pytest.mark.parametrize(
        "terminal_payload",
        [
            '[{"files_touched": ["terminal.py"]}]',
            "42",
            '"terminal evidence"',
            "null",
        ],
    )
    def test_terminal_non_object_displaces_earlier_valid_object(
        self, earlier_form: str, terminal_payload: str
    ) -> None:
        """The final complete JSON value owns the evidence boundary.

        Regression: recovery considered only objects, allowing an earlier
        schema-valid record to remain authoritative when the terminal payload
        was a prohibited list or scalar.
        """
        earlier = '{"files_touched": ["stale.py"]}'
        if earlier_form == "fenced":
            earlier = f"```json\n{earlier}\n```"
        text = f"Summary\n{earlier}\nFinal evidence:\n{terminal_payload}\n"

        with pytest.raises(EvidenceError, match="must be a JSON object"):
            extract_evidence(text)

    @pytest.mark.parametrize(
        "terminal_payload",
        [
            '[{"files_touched": ["terminal.py"]}]',
            "{broken",
        ],
    )
    def test_trusted_object_cannot_bypass_terminal_authority(self, terminal_payload: str) -> None:
        text = f'{{"files_touched": ["stale.py"]}}\nValidation evidence: {terminal_payload}'

        with pytest.raises(EvidenceError):
            extract_evidence(text)

    @pytest.mark.parametrize("fence_tag", ["json", ""])
    @pytest.mark.parametrize(
        "terminal_payload",
        [
            '[{"files_touched": ["terminal.py"]}]',
            "{broken",
        ],
    )
    def test_fenced_object_cannot_bypass_terminal_payload(
        self, fence_tag: str, terminal_payload: str
    ) -> None:
        text = f'```{fence_tag}\n{{"files_touched": ["stale.py"]}}\n{terminal_payload}\n```\n'

        with pytest.raises(EvidenceError):
            extract_evidence(text)

    def test_valid_inline_evidence_label_is_recovered(self) -> None:
        record = extract_evidence('Actual evidence: {"files_touched": ["actual.py"]}')

        assert record.data["files_touched"] == ["actual.py"]

    def test_inline_evidence_label_displaces_stale_object(self) -> None:
        record = extract_evidence(
            '{"files_touched": ["stale.py"]}\nActual evidence: {"files_touched": ["actual.py"]}'
        )

        assert record.data["files_touched"] == ["actual.py"]

    @pytest.mark.parametrize(
        "label",
        [
            "evidence:",
            "actual evidence:",
            "validation evidence:",
            "evidence follows:",
            "actual evidence follows:",
            "validation evidence follows:",
        ],
    )
    @pytest.mark.parametrize("terminal_payload", ["null", '"invalid"', "true", "17"])
    def test_inline_scalar_evidence_label_displaces_stale_object(
        self,
        label: str,
        terminal_payload: str,
    ) -> None:
        text = (
            '{"files_touched":["stale.py"],"commands_run":["pytest"],"tests_passed":["x"]}\n'
            f"{label} {terminal_payload}"
        )

        with pytest.raises(EvidenceError, match="must be a JSON object"):
            extract_evidence(text)

    @pytest.mark.parametrize(
        "terminal_payload",
        [
            "[undefined]",
            "[broken,",
            "[tru,",
            "[null,",
            "[{'not': 'json'}]",
        ],
    )
    def test_malformed_inline_array_label_displaces_stale_object(
        self,
        terminal_payload: str,
    ) -> None:
        text = (
            '{"files_touched":["stale.py"],"commands_run":["pytest"],"tests_passed":["x"]}\n'
            f"Actual evidence: {terminal_payload}"
        )

        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence(text)

    def test_earlier_illustrative_object_does_not_displace_final(self) -> None:
        """Recovery must prefer the terminal evidence object over earlier ones.

        Regression: prose containing an earlier valid illustrative object
        previously caused that object to be returned instead of the later
        final evidence record.
        """
        text = (
            'For example: {"illustrative": true}\n'
            "Here is the actual result:\n"
            '{"files_touched": ["main.py"], "pass": true}'
        )
        record = extract_evidence(text)
        assert record.data["files_touched"] == ["main.py"]
        assert record.data["pass"] is True
        assert "illustrative" not in record.data

    @pytest.mark.parametrize("example_tag", ["json", ""])
    @pytest.mark.parametrize("actual_form", ["bare", "json_fence", "untagged_fence"])
    def test_illustrative_fence_cannot_displace_later_evidence(
        self, example_tag: str, actual_form: str
    ) -> None:
        example = f'```{example_tag}\n{{"illustrative": true}}\n```'
        payload = '{"files_touched": ["real.py"], "tests_passed": ["test_real"]}'
        if actual_form == "bare":
            actual = payload
        elif actual_form == "json_fence":
            actual = f"```json\n{payload}\n```"
        else:
            actual = f"```\n{payload}\n```"
        text = f"Example:\n{example}\nActual evidence:\n{actual}\n"

        record = extract_evidence(text)

        assert record.data == {
            "files_touched": ["real.py"],
            "tests_passed": ["test_real"],
        }

    def test_quoted_brace_inside_string_value(self) -> None:
        # Regression: old regex stopped at the first `}` even inside a
        # JSON string value, truncating the payload (bot finding #1 on
        # PR #883). The fence-aware extractor must keep the entire body
        # and let json.loads handle string escaping.
        payload = '{"note": "hello } still inside string", "ok": true}'
        record = extract_evidence(payload)
        assert record.data == {
            "note": "hello } still inside string",
            "ok": True,
        }

    def test_quoted_backticks_inside_string_value(self) -> None:
        text = '```json\n{"note": "embedded `single backtick` survives", "ok": true}\n```\n'
        record = extract_evidence(text)
        assert record.data["ok"] is True
        assert "single backtick" in record.data["note"]

    def test_uppercase_json_fence_tag(self) -> None:
        record = extract_evidence('```JSON\n{"x": 1}\n```\n')
        assert record.data == {"x": 1}

    def test_triple_backtick_inside_json_string_value(self) -> None:
        # Regression: an earlier fence-scanner stopped at the first raw
        # ``` after the opener, even when it sat inside a quoted JSON
        # string value (bot finding on PR #883 round 2). The JSON-aware
        # raw_decode must let `json.JSONDecoder` decide where the value
        # ends.
        text = '```json\n{"note": "embedded ``` triple-backtick survives", "ok": true}\n```\n'
        record = extract_evidence(text)
        assert record.data["ok"] is True
        assert "triple-backtick" in record.data["note"]

    def test_extra_text_after_object_is_ignored(self) -> None:
        # raw_decode stops at the end of the first JSON value, so trailing
        # narrative text inside the fence does not corrupt parsing.
        text = '```json\n{"x": 1}\nsome trailing prose\n```\n'
        record = extract_evidence(text)
        assert record.data == {"x": 1}

    def test_nested_object_in_prose_prefixed_final_record(self) -> None:
        """Recovery must return the complete top-level object, not an inner
        nested object.

        Regression: reverse scanning without nesting awareness encountered
        the innermost ``{`` first and returned ``{"source": "final"}``
        instead of the complete enclosing record.
        """
        text = 'Summary\n{"files_touched": ["a.py"], "metadata": {"source": "final"}}'
        record = extract_evidence(text)
        assert record.data == {
            "files_touched": ["a.py"],
            "metadata": {"source": "final"},
        }

    def test_multi_element_list_does_not_leak_any_inner_object(self) -> None:
        """Recovery must not extract any object from a multi-element top-level
        list, regardless of element position.

        Regression: the backward comma scan in _is_inside_array stopped at
        the first element's ``{`` before reaching the containing ``[``, so
        the second element was accepted as evidence.
        """
        text = 'Summary\n[{"a": 1}, {"files_touched": ["wrong.py"]}]'
        with pytest.raises(EvidenceError):
            extract_evidence(text)

    def test_malformed_fenced_json_reports_malformed_not_absent(self) -> None:
        """A fenced block containing invalid JSON must report 'not valid JSON',
        not 'no JSON object'.

        Regression: error classification used only ``text.find("{")`` so
        malformed JSON without a brace was reported as having no evidence.
        """
        text = "```json\nnot valid json at all\n```\n"
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence(text)

    def test_malformed_bracket_payload_reports_malformed_not_absent(self) -> None:
        """Prose followed by a broken array ``[1, 2,]`` must report malformed,
        not absent.

        Regression: presence of ``{`` was the only signal; a broken array
        without braces was classified as 'no JSON object'.
        """
        text = "Summary\n[1, 2,]"
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence(text)

    def test_deeply_nested_object_not_extracted(self) -> None:
        """An object nested multiple levels deep must not be extracted."""
        text = 'Summary\n{"outer": {"middle": {"deep": true}}, "files_touched": ["x.py"]}'
        record = extract_evidence(text)
        # Must return the full outer object, not {"deep": true} or {"middle": ...}
        assert record.data["outer"] == {"middle": {"deep": True}}
        assert record.data["files_touched"] == ["x.py"]

    def test_malformed_final_evidence_fence_fails_closed(self) -> None:
        """A malformed JSON body inside the final evidence fence must fail.

        Regression (round2): when the authoritative fence contained invalid
        JSON wrapping a valid inner object, recovery rescued the inner object.
        The fence is the strongest structural boundary and must own its body.
        """
        text = (
            "Validation evidence:\n\n"
            "```json\n"
            '{invalid_wrapper: {"files_touched": ["rescued.py"]}}\n'
            "```\n"
        )
        with pytest.raises(EvidenceError, match="fence is malformed"):
            extract_evidence(text)

    def test_malformed_fence_with_earlier_valid_object_fails_closed(self) -> None:
        """Earlier illustrative objects cannot override a malformed final fence.

        Regression (round2): when an earlier valid object existed in prose
        before a malformed evidence fence, recovery adopted the earlier object
        instead of failing closed on the authoritative fence.
        """
        text = (
            'Earlier example: {"illustrative": true}\n\n'
            "Validation evidence:\n\n"
            "```json\n"
            "{not valid json at all\n"
            "```\n"
        )
        with pytest.raises(EvidenceError, match="fence is malformed"):
            extract_evidence(text)

    def test_malformed_untagged_fence_fails_closed(self) -> None:
        """An untagged fence (```) with malformed body also has fence authority.

        Regression (round2): only json-tagged fences were treated as
        authoritative; untagged fences allowed recovery to bypass them.
        """
        text = 'Earlier: {"illustrative": true}\n\n```\n{broken json\n```\n'
        with pytest.raises(EvidenceError, match="fence is malformed"):
            extract_evidence(text)

    def test_malformed_array_does_not_yield_inner_candidates(self) -> None:
        """A malformed outer array cannot yield valid inner objects.

        Regression (round2): `[{"valid": true}, {broken` allowed recovery
        to rescue `{"valid": true}` because the malformed outer boundary
        was absent from the containment check.
        """
        text = 'Summary\n[{"files_touched": ["wrong.py"]}, {broken'
        with pytest.raises(EvidenceError):
            extract_evidence(text)

    def test_malformed_object_does_not_yield_inner_candidates(self) -> None:
        """A malformed outer object cannot yield valid inner objects.

        Regression (round2): `{wrapper: {"valid": true}` (no closer) allowed
        recovery to rescue the inner object.
        """
        text = 'Summary\n{wrapper: {"files_touched": ["wrong.py"]}'
        with pytest.raises(EvidenceError):
            extract_evidence(text)

    @pytest.mark.parametrize(
        "text",
        [
            'Summary\n{foo-bar:\n{"files_touched": ["wrong.py"]}\n}',
            'Summary\n{1invalid:\n{"files_touched": ["wrong.py"]}\n}',
            'Summary\n[tru,\n{"files_touched": ["wrong.py"]}\n]',
            'Summary\n[undefined,\n{"files_touched": ["wrong.py"]}\n]',
        ],
    )
    def test_uncommon_malformed_container_tokens_cannot_expose_inner_object(
        self, text: str
    ) -> None:
        """Containment must not depend on recognizing the invalid first token."""
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence(text)

    @pytest.mark.parametrize(
        "text",
        [
            'Actual evidence: [broken,\n{"files_touched": ["rescued.py"]}\n]',
            'Earlier: {"files_touched": ["stale.py"]}\nActual evidence: {broken',
        ],
    )
    def test_inline_malformed_evidence_label_is_authoritative(self, text: str) -> None:
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence(text)

    def test_earlier_example_cannot_override_malformed_final_evidence(self) -> None:
        """When the final structural evidence is malformed, earlier objects
        in prose must not become authoritative.

        Regression (round2): the last-object preference in recovery meant
        the earlier object was returned when the final one was malformed.
        """
        text = (
            'Example output: {"illustrative": true}\n'
            "Actual evidence follows:\n"
            '{broken: {"files_touched": ["wrong.py"]}}'
        )
        with pytest.raises(EvidenceError):
            extract_evidence(text)


class TestValidateCodeProfile:
    def test_accepts_complete_record(self, code_profile) -> None:
        record = EvidenceRecord(
            data={
                "files_touched": ["src/a.py"],
                "commands_run": ["pytest"],
                "tests_passed": ["test_a"],
            }
        )
        result = validate_evidence(code_profile, record)
        assert result.ok is True
        assert result.missing_fields == ()
        assert result.rejected_by == ()

    def test_rejects_empty_tests_passed(self, code_profile) -> None:
        record = EvidenceRecord(
            data={
                "files_touched": ["src/a.py"],
                "commands_run": ["pytest"],
                "tests_passed": [],
            }
        )
        result = validate_evidence(code_profile, record)
        assert result.ok is False
        assert result.rejected_by == ("tests_passed == []",)
        assert result.missing_fields == ()

    def test_reports_missing_fields(self, code_profile) -> None:
        record = EvidenceRecord(data={"files_touched": ["a.py"]})
        result = validate_evidence(code_profile, record)
        assert result.ok is False
        assert "commands_run" in result.missing_fields
        assert "tests_passed" in result.missing_fields

    def test_reasons_summarize_failures(self, code_profile) -> None:
        record = EvidenceRecord(data={"tests_passed": []})
        result = validate_evidence(code_profile, record)
        reasons = result.reasons()
        assert any("missing required fields" in r for r in reasons)
        assert any("tests_passed == []" in r for r in reasons)


class TestValidateResearchProfile:
    def test_accepts_triangulated(self, research_profile) -> None:
        record = EvidenceRecord(
            data={
                "external_sources": ["https://example.com/a"],
                "claims": [{"text": "x", "source": 0}],
                "triangulated_sources": ["https://example.com/a", "https://example.com/b"],
            }
        )
        result = validate_evidence(research_profile, record)
        assert result.ok is True

    def test_rejects_no_external_sources(self, research_profile) -> None:
        record = EvidenceRecord(
            data={
                "external_sources": [],
                "claims": [],
                "triangulated_sources": [],
            }
        )
        result = validate_evidence(research_profile, record)
        assert result.ok is False
        assert "external_sources == []" in result.rejected_by
        assert "triangulated_sources == []" in result.rejected_by


class TestValidateAnalysisProfile:
    def test_accepts_perspectives(self, analysis_profile) -> None:
        record = EvidenceRecord(
            data={
                "claims": [{"text": "x"}],
                "perspectives_compared": ["pro", "con"],
            }
        )
        result = validate_evidence(analysis_profile, record)
        assert result.ok is True

    def test_rejects_one_sided(self, analysis_profile) -> None:
        record = EvidenceRecord(
            data={
                "claims": [{"text": "x"}],
                "perspectives_compared": [],
            }
        )
        result = validate_evidence(analysis_profile, record)
        assert result.ok is False
        assert result.rejected_by == ("perspectives_compared == []",)


class TestRejectionGrammar:
    """rejected_if grammar is intentionally narrow; bad expressions must surface."""

    def test_unsupported_expression_raises(
        self, code_profile, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        broken = code_profile.model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("len(tests_passed) < 1",),
                )
            }
        )
        record = EvidenceRecord(data={"tests_passed": [1]})
        with pytest.raises(ProfileEvidenceConfigError, match="Unsupported rejected_if"):
            validate_evidence(broken, record)

    def test_unsupported_literal_raises(self, code_profile) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        broken = code_profile.model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("tests_passed == os.system",),
                )
            }
        )
        record = EvidenceRecord(data={"tests_passed": []})
        with pytest.raises(ProfileEvidenceConfigError, match="Unsupported literal"):
            validate_evidence(broken, record)

    def test_missing_field_compared_to_none_triggers(self) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        profile = load_profile("code").model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("never_emitted == None",),
                )
            }
        )
        record = EvidenceRecord(data={})
        result = validate_evidence(profile, record)
        assert result.ok is False
        assert result.rejected_by == ("never_emitted == None",)


class TestJsonYamlLiteralSpellings:
    """rejected_if must accept literals YAML / JSON authors write.

    Profiles are YAML-authored and evidence is JSON, so authors reach
    for `null`, `true`, `false` — not Python's `None`/`True`/`False`.
    Both spellings must work (bot finding #2 on PR #883).
    """

    def _profile_with_rule(self, rule: str):
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        return load_profile("code").model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=(rule,),
                )
            }
        )

    def test_json_null_literal(self) -> None:
        profile = self._profile_with_rule("flag == null")
        record = EvidenceRecord(data={"flag": None})
        result = validate_evidence(profile, record)
        assert result.ok is False
        assert result.rejected_by == ("flag == null",)

    def test_json_true_literal(self) -> None:
        profile = self._profile_with_rule("flag == true")
        record = EvidenceRecord(data={"flag": True})
        assert validate_evidence(profile, record).ok is False

    def test_json_false_literal(self) -> None:
        profile = self._profile_with_rule("flag == false")
        record = EvidenceRecord(data={"flag": False})
        assert validate_evidence(profile, record).ok is False

    def test_python_spellings_still_work(self) -> None:
        # Backwards-compat with the legacy Python literal spellings.
        for rule, value in (
            ("flag == None", None),
            ("flag == True", True),
            ("flag == False", False),
        ):
            profile = self._profile_with_rule(rule)
            record = EvidenceRecord(data={"flag": value})
            assert validate_evidence(profile, record).ok is False, (
                f"{rule!r} did not trigger for {value!r}"
            )

    def test_json_number_literal(self) -> None:
        profile = self._profile_with_rule("count == 0")
        record = EvidenceRecord(data={"count": 0})
        assert validate_evidence(profile, record).ok is False

    def test_json_string_literal(self) -> None:
        profile = self._profile_with_rule('status == "blocked"')
        record = EvidenceRecord(data={"status": "blocked"})
        assert validate_evidence(profile, record).ok is False


class TestBlockedEvidence:
    def test_blocked_record_is_typed_not_missing_evidence(self, code_profile) -> None:
        record = EvidenceRecord(
            data={
                "status": "blocked",
                "blocker": {
                    "code": "MISSING_TOOL",
                    "reason": "pytest is not installed in the execution image",
                    "required_by": "AC-1 test verification",
                },
            }
        )
        result = validate_evidence(code_profile, record)
        assert result.ok is False
        assert result.missing_fields == ()
        assert result.rejected_by == ()
        assert result.blocker is not None
        assert result.blocker.code.value == "MISSING_TOOL"
        assert result.reasons() == (
            "blocked[MISSING_TOOL]: pytest is not installed in the execution image "
            "(required_by: AC-1 test verification)",
        )

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({"status": "blocked", "blocker": "nope"}, "blocker must be an object"),
            ({"status": "blocked", "blocker": {"reason": "x"}}, "blocker.code"),
            (
                {"status": "blocked", "blocker": {"code": "MYSTERY", "reason": "x"}},
                "Unknown blocker.code",
            ),
            (
                {"status": "blocked", "blocker": {"code": "MISSING_TOOL", "reason": ""}},
                "blocker.reason",
            ),
        ],
    )
    def test_malformed_blocked_record_is_schema_error(
        self, code_profile, payload, message: str
    ) -> None:
        with pytest.raises(EvidenceError, match=message):
            validate_evidence(code_profile, EvidenceRecord(data=payload))

    def test_blocked_record_still_surfaces_malformed_profile_rule(self, code_profile) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        broken = code_profile.model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("len(tests_passed) < 1",),
                )
            }
        )
        record = EvidenceRecord(
            data={
                "status": "blocked",
                "blocker": {
                    "code": "MISSING_TOOL",
                    "reason": "pytest is not installed",
                },
            }
        )

        with pytest.raises(ProfileEvidenceConfigError, match="Unsupported rejected_if"):
            validate_evidence(broken, record)
