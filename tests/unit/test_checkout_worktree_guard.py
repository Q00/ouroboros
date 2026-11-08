"""The unit-suite guard against tests registering worktrees in the checkout.

``tests/unit/conftest.py`` runs ``fail_on_worktrees_registered_in_checkout``
around every unit test. These tests pin how it attributes a registration to the
running test, against a throwaway repository standing in for the developer's
checkout.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests._checkout_worktree_guard import (
    LeakedWorktree,
    checkout_worktree_registry,
    describe_leaks,
    fail_if_worktrees_leaked,
    leaked_worktrees,
    registered_worktree_names,
)

_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
    return repo


def _add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")


def test_registration_under_a_test_root_is_reported_with_its_branch(
    checkout: Path, tmp_path: Path
) -> None:
    registry = checkout_worktree_registry(checkout)
    assert registry is not None
    before = registered_worktree_names(registry)
    leaked_path = tmp_path / "home" / ".ouroboros" / "worktrees" / "checkout" / "auto_0123"

    _add_worktree(checkout, leaked_path, "ooo/auto_0123")

    assert leaked_worktrees(registry, before, [tmp_path / "home"]) == [
        LeakedWorktree(name="auto_0123", path=leaked_path.resolve(), branch="ooo/auto_0123")
    ]
    with pytest.raises(pytest.fail.Exception, match="registered 1 git worktree"):
        fail_if_worktrees_leaked(checkout, registry, before, [tmp_path / "home"])


def test_registrations_outside_the_roots_or_already_present_are_ignored(
    checkout: Path, tmp_path: Path
) -> None:
    """A concurrent run or the developer's own worktree is never attributed."""
    root = tmp_path / "home"
    _add_worktree(checkout, root / "existing", "ooo/existing")
    registry = checkout_worktree_registry(checkout)
    assert registry is not None
    before = registered_worktree_names(registry)

    _add_worktree(checkout, tmp_path / "other-process" / "auto_4567", "ooo/auto_4567")

    assert leaked_worktrees(registry, before, [root]) == []
    fail_if_worktrees_leaked(checkout, registry, before, [root])


def test_a_checkout_without_linked_worktrees_has_an_empty_registry(checkout: Path) -> None:
    registry = checkout_worktree_registry(checkout)
    assert registry is not None
    assert not registry.exists()
    assert registered_worktree_names(registry) == frozenset()


def test_outside_a_git_checkout_the_guard_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()

    assert checkout_worktree_registry(plain) is None


def test_failure_message_names_the_commands_that_remove_the_leak(tmp_path: Path) -> None:
    leak = LeakedWorktree(name="auto_89ab", path=tmp_path / "auto_89ab", branch="ooo/auto_89ab")

    message = describe_leaks(Path("/repo"), [leak])

    assert f"git -C /repo worktree remove --force {tmp_path / 'auto_89ab'}" in message
    assert "git -C /repo branch -D ooo/auto_89ab" in message


def test_guard_runs_around_every_test_and_watches_this_checkout(
    request: pytest.FixtureRequest,
) -> None:
    assert "fail_on_worktrees_registered_in_checkout" in request.fixturenames
    if not (_CHECKOUT_ROOT / ".git").exists():
        pytest.skip("suite is not running from a git checkout")
    registry = request.getfixturevalue("_checkout_worktree_registry")
    expected = Path(_git(_CHECKOUT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert registry == expected.resolve() / "worktrees"


def test_auto_project_cwd_keeps_the_coding_profile_without_creating_a_worktree(
    auto_project_cwd: Path,
) -> None:
    """The remedy the leaking ``ooo auto`` tests use: same profile, no worktree."""
    import asyncio
    from unittest.mock import patch

    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoWorktreePolicy
    from ouroboros.cli.commands.auto import _run_auto

    seen: dict[str, object] = {}

    async def fake_run(self, state):  # noqa: ARG001
        seen.update(
            profile=state.active_domain_profile_name,
            policy=state.worktree_policy,
            managed_worktree=state.managed_worktree,
            cwd=state.cwd,
        )
        return AutoPipelineResult(
            status="complete", auto_session_id=state.auto_session_id, phase="complete"
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_run):
        asyncio.run(
            _run_auto(
                goal="Build a CLI",
                resume=None,
                runtime="claude",
                max_interview_rounds=1,
                max_repair_rounds=1,
                skip_run=True,
            )
        )

    assert seen == {
        "profile": "coding",
        "policy": AutoWorktreePolicy.AUTO,
        "managed_worktree": None,
        "cwd": str(auto_project_cwd),
    }
