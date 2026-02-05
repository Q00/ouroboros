"""Unit tests for ouroboros.observability.logging module."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.observability.logging import (
    LoggingConfig,
    LogMode,
    bind_context,
    clear_context,
    configure_logging,
    get_current_config,
    get_logger,
    is_configured,
    reset_logging,
    set_console_logging,
    unbind_context,
)


@pytest.fixture(autouse=True)
def reset_logging_state() -> Any:
    """Reset logging state before and after each test."""
    reset_logging()
    set_console_logging(True)  # Ensure console logging is enabled for tests
    yield
    reset_logging()
    set_console_logging(True)  # Reset after test


@pytest.fixture
def temp_log_dir() -> Any:
    """Create a temporary directory for log files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestLogMode:
    """Test LogMode enum."""

    def test_log_mode_dev_value(self) -> None:
        """LogMode.DEV has correct string value."""
        assert LogMode.DEV.value == "dev"

    def test_log_mode_prod_value(self) -> None:
        """LogMode.PROD has correct string value."""
        assert LogMode.PROD.value == "prod"

    def test_log_mode_is_string_enum(self) -> None:
        """LogMode is a string enum."""
        assert isinstance(LogMode.DEV, str)
        assert LogMode.DEV == "dev"


class TestLoggingConfig:
    """Test LoggingConfig Pydantic model."""

    def test_default_config(self) -> None:
        """LoggingConfig has sensible defaults."""
        config = LoggingConfig()
        assert config.mode == LogMode.DEV
        assert config.log_level == "INFO"
        assert config.max_log_days == 7
        assert config.enable_file_logging is True
        assert config.log_dir == Path.home() / ".ouroboros" / "logs"

    def test_custom_config(self) -> None:
        """LoggingConfig accepts custom values."""
        config = LoggingConfig(
            mode=LogMode.PROD,
            log_level="DEBUG",
            max_log_days=14,
            enable_file_logging=False,
        )
        assert config.mode == LogMode.PROD
        assert config.log_level == "DEBUG"
        assert config.max_log_days == 14
        assert config.enable_file_logging is False

    def test_config_is_frozen(self) -> None:
        """LoggingConfig is immutable."""
        from pydantic import ValidationError as PydanticValidationError

        config = LoggingConfig()
        with pytest.raises(PydanticValidationError):
            config.mode = LogMode.PROD  # type: ignore[misc]

    def test_max_log_days_min_validation(self) -> None:
        """LoggingConfig validates max_log_days minimum."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            LoggingConfig(max_log_days=0)

    def test_max_log_days_max_validation(self) -> None:
        """LoggingConfig validates max_log_days maximum."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            LoggingConfig(max_log_days=400)

    def test_custom_log_dir(self, temp_log_dir: Path) -> None:
        """LoggingConfig accepts custom log directory."""
        config = LoggingConfig(log_dir=temp_log_dir)
        assert config.log_dir == temp_log_dir


class TestConfigureLogging:
    """Test configure_logging function."""

    def test_configure_with_defaults(self) -> None:
        """configure_logging works with default config."""
        configure_logging()
        assert is_configured()

    def test_configure_with_custom_config(self) -> None:
        """configure_logging accepts custom config."""
        config = LoggingConfig(mode=LogMode.PROD, log_level="DEBUG")
        configure_logging(config)
        assert is_configured()
        assert get_current_config() == config

    def test_configure_sets_current_config(self) -> None:
        """configure_logging stores the current config."""
        config = LoggingConfig(mode=LogMode.DEV)
        configure_logging(config)
        assert get_current_config() == config

    def test_configure_uses_env_mode(self) -> None:
        """configure_logging uses OUROBOROS_LOG_MODE environment variable."""
        with patch.dict(os.environ, {"OUROBOROS_LOG_MODE": "prod"}):
            configure_logging()
            config = get_current_config()
            assert config is not None
            assert config.mode == LogMode.PROD

    def test_configure_env_mode_defaults_to_dev(self) -> None:
        """configure_logging defaults to dev mode if env not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove OUROBOROS_LOG_MODE if it exists
            os.environ.pop("OUROBOROS_LOG_MODE", None)
            configure_logging()
            config = get_current_config()
            assert config is not None
            assert config.mode == LogMode.DEV

    def test_configure_env_mode_invalid_defaults_to_dev(self) -> None:
        """configure_logging defaults to dev mode for invalid env value."""
        with patch.dict(os.environ, {"OUROBOROS_LOG_MODE": "invalid"}):
            configure_logging()
            config = get_current_config()
            assert config is not None
            assert config.mode == LogMode.DEV

    def test_configure_creates_log_directory(self, temp_log_dir: Path) -> None:
        """configure_logging creates log directory if it doesn't exist."""
        log_subdir = temp_log_dir / "nested" / "logs"
        config = LoggingConfig(log_dir=log_subdir, enable_file_logging=True)
        configure_logging(config)
        assert log_subdir.exists()

    def test_configure_without_file_logging(self, temp_log_dir: Path) -> None:
        """configure_logging works without file logging."""
        config = LoggingConfig(log_dir=temp_log_dir, enable_file_logging=False)
        configure_logging(config)
        assert is_configured()
        # Log directory should not be created if file logging disabled
        # (Only if it doesn't already exist)


