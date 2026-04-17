"""CLI test isolation helpers.

CI runners (GitHub Actions) set ``XDG_CONFIG_HOME=/home/runner/.config``.
``opencode_config_dir()`` honours XDG before ``Path.home()``, so tests that
only patch ``Path.home`` leak into the runner's real config directory.
Clearing the env vars here forces the ``Path.home()`` fallback path, which
the tests then patch to ``tmp_path``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_opencode_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear env vars that bypass Path.home() in opencode_config_dir()."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
