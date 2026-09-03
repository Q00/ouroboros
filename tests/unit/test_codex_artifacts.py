"""Unit tests for packaged Codex artifact installation."""

import ctypes
import errno
import os
from pathlib import Path
import shutil

import pytest

from ouroboros.codex import artifacts as codex_artifacts
from ouroboros.codex.artifacts import (
    _SKILL_CAPABILITY_GUIDE_MARKER,
    CODEX_RULE_FILENAME,
    CODEX_SKILL_NAMESPACE,
    CodexManagedArtifact,
    CodexPackagedAssets,
    install_codex_rules,
    install_codex_skills,
    load_packaged_codex_rules,
    load_packaged_codex_skill,
    resolve_packaged_codex_assets,
    resolve_packaged_codex_skill_path,
)


class TestInstallCodexRules:
    """Test installation of the packaged Codex rules asset."""

    @staticmethod
    def _write_rule(rules_dir: Path, rule_name: str, content: str) -> Path:
        rule_path = rules_dir / rule_name
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(content, encoding="utf-8")
        return rule_path

    def test_windows_rename_does_not_load_posix_process_library(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Windows rename path must not call ``ctypes.CDLL(None)`` first."""
        source_path = tmp_path / "source.txt"
        target_path = tmp_path / "target.txt"
        source_path.write_text("managed content", encoding="utf-8")

        def _unexpected_cdll(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Windows rename attempted to load a POSIX process library")

        monkeypatch.setattr(codex_artifacts.os, "name", "nt")
        monkeypatch.setattr(codex_artifacts.ctypes, "CDLL", _unexpected_cdll)

        codex_artifacts._rename_noreplace(source_path, target_path)

        assert not source_path.exists()
        assert target_path.read_text(encoding="utf-8") == "managed content"

    def test_installs_packaged_rules_into_default_codex_rules_dir(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """Default install path should be ``~/.codex/rules/ouroboros.md``."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

        installed_path = install_codex_rules()

        assert installed_path == tmp_path / ".codex" / "rules" / CODEX_RULE_FILENAME
        assert installed_path.read_text(encoding="utf-8") == load_packaged_codex_rules()

    def test_replaces_existing_rules_file_with_packaged_content(self, tmp_path: Path) -> None:
        """Rule refresh should replace every packaged Ouroboros rule asset."""
        packaged_rules_dir = tmp_path / "packaged-rules"
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        target_path = rules_dir / CODEX_RULE_FILENAME
        secondary_target_path = rules_dir / "ouroboros-status.md"
        target_path.parent.mkdir(parents=True)
        target_path.write_text("stale rules", encoding="utf-8")
        secondary_target_path.write_text("stale secondary rules", encoding="utf-8")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")
        self._write_rule(packaged_rules_dir, "ouroboros-status.md", "# status rules\n")
        self._write_rule(packaged_rules_dir, "team.md", "# unrelated\n")

        installed_path = install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert installed_path == target_path
        installed_content = installed_path.read_text(encoding="utf-8")
        assert installed_content.startswith("# fresh rules\n")
        assert _SKILL_CAPABILITY_GUIDE_MARKER in installed_content
        assert secondary_target_path.read_text(encoding="utf-8") == "# status rules\n"
        assert not rules_dir.joinpath("team.md").exists()

    def test_checks_read_generation_before_replacing_existing_rule(self, tmp_path: Path) -> None:
        """A setup-owned rule refresh must be rejectable before replacement."""
        packaged_rules_dir = tmp_path / "packaged-rules"
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "rules" / CODEX_RULE_FILENAME
        target_path.parent.mkdir(parents=True)
        target_path.write_text("operator rule\n", encoding="utf-8")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")

        def _reject(path: Path) -> None:
            assert path == target_path
            assert path.read_text(encoding="utf-8") == "operator rule\n"
            raise OSError("stale rule generation")

        with pytest.raises(OSError, match="stale rule generation"):
            install_codex_rules(
                codex_dir=codex_dir,
                rules_dir=packaged_rules_dir,
                before_mutation=_reject,
            )

        assert target_path.read_text(encoding="utf-8") == "operator rule\n"

    def test_partial_primary_rule_write_preserves_existing_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed temp-file write never exposes partial bytes at the managed path."""
        packaged_rules_dir = tmp_path / "packaged-rules"
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "rules" / CODEX_RULE_FILENAME
        target_path.parent.mkdir(parents=True)
        target_path.write_text("original rule\n", encoding="utf-8")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")
        original_write_bytes = Path.write_bytes

        def _partial_write(path: Path, data: bytes) -> int:
            if path.parent == target_path.parent and path.suffix == ".tmp":
                original_write_bytes(path, data[:8])
                raise OSError("synthetic partial write")
            return original_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", _partial_write)

        with pytest.raises(OSError, match="synthetic partial write"):
            install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert target_path.read_text(encoding="utf-8") == "original rule\n"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))

    def test_failed_rule_staging_preserves_directory_shaped_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rule staging failure must not delete an existing directory topology."""
        packaged_rules_dir = tmp_path / "packaged-rules"
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "rules" / CODEX_RULE_FILENAME
        target_path.mkdir(parents=True)
        target_path.joinpath("operator.txt").write_text("keep", encoding="utf-8")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")
        original_write_bytes = Path.write_bytes

        def _fail_staging_write(path: Path, data: bytes) -> int:
            if path.parent == target_path.parent and path.name.endswith(".tmp"):
                raise OSError("synthetic rule staging failure")
            return original_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", _fail_staging_write)

        with pytest.raises(OSError, match="synthetic rule staging failure"):
            install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert target_path.joinpath("operator.txt").read_text(encoding="utf-8") == "keep"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))

    def test_packaged_rules_delegate_auto_monitoring_out_of_main_session(self) -> None:
        """Codex rules should assign one child observer exclusive polling ownership."""
        rules = load_packaged_codex_rules()
        compact = " ".join(rules.split())

        assert "explicitly delegate that object to exactly one native Codex subagent" in compact
        assert "read-only and exclusively owns" in compact
        assert "do not poll the same job from both sessions" in compact
        assert "the main conversation remains available" in compact
        assert "ouroboros tui open" in compact
        assert "attention_required" in compact
        assert "use an isolated worktree" in compact
        assert "Do not hand the user polling instructions as the final UX" in compact
        assert "ouroboros_job_result(job_id)" in compact

    def test_refresh_does_not_prune_stale_namespaced_rules_by_default(self, tmp_path: Path) -> None:
        """Setup refresh should leave removed Ouroboros rules untouched unless update-mode prune is requested."""
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        packaged_rules_dir = tmp_path / "packaged-rules"
        stale_namespaced_rule = rules_dir / "ouroboros-legacy.md"
        unrelated_rule = rules_dir / "team.md"
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")
        rules_dir.mkdir(parents=True)
        stale_namespaced_rule.write_text("keep for refresh-only", encoding="utf-8")
        unrelated_rule.write_text("keep me", encoding="utf-8")

        installed_path = install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert installed_path == rules_dir / CODEX_RULE_FILENAME
        installed_content = installed_path.read_text(encoding="utf-8")
        assert installed_content.startswith("# fresh rules\n")
        assert _SKILL_CAPABILITY_GUIDE_MARKER in installed_content
        assert stale_namespaced_rule.read_text(encoding="utf-8") == "keep for refresh-only"
        assert unrelated_rule.read_text(encoding="utf-8") == "keep me"

    def test_prunes_removed_namespaced_rules_when_requested(self, tmp_path: Path) -> None:
        """Update-mode install should remove stale Ouroboros-owned rule files only."""
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        packaged_rules_dir = tmp_path / "packaged-rules"
        stale_namespaced_rule = rules_dir / "ouroboros-legacy.md"
        unrelated_rule = rules_dir / "team.md"
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# upgraded rules\n")
        self._write_rule(packaged_rules_dir, "ouroboros-status.md", "# upgraded status\n")
        rules_dir.mkdir(parents=True)
        stale_namespaced_rule.write_text("remove me", encoding="utf-8")
        unrelated_rule.write_text("keep me", encoding="utf-8")

        installed_path = install_codex_rules(
            codex_dir=codex_dir,
            rules_dir=packaged_rules_dir,
            prune=True,
        )

        assert installed_path == rules_dir / CODEX_RULE_FILENAME
        installed_content = installed_path.read_text(encoding="utf-8")
        assert installed_content.startswith("# upgraded rules\n")
        assert _SKILL_CAPABILITY_GUIDE_MARKER in installed_content
        assert rules_dir.joinpath("ouroboros-status.md").read_text(encoding="utf-8") == (
            "# upgraded status\n"
        )
        assert not stale_namespaced_rule.exists()
        assert unrelated_rule.read_text(encoding="utf-8") == "keep me"

    def test_refuses_symlinked_rules_root(self, tmp_path: Path) -> None:
        """Rule install must not follow a symlinked managed rules directory."""
        codex_dir = tmp_path / ".codex"
        outside_dir = tmp_path / "outside-rules"
        packaged_rules_dir = tmp_path / "packaged-rules"
        outside_dir.mkdir()
        (codex_dir).mkdir()
        (codex_dir / "rules").symlink_to(outside_dir, target_is_directory=True)
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")

        with pytest.raises(OSError, match="symlinked directory"):
            install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert not (outside_dir / CODEX_RULE_FILENAME).exists()

    def test_refuses_symlinked_codex_dir_for_rules(self, tmp_path: Path) -> None:
        """Rule install must not follow a symlinked Codex root ancestor."""
        codex_dir = tmp_path / ".codex"
        outside_codex_dir = tmp_path / "outside-codex"
        packaged_rules_dir = tmp_path / "packaged-rules"
        outside_codex_dir.mkdir()
        codex_dir.symlink_to(outside_codex_dir, target_is_directory=True)
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")

        with pytest.raises(OSError, match="symlinked"):
            install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert not outside_codex_dir.joinpath("rules", CODEX_RULE_FILENAME).exists()

    def test_replaces_dangling_symlinked_rule_leaf(self, tmp_path: Path) -> None:
        """Rule refresh should replace a dangling managed rule symlink leaf."""
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        packaged_rules_dir = tmp_path / "packaged-rules"
        missing_target = tmp_path / "missing-outside-rule.md"
        target_path = rules_dir / CODEX_RULE_FILENAME
        rules_dir.mkdir(parents=True)
        target_path.symlink_to(missing_target)
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")

        installed_path = install_codex_rules(codex_dir=codex_dir, rules_dir=packaged_rules_dir)

        assert installed_path == target_path
        assert not installed_path.is_symlink()
        assert installed_path.read_text(encoding="utf-8").startswith("# fresh rules\n")
        assert not missing_target.exists()

    def test_refuses_relative_rules_root_from_symlinked_cwd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Relative rule installs must not resolve through a symlinked cwd."""
        real_workspace = tmp_path / "real-workspace"
        symlink_workspace = tmp_path / "linked-workspace"
        packaged_rules_dir = tmp_path / "packaged-rules"
        real_workspace.mkdir()
        symlink_workspace.symlink_to(real_workspace, target_is_directory=True)
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# fresh rules\n")
        monkeypatch.chdir(symlink_workspace)
        monkeypatch.setenv("PWD", str(symlink_workspace))

        with pytest.raises(OSError, match="symlinked"):
            install_codex_rules(codex_dir=".codex", rules_dir=packaged_rules_dir)

        assert not real_workspace.joinpath(".codex", "rules", CODEX_RULE_FILENAME).exists()

    def test_packaged_rules_fail_closed_for_ooo_auto(self) -> None:
        """Codex rules must route ``ooo auto`` to the real MCP tool, not manual work."""
        rules = load_packaged_codex_rules()
        compact = " ".join(rules.split())

        assert "| `ooo auto ...` | `ouroboros_start_auto`" in rules
        assert "Do not emulate it with manual" in rules
        assert "If that MCP tool is unavailable" in compact
        assert "Do not call a `blocked` or `failed` auto-session result a dispatch" in compact

    def test_packaged_rules_include_rendered_skill_capability_guide(self) -> None:
        """Codex rules should include the generated runtime skill capability guide."""
        rules = load_packaged_codex_rules()

        assert _SKILL_CAPABILITY_GUIDE_MARKER in rules
        assert "## Ouroboros Skill Capability Guide: Codex" in rules
        assert "### When a skill requires `ask_user`" in rules
        assert "### When a skill requires `run_lateral_review`" in rules
        assert "### When a skill requires `run_closure_gate`" in rules
        assert "lateral_review_required=true" in rules
        assert "MCP `seed-ready`" in rules

    def test_rendered_skill_capability_guide_is_idempotent(self, tmp_path: Path) -> None:
        """Refreshing from an already rendered rule source should not duplicate the guide."""
        packaged_rules_dir = tmp_path / "packaged-rules"
        rendered_once = (
            f"# custom rules\n\n{_SKILL_CAPABILITY_GUIDE_MARKER}\n## stale generated guide\n"
        )
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, rendered_once)

        installed_path = install_codex_rules(
            codex_dir=tmp_path / ".codex",
            rules_dir=packaged_rules_dir,
        )
        installed_content = installed_path.read_text(encoding="utf-8")

        assert installed_content.count(_SKILL_CAPABILITY_GUIDE_MARKER) == 1
        assert "## stale generated guide" not in installed_content
        assert "## Ouroboros Skill Capability Guide: Codex" in installed_content


class TestRenameNoReplaceOnFilesystemsWithoutTheFlag:
    """Setup must still install when the kernel or filesystem rejects RENAME_NOREPLACE.

    NFS answers ``renameat2(..., RENAME_NOREPLACE)`` with ``EINVAL`` and renames
    nothing, so a home directory on NFS fails every Codex artifact install — and
    the rollback, which renames the same way, leaves the previous generation
    deleted. These tests pin the requirement: an unsupported flag degrades to the
    portable path, and the no-replace guarantee itself still holds.
    """

    @staticmethod
    def _force_renameat2_errno(
        monkeypatch: pytest.MonkeyPatch,
        error_number: int,
    ) -> list[int]:
        """Make ``renameat2`` fail with ``error_number`` without renaming anything."""
        calls: list[int] = []

        class _FailingRenameat2:
            argtypes: object = None
            restype: object = None

            def __call__(self, *_args: object) -> int:
                calls.append(error_number)
                ctypes.set_errno(error_number)
                return -1

        class _FakeLibc:
            renameat2 = _FailingRenameat2()

        monkeypatch.setattr(codex_artifacts.os, "name", "posix")
        monkeypatch.setattr(codex_artifacts.sys, "platform", "linux")
        monkeypatch.setattr(codex_artifacts.ctypes, "CDLL", lambda *_a, **_kw: _FakeLibc())
        return calls

    def test_installs_file_artifact_when_the_flag_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rules file must install on a filesystem without RENAME_NOREPLACE."""
        calls = self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        installed_path = install_codex_rules(codex_dir=tmp_path / ".codex")

        assert calls, "the rejected renameat2 path was never exercised"
        assert installed_path.read_text(encoding="utf-8") == load_packaged_codex_rules()

    def test_installs_directory_artifact_when_the_flag_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skills install as directories, which the hard-link fallback cannot handle."""
        source_skills_dir = tmp_path / "packaged-skills"
        skill_dir = source_skills_dir / "run"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: run\n---\n", encoding="utf-8")
        calls = self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        installed_paths = install_codex_skills(
            codex_dir=tmp_path / ".codex",
            skills_dir=source_skills_dir,
        )

        assert calls, "the rejected renameat2 path was never exercised"
        assert installed_paths == (tmp_path / ".codex" / "skills" / f"{CODEX_SKILL_NAMESPACE}run",)
        assert installed_paths[0].joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "---\nname: run\n---\n"
        )

    def test_refresh_over_an_existing_generation_keeps_the_rule_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed commit used to strand the previous rule in a hidden backup sibling."""
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / CODEX_RULE_FILENAME).write_text("previous generation", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        installed_path = install_codex_rules(codex_dir=codex_dir)

        assert installed_path.read_text(encoding="utf-8") == load_packaged_codex_rules()
        assert [entry.name for entry in rules_dir.iterdir()] == [CODEX_RULE_FILENAME]

    @pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP])
    def test_fallback_still_refuses_an_occupied_file_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_number: int,
    ) -> None:
        """Degrading the primitive must not degrade the no-replace guarantee."""
        source_path = tmp_path / "source.txt"
        target_path = tmp_path / "target.txt"
        source_path.write_text("staged", encoding="utf-8")
        target_path.write_text("another writer", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, error_number)

        with pytest.raises(FileExistsError):
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert target_path.read_text(encoding="utf-8") == "another writer"
        assert source_path.read_text(encoding="utf-8") == "staged"

    def test_fallback_still_refuses_an_occupied_directory_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty directory target would be silently replaced by a bare ``os.rename``."""
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        target_path.mkdir()
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        with pytest.raises(FileExistsError):
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert list(target_path.iterdir()) == []
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_directory_commit_admits_an_occupied_name_only_through_the_reservation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Admission is the ``mkdir`` reservation, never an existence check.

        POSIX ``rename`` removes an empty destination directory, so a writer that
        holds the target would lose it silently if publication were admitted by a
        check. The reservation refuses the name instead, even when every earlier
        check reports it free.
        """
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        target_path.mkdir()  # the racing writer, invisible to any earlier check
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)
        monkeypatch.setattr(codex_artifacts.os.path, "lexists", lambda _path: False)

        with pytest.raises(FileExistsError):
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert list(target_path.iterdir()) == []
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_directory_commit_leaves_no_reservation_when_publication_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed publication must not leave the reserved name occupied."""
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        def _refuse_rename(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

        monkeypatch.setattr(codex_artifacts.os, "rename", _refuse_rename)

        with pytest.raises(OSError):
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert not target_path.exists()

    def test_refresh_failed_directory_publication_restores_previous_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_skills_dir = tmp_path / "packaged-skills"
        skill_dir = source_skills_dir / "run"
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text("fresh skill", encoding="utf-8")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed skill", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)
        real_stat = os.stat
        stat_failures_remaining = 1

        def _fail_first_target_stat(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            nonlocal stat_failures_remaining
            if (
                Path(os.fsdecode(path)) == target_path
                and kwargs.get("follow_symlinks") is False
                and not target_path.joinpath("SKILL.md").exists()
                and stat_failures_remaining
            ):
                stat_failures_remaining -= 1
                raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))
            return real_stat(path, *args, **kwargs)

        real_rename = os.rename

        def _refuse_staged_directory_publication(
            source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        ) -> None:
            if (
                Path(os.fsdecode(source)).name.endswith(".tmp")
                and Path(os.fsdecode(destination)) == target_path
            ):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            real_rename(source, destination)

        monkeypatch.setattr(codex_artifacts.os, "stat", _fail_first_target_stat)
        monkeypatch.setattr(codex_artifacts.os, "rename", _refuse_staged_directory_publication)

        with pytest.raises(OSError, match=os.strerror(errno.EXDEV)):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "installed skill"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))

    def test_failed_staging_cleanup_does_not_report_a_failed_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Linking publishes the target, so later cleanup cannot fail the commit.

        Raising after the link made the caller roll back a generation that was
        already live, leaving the previous one stranded in its backup sibling.
        """
        codex_dir = tmp_path / ".codex"
        rules_dir = codex_dir / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / CODEX_RULE_FILENAME).write_text("previous generation", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        real_unlink = Path.unlink
        refused: list[Path] = []

        def _refuse_first_staging_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if not refused and self.name.startswith(".") and self.name.endswith(".tmp"):
                refused.append(self)
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(self))
            real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", _refuse_first_staging_unlink)

        installed_path = install_codex_rules(codex_dir=codex_dir)

        assert refused, "the staging cleanup failure was never exercised"
        assert installed_path.read_text(encoding="utf-8") == load_packaged_codex_rules()
        # No stranded previous generation, and no staging hard link left aliasing
        # the live rule.
        assert [entry.name for entry in rules_dir.iterdir()] == [CODEX_RULE_FILENAME]

    def test_failed_publication_never_removes_a_reservation_taken_over(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup must prove ownership: a failed publication is not a licence to delete.

        A writer that removes the reservation and puts their own directory at the
        same path keeps it, even though publication fails afterwards.
        """
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        def _take_over_the_reservation_then_fail(*_args: object, **_kwargs: object) -> None:
            os.rmdir(target_path)
            target_path.mkdir()  # a different writer now owns this pathname
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

        monkeypatch.setattr(codex_artifacts.os, "rename", _take_over_the_reservation_then_fail)

        with pytest.raises(OSError) as publication_error:
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert publication_error.value.errno == errno.EXDEV
        assert target_path.is_dir(), "cleanup deleted a directory it did not reserve"
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_failed_publication_never_removes_a_renamed_reservation_replacement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live descriptor does not prove that the target still names its directory."""
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        held_reservation_path = tmp_path / "held-reservation"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)
        real_rename = os.rename

        def _rename_reservation_away_then_fail(*_args: object, **_kwargs: object) -> None:
            real_rename(target_path, held_reservation_path)
            target_path.mkdir()  # a different writer now owns this pathname
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(
            codex_artifacts.os,
            "rename",
            _rename_reservation_away_then_fail,
        )

        with pytest.raises(OSError) as publication_error:
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert publication_error.value.errno == errno.EIO
        assert target_path.is_dir(), "cleanup deleted a directory it did not reserve"
        assert held_reservation_path.is_dir(), "the original reservation was not preserved"
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_directory_commit_never_destroys_a_racing_writer_with_content(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reservation window cannot cost another writer their tree.

        Publication consumes the reserved name with a POSIX rename, which refuses
        a destination that is not empty. This bounds the residual reservation race
        documented on ``_rename_noreplace_fallback``: a racing writer that put
        anything in the path keeps it.
        """
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)

        real_mkdir = os.mkdir

        def _fill_the_reservation(path: object, *args: object, **kwargs: object) -> None:
            real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
            Path(str(path)).joinpath("operator.txt").write_text("theirs", encoding="utf-8")

        monkeypatch.setattr(codex_artifacts.os, "mkdir", _fill_the_reservation)

        with pytest.raises(OSError) as publication_error:
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert publication_error.value.errno == errno.ENOTEMPTY
        assert target_path.joinpath("operator.txt").read_text(encoding="utf-8") == "theirs"
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_unpinnable_reservation_does_not_occupy_the_final_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reservation that cannot be pinned must not survive the failure.

        Leaving it at the final name makes every later replay see a collision
        that never resolves, with the previous generation still hidden in its
        backup sibling — the failure mode this whole fallback exists to remove.
        """
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)
        real_open = os.open

        def _refuse_to_pin_the_reservation(path: object, *args: object, **kwargs: object) -> int:
            if (
                isinstance(path, str | bytes | os.PathLike)
                and Path(os.fsdecode(path)) == target_path
            ):
                raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(codex_artifacts.os, "open", _refuse_to_pin_the_reservation)

        with pytest.raises(OSError) as publication_error:
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert publication_error.value.errno == errno.EMFILE
        assert not target_path.exists(), "an unpinned reservation was left at the final name"
        assert source_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"

    def test_release_failure_after_the_rename_keeps_the_publication_committed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rename commits, so releasing the descriptor cannot fail the commit.

        Reporting the release as a failed publication would send the caller into
        a rollback that deletes the generation already live and strands the
        previous one in its backup sibling.
        """
        source_path = tmp_path / "source"
        target_path = tmp_path / "target"
        source_path.mkdir()
        (source_path / "SKILL.md").write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EINVAL)
        real_open = os.open
        real_close = os.close
        reservations: list[int] = []

        def _record_the_reservation(path: object, *args: object, **kwargs: object) -> int:
            descriptor = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            if (
                isinstance(path, str | bytes | os.PathLike)
                and Path(os.fsdecode(path)) == target_path
            ):
                reservations.append(descriptor)
            return descriptor

        def _refuse_to_release_the_reservation(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor in reservations:
                raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(codex_artifacts.os, "open", _record_the_reservation)
        monkeypatch.setattr(codex_artifacts.os, "close", _refuse_to_release_the_reservation)

        codex_artifacts._rename_noreplace(source_path, target_path)

        assert reservations, "the directory branch did not pin a reservation"
        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "staged"
        assert not source_path.exists()

    def test_existing_target_reported_by_renameat2_is_not_degraded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``EEXIST`` is the guarantee working, not a missing capability."""
        source_path = tmp_path / "source.txt"
        target_path = tmp_path / "target.txt"
        source_path.write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EEXIST)

        with pytest.raises(FileExistsError):
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert not target_path.exists()

    def test_unrelated_rename_failure_still_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only capability errnos may fall through to the portable path."""
        source_path = tmp_path / "source.txt"
        target_path = tmp_path / "target.txt"
        source_path.write_text("staged", encoding="utf-8")
        self._force_renameat2_errno(monkeypatch, errno.EACCES)

        with pytest.raises(OSError) as rename_error:
            codex_artifacts._rename_noreplace(source_path, target_path)

        assert rename_error.value.errno == errno.EACCES
        assert not target_path.exists()


class TestLoadPackagedCodexSkills:
    """Test packaged Codex skill entrypoint resolution helpers."""

    @staticmethod
    def _write_skill(skills_dir: Path, skill_name: str, *, body: str = "# Skill\n") -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(body, encoding="utf-8")
        return skill_md_path

    def test_loads_explicit_packaged_skill_markdown(self, tmp_path: Path) -> None:
        """Explicit skill bundles should expose the packaged SKILL.md contents."""
        packaged_skills_dir = tmp_path / "packaged-skills"
        skill_md_path = self._write_skill(
            packaged_skills_dir,
            "interview",
            body="---\nname: interview\n---\n",
        )

        assert load_packaged_codex_skill(
            "interview", skills_dir=packaged_skills_dir
        ) == skill_md_path.read_text(encoding="utf-8")

    def test_resolves_repo_packaged_skill_path_by_default(self) -> None:
        """Default skill lookup should resolve the packaged Codex skill bundle."""
        with resolve_packaged_codex_skill_path("run") as skill_md_path:
            assert skill_md_path.parent.name == "run"
            assert skill_md_path.name == "SKILL.md"
            assert skill_md_path.read_text(encoding="utf-8").startswith(
                "---\nname: ouroboros-run\n"
            )

    def test_packaged_auto_skill_forbids_manual_fallback(self) -> None:
        """The auto skill body must not allow silent manual emulation."""
        skill = load_packaged_codex_skill("auto")

        compact = " ".join(skill.split())
        assert "must be executed by invoking MCP tool `ouroboros_start_auto`" in compact
        assert "manual fallback is not an `ooo auto` run" in compact
        assert "Do not label a `blocked` or `failed` outcome as MCP dispatch failure" in compact
        assert "The user should not have to poll the job manually" in compact
        assert "ouroboros_job_result" in compact

    def test_packaged_interview_skill_uses_runtime_capability_terms(self) -> None:
        """Runtime skill instructions should not hardcode Claude-only tool surfaces."""
        skill = load_packaged_codex_skill("interview")

        assert "AskUserQuestion" not in skill
        assert "ToolSearch" not in skill
        assert "Read/Glob/Grep" not in skill
        assert "WebFetch/WebSearch" not in skill
        assert "active runtime's `ask_user` capability" in skill
        assert "active runtime's tool-discovery capability" in skill
        assert "`run_lateral_review`" in skill

    def test_raises_when_explicit_packaged_skill_is_missing(self, tmp_path: Path) -> None:
        """Missing skill entrypoints should fail fast."""
        packaged_skills_dir = tmp_path / "packaged-skills"
        packaged_skills_dir.mkdir(parents=True)

        with pytest.raises(FileNotFoundError, match="missing"):
            load_packaged_codex_skill("missing", skills_dir=packaged_skills_dir)


class TestInstallCodexSkills:
    """Test installation of packaged Codex skill assets."""

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        *,
        body: str = "# Skill\n",
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        for relative_path, content in (extra_files or {}).items():
            file_path = skill_dir / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        return skill_dir

    def test_installs_packaged_skills_into_default_codex_skills_dir(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """Default install path should namespace every packaged skill under ``~/.codex/skills``."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(
            source_skills_dir,
            "run",
            body="---\nname: run\n---\n",
            extra_files={"notes.txt": "copied"},
        )
        self._write_skill(
            source_skills_dir,
            "interview",
            body="---\nname: interview\n---\n",
        )
        # Non-skill directories are ignored.
        misc_dir = source_skills_dir / "misc"
        misc_dir.mkdir(parents=True)
        (misc_dir / "README.md").write_text("not a skill", encoding="utf-8")

        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

        installed_paths = install_codex_skills(skills_dir=source_skills_dir)

        assert installed_paths == (
            tmp_path / ".codex" / "skills" / f"{CODEX_SKILL_NAMESPACE}interview",
            tmp_path / ".codex" / "skills" / f"{CODEX_SKILL_NAMESPACE}run",
        )
        assert installed_paths[1].joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "---\nname: run\n---\n"
        )
        assert installed_paths[1].joinpath("notes.txt").read_text(encoding="utf-8") == "copied"
        assert not (tmp_path / ".codex" / "skills" / f"{CODEX_SKILL_NAMESPACE}misc").exists()

    def test_replaces_existing_skill_directory_with_packaged_content(self, tmp_path: Path) -> None:
        """Setup refresh should remove stale files before copying the packaged skill tree."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(
            source_skills_dir,
            "status",
            body="fresh skill",
            extra_files={"nested/config.json": '{"fresh": true}'},
        )

        codex_dir = tmp_path / ".codex"
        stale_skill_dir = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status"
        stale_skill_dir.mkdir(parents=True)
        (stale_skill_dir / "SKILL.md").write_text("stale skill", encoding="utf-8")
        (stale_skill_dir / "old.txt").write_text("remove me", encoding="utf-8")

        installed_paths = install_codex_skills(
            codex_dir=codex_dir,
            skills_dir=source_skills_dir,
        )

        assert installed_paths == (stale_skill_dir,)
        assert stale_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "fresh skill"
        assert stale_skill_dir.joinpath("nested/config.json").read_text(encoding="utf-8") == (
            '{"fresh": true}'
        )
        assert not stale_skill_dir.joinpath("old.txt").exists()

    def test_checks_read_generation_before_removing_existing_skill(self, tmp_path: Path) -> None:
        """A setup-owned skill refresh must be rejectable before removal."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh skill")

        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("operator skill", encoding="utf-8")

        def _reject(path: Path) -> None:
            assert path == target_path
            assert path.joinpath("SKILL.md").read_text(encoding="utf-8") == "operator skill"
            raise OSError("stale skill generation")

        with pytest.raises(OSError, match="stale skill generation"):
            install_codex_skills(
                codex_dir=codex_dir,
                skills_dir=source_skills_dir,
                before_mutation=_reject,
            )

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "operator skill"

    def test_failed_staging_copy_preserves_concurrently_created_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed staging cleanup must never delete a target another process created."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh skill")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status"

        def _copytree_then_race(
            _source: Path,
            _destination: Path,
            *args: object,
            **kwargs: object,
        ) -> Path:
            del args, kwargs
            target_path.mkdir(parents=True)
            target_path.joinpath("SKILL.md").write_text("operator skill", encoding="utf-8")
            raise OSError("synthetic staging copy failure")

        monkeypatch.setattr(shutil, "copytree", _copytree_then_race)

        with pytest.raises(OSError, match="synthetic staging copy failure"):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "operator skill"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))

    def test_failed_final_swap_restores_previous_skill_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed staged swap must atomically restore the installed skill."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed skill", encoding="utf-8")
        original_rename_noreplace = codex_artifacts._rename_noreplace

        def _fail_staging_swap(source: Path, destination: Path) -> None:
            if destination == target_path and source.name.endswith(".tmp"):
                raise OSError("synthetic final swap failure")
            original_rename_noreplace(source, destination)

        monkeypatch.setattr(codex_artifacts, "_rename_noreplace", _fail_staging_swap)

        with pytest.raises(OSError, match="synthetic final swap failure"):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "installed skill"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))

    def test_generation_callback_failure_restores_previous_skill_generation(
        self,
        tmp_path: Path,
    ) -> None:
        """Bookkeeping failure before commit must restore the installed skill."""
        source_skills_dir = tmp_path / "packaged-skills"
        source_skill = self._write_skill(source_skills_dir, "run", body="fresh skill")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed skill", encoding="utf-8")

        def _fail_new_generation(generation: object) -> None:
            source_path = getattr(generation, "source_path", None)
            if source_path == source_skill:
                raise OSError("synthetic generation snapshot failure")

        with pytest.raises(OSError, match="synthetic generation snapshot failure"):
            install_codex_skills(
                codex_dir=codex_dir,
                skills_dir=source_skills_dir,
                on_generation=_fail_new_generation,
            )

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "installed skill"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))

    def test_partial_backup_cleanup_keeps_committed_skill_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Partial disposal cannot corrupt the active or prior skill generation."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed skill", encoding="utf-8")
        target_path.joinpath("operator.txt").write_text(
            "preserve as one generation", encoding="utf-8"
        )
        original_remove = codex_artifacts._remove_installed_artifact

        def _partially_remove_disposal(path: Path) -> None:
            if path.name.endswith(".discard"):
                path.joinpath("SKILL.md").unlink()
                raise OSError("synthetic partial disposal failure")
            original_remove(path)

        monkeypatch.setattr(
            codex_artifacts,
            "_remove_installed_artifact",
            _partially_remove_disposal,
        )

        install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "fresh skill"
        assert not target_path.joinpath("operator.txt").exists()
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))
        disposal_paths = tuple(target_path.parent.glob(".*.discard"))
        assert len(disposal_paths) == 1
        assert disposal_paths[0].joinpath("operator.txt").read_text(encoding="utf-8") == (
            "preserve as one generation"
        )

    def test_backup_detach_failure_restores_previous_skill_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure before the cleanup commit boundary must restore the intact backup."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill")
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed skill", encoding="utf-8")
        original_replace = os.replace

        def _fail_backup_detach(source: str | Path, destination: str | Path) -> None:
            if Path(source).name.endswith(".backup") and Path(destination).name.endswith(
                ".discard"
            ):
                raise OSError("synthetic backup detach failure")
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", _fail_backup_detach)

        with pytest.raises(OSError, match="synthetic backup detach failure"):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == "installed skill"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))
        assert not tuple(target_path.parent.glob(".*.discard"))

    def test_prune_bookkeeping_failure_restores_removed_skill(
        self,
        tmp_path: Path,
    ) -> None:
        """Transactional pruning must restore a target when bookkeeping fails."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status")
        codex_dir = tmp_path / ".codex"
        stale_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}legacy"
        stale_path.mkdir(parents=True)
        stale_path.joinpath("SKILL.md").write_text("installed legacy", encoding="utf-8")

        def _fail_stale_removal(generation: object) -> None:
            if getattr(generation, "target_path", None) == stale_path and getattr(
                generation, "missing", False
            ):
                raise OSError("synthetic prune bookkeeping failure")

        with pytest.raises(OSError, match="synthetic prune bookkeeping failure"):
            install_codex_skills(
                codex_dir=codex_dir,
                skills_dir=source_skills_dir,
                prune=True,
                on_generation=_fail_stale_removal,
            )

        assert stale_path.joinpath("SKILL.md").read_text(encoding="utf-8") == ("installed legacy")
        assert not tuple(stale_path.parent.glob(f".{stale_path.name}.*.backup"))

    def test_partial_prune_cleanup_keeps_removal_committed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Partial post-commit cleanup cannot restore a damaged pruned generation."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status")
        codex_dir = tmp_path / ".codex"
        stale_path = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}legacy"
        stale_path.mkdir(parents=True)
        stale_path.joinpath("SKILL.md").write_text("installed legacy", encoding="utf-8")
        stale_path.joinpath("operator.txt").write_text("old companion", encoding="utf-8")
        original_remove = codex_artifacts._remove_installed_artifact

        def _partially_remove_disposal(path: Path) -> None:
            if path.name.endswith(".discard"):
                path.joinpath("SKILL.md").unlink()
                raise OSError("synthetic partial prune disposal failure")
            original_remove(path)

        monkeypatch.setattr(
            codex_artifacts,
            "_remove_installed_artifact",
            _partially_remove_disposal,
        )

        install_codex_skills(
            codex_dir=codex_dir,
            skills_dir=source_skills_dir,
            prune=True,
        )

        assert not stale_path.exists()
        assert not tuple(stale_path.parent.glob(f".{stale_path.name}.*.backup"))
        disposal_paths = tuple(stale_path.parent.glob(".*.discard"))
        assert len(disposal_paths) == 1
        assert disposal_paths[0].joinpath("operator.txt").read_text(encoding="utf-8") == (
            "old companion"
        )

    def test_refreshes_existing_namespaced_skills_from_updated_packaged_bundle(
        self,
        tmp_path: Path,
    ) -> None:
        """Update refresh should replace installed Ouroboros skills with the latest packaged copies."""
        codex_dir = tmp_path / ".codex"
        initial_skills_dir = tmp_path / "packaged-skills-v1"
        refreshed_skills_dir = tmp_path / "packaged-skills-v2"

        self._write_skill(
            initial_skills_dir,
            "run",
            body="run v1",
            extra_files={"notes.txt": "old run notes"},
        )
        self._write_skill(
            initial_skills_dir,
            "status",
            body="status v1",
            extra_files={"old.txt": "remove on refresh"},
        )
        install_codex_skills(codex_dir=codex_dir, skills_dir=initial_skills_dir)

        self._write_skill(
            refreshed_skills_dir,
            "run",
            body="run v2",
            extra_files={"notes.txt": "new run notes"},
        )
        self._write_skill(
            refreshed_skills_dir,
            "status",
            body="status v2",
            extra_files={"nested/config.json": '{"fresh": true}'},
        )

        installed_paths = install_codex_skills(codex_dir=codex_dir, skills_dir=refreshed_skills_dir)
        run_skill_dir = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run"
        status_skill_dir = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status"

        assert installed_paths == (run_skill_dir, status_skill_dir)
        assert run_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "run v2"
        assert run_skill_dir.joinpath("notes.txt").read_text(encoding="utf-8") == "new run notes"
        assert status_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "status v2"
        assert status_skill_dir.joinpath("nested/config.json").read_text(encoding="utf-8") == (
            '{"fresh": true}'
        )
        assert not status_skill_dir.joinpath("old.txt").exists()

    def test_installs_repo_packaged_skills_by_default(self, tmp_path: Path, monkeypatch) -> None:
        """Default installs should use the packaged Ouroboros skills bundle."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

        installed_paths = install_codex_skills()
        installed_names = {path.name for path in installed_paths}

        assert f"{CODEX_SKILL_NAMESPACE}setup" in installed_names
        assert f"{CODEX_SKILL_NAMESPACE}run" in installed_names
        assert all(path.joinpath("SKILL.md").is_file() for path in installed_paths)

    def test_refresh_does_not_prune_removed_namespaced_skills_by_default(
        self, tmp_path: Path
    ) -> None:
        """Setup refresh should not remove stale namespaced skills unless update-mode prune is enabled."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")

        codex_dir = tmp_path / ".codex"
        skills_dir = codex_dir / "skills"
        stale_skill_dir = skills_dir / f"{CODEX_SKILL_NAMESPACE}legacy"
        unrelated_skill_dir = skills_dir / "team-helper"
        stale_skill_dir.mkdir(parents=True)
        unrelated_skill_dir.mkdir(parents=True)
        (stale_skill_dir / "SKILL.md").write_text("stale", encoding="utf-8")
        (unrelated_skill_dir / "SKILL.md").write_text("keep", encoding="utf-8")

        installed_paths = install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert installed_paths == (skills_dir / f"{CODEX_SKILL_NAMESPACE}status",)
        assert stale_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "stale"
        assert unrelated_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "keep"

    def test_prunes_removed_namespaced_skills_when_requested(self, tmp_path: Path) -> None:
        """Update-mode install should prune stale Ouroboros-owned skills only."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")

        codex_dir = tmp_path / ".codex"
        skills_dir = codex_dir / "skills"
        stale_skill_dir = skills_dir / f"{CODEX_SKILL_NAMESPACE}legacy"
        unrelated_skill_dir = skills_dir / "team-helper"
        stale_skill_dir.mkdir(parents=True)
        unrelated_skill_dir.mkdir(parents=True)
        (stale_skill_dir / "SKILL.md").write_text("stale", encoding="utf-8")
        (unrelated_skill_dir / "SKILL.md").write_text("keep", encoding="utf-8")

        installed_paths = install_codex_skills(
            codex_dir=codex_dir,
            skills_dir=source_skills_dir,
            prune=True,
        )

        assert installed_paths == (skills_dir / f"{CODEX_SKILL_NAMESPACE}status",)
        assert not stale_skill_dir.exists()
        assert unrelated_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "keep"

    def test_raises_when_packaged_skill_bundle_is_empty_before_pruning(
        self, tmp_path: Path
    ) -> None:
        """Update should fail fast on an empty packaged bundle without deleting installed skills."""
        codex_dir = tmp_path / ".codex"
        installed_skill_dir = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status"
        empty_bundle_dir = tmp_path / "packaged-skills"
        installed_skill_dir.mkdir(parents=True)
        (installed_skill_dir / "SKILL.md").write_text("installed status", encoding="utf-8")
        empty_bundle_dir.mkdir(parents=True)
        (empty_bundle_dir / "README.md").write_text("not a skill", encoding="utf-8")

        with pytest.raises(FileNotFoundError, match="SKILL.md"):
            install_codex_skills(
                codex_dir=codex_dir,
                skills_dir=empty_bundle_dir,
                prune=True,
            )

        assert installed_skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "installed status"
        )

    def test_refuses_symlinked_skills_root(self, tmp_path: Path) -> None:
        """Skill install must not copy or prune through a symlinked managed root."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")

        codex_dir = tmp_path / ".codex"
        outside_dir = tmp_path / "outside-skills"
        outside_dir.mkdir()
        codex_dir.mkdir()
        (codex_dir / "skills").symlink_to(outside_dir, target_is_directory=True)
        legacy_skill = outside_dir / f"{CODEX_SKILL_NAMESPACE}legacy"
        legacy_skill.mkdir()
        legacy_skill.joinpath("SKILL.md").write_text("outside stale", encoding="utf-8")

        with pytest.raises(OSError, match="symlinked directory"):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir, prune=True)

        assert legacy_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "outside stale"
        assert not outside_dir.joinpath(f"{CODEX_SKILL_NAMESPACE}status").exists()

    def test_refuses_symlinked_codex_dir_for_skills(self, tmp_path: Path) -> None:
        """Skill install must not copy or prune through a symlinked Codex root ancestor."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")

        codex_dir = tmp_path / ".codex"
        outside_codex_dir = tmp_path / "outside-codex"
        outside_skills_dir = outside_codex_dir / "skills"
        outside_skills_dir.mkdir(parents=True)
        legacy_skill = outside_skills_dir / f"{CODEX_SKILL_NAMESPACE}legacy"
        legacy_skill.mkdir()
        legacy_skill.joinpath("SKILL.md").write_text("outside stale", encoding="utf-8")
        codex_dir.symlink_to(outside_codex_dir, target_is_directory=True)

        with pytest.raises(OSError, match="symlinked"):
            install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir, prune=True)

        assert legacy_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "outside stale"
        assert not outside_skills_dir.joinpath(f"{CODEX_SKILL_NAMESPACE}status").exists()

    def test_replaces_dangling_symlinked_skill_leaf(self, tmp_path: Path) -> None:
        """Skill refresh should replace a dangling managed skill symlink leaf."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")

        codex_dir = tmp_path / ".codex"
        skills_dir = codex_dir / "skills"
        missing_target = tmp_path / "missing-outside-skill"
        target_path = skills_dir / f"{CODEX_SKILL_NAMESPACE}status"
        skills_dir.mkdir(parents=True)
        target_path.symlink_to(missing_target, target_is_directory=True)

        installed_paths = install_codex_skills(codex_dir=codex_dir, skills_dir=source_skills_dir)

        assert installed_paths == (target_path,)
        assert not target_path.is_symlink()
        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "fresh status skill"
        )
        assert not missing_target.exists()

    def test_refuses_relative_skills_root_from_symlinked_cwd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Relative skill installs must not resolve through a symlinked cwd."""
        source_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(source_skills_dir, "status", body="fresh status skill")
        real_workspace = tmp_path / "real-workspace"
        symlink_workspace = tmp_path / "linked-workspace"
        real_workspace.mkdir()
        symlink_workspace.symlink_to(real_workspace, target_is_directory=True)
        monkeypatch.chdir(symlink_workspace)
        monkeypatch.setenv("PWD", str(symlink_workspace))

        with pytest.raises(OSError, match="symlinked"):
            install_codex_skills(codex_dir=".codex", skills_dir=source_skills_dir)

        assert not real_workspace.joinpath(
            ".codex", "skills", f"{CODEX_SKILL_NAMESPACE}status"
        ).exists()


