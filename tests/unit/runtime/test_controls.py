"""Tests for :class:`RuntimeControls` and :func:`load_runtime_controls`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.runtime.controls import (
    DEFAULT_SESSION_WALL_CLOCK_SECONDS,
    RuntimeControls,
    load_runtime_controls,
)


def test_default_constructor() -> None:
    """Bare ``RuntimeControls()`` carries the documented default."""
    controls = RuntimeControls()
    assert controls.session_wall_clock_seconds == DEFAULT_SESSION_WALL_CLOCK_SECONDS
    assert controls.watchdog_enabled


def test_explicit_constructor() -> None:
    controls = RuntimeControls(session_wall_clock_seconds=600)
    assert controls.session_wall_clock_seconds == 600
    assert controls.watchdog_enabled


def test_zero_disables_watchdog() -> None:
    """``session_wall_clock_seconds == 0`` is the explicit disable knob."""
    controls = RuntimeControls(session_wall_clock_seconds=0)
    assert controls.session_wall_clock_seconds == 0
    assert not controls.watchdog_enabled


def test_negative_rejected() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        RuntimeControls(session_wall_clock_seconds=-1)


def test_load_returns_defaults_when_path_is_none() -> None:
    assert load_runtime_controls(None) == RuntimeControls()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_runtime_controls(tmp_path / "nope.yaml")


def test_load_yaml_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "runtime_controls.yaml"
    fixture.write_text("runtime_controls:\n  session_wall_clock_seconds: 600\n")
    controls = load_runtime_controls(fixture)
    assert controls.session_wall_clock_seconds == 600


def test_load_empty_yaml_returns_defaults(tmp_path: Path) -> None:
    fixture = tmp_path / "empty.yaml"
    fixture.write_text("")
    assert load_runtime_controls(fixture) == RuntimeControls()


def test_load_missing_block_returns_defaults(tmp_path: Path) -> None:
    fixture = tmp_path / "no_block.yaml"
    fixture.write_text("unrelated_key: value\n")
    # Missing ``runtime_controls`` mapping → defaults.
    assert load_runtime_controls(fixture) == RuntimeControls()


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.yaml"
    fixture.write_text("runtime_controls: [this, isn't, a, mapping\n")
    with pytest.raises(ValueError):
        load_runtime_controls(fixture)


def test_load_non_mapping_top_level_raises(tmp_path: Path) -> None:
    fixture = tmp_path / "list_top.yaml"
    fixture.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_runtime_controls(fixture)


def test_load_non_mapping_block_raises(tmp_path: Path) -> None:
    fixture = tmp_path / "list_block.yaml"
    fixture.write_text("runtime_controls:\n  - 600\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_runtime_controls(fixture)


def test_load_unknown_key_rejected(tmp_path: Path) -> None:
    """Unknown keys under ``runtime_controls`` are rejected — v2 expansion
    keys (idle/no_progress/safety, directive vocabulary, …) require code
    review to land, not silent acceptance."""
    fixture = tmp_path / "extra_key.yaml"
    fixture.write_text(
        "runtime_controls:\n"
        "  session_wall_clock_seconds: 600\n"
        "  idle_timeout_seconds: 60\n"  # v2 expansion path; not yet here.
    )
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        load_runtime_controls(fixture)


def test_load_non_integer_budget_rejected(tmp_path: Path) -> None:
    fixture = tmp_path / "string_budget.yaml"
    fixture.write_text("runtime_controls:\n  session_wall_clock_seconds: '600'\n")
    with pytest.raises(ValueError, match="must be an integer"):
        load_runtime_controls(fixture)


def test_runtime_controls_is_frozen() -> None:
    """``RuntimeControls`` is a frozen dataclass — accidental mutation by
    a consumer must raise rather than silently shift the contract."""
    controls = RuntimeControls(session_wall_clock_seconds=600)
    with pytest.raises(Exception):  # noqa: BLE001 - frozen dataclass raises FrozenInstanceError
        controls.session_wall_clock_seconds = 900  # type: ignore[misc]


def test_load_explicit_zero_disable(tmp_path: Path) -> None:
    fixture = tmp_path / "disabled.yaml"
    fixture.write_text("runtime_controls:\n  session_wall_clock_seconds: 0\n")
    controls = load_runtime_controls(fixture)
    assert not controls.watchdog_enabled


def test_default_budget_is_four_hours() -> None:
    """Pin the documented default — changes here ripple through every
    operator's invocation, so the value gets a regression guard."""
    assert DEFAULT_SESSION_WALL_CLOCK_SECONDS == 4 * 60 * 60


# ``yaml`` import is exercised indirectly; keep this so an environment
# missing PyYAML fails the suite loudly here rather than mid-pipeline.
def test_yaml_dependency_available() -> None:
    import yaml as _yaml  # noqa: PLC0415 — intentional probe

    parsed: Any = _yaml.safe_load("x: 1")
    assert parsed == {"x": 1}
