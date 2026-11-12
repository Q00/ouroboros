"""Documented credential loading errors must not disclose input values."""

from pathlib import Path
import traceback

import pytest

from ouroboros.config import load_credentials
from ouroboros.core.errors import ConfigError


@pytest.mark.parametrize(
    "content",
    [
        "providers:\n  anthropic:\n    api_key: SYNTHETIC_PRIVATE_VALUE: x\n",
        "providers:\n  anthropic:\n    api_key: [SYNTHETIC_PRIVATE_VALUE]\n",
        "providers:\n  SYNTHETIC_PRIVATE_VALUE:\n    api_key: []\n",
    ],
)
def test_public_credentials_error_never_exposes_input(tmp_path: Path, content: str) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConfigError) as caught:
        load_credentials(path)
    error = caught.value
    rendered = "".join(traceback.format_exception(error))
    assert "SYNTHETIC_PRIVATE_VALUE" not in rendered
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(error.details)
    assert error.config_file == str(path)
    assert error.__context__ is None


def test_credentials_validation_error_preserves_field_and_classification(tmp_path: Path) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text("providers:\n  anthropic:\n    api_key: []\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_credentials(path)
    assert "providers.*.api_key" in str(caught.value)
    detail = caught.value.details["validation_errors"][0]
    assert detail["type"] == "string_type"
    assert detail["loc"] == ("providers", "*", "api_key")


def test_public_parse_error_suppresses_parser_snippet_and_chain(tmp_path: Path) -> None:
    from unittest.mock import patch

    import yaml

    path = tmp_path / "credentials.yaml"
    path.write_text("placeholder: true\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError) as parsed:
        yaml.safe_load("api_key: SYNTHETIC_PRIVATE_VALUE: x\n")
    assert "SYNTHETIC_PRIVATE_VALUE" in str(parsed.value)
    with (
        patch("ouroboros.config.loader.yaml.safe_load", side_effect=parsed.value),
        pytest.raises(ConfigError) as caught,
    ):
        load_credentials(path)
    assert "SYNTHETIC_PRIVATE_VALUE" not in "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value.details)
    assert caught.value.__context__ is None
    assert caught.value.details["yaml_error"] == "invalid_yaml"
    assert caught.value.details["line"] == 1
    assert caught.value.details["column"] > 0
