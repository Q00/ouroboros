"""Orchestrator test fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def fake_codex_cli_on_path(tmp_path, monkeypatch):
    """Give Codex runtime tests a stable executable identity on CI.

    Most runtime tests patch subprocess creation and assert command-shaping
    behavior; they should not depend on whether the CI image happens to install
    a real ``codex`` binary. The production runtime now fails closed for bare
    commands that cannot be resolved to an executable at initialization, so make
    the test PATH explicit and hermetic.
    """
    bin_dir = tmp_path / ".fake-codex-bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\necho codex test\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
