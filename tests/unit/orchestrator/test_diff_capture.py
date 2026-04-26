"""Unit tests for ouroboros.orchestrator.diff_capture.

Covers the Q2 (Phase 2) per-AC diff capture helpers.  Failure modes must
ALL degrade gracefully — every error path returns an empty diff_summary
without raising.

[[INVARIANT: diff capture failures never propagate — diff_summary
becomes "" on every error path]]
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

from ouroboros.orchestrator import diff_capture
from ouroboros.orchestrator.diff_capture import (
    capture_pre_ac_snapshot,
    compute_diff_summary,
)

# --- Helpers -----------------------------------------------------------------


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run a git command in ``cwd`` and return stripped stdout (raises on non-zero)."""
    base_env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(cwd),
    }
    if env:
        base_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=base_env,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path, *, initial_files: dict[str, str] | None = None) -> None:
    """Initialise a git repo at ``repo`` with one initial commit."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    files = initial_files or {"README.md": "initial\n"}
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


# --- capture_pre_ac_snapshot -------------------------------------------------


class TestCapturePreAcSnapshot:
    def test_clean_repo_returns_sha(self, tmp_path: Path) -> None:
        """1. capture_pre_ac_snapshot in clean tmp git repo returns 40-char SHA.

        ``git stash create`` returns empty stdout when the worktree is clean
        (nothing to stash) — the helper documents this as a "capture failed"
        case so downstream short-circuits cleanly.  The "clean repo" test
        therefore covers the dirty-worktree-with-content path: write an
        unstaged change, snapshot returns a real SHA.
        """
        _init_repo(tmp_path)
        # Dirty the worktree so stash create produces a real SHA.
        (tmp_path / "README.md").write_text("changed\n")
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is not None
        assert re.fullmatch(r"[0-9a-f]{40}", sha), sha

    def test_dirty_worktree_sha_differs_from_head_tree(self, tmp_path: Path) -> None:
        """2. SHA encodes the unstaged changes.

        Verify the stash SHA's tree differs from HEAD's tree after dirtying
        the worktree.
        """
        _init_repo(tmp_path)
        (tmp_path / "new_file.py").write_text("print('hi')\n")
        # Track the file so stash create includes the change (stash create
        # only includes tracked changes by default, which matches our
        # purpose — we're capturing AC edits to tracked files).
        _git(tmp_path, "add", "new_file.py")
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is not None
        head_tree = _git(tmp_path, "rev-parse", "HEAD^{tree}")
        stash_tree = _git(tmp_path, "rev-parse", f"{sha}^{{tree}}")
        assert head_tree != stash_tree
        # Confirm the file appears in the diff between HEAD and the stash SHA.
        diff = _git(tmp_path, "diff", "--name-only", "HEAD", sha)
        assert "new_file.py" in diff

    def test_outside_git_repo_returns_none_and_logs_skipped(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """3. tmp_path with no .git/ returns None, logs `serial_executor.diff_capture.skipped`.

        Uses ``capfd`` because structlog renders via stderr file descriptor
        rather than the stdlib logging facade ``caplog`` listens on.
        """
        # tmp_path has no .git/ directory by default.
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is None
        captured = capfd.readouterr()
        joined = captured.out + captured.err
        assert "serial_executor.diff_capture.skipped" in joined
        assert "not_a_git_repo" in joined

    def test_returns_none_when_git_binary_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """4. FileNotFoundError when git binary is unavailable → None."""
        _init_repo(tmp_path)
        # Drain capture from _init_repo's git invocations so we only assert
        # against the helper's log output.
        capfd.readouterr()

        def _raise_fnf(*_a: Any, **_kw: Any) -> Any:
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(diff_capture.subprocess, "run", _raise_fnf)
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is None
        captured = capfd.readouterr()
        joined = captured.out + captured.err
        assert "serial_executor.diff_capture.failed" in joined
        assert "git_binary_missing" in joined

    def test_returns_none_on_subprocess_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """5. Returns None on subprocess timeout."""
        _init_repo(tmp_path)
        capfd.readouterr()  # drain init noise

        def _raise_timeout(*_a: Any, **_kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="git stash create", timeout=5.0)

        monkeypatch.setattr(diff_capture.subprocess, "run", _raise_timeout)
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is None
        captured = capfd.readouterr()
        joined = captured.out + captured.err
        assert "serial_executor.diff_capture.failed" in joined
        assert "timeout" in joined

    def test_stash_create_does_not_grow_stash_list(self, tmp_path: Path) -> None:
        """Phase-2 invariant: ``git stash create`` produces an unreferenced SHA
        without modifying ``.git/refs/stash``.  Run capture multiple times,
        assert ``git stash list`` stays empty.

        [[INVARIANT: git stash create produces an unreferenced SHA without
        modifying .git/refs/stash, so stash list stays empty across runs]]
        """
        _init_repo(tmp_path)
        for _ in range(5):
            (tmp_path / "README.md").write_text(f"change-{(tmp_path / 'README.md').read_text()}")
            sha = capture_pre_ac_snapshot(tmp_path)
            assert sha is not None
        stash_list = _git(tmp_path, "stash", "list")
        assert stash_list == "", f"stash list should be empty, got: {stash_list!r}"

    def test_returns_head_sha_when_tree_truly_clean(self, tmp_path: Path) -> None:
        """capture_pre_ac_snapshot on a clean tree falls back to HEAD SHA.

        The SerialCompoundingExecutor workflow has the agent commit each
        AC's work, leaving the tree clean at AC boundaries.  Without HEAD
        fallback, ``git stash create`` returns empty stdout and downstream
        loses the ability to diff committed-only changes.

        [[INVARIANT: capture_pre_ac_snapshot falls back to git rev-parse
        HEAD when git stash create returns empty (clean tree)]]
        """
        _init_repo(tmp_path)
        head_sha = _git(tmp_path, "rev-parse", "HEAD")
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is not None
        assert sha == head_sha

    def test_returns_none_when_repo_has_no_head(self, tmp_path: Path) -> None:
        """Brand-new ``git init`` repo with no commits → both stash and HEAD
        fail → return None.

        Verifies the HEAD fallback itself degrades gracefully when there's
        no commit to fall back to.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        _git(tmp_path, "init", "-q", "-b", "main")
        sha = capture_pre_ac_snapshot(tmp_path)
        assert sha is None


