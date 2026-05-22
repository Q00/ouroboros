"""Runtime control substrate (L2 of #1157 / #1172).

v1 ships the smallest possible watchdog:

- One config (``RuntimeControls.session_wall_clock_seconds``).
- One event family (``runtime.watchdog.cancel``).
- One outcome (the session is cancelled when wall-clock budget elapses).

Richer designs (3-timer ``idle``/``no_progress``/``safety``,
4-directive ``WAIT``/``RETRY``/``UNSTUCK``/``CANCEL`` vocabulary,
subscriber pattern, per-layer ad-hoc-timeout deprecation) are
documented as v2 expansion in #1172 and added only when evidence of
a stall slipping past wall-clock-only surfaces.
"""

from ouroboros.runtime.controls import (
    DEFAULT_SESSION_WALL_CLOCK_SECONDS,
    RuntimeControls,
    load_runtime_controls,
)
from ouroboros.runtime.watchdog import (
    WATCHDOG_AGGREGATE_TYPE,
    WATCHDOG_CANCEL_EVENT_TYPE,
    WATCHDOG_STOP_REASON_CODE,
    Watchdog,
    WatchdogDecision,
)

__all__ = [
    "DEFAULT_SESSION_WALL_CLOCK_SECONDS",
    "RuntimeControls",
    "WATCHDOG_AGGREGATE_TYPE",
    "WATCHDOG_CANCEL_EVENT_TYPE",
    "WATCHDOG_STOP_REASON_CODE",
    "Watchdog",
    "WatchdogDecision",
    "load_runtime_controls",
]
