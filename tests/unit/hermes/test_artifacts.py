"""Unit tests for Hermes skill artifact installation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from ouroboros.hermes.artifacts import (
    _SWAP_MARKER,
    _SWAP_MARKER_CONTENT,
    HERMES_SKILL_CAPABILITY_GUIDE_FILENAME,
    HERMES_SKILL_CATEGORY,
    _remove_target_path,
    install_hermes_skills,
)


class TestInstallHermesSkills:
    """Test installation of the packaged Hermes skill bundle."""

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        *,
        body: str = "---\nname: skill\n---\n",
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

    def test_installs_repo_root_skills_into_hermes_namespace(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Editable installs should copy the repo-root shared skills bundle for Hermes."""
        repo_root = tmp_path / "repo"
        source_skills_dir = repo_root / "skills"
        self._write_skill(
            source_skills_dir,
            "run",
            body="---\nname: run\n---\n",
            extra_files={"notes.txt": "copied"},
        )
        self._write_skill(source_skills_dir, "interview", body="---\nname: interview\n---\n")

        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        installed_path = install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert installed_path == (
            tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        )
        assert installed_path.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == (
            "---\nname: run\n---\n"
        )
        assert installed_path.joinpath("run", "notes.txt").read_text(encoding="utf-8") == "copied"
        assert installed_path.joinpath("interview", "SKILL.md").is_file()

    def test_installs_runtime_skill_capability_guide(self, tmp_path: Path, monkeypatch) -> None:
        """Hermes installs should include backend-specific skill execution guidance."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "interview", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        installed_path = install_hermes_skills(hermes_dir=tmp_path / ".hermes")
        guide = installed_path.joinpath(HERMES_SKILL_CAPABILITY_GUIDE_FILENAME).read_text(
            encoding="utf-8"
        )

        assert guide.startswith("## Ouroboros Skill Capability Guide: Hermes\n")
        for capability_name in (
            "ask_user",
            "inspect_code",
            "call_mcp",
            "run_lateral_review",
            "web_research",
            "run_shell",
            "refine_answer",
            "maintain_ledger",
            "run_closure_gate",
            "restate_goal",
        ):
            assert f"### When a skill requires `{capability_name}`" in guide

    def test_replaces_existing_hermes_bundle(self, tmp_path: Path, monkeypatch) -> None:
        """Refreshing the Hermes install should replace managed skill directories."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(
            source_skills_dir,
            "status",
            body="fresh skill\n",
            extra_files={"nested/config.json": '{"fresh": true}'},
        )
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        target_dir = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        stale_skill_dir = target_dir / "status"
        stale_skill_dir.mkdir(parents=True)
        stale_skill_dir.joinpath("stale.txt").write_text("remove me", encoding="utf-8")

        installed_path = install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert installed_path == target_dir
        assert not stale_skill_dir.joinpath("stale.txt").exists()
        assert target_dir.joinpath("status", "SKILL.md").read_text(encoding="utf-8") == (
            "fresh skill\n"
        )
        assert (
            target_dir.joinpath("status", "nested", "config.json").read_text(encoding="utf-8")
            == '{"fresh": true}'
        )

    def test_refresh_removes_legacy_package_scaffolding(self, tmp_path: Path, monkeypatch) -> None:
        """Refreshing the Hermes bundle should clean old package helper artifacts."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        target_dir = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target_dir.mkdir(parents=True)
        target_dir.joinpath("__init__.py").write_text("legacy", encoding="utf-8")

        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert not target_dir.joinpath("__init__.py").exists()
        assert target_dir.joinpath("run", "SKILL.md").is_file()

    def test_prune_removes_stale_managed_skill_directories(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Prune mode should remove managed skill directories absent from the source bundle."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        target_dir = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        stale_skill_dir = target_dir / "status"
        stale_skill_dir.mkdir(parents=True)
        stale_skill_dir.joinpath("SKILL.md").write_text("stale skill\n", encoding="utf-8")
        target_dir.joinpath("notes.txt").write_text("keep me", encoding="utf-8")

        install_hermes_skills(hermes_dir=tmp_path / ".hermes", prune=True)

        assert not stale_skill_dir.exists()
        assert target_dir.joinpath("run", "SKILL.md").is_file()
        assert target_dir.joinpath("notes.txt").read_text(encoding="utf-8") == "keep me"

    def test_mid_copy_failure_preserves_existing_generation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed staged refresh must leave the live Hermes skills byte-identical."""
        source_skills_dir = tmp_path / "source-skills"
        source_skill_dir = self._write_skill(source_skills_dir, "run", body="new skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        live_skill = target_dir / "run" / "SKILL.md"
        live_skill.parent.mkdir(parents=True)
        live_skill.write_text("working skill\n", encoding="utf-8")

        real_copytree = shutil.copytree

        def fail_new_generation(src, dst, *args, **kwargs):
            if Path(src) == source_skill_dir:
                Path(dst).mkdir(parents=True, exist_ok=True)
                Path(dst, "SKILL.md").write_text("partial", encoding="utf-8")
                raise OSError("simulated disk full")
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr("ouroboros.hermes.artifacts.shutil.copytree", fail_new_generation)

        with pytest.raises(OSError, match="disk full"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes", prune=True)

        assert live_skill.read_bytes() == b"working skill\n"

    def test_non_directory_live_target_is_refused_without_mutation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An operator-owned obstruction must survive a failed activation byte-for-byte."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator-owned obstruction\x00")

        with pytest.raises(OSError, match="non-directory Hermes skill target"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert target.is_file()
        assert target.read_bytes() == b"operator-owned obstruction\x00"

    def test_failed_fresh_install_leaves_no_refresh_eligible_target(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed first activation must not look like an installed runtime."""
        source_skills_dir = tmp_path / "source-skills"
        source_skill_dir = self._write_skill(source_skills_dir, "run", body="new skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        real_copytree = shutil.copytree

        def fail_new_generation(src, dst, *args, **kwargs):
            if Path(src) == source_skill_dir:
                raise OSError("simulated fresh copy failure")
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr("ouroboros.hermes.artifacts.shutil.copytree", fail_new_generation)

        with pytest.raises(OSError, match="fresh copy failure"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert not target_dir.exists()
        assert not target_dir.is_symlink()

    def test_symlinked_ancestor_is_rejected_before_backup_recovery(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        real_root = tmp_path / "real-hermes"
        real_root.mkdir()
        outside_backup = real_root / "skills" / HERMES_SKILL_CATEGORY / ".ouroboros.old.dead"
        outside_backup.mkdir(parents=True)
        outside_backup.joinpath(_SWAP_MARKER).write_text(_SWAP_MARKER_CONTENT, encoding="utf-8")
        outside_backup.joinpath("operator.txt").write_text("keep", encoding="utf-8")
        linked_root = tmp_path / ".hermes"
        linked_root.symlink_to(real_root, target_is_directory=True)

        with pytest.raises(OSError, match="symlinked"):
            install_hermes_skills(hermes_dir=linked_root)

        assert outside_backup.joinpath("operator.txt").read_text(encoding="utf-8") == "keep"

    def test_unmanaged_symlinks_survive_staged_copy_without_dereference(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        external_dir = tmp_path / "operator-data"
        external_dir.mkdir()
        external_dir.joinpath("secret.txt").write_text("do not copy", encoding="utf-8")
        target_dir.mkdir(parents=True)
        target_dir.joinpath("operator-link").symlink_to(external_dir, target_is_directory=True)

        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        link = target_dir / "operator-link"
        assert link.is_symlink()
        assert link.resolve() == external_dir.resolve()

    def test_preexisting_live_marker_is_refused_and_preserved_byte_for_byte(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        target_dir.mkdir(parents=True)
        original_marker = target_dir / _SWAP_MARKER
        original_marker.write_text("operator marker\n", encoding="utf-8")
        with pytest.raises(OSError, match="reserved Hermes swap marker"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert original_marker.read_text(encoding="utf-8") == "operator marker\n"

    def test_symlinked_managed_backup_is_never_followed_or_recovered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        target_dir.parent.mkdir(parents=True)
        external_dir = tmp_path / "external-generation"
        external_dir.mkdir()
        external_dir.joinpath(_SWAP_MARKER).write_text(_SWAP_MARKER_CONTENT, encoding="utf-8")
        external_secret = external_dir / "operator-secret.txt"
        external_secret.write_text("do not ingest", encoding="utf-8")
        backup_link = target_dir.with_name(".ouroboros.old.symlink")
        backup_link.symlink_to(external_dir, target_is_directory=True)

        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert external_secret.read_text(encoding="utf-8") == "do not ingest"
        assert backup_link.is_symlink()
        assert not target_dir.joinpath("operator-secret.txt").exists()

    def test_malformed_managed_backup_marker_fails_as_controlled_oserror(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        parent = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY
        backup = parent / ".ouroboros.old.corrupt"
        backup.mkdir(parents=True)
        backup.joinpath(_SWAP_MARKER).write_bytes(b"\xff\xfe")

        with pytest.raises(OSError, match="malformed Hermes backup marker"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

    def test_interrupted_swap_recovers_previous_generation_on_retry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A crash after backup creation must be recoverable by the next refresh."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="new skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        live_note = target_dir / "operator-notes.txt"
        live_note.parent.mkdir(parents=True)
        live_note.write_text("keep across crash", encoding="utf-8")

        real_replace = os.replace
        calls = 0

        def interrupt_after_backup(src, dst):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return real_replace(src, dst)

        monkeypatch.setattr("ouroboros.hermes.artifacts.os.replace", interrupt_after_backup)
        with pytest.raises(KeyboardInterrupt):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        monkeypatch.setattr("ouroboros.hermes.artifacts.os.replace", real_replace)
        real_copytree = shutil.copytree
        failed_recovery_copy = False

        def fail_source_copy_once(src, dst, *args, **kwargs):
            nonlocal failed_recovery_copy
            if Path(src) == source_skills_dir / "run" and not failed_recovery_copy:
                failed_recovery_copy = True
                raise OSError("synthetic recovery copy failure")
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr("ouroboros.hermes.artifacts.shutil.copytree", fail_source_copy_once)
        with pytest.raises(OSError, match="recovery copy failure"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert not target_dir.joinpath(_SWAP_MARKER).exists()
        monkeypatch.setattr("ouroboros.hermes.artifacts.shutil.copytree", real_copytree)
        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert live_note.read_text(encoding="utf-8") == "keep across crash"
        assert target_dir.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == "new skill\n"

    def test_recovery_marker_cleanup_failure_leaves_retryable_backup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        parent = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY
        backup = parent / ".ouroboros.old.recoverable"
        backup.mkdir(parents=True)
        backup.joinpath(_SWAP_MARKER).write_text(_SWAP_MARKER_CONTENT, encoding="utf-8")
        backup.joinpath("operator-note.txt").write_text("preserve\n", encoding="utf-8")
        target = parent / "ouroboros"
        real_remove = _remove_target_path
        failed = False

        def fail_live_marker_cleanup(path: Path) -> None:
            nonlocal failed
            if path == target / _SWAP_MARKER and not failed:
                failed = True
                raise OSError("simulated marker cleanup failure")
            real_remove(path)

        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._remove_target_path", fail_live_marker_cleanup
        )
        with pytest.raises(OSError, match="marker cleanup failure"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert not target.exists()
        assert backup.joinpath(_SWAP_MARKER).is_file()
        monkeypatch.setattr("ouroboros.hermes.artifacts._remove_target_path", real_remove)
        install_hermes_skills(hermes_dir=tmp_path / ".hermes")
        assert target.joinpath("operator-note.txt").read_text(encoding="utf-8") == "preserve\n"

    def test_cleanup_failure_then_interruption_recovers_newest_generation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A stale backup cannot displace a newer live generation during recovery."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="first refresh\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        operator_note = target_dir / "operator-notes.txt"
        operator_note.parent.mkdir(parents=True)
        operator_note.write_text("newest operator state", encoding="utf-8")

        real_remove = _remove_target_path
        failed_cleanup = False

        def fail_first_backup_cleanup(path: Path) -> None:
            nonlocal failed_cleanup
            if path.name.startswith(".ouroboros.old.") and not failed_cleanup:
                failed_cleanup = True
                raise OSError("simulated backup cleanup failure")
            real_remove(path)

        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._remove_target_path", fail_first_backup_cleanup
        )
        with pytest.raises(OSError, match="backup cleanup failure"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        real_replace = os.replace
        live_backup_seen = False

        def interrupt_after_newest_backup(src, dst):
            nonlocal live_backup_seen
            result = real_replace(src, dst)
            if Path(src) == target_dir:
                live_backup_seen = True
                raise KeyboardInterrupt
            return result

        monkeypatch.setattr("ouroboros.hermes.artifacts._remove_target_path", real_remove)
        monkeypatch.setattr("ouroboros.hermes.artifacts.os.replace", interrupt_after_newest_backup)
        with pytest.raises(KeyboardInterrupt):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")
        assert live_backup_seen

        monkeypatch.setattr("ouroboros.hermes.artifacts.os.replace", real_replace)
        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert operator_note.read_text(encoding="utf-8") == "newest operator state"
        assert not tuple(target_dir.parent.glob(".ouroboros.old.*"))

    def test_backup_cleanup_failure_restores_previous_live_generation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source = tmp_path / "source-skills"
        self._write_skill(source, "run", body="fresh\n")
        monkeypatch.setattr("ouroboros.hermes.artifacts._repo_root_skills_dir", lambda: source)
        target = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        target.joinpath("run").mkdir(parents=True)
        target.joinpath("run", "SKILL.md").write_text("previous\n", encoding="utf-8")
        real_remove = _remove_target_path

        def fail_backup_cleanup(path: Path) -> None:
            if path.name.startswith(".ouroboros.old."):
                raise OSError("synthetic backup cleanup failure")
            real_remove(path)

        monkeypatch.setattr("ouroboros.hermes.artifacts._remove_target_path", fail_backup_cleanup)
        with pytest.raises(OSError, match="backup cleanup failure"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert target.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == "previous\n"
        assert not target.joinpath(_SWAP_MARKER).exists()

    @pytest.mark.parametrize("target_exists", (False, True))
    def test_foreign_fixed_backup_sibling_is_never_recovered_or_deleted(
        self, tmp_path: Path, monkeypatch, target_exists: bool
    ) -> None:
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="new skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        target_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / "ouroboros"
        if target_exists:
            target_dir.mkdir(parents=True)
        foreign_backup = target_dir.with_name(".ouroboros.old")
        foreign_backup.mkdir(parents=True)
        foreign_note = foreign_backup / "operator-note.txt"
        foreign_note.write_text("foreign content", encoding="utf-8")

        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        assert foreign_note.read_text(encoding="utf-8") == "foreign content"
        assert target_dir.joinpath("run", "SKILL.md").is_file()

    def test_replaces_symlinked_capability_guide_without_following_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Hermes install should replace a guide symlink without writing through it."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        target_dir = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        outside_file = tmp_path / "outside-guide.md"
        outside_file.write_text("outside content", encoding="utf-8")
        target_dir.mkdir(parents=True)
        target_dir.joinpath(HERMES_SKILL_CAPABILITY_GUIDE_FILENAME).symlink_to(outside_file)

        install_hermes_skills(hermes_dir=tmp_path / ".hermes")

        guide_path = target_dir / HERMES_SKILL_CAPABILITY_GUIDE_FILENAME
        assert not guide_path.is_symlink()
        assert guide_path.read_text(encoding="utf-8").startswith(
            "## Ouroboros Skill Capability Guide: Hermes\n"
        )
        assert outside_file.read_text(encoding="utf-8") == "outside content"

    def test_refuses_symlinked_hermes_skill_root(self, tmp_path: Path, monkeypatch) -> None:
        """Hermes install must not write or prune through a symlinked managed root."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        target_dir = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        outside_dir = tmp_path / "outside-hermes"
        outside_dir.mkdir()
        target_dir.parent.mkdir(parents=True)
        target_dir.symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(OSError, match="symlinked directory"):
            install_hermes_skills(hermes_dir=tmp_path / ".hermes", prune=True)

        assert not outside_dir.joinpath(HERMES_SKILL_CAPABILITY_GUIDE_FILENAME).exists()
        assert not outside_dir.joinpath("run").exists()

    def test_refuses_symlinked_hermes_dir_ancestor(self, tmp_path: Path, monkeypatch) -> None:
        """Hermes install must not write or prune through a symlinked Hermes root."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )

        hermes_dir = tmp_path / ".hermes"
        outside_hermes_dir = tmp_path / "outside-hermes"
        outside_skill_root = outside_hermes_dir / "skills" / "autonomous-ai-agents" / "ouroboros"
        outside_skill_root.mkdir(parents=True)
        stale_skill = outside_skill_root / "status"
        stale_skill.mkdir()
        stale_skill.joinpath("SKILL.md").write_text("outside stale", encoding="utf-8")
        hermes_dir.symlink_to(outside_hermes_dir, target_is_directory=True)

        with pytest.raises(OSError, match="symlinked"):
            install_hermes_skills(hermes_dir=hermes_dir, prune=True)

        assert stale_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "outside stale"
        assert not outside_skill_root.joinpath("run").exists()

    def test_refuses_relative_hermes_root_from_symlinked_cwd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Relative Hermes installs must not resolve through a symlinked cwd."""
        source_skills_dir = tmp_path / "source-skills"
        self._write_skill(source_skills_dir, "run", body="fresh skill\n")
        monkeypatch.setattr(
            "ouroboros.hermes.artifacts._repo_root_skills_dir",
            lambda: source_skills_dir,
        )
        real_workspace = tmp_path / "real-workspace"
        symlink_workspace = tmp_path / "linked-workspace"
        real_workspace.mkdir()
        symlink_workspace.symlink_to(real_workspace, target_is_directory=True)
        monkeypatch.chdir(symlink_workspace)
        monkeypatch.setenv("PWD", str(symlink_workspace))

        with pytest.raises(OSError, match="symlinked"):
            install_hermes_skills(hermes_dir=".hermes")

        assert not real_workspace.joinpath(
            ".hermes", "skills", "autonomous-ai-agents", "ouroboros", "run"
        ).exists()
