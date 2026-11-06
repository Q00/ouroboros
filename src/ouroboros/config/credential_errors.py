"""Sanitize validation errors from credential files before reporting them."""

from typing import Any

from pydantic import ValidationError


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
