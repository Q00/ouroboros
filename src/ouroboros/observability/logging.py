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

from dataclasses import dataclass
from enum import StrEnum
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import re
import sys
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.core.security import (
    is_credential_shaped,
    is_safe_structured_event_identifier,
    mask_sensitive_value,
    sanitize_for_logging,
)


class LogMode(StrEnum):
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


def _close_root_handlers() -> None:
    """Detach and close all handlers currently attached to the root logger."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if isinstance(handler, _SynchronizedTimedRotatingFileHandler):
            handler.retire()
        root_logger.removeHandler(handler)
        try:
            handler.flush()
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass


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
        Configured TimedRotatingFileHandler or None if file logging is disabled
        or the handler cannot be created.
    """
    if not config.enable_file_logging:
        return None

    try:
        # Ensure log directory exists
        config.log_dir.mkdir(parents=True, exist_ok=True)

        # Create log file path with date
        log_file = config.log_dir / "ouroboros.log"

        # Configure rotating file handler
        # - when="midnight" for daily rotation
        # - backupCount controls retention
        handler = _SynchronizedTimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            interval=1,
            backupCount=config.max_log_days,
            encoding="utf-8",
            utc=True,
        )
    except OSError:
        # File logging should not break imports or test collection when the
        # default log path is unavailable (for example in sandboxed CI).
        return None

    # Set formatter based on mode
    if config.mode == LogMode.PROD:
        # JSON format for production - structlog will handle formatting
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        # Simple format for dev - structlog console renderer handles formatting
        handler.setFormatter(logging.Formatter("%(message)s"))

    handler.setLevel(_get_log_level(config.log_level))

    return handler


_EXCEPTION_SECRET_QUOTED_ASSIGNMENT = re.compile(
    r"""(?i)(["'])(password|passwd|api[-_]?key|access[-_]?token|client[-_]?secret|"""
    r"""authorization|credential|secret|token)\1\s*:\s*(["'])(?:\\.|(?!\3).)*\3"""
)
_EXCEPTION_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|api[-_]?key|access[-_]?token|client[-_]?secret|"
    r"authorization|credential|secret|token)\b\s*[:=]\s*([^\s,;]+)"
)


def _sanitize_exception_text(text: str) -> str:
    """Mask credentials in traceback text without discarding diagnostics."""

    def replace_quoted(match: re.Match[str]) -> str:
        key_quote, key, value_quote = match.group(1), match.group(2), match.group(3)
        return f"{key_quote}{key}{key_quote}: {value_quote}<REDACTED>{value_quote}"

    text = _EXCEPTION_SECRET_QUOTED_ASSIGNMENT.sub(replace_quoted, text)
    text = _EXCEPTION_SECRET_ASSIGNMENT.sub(r"\1=<REDACTED>", text)
    token_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{7,}")

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if (
            match.start() > 0
            and text[match.start() - 1] in ("'", '"')
            and match.end() + 1 < len(text)
            and text[match.end()] == text[match.start() - 1]
            and text[match.end() + 1] == ":"
        ):
            return token
        if is_credential_shaped(token):
            return mask_sensitive_value(token)
        return token

    return token_pattern.sub(replace, text)


