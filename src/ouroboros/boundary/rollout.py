"""Randomized default for the check package boundary of ``ooo run``.

The check package is a product default under evaluation. Each installation is
assigned an arm, ``on`` or ``off``, and every explicit user setting overrides
that assignment. Precedence, first match wins:

1. the CLI flag ``--check-package`` / ``--no-check-package``;
2. the environment variable ``OUROBOROS_CHECK_PACKAGE=on|off``;
3. ``boundary.check_package: on|off`` in ``~/.ouroboros/config.yaml``;
4. the randomized arm, a deterministic function of the anonymous telemetry ID
   (see ``randomized_arm``), used only when telemetry is enabled, the ID
   already exists, and the installation was shown the current telemetry
   notice (which discloses randomized defaults);
5. otherwise ``off``, recorded as ``fallback``.

Installs that send no telemetry therefore always keep the previous behavior
unless they opt in; nobody is randomized without the disclosure. The arm and
its source are recorded on the run's ``workflow_outcome`` (TELEMETRY.md,
"Randomized defaults").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os

CHECK_PACKAGE_ENV = "OUROBOROS_CHECK_PACKAGE"
# Changing the key starts a new, independent randomization; bump it (and the
# TELEMETRY.md changelog) when the evaluated default changes materially.
CHECK_PACKAGE_EXPERIMENT_KEY = "ouroboros.check_package_default.v1"
# Share of randomized installations assigned ``on``, in percent.
CHECK_PACKAGE_ON_PERCENT = 50


class Arm(StrEnum):
    """Whether the check package boundary runs for this installation."""

    ON = "on"
    OFF = "off"


class AssignmentSource(StrEnum):
    """Why the arm has its value."""

    RANDOMIZED = "randomized"
    USER_FORCED_ON = "user_forced_on"
    USER_FORCED_OFF = "user_forced_off"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class CheckPackageAssignment:
    """The resolved arm for one run and where it came from."""

    arm: Arm
    source: AssignmentSource

    @property
    def enabled(self) -> bool:
        return self.arm is Arm.ON


def parse_switch(value: str) -> bool | None:
    """Parse an on/off spelling; ``None`` for anything else (including empty)."""
    normalized = value.strip().lower()
    if normalized in {"on", "1", "true", "yes"}:
        return True
    if normalized in {"off", "0", "false", "no"}:
        return False
    return None


def randomized_arm(
    installation_id: str,
    *,
    experiment_key: str = CHECK_PACKAGE_EXPERIMENT_KEY,
    on_percent: int = CHECK_PACKAGE_ON_PERCENT,
) -> Arm:
    """Map an anonymous installation ID to an arm, deterministically.

    SHA-256 of ``experiment_key``, a NUL separator, and the ID; the first eight
    bytes modulo 100 give a bucket in ``[0, 100)``, and buckets below
    ``on_percent`` are ``on``. The same ID always gets the same arm.
    """
    digest = hashlib.sha256(f"{experiment_key}\0{installation_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    return Arm.ON if bucket < on_percent else Arm.OFF


def _forced(enabled: bool) -> CheckPackageAssignment:
    if enabled:
        return CheckPackageAssignment(Arm.ON, AssignmentSource.USER_FORCED_ON)
    return CheckPackageAssignment(Arm.OFF, AssignmentSource.USER_FORCED_OFF)


def _telemetry_rollout_identity() -> str | None:
    from ouroboros import telemetry

    return telemetry.rollout_identity()


def resolve_check_package_assignment(
    cli_value: bool | None,
    *,
    configured: str | None,
    environ: Mapping[str, str] | None = None,
    identity_reader: Callable[[], str | None] = _telemetry_rollout_identity,
) -> CheckPackageAssignment:
    """Resolve the arm with the precedence in the module docstring.

    ``configured`` is ``boundary.check_package`` from config (``"on"``,
    ``"off"``, or ``None`` when unset). ``identity_reader`` returns the
    anonymous ID eligible for randomization, or ``None``; it never creates one.
    """
    if cli_value is not None:
        return _forced(cli_value)
    env = os.environ if environ is None else environ
    from_env = parse_switch(env.get(CHECK_PACKAGE_ENV, ""))
    if from_env is not None:
        return _forced(from_env)
    if configured in {"on", "off"}:
        return _forced(configured == "on")
    try:
        installation_id = identity_reader()
    except Exception:  # noqa: BLE001 - the assignment must never fail a run
        installation_id = None
    if installation_id:
        return CheckPackageAssignment(randomized_arm(installation_id), AssignmentSource.RANDOMIZED)
    return CheckPackageAssignment(Arm.OFF, AssignmentSource.FALLBACK)


__all__ = [
    "CHECK_PACKAGE_ENV",
    "CHECK_PACKAGE_EXPERIMENT_KEY",
    "CHECK_PACKAGE_ON_PERCENT",
    "Arm",
    "AssignmentSource",
    "CheckPackageAssignment",
    "parse_switch",
    "randomized_arm",
    "resolve_check_package_assignment",
]
