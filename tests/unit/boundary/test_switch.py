"""The check package switch: record by default, explicit settings win (boundary/switch.py)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouroboros.boundary.switch import (
    CHECK_PACKAGE_DEFAULT,
    resolve_check_package_settings,
    resolve_switch,
)
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key


def _resolve(
    cli: bool | None = None,
    *,
    env: str | None = None,
    configured: str | None = None,
    config_readable: bool = True,
) -> str:
    environ = {} if env is None else {"OUROBOROS_CHECK_PACKAGE": env}
    return resolve_switch(
        cli, configured=configured, config_readable=config_readable, environ=environ
    )


@pytest.mark.parametrize(
    ("kwargs", "mode"),
    [
        ({"cli": True, "env": "off", "configured": "off"}, "decide"),
        ({"cli": False, "env": "on", "configured": "on"}, "off"),
        ({"env": "on", "configured": "off"}, "decide"),
        ({"env": "decide", "configured": "record"}, "decide"),
        ({"env": "record", "configured": "decide"}, "record"),
        ({"env": "off", "configured": "on"}, "off"),
        # a set but unreadable value is an opt-out (with a warning), never the default.
        ({"env": "garbage", "configured": "on"}, "off"),
        ({"env": "disable"}, "off"),
        ({"configured": "off"}, "off"),
        # ``on`` is the older spelling of ``decide``: an explicit on keeps deciding.
        ({"configured": "on"}, "decide"),
        ({"configured": "decide"}, "decide"),
        ({"configured": "record"}, "record"),
        ({}, "record"),
        # a present but empty or whitespace value is set: off, never the default.
        ({"env": ""}, "off"),
        ({"env": "   "}, "off"),
        ({"env": "\t\n"}, "off"),
        # an unreadable config may hold an explicit off: never the default.
        ({"config_readable": False}, "off"),
        ({"config_readable": False, "env": "on"}, "decide"),
        ({"config_readable": False, "env": "record"}, "record"),
        ({"config_readable": False, "cli": True}, "decide"),
    ],
)
def test_explicit_settings_override_the_default(kwargs: dict, mode: str) -> None:
    assert _resolve(kwargs.pop("cli", None), **kwargs) == mode


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "ON", "On", " on", "on\n", "enable", "Record", "DECIDE"]
)
def test_only_the_exact_mode_names_are_read_from_the_environment(value: str) -> None:
    """The grammar is exactly a mode name: an alias or another spelling means off."""
    assert _resolve(env=value, configured="on") == "off"
    assert _resolve(env=value) == "off"


def test_the_default_depends_on_nothing_but_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No telemetry, no anonymous ID, no notice: the check package still runs, recording."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    assert CHECK_PACKAGE_DEFAULT == "record"
    assert resolve_switch(None, configured=None) == "record"


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
            worker_test_gate="decide",
        )
    )
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=config.boundary):
        settings = resolve_check_package_settings(None)
        assert settings.enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
        settings = resolve_check_package_settings(None)
        assert settings.enabled is True and settings.max_construction_attempts == 3
        assert settings.mode == "decide"
        assert (settings.base_regression, settings.worker_test_gate) == ("off", "decide")
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "record")
        settings = resolve_check_package_settings(None)
        assert settings.enabled is True and settings.mode == "record"
        assert resolve_check_package_settings(False).enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
        settings = resolve_check_package_settings(True)
        assert settings.enabled is True and settings.mode == "decide"


def test_unset_switch_records_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.config.models import BoundaryConfig

    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        settings = resolve_check_package_settings(None)
    assert settings.enabled is True and settings.mode == "record"
    for configured, mode in (("on", "decide"), ("decide", "decide"), ("record", "record")):
        explicit = BoundaryConfig.model_validate({"check_package": configured})
        with patch("ouroboros.boundary.switch._load_boundary_config", return_value=explicit):
            settings = resolve_check_package_settings(None)
        assert settings.enabled is True and settings.mode == mode


def test_unset_base_regression_takes_the_one_default() -> None:
    from ouroboros.boundary.base_regression import (
        BASE_REGRESSION_DEFAULT,
        WORKER_TEST_GATE_DEFAULT,
    )
    from ouroboros.config.models import BoundaryConfig

    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        settings = resolve_check_package_settings(None)
    assert (settings.base_regression, settings.worker_test_gate) == ("record", "record")
    assert (BASE_REGRESSION_DEFAULT, WORKER_TEST_GATE_DEFAULT) == ("record", "record")
    # An explicit decide keeps deciding.
    deciding = BoundaryConfig.model_validate({"base_regression": "decide"})
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=deciding):
        settings = resolve_check_package_settings(None)
    assert settings.base_regression == "decide"
    # The two switches are independent; a YAML boolean reads as decide or off.
    configured = BoundaryConfig.model_validate({"base_regression": False, "worker_test_gate": True})
    assert (configured.base_regression, configured.worker_test_gate) == ("off", "decide")
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=configured):
        settings = resolve_check_package_settings(None)
    assert (settings.base_regression, settings.worker_test_gate) == ("off", "decide")
    recorded = BoundaryConfig.model_validate({"base_regression": "record"})
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=recorded):
        settings = resolve_check_package_settings(None)
    assert (settings.base_regression, settings.worker_test_gate) == ("record", "record")
    with pytest.raises(ValueError):
        BoundaryConfig.model_validate({"base_regression": "on"})


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
    # Unset means "use the default (record)", which differs from an explicit setting.
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
