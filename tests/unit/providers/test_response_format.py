"""Tests for the shared ``response_format`` directive builder and validator.

Per #1785 these lived as five independent copies. A schema-validity guard was
added to `zcode_cli_adapter` and never reached the other four, so a malformed
`json_schema` raised an uncaught `UnknownType` from them instead of returning a
message. `TestMalformedSchemaNoLongerCrashes` pins that per adapter.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.providers.response_format import (
    build_response_format_directive,
    validate_response_format_payload,
)

MALFORMED_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"schema": {"type": "not-a-real-type"}},
}


class TestBuildDirective:
    @pytest.mark.parametrize(
        "response_format",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty"),
            pytest.param({"type": "unrecognized"}, id="unknown-type"),
            pytest.param({"type": "json_schema"}, id="json-schema-missing"),
            pytest.param(
                {"type": "json_schema", "json_schema": "not-a-dict"}, id="schema-not-dict"
            ),
        ],
    )
    def test_nothing_to_instruct_returns_none(self, response_format: dict | None) -> None:
        assert build_response_format_directive(response_format) is None

    def test_json_object_directive(self) -> None:
        directive = build_response_format_directive({"type": "json_object"})
        assert directive is not None
        assert "ONLY a valid JSON object" in directive
        assert "markdown fences" in directive

    @pytest.mark.parametrize(
        ("schema", "expected_noun"),
        [
            pytest.param({"type": "object"}, "JSON object", id="object"),
            pytest.param({"type": "array"}, "JSON array", id="array"),
            pytest.param({"type": "string"}, "JSON value", id="other-type-falls-back"),
            pytest.param({}, "JSON object", id="missing-type-defaults-to-object"),
        ],
    )
    def test_type_noun_follows_the_schema(self, schema: dict, expected_noun: str) -> None:
        directive = build_response_format_directive({"type": "json_schema", "json_schema": schema})
        assert directive is not None
        assert f"ONLY a valid {expected_noun}" in directive

    def test_openai_style_wrapper_is_unwrapped(self) -> None:
        """`json_schema` may hold the schema directly or under a `schema` key."""
        wrapped = build_response_format_directive(
            {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "array"}}}
        )
        direct = build_response_format_directive(
            {"type": "json_schema", "json_schema": {"type": "array"}}
        )
        assert wrapped is not None and direct is not None
        assert "ONLY a valid JSON array" in wrapped
        assert "ONLY a valid JSON array" in direct
        assert '"name"' not in wrapped

    def test_unserializable_schema_falls_back_to_str(self) -> None:
        """The directive is advisory; an odd schema must not fail the request."""
        directive = build_response_format_directive(
            {"type": "json_schema", "json_schema": {"schema": {"a": {1, 2}}}}
        )
        assert directive is not None
        assert "JSON schema:" in directive

    def test_schema_is_rendered_deterministically(self) -> None:
        """Sorted keys keep the prompt stable across runs, which caching relies on."""
        schema = {
            "type": "object",
            "properties": {"b": {"type": "string"}, "a": {"type": "string"}},
        }
        directive = build_response_format_directive({"type": "json_schema", "json_schema": schema})
        assert directive is not None
        rendered = directive.split("JSON schema:\n", 1)[1]
        assert rendered == json.dumps(schema, indent=2, sort_keys=True)


class TestValidatePayload:
    def test_valid_object_passes(self) -> None:
        assert validate_response_format_payload('{"a": 1}', {"type": "json_object"}) is None

    def test_non_object_is_reported(self) -> None:
        assert validate_response_format_payload("[1]", {"type": "json_object"}) == (
            "expected a JSON object"
        )

    def test_unparseable_json_is_reported(self) -> None:
        reason = validate_response_format_payload("{not json", {"type": "json_object"})
        assert reason is not None
        assert reason.startswith("invalid JSON:")

    def test_schema_violation_is_reported(self) -> None:
        reason = validate_response_format_payload(
            '{"a": 1}',
            {"type": "json_schema", "json_schema": {"schema": {"type": "array"}}},
        )
        assert reason is not None
        assert "array" in reason

    def test_schema_match_passes(self) -> None:
        assert (
            validate_response_format_payload(
                '{"a": "x"}',
                {
                    "type": "json_schema",
                    "json_schema": {"schema": {"type": "object", "required": ["a"]}},
                },
            )
            is None
        )

    def test_missing_schema_object_is_reported(self) -> None:
        assert validate_response_format_payload(
            '{"a": 1}', {"type": "json_schema", "json_schema": "nope"}
        ) == ("json_schema response_format is missing a schema object")

    def test_malformed_schema_is_reported_not_raised(self) -> None:
        """The #1785 regression. `UnknownType` is not a `ValidationError`."""
        reason = validate_response_format_payload('{"a": 1}', MALFORMED_SCHEMA)
        assert reason is not None
        assert reason.startswith("invalid JSON schema:")

    def test_unrecognized_format_type_passes(self) -> None:
        assert validate_response_format_payload("{}", {"type": "something-else"}) is None


