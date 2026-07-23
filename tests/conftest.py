"""Pytest configuration for Ouroboros."""

import inspect
import os
from pathlib import Path
import sys

import pytest
import pytest_asyncio

# In CI, GITHUB_ACTIONS env var causes Typer to set force_terminal=True on
# Rich Console (see typer/rich_utils.py:75-78). This makes Rich emit ANSI
# escape codes even into CliRunner's string buffer, inserting style sequences
# at word boundaries (e.g. hyphens in --llm-backend) and breaking plain-text
# assertions. _TYPER_FORCE_DISABLE_TERMINAL is Typer's built-in escape hatch
# that sets force_terminal=False, letting Rich detect non-TTY output correctly.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

# The live web dashboard spawns a detached daemon process + binds a port the first
# time a run is launched. Unit tests must never do that (process/port/FS side
# effects, non-deterministic URL in responses). Force it OFF by default; tests that
# exercise the wiring opt back in explicitly via monkeypatch + a mocked resolver.
os.environ["OUROBOROS_DASHBOARD"] = "0"


@pytest.fixture(autouse=True)
def isolate_ouroboros_home(tmp_path_factory, monkeypatch):
    """Point every test at a pristine, isolated home so no test reads or writes
    the developer's real ``~/.ouroboros``.

    Runtime-backend resolution and the default ``EventStore`` DB path resolve
    through ``get_config_dir()`` → ``Path.home()``. Without isolation tests
    silently inherit the developer's machine state, with two concrete failure
    modes:

    * Runtime-inference tests read the real ``config.yaml`` ``runtime_profile``
      (e.g. ``interview: codex``) and fail locally with ``codex != opencode``
      while passing on CI's empty home — the classic "passes on CI, fails on my
      machine" split.
    * A no-arg ``EventStore()`` writes into the single shared real
      ``~/.ouroboros/ouroboros.db``; under ``pytest -n`` that is cross-worker
      state and lock contention (and it silently bloats the real DB).

    We patch ``Path.home`` rather than ``$HOME``/``$OUROBOROS_HOME`` on purpose:

    * ``$HOME`` drives external tools (e.g. ``uv build``'s cache); leaving it
      untouched keeps those hermetic against the real environment.
    * Patching ``Path.home`` composes with the ~dozens of tests that already do
      ``patch("pathlib.Path.home", return_value=tmp_path)`` in their body — that
      in-body patch is applied after this fixture and wins, so their explicit
      isolation still routes ``get_config_dir()`` where they expect.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))


@pytest.fixture(autouse=True)
def block_runner_real_llm_adapter(monkeypatch):
    """Block execute_seed's dependency analysis from spawning real agent CLIs.

    ``OrchestratorRunner._build_dependency_analyzer`` resolves an LLM adapter
    from the adapter's ``llm_backend`` / the developer's real ~/.ouroboros
    config, and if a matching CLI binary exists on the machine (claude,
    opencode, ...) it spawns it for a REAL completion — with no timeout on
    some backends. In tests this meant real API calls locally and a ~60s
    hang per failure-path e2e test on CI.

    Raising here routes the builder through its existing RuntimeError
    fallback (structured-only ``DependencyAnalyzer()``), which is also the
    effective CI behavior. Tests that exercise the builder itself patch
    ``ouroboros.orchestrator.runner.create_llm_adapter`` inside the test
    body; that patch is applied after this one and wins.

    Gated on sys.modules so tests that never import the orchestrator don't
    pay the import cost.
    """
    runner_mod = sys.modules.get("ouroboros.orchestrator.runner")
    if runner_mod is not None:

        def _blocked_create_llm_adapter(*_args, **_kwargs):
            raise RuntimeError(
                "create_llm_adapter blocked in tests: dependency analysis must not "
                "spawn real agent CLIs (patch ouroboros.orchestrator.runner."
                "create_llm_adapter to test the LLM path)"
            )

        monkeypatch.setattr(runner_mod, "create_llm_adapter", _blocked_create_llm_adapter)


@pytest.fixture(autouse=True)
def block_interview_answer_refiner_cli_spawn(monkeypatch):
    """Stop the auto interview's answer refiner from spawning real agent CLIs.

    ``build_answer_refiner()`` builds an ``LLMAnswerRefiner`` from the configured
    interview backend via ``ouroboros.providers.create_llm_adapter``. When a
    matching CLI (claude, ...) is installed, the safe-default synthesis path then
    spawns it for a REAL completion per open section — ~4 ``claude`` subprocesses
    and ~13s in a single otherwise-mocked auto test locally, while CI (no CLI
    auth) simply degrades the refiner to ``None``.

    Force the refiner OFF by default. Its own documented fallback is
    deterministic-only answering (``build_answer_refiner`` returns ``None`` when
    the provider is unavailable), so this preserves behavior and makes local runs
    match CI. Tests that exercise the refiner inject an ``LLMAnswerRefiner``
    directly or re-patch after this fixture and win.

    Patched in the consuming modules (where the name is bound at import) and
    gated on sys.modules so tests that never import them don't pay.
    """
    for mod_name in ("ouroboros.mcp.tools.auto_handler", "ouroboros.cli.commands.auto"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "build_answer_refiner"):
            monkeypatch.setattr(mod, "build_answer_refiner", lambda: None)


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
