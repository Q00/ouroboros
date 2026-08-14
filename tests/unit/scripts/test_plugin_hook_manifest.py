"""Behavioral regression tests for host-facing plugin hook commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

_HOOKS_PATH = Path(__file__).resolve().parents[3] / "hooks" / "hooks.json"


def _hook_command(event_name: str) -> str:
    manifest = json.loads(_HOOKS_PATH.read_text(encoding="utf-8"))
    entries = manifest["hooks"][event_name]
    assert len(entries) == 1
    hooks = entries[0]["hooks"]
    assert len(hooks) == 1
    return hooks[0]["command"]


@pytest.mark.parametrize("event_name", ["SessionStart", "UserPromptSubmit", "PostToolUse"])
def test_advisory_hook_fails_open_when_loaded_plugin_version_was_removed(
    event_name: str,
    tmp_path: Path,
) -> None:
    """A host cache rotation must not turn a missing advisory hook into a block."""
    removed_plugin_root = tmp_path / "cache" / "ouroboros" / "removed-version"
    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(removed_plugin_root)
    env["CLAUDE_PLUGIN_ROOT"] = str(removed_plugin_root)

    result = subprocess.run(
        _hook_command(event_name),
        shell=True,
        input=json.dumps({"hook_event_name": event_name}),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_codex_plugin_root_takes_precedence_over_claude_compatibility_root(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "current-version"
    script = plugin_root / "scripts" / "keyword-detector.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('PLUGIN_ROOT_OK')\n", encoding="utf-8")
    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(plugin_root)
    env["CLAUDE_PLUGIN_ROOT"] = str(tmp_path / "removed-version")

    result = subprocess.run(
        _hook_command("UserPromptSubmit"),
        shell=True,
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PLUGIN_ROOT_OK"
