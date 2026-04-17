"""Unit tests for Hermes skill artifact installation."""

from __future__ import annotations

from pathlib import Path

from ouroboros.hermes.artifacts import install_hermes_skills


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
