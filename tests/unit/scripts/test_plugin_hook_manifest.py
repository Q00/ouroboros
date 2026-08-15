"""Behavioral regression tests for host-facing plugin hook commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOOKS_PATH = _REPO_ROOT / "hooks" / "hooks.json"
_CLAUDE_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"
_CODEX_HOOKS_PATH = _REPO_ROOT / ".codex" / "hooks.json"


def _hook_command(manifest_path: Path, event_name: str) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["hooks"][event_name]
    assert len(entries) == 1
    hooks = entries[0]["hooks"]
    assert len(hooks) == 1
    return hooks[0]["command"]


@pytest.mark.parametrize(
    ("manifest_path", "event_name"),
    [
        (_HOOKS_PATH, "SessionStart"),
        (_HOOKS_PATH, "UserPromptSubmit"),
        (_HOOKS_PATH, "PostToolUse"),
        (_CLAUDE_SETTINGS_PATH, "UserPromptSubmit"),
        (_CLAUDE_SETTINGS_PATH, "PostToolUse"),
        (_CODEX_HOOKS_PATH, "UserPromptSubmit"),
        (_CODEX_HOOKS_PATH, "PostToolUse"),
    ],
)
def test_advisory_hook_fails_open_when_loaded_plugin_version_was_removed(
    manifest_path: Path,
    event_name: str,
    tmp_path: Path,
) -> None:
    """A host cache rotation must not turn a missing advisory hook into a block."""
    removed_plugin_root = tmp_path / "cache" / "ouroboros" / "removed-version"
    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(removed_plugin_root)
    env["CLAUDE_PLUGIN_ROOT"] = str(removed_plugin_root)

    result = subprocess.run(
        _hook_command(manifest_path, event_name),
        shell=True,
        input=json.dumps({"hook_event_name": event_name}),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("event_name", "script_name"),
    [
        ("SessionStart", "session-start.py"),
        ("UserPromptSubmit", "keyword-detector.py"),
        ("PostToolUse", "drift-monitor.py"),
    ],
)
def test_packaged_hook_does_not_execute_project_script_without_plugin_root(
    event_name: str,
    script_name: str,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "untrusted-project"
    script = project_root / "scripts" / script_name
    script.parent.mkdir(parents=True)
    marker = tmp_path / "executed"
    script.write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['MARKER']).write_text('executed')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PLUGIN_ROOT", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["MARKER"] = str(marker)

    result = subprocess.run(
        _hook_command(_HOOKS_PATH, event_name),
        shell=True,
        input=json.dumps({"hook_event_name": event_name}),
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "manifest_path",
    [_HOOKS_PATH, _CLAUDE_SETTINGS_PATH, _CODEX_HOOKS_PATH],
)
def test_plugin_root_takes_precedence_over_claude_compatibility_root(
    manifest_path: Path,
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
        _hook_command(manifest_path, "UserPromptSubmit"),
        shell=True,
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PLUGIN_ROOT_OK"
