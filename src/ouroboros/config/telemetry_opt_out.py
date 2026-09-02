"""Process-level telemetry opt-out helpers.

Kept out of ``config.loader`` so grandfathered modules do not grow when
setup persists ``DO_NOT_TRACK`` / ``OUROBOROS_TELEMETRY``.
"""

from __future__ import annotations

import os

from ouroboros.config.models import OuroborosConfig, TelemetryConfig


def telemetry_opt_out_environ() -> dict[str, str]:
    """Return normalized process-level telemetry opt-out variables.

    Only environment signals are included. A persisted
    ``telemetry.enabled: false`` is not copied into the mapping: that
    file-level preference already applies in this process, and MCP child
    environments should not invent keys the operator did not set.

    Values are normalized to ``DO_NOT_TRACK=1`` and ``OUROBOROS_TELEMETRY=0``
    so Codex MCP registrations inherit a stable opt-out regardless of the
    original spelling (``true``/``yes``/``off``/``no``).
    """
    env: dict[str, str] = {}
    raw_do_not_track = os.environ.get("DO_NOT_TRACK", "").strip().lower()
    if raw_do_not_track in ("1", "true", "on", "yes"):
        env["DO_NOT_TRACK"] = "1"
    raw_telemetry = os.environ.get("OUROBOROS_TELEMETRY", "").strip().lower()
    if raw_telemetry in ("0", "false", "off", "no"):
        env["OUROBOROS_TELEMETRY"] = "0"
    return env


def with_process_telemetry_opt_out(config: OuroborosConfig) -> OuroborosConfig:
    """Persist an active process opt-out onto a freshly generated config."""
    if not telemetry_opt_out_environ():
        return config
    return config.model_copy(update={"telemetry": TelemetryConfig(enabled=False)})
