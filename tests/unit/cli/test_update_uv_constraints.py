"""Receipt version constraints must fail before a uv update can mutate anything."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.update import (
    InstallationIdentity,
    InstallationIdentityError,
    _check_uv_target,
    _detect_installation_identity,
    app,
)

runner = CliRunner()


def _uv_identity(
    tmp_path: Path,
    specifier: str | None,
    *,
    marker: str | None = None,
) -> tuple[InstallationIdentity, Path]:
    environment = tmp_path / "custom-uv-root" / "ouroboros-ai"
    script_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    script_dir.mkdir(parents=True)
    console = script_dir / ("ouroboros.exe" if os.name == "nt" else "ouroboros")
    console.write_text("launcher", encoding="utf-8")

    main: dict[str, object] = {"name": "Ouroboros_AI", "extras": ["mcp"]}
    if specifier is not None:
        main["specifier"] = specifier
    if marker is not None:
        main["marker"] = marker
    requirements = [main, {"name": "click", "specifier": ">=8.1,<9"}]
    entries = [
        "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in item.items()) + " }"
        for item in requirements
    ]
    receipt = environment / "uv-receipt.toml"
    receipt.write_text("[tool]\nrequirements = [" + ", ".join(entries) + "]\n", encoding="utf-8")
    with patch("ouroboros.cli.commands.update.shutil.which", return_value="/selected/bin/uv"):
        identity = _detect_installation_identity(environment)
    return identity, receipt


def _plain(output: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize(
    "specifier",
    ["==0.55.4", "===0.55.4", "<0.55.6", "!=0.55.6", "~=0.54.0"],
)
def test_excluding_main_requirement_blocks_selected_target(tmp_path: Path, specifier: str) -> None:
    identity, receipt = _uv_identity(tmp_path, specifier)
    original = receipt.read_bytes()

    with pytest.raises(InstallationIdentityError):
        _check_uv_target(identity, "0.55.6")

    assert receipt.read_bytes() == original


@pytest.mark.parametrize(
    ("specifier", "target"),
    [
        (None, "0.55.6"),
        ("", "0.55.6"),
        (">=0.55.4,<0.56", "0.55.6"),
        ("==0.55.*", "0.55.6"),
        ("~=0.55.4", "0.55.6"),
        ("==0.55.6", "0.55.6"),
        (">=0.55.4", "0.56.0rc1"),
    ],
)
def test_permitted_targets_ignore_other_package_constraints(
    tmp_path: Path, specifier: str | None, target: str
) -> None:
    identity, receipt = _uv_identity(tmp_path, specifier)
    original = receipt.read_bytes()

    _check_uv_target(identity, target)

    assert identity.version_specifier == (specifier or "")
    assert receipt.read_bytes() == original


def test_applicable_marker_keeps_main_requirement_constraint(tmp_path: Path) -> None:
    identity, _ = _uv_identity(tmp_path, "==0.55.4", marker='python_version >= "3"')

    with pytest.raises(InstallationIdentityError):
        _check_uv_target(identity, "0.55.6")


def test_inactive_marker_does_not_block_target(tmp_path: Path) -> None:
    identity, _ = _uv_identity(tmp_path, "==0.55.4", marker='python_version < "0"')

    _check_uv_target(identity, "0.55.6")


@pytest.mark.parametrize(
    ("specifier", "marker"),
    [
        (">=>0.55", None),
        (">=0.55", "invalid marker"),
        (None, "invalid marker"),
        (">=0.55", 'python_version ~= "not-a-version"'),
    ],
)
def test_invalid_constraint_metadata_fails_closed(
    tmp_path: Path, specifier: str | None, marker: str | None
) -> None:
    identity, receipt = _uv_identity(tmp_path, specifier, marker=marker)
    original = receipt.read_bytes()

    with pytest.raises(InstallationIdentityError):
        _check_uv_target(identity, "0.55.6")

    assert receipt.read_bytes() == original


def test_uv_preflight_does_not_change_pipx_semantics(tmp_path: Path) -> None:
    identity, _ = _uv_identity(tmp_path, "==0.55.4")
    pipx_identity = replace(
        identity,
        manager="pipx",
        version_specifier="invalid specifier",
        requirement_marker="invalid marker",
    )

    _check_uv_target(pipx_identity, "0.55.6")


@pytest.mark.parametrize("options", [[], ["--yes"], ["--dry-run"]])
def test_blocked_update_stops_before_prompt_and_mutating_steps(
    tmp_path: Path, options: list[str]
) -> None:
    identity, receipt = _uv_identity(tmp_path, "==0.55.4")
    original = receipt.read_bytes()
    with (
        patch("ouroboros.cli.commands.update.__version__", "0.55.4"),
        patch("ouroboros.cli.commands.update._latest_pypi_version", return_value="0.55.6"),
        patch("ouroboros.cli.commands.update._detect_installation_identity", return_value=identity),
        patch("ouroboros.cli.commands.update.typer.confirm") as confirm,
        patch("ouroboros.cli.commands.update._run_step") as step,
        patch("ouroboros.cli.commands.update._installed_version") as installed_version,
        patch("ouroboros.cli.commands.update.configure_omp_tool_call_timeout") as configure_omp,
    ):
        result = runner.invoke(app, [*options, "--runtime", "none"])

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "==0.55.4" in output
    assert "0.55.6" in output
    assert "No changes were made" in output
    assert "uv-receipt.toml" in output
    assert "Upgraded" not in output
    confirm.assert_not_called()
    step.assert_not_called()
    installed_version.assert_not_called()
    configure_omp.assert_not_called()
    assert receipt.read_bytes() == original


def test_allowed_update_replays_original_uv_environment(tmp_path: Path) -> None:
    identity, receipt = _uv_identity(tmp_path, ">=0.55.4,<0.56")
    original = receipt.read_bytes()
    with (
        patch("ouroboros.cli.commands.update.__version__", "0.55.4"),
        patch("ouroboros.cli.commands.update._latest_pypi_version", return_value="0.55.6"),
        patch("ouroboros.cli.commands.update._detect_installation_identity", return_value=identity),
        patch("ouroboros.cli.commands.update._run_step", return_value=True) as step,
        patch("ouroboros.cli.commands.update._installed_version", return_value="0.55.6"),
        patch("ouroboros.cli.commands.update.configure_omp_tool_call_timeout", return_value=True),
    ):
        result = runner.invoke(app, ["--yes", "--runtime", "none"])

    assert result.exit_code == 0, result.output
    step.assert_called_once()
    assert step.call_args.args[0] == [
        identity.manager_binary,
        "tool",
        "upgrade",
        identity.tool_name,
    ]
    assert step.call_args.kwargs["env_overrides"] == {"UV_TOOL_DIR": str(identity.manager_home)}
    assert receipt.read_bytes() == original


def test_check_still_reports_versions_without_installation_probe() -> None:
    with (
        patch("ouroboros.cli.commands.update.__version__", "0.55.4"),
        patch("ouroboros.cli.commands.update._latest_pypi_version", return_value="0.55.6"),
        patch("ouroboros.cli.commands.update._detect_installation_identity") as detect,
        patch("ouroboros.cli.commands.update._run_step") as step,
    ):
        result = runner.invoke(app, ["--check"])

    assert result.exit_code == 0
    assert "Update available" in result.output
    detect.assert_not_called()
    step.assert_not_called()


def test_zero_exit_without_version_upgrade_does_not_report_success(tmp_path: Path) -> None:
    identity, _ = _uv_identity(tmp_path, None)
    with (
        patch("ouroboros.cli.commands.update.__version__", "0.55.4"),
        patch("ouroboros.cli.commands.update._latest_pypi_version", return_value="0.55.6"),
        patch("ouroboros.cli.commands.update._detect_installation_identity", return_value=identity),
        patch("ouroboros.cli.commands.update.subprocess.run", return_value=MagicMock(returncode=0)),
        patch("ouroboros.cli.commands.update._installed_version", return_value="0.55.4"),
        patch("ouroboros.cli.commands.update.print_success") as success,
    ):
        result = runner.invoke(app, ["--yes", "--runtime", "none"])

    assert result.exit_code == 1
    assert "Runtime integration was not refreshed" in _plain(result.output)
    success.assert_not_called()
