"""Structured progress events for ``ooo auto`` sessions.

Observers (CLI streaming, MCP history, TUI/HUD) subscribe to a single
``AutoProgressCallback`` to render auto pipeline progress without scraping the
persisted JSON state. The dataclass is intentionally narrow so future surfaces
can extend it without a breaking change to the contract that lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ouroboros.auto.state import utc_now_iso

AutoProgressKind = Literal["phase", "grade", "repair"]


@dataclass(frozen=True, slots=True)
class AutoProgressEvent:
    """Immutable observation of an auto pipeline state change.

    ``kind`` is the discriminant:

    - ``phase``: a phase transition (including terminal phases) just occurred.
    - ``grade``: a Seed review produced a grade.
    - ``repair``: the repair round counter advanced.
    """

    auto_session_id: str
    phase: str
    kind: AutoProgressKind
    message: str
    round: int | None = None
    grade: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)


AutoProgressCallback = Callable[[AutoProgressEvent], None]
