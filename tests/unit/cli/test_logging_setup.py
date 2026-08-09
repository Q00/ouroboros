"""Tests for the bridge between the saved logging level and structlog (#1955).

The regression these guard: `ouroboros config set logging.level warning` wrote
to the config file and reported success, but the CLI kept printing INFO records
because the saved value never reached `configure_logging`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.cli import logging_setup


def _stub_config(level: str) -> SimpleNamespace:
    return SimpleNamespace(logging=SimpleNamespace(level=level))


def test_saved_level_is_used_when_debug_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ouroboros.config.load_config", lambda: _stub_config("warning"), raising=False
    )

    assert logging_setup.resolve_cli_log_level(debug=False) == "WARNING"


@pytest.mark.parametrize("saved", ["debug", "info", "warning", "error"])
def test_every_saved_level_survives_the_field_name_gap(
    monkeypatch: pytest.MonkeyPatch, saved: str
) -> None:
    """`config.models.LoggingConfig.level` must reach `observability`'s `log_level`."""
    monkeypatch.setattr("ouroboros.config.load_config", lambda: _stub_config(saved), raising=False)

    assert logging_setup.resolve_cli_log_level(debug=False) == saved.upper()


def test_debug_flag_overrides_the_saved_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """--debug has to stay usable for reporting a problem, whatever is saved."""

    def _fail() -> SimpleNamespace:  # pragma: no cover - must not be reached
        raise AssertionError("--debug must not need to read the config file")

    monkeypatch.setattr("ouroboros.config.load_config", _fail, raising=False)

    assert logging_setup.resolve_cli_log_level(debug=True) == "DEBUG"


def test_unreadable_config_falls_back_instead_of_failing_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging setup is not the user's goal; a broken config must not abort the run."""

    def _raise() -> SimpleNamespace:
        raise OSError("config file is not readable")

    monkeypatch.setattr("ouroboros.config.load_config", _raise, raising=False)

    assert logging_setup.resolve_cli_log_level(debug=False) == "INFO"


def test_configure_passes_the_resolved_level_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ouroboros.config.load_config", lambda: _stub_config("error"), raising=False
    )
    seen: list[str] = []
    monkeypatch.setattr(
        logging_setup, "configure_logging", lambda config: seen.append(config.log_level)
    )

    logging_setup.configure_cli_logging(debug=False)

    assert seen == ["ERROR"]
