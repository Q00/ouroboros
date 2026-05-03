"""``AgentRuntimeContext`` — the runtime-offering side of #476 Q1.

The runtime context describes *what the orchestrator runtime offers* a
handler — the EventStore it appends into, optional capability sources,
and the :class:`ControlBus` that lets handlers react to directive events.
The context is intentionally narrow: per the maintainer commitment in
#476 Q1, every new field added later must include a one-line PR-body
justification so the type does not drift into a service locator.

This first step keeps the membership minimal:

* ``event_store`` — the persistence boundary handlers already share.
* ``runtime_backend`` / ``llm_backend`` — informational labels existing
  callers (rate-limit, telemetry) already pass alongside the bridge.
* ``mcp_bridge`` — the existing capability source from #280's bridge
  work; optional so non-MCP code paths stay valid.
* ``control`` — the :class:`ControlBus` introduced alongside this
  context; ``None`` while #474's migration is in progress so existing
  callers compile unchanged.

Subsequent issues (#474, #475) wire concrete handlers to consume the
context. This module deliberately does *not* import any handler-side
type so that adopting the context becomes a one-import change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.mcp.bridge.bridge import MCPBridge
    from ouroboros.orchestrator.control_bus import ControlBus
    from ouroboros.persistence.event_store import EventStore


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """Minimal, narrow-membership runtime context handed to MCP handlers."""

    event_store: EventStore
    runtime_backend: str | None = None
    llm_backend: str | None = None
    mcp_bridge: MCPBridge | None = None
    control: ControlBus | None = None