class TestGetLogger:
    """Test get_logger function."""

    def test_get_logger_returns_bound_logger(self) -> None:
        """get_logger returns a bound logger."""
        log = get_logger()
        assert log is not None

    def test_get_logger_auto_configures(self) -> None:
        """get_logger auto-configures if not configured."""
        assert not is_configured()
        log = get_logger()
        assert log is not None
        assert is_configured()

    def test_get_logger_with_name(self) -> None:
        """get_logger accepts a logger name."""
        log = get_logger("test.module")
        assert log is not None

    def test_get_logger_can_log(self, capsys: Any) -> None:
        """Logger can log messages."""
        config = LoggingConfig(enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.event.logged")
        captured = capsys.readouterr()
        assert "test.event.logged" in captured.err


class TestBindContext:
    """Test context binding functions."""

    def test_bind_context_adds_to_logs(self, capsys: Any) -> None:
        """bind_context adds context to log entries."""
        config = LoggingConfig(enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        bind_context(seed_id="seed_123", ac_id="ac_456")
        log.info("test.event")

        captured = capsys.readouterr()
        assert "seed_123" in captured.err
        assert "ac_456" in captured.err

    def test_unbind_context_removes_from_logs(self, capsys: Any) -> None:
        """unbind_context removes context from log entries."""
        config = LoggingConfig(enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        bind_context(seed_id="seed_123", ac_id="ac_456")
        unbind_context("ac_id")
        log.info("test.event.after.unbind")

        captured = capsys.readouterr()
        assert "seed_123" in captured.err
        # ac_id should not appear after unbind
        assert "ac_456" not in captured.err

    def test_clear_context_removes_all(self, capsys: Any) -> None:
        """clear_context removes all bound context."""
        config = LoggingConfig(enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        bind_context(seed_id="seed_123", ac_id="ac_456", depth=2)
        clear_context()
        log.info("test.event.after.clear")

        captured = capsys.readouterr()
        assert "seed_123" not in captured.err
        assert "ac_456" not in captured.err

    def test_bind_standard_keys(self, capsys: Any) -> None:
        """Standard log keys can be bound."""
        config = LoggingConfig(enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        bind_context(
            seed_id="seed_001",
            ac_id="ac_001",
            depth=3,
            iteration=5,
            tier="standard",
        )
        log.info("ac.execution.started")

        captured = capsys.readouterr()
        output = captured.err
        assert "seed_001" in output
        assert "ac_001" in output
        assert "depth" in output
        assert "iteration" in output
        assert "tier" in output


class TestDevModeOutput:
    """Test development mode output formatting."""

    def test_dev_mode_human_readable(self, capsys: Any) -> None:
        """Dev mode produces human-readable output."""
        config = LoggingConfig(mode=LogMode.DEV, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.dev.mode")

        captured = capsys.readouterr()
        # Dev mode should not be JSON (no leading brace)
        assert not captured.err.strip().startswith("{")

    def test_dev_mode_includes_log_level(self, capsys: Any) -> None:
        """Dev mode includes log level."""
        config = LoggingConfig(mode=LogMode.DEV, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.info.level")
        log.warning("test.warning.level")

        captured = capsys.readouterr()
        assert "info" in captured.err.lower()
        assert "warning" in captured.err.lower()


class TestProdModeOutput:
    """Test production mode output formatting."""

    def test_prod_mode_json_output(self, capsys: Any) -> None:
        """Prod mode produces JSON output."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.prod.mode")

        captured = capsys.readouterr()
        # Each line should be valid JSON
        for line in captured.err.strip().split("\n"):
            if line:
                data = json.loads(line)
                assert "event" in data

    def test_prod_mode_includes_timestamp(self, capsys: Any) -> None:
        """Prod mode includes ISO 8601 timestamp."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.timestamp")

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert "timestamp" in data
        # ISO 8601 format check (contains T and ends with +00:00 or Z)
        timestamp = data["timestamp"]
        assert "T" in timestamp

    def test_prod_mode_includes_log_level(self, capsys: Any) -> None:
        """Prod mode includes log level."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.level")

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert "level" in data
        assert data["level"] == "info"

    def test_prod_mode_includes_bound_context(self, capsys: Any) -> None:
        """Prod mode includes bound context in JSON."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        bind_context(seed_id="seed_json", depth=7)
        log.info("test.context")

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert data["seed_id"] == "seed_json"
        assert data["depth"] == 7


class TestLogLevels:
    """Test log level filtering."""

    def test_info_level_filters_debug(self, capsys: Any) -> None:
        """INFO level filters out DEBUG messages."""
        config = LoggingConfig(log_level="INFO", enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.debug("debug.message")
        log.info("info.message")

        captured = capsys.readouterr()
        assert "debug.message" not in captured.err
        assert "info.message" in captured.err

    def test_debug_level_shows_all(self, capsys: Any) -> None:
        """DEBUG level shows all messages."""
        config = LoggingConfig(log_level="DEBUG", enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.debug("debug.message")
        log.info("info.message")

        captured = capsys.readouterr()
        assert "debug.message" in captured.err
        assert "info.message" in captured.err

    def test_error_level_filters_lower(self, capsys: Any) -> None:
        """ERROR level filters out lower level messages."""
        config = LoggingConfig(log_level="ERROR", enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("info.message")
        log.warning("warning.message")
        log.error("error.message")

        captured = capsys.readouterr()
        assert "info.message" not in captured.err
        assert "warning.message" not in captured.err
        assert "error.message" in captured.err


class TestLogRotation:
    """Test log file rotation configuration."""

    def test_log_file_created(self, temp_log_dir: Path) -> None:
        """Log file is created when file logging enabled."""
        config = LoggingConfig(log_dir=temp_log_dir, enable_file_logging=True)
        configure_logging(config)
        log = get_logger()
        log.info("test.file.logging")

        log_file = temp_log_dir / "ouroboros.log"
        assert log_file.exists()

    def test_log_file_contains_message(self, temp_log_dir: Path) -> None:
        """Log file contains logged messages."""
        config = LoggingConfig(log_dir=temp_log_dir, enable_file_logging=True)
        configure_logging(config)
        log = get_logger()
        log.info("unique.test.message.12345")

        # Flush handlers
        for handler in logging.getLogger().handlers:
            handler.flush()

        log_file = temp_log_dir / "ouroboros.log"
        content = log_file.read_text()
        assert "unique.test.message.12345" in content

    def test_no_log_file_when_disabled(self, temp_log_dir: Path) -> None:
        """Log file is not created when file logging disabled."""
        log_subdir = temp_log_dir / "no_logs"
        config = LoggingConfig(log_dir=log_subdir, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()
        log.info("test.no.file")

        # Directory should not exist or be empty
        if log_subdir.exists():
            assert not any(log_subdir.iterdir())


class TestResetLogging:
    """Test reset_logging function."""

    def test_reset_clears_configured_state(self) -> None:
        """reset_logging clears configured state."""
        configure_logging()
        assert is_configured()

        reset_logging()
        assert not is_configured()

    def test_reset_clears_current_config(self) -> None:
        """reset_logging clears current config."""
        configure_logging()
        assert get_current_config() is not None

        reset_logging()
        assert get_current_config() is None

    def test_reset_clears_context(self, capsys: Any) -> None:
        """reset_logging clears bound context."""
        configure_logging(LoggingConfig(enable_file_logging=False))
        bind_context(test_key="test_value")

        reset_logging()
        configure_logging(LoggingConfig(enable_file_logging=False))

        log = get_logger()
        log.info("after.reset")

        captured = capsys.readouterr()
        assert "test_value" not in captured.err


class TestIsConfigured:
    """Test is_configured function."""

    def test_not_configured_initially(self) -> None:
        """is_configured returns False initially."""
        assert not is_configured()

    def test_configured_after_configure_logging(self) -> None:
        """is_configured returns True after configure_logging."""
        configure_logging()
        assert is_configured()


class TestModuleExports:
    """Test module exports and public API."""

    def test_public_api_exports(self) -> None:
        """All public functions are exported from module."""
        from ouroboros.observability import (
            LoggingConfig,
            LogMode,
            bind_context,
            configure_logging,
            get_logger,
            unbind_context,
        )

        # Just verify these can be imported
        assert LogMode is not None
        assert LoggingConfig is not None
        assert configure_logging is not None
        assert get_logger is not None
        assert bind_context is not None
        assert unbind_context is not None


class TestEventNamingConvention:
    """Test that logging supports event naming convention."""

    def test_dot_notation_event_names(self, capsys: Any) -> None:
        """Event names with dot notation are logged correctly."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("ac.execution.started")
        log.info("ontology.concept.added")
        log.info("consensus.voting.completed")

        captured = capsys.readouterr()
        lines = [line for line in captured.err.strip().split("\n") if line]

        events = [json.loads(line)["event"] for line in lines]
        assert "ac.execution.started" in events
        assert "ontology.concept.added" in events
        assert "consensus.voting.completed" in events


class TestExceptionLogging:
    """Test exception logging capabilities."""

    def test_exception_info_logged(self, capsys: Any) -> None:
        """Exception information is included in logs."""
        config = LoggingConfig(mode=LogMode.DEV, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        try:
            raise ValueError("test exception")
        except ValueError:
            log.exception("error.occurred")

        captured = capsys.readouterr()
        assert "error.occurred" in captured.err
        assert "ValueError" in captured.err or "test exception" in captured.err


class TestSensitiveDataMasking:
    """Test that sensitive data is masked in logs."""

    def test_api_key_field_masked(self, capsys: Any) -> None:
        """API key fields are automatically masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("config.loaded", api_key="sk-1234567890abcdef")

        captured = capsys.readouterr()
        output = captured.err
        # Should not contain the actual key
        assert "1234567890" not in output
        # Should contain redacted marker
        assert "REDACTED" in output

    def test_password_field_masked(self, capsys: Any) -> None:
        """Password fields are automatically masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("auth.attempt", password="super_secret_123")

        captured = capsys.readouterr()
        output = captured.err
        assert "super_secret_123" not in output
        assert "REDACTED" in output

    def test_sensitive_value_pattern_masked(self, capsys: Any) -> None:
        """Values matching sensitive patterns are masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("request.headers", some_field="sk-ant-1234567890abcdef")

        captured = capsys.readouterr()
        output = captured.err
        # The full key should not appear
        assert "1234567890abcdef" not in output

    def test_normal_fields_not_masked(self, capsys: Any) -> None:
        """Non-sensitive fields are not masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info("user.action", name="John Doe", email="john@example.com")

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert data["name"] == "John Doe"
        assert data["email"] == "john@example.com"

    def test_nested_sensitive_fields_masked(self, capsys: Any) -> None:
        """Sensitive fields in nested dicts are masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info(
            "config.loaded",
            config={"provider": {"api_key": "sk-secret123"}, "name": "test"}
        )

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert data["config"]["provider"]["api_key"] == "<REDACTED>"
        assert data["config"]["name"] == "test"