def _format_exc_info_safely(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Render reserved ``exc_info`` data, then sanitize its text output."""
    try:
        event_dict = structlog.processors.format_exc_info(_logger, _method_name, event_dict)
    except Exception:
        event_dict.pop("exc_info", None)
        event_dict["exception"] = "<REDACTED>"
        return event_dict
    exception = event_dict.get("exception")
    if isinstance(exception, str):
        event_dict["exception"] = _sanitize_exception_text(exception)
    return event_dict


def _mask_sensitive_data(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return a renderer-safe event dictionary with nested secrets masked.

    ``event`` is structlog's structured event value. String event identifiers
    remain exact unless their content is credential-shaped. Reserved
    ``exc_info`` is retained for the exception formatter and sanitized after
    rendering by ``_format_exc_info_safely``.
    """
    marker = object()
    callsite = dict.pop(event_dict, _CALLSITE_EVENT_KEY, marker)
    event = dict.pop(event_dict, "event", marker)
    exc_info = dict.pop(event_dict, "exc_info", marker)
    sanitized = sanitize_for_logging(event_dict)

    if event is not marker:
        if isinstance(event, str):
            try:
                normalized_event = str.__str__(event)
            except Exception:
                normalized_event = "<REDACTED>"
            sanitized["event"] = (
                normalized_event
                if is_safe_structured_event_identifier(normalized_event)
                else mask_sensitive_value(normalized_event)
                if is_credential_shaped(normalized_event)
                else normalized_event
            )
        else:
            sanitized_event = sanitize_for_logging({"value": event})
            sanitized["event"] = sanitized_event.get("value", "<REDACTED>")
    if exc_info is not marker:
        sanitized["exc_info"] = exc_info
    if callsite is not marker:
        sanitized[_CALLSITE_EVENT_KEY] = callsite
    return sanitized


def _mask_dict_sensitive_data(data: dict[Any, Any]) -> dict[Any, Any]:
    """Recursively sanitize a mapping through the shared logging boundary."""
    return sanitize_for_logging(data)


def _mask_sequence_sensitive_data(
    data: list[Any] | tuple[Any, ...],
) -> list[Any] | tuple[Any, ...]:
    """Sanitize a sequence through the shared renderer boundary."""
    sanitized = sanitize_for_logging({"value": data})["value"]
    assert isinstance(sanitized, (list, tuple))
    return sanitized


@dataclass(frozen=True, slots=True)
class _ApplicationCallsite:
    """Application frame captured before entering the custom processor loop."""

    filename: str
    lineno: int
    func_name: str


_CALLSITE_EVENT_KEY = "_ouroboros_application_callsite"


class _ApplicationCallsiteParameterAdder:
    """Preserve sync callers while retaining structlog's async stack support."""

    def __init__(self) -> None:
        self._fallback = structlog.processors.CallsiteParameterAdder(
            parameters=[
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.LINENO,
                structlog.processors.CallsiteParameter.FUNC_NAME,
            ]
        )

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        callsite = event_dict.pop(_CALLSITE_EVENT_KEY, None)
        if not isinstance(callsite, _ApplicationCallsite):
            return self._fallback(logger, method_name, event_dict)

        event_dict["filename"] = Path(callsite.filename).name
        event_dict["lineno"] = callsite.lineno
        event_dict["func_name"] = callsite.func_name
        return event_dict


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
        _ApplicationCallsiteParameterAdder(),
        # Preserve saved/implicit exception diagnostics, then mask secrets in
        # the generated traceback before any console or file renderer sees it.
        _format_exc_info_safely,
    ]


def _get_console_processors(mode: LogMode) -> list[Any]:
    """Get the processor chain for console output.

    Args:
        mode: Logging output mode.

    Returns:
        List of structlog processors including renderer.
    """
    processors = _get_shared_processors()

    # Add final renderer based on mode
    if mode == LogMode.DEV:
        # Exception data is rendered and sanitized before this final renderer.
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            )
        )
    else:
        processors.append(structlog.processors.JSONRenderer())

    return processors


def _get_file_processors() -> list[Any]:
    """Get the processor chain for file output.

    File output always uses JSON format for easy parsing.

    Returns:
        List of structlog processors for file logging.
    """
    processors = _get_shared_processors()

    # Always use JSON for file output (for log aggregation tools)
    processors.append(structlog.processors.JSONRenderer())

    return processors


# Global flag to control console log output
_console_logging_enabled: bool = True


@dataclass(frozen=True, slots=True)
class _LoggingGeneration:
    """One atomically published logging pipeline generation."""

    min_level: int
    # structlog.testing.capture_logs() temporarily mutates the configured list
    # in place.  Sharing that exact container preserves structlog's public test
    # hook; production transitions still publish a fresh list per generation.
    processors: list[Any]
    file_handler: TimedRotatingFileHandler | None


# Emission holds this lock through handler.emit(), while configure/reset hold
# it through sink publication and old-handler retirement. RLock is required so
# a handler/setup hook that logs during a transition safely reaches the neutral
# sink instead of deadlocking or reopening the retiring file.
_sink_lock = RLock()
_live_generation = _LoggingGeneration(
    min_level=logging.INFO,
    processors=_get_console_processors(LogMode.DEV),
    file_handler=None,
)


_METHOD_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "msg": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "exception": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}


def _capture_sync_application_callsite() -> _ApplicationCallsite | None:
    """Return the sync application frame outside the native structlog wrapper."""
    try:
        frame = sys._getframe(2)
    except ValueError:  # pragma: no cover - CPython always has these frames here.
        return None

    while frame is not None:
        module_name = str(frame.f_globals.get("__name__", ""))
        if module_name == "structlog" or module_name.startswith("structlog."):
            frame = frame.f_back
            continue

        # Native async methods execute _proxy_to_logger in an executor.  In
        # that case structlog's CallsiteParameterAdder uses its preserved
        # calling-stack context; substituting an executor frame would corrupt
        # the already-correct async attribution.
        if module_name == "threading" or module_name.startswith(
            ("asyncio.", "concurrent.futures.")
        ):
            return None

        return _ApplicationCallsite(
            filename=frame.f_code.co_filename,
            lineno=frame.f_lineno,
            func_name=frame.f_code.co_name,
        )

    return None


