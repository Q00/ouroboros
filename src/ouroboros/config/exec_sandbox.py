"""The execution-sandbox switch: ``OUROBOROS_EXEC_SANDBOX``, then config.

``ouroboros.runtime.exec_sandbox`` confines the commands the controller runs
on its own authority and reads no configuration; its caller passes
``enabled``. This module is where that choice comes from. The sandbox is on
by default. ``OUROBOROS_EXEC_SANDBOX=off`` (or ``0``, ``false``, ``no``) or
``execution.exec_sandbox: false`` switches it off, which is unsafe: the
commands then run unconfined. The environment variable wins over the config
file, and a project ``.env`` cannot set it (``config/untrusted_env.py``).
A configuration that is missing or cannot be loaded keeps the sandbox on.

The orchestrator reads it once per run and seals it in the
execution-semantics contract, so a resumed run keeps the policy it started
with.
"""

from __future__ import annotations

import os

EXEC_SANDBOX_ENV_VAR = "OUROBOROS_EXEC_SANDBOX"


def exec_sandbox_config_enabled() -> bool:
    """``execution.exec_sandbox`` from ``~/.ouroboros/config.yaml``; True unless false."""
    from ouroboros.config.loader import load_config
    from ouroboros.core.errors import ConfigError

    try:
        return load_config().execution.exec_sandbox is not False
    except (ConfigError, OSError):
        return True


def exec_sandbox_enabled() -> bool:
    """The live sandbox policy: False only when the unsafe off switch is set."""
    raw = os.environ.get(EXEC_SANDBOX_ENV_VAR, "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return exec_sandbox_config_enabled()


__all__ = ["EXEC_SANDBOX_ENV_VAR", "exec_sandbox_config_enabled", "exec_sandbox_enabled"]
