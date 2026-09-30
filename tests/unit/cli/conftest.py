"""CLI test isolation helpers.

CI runners (GitHub Actions) set ``XDG_CONFIG_HOME=/home/runner/.config``.
``opencode_config_dir()`` honours XDG before ``Path.home()``, so tests that
only patch ``Path.home`` leak into the runner's real config directory.
Clearing the env vars here forces the ``Path.home()`` fallback path.

Tests that need config isolation now patch ``opencode_config_dir`` directly
(platform-agnostic), so the ``sys.platform`` override is no longer needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_opencode_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear env vars that bypass Path.home() in opencode_config_dir()."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)


@pytest.fixture(autouse=True)
def _no_run_evaluation_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``_run_orchestrator`` tests off the post-run evaluation chain.

    A finished ``ouroboros run`` continues into formal evaluation, which builds
    the whole MCP server. Tests of the run itself do not exercise that chain;
    ``tests/unit/cli/test_run_successors.py`` does, and overrides this fixture.
    """
    monkeypatch.setattr(
        "ouroboros.cli.commands.run_successors.continue_run_into_evaluation",
        AsyncMock(),
    )
