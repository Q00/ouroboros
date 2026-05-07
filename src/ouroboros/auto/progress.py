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

    Each event is a *snapshot* of the current pipeline state at the
    moment of emission, not a delta. Consumers should treat the stream
    as "the latest known state plus selected milestones".

    ``kind`` is the discriminant:

    - ``phase``: the pipeline phase just changed since the last
      observation. ``AutoPipeline`` also emits a ``phase`` event on the
      first save of every ``run()`` invocation — including resume paths
      where no fresh transition occurred in this process — so observers
      always learn the current phase without polling the store.
    - ``grade``: a Seed review produced a new grade since the last
      observation.
    - ``repair``: the repair round counter advanced since the last
      observation.
    """

    auto_session_id: str
    phase: str
    kind: AutoProgressKind
    message: str
    round: int | None = None
    grade: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)


AutoProgressCallback = Callable[[AutoProgressEvent], None]
