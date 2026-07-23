"""Pytest configuration for Ouroboros."""

import atexit
import inspect
import os
from pathlib import Path
import shutil
import sys
import tempfile

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


# ── Hermetic home isolation (before collection) ──────────────────────────────
# Ouroboros derives its state root from ``Path.home()`` (``~/.ouroboros`` for
# config, the default ``EventStore`` DB, logs, ...). Isolate it at conftest
# import — i.e. *before* collection — so it also covers effects the per-test
# fixture below cannot reach: module-level constants captured at import
# (handlers binding ``Path.home() / ".ouroboros" / ...``), a logging handler
# opening ``~/.ouroboros/logs/ouroboros.log``, and ``config.loader`` reading
# ``~/.ouroboros/.env`` at import time. Everything now lands in a throwaway dir.
#
# We patch ``Path.home`` rather than ``$HOME`` on purpose: ``$HOME`` drives
# external tools spawned by tests (e.g. ``uv build``'s cache), so leaving it
# untouched keeps those hermetic against the real environment, and the ~dozens
# of tests that ``patch("pathlib.Path.home", ...)`` in their body still win.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="ouroboros-test-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
Path.home = classmethod(lambda _cls: _SESSION_HOME)  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def isolate_ouroboros_home(tmp_path_factory, monkeypatch):
    """Give each test its own home dir on top of the session-wide baseline set
    above, so per-test runtime state does not bleed across tests.

    Runtime-backend resolution and the default ``EventStore`` DB path resolve
    through ``get_config_dir()`` → ``Path.home()``. Two concrete failure modes
    this prevents:

    * Runtime-inference tests read the developer's real ``config.yaml``
      ``runtime_profile`` (e.g. ``interview: codex``) and fail locally with
      ``codex != opencode`` while passing on CI's empty home — the classic
      "passes on CI, fails on my machine" split.
    * A no-arg ``EventStore()`` writes into a single shared ``ouroboros.db``;
      under ``pytest -n`` a per-test home keeps that off any shared file.

    Tests that need a specific dir still win: their own
    ``patch("pathlib.Path.home", ...)`` is applied after this fixture.
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


@pytest.fixture(autouse=True)
def block_setup_probe_cli_spawns(monkeypatch):
    """Stop ``ooo setup`` capability probes from spawning real codex/opencode CLIs.

    ``_codex_uses_profile_v2`` spawns ``codex --help`` and ``_debug_paths_config_dir``
    spawns ``opencode debug paths`` to sniff installed-CLI capabilities. On a
    machine where those CLIs happen to be installed the probe result — and thus
    the setup tests' behavior — depends on the real binary's output, so the same
    test can branch differently across machines (the "passes on CI, fails on my
    machine" split). Pin both to their documented CLI-unavailable fallback
    (``False`` / ``None``), which is exactly the behavior on a clean CI with no
    such CLI on PATH. Tests that exercise the probe re-patch after this fixture
    and win.

    Note: this deliberately does NOT touch runtime adapters (e.g. the zcode/codex
    CLI runtimes) that exec a caller-provided CLI path — those tests build a stub
    binary under ``tmp_path`` and executing it IS the behavior under test.

    Gated on sys.modules so tests that never import these modules don't pay.
    """
    setup_mod = sys.modules.get("ouroboros.cli.commands.setup")
    if setup_mod is not None and hasattr(setup_mod, "_codex_uses_profile_v2"):
        monkeypatch.setattr(setup_mod, "_codex_uses_profile_v2", lambda *_a, **_k: False)
    opencode_mod = sys.modules.get("ouroboros.cli.opencode_config")
    if opencode_mod is not None and hasattr(opencode_mod, "_debug_paths_config_dir"):
        monkeypatch.setattr(opencode_mod, "_debug_paths_config_dir", lambda *_a, **_k: None)


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
