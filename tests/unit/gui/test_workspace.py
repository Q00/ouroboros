"""The project folder's git state, and preparing a folder for a hand-off."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ouroboros.gui import workspace


@pytest.fixture(autouse=True)
def no_ambient_git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No identity from the machine running the tests: an empty global config, no system one."""
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
        "GIT_DIR",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(name, raising=False)
    return config


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit(cwd: Path, message: str = "init") -> None:
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)


def _folder(tmp_path: Path, name: str = "project") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_states_are_read_without_changing_the_folder(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert workspace.git_state(str(folder)).kind == "not_repo"
    assert list(folder.iterdir()) == []  # reading created nothing
    _git(folder, "init", "-q")
    assert workspace.git_state(str(folder)).kind == "no_commits"
    (folder / "a.txt").write_text("a")
    _git(folder, "add", "a.txt")
    _commit(folder)
    assert workspace.git_state(str(folder)).kind == "ready"


def test_a_plain_folder_becomes_a_repository_with_one_empty_commit(
    tmp_path: Path, no_ambient_git_identity: Path
) -> None:
    folder = _folder(tmp_path)
    (folder / "notes.txt").write_text("mine")

    assert workspace.prepare_git(str(folder)) == "initialized"

    assert workspace.git_state(str(folder)).kind == "ready"
    log = _git(folder, "log", "--format=%an <%ae>|%s").strip().splitlines()
    # No identity resolved: the fallback, on that one commit.
    assert log == ["Ouroboros <ouroboros@localhost>|Start Ouroboros project"]
    assert _git(folder, "show", "--name-only", "--format=", "HEAD").strip() == ""
    assert _git(folder, "status", "--porcelain").strip() == "?? notes.txt"
    # The fallback identity was never written to any git config.
    local = subprocess.run(
        ["git", "-C", str(folder), "config", "--local", "--get", "user.email"],
        capture_output=True,
        text=True,
    )
    assert local.returncode == 1
    assert no_ambient_git_identity.read_text(encoding="utf-8") == ""


def test_the_users_own_identity_is_kept(tmp_path: Path, no_ambient_git_identity: Path) -> None:
    no_ambient_git_identity.write_text(
        "[user]\n\tname = Dev\n\temail = dev@example.invalid\n", encoding="utf-8"
    )
    folder = _folder(tmp_path)
    assert workspace.prepare_git(str(folder)) == "initialized"
    assert _git(folder, "log", "--format=%an <%ae>").strip() == "Dev <dev@example.invalid>"


def test_only_the_missing_identity_key_falls_back(
    tmp_path: Path, no_ambient_git_identity: Path
) -> None:
    no_ambient_git_identity.write_text("[user]\n\temail = dev@example.invalid\n", encoding="utf-8")
    folder = _folder(tmp_path)
    workspace.prepare_git(str(folder))
    assert _git(folder, "log", "--format=%an <%ae>").strip() == "Ouroboros <dev@example.invalid>"


def test_a_repository_without_commits_gets_an_empty_commit_and_keeps_what_is_staged(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    _git(folder, "init", "-q")
    (folder / "staged.txt").write_text("staged")
    _git(folder, "add", "staged.txt")

    assert workspace.prepare_git(str(folder)) == "committed"

    assert _git(folder, "show", "--name-only", "--format=", "HEAD").strip() == ""
    assert _git(folder, "status", "--porcelain").strip() == "A  staged.txt"


def test_a_repository_with_commits_is_never_touched(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _git(folder, "init", "-q")
    (folder / "a.txt").write_text("a")
    _git(folder, "add", "a.txt")
    _commit(folder)
    (folder / "a.txt").write_text("changed")
    head = _git(folder, "rev-parse", "HEAD")
    status = _git(folder, "status", "--porcelain")

    assert workspace.prepare_git(str(folder)) == "unchanged"

    assert _git(folder, "rev-parse", "HEAD") == head
    assert _git(folder, "status", "--porcelain") == status


def test_a_repository_git_cannot_read_is_never_initialized_over(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / ".git").write_text("gitdir: /nonexistent/elsewhere\n")

    state = workspace.git_state(str(folder))
    assert state.kind == "unreadable"
    assert not state.needs_prepare
    with pytest.raises(workspace.GitPrepareError, match="cannot use this folder"):
        workspace.prepare_git(str(folder))
    assert (folder / ".git").is_file()  # still the file: no `git init` ran


def test_a_failed_commit_is_reported_not_hidden(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _git(folder, "init", "-q")
    hook = folder / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n")
    hook.chmod(0o755)

    with pytest.raises(workspace.GitPrepareError, match="git commit failed: refused by hook"):
        workspace.prepare_git(str(folder))
    assert workspace.git_state(str(folder)).kind == "no_commits"


def test_a_git_that_hangs_is_bounded_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    real = workspace._git

    def hanging(cwd: str, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "commit" in args:
            assert kwargs["timeout"] == workspace._COMMIT_TIMEOUT
            raise subprocess.TimeoutExpired(["git", *args], workspace._COMMIT_TIMEOUT)
        return real(cwd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace, "_git", hanging)
    with pytest.raises(workspace.GitPrepareError, match="did not finish within 20 seconds"):
        workspace.prepare_git(str(folder))


def test_folder_changes_lists_git_status_only_in_a_repository(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert workspace.folder_changes(str(folder)) is None
    _git(folder, "init", "-q")
    _commit_empty = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    _git(folder, *_commit_empty, "commit", "-q", "--allow-empty", "-m", "init")
    (folder / "palindrome.py").write_text("x")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "p.cpython-312.pyc").write_bytes(b"")

    changes = workspace.folder_changes(str(folder))
    assert changes == workspace.FolderChanges(("palindrome.py",), 1)


def test_folder_changes_is_bounded(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _git(folder, "init", "-q")
    for index in range(workspace._MAX_FILES + 5):
        (folder / f"f{index:03}.txt").write_text("x")
    changes = workspace.folder_changes(str(folder))
    assert changes is not None
    assert len(changes.files) == workspace._MAX_FILES
    assert changes.count == workspace._MAX_FILES + 5