class _GenerationBoundLogger(
    structlog.make_filtering_bound_logger(logging.NOTSET)  # type: ignore[misc]
):
    """Bind processors, filtering, and destination to one live generation."""

    def _proxy_to_logger(
        self,
        method_name: str,
        event: str | None = None,
        **event_kw: Any,
    ) -> Any:
        # A lazy proxy can materialize immediately before a transition.  The
        # wrapper class is deliberately stable and ignores its captured
        # processor list; selecting the live generation under this lock keeps
        # rendering, level filtering, and destination emission coherent.
        with _sink_lock:
            generation = _live_generation
            if _METHOD_LEVELS.get(method_name, logging.INFO) < generation.min_level:
                return None

            event_dict: Any = self._context.copy()
            event_dict.update(**event_kw)
            if event is not None:
                event_dict["event"] = event

            # capture_logs() intentionally replaces the processor list.  Only
            # attach the private frame marker when our consuming processor is
            # present so the marker cannot leak into captured/public payloads.
            if any(
                isinstance(processor, _ApplicationCallsiteParameterAdder)
                for processor in generation.processors
            ):
                event_dict[_CALLSITE_EVENT_KEY] = _capture_sync_application_callsite()

            try:
                for processor in generation.processors:
                    event_dict = processor(self._logger, method_name, event_dict)

                if isinstance(event_dict, (str, bytes, bytearray)):
                    args, kwargs = (event_dict,), {}
                elif isinstance(event_dict, tuple):
                    args, kwargs = event_dict
                elif isinstance(event_dict, dict):
                    args, kwargs = (), event_dict
                else:
                    raise ValueError(
                        "Last processor didn't return an appropriate value. "
                        "Valid return values are a dict, a tuple of "
                        "(args, kwargs), bytes, or a str."
                    )

                return getattr(self._logger, method_name)(*args, **kwargs)
            except structlog.DropEvent:
                return None

    def is_enabled_for(self, level: int) -> bool:
        """Return whether *level* is enabled in the live generation."""
        with _sink_lock:
            return level >= _live_generation.min_level

    def get_effective_level(self) -> int:
        """Return the live generation's effective level."""
        with _sink_lock:
            return _live_generation.min_level


class _SynchronizedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """A project-owned root handler that cannot emit after retirement."""

    _retired: bool = False

    def handle(self, record: logging.LogRecord) -> bool:
        """Serialize stdlib emission with generation retirement."""
        with _sink_lock:
            if self._retired:
                return False
            return super().handle(record)

    def retire(self) -> None:
        """Prevent already-selected calls from reopening this handler."""
        with _sink_lock:
            self._retired = True


def set_console_logging(enabled: bool) -> None:
    """Enable or disable console log output.

    Args:
        enabled: True to enable console logging, False to disable.
    """
    global _console_logging_enabled
    with _sink_lock:
        _console_logging_enabled = enabled


def is_console_logging_enabled() -> bool:
    """Check if console logging is enabled.

    Returns:
        True if console logging is enabled.
    """
    with _sink_lock:
        return _console_logging_enabled


class _FileWritingPrintLogger:
    """Print logger that also writes to a file handler.

    This logger writes to stderr (for console output, keeping stdout reserved
    for command output) and optionally to a file handler for persistent
    logging. Supports proper log levels.
    """

    def _log(self, message: str, level: int = logging.INFO) -> None:
        """Log a message to console and file with proper level.

        Args:
            message: The message to log.
            level: The log level (e.g., logging.DEBUG, logging.INFO).
        """

        # Snapshot the complete sink exactly once and keep retirement fenced
        # through emit(). Cached proxies therefore cannot observe a new level
        # with an old handler, dereference a removed handler, or reopen a file
        # that reset/reconfigure has just closed.
        with _sink_lock:
            generation = _live_generation
            if level < generation.min_level:
                return

            if _console_logging_enabled:
                print(message, file=sys.stderr)

            if generation.file_handler is not None:
                record = logging.LogRecord(
                    name="ouroboros",
                    level=level,
                    pathname="",
                    lineno=0,
                    msg=message,
                    args=(),
                    exc_info=None,
                )
                generation.file_handler.emit(record)

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

    def __call__(self, *_args: Any) -> _FileWritingPrintLogger:
        """Create a new logger instance.

        Args:
            *_args: Ignored arguments (structlog may pass logger name).
        """
        return _FileWritingPrintLogger()


