"""Regression tests for Claude plugin hook command wiring."""

import json
from pathlib import Path

_SETTINGS_PATH = Path(__file__).resolve().parents[3] / ".claude" / "settings.json"


def _hook_commands() -> list[str]:
    settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    commands: list[str] = []
    for hook_entries in settings["hooks"].values():
        for entry in hook_entries:
            for hook in entry["hooks"]:
                commands.append(hook["command"])
    return commands


def test_plugin_hooks_use_plugin_root_for_bundled_scripts() -> None:
    """Hooks must not resolve bundled scripts relative to the user's project cwd."""
    commands = _hook_commands()

    assert any("${CLAUDE_PLUGIN_ROOT}/scripts/keyword-detector.py" in cmd for cmd in commands)
    assert any("${CLAUDE_PLUGIN_ROOT}/scripts/drift-monitor.py" in cmd for cmd in commands)
    assert all("CLAUDE_PROJECT_DIR" not in cmd for cmd in commands)
    assert all("cd " not in cmd for cmd in commands)


def test_plugin_hooks_fall_back_to_unversioned_python() -> None:
    """Prefer python3 where available, but fall back for Windows installs."""
    commands = _hook_commands()

    assert all(cmd.startswith("python3 ") for cmd in commands)
    assert all(" || python " in cmd for cmd in commands)