class TestResolvePackagedCodexAssets:
    """Test packaged asset resolution used by Codex setup/update flows."""

    @staticmethod
    def _write_skill(skills_dir: Path, skill_name: str, *, body: str = "# Skill\n") -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        return skill_dir

    @staticmethod
    def _write_rule(rules_dir: Path, rule_name: str, content: str) -> Path:
        rule_path = rules_dir / rule_name
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(content, encoding="utf-8")
        return rule_path

    def test_resolves_explicit_skill_bundle_and_matching_rules_file(self, tmp_path: Path) -> None:
        """Explicit asset roots should produce deterministic skill metadata and rules path."""
        packaged_skills_dir = tmp_path / "packaged-skills"
        packaged_rules_path = tmp_path / "packaged-rules" / CODEX_RULE_FILENAME
        self._write_skill(packaged_skills_dir, "setup")
        self._write_skill(packaged_skills_dir, "interview")
        (packaged_skills_dir / "notes").mkdir(parents=True)
        packaged_rules_path.parent.mkdir(parents=True)
        packaged_rules_path.write_text("# custom rules\n", encoding="utf-8")

        with resolve_packaged_codex_assets(
            skills_dir=packaged_skills_dir,
            rules_path=packaged_rules_path,
        ) as assets:
            assert isinstance(assets, CodexPackagedAssets)
            assert isinstance(assets.managed_artifacts[0], CodexManagedArtifact)
            assert [skill.skill_name for skill in assets.skills] == ["interview", "setup"]
            assert [skill.install_dir_name for skill in assets.skills] == [
                f"{CODEX_SKILL_NAMESPACE}interview",
                f"{CODEX_SKILL_NAMESPACE}setup",
            ]
            assert all(skill.skill_md_path.is_file() for skill in assets.skills)
            assert assets.rules_path == packaged_rules_path
            assert [artifact.artifact_type for artifact in assets.managed_artifacts] == [
                "rule",
                "skill",
                "skill",
            ]
            assert [path.as_posix() for path in assets.managed_relative_install_paths] == [
                f"rules/{CODEX_RULE_FILENAME}",
                f"skills/{CODEX_SKILL_NAMESPACE}interview",
                f"skills/{CODEX_SKILL_NAMESPACE}setup",
            ]
            assert [artifact.source_path for artifact in assets.managed_artifacts] == [
                packaged_rules_path,
                packaged_skills_dir / "interview",
                packaged_skills_dir / "setup",
            ]

    def test_resolves_explicit_rules_directory_as_managed_rule_set(self, tmp_path: Path) -> None:
        """Explicit rules directories should expose every managed Ouroboros rule asset."""
        packaged_skills_dir = tmp_path / "packaged-skills"
        packaged_rules_dir = tmp_path / "packaged-rules"
        self._write_skill(packaged_skills_dir, "setup")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# primary\n")
        self._write_rule(packaged_rules_dir, "ouroboros-status.md", "# status\n")
        self._write_rule(packaged_rules_dir, "team.md", "# ignore\n")

        with resolve_packaged_codex_assets(
            skills_dir=packaged_skills_dir,
            rules_dir=packaged_rules_dir,
        ) as assets:
            assert assets.rules_path == packaged_rules_dir / CODEX_RULE_FILENAME
            assert [
                artifact.relative_install_path.as_posix() for artifact in assets.managed_artifacts
            ] == [
                f"rules/{CODEX_RULE_FILENAME}",
                "rules/ouroboros-status.md",
                f"skills/{CODEX_SKILL_NAMESPACE}setup",
            ]

    def test_resolves_repo_skills_and_packaged_rules_by_default(self) -> None:
        """Source checkouts should still resolve the repo skills tree plus packaged rules."""
        with resolve_packaged_codex_assets() as assets:
            assert assets.rules_path.name == CODEX_RULE_FILENAME
            assert assets.rules_path.is_file()
            assert "setup" in {skill.skill_name for skill in assets.skills}
            assert "run" in {skill.skill_name for skill in assets.skills}
            assert assets.managed_relative_install_paths[0] == Path("rules") / CODEX_RULE_FILENAME
            assert Path("skills") / f"{CODEX_SKILL_NAMESPACE}setup" in (
                assets.managed_relative_install_paths
            )
            assert Path("skills") / f"{CODEX_SKILL_NAMESPACE}run" in (
                assets.managed_relative_install_paths
            )

    def test_raises_when_explicit_rules_path_is_missing(self, tmp_path: Path) -> None:
        """A missing rules file should fail resolution before setup copies anything."""
        packaged_skills_dir = tmp_path / "packaged-skills"
        self._write_skill(packaged_skills_dir, "setup")

        with pytest.raises(FileNotFoundError, match="rules file"):
            with resolve_packaged_codex_assets(
                skills_dir=packaged_skills_dir,
                rules_path=tmp_path / "missing" / CODEX_RULE_FILENAME,
            ):
                pass