class TestMalformedSchemaNoLongerCrashes:
    """Each adapter that validates must report, not raise.

    Before #1785 only `zcode` survived this input; the other four propagated
    `jsonschema.exceptions.UnknownType` to the caller.
    """

    @staticmethod
    def _validate(module_name: str, class_name: str, payload: str, response_format: dict) -> object:
        """Call the adapter's validator, whether it is a staticmethod or bound.

        `ourocode` declares it `@staticmethod`; the rest take `self`. The point
        of this suite is the returned reason, not the binding, so normalize.
        """
        import importlib
        import inspect

        module = importlib.import_module(f"ouroboros.providers.{module_name}")
        cls = getattr(module, class_name)
        func = inspect.getattr_static(cls, "_validate_response_format_payload")
        if isinstance(func, staticmethod):
            return cls._validate_response_format_payload(payload, response_format)
        return cls._validate_response_format_payload(object(), payload, response_format)

    @pytest.mark.parametrize(
        ("module_name", "class_name"),
        [
            pytest.param("pi_llm_adapter", "PiLLMAdapter", id="pi"),
            pytest.param("omp_llm_adapter", "OmpLLMAdapter", id="omp"),
            pytest.param("gjc_llm_adapter", "GjcLLMAdapter", id="gjc"),
            pytest.param("ourocode_llm_adapter", "OurocodeLLMAdapter", id="ourocode"),
            pytest.param("goose_cli_adapter", "GooseCliLLMAdapter", id="goose"),
            pytest.param("zcode_cli_adapter", "ZcodeCliLLMAdapter", id="zcode"),
        ],
    )
    def test_adapter_reports_malformed_schema(self, module_name: str, class_name: str) -> None:
        reason = self._validate(module_name, class_name, '{"a": 1}', MALFORMED_SCHEMA)

        assert isinstance(reason, str)
        assert reason.startswith("invalid JSON schema:")


UNRESOLVABLE_REF_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"schema": {"$ref": "#/$defs/missing"}},
}


class TestUnresolvableReferenceIsReported:
    """A `$ref` pointing at nothing is structurally valid but cannot validate.

    `check_schema` passes, then `validate` raises `_WrappedReferencingError`,
    whose MRO is `_RefResolutionError -> referencing.exceptions.Unresolvable ->
    Exception`. It is neither a `SchemaError` nor a `ValidationError`, so it
    escaped both clauses -- including in the one copy that already had the
    `check_schema` guard.
    """

    def test_shared_validator_reports_instead_of_raising(self) -> None:
        reason = validate_response_format_payload('{"a": 1}', UNRESOLVABLE_REF_SCHEMA)
        assert reason is not None
        assert reason.startswith("could not validate against JSON schema:")

    def test_a_resolvable_ref_still_validates(self) -> None:
        """The guard must not swallow schemas whose `$ref` does resolve."""
        schema = {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "$defs": {"positive": {"type": "integer", "minimum": 1}},
                    "$ref": "#/$defs/positive",
                }
            },
        }
        assert validate_response_format_payload("5", schema) is None
        assert validate_response_format_payload("0", schema) is not None

    @pytest.mark.parametrize(
        ("module_name", "class_name"),
        [
            pytest.param("pi_llm_adapter", "PiLLMAdapter", id="pi"),
            pytest.param("omp_llm_adapter", "OmpLLMAdapter", id="omp"),
            pytest.param("gjc_llm_adapter", "GjcLLMAdapter", id="gjc"),
            pytest.param("ourocode_llm_adapter", "OurocodeLLMAdapter", id="ourocode"),
            pytest.param("goose_cli_adapter", "GooseCliLLMAdapter", id="goose"),
            pytest.param("zcode_cli_adapter", "ZcodeCliLLMAdapter", id="zcode"),
        ],
    )
    def test_adapter_reports_unresolvable_reference(
        self, module_name: str, class_name: str
    ) -> None:
        reason = TestMalformedSchemaNoLongerCrashes._validate(
            module_name, class_name, '{"a": 1}', UNRESOLVABLE_REF_SCHEMA
        )
        assert isinstance(reason, str)
        assert reason.startswith("could not validate against JSON schema:")
