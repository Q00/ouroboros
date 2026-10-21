"""The project virtualenv is found from a linked task worktree (``core/project_env``)."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from ouroboros.core.project_env import (
    main_worktree_root,
    project_venv_python,
    project_venv_scripts,
    with_project_venv,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX virtualenv layout")


def _make_venv(root: Path, name: str = ".venv") -> Path:
    bin_dir = root / name / "bin"
    bin_dir.mkdir(parents=True)
    (root / name / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = bin_dir / "python3"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    return python


def _linked_worktree(main: Path, worktree: Path) -> Path:
    """A linked worktree as git lays it out: a ``.git`` file naming the main repo."""
    gitdir = main / ".git" / "worktrees" / worktree.name
    gitdir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return worktree


def test_linked_worktree_uses_the_main_working_tree_venv(tmp_path: Path) -> None:
    main = tmp_path / "project"
    python = _make_venv(main)
    worktree = _linked_worktree(main, tmp_path / "worktrees" / "orch_1")

    assert main_worktree_root(worktree) == main
    assert project_venv_python(worktree) == python
    assert project_venv_scripts(worktree) == python.parent


def test_checkout_venv_wins_over_the_main_working_tree_venv(tmp_path: Path) -> None:
    main = tmp_path / "project"
    _make_venv(main)
    worktree = _linked_worktree(main, tmp_path / "worktrees" / "orch_1")
    own = _make_venv(worktree, "venv")

    assert project_venv_python(worktree) == own


def test_no_venv_leaves_the_environment_unchanged(tmp_path: Path) -> None:
    worktree = _linked_worktree(tmp_path / "project", tmp_path / "worktrees" / "orch_1")
    env = {"PATH": "/usr/bin", "LANG": "C"}

    assert project_venv_python(worktree) is None
    assert with_project_venv(env, worktree) == env


def test_with_project_venv_activates_the_venv(tmp_path: Path) -> None:
    main = tmp_path / "project"
    python = _make_venv(main)
    worktree = _linked_worktree(main, tmp_path / "worktrees" / "orch_1")
    env = {"PATH": "/usr/bin", "LANG": "C"}

    result = with_project_venv(env, worktree)

    assert result["PATH"] == os.pathsep.join((str(python.parent), "/usr/bin"))
    assert result["VIRTUAL_ENV"] == str(main / ".venv")
    assert result["LANG"] == "C"
    assert env == {"PATH": "/usr/bin", "LANG": "C"}
