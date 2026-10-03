"""The on/off switch for the check package boundary of ``ooo run``.

The check package is on by default for every eligible run. An explicit user
setting turns it off (or on). Precedence, first match wins:

1. the CLI flag ``--check-package`` / ``--no-check-package``;
2. the environment variable ``OUROBOROS_CHECK_PACKAGE=on|off``: only the
   exact value ``on`` turns it on; every other set value (``off``, an alias
   such as ``1``, ``true`` or ``ON``, empty or whitespace) means off, since
   it is an attempt to opt out;
3. ``boundary.check_package: on|off`` in ``~/.ouroboros/config.yaml``;
4. otherwise ``on``.

An unreadable ``config.yaml`` may hold an explicit ``off`` this process cannot
see, so without a flag or environment setting it never yields the default:
the switch is then ``off``. A ``config.yaml`` that does not exist holds no
setting, so the default ``on`` applies. The switch depends on nothing else: not on
telemetry, not on the anonymous ID, not on any notice.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

import structlog

from ouroboros.boundary.base_regression import BASE_REGRESSION_DEFAULT
from ouroboros.boundary.run_wiring import CheckPackageSettings

CHECK_PACKAGE_ENV = "OUROBOROS_CHECK_PACKAGE"

log = structlog.get_logger(__name__)


def parse_switch(value: str) -> bool | None:
    """Parse exactly ``on`` or ``off``; ``None`` for anything else (including empty)."""
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def resolve_switch(
    cli_value: bool | None,
    *,
    configured: str | None,
    config_readable: bool = True,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether the check package runs, with the precedence in the module docstring.

    ``configured`` is ``boundary.check_package`` from config (``"on"``,
    ``"off"``, or ``None`` when unset); ``config_readable`` is false when the
    config could not be loaded.
    """
    if cli_value is not None:
        return cli_value
    env = os.environ if environ is None else environ
    raw = env.get(CHECK_PACKAGE_ENV)
    if raw is not None:
        # A present value decides, whatever it is: one that is not exactly
        # ``on`` or ``off`` (an alias, empty, whitespace) is an attempt to opt out.
        from_env = parse_switch(raw)
        if from_env is None:
            log.warning("boundary.switch.unparsable", variable=CHECK_PACKAGE_ENV)
            return False
        return from_env
    if configured in {"on", "off"}:
        return configured == "on"
    if not config_readable:
        log.warning("boundary.switch.config_unreadable")
        return False
    return True


def _load_boundary_config() -> Any:
    """``boundary`` from ``config.yaml``; its defaults (switch unset) when there is no file.

    Only a file that does not exist is absent: no file can hold an explicit
    ``off``, so the default applies. Anything else that stops the file being
    read (permissions, a dangling link, bad YAML, invalid values) raises, and
    the switch is then ``off``.
    """
    from ouroboros.config.loader import load_config
    from ouroboros.config.models import BoundaryConfig, get_config_dir

    config_path = get_config_dir() / "config.yaml"
    try:
        config_path.lstat()
    except FileNotFoundError:
        return BoundaryConfig()
    return load_config(config_path).boundary


def resolve_check_package_settings(cli_value: bool | None = None) -> CheckPackageSettings:
    """Resolve the switch and the budgets for one run.

    Budgets always come from ``boundary`` in config (their defaults when the
    config cannot be read), and so does ``base_regression`` (unset:
    ``BASE_REGRESSION_DEFAULT``).
    """
    try:
        config = _load_boundary_config()
    except Exception:  # noqa: BLE001 - an unreadable config means no default ``on``
        return CheckPackageSettings(
            enabled=resolve_switch(cli_value, configured=None, config_readable=False)
        )
    return CheckPackageSettings(
        enabled=resolve_switch(cli_value, configured=config.check_package),
        constructor_timeout_seconds=config.constructor_timeout_seconds,
        check_timeout_seconds=config.check_timeout_seconds,
        max_construction_attempts=config.max_construction_attempts,
        base_regression=(
            BASE_REGRESSION_DEFAULT
            if config.base_regression is None
            else config.base_regression == "on"
        ),
    )


__all__ = [
    "CHECK_PACKAGE_ENV",
    "parse_switch",
    "resolve_check_package_settings",
    "resolve_switch",
]
