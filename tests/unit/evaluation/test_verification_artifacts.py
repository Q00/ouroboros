"""Tests for persisted post-run verification artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.evaluation.mechanical import CommandResult, MechanicalConfig
from ouroboros.evaluation.verification_artifacts import (
    _auto_detect_mechanical_toml,
    build_verification_artifacts,
)


def _git_diff_side_effect(command: tuple[str, ...]) -> str:
    if command[:3] == ("git", "status", "--short"):
        return " M src/ouroboros/example.py\n?? tests/unit/test_example.py\n"
    if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
        return " M src/ouroboros/example.py\0?? tests/unit/test_example.py\0"
    if command[:3] == ("git", "diff", "--stat"):
        return (
            " src/ouroboros/example.py | 4 ++--\n"
            " tests/unit/test_example.py | 8 ++++++++\n"
            " 2 files changed, 10 insertions(+), 2 deletions(-)\n"
        )
    if command[:3] == ("git", "log", "--oneline"):
        # Test fixture: empty recent-history capture by default. Tests that
        # exercise committed-history rendering supply their own side_effect.
        return ""
    if command[:2] == ("git", "show"):
        return ""
    raise AssertionError(f"Unexpected git command: {command}")


class TestBuildVerificationArtifacts:
    """Tests for raw-log verification artifact generation."""

    @pytest.mark.asyncio
    async def test_persists_raw_outputs_and_renders_canonical_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """The rendered artifact should include canonical commands and raw log paths."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            test_command=("uv", "run", "pytest", "--tb=short", "-q"),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\n", "")
            if "pytest" in command:
                return CommandResult(
                    0,
                    (
                        "tests/unit/test_example.py::test_flow PASSED\n"
                        "tests/unit/test_example.py::test_edge_case PASSED\n"
                        "2 passed in 0.12s\n"
                    ),
                    "",
                )
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts(
                "exec_test123",
                "Execution completed successfully.",
                tmp_path,
            )

        assert "Integrated Verification: present" in artifacts.artifact
        assert "Canonical Test Command: uv run pytest --tb=short -q" in artifacts.artifact
        assert "- src/ouroboros/example.py" in artifacts.artifact
        assert "2 passed in 0.12s" in artifacts.artifact
        assert "Stdout Log:" in artifacts.reference
        assert "git status --short" in artifacts.reference

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["execution_id"] == "exec_test123"
        assert manifest["has_integrated_verification"] is True
        assert manifest["changed_files"] == [
            "src/ouroboros/example.py",
            "tests/unit/test_example.py",
        ]
        assert len(manifest["runs"]) == 2
        assert (
            Path(manifest["runs"][1]["stdout_path"])
            .read_text(encoding="utf-8")
            .endswith("2 passed in 0.12s\n")
        )

    @pytest.mark.asyncio
    async def test_marks_missing_integrated_verification_explicitly(
        self,
        tmp_path: Path,
    ) -> None:
        """If no canonical test command exists, the artifact should say so directly."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_no_test", "", tmp_path)

        assert "Integrated Verification: missing" in artifacts.artifact
        assert "Canonical Test Command: (not detected)" in artifacts.artifact

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["has_integrated_verification"] is False
        assert len(manifest["runs"]) == 1

    @pytest.mark.asyncio
    async def test_expands_rename_and_copy_paths_in_changed_files(
        self,
        tmp_path: Path,
    ) -> None:
        """Rename and copy entries should expand into concrete file paths."""
        config = MechanicalConfig(
            test_command=("uv", "run", "pytest", "-q"),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(
                    0,
                    (
                        "R  src/ouroboros/old_name.py -> src/ouroboros/new_name.py\n"
                        "C  src/ouroboros/source.py -> src/ouroboros/copied.py\n"
                        "?? tests/unit/test_example.py\n"
                    ),
                    "",
                )
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(
                    0,
                    (
                        "R  src/ouroboros/old_name.py\0src/ouroboros/new_name.py\0"
                        "C  src/ouroboros/source.py\0src/ouroboros/copied.py\0"
                        "?? tests/unit/test_example.py\0"
                    ),
                    "",
                )
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(0, " 3 files changed, 8 insertions(+)\n", "")
            if command[:3] == ("git", "log", "--oneline"):
                return CommandResult(0, "", "")
            if command[:2] == ("git", "show"):
                return CommandResult(0, "", "")
            if "pytest" in command:
                return CommandResult(0, "1 passed in 0.10s\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_rename_copy", "", tmp_path)

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["changed_files"] == [
            "src/ouroboros/old_name.py",
            "src/ouroboros/new_name.py",
            "src/ouroboros/source.py",
            "src/ouroboros/copied.py",
            "tests/unit/test_example.py",
        ]

    @pytest.mark.asyncio
    async def test_captures_git_state_before_verification_side_effects(
        self,
        tmp_path: Path,
    ) -> None:
        """Verifier-generated files must not pollute execution diff evidence."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            timeout_seconds=30,
            working_dir=tmp_path,
        )
        call_order: list[tuple[str, ...]] = []

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            call_order.append(command)
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(0, " M src/ouroboros/example.py\n", "")
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(0, " M src/ouroboros/example.py\0", "")
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(0, " src/ouroboros/example.py | 2 +-\n", "")
            if command[:3] == ("git", "log", "--oneline"):
                return CommandResult(0, "", "")
            if command[:2] == ("git", "show"):
                return CommandResult(0, "", "")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\nGenerated coverage.xml\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_pre_git", "", tmp_path)

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert call_order[:3] == [
            ("git", "status", "--short"),
            ("git", "status", "--porcelain=v1", "-z"),
            ("git", "diff", "--stat", "--find-renames"),
        ]
        assert manifest["changed_files"] == ["src/ouroboros/example.py"]
        assert "coverage.xml" not in manifest["changed_files"]
        assert "- coverage.xml" not in artifacts.artifact

    @pytest.mark.asyncio
    async def test_marks_git_state_unavailable_when_working_dir_is_not_a_repo(
        self,
        tmp_path: Path,
    ) -> None:
        """Git stderr must not be parsed into fake changed files."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(128, "", "fatal: not a git repository\n")
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(128, "", "fatal: not a git repository\n")
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(128, "", "fatal: not a git repository\n")
            if command[:3] == ("git", "log", "--oneline"):
                return CommandResult(128, "", "fatal: not a git repository\n")
            if command[:2] == ("git", "show"):
                return CommandResult(128, "", "fatal: not a git repository\n")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_non_repo", "", tmp_path)

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert artifacts.changed_files == ()
        assert artifacts.git_state_available is False
        assert artifacts.git_state_error == "fatal: not a git repository"
        assert "- (git state unavailable)" in artifacts.artifact
        assert "## Git State" in artifacts.reference
        assert "fatal: not a git repository" in artifacts.reference
        assert "## git status --short" not in artifacts.reference
        assert "## git diff --stat --find-renames" not in artifacts.reference
        assert manifest["changed_files"] == []
        assert manifest["git_state_available"] is False
        assert manifest["git_state_error"] == "fatal: not a git repository"

    @pytest.mark.asyncio
    async def test_distinct_execution_ids_do_not_alias_artifact_directories(
        self,
        tmp_path: Path,
    ) -> None:
        """Distinct execution IDs must not collapse onto the same persisted directory."""
        config = MechanicalConfig(
            test_command=("uv", "run", "pytest", "-q"),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "pytest" in command:
                return CommandResult(0, "1 passed in 0.10s\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            first = await build_verification_artifacts("foo/bar", "done", tmp_path)
            second = await build_verification_artifacts("foo?bar", "done", tmp_path)
            third = await build_verification_artifacts("../../tmp/pwn", "done", tmp_path)

        directories = [
            Path(first.artifact_dir).resolve(),
            Path(second.artifact_dir).resolve(),
            Path(third.artifact_dir).resolve(),
        ]
        assert len(set(directories)) == 3
        for artifact_dir, execution_id, artifacts in (
            (directories[0], "foo/bar", first),
            (directories[1], "foo?bar", second),
            (directories[2], "../../tmp/pwn", third),
        ):
            assert artifact_dir.is_relative_to(artifact_root.resolve())
            manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
            assert manifest["execution_id"] == execution_id
            assert manifest["artifact_key"]
            assert manifest["artifact_run_id"]
            assert f"Execution ID: {execution_id}" in artifacts.artifact
            assert f"Execution ID: {execution_id}" in artifacts.reference

    @pytest.mark.asyncio
    async def test_preserves_separate_artifacts_for_repeated_execution_ids(
        self,
        tmp_path: Path,
    ) -> None:
        """Repeated verification attempts must not overwrite earlier evidence."""
        config = MechanicalConfig(
            test_command=("uv", "run", "pytest", "-q"),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "pytest" in command:
                return CommandResult(0, "1 passed in 0.10s\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            first = await build_verification_artifacts("lin_test-gen-3", "done", tmp_path)
            second = await build_verification_artifacts("lin_test-gen-3", "done", tmp_path)

        first_dir = Path(first.artifact_dir)
        second_dir = Path(second.artifact_dir)
        assert first_dir != second_dir
        assert first_dir.exists()
        assert second_dir.exists()
        assert Path(first.manifest_path).exists()
        assert Path(second.manifest_path).exists()
        assert (first_dir / "runs" / "01-test" / "stdout.log").exists()
        assert (second_dir / "runs" / "01-test" / "stdout.log").exists()

        first_manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
        second_manifest = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
        assert first_manifest["execution_id"] == "lin_test-gen-3"
        assert second_manifest["execution_id"] == "lin_test-gen-3"
        assert first_manifest["artifact_key"] == second_manifest["artifact_key"]
        assert first_manifest["artifact_run_id"] != second_manifest["artifact_run_id"]


class TestCommittedHistoryCapture:
    """The QA artifact must surface committed work even when the worktree is clean.

    Compounding-mode regression: ``SerialCompoundingExecutor`` commits per AC,
    so by QA time the worktree shows zero unstaged/staged changes.  Without
    capturing the recent commit log, the QA judge sees ``"(no changed files
    detected)"`` and concludes nothing happened — masking the real work.
    """

    @pytest.mark.asyncio
    async def test_recent_commits_surface_when_worktree_is_clean(
        self,
        tmp_path: Path,
    ) -> None:
        """Clean worktree + recent commits → 'see Recent Commits below' note + log section."""
        config = MechanicalConfig(timeout_seconds=30, working_dir=tmp_path)

        recent_log = (
            "98c4a15 test(serial): Sub-AC 3 — truncation event\n"
            "ee3adf7 feat(serial-executor): monolithic agent-adjudicated resume (Q6.2)\n"
            "59ed412 feat(serial-executor): sub-postmortem resume path\n"
            "2af4075 feat(cli): add --resume flag to ooo run --compounding\n"
        )
        head_show = (
            "98c4a15c3c93336cf69248455fb5fa13d34b3e2\n"
            "test(serial): Sub-AC 3 — truncation event\n"
            "\n"
            "Keithm\n"
            "Sat Apr 25 06:01:00 2026 +0000\n"
            "\n"
            " src/ouroboros/orchestrator/serial_executor.py | 4 +++-\n"
            " 1 file changed, 3 insertions(+), 1 deletion(-)\n"
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            # Worktree is clean → status/diff capture nothing.
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(0, "", "")
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(0, "", "")
            # …but recent commits exist on the branch.
            if command[:3] == ("git", "log", "--oneline"):
                return CommandResult(0, recent_log, "")
            if command[:2] == ("git", "show"):
                return CommandResult(0, head_show, "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_clean_with_log", "done", tmp_path)

        # Compact artifact: no longer says the harshly misleading "no changed
        # files detected" — instead surfaces the committed work.
        assert "(no changed files detected)" not in artifacts.artifact
        assert "see Recent Commits below" in artifacts.artifact
        assert "## Recent Commits" in artifacts.artifact
        assert "98c4a15 test(serial): Sub-AC 3 — truncation event" in artifacts.artifact
        assert "## HEAD Commit Diff Stat" in artifacts.artifact
        assert "1 file changed" in artifacts.artifact

        # Reference artifact echoes the raw captures for the QA judge to dig in.
        # Pull _RECENT_LOG_LIMIT from the module so this assertion stays in
        # sync with any future tweak to the constant.
        from ouroboros.evaluation.verification_artifacts import _RECENT_LOG_LIMIT

        assert f"## git log --oneline -n {_RECENT_LOG_LIMIT}" in artifacts.reference
        assert "## git show --stat HEAD" in artifacts.reference

        # Manifest persists raw history evidence for downstream tooling.
        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["history_available"] is True
        assert manifest["history_error"] is None
        assert len(manifest["recent_commits"]) == 4
        assert manifest["recent_commits"][0].startswith("98c4a15")

        # Public dataclass surface includes the parsed commits.
        assert len(artifacts.recent_commits) == 4

    @pytest.mark.asyncio
    async def test_uncommitted_changes_still_surfaced_alongside_recent_commits(
        self,
        tmp_path: Path,
    ) -> None:
        """When BOTH dirty state and recent commits exist, both are reported."""
        config = MechanicalConfig(timeout_seconds=30, working_dir=tmp_path)

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                if command[:3] == ("git", "status", "--short"):
                    return CommandResult(0, " M src/ouroboros/example.py\n", "")
                if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                    return CommandResult(0, " M src/ouroboros/example.py\0", "")
                if command[:3] == ("git", "diff", "--stat"):
                    return CommandResult(0, " src/ouroboros/example.py | 2 +-\n", "")
                if command[:3] == ("git", "log", "--oneline"):
                    return CommandResult(0, "abc1234 feat: shipped feature\n", "")
                if command[:2] == ("git", "show"):
                    return CommandResult(0, "abc1234\nfeat: shipped feature\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_dirty_and_log", "done", tmp_path)

        # Dirty file should still appear; the new committed-history note must
        # NOT replace it (changed_files takes precedence in the bullet section).
        assert "src/ouroboros/example.py" in artifacts.artifact
        assert "see Recent Commits below" not in artifacts.artifact
        # …but the recent commits section is still rendered for context.
        assert "## Recent Commits" in artifacts.artifact
        assert "abc1234 feat: shipped feature" in artifacts.artifact

    @pytest.mark.asyncio
    async def test_head_show_stat_renders_without_recent_log(
        self,
        tmp_path: Path,
    ) -> None:
        """A successful HEAD show with empty log (e.g. shallow clone) still renders.

        Edge case: ``git log --oneline`` returns nothing but ``git show HEAD``
        succeeds. Before the fix, the HEAD diff stat was nested under the
        ``recent_commits`` block and got dropped — now it renders independently.
        """
        config = MechanicalConfig(timeout_seconds=30, working_dir=tmp_path)

        head_show = (
            "deadbeefdeadbeefdeadbeef\n"
            "feat: shallow-only HEAD\n"
            "\n"
            "Keithm\n"
            "\n"
            " src/foo.py | 1 +\n"
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(0, "", "")
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "log", "--oneline"):
                # Shallow clone: log unavailable / empty.
                return CommandResult(0, "", "")
            if command[:2] == ("git", "show"):
                return CommandResult(0, head_show, "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_show_no_log", "done", tmp_path)

        # Recent Commits section should NOT appear (no log).
        assert "## Recent Commits" not in artifacts.artifact
        # HEAD diff stat must still render — that's the whole point of the fix.
        assert "## HEAD Commit Diff Stat" in artifacts.artifact
        assert "src/foo.py | 1 +" in artifacts.artifact

    @pytest.mark.asyncio
    async def test_history_capture_failure_is_non_fatal(
        self,
        tmp_path: Path,
    ) -> None:
        """A failing log/show capture must not fail the artifact build."""
        config = MechanicalConfig(timeout_seconds=30, working_dir=tmp_path)

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command[:3] == ("git", "status", "--short"):
                return CommandResult(0, "", "")
            if command[:4] == ("git", "status", "--porcelain=v1", "-z"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "diff", "--stat"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "log", "--oneline"):
                return CommandResult(128, "", "fatal: bad object\n")
            if command[:2] == ("git", "show"):
                return CommandResult(128, "", "fatal: ambiguous argument 'HEAD'\n")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_history_fail", "done", tmp_path)

        # No commits surfaced; original "no changed files detected" returns
        # because there's nothing to suggest otherwise.
        assert "(no changed files detected)" in artifacts.artifact
        assert "## Recent Commits" not in artifacts.artifact
        assert artifacts.recent_commits == ()

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["history_available"] is False
        assert manifest["history_error"] is not None


class TestAutoDetectIntegration:
    """`build_verification_artifacts` must author mechanical.toml when missing."""

    @pytest.mark.asyncio
    async def test_auto_detect_runs_when_toml_absent(self, tmp_path: Path) -> None:
        """When no toml exists, ensure_mechanical_toml is invoked with the provided adapter."""
        workdir = tmp_path / "project"
        workdir.mkdir()
        called: dict[str, object] = {}

        async def fake_ensure(
            working_dir: Path,
            adapter: object,
            *,
            backend: object = None,
            **_: object,
        ) -> bool:
            called["working_dir"] = working_dir
            called["adapter"] = adapter
            called["backend"] = backend
            return True

        sentinel_adapter = object()
        with patch(
            "ouroboros.evaluation.verification_artifacts.ensure_mechanical_toml",
            side_effect=fake_ensure,
        ):
            await _auto_detect_mechanical_toml(workdir, sentinel_adapter, "codex")  # type: ignore[arg-type]

        assert called["working_dir"] == workdir
        assert called["adapter"] is sentinel_adapter
        assert called["backend"] == "codex"

    @pytest.mark.asyncio
    async def test_auto_detect_uses_default_adapter_when_none_supplied(
        self,
        tmp_path: Path,
    ) -> None:
        """Callers that do not thread an adapter still get Stage 1 coverage."""
        workdir = tmp_path / "project"
        workdir.mkdir()
        default_adapter = object()
        ensure_calls: list[tuple[Path, object, object]] = []

        async def fake_ensure(
            working_dir: Path,
            adapter: object,
            *,
            backend: object = None,
            **_: object,
        ) -> bool:
            ensure_calls.append((working_dir, adapter, backend))
            return True

        with (
            patch(
                "ouroboros.providers.factory.create_llm_adapter",
                return_value=default_adapter,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.ensure_mechanical_toml",
                side_effect=fake_ensure,
            ),
        ):
            await _auto_detect_mechanical_toml(workdir, None)

        assert ensure_calls == [(workdir, default_adapter, None)]

    @pytest.mark.asyncio
    async def test_auto_detect_swallows_adapter_construction_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """If the default adapter cannot be built, detect is silently skipped."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        def boom(**_: object) -> object:
            raise RuntimeError("no backend configured")

        ensure_calls: list[tuple[Path, object, object]] = []

        async def fake_ensure(
            working_dir: Path,
            adapter: object,
            *,
            backend: object = None,
            **_: object,
        ) -> bool:
            ensure_calls.append((working_dir, adapter, backend))
            return True

        with (
            patch(
                "ouroboros.providers.factory.create_llm_adapter",
                side_effect=boom,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.ensure_mechanical_toml",
                side_effect=fake_ensure,
            ),
        ):
            await _auto_detect_mechanical_toml(workdir, None)

        assert ensure_calls == []  # never invoked

    @pytest.mark.asyncio
    async def test_build_skips_auto_detect_when_toml_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-authored toml short-circuits the detector call entirely."""
        workdir = tmp_path / "project"
        (workdir / ".ouroboros").mkdir(parents=True)
        (workdir / ".ouroboros" / "mechanical.toml").write_text('test = "pytest -q"\n')

        ensure_mock = AsyncMock(return_value=True)
        config = MechanicalConfig(
            test_command=("pytest", "-q"),
            timeout_seconds=30,
            working_dir=workdir,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            return CommandResult(0, "ok\n", "")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.ensure_mechanical_toml",
                new=ensure_mock,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            await build_verification_artifacts(
                "exec-1",
                "some output",
                workdir,
            )

        ensure_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_git_state_captured_before_detector_authors_toml(
        self,
        tmp_path: Path,
    ) -> None:
        """Detector must not pollute the captured git diff with its own toml."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        sequence: list[str] = []

        async def fake_git(
            working_dir: Path,  # noqa: ARG001
            artifact_dir: Path,  # noqa: ARG001
        ) -> tuple[tuple[str, ...], str, str, bool, str | None]:
            sequence.append("git_state")
            return ((), "", "", True, None)

        async def fake_detect(
            working_dir: Path,  # noqa: ARG001
            adapter: object,  # noqa: ARG001
            llm_backend: object = None,  # noqa: ARG001
        ) -> None:
            sequence.append("detect")

        config = MechanicalConfig(timeout_seconds=30, working_dir=workdir)
        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts._capture_git_state",
                side_effect=fake_git,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts._auto_detect_mechanical_toml",
                side_effect=fake_detect,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.has_mechanical_toml",
                return_value=False,
            ),
        ):
            await build_verification_artifacts(
                "exec-ordering",
                "execution output",
                workdir,
            )

        assert sequence == ["git_state", "detect"], (
            "git state must be captured before the detector writes .ouroboros/mechanical.toml"
        )