def default_logging_config() -> LoggingConfig:
    """Return the config ``configure_logging()`` would pick on its own.

    Callers that want to change one field must start from this rather than
    constructing a fresh ``LoggingConfig``: the mode comes from
    ``OUROBOROS_LOG_MODE`` and is an operator override, so building a new config
    with only a level set would silently drop an operator's ``prod`` selection
    back to ``dev``.

    Returns:
        The active config if logging is already configured, otherwise the
        environment-derived default.
    """
    return _current_config or LoggingConfig(mode=_get_mode_from_env())


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
    global _configured, _current_config, _live_generation

    if config is None:
        config = LoggingConfig(mode=_get_mode_from_env())

    log_level = _get_log_level(config.log_level)
    processors = _get_console_processors(config.mode)

    with _sink_lock:
        # Publish a handler-free transition generation before touching the old
        # handler. Re-entrant logs from setup/flush/close remain observable on
        # stderr but can never reopen or append to the retiring file.
        _live_generation = _LoggingGeneration(
            min_level=log_level,
            processors=processors,
            file_handler=None,
        )
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        _close_root_handlers()

        file_handler = _setup_file_handler(config)
        if file_handler:
            root_logger.addHandler(file_handler)

        structlog.configure(
            processors=processors,
            wrapper_class=_GenerationBoundLogger,
            context_class=dict,
            logger_factory=_FileWritingPrintLoggerFactory(),
            # The stable wrapper resolves filtering and processors from the live
            # generation, so even explicitly bound loggers follow later
            # configurations.  Avoiding proxy caching additionally keeps the
            # surrounding structlog factory/context configuration replaceable.
            cache_logger_on_first_use=False,
        )
        _live_generation = _LoggingGeneration(
            min_level=log_level,
            processors=processors,
            file_handler=file_handler,
        )
        _current_config = config
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

    with _sink_lock:
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
    with _sink_lock:
        return _current_config


def is_configured() -> bool:
    """Check if logging has been configured.

    Returns:
        True if configure_logging has been called.
    """
    with _sink_lock:
        return _configured


def reset_logging() -> None:
    """Reset logging configuration state.

    This is primarily for testing purposes. It clears the Ouroboros module
    state and installs a neutral global structlog configuration in place of
    the application one.

    Neutral, not absent. ``structlog.reset_defaults()`` on its own restores
    the *library* defaults, which emit every level — down to debug —
    through a ``PrintLogger`` bound to stdout. A configured Ouroboros
    logger filters at ``LoggingConfig.log_level`` and prints to stderr (see
    ``_FileWritingPrintLogger._log``), so stdout stays reserved for command
    output. Dropping to library defaults therefore makes the process both
    louder and misdirected.

    That gap is not self-healing. Most modules bind their logger once at
    import time via a bare ``structlog.get_logger``, which never routes
    through :func:`get_logger` and so never triggers the lazy
    reconfiguration that clearing ``_configured`` sets up. Once those
    proxies exist, a reset silently redirects the process's logging to
    stdout, which contaminates any CLI result parsed by a caller or
    asserted on by a later test.

    So carry both halves of the contract across the reset: keep writing to
    stderr and never become louder than either INFO or the outgoing
    configuration. Preserving a quieter outgoing level matters because
    resetting an ``ERROR``-level process to ``INFO`` would be its own volume
    regression; clamping a noisier DEBUG process to INFO keeps the neutral
    baseline safe. ``_configured`` stays false and ``_current_config`` stays
    ``None``, so the next :func:`get_logger` still performs a full
    reconfiguration.

    The stream is carried by reusing the project's own factory with no file
    handler, not by handing ``sys.stderr`` to ``structlog.PrintLoggerFactory``.
    Two reasons, both of which a captured stream gets wrong.
    ``_FileWritingPrintLogger._log`` resolves ``sys.stderr`` at emit time, so a
    reset that runs while pytest's ``capsys`` is active cannot pin that
    fixture's buffer into the global configuration and raise on a write after
    its teardown. And that logger is the only one that consults
    ``_console_logging_enabled``, so routing through it keeps
    :func:`set_console_logging` honored across a reset instead of silently
    re-enabling console output.
    """
    global _configured, _current_config, _live_generation
    with _sink_lock:
        # Read before clearing: the reset must not be louder than what it replaces.
        if _current_config is None:
            outgoing_level = _live_generation.min_level
        else:
            outgoing_level = _get_log_level(_current_config.log_level)
        baseline_level = max(logging.INFO, outgoing_level)
        _configured = False
        _current_config = None
        processors = _get_console_processors(LogMode.DEV)
        _live_generation = _LoggingGeneration(
            min_level=baseline_level,
            processors=processors,
            file_handler=None,
        )
        _close_root_handlers()
        structlog.contextvars.clear_contextvars()
        structlog.configure(
            processors=processors,
            wrapper_class=_GenerationBoundLogger,
            context_class=dict,
            # No file handler: the outgoing one was just closed by
            # _close_root_handlers(), and a reset must not hold a resource.
            logger_factory=_FileWritingPrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
