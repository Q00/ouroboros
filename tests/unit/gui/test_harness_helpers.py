"""Pure helpers behind the chat harness."""

from __future__ import annotations

from pathlib import Path
import subprocess

from ouroboros.gui import harness, installers


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_workspace_problem_reports_missing_repo_and_commit(tmp_path: Path) -> None:
    assert "not a git" in (harness._workspace_problem(str(tmp_path)) or "")
    _git(tmp_path, "init", "-q")
    assert "no commits" in (harness._workspace_problem(str(tmp_path)) or "")
    (tmp_path / "a.txt").write_text("a")
    _git(tmp_path, "add", "a.txt")
    _git(
        tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init"
    )
    assert harness._workspace_problem(str(tmp_path)) is None


def test_item_text_prefers_description() -> None:
    assert (
        harness._item_text({"description": "Returns True", "verify_command": "x"}) == "Returns True"
    )
    assert harness._item_text("plain") == "plain"


def test_effective_runtime_routes_sdkless_claude_to_cli(monkeypatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "claude_agent_sdk" else real(name, *a, **k),
    )
    assert harness.effective_runtime("claude", public=True) == "claude-cli"
    assert harness.effective_runtime("codex", public=True) == "codex"


def test_installer_commands_are_fixed_vendor_commands() -> None:
    methods = {m["id"]: m for m in installers.methods_for("codex")}
    assert methods["npm"]["command"] == "npm install -g @openai/codex"
    script = installers.find("opencode", "script")
    assert script is not None
    assert installers.argv(script)[:2] == ["/bin/bash", "-c"]
    assert installers.methods_for("nonexistent") == []


def test_reloaded_chats_are_marked_interrupted(isolated_home: Path) -> None:
    import json

    chats = harness.chats_dir()
    (chats / "abc123.json").write_text(
        json.dumps(
            {
                "id": "abc123",
                "title": "t",
                "mode": "interview",
                "status": "thinking",
                "suggestion": {"pending": True, "question": "q"},
                "messages": [],
            }
        )
    )
    loaded = harness.Harness().get("abc123")
    assert loaded is not None
    assert loaded["status"] == "interrupted"
    assert loaded["suggestion"] == {"pending": False}


def test_create_rejects_missing_folder(isolated_home: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        harness.Harness().create({"goal": "x", "cwd": str(isolated_home / "nope")})
