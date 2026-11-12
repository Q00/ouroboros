"""Safe diagnostics for credential and configuration loading failures."""

from typing import Any, get_args

from pydantic import BaseModel, ValidationError
import yaml

from ouroboros.config.models import OuroborosConfig


def sanitize_credential_errors(error: ValidationError) -> list[dict[str, Any]]:
    """Return only known-safe fields and locations from a validation error."""
    messages = {
        "dict_type": "Input should be a valid mapping",
        "model_type": "Input should be a valid provider mapping",
        "missing": "Field required",
        "string_type": "Input should be a valid string",
        "string_too_short": "String is too short",
    }
    safe_errors = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        # Provider names are user-controlled, so hide them even in locations.
        loc = tuple(
            part if index != 1 and part in {"providers", "api_key", "base_url"} else "*"
            for index, part in enumerate(item["loc"])
        )
        error_type = item["type"] if item["type"] in messages else "invalid"
        safe_errors.append(
            {"loc": loc, "type": error_type, "msg": messages.get(error_type, "Invalid value")}
        )
    return safe_errors


def safe_yaml_error_details(error: UnicodeDecodeError | yaml.YAMLError) -> dict[str, Any]:
    """Keep parser categories and numeric positions, never snippets or context."""
    mark = getattr(error, "problem_mark", None)
    location = {"line": mark.line + 1, "column": mark.column + 1} if mark is not None else {}
    category = "invalid_utf8" if isinstance(error, UnicodeDecodeError) else "invalid_yaml"
    return {"yaml_error": category, **location}


def sanitize_config_errors(error: ValidationError) -> list[dict[str, Any]]:
    """Keep declared model fields; mask locations once a container is reached.

    Unlike credentials, config contains several unrelated user-keyed mappings.
    Follow only model fields (including nullable models), never mapping keys,
    indices or union labels. The rest of a container's path becomes '*'.
    """
    messages = {
        "dict_type": "Input should be a valid mapping",
        "model_type": "Input should be a valid configuration mapping",
        "missing": "Field required",
        "string_type": "Input should be a valid string",
        "string_too_short": "String is too short",
        "int_type": "Input should be a valid integer",
        "int_parsing": "Input should be a valid integer",
        "int_from_float": "Input should be a valid integer",
        "float_type": "Input should be a valid number",
        "float_parsing": "Input should be a valid number",
        "bool_type": "Input should be a valid boolean",
        "bool_parsing": "Input should be a valid boolean",
        "list_type": "Input should be a valid list",
        "tuple_type": "Input should be a valid tuple",
        "literal_error": "Input should be an allowed value",
        "greater_than": "Value is below the allowed range",
        "greater_than_equal": "Value is below the allowed range",
        "less_than": "Value is above the allowed range",
        "less_than_equal": "Value is above the allowed range",
        "value_error": "Invalid value",
    }
    safe_errors = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        loc = []
        model: type[BaseModel] | None = OuroborosConfig
        for part in item["loc"]:
            field = model.model_fields.get(part) if model is not None else None
            loc.append(part if field is not None else "*")
            annotation = field.annotation if field is not None else None
            args = get_args(annotation)
            if len(args) == 2 and type(None) in args:
                annotation = next(arg for arg in args if arg is not type(None))
            model = (
                annotation
                if isinstance(annotation, type) and issubclass(annotation, BaseModel)
                else None
            )
        error_type = item["type"] if item["type"] in messages else "invalid"
        safe_errors.append(
            {
                "loc": tuple(loc),
                "type": error_type,
                "msg": messages.get(error_type, "Invalid value"),
            }
        )
    return safe_errors
