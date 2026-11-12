"""Public configuration diagnostics omit file contents, including mapping keys."""

from pathlib import Path
import traceback
from unittest.mock import patch

from pydantic import ValidationError
import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.config import app
from ouroboros.config import load_config
from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ConfigError

SECRET = "SYNTHETIC_PRIVATE_VALUE"
CASES = {
    "runtime": f"orchestrator:\n  runtime_backend: {SECRET}\n",
    "number": f"evaluation:\n  satisfaction_threshold: {SECRET}\n",
    "yaml": f"evaluation:\n  satisfaction_threshold: {SECRET}: x\n",
    "utf8": SECRET.encode() + b"\xff",
    "scalar": SECRET,
    "sequence": f"- {SECRET}\n",
    "section": f"evaluation: {SECRET}\n",
    "profile-key": f"llm_profiles:\n  {SECRET}:\n    temperature: {SECRET}\n",
    "provider-key": f"llm_profiles:\n  myprofile:\n    providers:\n      {SECRET}:\n        temperature: {SECRET}\n",
    "field-as-key": f"llm_profiles:\n  evaluation:\n    providers:\n      satisfaction_threshold:\n        max_tokens: {SECRET}\n",
    "stage-key": f"orchestrator:\n  runtime_profile:\n    stages:\n      {SECRET}: codex\n",
    "validator-message": f"execution:\n  project_guidance: [{SECRET}, {SECRET}]\n",
    "key-type": f"llm_role_profiles:\n  123: {SECRET}\n",
}
CASES = {k: v.encode("utf-8") if isinstance(v, str) else v for k, v in CASES.items()}
CASES["bom-runtime"] = b"\xef\xbb\xbf" + CASES["runtime"]
VALID = [
    b"",
    b"{}",
    b"\xef\xbb\xbf# UTF-8 \xec\x84\xa4\xec\xa0\x95\nmodels:\n  pin: true\norchestrator:\n  runtime_backend: codex\n  worktree_root: /tmp/\xec\x84\xa4\xec\xa0\x95\n",
]


@pytest.mark.parametrize("content", CASES.values(), ids=CASES.keys())
def test_public_config_error_is_safe(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    error = caught.value
    assert SECRET not in str(error)
    assert SECRET not in str(error.details)
    assert SECRET not in str(error.config_key)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert error.__context__ is None
    assert error.__cause__ is None
    assert error.config_file == str(path)
    for item in error.details.get("validation_errors", []):
        assert set(item) == {"loc", "type", "msg"}


@pytest.mark.parametrize("content", CASES.values(), ids=CASES.keys())
def test_config_validate_omits_invalid_file_contents(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    with (
        patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
        patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
    ):
        result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 1, result.output
    assert SECRET not in result.output


@pytest.mark.parametrize("backend", [SECRET, [SECRET], {SECRET: SECRET}])
def test_validate_runtime_check_is_safe_without_schema_loader(tmp_path: Path, backend) -> None:
    # The CLI checks raw YAML independently of the public loader.
    with (
        patch(
            "ouroboros.cli.commands.config._load_config",
            return_value=({"orchestrator": {"runtime_backend": backend}}, tmp_path / "config.yaml"),
        ),
        patch("ouroboros.config.loader.load_config"),
    ):
        result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "orchestrator.runtime_backend" in result.output
    assert SECRET not in result.output


def test_config_error_keeps_safe_field_and_type(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(CASES["number"])
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    error = caught.value
    assert error.config_key == "evaluation.satisfaction_threshold"
    assert error.details["config_keys"] == ["evaluation.satisfaction_threshold"]
    assert error.details["validation_errors"] == [
        {
            "loc": ("evaluation", "satisfaction_threshold"),
            "type": "float_parsing",
            "msg": "Input should be a valid number",
        }
    ]


def test_dict_key_cannot_impersonate_schema_location(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(CASES["field-as-key"])
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    loc = caught.value.details["validation_errors"][0]["loc"]
    assert loc == ("llm_profiles", "*", "*", "*", "*")


@pytest.mark.parametrize("content", [CASES["yaml"], CASES["utf8"]], ids=["yaml", "utf8"])
def test_config_parse_error_keeps_category_and_numeric_location(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    details = caught.value.details
    if content == CASES["yaml"]:
        assert details["yaml_error"] == "invalid_yaml"
        assert details["line"] == 2
        assert isinstance(details["column"], int)
    else:
        assert details == {"yaml_error": "invalid_utf8"}


@pytest.mark.parametrize("content", VALID)
def test_valid_config_still_loads(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    config = load_config(path)
    assert isinstance(config, OuroborosConfig)
    assert config.logging.level == "info"
    assert config.models.pin is (content == VALID[2])
    if content == VALID[2]:
        assert config.orchestrator.worktree_root == "/tmp/설정"


def test_direct_pydantic_callers_keep_validation_error_contract() -> None:
    with pytest.raises(ValidationError) as caught:
        OuroborosConfig.model_validate({"evaluation": {"satisfaction_threshold": SECRET}})
    assert caught.value.errors()[0]["input"] == SECRET


@pytest.mark.parametrize("bom", [False, True], ids=["utf8", "bom"])
def test_config_validate_accepts_valid_utf8(tmp_path: Path, bom: bool) -> None:
    content = (
        "# 설정 😀\norchestrator:\n  runtime_backend: codex\n  worktree_root: /tmp/설정\n".encode()
    )
    (tmp_path / "config.yaml").write_bytes((b"\xef\xbb\xbf" if bom else b"") + content)
    with (
        patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
        patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
    ):
        result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "Configuration is valid" in result.output
