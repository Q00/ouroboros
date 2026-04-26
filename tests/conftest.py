"""Pytest configuration for Ouroboros."""

import inspect
import os

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _redirect_chain_artifact_dir(tmp_path, monkeypatch):
    """Redirect SerialCompoundingExecutor chain artifacts to a per-test tmp dir.

    Without this, tests that exercise the executor write postmortem-chain
    markdown into the real ``docs/brainstorm/`` directory (the production
    default at ``serial_executor.py:_DEFAULT_CHAIN_ARTIFACT_DIR``). Tests
    that set their own ``OUROBOROS_CHAIN_ARTIFACT_DIR`` still win because
    pytest applies their ``monkeypatch.setenv`` after this autouse fixture.
    """
    monkeypatch.setenv(
        "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "chain_artifacts")
    )

# In CI, GITHUB_ACTIONS env var causes Typer to set force_terminal=True on
# Rich Console (see typer/rich_utils.py:75-78). This makes Rich emit ANSI
# escape codes even into CliRunner's string buffer, inserting style sequences
# at word boundaries (e.g. hyphens in --llm-backend) and breaking plain-text
# assertions. _TYPER_FORCE_DISABLE_TERMINAL is Typer's built-in escape hatch
# that sets force_terminal=False, letting Rich detect non-TTY output correctly.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"


@pytest_asyncio.fixture(autouse=True)
async def close_test_owned_stores(monkeypatch):
    """Close stores created during a test to prevent aiosqlite leak warnings."""
    from ouroboros.persistence.brownfield import BrownfieldStore
    from ouroboros.persistence.event_store import EventStore

    created_stores: list[object] = []
    original_event_store_init = EventStore.__init__
    original_brownfield_store_init = BrownfieldStore.__init__

    def _track(store: object) -> None:
        created_stores.append(store)

    def _event_store_init(self, *args, **kwargs) -> None:
        original_event_store_init(self, *args, **kwargs)
        _track(self)

    def _brownfield_store_init(self, *args, **kwargs) -> None:
        original_brownfield_store_init(self, *args, **kwargs)
        _track(self)

    monkeypatch.setattr(EventStore, "__init__", _event_store_init)
    monkeypatch.setattr(BrownfieldStore, "__init__", _brownfield_store_init)

    try:
        yield
    finally:
        closed_ids: set[int] = set()
        for store in reversed(created_stores):
            store_id = id(store)
            if store_id in closed_ids:
                continue
            closed_ids.add(store_id)

            close_result = store.close()
            if inspect.isawaitable(close_result):
                try:
                    await close_result
                except Exception:
                    pass
