"""Structured logging configuration for Ouroboros.

This module configures structlog with standard processors for structured
logging throughout the application. It supports both development mode
(human-readable console output) and production mode (JSON output).

Features:
- ISO 8601 timestamps
- Log level in all entries
- contextvars integration for cross-async context propagation
- Daily log rotation with configurable retention
- Mode selection via environment variable or config

Standard log keys:
- seed_id: Seed identifier
- ac_id: Atomic Capability identifier
- depth: Current depth in execution tree
- iteration: Iteration number
- tier: PAL routing tier

Event naming convention:
- Use dot.notation (e.g., "ac.execution.started", "ontology.concept.added")
- Format: domain.entity.verb_past_tense

Usage:
    from ouroboros.observability import configure_logging, get_logger, bind_context

    # Configure at application startup
    configure_logging(LoggingConfig(mode=LogMode.DEV))

    # Get a logger
    log = get_logger()

    # Bind context for async propagation
    bind_context(seed_id="seed_123", ac_id="ac_456")

    # Log with standard keys
    log.info("ac.execution.started", depth=2, iteration=1, tier="mini")
"""

from __future__ import annotations

from enum import Enum
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.core.security import (
    is_sensitive_field,
    is_sensitive_value,
    mask_api_key,
)


class LogMode(str, Enum):
    """Logging output mode."""

    DEV = "dev"
    PROD = "prod"


class LoggingConfig(BaseModel):
    """Configuration for structured logging.

    Attributes:
        mode: Output mode (dev for human-readable, prod for JSON).
        log_level: Minimum log level to output.
        log_dir: Directory for log files. Defaults to ~/.ouroboros/logs/.
        max_log_days: Number of days to retain log files. Defaults to 7.
        enable_file_logging: Whether to write logs to files.
    """

    mode: LogMode = Field(default=LogMode.DEV)
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default_factory=lambda: Path.home() / ".ouroboros" / "logs")
    max_log_days: int = Field(default=7, ge=1, le=365)
    enable_file_logging: bool = Field(default=True)

    model_config = {"frozen": True}


# Module-level state for tracking configuration
_configured: bool = False
_current_config: LoggingConfig | None = None


def _get_mode_from_env() -> LogMode:
    """Get logging mode from environment variable.

    Returns:
        LogMode based on OUROBOROS_LOG_MODE environment variable.
        Defaults to DEV if not set or invalid.
    """
    env_mode = os.environ.get("OUROBOROS_LOG_MODE", "dev").lower()
    if env_mode == "prod":
        return LogMode.PROD
    return LogMode.DEV


def _get_log_level(level_str: str) -> int:
    """Convert log level string to logging constant.

    Args:
        level_str: Log level as string (e.g., "INFO", "DEBUG").

    Returns:
        Logging constant (e.g., logging.INFO).
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return level_map.get(level_str.upper(), logging.INFO)


def _setup_file_handler(config: LoggingConfig) -> TimedRotatingFileHandler | None:
    """Set up rotating file handler for log output.

    Args:
        config: Logging configuration.

    Returns:
        Configured TimedRotatingFileHandler or None if file logging disabled.
    """
    if not config.enable_file_logging:
        return None

    # Ensure log directory exists
    config.log_dir.mkdir(parents=True, exist_ok=True)

    # Create log file path with date
    log_file = config.log_dir / "ouroboros.log"

    # Configure rotating file handler
    # - when="midnight" for daily rotation
    # - backupCount controls retention
    handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=config.max_log_days,
        encoding="utf-8",
        utc=True,
    )

    # Set formatter based on mode
    if config.mode == LogMode.PROD:
        # JSON format for production - structlog will handle formatting
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        # Simple format for dev - structlog console renderer handles formatting
        handler.setFormatter(logging.Formatter("%(message)s"))

    handler.setLevel(_get_log_level(config.log_level))

    return handler


def _mask_sensitive_data(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that masks sensitive data in log entries.

    Automatically detects and masks API keys, tokens, and other sensitive
    values to prevent accidental exposure in logs.

    Args:
        _logger: The logger instance (unused).
        _method_name: The log method name (unused).
        event_dict: The event dictionary to process.

    Returns:
        Event dictionary with sensitive values masked.
    """
    for key, value in list(event_dict.items()):
        # Skip standard structlog keys
        if key in ("event", "level", "timestamp", "filename", "lineno"):
            continue

        # Check if field name indicates sensitivity
        if is_sensitive_field(key):
            event_dict[key] = "<REDACTED>"
            continue

        # Check if value looks sensitive
        if isinstance(value, str) and is_sensitive_value(value):
            event_dict[key] = mask_api_key(value)
            continue

        # Recursively handle nested dicts
        if isinstance(value, dict):
            event_dict[key] = _mask_dict_sensitive_data(value)

    return event_dict


