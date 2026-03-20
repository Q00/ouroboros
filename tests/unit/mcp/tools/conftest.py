"""Shared fixtures and helpers for MCP tools tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.bigbang.pm_interview import PMInterviewEngine


def make_pm_engine_mock(**kwargs) -> PMInterviewEngine:
    """Create a MagicMock(spec=PMInterviewEngine) with delegated methods wired.

    Wires ``compute_deferred_diff``, ``get_pending_reframe``, and
    ``get_last_classification`` to the real implementations so that
    handler code that delegates to engine methods works correctly
    with mock engines.

    Any keyword arguments are set as attributes on the mock (e.g.
    ``deferred_items=["q1"]``).
    """
    engine = MagicMock(spec=PMInterviewEngine)
    engine.deferred_items = []
    engine.decide_later_items = []
    engine.codebase_context = ""
    engine._reframe_map = {}
    engine.classifications = []
    engine._selected_brownfield_repos = []
    engine.classifier = MagicMock()

    # Apply caller overrides
    for key, value in kwargs.items():
        setattr(engine, key, value)

    # Wire real implementations for methods the handler delegates to
    engine.compute_deferred_diff = lambda db, dlb: PMInterviewEngine.compute_deferred_diff(
        engine, db, dlb
    )
    engine.get_pending_reframe = lambda: PMInterviewEngine.get_pending_reframe(engine)
    engine.get_last_classification = lambda: PMInterviewEngine.get_last_classification(engine)
    engine.restore_meta = lambda meta: PMInterviewEngine.restore_meta(engine, meta)
    engine.check_completion = lambda state: PMInterviewEngine.check_completion(engine, state)

    return engine
