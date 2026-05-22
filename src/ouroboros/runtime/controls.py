"""Runtime control configuration (L2 v1).

A single wall-clock budget per ``ooo auto`` session. Loaded from a
YAML fixture in tests; falls back to :data:`DEFAULT_SESSION_WALL_CLOCK_SECONDS`
when the caller does not supply one.

The v2 expansion path (idle / no-progress / safety split, directive
vocabulary, subscriber pattern) is intentionally absent here — adding
fields prematurely is the same over-engineering reflex that produced
the earlier draft this minimal version supersedes. See #1172 for the
deferral rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DEFAULT_SESSION_WALL_CLOCK_SECONDS",
    "RuntimeControls",
    "load_runtime_controls",
]

DEFAULT_SESSION_WALL_CLOCK_SECONDS: int = 4 * 60 * 60  # 4 hours
"""Default per-session wall-clock budget. Generous enough for canonical
cli-todo / webhook-receiver / refactor-in-place runs, tight enough that
a truly hung session is caught within an operator's working day.

Set to ``0`` to disable the wall-clock watchdog. The dataclass invariant
rejects negative values."""


@dataclass(frozen=True, slots=True)
class RuntimeControls:
    """Frozen v1 runtime-control configuration.

    Attributes
    ----------
    session_wall_clock_seconds:
        Maximum elapsed wall-clock seconds before the watchdog cancels
        the session. ``0`` disables the watchdog entirely (no
        cancellation regardless of elapsed time). Negative values are
        rejected at construction time.
    """

    session_wall_clock_seconds: int = DEFAULT_SESSION_WALL_CLOCK_SECONDS

    def __post_init__(self) -> None:
        if self.session_wall_clock_seconds < 0:
            msg = (
                "RuntimeControls.session_wall_clock_seconds must be >= 0; "
                f"got {self.session_wall_clock_seconds}"
            )
            raise ValueError(msg)

    @property
    def watchdog_enabled(self) -> bool:
        """True iff the wall-clock watchdog should fire."""
        return self.session_wall_clock_seconds > 0


def load_runtime_controls(path: Path | str | None = None) -> RuntimeControls:
    """Load :class:`RuntimeControls` from a YAML fixture at *path*.

    The expected YAML layout (kept intentionally narrow for v1):

    .. code-block:: yaml

        runtime_controls:
          session_wall_clock_seconds: 14400   # 4h default

    Behaviour:

    - ``path is None`` → return ``RuntimeControls()`` with defaults.
    - ``path`` exists but no ``runtime_controls`` key → defaults.
    - YAML parse error → ``ValueError`` so callers fail loudly.
    - Unrecognized keys under ``runtime_controls`` → ``ValueError``;
      the v2 expansion path adds new keys only through code review.
    """
    if path is None:
        return RuntimeControls()
    resolved = Path(path)
    if not resolved.is_file():
        msg = f"runtime_controls config not found at {resolved}"
        raise FileNotFoundError(msg)
    try:
        raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"runtime_controls config at {resolved} does not parse as YAML: {exc}"
        raise ValueError(msg) from exc
    if raw is None:
        return RuntimeControls()
    if not isinstance(raw, dict):
        msg = (
            f"runtime_controls config at {resolved} must be a YAML mapping; "
            f"got {type(raw).__name__}"
        )
        raise ValueError(msg)
    block: Any = raw.get("runtime_controls", {})
    if not isinstance(block, dict):
        msg = f"runtime_controls block at {resolved} must be a mapping; got {type(block).__name__}"
        raise ValueError(msg)
    allowed = {"session_wall_clock_seconds"}
    unknown = set(block.keys()) - allowed
    if unknown:
        msg = (
            f"runtime_controls config at {resolved} contains unknown keys "
            f"{sorted(unknown)}; allowed keys: {sorted(allowed)}"
        )
        raise ValueError(msg)
    budget = block.get("session_wall_clock_seconds", DEFAULT_SESSION_WALL_CLOCK_SECONDS)
    if not isinstance(budget, int):
        msg = (
            "runtime_controls.session_wall_clock_seconds must be an integer; "
            f"got {type(budget).__name__}"
        )
        raise ValueError(msg)
    return RuntimeControls(session_wall_clock_seconds=budget)
