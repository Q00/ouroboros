"""Tests for N-version tournament scaffolding (PR-X X3, not live-wired)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from ouroboros.orchestrator import n_version_tournament as nvt
from ouroboros.orchestrator.n_version_tournament import TournamentEntry


class TestPlanTournament:
    def test_picks_up_to_max_distinct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = ["codex", "gemini", "opencode"]

        def _fake_pick(failed: str, *, exclude=None, weights=None):  # type: ignore[no-untyped-def]
            for name in pool:
                if name not in (exclude or set()):
                    return name
            return None

        monkeypatch.setattr(nvt, "pick_alternative_runtime", _fake_pick)
        contestants = nvt.plan_tournament("claude", max_contestants=2)
        assert contestants == ("codex", "gemini")
        assert len(set(contestants)) == 2  # distinct

    def test_stops_when_pool_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _fake_pick(failed: str, *, exclude=None, weights=None):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return "codex" if calls["n"] == 1 else None

        monkeypatch.setattr(nvt, "pick_alternative_runtime", _fake_pick)
        assert nvt.plan_tournament("claude", max_contestants=3) == ("codex",)

    def test_zero_max_is_empty(self) -> None:
        assert nvt.plan_tournament("claude", max_contestants=0) == ()


class TestWinnerSelection:
    def test_first_passing_wins(self) -> None:
        entries = [
            TournamentEntry(backend="codex", passed=False),
            TournamentEntry(backend="gemini", passed=True),
            TournamentEntry(backend="opencode", passed=True),
        ]
        winner = nvt.select_tournament_winner(entries)
        assert winner is not None
        assert winner.backend == "gemini"

    def test_no_passing_returns_none(self) -> None:
        entries = [
            TournamentEntry(backend="codex", passed=False),
            TournamentEntry(backend="gemini", passed=False),
        ]
        assert nvt.select_tournament_winner(entries) is None

    def test_empty_returns_none(self) -> None:
        assert nvt.select_tournament_winner([]) is None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


class TestWorktreeIsolation:
    def test_contestants_get_isolated_worktrees(self, git_workspace: Path) -> None:
        with nvt.RunWorktreeManager(git_workspace) as manager:
            wt_a = manager.create("codex")
            wt_b = manager.create("gemini")
            # Distinct, real, and separate from the main workspace.
            assert wt_a != wt_b
            assert wt_a.exists() and wt_b.exists()
            assert wt_a.resolve() != git_workspace.resolve()
            # A dirty edit in one contestant never touches the other or main.
            (wt_a / "file.txt").write_text("codex-change\n")
            assert (wt_b / "file.txt").read_text() == "base\n"
            assert (git_workspace / "file.txt").read_text() == "base\n"
        # Cleanup removed the worktrees.
        assert not wt_a.exists()
        assert not wt_b.exists()

    def test_winner_diff_applies_to_workspace(self, git_workspace: Path) -> None:
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "file.txt").write_text("winning change\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert b"winning change" in diff
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / "file.txt").read_text() == "winning change\n"

    def test_empty_diff_is_noop_success(self, git_workspace: Path) -> None:
        assert nvt.apply_diff_to_workspace(git_workspace, b"") is True

    def test_unchanged_winner_exports_empty_not_none(self, git_workspace: Path) -> None:
        """A contestant that genuinely changed nothing exports ``b""``, not ``None``."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            assert nvt.export_worktree_diff(winner) == b""


