"""Tests for the bridge between the saved logging level and structlog (#1955).

The regression these guard: `ouroboros config set logging.level warning` wrote
to the config file and reported success, but the CLI kept printing INFO records
because the saved value never reached `configure_logging`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.cli import logging_setup
from ouroboros.observability import LoggingConfig, LogMode


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


@pytest.mark.parametrize("debug", [False, True])
def test_operator_prod_mode_survives_applying_a_level(
    monkeypatch: pytest.MonkeyPatch, debug: bool
) -> None:
    """OUROBOROS_LOG_MODE is an operator override and must not be reset to dev.

    Replacing the whole config with a fresh `LoggingConfig(log_level=...)` used
    the default mode, so every `init`/`pm`/`run` invocation silently turned
    production JSON logging back into human-readable output.
    """
    monkeypatch.setenv("OUROBOROS_LOG_MODE", "prod")
    monkeypatch.setattr(
        "ouroboros.config.load_config", lambda: _stub_config("warning"), raising=False
    )
    # A previous test may have left logging configured; the env default only
    # applies when it has not been.
    monkeypatch.setattr("ouroboros.observability.logging._current_config", None)
    seen: list[LoggingConfig] = []
    monkeypatch.setattr(logging_setup, "configure_logging", seen.append)

    logging_setup.configure_cli_logging(debug=debug)

    assert seen[0].mode is LogMode.PROD
    assert seen[0].log_level == ("DEBUG" if debug else "WARNING")


def test_only_the_level_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything the base config carries other than the level is preserved."""
    monkeypatch.delenv("OUROBOROS_LOG_MODE", raising=False)
    monkeypatch.setattr("ouroboros.observability.logging._current_config", None)
    monkeypatch.setattr(
        "ouroboros.config.load_config", lambda: _stub_config("warning"), raising=False
    )
    base = logging_setup.default_logging_config()
    seen: list[LoggingConfig] = []
    monkeypatch.setattr(logging_setup, "configure_logging", seen.append)

    logging_setup.configure_cli_logging(debug=False)

    assert seen[0].model_dump(exclude={"log_level"}) == base.model_dump(exclude={"log_level"})


def test_run_command_applies_logging_before_importing_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run` also takes --debug, and its orchestrator import auto-configures logging.

    Whichever happens first wins, so the seam has to run before the import or the
    saved level is ignored for the primary execution path (#1955 follow-up).
    """
    from ouroboros.cli.commands import run as run_cmd

    order: list[str] = []
    monkeypatch.setattr(
        run_cmd, "configure_cli_logging", lambda *, debug: order.append(f"seam:{debug}")
    )

    def _fake_asyncio_run(coro: object) -> None:
        coro.close()  # type: ignore[union-attr]
        order.append("orchestrator")

    monkeypatch.setattr(run_cmd.asyncio, "run", _fake_asyncio_run)

    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: demo\n", encoding="utf-8")

    run_cmd.workflow(seed_file=seed_file, orchestrator=True, debug=False)

    assert order == ["seam:False", "orchestrator"]
