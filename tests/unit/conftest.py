"""Unit-test guard: never register git worktrees in the developer's checkout.

``tests/conftest.py`` isolates ``$HOME``, which moves the managed-worktree
*root* into a temp dir. Code that resolves its repository from the process cwd
(a new ``ooo auto`` session, for one) still runs ``git worktree add`` against
the checkout the suite runs from. See ``tests/_checkout_worktree_guard.py`` for
how a leaked registration is attributed to the test that made it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._checkout_worktree_guard import (
    checkout_worktree_registry,
    fail_if_worktrees_leaked,
    registered_worktree_names,
)

_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def _checkout_worktree_registry() -> Path | None:
    return checkout_worktree_registry(_CHECKOUT_ROOT)


@pytest.fixture(autouse=True)
def fail_on_worktrees_registered_in_checkout(_checkout_worktree_registry, tmp_path_factory):
    """Fail a test that leaves a git worktree registered in the developer's checkout.

    Only registrations pointing into this process's temp roots (``tmp_path``,
    the per-test and session homes) count, so concurrent runs and the
    developer's own worktrees never trip it. Costs one directory listing before
    and after each test.
    """
    registry = _checkout_worktree_registry
    if registry is None:
        yield
        return

    roots = [tmp_path_factory.getbasetemp()]
    session_home = os.environ.get("_OUROBOROS_TEST_SESSION_HOME")
    if session_home:
        roots.append(Path(session_home))
    before = registered_worktree_names(registry)
    yield
    fail_if_worktrees_leaked(_CHECKOUT_ROOT, registry, before, roots)


@pytest.fixture
def auto_project_cwd(tmp_path, monkeypatch) -> Path:
    """Run ``ooo auto`` entry points from a throwaway coding project.

    A new ``ooo auto`` session targets the process cwd. From the checkout, the
    coding profile's default worktree policy makes it register an
    ``ooo/auto_<id>`` worktree and branch there. This project keeps the coding
    profile (it has a ``pyproject.toml``) but is not a git repository, so no
    worktree is created anywhere.
    """
    project = tmp_path / "auto-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    monkeypatch.chdir(project)
    return project
