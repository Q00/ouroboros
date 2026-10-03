"""The check package switch: on by default, explicit settings win (boundary/switch.py)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouroboros.boundary.switch import resolve_check_package_settings, resolve_switch
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key


def _resolve(
    cli: bool | None = None,
    *,
    env: str | None = None,
    configured: str | None = None,
    config_readable: bool = True,
) -> bool:
    environ = {} if env is None else {"OUROBOROS_CHECK_PACKAGE": env}
    return resolve_switch(
        cli, configured=configured, config_readable=config_readable, environ=environ
    )


@pytest.mark.parametrize(
    ("kwargs", "enabled"),
    [
        ({"cli": True, "env": "off", "configured": "off"}, True),
        ({"cli": False, "env": "on", "configured": "on"}, False),
        ({"env": "on", "configured": "off"}, True),
        ({"env": "off", "configured": "on"}, False),
        # a set but unreadable value is an opt-out (with a warning), never the default.
        ({"env": "garbage", "configured": "on"}, False),
        ({"env": "disable"}, False),
        ({"configured": "off"}, False),
        ({"configured": "on"}, True),
        ({}, True),
        # a present but empty or whitespace value is set: off, never the default.
        ({"env": ""}, False),
        ({"env": "   "}, False),
        ({"env": "\t\n"}, False),
        # an unreadable config may hold an explicit off: never the default.
        ({"config_readable": False}, False),
        ({"config_readable": False, "env": "on"}, True),
        ({"config_readable": False, "cli": True}, True),
    ],
)
def test_explicit_settings_override_the_default(kwargs: dict, enabled: bool) -> None:
    assert _resolve(kwargs.pop("cli", None), **kwargs) is enabled


@pytest.mark.parametrize("value", ["1", "true", "yes", "ON", "On", " on", "on\n", "enable"])
def test_only_the_exact_value_on_turns_the_package_on_from_the_environment(value: str) -> None:
    """The grammar is exactly ``on`` or ``off``: an alias or another spelling means off."""
    assert _resolve(env=value, configured="on") is False
    assert _resolve(env=value) is False


def test_the_default_depends_on_nothing_but_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No telemetry, no anonymous ID, no notice: the check package is still on."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    assert resolve_switch(None, configured=None) is True


def test_project_env_cannot_set_the_switch() -> None:
    assert is_untrusted_env_denied_key("OUROBOROS_CHECK_PACKAGE")


def test_switch_precedence_cli_then_env_then_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    config = SimpleNamespace(
        boundary=SimpleNamespace(
            check_package="off",
            constructor_timeout_seconds=300,
            check_timeout_seconds=60,
            max_construction_attempts=3,
            base_regression="off",
        )
    )
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=config.boundary):
        settings = resolve_check_package_settings(None)
        assert settings.enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
        settings = resolve_check_package_settings(None)
        assert settings.enabled is True and settings.max_construction_attempts == 3
        assert settings.base_regression is False
        assert resolve_check_package_settings(False).enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
        assert resolve_check_package_settings(True).enabled is True


def test_unset_switch_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.config.models import BoundaryConfig

    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        settings = resolve_check_package_settings(None)
    assert settings.enabled is True


def test_unset_base_regression_takes_the_one_default() -> None:
    from ouroboros.boundary.base_regression import BASE_REGRESSION_DEFAULT
    from ouroboros.config.models import BoundaryConfig

    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        assert resolve_check_package_settings(None).base_regression is BASE_REGRESSION_DEFAULT
    configured = BoundaryConfig.model_validate({"base_regression": False})
    assert configured.base_regression == "off"
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=configured):
        assert resolve_check_package_settings(None).base_regression is False


def test_unreadable_config_never_turns_the_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable config may hold an explicit off: without a flag or variable, off."""
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    with patch(
        "ouroboros.boundary.switch._load_boundary_config", side_effect=ValueError("bad yaml")
    ):
        settings = resolve_check_package_settings(None)
        assert settings.enabled is False
        assert resolve_check_package_settings(True).enabled is True
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
        assert resolve_check_package_settings(None).enabled is True


def test_config_accepts_yaml_boolean_spelling() -> None:
    from ouroboros.config.models import BoundaryConfig, OuroborosConfig

    assert BoundaryConfig.model_validate({"check_package": True}).check_package == "on"
    assert BoundaryConfig.model_validate({"check_package": False}).check_package == "off"
    assert BoundaryConfig.model_validate({"check_package": "off"}).check_package == "off"
    # Unset means "use the default (on)", which differs from an explicit on or off.
    assert OuroborosConfig().boundary.check_package is None


def test_an_absent_config_file_keeps_the_default_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``~/.ouroboros/config.yaml`` is a supported fresh setup, not an unreadable one.

    Before the fix the loader's missing-file error was read as unreadable: off.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    (tmp_path / ".ouroboros").mkdir()
    assert resolve_check_package_settings(None).enabled is True
    (tmp_path / ".ouroboros").rmdir()
    assert resolve_check_package_settings(None).enabled is True


@pytest.mark.parametrize("kind", ["unparsable", "dangling_link", "unreadable"])
def test_a_present_config_file_that_cannot_be_read_turns_the_default_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    if kind == "unparsable":
        config.write_text("boundary: [unclosed\n")
    elif kind == "dangling_link":
        config.symlink_to(config_dir / "missing.yaml")
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        config.write_text("boundary:\n  check_package: off\n")
        config.chmod(0)
    try:
        assert resolve_check_package_settings(None).enabled is False
        assert resolve_check_package_settings(True).enabled is True
    finally:
        if config.exists():
            config.chmod(0o600)
