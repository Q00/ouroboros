"""Bridge the saved logging level into the structlog configuration.

Why this exists: two unrelated classes are both named ``LoggingConfig``. The one
in ``ouroboros.config.models`` carries ``level`` and is what ``ouroboros config
set logging.level`` writes to ``~/.ouroboros/config.yaml``. The one in
``ouroboros.observability.logging`` carries ``log_level`` and is what actually
sets the handler level. Nothing connected them, and the CLI only called
``configure_logging`` behind ``--debug``, so the saved value never reached the
logger: ``config set logging.level warning`` reported success, persisted, and
changed nothing about what the CLI printed (#1955).

Every CLI entry point that accepts ``--debug`` should call
:func:`configure_cli_logging` instead of calling ``configure_logging`` directly,
so the two field names cannot drift apart again at the call sites. ``run`` must
call it before importing the orchestrator, whose module-level ``get_logger()``
would otherwise auto-configure logging at the default level first.
"""

from __future__ import annotations

from ouroboros.observability import configure_logging, default_logging_config

__all__ = ["configure_cli_logging", "resolve_cli_log_level"]

_DEBUG_LEVEL = "DEBUG"
_FALLBACK_LEVEL = "INFO"


def resolve_cli_log_level(*, debug: bool) -> str:
    """Resolve the log level for a CLI invocation.

    ``--debug`` always wins so the flag stays usable for reporting a problem
    regardless of what is saved in the config file. Otherwise the saved
    ``logging.level`` applies.

    A config that cannot be read must not take the command down with it --
    logging setup is not the user's goal -- so an unreadable config falls back
    to the previous default of INFO.

    Args:
        debug: Whether the caller passed ``--debug``.

    Returns:
        Log level name in the upper-case form ``configure_logging`` expects.
    """
    if debug:
        return _DEBUG_LEVEL

    # Imported lazily: loading the config touches the filesystem, and the
    # --debug path has no reason to pay for it.
    from ouroboros.config import load_config

    try:
        return str(load_config().logging.level).upper()
    except Exception:
        return _FALLBACK_LEVEL


def configure_cli_logging(*, debug: bool) -> None:
    """Configure structlog for a CLI invocation.

    Only the level is replaced. Everything else -- most importantly the mode
    selected by ``OUROBOROS_LOG_MODE`` -- is carried over from the config
    ``configure_logging()`` would have chosen, so applying a saved level cannot
    knock an operator's ``prod`` JSON output back to human-readable ``dev``.

    Args:
        debug: Whether the caller passed ``--debug``.
    """
    base = default_logging_config()
    configure_logging(base.model_copy(update={"log_level": resolve_cli_log_level(debug=debug)}))
