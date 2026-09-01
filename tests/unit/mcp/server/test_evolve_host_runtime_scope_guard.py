"""The ``host`` agent runtime requires a bound ``session_id`` so submissions
correlate against the right dispatch record (``host_dispatch.py``). Evolve /
Ralph jobs carry no per-session identity through ``EvolutionaryLoop``'s
executor callback, so binding the host bridge there would silently park a
dispatch that can never be discovered or submitted against — it later dies
deep inside ``execute_task`` with ``host_dispatch_scope_missing``.

These tests pin the fail-early guard: selecting ``host`` for evolve/Ralph
execution raises a ``ConfigError`` immediately, before any runtime is
constructed or any dispatch is parked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.core.errors import ConfigError
from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.persistence.event_store import EventStore


@pytest.mark.asyncio
async def test_evolution_executor_rejects_host_runtime(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    server = create_ouroboros_server(
        event_store=store,
        state_dir=tmp_path / "state",
        runtime_backend="host",
        llm_backend="claude",
    )

    try:
        handler = server._tool_handlers["ouroboros_evolve_step"]
        executor = handler.evolutionary_loop.executor

        with pytest.raises(ConfigError, match="not supported for evolve/Ralph"):
            await executor(seed=MagicMock(), parallel=True, execution_id="exec_1")
    finally:
        await server.shutdown()
