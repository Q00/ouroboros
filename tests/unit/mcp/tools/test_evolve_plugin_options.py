"""Plugin evolution must not silently discard checkpoint and recovery options."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler, StartEvolveStepHandler
from ouroboros.persistence.event_store import EventStore

HANDLERS = [EvolveStepHandler, StartEvolveStepHandler]
OPTIONS = [
    ("commit_policy", "ac_checkpoint"),
    ("auto_session_id", "auto_example"),
    ("execution_id", "execution_example"),
    ("checkpoint_commits", [{"ac_id": "AC-1", "commit_sha": "a" * 40}]),
    ("checkpoint_attempted_ac_ids", ["AC-1"]),
    ("recover_expired_claim", True),
]
DEFAULTS = {
    "commit_policy": None,
    "auto_session_id": None,
    "execution_id": None,
    "checkpoint_commits": [],
    "checkpoint_attempted_ac_ids": [],
    "recover_expired_claim": False,
}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'plugin.db'}")
    await event_store.initialize()
    try:
        yield event_store
    finally:
        await event_store.close()


@pytest.mark.parametrize("handler_type", HANDLERS)
@pytest.mark.parametrize(("option", "value"), OPTIONS)
async def test_plugin_rejects_unsupported_option_before_recording_work(
    handler_type: type[EvolveStepHandler] | type[StartEvolveStepHandler],
    option: str,
    value: Any,
    store: EventStore,
) -> None:
    handler = handler_type(
        event_store=store,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    result = await handler.handle({"lineage_id": "plugin-lineage", option: value})

    assert result.is_err
    assert option in str(result.error)
    assert "in-process" in str(result.error)
    assert await store.count_events() == 0


@pytest.mark.parametrize("handler_type", HANDLERS)
async def test_plugin_reports_all_unsupported_options(
    handler_type: type[EvolveStepHandler] | type[StartEvolveStepHandler],
    store: EventStore,
) -> None:
    handler = handler_type(
        event_store=store,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    result = await handler.handle({"lineage_id": "plugin-lineage", **dict(OPTIONS)})

    assert result.is_err
    for option, _ in OPTIONS:
        assert option in str(result.error)
    assert await store.count_events() == 0


@pytest.mark.parametrize("handler_type", HANDLERS)
@pytest.mark.parametrize("options", [{}, DEFAULTS], ids=["omitted", "explicit-defaults"])
async def test_plugin_keeps_default_delegation(
    handler_type: type[EvolveStepHandler] | type[StartEvolveStepHandler],
    options: dict[str, Any],
    store: EventStore,
) -> None:
    handler = handler_type(
        event_store=store,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    result = await handler.handle({"lineage_id": "plugin-lineage", **options})

    assert result.is_ok
    assert result.value.meta["_subagent"]["context"]["lineage_id"] == "plugin-lineage"
    assert await store.count_events() > 0
