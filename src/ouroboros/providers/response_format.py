"""Shared OpenAI-style ``response_format`` handling for provider adapters.

Backends without a native JSON-schema flag enforce structured output
cooperatively: the request carries a prompt directive, and the response is
validated after extraction. Five adapters implemented that pair independently,
and the copies drifted -- per Q00/ouroboros#1785, a schema-validity guard was
added to ``zcode_cli_adapter`` and never reached the other four, so a malformed
``json_schema`` raised an uncaught ``UnknownType`` there instead of returning a
message. Both halves now live here so a fix lands once.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

_TYPE_NOUNS = {
    "array": "JSON array",
    "object": "JSON object",
}

_JSON_OBJECT_DIRECTIVE = (
    "Respond with ONLY a valid JSON object. Do not use markdown fences, "
    "headers, or explanatory text."
)


def build_response_format_directive(response_format: dict[str, object] | None) -> str | None:
    """Translate ``response_format`` into a prompt directive.

    Returns ``None`` when there is nothing to instruct: no ``response_format``,
    an unrecognized ``type``, or a ``json_schema`` entry that is not an object.

    A ``json_schema`` payload may be either the schema itself or an OpenAI-style
    wrapper holding it under a ``schema`` key; both spellings are accepted. A
    schema that cannot be serialized falls back to ``str()`` rather than failing
    the request, since the directive is advisory and the payload is validated
    separately by :func:`validate_response_format_payload`.
    """
    if not response_format:
        return None

    fmt_type = response_format.get("type")
    if fmt_type == "json_object":
        return _JSON_OBJECT_DIRECTIVE

    if fmt_type == "json_schema":
        schema = response_format.get("json_schema")
        if not isinstance(schema, dict):
            return None
        schema_payload = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
        top_type = (
            schema_payload.get("type", "object") if isinstance(schema_payload, dict) else "object"
        )
        type_noun = _TYPE_NOUNS.get(str(top_type), "JSON value")
        try:
            rendered = json.dumps(schema_payload, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(schema_payload)
        return (
            f"Respond with ONLY a valid {type_noun} that matches this schema. "
            "Do not use markdown fences, headers, or explanatory text.\n\n"
            f"JSON schema:\n{rendered}"
        )

    return None


def validate_response_format_payload(
    payload: str,
    response_format: dict[str, object],
) -> str | None:
    """Validate extracted JSON against ``response_format``.

    Returns ``None`` when the payload satisfies the requested format, otherwise
    a human-readable reason. This function reports problems by returning them;
    it does not raise for bad input.

    Two failure modes are easy to miss, both of which raise from inside
    ``jsonschema`` without being a ``ValidationError``:

    * a schema that is itself invalid (``{"type": "not-a-real-type"}``) makes
      ``validate`` raise ``UnknownType``, which escaped the ``except`` clause
      in four of the five original copies;
    * a ``$ref`` pointing at nothing passes ``check_schema`` and then raises
      ``_WrappedReferencingError`` during ``validate`` -- which every copy
      missed, including the one that had the ``check_schema`` guard.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc}"

    fmt_type = response_format.get("type")
    if fmt_type == "json_object":
        return None if isinstance(parsed, dict) else "expected a JSON object"

    if fmt_type == "json_schema":
        schema = response_format.get("json_schema")
        if not isinstance(schema, dict):
            return "json_schema response_format is missing a schema object"
        schema_payload = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
        try:
            Draft202012Validator.check_schema(schema_payload)
            Draft202012Validator(schema_payload).validate(parsed)
        except JsonSchemaError as exc:
            return f"invalid JSON schema: {exc.message}"
        except JsonSchemaValidationError as exc:
            return exc.message
        except Exception as exc:  # noqa: BLE001 - the contract is to report, never raise
            # `jsonschema` raises outside its own exception hierarchy. A `$ref`
            # pointing at nothing is structurally valid, so `check_schema`
            # passes and `validate` raises `_WrappedReferencingError`, whose
            # MRO is `_RefResolutionError -> referencing.exceptions.Unresolvable
            # -> Exception` -- neither a `SchemaError` nor a `ValidationError`.
            #
            # Catching that one class by name would mean importing
            # `referencing`, an undeclared transitive dependency of
            # `jsonschema`, and would leave the next such class uncovered. The
            # caller-visible contract is that this function reports rather than
            # raises, so it is enforced here rather than enumerated.
            return f"could not validate against JSON schema: {type(exc).__name__}: {exc}"

    return None


__all__ = [
    "build_response_format_directive",
    "validate_response_format_payload",
]