# --- compute_diff_summary ----------------------------------------------------


class TestComputeDiffSummary:
    def test_returns_empty_when_pre_sha_is_none(self, tmp_path: Path) -> None:
        """6. compute_diff_summary returns "" when pre_sha is None."""
        _init_repo(tmp_path)
        assert compute_diff_summary(None, tmp_path) == ""

    def test_returns_empty_when_pre_eq_post_no_op_ac(self, tmp_path: Path) -> None:
        """7. Returns "" when pre==post (no-op AC: no changes during AC)."""
        _init_repo(tmp_path)
        # Dirty the worktree, capture pre_sha, do NOT change anything.
        (tmp_path / "README.md").write_text("dirty\n")
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # No changes between pre and post → diff_summary == "".
        assert compute_diff_summary(pre_sha, tmp_path) == ""

    def test_real_edit_returns_stat_with_file_and_summary(self, tmp_path: Path) -> None:
        """8. Real edit → output contains the file path and the summary footer."""
        _init_repo(tmp_path)
        # Make a tracked change so stash create has content for pre.
        (tmp_path / "README.md").write_text("pre-state\n")
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # Now simulate the AC body: edit a file.
        (tmp_path / "README.md").write_text("post-state\nline 2\nline 3\nline 4\nline 5\n")
        out = compute_diff_summary(pre_sha, tmp_path)
        assert "README.md" in out
        # The summary footer always says "<N> file changed, <M> insertion(s)" etc.
        # Match either "file" or "files" (count-dependent).
        assert re.search(r"\d+ files? changed", out), out

    def test_captures_committed_only_changes_when_tree_clean_at_both_boundaries(
        self, tmp_path: Path
    ) -> None:
        """Per-AC commits are captured even when the tree is clean at both
        snapshots — mirrors SerialCompoundingExecutor's commit-per-AC pattern.

        Pre regression fix this returned ``""`` because both pre and post
        ``git stash create`` calls produced empty stdout.
        """
        _init_repo(tmp_path)
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # Simulate the agent making a commit during the AC.
        (tmp_path / "feature.py").write_text("def foo():\n    return 1\n")
        _git(tmp_path, "add", "feature.py")
        _git(tmp_path, "commit", "-q", "-m", "feat: add foo")
        out = compute_diff_summary(pre_sha, tmp_path)
        assert "feature.py" in out, out
        assert re.search(r"\d+ files? changed", out), out

    def test_captures_committed_plus_uncommitted_changes(
        self, tmp_path: Path
    ) -> None:
        """Diff covers committed AND uncommitted changes from one AC.

        The agent commits some sub-AC work but may leave staged-but-
        uncommitted changes mid-AC; both must appear in diff_summary.
        """
        _init_repo(tmp_path)
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # Commit one file.
        (tmp_path / "committed.py").write_text("x = 1\n")
        _git(tmp_path, "add", "committed.py")
        _git(tmp_path, "commit", "-q", "-m", "add committed.py")
        # Stage a second file without committing.
        (tmp_path / "staged.py").write_text("y = 2\n")
        _git(tmp_path, "add", "staged.py")
        out = compute_diff_summary(pre_sha, tmp_path)
        assert "committed.py" in out, out
        assert "staged.py" in out, out

    def test_truncates_to_top_file_cap_with_more_files_filler(self, tmp_path: Path) -> None:
        """9. 30 changed files with file_cap=20 → output has 20 file lines + `... and 10 more files`."""
        _init_repo(tmp_path)
        # Create 30 tracked files first (initial state).
        for i in range(30):
            (tmp_path / f"f{i:02d}.txt").write_text(f"baseline {i}\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "30 baseline files")
        # Capture pre (clean state, but stash create needs dirty — touch a
        # sentinel so stash create returns a SHA).
        # Easier: use the HEAD tree as the "pre" baseline by stashing dirt.
        (tmp_path / "f00.txt").write_text("dirty pre\n")
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # Now modify all 30 files (including f00.txt to a new value) — this
        # is the post-AC state.
        for i in range(30):
            (tmp_path / f"f{i:02d}.txt").write_text(f"changed {i}\n" + ("x\n" * (i + 1)))
        out = compute_diff_summary(pre_sha, tmp_path, file_cap=20)
        # Count the file rows (lines containing "|" before the summary).
        lines = out.split("\n")
        file_lines = [ln for ln in lines if "|" in ln]
        assert len(file_lines) == 20, (
            f"expected 20 file lines, got {len(file_lines)}\n--- output ---\n{out}"
        )
        assert "... and 10 more files" in out
        # Summary footer preserved.
        assert re.search(r"\d+ files? changed", out)

    def test_hard_caps_at_char_budget_with_truncated_marker(self, tmp_path: Path) -> None:
        """10. Hard-caps at char_budget; ends with `[truncated]`, summary footer preserved."""
        _init_repo(tmp_path)
        # Create many files with long names so stat output blows past 200 chars.
        for i in range(40):
            (tmp_path / f"some_long_filename_for_stat_truncation_test_{i:03d}.txt").write_text(
                f"baseline {i}\n"
            )
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "long-name baseline")
        (tmp_path / "some_long_filename_for_stat_truncation_test_000.txt").write_text("dirty\n")
        pre_sha = capture_pre_ac_snapshot(tmp_path)
        assert pre_sha is not None
        # Modify all 40 files for big stat output.
        for i in range(40):
            path = tmp_path / f"some_long_filename_for_stat_truncation_test_{i:03d}.txt"
            path.write_text("changed\n" + ("y\n" * 5))
        # Use a small char_budget to force truncation.
        out = compute_diff_summary(pre_sha, tmp_path, file_cap=40, char_budget=200)
        assert out.endswith("[truncated]"), out
        # Summary footer preserved (contains "changed,").
        assert "changed," in out
        # Hard-cap respected (allow tiny overhead from marker/footer
        # arithmetic — the string should not exceed the budget).
        assert len(out) <= 200 + 16

    def test_returns_empty_when_disabled_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """11. OUROBOROS_DIFF_CAPTURE_ENABLED=false/0/empty → "" without invoking subprocess."""
        _init_repo(tmp_path)
        # Track call count to subprocess.run via a sentinel.
        calls: list[tuple[Any, ...]] = []
        original_run = diff_capture.subprocess.run

        def _spy_run(*args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(diff_capture.subprocess, "run", _spy_run)

        for value in ("false", "FALSE", "0", ""):
            calls.clear()
            monkeypatch.setenv("OUROBOROS_DIFF_CAPTURE_ENABLED", value)
            # Pass a fake non-None pre_sha so the early-return is the env gate.
            assert compute_diff_summary("0" * 40, tmp_path) == "", f"value={value!r} should disable"
            assert calls == [], (
                f"value={value!r} must NOT invoke subprocess; got {len(calls)} call(s)"
            )

    def test_returns_empty_on_git_diff_non_zero_exit(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """12. Returns "" gracefully on `git diff` non-zero exit (e.g., bogus pre_sha)."""
        _init_repo(tmp_path)
        # Dirty the worktree so post-snapshot returns a real SHA.
        (tmp_path / "README.md").write_text("dirty\n")
        capfd.readouterr()  # drain
        bogus_pre_sha = "deadbeef" * 5  # 40 chars, unknown to git
        out = compute_diff_summary(bogus_pre_sha, tmp_path)
        assert out == ""
        captured = capfd.readouterr()
        joined = captured.out + captured.err
        assert "serial_executor.diff_capture.failed" in joined


# --- _truncate_stat micro-tests (defensive coverage of the truncation core) --


class TestTruncateStat:
    def test_passthrough_when_under_caps(self) -> None:
        raw = " src/foo.py | 5 +++--\n 1 file changed, 3 insertions(+), 2 deletions(-)\n"
        out = diff_capture._truncate_stat(raw, file_cap=20, char_budget=4000)
        assert "src/foo.py" in out
        assert "1 file changed" in out

    def test_empty_input_returns_empty(self) -> None:
        assert diff_capture._truncate_stat("", file_cap=20, char_budget=4000) == ""

    def test_tight_budget_smaller_than_overhead_respects_cap(self) -> None:
        # Regression: PR #4 review (CodeRabbit) — when footer+marker overhead
        # alone exceeds char_budget, the previous code returned body+footer+
        # marker and blew past the budget.  The tight-budget fallback must
        # hard-cap the output at char_budget regardless of what's available
        # to render.
        raw = (
            " src/foo.py | 50 +++++++++++++++++++++++++++++++++++++++++++++++\n"
            " 1 file changed, 50 insertions(+), 0 deletions(-)\n"
        )
        out = diff_capture._truncate_stat(raw, file_cap=20, char_budget=8)
        assert len(out) <= 8

    def test_tight_budget_no_summary_still_respects_cap(self) -> None:
        # No summary line in input ("changed," missing) — fallback should
        # still respect the cap and at minimum surface the truncated marker
        # if it fits.
        raw = " src/foo.py | 50 +++++++++++++++++++++\n src/bar.py | 30 ++++++++\n"
        out = diff_capture._truncate_stat(raw, file_cap=20, char_budget=5)
        assert len(out) <= 5

    def test_tight_budget_prefers_summary_over_body(self) -> None:
        # When budget fits the summary plus marker but not the file rows,
        # the fallback returns "<summary>\n[truncated]" (truncated to budget).
        raw = (
            " src/foo.py | 50 +++++++++++++++++++++++++++++++++++++++++++++++\n"
            " src/bar.py | 30 ++++++++\n"
            " 2 files changed, 80 insertions(+), 0 deletions(-)\n"
        )
        # Big enough for summary + marker, too small for file rows + footer.
        budget = len(" 2 files changed, 80 insertions(+), 0 deletions(-)") + len(
            "\n[truncated]"
        )
        out = diff_capture._truncate_stat(raw, file_cap=20, char_budget=budget)
        assert len(out) <= budget
        assert "2 files changed" in out
        assert "[truncated]" in out

    def test_file_cap_filters_by_index_not_value(self) -> None:
        # Regression: PR #4 review (CodeRabbit) — set-based filtering would
        # over-include byte-identical stat lines past the cap. Index-based
        # filtering must keep at most file_cap rows even when duplicates exist.
        # Construct two identical rows + a unique third row; cap=2 keeps the
        # two identical rows ONCE each plus the unique row would exceed cap.
        # With cap=1, only the highest-churn row should survive.
        raw = (
            " src/foo.py | 50 ++++++++++++++++++++++++++++++++++++++++++++\n"
            " src/foo.py | 50 ++++++++++++++++++++++++++++++++++++++++++++\n"
            " src/bar.py |  5 ++--\n"
            " 3 files changed, 105 insertions(+), 0 deletions(-)\n"
        )
        out = diff_capture._truncate_stat(raw, file_cap=1, char_budget=4000)
        # Cap=1 means exactly one file row plus the truncation marker plus the
        # summary footer. Even though two rows are byte-identical, only ONE
        # should survive.
        file_rows = [
            ln
            for ln in out.split("\n")
            if "|" in ln and "more files" not in ln
        ]
        assert len(file_rows) == 1, f"expected 1 file row, got {file_rows}"
        assert "... and 2 more files" in out


class TestEnvOverrideSemantics:
    """Regression: PR #4 review (CodeRabbit) — file_cap/char_budget=None means
    'consult env'; any explicit int is a hard override regardless of value.
    """

    def test_explicit_default_value_overrides_env(self, monkeypatch) -> None:
        # Caller passes file_cap=20 (the default constant). Env var says 999.
        # Explicit value MUST win over env.
        monkeypatch.setenv("OUROBOROS_DIFF_SUMMARY_FILE_CAP", "999")
        assert diff_capture._resolve_file_cap(20) == 20

    def test_none_consults_env(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DIFF_SUMMARY_FILE_CAP", "5")
        assert diff_capture._resolve_file_cap(None) == 5

    def test_none_falls_back_to_default_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("OUROBOROS_DIFF_SUMMARY_FILE_CAP", raising=False)
        assert diff_capture._resolve_file_cap(None) == 20  # _DEFAULT_FILE_CAP

    def test_none_falls_back_to_default_on_invalid_env(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DIFF_SUMMARY_FILE_CAP", "not-a-number")
        assert diff_capture._resolve_file_cap(None) == 20

    def test_explicit_int_wins_over_env_for_char_budget(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DIFF_SUMMARY_CHAR_BUDGET", "999")
        assert diff_capture._resolve_char_budget(4000) == 4000

    def test_none_consults_char_budget_env(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DIFF_SUMMARY_CHAR_BUDGET", "100")
        assert diff_capture._resolve_char_budget(None) == 100