class TestExportFailureIsNotSilentDataLoss:
    """Regression: a failed ``git diff`` must not look like "winner changed nothing".
    Previously ``export_worktree_diff`` returned ``b""`` on any git error and
    ``apply_diff_to_workspace(b"")`` returned ``True``, so a git failure while
    exporting the tournament winner's work was reported as a *successful* merge
    and the winning contestant's code was silently discarded.
    """

    def test_export_returns_none_when_not_a_repo(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        assert nvt.export_worktree_diff(not_a_repo) is None

    def test_export_returns_none_when_git_missing(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise OSError("git not found")

        monkeypatch.setattr(nvt.subprocess, "run", _boom)
        assert nvt.export_worktree_diff(git_workspace) is None

    def test_export_returns_none_on_timeout(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _timeout(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git diff HEAD", timeout=1)

        monkeypatch.setattr(nvt.subprocess, "run", _timeout)
        assert nvt.export_worktree_diff(git_workspace) is None

    def test_apply_rejects_failed_export(self, git_workspace: Path) -> None:
        assert nvt.apply_diff_to_workspace(git_workspace, None) is False

    def test_failed_export_does_not_report_successful_merge(self, tmp_path: Path) -> None:
        """End-to-end shape of the data-loss bug: export fails -> merge fails."""
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        diff = nvt.export_worktree_diff(not_a_repo)
        # The failure is distinguishable from "no changes"...
        assert diff is None
        assert diff != b""
        # ...and is never laundered into a successful no-op merge.
        assert nvt.apply_diff_to_workspace(tmp_path, diff) is False


class TestGitCommandTimeouts:
    """Regression: unbounded git calls could hang the tournament, including teardown."""

    def test_timeout_constant_is_bounded_and_larger_than_probe_timeout(self) -> None:
        assert isinstance(nvt.GIT_COMMAND_TIMEOUT_SECONDS, int)
        # Worktree add/remove is slower than a ``rev-parse`` probe (5s elsewhere).
        assert 5 < nvt.GIT_COMMAND_TIMEOUT_SECONDS <= 300

    def test_git_helper_passes_timeout(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        real_run = nvt.subprocess.run

        def _spy(*args: object, **kwargs: object) -> object:
            seen.update(kwargs)
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        manager = nvt.RunWorktreeManager(git_workspace)
        try:
            monkeypatch.setattr(nvt.subprocess, "run", _spy)
            manager._git("rev-parse", "HEAD")
        finally:
            monkeypatch.undo()
            manager.cleanup_all()
        assert seen.get("timeout") == nvt.GIT_COMMAND_TIMEOUT_SECONDS

    def test_apply_diff_passes_timeout(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _spy(*_args: object, **kwargs: object) -> object:
            seen.update(kwargs)
            raise subprocess.TimeoutExpired(cmd="git apply", timeout=1)

        monkeypatch.setattr(nvt.subprocess, "run", _spy)
        assert nvt.apply_diff_to_workspace(git_workspace, b"some diff\n") is False
        assert seen.get("timeout") == nvt.GIT_COMMAND_TIMEOUT_SECONDS

    def test_cleanup_survives_timeout_and_still_prunes_temp_root(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged git during teardown must not leak the temp root."""
        manager = nvt.RunWorktreeManager(git_workspace)
        manager.create("codex")
        manager.create("gemini")
        root = manager._root
        assert root.exists()

        def _timeout(*_args: str, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git worktree remove", timeout=1)

        monkeypatch.setattr(manager, "_git", _timeout)
        # Does not raise, and every remaining cleanup step still runs.
        manager.cleanup_all()
        assert not root.exists()
        assert manager._created == []

    def test_context_manager_exit_survives_timeout(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with nvt.RunWorktreeManager(git_workspace) as manager:
            manager.create("codex")
            root = manager._root
            monkeypatch.setattr(
                manager,
                "_git",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(cmd="git", timeout=1)
                ),
            )
        # __exit__ completed instead of propagating / hanging.
        assert not root.exists()

    def test_failed_create_is_tracked_for_cleanup(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timed-out ``worktree add`` may leave a partial worktree; track it."""
        manager = nvt.RunWorktreeManager(git_workspace)
        try:

            def _timeout(*_args: str, **_kwargs: object) -> object:
                raise subprocess.TimeoutExpired(cmd="git worktree add", timeout=1)

            monkeypatch.setattr(manager, "_git", _timeout)
            with pytest.raises(subprocess.TimeoutExpired):
                manager.create("codex")
            assert len(manager._created) == 1
        finally:
            monkeypatch.undo()
            manager.cleanup_all()


class TestUntrackedFilesInExport:
    """Regression: a winner that only creates new files must still export a patch.

    Previously ``export_worktree_diff`` used only ``git diff HEAD`` which omits
    untracked files entirely, causing an empty diff (``""``) when the winner only
    created new files. ``apply_diff_to_workspace`` would then report a no-op
    success and the winner's new files would be silently lost.
    """

    def test_export_includes_untracked_new_files(self, git_workspace: Path) -> None:
        """A worktree with only new (untracked) files produces a non-empty diff."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            # Create a brand-new file that is untracked (never staged/committed).
            (winner / "new_feature.py").write_text("print('hello')\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            assert b"new_feature.py" in diff
            assert b"print('hello')" in diff

    def test_export_includes_both_tracked_and_untracked(self, git_workspace: Path) -> None:
        """A worktree with both tracked edits and new files captures everything."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            # Edit existing tracked file.
            (winner / "file.txt").write_text("tracked change\n")
            # Create untracked new file.
            (winner / "brand_new.rs").write_text("fn main() {}\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert b"tracked change" in diff
            assert b"brand_new.rs" in diff
            assert b"fn main()" in diff

    def test_untracked_diff_applies_to_workspace(self, git_workspace: Path) -> None:
        """Exported untracked-file diff can be applied to the main workspace."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "new_file.txt").write_text("new content\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / "new_file.txt").exists()
        assert (git_workspace / "new_file.txt").read_text() == "new content\n"

    def test_export_returns_none_when_ls_files_fails(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If listing untracked files fails, export returns None (not partial)."""
        call_count = {"n": 0}
        real_run = subprocess.run

        def _fail_ls_files(*args: object, **kwargs: object) -> object:
            call_count["n"] += 1
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-files" in cmd:
                raise OSError("ls-files failed")
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "new.txt").write_text("x\n")
            monkeypatch.setattr(nvt.subprocess, "run", _fail_ls_files)
            assert nvt.export_worktree_diff(winner) is None


class TestWorktreeCleanupOrdering:
    """Regression: ``git worktree prune`` must run AFTER ``rmtree``.

    Previously prune ran before rmtree, meaning stale worktree registrations
    left by a force-removed directory were not cleaned up — the registration
    pointed to a path that still existed at prune time.
    """

    def test_prune_runs_after_rmtree(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify that git worktree prune is called after the temp root is gone."""
        call_order: list[str] = []
        real_rmtree = shutil.rmtree
        manager = nvt.RunWorktreeManager(git_workspace)
        manager.create("codex")
        root = manager._root
        assert root.exists()

        original_git = manager._git

        def _tracking_rmtree(path: object, **kwargs: object) -> None:
            call_order.append("rmtree")
            real_rmtree(path, **kwargs)  # type: ignore[arg-type]

        def _tracking_git(*args: str, **kwargs: object) -> object:
            if args and args[0] == "worktree" and len(args) > 1 and args[1] == "prune":
                call_order.append("prune")
                # At prune time, root should already be gone.
                assert not root.exists(), "prune must run after rmtree removes the root"
            return original_git(*args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", _tracking_rmtree)
        monkeypatch.setattr(manager, "_git", _tracking_git)
        manager.cleanup_all()

        assert "rmtree" in call_order
        assert "prune" in call_order
        assert call_order.index("rmtree") < call_order.index("prune")


class TestBinaryPatchExport:
    """Regression: binary files must produce applicable --binary patches.

    Without ``--binary``, git emits only a "Binary files differ" marker that
    ``git apply`` rejects. The winner's binary content would be silently lost.
    """

    def test_tracked_binary_change_exports_applicable_patch(self, git_workspace: Path) -> None:
        """A tracked binary file modified in the winner worktree produces a patch that applies."""
        # Create a binary file in the base repo.
        binary_content_base = bytes(range(256))
        (git_workspace / "image.bin").write_bytes(binary_content_base)
        _git(git_workspace, "add", "-A")
        _git(git_workspace, "commit", "-m", "add binary")

        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            # Modify the tracked binary in the winner worktree.
            binary_content_new = bytes(range(255, -1, -1))
            (winner / "image.bin").write_bytes(binary_content_new)
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            # The patch must apply cleanly.
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        # Verify the binary content was transferred.
        assert (git_workspace / "image.bin").read_bytes() == binary_content_new

    def test_untracked_binary_file_exports_applicable_patch(self, git_workspace: Path) -> None:
        """An untracked binary file in the winner worktree produces an applicable patch."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            # Create a new binary file (untracked).
            binary_content = b"\x00\x01\x02\xff\xfe\xfd" * 100
            (winner / "data.bin").write_bytes(binary_content)
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            assert b"data.bin" in diff
            # The patch must apply cleanly.
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        # Verify the binary content was transferred.
        assert (git_workspace / "data.bin").read_bytes() == binary_content


class TestNonAsciiAndQuotedFilenames:
    """Regression: Git path bytes must survive export and apply unchanged.

    Git quotes filenames containing non-ASCII characters, tabs, newlines, or
    literal quotes when using line-oriented display. NUL-delimited byte output,
    filesystem decoding, and option separators preserve both ordinary Unicode
    names and path bytes that are not valid UTF-8.
    """

    def test_non_ascii_filename_exports_and_applies(self, git_workspace: Path) -> None:
        """An untracked file with a non-ASCII name (e.g. café.py) is captured."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            non_ascii_name = "café.py"
            (winner / non_ascii_name).write_text("# encoding test\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / non_ascii_name).exists()
        assert (git_workspace / non_ascii_name).read_text() == "# encoding test\n"

    def test_filename_with_space_exports_and_applies(self, git_workspace: Path) -> None:
        """An untracked file with spaces in the name is captured correctly."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            spaced_name = "my file (1).txt"
            (winner / spaced_name).write_text("spaced\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / spaced_name).exists()
        assert (git_workspace / spaced_name).read_text() == "spaced\n"

    def test_filename_with_quotes_exports_and_applies(self, git_workspace: Path) -> None:
        """An untracked file with literal quotes in the name is captured."""
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            quoted_name = 'say"hello".txt'
            (winner / quoted_name).write_text("quoted\n")
            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / quoted_name).exists()
        assert (git_workspace / quoted_name).read_text() == "quoted\n"

    @pytest.mark.skipif(os.name == "nt", reason="Windows filenames are Unicode-only")
    def test_undecodable_filename_exports_and_applies(self, git_workspace: Path) -> None:
        """A valid non-UTF-8 Git pathname reaches the workspace byte-for-byte."""
        raw_name = b"winner-\xff.bin"
        content = b"\x00winning bytes\xff\n"

        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            winner_file = os.path.join(os.fsencode(winner), raw_name)
            fd = os.open(winner_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)

            diff = nvt.export_worktree_diff(winner)
            assert diff is not None
            assert diff != b""
            assert nvt.apply_diff_to_workspace(git_workspace, diff) is True

        exported_file = os.path.join(os.fsencode(git_workspace), raw_name)
        with open(exported_file, "rb") as stream:
            assert stream.read() == content


class TestRejectRc1WithNoPatch:
    """Regression: rc=1 from git diff --no-index with empty stdout is a failure.

    If git cannot read an untracked file (broken symlink, permission error) it
    may return rc=1 with empty stdout. Treating that as "no difference" would
    silently lose the file. The export must return None.
    """

    def test_rc1_no_stdout_returns_none(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git diff --no-index returning rc=1 with empty stdout causes export failure."""
        call_count = {"n": 0}
        real_run = subprocess.run

        def _fake_no_index_empty(*args: object, **kwargs: object) -> object:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "--no-index" in cmd:
                call_count["n"] += 1
                # Simulate rc=1 with no patch output (e.g. file unreadable).
                result = subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout=b"", stderr=b"fatal: cannot read file"
                )
                return result
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "unreadable.bin").write_text("x\n")
            monkeypatch.setattr(nvt.subprocess, "run", _fake_no_index_empty)
            diff = nvt.export_worktree_diff(winner)
            assert diff is None
            assert call_count["n"] >= 1

    def test_signal_termination_returns_none(
        self, git_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A signal-terminated untracked diff must not become a successful no-op."""
        real_run = subprocess.run

        def _fake_no_index_signal(*args: object, **kwargs: object) -> object:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "--no-index" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=-9, stdout=b"", stderr=b"")
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "signal-lost.bin").write_bytes(b"winner data")
            monkeypatch.setattr(nvt.subprocess, "run", _fake_no_index_signal)

            diff = nvt.export_worktree_diff(winner)

            assert diff is None
            assert nvt.apply_diff_to_workspace(git_workspace, diff) is False
