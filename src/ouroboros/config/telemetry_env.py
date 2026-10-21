"""Telemetry environment opt-out parsing."""

from __future__ import annotations

import os


def telemetry_opt_out_in_env() -> bool:
    """Return whether the process environment disables telemetry."""
    do_not_track = os.environ.get("DO_NOT_TRACK", "").strip().lower()
    if do_not_track in ("1", "true", "on", "yes"):
        return True

    telemetry = os.environ.get("OUROBOROS_TELEMETRY", "").strip().lower()
    return telemetry in ("0", "false", "off", "no")
