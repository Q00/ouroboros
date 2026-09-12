"""Unit tests for ouroboros.observability.logging module."""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from typing import Any
from unittest.mock import patch
import warnings

import pytest
import structlog

import ouroboros.observability.logging as ouroboros_logging
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

    def test_configure_falls_back_when_file_handler_cannot_be_created(
        self, temp_log_dir: Path, capsys: Any
    ) -> None:
        """configure_logging falls back to console logging on file errors."""
        config = LoggingConfig(log_dir=temp_log_dir, enable_file_logging=True)

        with patch.object(
            ouroboros_logging._SynchronizedTimedRotatingFileHandler,
            "__init__",
            side_effect=PermissionError("denied"),
        ):
            configure_logging(config)

        log = get_logger()
        log.info("test.console.fallback")

        captured = capsys.readouterr()
        assert "test.console.fallback" in captured.err


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

    @pytest.mark.asyncio
    async def test_sync_and_async_records_preserve_application_callsite(
        self,
        capsys: Any,
    ) -> None:
        """The generation wrapper is never reported as the event origin."""
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))
        logger = structlog.get_logger("callsite-generation")

        def emit_sync() -> int:
            expected_line = sys._getframe().f_lineno + 1
            logger.info("sync-callsite")
            return expected_line

        async def emit_async() -> int:
            expected_line = sys._getframe().f_lineno + 1
            await logger.ainfo("async-callsite")
            return expected_line

        sync_line = emit_sync()
        async_line = await emit_async()
        payloads = [
            json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
        ]

        assert [payload["filename"] for payload in payloads] == [
            Path(__file__).name,
            Path(__file__).name,
        ]
        assert [payload["lineno"] for payload in payloads] == [sync_line, async_line]
        assert [payload["func_name"] for payload in payloads] == [
            "emit_sync",
            "emit_async",
        ]

    def test_stdlib_record_selected_before_reconfigure_cannot_reopen_old_handler(
        self,
        tmp_path: Path,
    ) -> None:
        """An already-selected root handler must observe retirement."""
        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        configure_logging(LoggingConfig(log_dir=old_dir, enable_file_logging=True))
        old_handler = logging.getLogger().handlers[0]
        original_handle = old_handler.handle
        selected = Event()
        resume = Event()
        failures: list[BaseException] = []

        def delayed_handle(record: logging.LogRecord) -> bool:
            selected.set()
            if not resume.wait(timeout=5):
                raise TimeoutError("test did not resume selected handler")
            return original_handle(record)

        old_handler.handle = delayed_handle  # type: ignore[method-assign]

        def emit() -> None:
            try:
                logging.getLogger("stdlib.reconfigure.overlap").warning(
                    "selected-before-reconfigure"
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        worker = Thread(target=emit)
        worker.start()
        assert selected.wait(timeout=5)
        configure_logging(LoggingConfig(log_dir=new_dir, enable_file_logging=True))
        resume.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert failures == []
        assert "selected-before-reconfigure" not in (old_dir / "ouroboros.log").read_text(
            encoding="utf-8"
        )
        assert old_handler.stream is None

    def test_stdlib_record_selected_before_reset_cannot_reopen_old_handler(
        self,
        tmp_path: Path,
    ) -> None:
        """Reset uses the same retirement fence as reconfiguration."""
        configure_logging(LoggingConfig(log_dir=tmp_path, enable_file_logging=True))
        old_handler = logging.getLogger().handlers[0]
        original_handle = old_handler.handle
        selected = Event()
        resume = Event()

        def delayed_handle(record: logging.LogRecord) -> bool:
            selected.set()
            assert resume.wait(timeout=5)
            return original_handle(record)

        old_handler.handle = delayed_handle  # type: ignore[method-assign]
        worker = Thread(
            target=logging.getLogger("stdlib.reset.overlap").warning,
            args=("selected-before-reset",),
        )
        worker.start()
        assert selected.wait(timeout=5)
        reset_logging()
        resume.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert "selected-before-reset" not in (tmp_path / "ouroboros.log").read_text(
            encoding="utf-8"
        )
        assert old_handler.stream is None

    def test_structlog_pipeline_finishes_one_generation_before_reconfigure(
        self,
        tmp_path: Path,
    ) -> None:
        """An old DEV processor cannot render into the new PROD sink."""
        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        processor_entered = Event()
        resume_processor = Event()
        transition_started = Event()
        transition_done = Event()
        original_processors = ouroboros_logging._get_console_processors

        def pause_processor(
            _logger: Any,
            _method_name: str,
            event_dict: dict[str, Any],
        ) -> dict[str, Any]:
            processor_entered.set()
            assert resume_processor.wait(timeout=5)
            return event_dict

        old_processors = original_processors(LogMode.DEV)
        old_processors.insert(-1, pause_processor)
        with patch(
            "ouroboros.observability.logging._get_console_processors",
            return_value=old_processors,
        ):
            configure_logging(
                LoggingConfig(
                    mode=LogMode.DEV,
                    log_dir=old_dir,
                    enable_file_logging=True,
                )
            )

        proxy = structlog.get_logger("pipeline.reconfigure.overlap")
        emitting = Thread(target=proxy.info, args=("old-generation-record",))
        emitting.start()
        assert processor_entered.wait(timeout=5)

        def reconfigure() -> None:
            transition_started.set()
            configure_logging(
                LoggingConfig(
                    mode=LogMode.PROD,
                    log_dir=new_dir,
                    enable_file_logging=True,
                )
            )
            transition_done.set()

        transition = Thread(target=reconfigure)
        transition.start()
        assert transition_started.wait(timeout=5)
        assert not transition_done.wait(timeout=0.05)
        resume_processor.set()
        emitting.join(timeout=5)
        transition.join(timeout=5)

        assert not emitting.is_alive()
        assert not transition.is_alive()
        assert transition_done.is_set()
        assert "old-generation-record" in (old_dir / "ouroboros.log").read_text(encoding="utf-8")
        assert "old-generation-record" not in (new_dir / "ouroboros.log").read_text(
            encoding="utf-8"
        )

    def test_structlog_pipeline_finishes_one_generation_before_reset(
        self,
        tmp_path: Path,
    ) -> None:
        """Reset waits for processor and destination emission together."""
        processor_entered = Event()
        resume_processor = Event()
        reset_started = Event()
        reset_done = Event()
        original_processors = ouroboros_logging._get_console_processors

        def pause_processor(
            _logger: Any,
            _method_name: str,
            event_dict: dict[str, Any],
        ) -> dict[str, Any]:
            processor_entered.set()
            assert resume_processor.wait(timeout=5)
            return event_dict

        processors = original_processors(LogMode.DEV)
        processors.insert(-1, pause_processor)
        with patch(
            "ouroboros.observability.logging._get_console_processors",
            return_value=processors,
        ):
            configure_logging(LoggingConfig(log_dir=tmp_path))

        proxy = structlog.get_logger("pipeline.reset.overlap")
        emitting = Thread(target=proxy.info, args=("record-before-reset",))
        emitting.start()
        assert processor_entered.wait(timeout=5)

        def reset() -> None:
            reset_started.set()
            reset_logging()
            reset_done.set()

        resetting = Thread(target=reset)
        resetting.start()
        assert reset_started.wait(timeout=5)
        assert not reset_done.wait(timeout=0.05)
        resume_processor.set()
        emitting.join(timeout=5)
        resetting.join(timeout=5)

        assert not emitting.is_alive()
        assert not resetting.is_alive()
        assert reset_done.is_set()
        assert "record-before-reset" in (tmp_path / "ouroboros.log").read_text(encoding="utf-8")

    def test_debug_call_waiting_at_pipeline_boundary_uses_new_debug_generation(
        self,
        capsys: Any,
    ) -> None:
        """A relaxed transition cannot lose a call to a stale INFO wrapper."""
        configure_logging(LoggingConfig(log_level="INFO", enable_file_logging=False))
        proxy = structlog.get_logger("pipeline.level.overlap")
        call_selected = Event()
        resume_call = Event()
        original_proxy = ouroboros_logging._GenerationBoundLogger._proxy_to_logger

        def delayed_proxy(
            logger: Any,
            method_name: str,
            event: str | None = None,
            **event_kw: Any,
        ) -> Any:
            call_selected.set()
            assert resume_call.wait(timeout=5)
            return original_proxy(logger, method_name, event, **event_kw)

        with patch.object(
            ouroboros_logging._GenerationBoundLogger,
            "_proxy_to_logger",
            new=delayed_proxy,
        ):
            worker = Thread(target=proxy.debug, args=("debug-after-relaxation",))
            worker.start()
            assert call_selected.wait(timeout=5)
            configure_logging(LoggingConfig(log_level="DEBUG", enable_file_logging=False))
            resume_call.set()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert "debug-after-relaxation" in capsys.readouterr().err

    def test_reconfigure_reentrant_log_cannot_reopen_retiring_file(
        self,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        configure_logging(LoggingConfig(log_dir=old_dir, enable_file_logging=True))
        proxy = structlog.get_logger("reconfigure.overlap")
        proxy.info("before-reconfigure")
        old_file = old_dir / "ouroboros.log"
        original_setup = ouroboros_logging._setup_file_handler

        def setup_with_reentrant_log(
            config: LoggingConfig,
        ) -> Any:
            proxy.info("during-reconfigure")
            return original_setup(config)

        with patch(
            "ouroboros.observability.logging._setup_file_handler",
            side_effect=setup_with_reentrant_log,
        ):
            configure_logging(LoggingConfig(log_dir=new_dir, enable_file_logging=True))

        proxy.info("after-reconfigure")
        captured = capsys.readouterr()
        assert "during-reconfigure" in captured.err
        assert "during-reconfigure" not in old_file.read_text(encoding="utf-8")
        new_content = (new_dir / "ouroboros.log").read_text(encoding="utf-8")
        assert "after-reconfigure" in new_content

    def test_reset_reentrant_log_cannot_use_retiring_file(
        self,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        configure_logging(LoggingConfig(log_dir=tmp_path, enable_file_logging=True))
        proxy = structlog.get_logger("reset.overlap")
        proxy.info("before-reset")
        log_file = tmp_path / "ouroboros.log"
        original_close = ouroboros_logging._close_root_handlers

        def close_with_reentrant_log() -> None:
            proxy.info("during-reset")
            original_close()

        with patch(
            "ouroboros.observability.logging._close_root_handlers",
            side_effect=close_with_reentrant_log,
        ):
            reset_logging()

        captured = capsys.readouterr()
        assert "during-reset" in captured.err
        assert "during-reset" not in log_file.read_text(encoding="utf-8")

    def test_reset_leaves_import_time_proxies_quiet_on_stdout(self, capsys: Any) -> None:
        """#1794: reset must not restore structlog's print-everything defaults.

        Modules that bind ``structlog.get_logger`` at import time keep logging
        through whatever configuration reset leaves behind, and CLI tests
        reserve stdout for command output — so the post-reset baseline must
        filter below INFO and route to stderr, like a configured logger.
        """
        configure_logging(LoggingConfig(enable_file_logging=False))
        reset_logging()

        proxy = structlog.get_logger("import-time-proxy")
        proxy.debug("debug-should-be-filtered")
        proxy.info("info-should-go-to-stderr")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "debug-should-be-filtered" not in captured.err
        assert "info-should-go-to-stderr" in captured.err

    def test_cached_proxies_follow_more_permissive_reconfiguration(self, capsys: Any) -> None:
        """#1794 round seven: relaxation must work too, not just restriction.

        A proxy materialized under INFO must emit DEBUG after a DEBUG
        reconfiguration — a frozen filtering wrapper would silently defeat an
        operator's debug setting.
        """
        configure_logging(LoggingConfig(log_level="INFO", enable_file_logging=False))
        proxy = structlog.get_logger("relax-proxy")
        proxy.info("materialize-under-info")
        capsys.readouterr()

        reset_logging()
        configure_logging(LoggingConfig(log_level="DEBUG", enable_file_logging=False))

        proxy.debug("debug-after-relaxation")
        captured = capsys.readouterr()
        assert "debug-after-relaxation" in captured.err

    def test_cached_proxies_follow_renderer_reconfiguration(self, capsys: Any) -> None:
        """A DEV-cached proxy must render PROD (JSON) after reconfiguration."""
        configure_logging(
            LoggingConfig(mode=LogMode.DEV, log_level="INFO", enable_file_logging=False)
        )
        proxy = structlog.get_logger("renderer-proxy")
        proxy.info("materialize-under-dev")
        capsys.readouterr()

        reset_logging()
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_level="INFO", enable_file_logging=False)
        )

        proxy.info("render-as-json-now")
        captured = capsys.readouterr()
        assert '"event": "render-as-json-now"' in captured.err

    def test_explicitly_bound_logger_follows_live_generation(self, capsys: Any) -> None:
        """Binding context must not freeze the processor or filtering generation."""
        configure_logging(
            LoggingConfig(
                mode=LogMode.DEV,
                log_level="INFO",
                enable_file_logging=False,
            )
        )
        bound = structlog.get_logger("bound-generation").bind(component="worker")
        bound.info("materialize-bound-under-dev")
        capsys.readouterr()

        configure_logging(
            LoggingConfig(
                mode=LogMode.PROD,
                log_level="DEBUG",
                enable_file_logging=False,
            )
        )
        bound.debug("bound-follows-prod-debug")

        payload = json.loads(capsys.readouterr().err)
        assert payload["event"] == "bound-follows-prod-debug"
        assert payload["component"] == "worker"

    def test_structlog_capture_logs_observes_live_generation(self) -> None:
        """The public structlog capture hook can replace processors in place."""
        from structlog.testing import capture_logs

        configure_logging(LoggingConfig(enable_file_logging=False))
        bound = structlog.get_logger("capture-generation").bind(component="worker")

        with capture_logs() as captured:
            bound.warning("captured-through-live-generation")

        assert captured == [
            {
                "component": "worker",
                "event": "captured-through-live-generation",
                "log_level": "warning",
            }
        ]

    def test_cached_proxies_follow_reconfiguration_after_reset(
        self, capsys: Any, tmp_path: Any
    ) -> None:
        """#1794 round six: reconfiguring must not revive stale cached sinks.

        A proxy cached under DEBUG/file logging, after reset and an
        INFO/no-file reconfiguration, must obey the NEW configuration — no
        DEBUG output, no writes through the old (closed) file handler.
        """
        old_dir = tmp_path / "old-logs"
        configure_logging(
            LoggingConfig(
                log_level="DEBUG",
                enable_file_logging=True,
                log_dir=str(old_dir),
            )
        )
        proxy = structlog.get_logger("stale-proxy")
        proxy.debug("materialize-under-debug-file")
        old_files = list(old_dir.glob("*"))
        sizes_before = {f: f.stat().st_size for f in old_files}
        capsys.readouterr()

        reset_logging()
        configure_logging(LoggingConfig(log_level="INFO", enable_file_logging=False))

        proxy.debug("stale-debug-must-not-emit")
        proxy.info("stale-info-goes-to-current-sink")
        captured = capsys.readouterr()
        assert "stale-debug-must-not-emit" not in captured.err
        assert "stale-debug-must-not-emit" not in captured.out
        assert "stale-info-goes-to-current-sink" in captured.err
        for f, size in sizes_before.items():
            assert f.stat().st_size == size, "old file handler was written after reset"

    def test_reset_baseline_governs_cached_pre_used_proxies(
        self, capsys: Any, tmp_path: Any
    ) -> None:
        """#1794 round five: proxies cached before reset must obey the baseline.

        With cache_logger_on_first_use, a proxy used under DEBUG/file logging
        keeps its bound logger across reset; the baseline contract (never
        below INFO, never the old file handler) must hold for it too.
        """
        configure_logging(
            LoggingConfig(
                log_level="DEBUG",
                enable_file_logging=True,
                log_dir=str(tmp_path),
            )
        )
        proxy = structlog.get_logger("pre-used-proxy")
        proxy.debug("materialize-cache")
        log_files = list(tmp_path.glob("*.log*"))
        sizes_before = {f: f.stat().st_size for f in log_files}
        capsys.readouterr()

        reset_logging()

        proxy.debug("post-reset-debug-must-vanish")
        proxy.info("post-reset-info-stderr-only")
        captured = capsys.readouterr()
        assert "post-reset-debug-must-vanish" not in captured.err
        assert "post-reset-debug-must-vanish" not in captured.out
        assert "post-reset-info-stderr-only" in captured.err
        assert captured.out == ""
        for f, size in sizes_before.items():
            assert f.stat().st_size == size, "the old file handler was reopened"

    def test_reset_baseline_survives_capture_teardown(self) -> None:
        """The baseline must resolve stderr at call time, not at reset time.

        Capturing the concrete stream during reset keeps a capsys/redirect
        buffer past its teardown; the next raw structlog call then raises on
        the closed file.
        """
        import contextlib
        import io

        configure_logging(LoggingConfig(enable_file_logging=False))
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            reset_logging()
        buffer.close()

        proxy = structlog.get_logger("post-capture-proxy")
        proxy.info("logged-after-capture-teardown")

    def test_reset_baseline_honors_console_logging_toggle(self, capsys: Any) -> None:
        configure_logging(LoggingConfig(enable_file_logging=False))
        reset_logging()
        set_console_logging(False)
        try:
            structlog.get_logger("muted-proxy").info("should-be-suppressed")
            captured = capsys.readouterr()
            assert "should-be-suppressed" not in captured.err
            assert captured.out == ""
        finally:
            set_console_logging(True)

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

    def test_reset_does_not_leave_the_process_louder_than_configured(self, capsys: Any) -> None:
        """A reset must not re-enable debug output, least of all on stdout.

        ``structlog.reset_defaults()`` alone restores the library defaults,
        which emit every level through a ``PrintLogger`` bound to stdout.
        Most modules bind their logger once at import time with a bare
        ``structlog.get_logger``, so they never route through
        :func:`get_logger` and never trigger the lazy reconfiguration that
        clearing ``_configured`` sets up — they just keep logging through
        whatever the reset left behind.

        The observable damage is stdout contamination: ``ouroboros job
        wait`` emits debug events, and its output is parsed by callers and
        asserted on verbatim in ``tests/unit/cli``. Because a reset only
        happens in tests, this surfaced as an order-dependent failure in
        the full parallel suite rather than anywhere reproducible.
        """
        configure_logging(LoggingConfig(enable_file_logging=False))
        reset_logging()

        structlog.get_logger("reset.probe").debug("reset.debug.must.stay.silent")

        captured = capsys.readouterr()
        assert "reset.debug.must.stay.silent" not in captured.out
        assert "reset.debug.must.stay.silent" not in captured.err

    def test_reset_keeps_emitted_levels_off_stdout(self, capsys: Any) -> None:
        """Whatever survives the level filter must still avoid stdout.

        Filtering debug is only half the contract. structlog's default
        ``PrintLoggerFactory`` writes to stdout, so an INFO record emitted
        after a reset still lands in command output — the same
        contamination as the debug case, one level up.
        """
        configure_logging(LoggingConfig(enable_file_logging=False))
        reset_logging()

        structlog.get_logger("reset.probe").info("reset.info.belongs.on.stderr")

        captured = capsys.readouterr()
        assert "reset.info.belongs.on.stderr" not in captured.out
        assert "reset.info.belongs.on.stderr" in captured.err

    def test_reset_preserves_a_quieter_configured_level(self, capsys: Any) -> None:
        """Resetting an ERROR-level process must not restore it to INFO.

        Assuming the ``LoggingConfig`` default on reset is a volume
        regression in its own right for any process configured quieter
        than INFO.
        """
        configure_logging(LoggingConfig(enable_file_logging=False, log_level="ERROR"))
        reset_logging()

        structlog.get_logger("reset.quiet.probe").info("reset.info.must.stay.silent")

        captured = capsys.readouterr()
        assert "reset.info.must.stay.silent" not in captured.out
        assert "reset.info.must.stay.silent" not in captured.err

    def test_repeated_reset_preserves_quiet_neutral_generation(self, capsys: Any) -> None:
        """A second reset cannot relax an already-neutral ERROR generation."""
        configure_logging(
            LoggingConfig(
                mode=LogMode.PROD,
                enable_file_logging=False,
                log_level="ERROR",
            )
        )
        bound = structlog.get_logger("repeated-reset").bind(component="worker")

        reset_logging()
        bound.error("after-first-reset")
        first = capsys.readouterr()
        reset_logging()
        bound.info("info-after-second-reset-must-stay-silent")
        bound.error("after-second-reset")
        second = capsys.readouterr()

        assert first.out == second.out == ""
        assert "after-first-reset" in first.err
        assert "after-second-reset" in second.err
        assert "info-after-second-reset-must-stay-silent" not in second.err
        assert '"event":' not in first.err
        assert '"event":' not in second.err

    def test_reset_resolves_the_stream_at_emit_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reset must not pin whatever ``sys.stderr`` happened to be.

        A reset that runs under a capture fixture (``capsys``, or any
        ``redirect_stderr``) would otherwise bind that fixture's buffer into
        the global configuration and keep writing to it after teardown --
        raising on a closed buffer, or silently swallowing later output.
        Rebinding ``sys.stderr`` after the reset and asserting the new stream
        receives the record is the direct proof that the lookup is late.
        """
        configure_logging(LoggingConfig(enable_file_logging=False, log_level="INFO"))
        reset_logging()

        replacement = io.StringIO()
        monkeypatch.setattr(sys, "stderr", replacement)
        structlog.get_logger("reset.stream.probe").info("reset.late.bound.stream")

        assert "reset.late.bound.stream" in replacement.getvalue()

    def test_reset_keeps_console_suppression(self, capsys: Any) -> None:
        """``set_console_logging(False)`` must survive the reset.

        Only ``_FileWritingPrintLogger`` consults ``_console_logging_enabled``.
        Reconfiguring onto a plain ``structlog.PrintLoggerFactory`` would route
        around that gate and silently re-enable console output for a process
        that had asked for silence.
        """
        configure_logging(LoggingConfig(enable_file_logging=False, log_level="INFO"))
        set_console_logging(False)
        try:
            reset_logging()
            structlog.get_logger("reset.console.probe").info("reset.console.must.stay.off")

            captured = capsys.readouterr()
            assert "reset.console.must.stay.off" not in captured.out
            assert "reset.console.must.stay.off" not in captured.err
        finally:
            set_console_logging(True)


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

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                raise ValueError("test exception")
            except ValueError:
                log.exception("error.occurred")

        captured = capsys.readouterr()
        assert "error.occurred" in captured.err
        assert "ValueError" in captured.err or "test exception" in captured.err
        assert not any("pretty exceptions" in str(w.message) for w in caught)


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

        log.info("config.loaded", config={"provider": {"api_key": "sk-secret123"}, "name": "test"})

        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert data["config"]["provider"]["api_key"] == "<REDACTED>"
        assert data["config"]["name"] == "test"

    def test_secrets_in_list_masked_live(self, capsys: Any) -> None:
        """Secrets nested inside lists are masked in live PROD JSON logging."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info(
            "config.loaded",
            providers=[{"api_key": "sk-live-abc123"}, {"api_key": "sk-live-def456"}],
        )

        captured = capsys.readouterr()
        output = captured.err.strip()
        # The full keys should not appear verbatim
        assert "sk-live-abc123" not in output
        assert "sk-live-def456" not in output
        data = json.loads(output)
        # The list structure should be preserved with keys masked
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) == 2
        assert data["providers"][0]["api_key"] == "<REDACTED>"
        assert data["providers"][1]["api_key"] == "<REDACTED>"

    def test_secrets_in_tuple_masked_live(self, capsys: Any) -> None:
        """Secrets nested inside tuples are masked in live PROD JSON logging."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info(
            "auth.config",
            credentials=("sk-live-secret1", "sk-live-secret2"),
        )

        captured = capsys.readouterr()
        output = captured.err.strip()
        assert "sk-live-secret1" not in output
        assert "sk-live-secret2" not in output

    def test_deeply_nested_list_secrets_masked_live(self, capsys: Any) -> None:
        """Deeply nested secrets in list-of-list structures are masked."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info(
            "multi.provider",
            groups=[
                [{"token": "ghp_secrettoken123"}],
                {"nested_list": ["sk-live-deeply-nested"]},
            ],
        )

        captured = capsys.readouterr()
        output = captured.err.strip()
        assert "ghp_secrettoken123" not in output
        assert "sk-live-deeply-nested" not in output

    def test_hostile_str_subclass_secret_not_leaked_live(self, capsys: Any) -> None:
        """A hostile str subclass overriding __getitem__ cannot leak secrets through masking."""

        class HostileKey(str):
            """Subclass that tries to leak the full secret through indexing."""

            def __getitem__(self, idx: Any) -> str:
                # Return the full secret regardless of the index
                return str.__str__(self)

            def index(self, sub: str, *args: Any) -> int:
                # Lie about dash position to disrupt prefix detection
                return 0

        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        hostile_secret = HostileKey("sk-live-abc123def456ghi789")
        log.info("auth.check", token=hostile_secret)

        captured = capsys.readouterr()
        output = captured.err.strip()
        # The full secret must NEVER appear in the output
        assert "sk-live-abc123def456ghi789" not in output

    def test_structured_event_payload_secrets_masked_live(self, capsys: Any) -> None:
        """Non-string positional event payloads containing secrets are sanitized."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        # structlog stores a non-string first positional arg under "event"
        log.info([{"api_key": "sk-live-eventsecret"}])

        captured = capsys.readouterr()
        output = captured.err.strip()
        # The secret nested inside a structured event value must not leak
        assert "sk-live-eventsecret" not in output

    def test_structured_dict_event_secrets_masked_live(self, capsys: Any) -> None:
        """Dict event payloads stored under 'event' key are sanitized."""
        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        log.info({"credentials": {"api_key": "sk-live-dictevent"}})

        captured = capsys.readouterr()
        output = captured.err.strip()
        assert "sk-live-dictevent" not in output

    def test_plain_string_event_secret_masked_live(self, capsys: Any) -> None:
        """A credential used as the event string is masked in PROD JSON."""
        secret = "sk-live-plain-event-secret"
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info(secret)

        output = capsys.readouterr().err.strip()
        assert secret not in output

    def test_jwt_shaped_event_masked_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Structured-event compatibility never overrides credential redaction."""
        secret = "abcdefgh.ijklmnop.qrstuvwx"
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        get_logger().info(secret)

        console = capsys.readouterr().err.strip()
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8").strip()
        assert secret not in console
        assert secret not in persistent
        assert json.loads(console)["event"] != secret
        assert json.loads(persistent)["event"] != secret

    def test_namespace_shaped_event_credentials_masked_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Semantic credential namespaces never bypass event redaction."""
        secrets = (
            "api_key:opaque-provider-credential",
            "access_token/opaque-provider-credential",
            "credentials:opaquevalue",
            "workflow.api_key_sk_live_abc123",
            "workflow.access_token_ghp_credentialshapedvalue",
            "workflow.client_secret_abc123",
        )
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        log = get_logger()
        for secret in secrets:
            log.info(secret)

        console = capsys.readouterr().err
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8")
        for secret in secrets:
            assert secret not in console
            assert secret not in persistent

    def test_cyclic_structured_event_safe_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """A cyclic positional event produces a safe record instead of raising."""
        cycle: list[Any] = []
        cycle.append(cycle)
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        get_logger().info(cycle)

        console = capsys.readouterr().err.strip()
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8").strip()
        assert json.loads(console)["event"] == ["<REDACTED>"]
        assert json.loads(persistent)["event"] == ["<REDACTED>"]

    def test_hostile_sensitive_mapping_key_masked_live(self, capsys: Any) -> None:
        """A string-subclass key cannot bypass sensitive-field detection."""

        class HostileKey(str):
            def lower(self) -> str:
                return "safe"

        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info("config.loaded", config={HostileKey("api_key"): "hunter2"})

        output = capsys.readouterr().err.strip()
        assert "hunter2" not in output
        assert json.loads(output)["config"]["api_key"] == "<REDACTED>"

    def test_credential_shaped_nested_mapping_key_masked_live(self, capsys: Any) -> None:
        """A credential mapping key and its paired value cannot reach PROD JSON."""
        secret = "sk-live-abc123def456ghi789"
        paired_value = "value-paired-with-secret-key"
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info("config.loaded", config={secret: paired_value})

        output = capsys.readouterr().err.strip()
        assert secret not in output
        assert paired_value not in output
        assert json.loads(output)["config"] == {"<REDACTED>": "<REDACTED>"}

    def test_unsupported_nested_mapping_key_does_not_abort_live_logging(self, capsys: Any) -> None:
        """Arbitrary object keys become safe placeholders before JSON rendering."""
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info("config.loaded", config={object(): "safe"})

        output = capsys.readouterr().err.strip()
        assert json.loads(output)["config"] == {"<unsupported-key>": "safe"}

    def test_top_level_credential_key_and_paired_value_masked_live(self, capsys: Any) -> None:
        """Top-level credential-shaped keys fail closed before rendering."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        paired_value = "value-paired-with-top-level-secret-key"
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info("config.loaded", **{secret: paired_value})

        output = capsys.readouterr().err.strip()
        assert secret not in output
        assert paired_value not in output
        assert json.loads(output)["<REDACTED>"] == "<REDACTED>"

    def test_arbitrary_scalar_fields_never_reach_renderer_repr(self, capsys: Any) -> None:
        """Ordinary structured fields redact objects before JSON fallback repr."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"

        class LeakyScalar:
            def __repr__(self) -> str:
                return secret

        class RaisingScalar:
            def __repr__(self) -> str:
                raise RuntimeError("hostile repr")

        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info("config.loaded", leaky=LeakyScalar(), raising=RaisingScalar())

        output = capsys.readouterr().err.strip()
        assert secret not in output
        data = json.loads(output)
        assert data["leaky"] == "<REDACTED>"
        assert data["raising"] == "<REDACTED>"

    def test_hostile_container_protocols_do_not_abort_live_logging(self, capsys: Any) -> None:
        """List iteration and dict items overrides cannot suppress a log event."""

        class HostileDict(dict):
            def items(self):
                raise RuntimeError("hostile items")

        class HostileList(list):
            def __iter__(self):
                raise RuntimeError("hostile iteration")

        secret = "sk-live-hostile-container-secret"
        configure_logging(LoggingConfig(mode=LogMode.PROD, enable_file_logging=False))

        get_logger().info(
            "config.loaded",
            payload=HostileDict({"values": HostileList([secret, "safe"])}),
        )

        output = capsys.readouterr().err.strip()
        assert secret not in output
        assert json.loads(output)["payload"]["values"][1] == "safe"

    def test_blockers_are_sanitized_in_persistent_json_log(self, temp_log_dir: Path) -> None:
        """The same sanitized event reaches the live file sink without secrets."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        paired_value = "persistent-paired-secret-value"

        class LeakyScalar:
            def __repr__(self) -> str:
                return secret

        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        get_logger().info(
            "config.loaded",
            scalar=LeakyScalar(),
            **{secret: paired_value},
        )

        content = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8")
        assert secret not in content
        assert paired_value not in content
        data = json.loads(content)
        assert data["<REDACTED>"] == "<REDACTED>"
        assert data["scalar"] == "<REDACTED>"

    def test_spoofed_namedtuple_cannot_leak_to_console_or_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Tuple subclasses become plain tuples before either JSON renderer runs."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"

        class SpoofedNamedTuple(tuple):
            _fields = ("left", "right")

            def __iter__(self):
                return iter((secret, "renderer-controlled"))

        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        get_logger().info(
            "tuple.test",
            data=SpoofedNamedTuple(("safe-left", "safe-right")),
        )

        console = capsys.readouterr().err.strip()
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8")
        assert secret not in console
        assert secret not in persistent
        assert json.loads(console)["data"] == ["safe-left", "safe-right"]
        assert json.loads(persistent)["data"] == ["safe-left", "safe-right"]

    def test_event_identifiers_remain_exact_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Credential-like event namespaces remain stable query contracts."""
        events = (
            "mcp.auth.invalid_api_key",
            "mcp.auth.token_valid",
            "context.token_count.failed",
        )
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        log = get_logger()
        for event in events:
            log.info(event)

        console_events = [
            json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()
        ]
        file_events = [
            json.loads(line)["event"]
            for line in (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8").splitlines()
        ]
        assert console_events == list(events)
        assert file_events == list(events)

    def test_saved_and_implicit_exceptions_are_sanitized_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Every supported exc_info form keeps diagnostics without leaking messages."""
        secret = "sk-live-exception-secret-abc123"
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )
        log = get_logger()

        try:
            raise ValueError(f"saved object failed: {secret}")
        except ValueError as exc:
            saved_exception = exc
            saved_tuple = (type(exc), exc, exc.__traceback__)

        log.error("saved.exception.object", exc_info=saved_exception)
        log.error("saved.exception.tuple", exc_info=saved_tuple)
        try:
            raise RuntimeError(f"implicit failure: {secret}")
        except RuntimeError:
            log.exception("implicit.exception")

        console = capsys.readouterr().err
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8")
        for output in (console, persistent):
            assert secret not in output
            records = [json.loads(line) for line in output.splitlines()]
            assert [record["event"] for record in records] == [
                "saved.exception.object",
                "saved.exception.tuple",
                "implicit.exception",
            ]
            assert "ValueError" in records[0]["exception"]
            assert "saved object failed" in records[0]["exception"]
            assert "ValueError" in records[1]["exception"]
            assert "RuntimeError" in records[2]["exception"]
            assert "implicit failure" in records[2]["exception"]

    def test_quoted_exception_mapping_secrets_sanitized_in_console_and_file(
        self, capsys: Any, temp_log_dir: Path
    ) -> None:
        """Quoted sensitive mapping values in exception text are redacted."""
        api_key = "not-shaped-sensitive-value"
        password = "ordinary-password-value"
        configure_logging(
            LoggingConfig(mode=LogMode.PROD, log_dir=temp_log_dir, enable_file_logging=True)
        )

        try:
            raise ValueError({"api_key": api_key, "password": password})
        except ValueError:
            get_logger().exception("mapping.exception")

        console = capsys.readouterr().err
        persistent = (temp_log_dir / "ouroboros.log").read_text(encoding="utf-8")
        for output in (console, persistent):
            assert api_key not in output
            assert password not in output
            record = json.loads(output.strip())
            assert record["event"] == "mapping.exception"
            assert "api_key" in record["exception"]
            assert "password" in record["exception"]
            assert record["exception"].count("<REDACTED>") >= 2

    def test_custom_tuple_subclass_make_failure_does_not_crash(self, capsys: Any) -> None:
        """A custom tuple subclass with a broken _make() degrades gracefully."""

        class BadTuple(tuple):
            @classmethod
            def _make(cls, _iterable: Any) -> BadTuple:
                raise TypeError("intentionally broken _make")

        config = LoggingConfig(mode=LogMode.PROD, enable_file_logging=False)
        configure_logging(config)
        log = get_logger()

        # The processor should not raise; it falls back to plain tuple
        broken = BadTuple(("sk-live-tuplesecret", "safe_value"))
        log.info("tuple.test", data=broken)

        captured = capsys.readouterr()
        output = captured.err.strip()
        # Secret must still be masked even when _make fails
        assert "sk-live-tuplesecret" not in output
        # The log line should still be valid JSON
        data = json.loads(output)
        assert "data" in data
