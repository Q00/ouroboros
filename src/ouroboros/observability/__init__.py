"""Observability module for Ouroboros.

This module provides structured logging and context propagation for
observability across the application.

Main components:
- configure_logging: Set up structlog with appropriate processors
- get_logger: Get a bound logger instance
- bind_context: Bind context variables for cross-async propagation
- unbind_context: Remove bound context variables
"""

from ouroboros.observability.logging import (
    LoggingConfig,
    LogMode,
    bind_context,
    configure_logging,
    get_logger,
    unbind_context,
)

__all__ = [
    "LogMode",
    "LoggingConfig",
    "bind_context",
    "configure_logging",
    "get_logger",
    "unbind_context",
]