def _mask_dict_sensitive_data(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask sensitive data in a dictionary.

    Args:
        data: Dictionary to process.

    Returns:
        Dictionary with sensitive values masked.
    """
    result = {}
    for key, value in data.items():
        if is_sensitive_field(key):
            result[key] = "<REDACTED>"
        elif isinstance(value, str) and is_sensitive_value(value):
            result[key] = mask_api_key(value)
        elif isinstance(value, dict):
            result[key] = _mask_dict_sensitive_data(value)
        else:
            result[key] = value
    return result


def _get_shared_processors() -> list[Any]:
    """Get the shared processor chain for structlog.

    These processors are used for preparing event dicts before final rendering.

    Returns:
        List of structlog processors.
    """
    return [
        # Merge contextvars into event dict (for cross-async context)
        structlog.contextvars.merge_contextvars,
        # Mask sensitive data (API keys, tokens, etc.) - SECURITY
        _mask_sensitive_data,
        # Add log level to all entries
        structlog.processors.add_log_level,
        # Add timestamp in ISO 8601 format
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Add stack info for exceptions
        structlog.processors.StackInfoRenderer(),
        # Add caller info (file, line, function) - useful for debugging
        structlog.processors.CallsiteParameterAdder(
            parameters=[
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
    ]


def _get_console_processors(mode: LogMode) -> list[Any]:
    """Get the processor chain for console output.

    Args:
        mode: Logging output mode.

    Returns:
        List of structlog processors including renderer.
    """
    processors = _get_shared_processors()

    # Format exceptions nicely for console
    processors.append(structlog.processors.format_exc_info)

    # Add final renderer based on mode
    if mode == LogMode.DEV:
        # Human-readable console output for development
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        # JSON output for production
        processors.append(structlog.processors.JSONRenderer())

    return processors


def _get_file_processors() -> list[Any]:
    """Get the processor chain for file output.

    File output always uses JSON format for easy parsing.

    Returns:
        List of structlog processors for file logging.
    """
    processors = _get_shared_processors()

    # Format exceptions for file
    processors.append(structlog.processors.format_exc_info)

    # Always use JSON for file output (for log aggregation tools)
    processors.append(structlog.processors.JSONRenderer())

    return processors


# Global flag to control console log output
_console_logging_enabled: bool = True


def set_console_logging(enabled: bool) -> None:
    """Enable or disable console log output.

    Args:
        enabled: True to enable console logging, False to disable.
    """
    global _console_logging_enabled
    _console_logging_enabled = enabled


def is_console_logging_enabled() -> bool:
    """Check if console logging is enabled.

    Returns:
        True if console logging is enabled.
    """
    return _console_logging_enabled


class _FileWritingPrintLogger:
    """Print logger that also writes to a file handler.

    This logger writes to stdout (for console output) and optionally
    to a file handler for persistent logging. Supports proper log levels.
    """

    def __init__(self, file_handler: TimedRotatingFileHandler | None = None) -> None:
        """Initialize the file-writing print logger.

        Args:
            file_handler: Optional file handler for persistent logging.
        """
        self._file_handler = file_handler

    def _log(self, message: str, level: int = logging.INFO) -> None:
        """Log a message to console and file with proper level.

        Args:
            message: The message to log.
            level: The log level (e.g., logging.DEBUG, logging.INFO).
        """
        import sys

        # Print to stderr only if console logging is enabled
        if _console_logging_enabled:
            print(message, file=sys.stderr)

        # Write to file if handler exists
        if self._file_handler:
            record = logging.LogRecord(
                name="ouroboros",
                level=level,
                pathname="",
                lineno=0,
                msg=message,
                args=(),
                exc_info=None,
            )
            self._file_handler.emit(record)

    def msg(self, message: str) -> None:
        """Log a message to console and file (default INFO level)."""
        self._log(message, logging.INFO)

    # Alias for structlog compatibility
    def __call__(self, message: str) -> None:
        """Log a message (alias for msg)."""
        self.msg(message)

    # Level-specific methods
    def debug(self, message: str) -> None:
        """Log a debug message."""
        self._log(message, logging.DEBUG)

    def info(self, message: str) -> None:
        """Log an info message."""
        self._log(message, logging.INFO)

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self._log(message, logging.WARNING)

    def warn(self, message: str) -> None:
        """Log a warning message (alias for warning)."""
        self._log(message, logging.WARNING)

    def error(self, message: str) -> None:
        """Log an error message."""
        self._log(message, logging.ERROR)

    def critical(self, message: str) -> None:
        """Log a critical message."""
        self._log(message, logging.CRITICAL)

    def fatal(self, message: str) -> None:
        """Log a fatal message (alias for critical)."""
        self._log(message, logging.CRITICAL)

    def exception(self, message: str) -> None:
        """Log an exception message (ERROR level)."""
        self._log(message, logging.ERROR)


class _FileWritingPrintLoggerFactory:
    """Factory for creating file-writing print loggers."""

    def __init__(self, file_handler: TimedRotatingFileHandler | None = None) -> None:
        """Initialize the factory.

        Args:
            file_handler: Optional file handler for persistent logging.
        """
        self._file_handler = file_handler

    def __call__(self, *_args: Any) -> _FileWritingPrintLogger:
        """Create a new logger instance.

        Args:
            *_args: Ignored arguments (structlog may pass logger name).
        """
        return _FileWritingPrintLogger(self._file_handler)


def configure_logging(config: LoggingConfig | None = None) -> None:
    """Configure structlog for the application.

    This should be called once at application startup. It configures:
    - structlog processors (log level, timestamp, stack info)
    - Output renderer (console for dev, JSON for prod)
    - File handler with daily rotation (if enabled)
    - contextvars integration for cross-async context

    Args:
        config: Logging configuration. If None, uses defaults with
               mode from OUROBOROS_LOG_MODE environment variable.

    Example:
        # Use environment variable for mode
        configure_logging()

        # Or specify config explicitly
        configure_logging(LoggingConfig(mode=LogMode.PROD, max_log_days=14))
    """
    global _configured, _current_config

    if config is None:
        config = LoggingConfig(mode=_get_mode_from_env())

    _current_config = config

    # Set up standard library logging
    log_level = _get_log_level(config.log_level)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reconfigure
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add file handler if enabled
    file_handler = _setup_file_handler(config)
    if file_handler:
        root_logger.addHandler(file_handler)

    # Get processors for console output
    processors = _get_console_processors(config.mode)

    # Create logger factory that writes to both console and file
    logger_factory = _FileWritingPrintLoggerFactory(file_handler)

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a bound logger instance.

    If logging has not been configured, this will configure it with defaults.

    Args:
        name: Optional logger name. If not provided, uses the calling module.

    Returns:
        A bound structlog logger.

    Example:
        log = get_logger()
        log.info("ac.execution.started", seed_id="seed_123", ac_id="ac_456")
    """
    global _configured

    if not _configured:
        configure_logging()

    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """Bind context variables for cross-async propagation.

    Context bound here will be included in all subsequent log entries
    within the same async context. This is useful for propagating
    request-scoped data like seed_id, ac_id, etc.

    Standard keys:
    - seed_id: Seed identifier
    - ac_id: Atomic Capability identifier
    - depth: Current depth in execution tree
    - iteration: Iteration number
    - tier: PAL routing tier

    IMPORTANT: Never bind sensitive data (API keys, credentials).

    Args:
        **kwargs: Key-value pairs to bind to the logging context.

    Example:
        bind_context(seed_id="seed_123", ac_id="ac_456", depth=2)

        # All subsequent logs will include these keys
        log.info("ac.execution.started")  # Will include seed_id, ac_id, depth
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def unbind_context(*keys: str) -> None:
    """Remove context variables from the logging context.

    Args:
        *keys: Keys to remove from the context.

    Example:
        unbind_context("ac_id", "depth")
    """
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    """Clear all bound context variables.

    This should be called at the end of a request or execution scope
    to prevent context leakage.

    Example:
        try:
            bind_context(seed_id="seed_123")
            # ... do work ...
        finally:
            clear_context()
    """
    structlog.contextvars.clear_contextvars()


def get_current_config() -> LoggingConfig | None:
    """Get the current logging configuration.

    Returns:
        The current LoggingConfig or None if not configured.
    """
    return _current_config


def is_configured() -> bool:
    """Check if logging has been configured.

    Returns:
        True if configure_logging has been called.
    """
    return _configured


def reset_logging() -> None:
    """Reset logging configuration state.

    This is primarily for testing purposes. It resets the module state
    but does not reconfigure the loggers.
    """
    global _configured, _current_config
    _configured = False
    _current_config = None
    # Clear any bound context
    structlog.contextvars.clear_contextvars()
    # Reset structlog configuration
    structlog.reset_defaults()
