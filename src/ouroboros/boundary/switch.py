"""The mode switch for the check package boundary of ``ooo run``.

The check package runs by default for every eligible run, in ``record`` mode:
it is constructed, admitted, run on the finished workspace, and what it would
have decided is recorded, but it decides nothing (``boundary/authority.py``).
An explicit setting makes it decide (``decide``, or the older spelling ``on``)
or turns it off (``off``). Precedence, first match wins:

1. the CLI flag ``--check-package`` (``decide``) / ``--no-check-package``
   (``off``);
2. the environment variable ``OUROBOROS_CHECK_PACKAGE=decide|on|record|off``:
   only those exact values are read (``on`` is ``decide``); every other set
   value (an alias such as ``1``, ``true`` or ``ON``, empty or whitespace)
   means off, since it is an attempt to opt out;
3. ``boundary.check_package: decide|on|record|off`` in
   ``~/.ouroboros/config.yaml``;
4. otherwise ``record`` (``CHECK_PACKAGE_DEFAULT``).

An unreadable ``config.yaml`` may hold an explicit ``off`` this process cannot
see, so without a flag or environment setting it never yields the default:
the switch is then ``off``. A ``config.yaml`` that does not exist holds no
setting, so the default applies. The switch depends on nothing else: not on
telemetry, not on the anonymous ID, not on any notice.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any, Literal

import structlog

from ouroboros.boundary.base_regression import BASE_REGRESSION_DEFAULT, WORKER_TEST_GATE_DEFAULT
from ouroboros.boundary.run_wiring import CheckPackageSettings

CHECK_PACKAGE_ENV = "OUROBOROS_CHECK_PACKAGE"

CheckPackageMode = Literal["decide", "record", "off"]
"""How the check package acts for one run (``off``: it does not run)."""
CHECK_PACKAGE_DEFAULT: Literal["decide", "record"] = "record"
"""The check package's mode when nothing configures it. The one place it lives."""

_MODES: dict[str, CheckPackageMode] = {
    "on": "decide",
    "decide": "decide",
    "record": "record",
    "off": "off",
}

log = structlog.get_logger(__name__)


def parse_switch(value: str) -> CheckPackageMode | None:
    """Parse exactly ``decide``, ``on`` (``decide``), ``record`` or ``off``; ``None`` otherwise."""
    return _MODES.get(value)


def resolve_switch(
    cli_value: bool | None,
    *,
    configured: str | None,
    config_readable: bool = True,
    environ: Mapping[str, str] | None = None,
) -> CheckPackageMode:
    """How the check package acts, with the precedence in the module docstring.

    ``cli_value`` is the CLI flag (``True``: ``decide``; ``False``: ``off``).
    ``configured`` is ``boundary.check_package`` from config (``"decide"``,
    ``"on"``, ``"record"``, ``"off"``, or ``None`` when unset);
    ``config_readable`` is false when the config could not be loaded.
    """
    if cli_value is not None:
        return "decide" if cli_value else "off"
    env = os.environ if environ is None else environ
    raw = env.get(CHECK_PACKAGE_ENV)
    if raw is not None:
        # A present value decides, whatever it is: one that is not exactly a
        # mode (an alias, empty, whitespace) is an attempt to opt out.
        from_env = parse_switch(raw)
        if from_env is None:
            log.warning("boundary.switch.unparsable", variable=CHECK_PACKAGE_ENV)
            return "off"
        return from_env
    if configured is not None and configured in _MODES:
        return _MODES[configured]
    if not config_readable:
        log.warning("boundary.switch.config_unreadable")
        return "off"
    return CHECK_PACKAGE_DEFAULT


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

    ``enabled`` is whether the package runs and ``mode`` whether it decides
    (``decide``) or only records (``record``). Budgets always come from
    ``boundary`` in config (their defaults when the config cannot be read),
    and so do the artifact check modes
    (``base_regression``, unset ``BASE_REGRESSION_DEFAULT``; ``worker_test_gate``,
    unset ``WORKER_TEST_GATE_DEFAULT``).
    """
    try:
        config = _load_boundary_config()
    except Exception:  # noqa: BLE001 - an unreadable config means no default mode
        mode = resolve_switch(cli_value, configured=None, config_readable=False)
        return CheckPackageSettings(
            enabled=mode != "off", mode="record" if mode == "record" else "decide"
        )
    mode = resolve_switch(cli_value, configured=config.check_package)
    return CheckPackageSettings(
        enabled=mode != "off",
        mode="record" if mode == "record" else "decide",
        constructor_timeout_seconds=config.constructor_timeout_seconds,
        check_timeout_seconds=config.check_timeout_seconds,
        max_construction_attempts=config.max_construction_attempts,
        base_regression=config.base_regression or BASE_REGRESSION_DEFAULT,
        worker_test_gate=config.worker_test_gate or WORKER_TEST_GATE_DEFAULT,
    )


__all__ = [
    "CHECK_PACKAGE_DEFAULT",
    "CHECK_PACKAGE_ENV",
    "CheckPackageMode",
    "parse_switch",
    "resolve_check_package_settings",
    "resolve_switch",
]