class TestCodexAssetSyncSmoke:
    """Smoke tests for combined Codex setup/update asset synchronization."""

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        *,
        body: str = "# Skill\n",
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        for relative_path, content in (extra_files or {}).items():
            file_path = skill_dir / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        return skill_dir

    @staticmethod
    def _write_rule(rules_dir: Path, rule_name: str, content: str) -> Path:
        rule_path = rules_dir / rule_name
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(content, encoding="utf-8")
        return rule_path

    @staticmethod
    def _sync_assets(
        *,
        codex_dir: Path,
        skills_dir: Path | None = None,
        rules_dir: Path | None = None,
        prune: bool,
    ) -> tuple[CodexPackagedAssets, Path, tuple[Path, ...]]:
        with resolve_packaged_codex_assets(
            skills_dir=skills_dir,
            rules_dir=rules_dir,
        ) as assets:
            installed_rule = install_codex_rules(
                codex_dir=codex_dir,
                rules_dir=rules_dir,
                prune=prune,
            )
            installed_skills = install_codex_skills(
                codex_dir=codex_dir,
                skills_dir=skills_dir,
                prune=prune,
            )
        return assets, installed_rule, installed_skills

    @staticmethod
    def _collect_managed_install_paths(codex_dir: Path) -> set[Path]:
        rules_dir = codex_dir / "rules"
        skills_dir = codex_dir / "skills"
        installed_paths: set[Path] = set()

        if rules_dir.is_dir():
            installed_paths.update(
                path.relative_to(codex_dir)
                for path in rules_dir.iterdir()
                if path.name == CODEX_RULE_FILENAME or path.name.startswith("ouroboros-")
            )

        if skills_dir.is_dir():
            installed_paths.update(
                path.relative_to(codex_dir)
                for path in skills_dir.iterdir()
                if path.name.startswith(CODEX_SKILL_NAMESPACE)
            )

        return installed_paths

    def test_setup_smoke_syncs_packaged_skills_and_rules_without_pruning(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo setup` should refresh packaged assets without pruning existing managed installs."""
        codex_dir = tmp_path / ".codex"
        packaged_skills_dir = tmp_path / "packaged-skills-v1"
        packaged_rules_dir = tmp_path / "packaged-rules-v1"
        self._write_skill(
            packaged_skills_dir,
            "run",
            body="run v1",
            extra_files={"notes.txt": "seed path support"},
        )
        self._write_skill(packaged_skills_dir, "setup", body="setup v1")
        self._write_rule(packaged_rules_dir, CODEX_RULE_FILENAME, "# codex rules v1\n")
        self._write_rule(packaged_rules_dir, "ouroboros-status.md", "# status rules v1\n")
        self._write_rule(packaged_rules_dir, "team.md", "# ignore me\n")

        stale_rule = codex_dir / "rules" / "ouroboros-legacy.md"
        unrelated_rule = codex_dir / "rules" / "team.md"
        stale_skill = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}legacy"
        unrelated_skill = codex_dir / "skills" / "team-helper"
        stale_rule.parent.mkdir(parents=True, exist_ok=True)
        stale_skill.parent.mkdir(parents=True, exist_ok=True)
        stale_rule.write_text("keep during setup", encoding="utf-8")
        unrelated_rule.write_text("keep unrelated rule", encoding="utf-8")
        (stale_skill / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (stale_skill / "SKILL.md").write_text("keep during setup", encoding="utf-8")
        (unrelated_skill / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (unrelated_skill / "SKILL.md").write_text("keep unrelated skill", encoding="utf-8")

        assets, installed_rule, installed_skills = self._sync_assets(
            codex_dir=codex_dir,
            skills_dir=packaged_skills_dir,
            rules_dir=packaged_rules_dir,
            prune=False,
        )

        assert installed_rule == codex_dir / "rules" / CODEX_RULE_FILENAME
        installed_content = installed_rule.read_text(encoding="utf-8")
        assert installed_content.startswith("# codex rules v1\n")
        assert _SKILL_CAPABILITY_GUIDE_MARKER in installed_content
        assert installed_skills == (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run",
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}setup",
        )
        assert assets.managed_relative_install_paths == (
            Path("rules") / CODEX_RULE_FILENAME,
            Path("rules") / "ouroboros-status.md",
            Path("skills") / f"{CODEX_SKILL_NAMESPACE}run",
            Path("skills") / f"{CODEX_SKILL_NAMESPACE}setup",
        )
        assert all((codex_dir / path).exists() for path in assets.managed_relative_install_paths)
        assert (codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run" / "notes.txt").read_text(
            encoding="utf-8"
        ) == "seed path support"
        assert stale_rule.read_text(encoding="utf-8") == "keep during setup"
        assert stale_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "keep during setup"
        assert unrelated_rule.read_text(encoding="utf-8") == "keep unrelated rule"
        assert unrelated_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "keep unrelated skill"
        )

    def test_update_smoke_refreshes_packaged_assets_and_prunes_stale_installs(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo update` should refresh managed assets and prune removed Ouroboros installs."""
        codex_dir = tmp_path / ".codex"
        initial_skills_dir = tmp_path / "packaged-skills-v1"
        initial_rules_dir = tmp_path / "packaged-rules-v1"
        refreshed_skills_dir = tmp_path / "packaged-skills-v2"
        refreshed_rules_dir = tmp_path / "packaged-rules-v2"

        self._write_skill(
            initial_skills_dir,
            "run",
            body="run v1",
            extra_files={"notes.txt": "old run notes"},
        )
        self._write_skill(initial_skills_dir, "status", body="status v1")
        self._write_rule(initial_rules_dir, CODEX_RULE_FILENAME, "# codex rules v1\n")
        self._write_rule(initial_rules_dir, "ouroboros-status.md", "# status rules v1\n")
        self._sync_assets(
            codex_dir=codex_dir,
            skills_dir=initial_skills_dir,
            rules_dir=initial_rules_dir,
            prune=False,
        )

        stale_rule = codex_dir / "rules" / "ouroboros-legacy.md"
        unrelated_rule = codex_dir / "rules" / "team.md"
        stale_skill = codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}legacy"
        unrelated_skill = codex_dir / "skills" / "team-helper"
        stale_rule.write_text("remove on update", encoding="utf-8")
        unrelated_rule.write_text("keep unrelated rule", encoding="utf-8")
        (stale_skill / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (stale_skill / "SKILL.md").write_text("remove on update", encoding="utf-8")
        (unrelated_skill / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (unrelated_skill / "SKILL.md").write_text("keep unrelated skill", encoding="utf-8")

        self._write_skill(
            refreshed_skills_dir,
            "interview",
            body="interview v2",
            extra_files={"prompts.txt": "clarify requirements"},
        )
        self._write_skill(
            refreshed_skills_dir,
            "run",
            body="run v2",
            extra_files={"notes.txt": "new run notes"},
        )
        self._write_rule(refreshed_rules_dir, CODEX_RULE_FILENAME, "# codex rules v2\n")
        self._write_rule(refreshed_rules_dir, "ouroboros-setup.md", "# setup rules v2\n")

        assets, installed_rule, installed_skills = self._sync_assets(
            codex_dir=codex_dir,
            skills_dir=refreshed_skills_dir,
            rules_dir=refreshed_rules_dir,
            prune=True,
        )

        assert installed_rule == codex_dir / "rules" / CODEX_RULE_FILENAME
        installed_content = installed_rule.read_text(encoding="utf-8")
        assert installed_content.startswith("# codex rules v2\n")
        assert _SKILL_CAPABILITY_GUIDE_MARKER in installed_content
        assert installed_skills == (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}interview",
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run",
        )
        assert (codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run" / "notes.txt").read_text(
            encoding="utf-8"
        ) == "new run notes"
        assert (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}interview" / "prompts.txt"
        ).read_text(encoding="utf-8") == "clarify requirements"
        assert not stale_rule.exists()
        assert not stale_skill.exists()
        assert not (codex_dir / "rules" / "ouroboros-status.md").exists()
        assert not (codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}status").exists()
        assert unrelated_rule.read_text(encoding="utf-8") == "keep unrelated rule"
        assert unrelated_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "keep unrelated skill"
        )
        assert self._collect_managed_install_paths(codex_dir) == set(
            assets.managed_relative_install_paths
        )
