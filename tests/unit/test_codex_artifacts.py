"""Unit tests for packaged Codex artifact installation."""

from pathlib import Path

import pytest

from ouroboros.codex.artifacts import (
    CODEX_RULE_FILENAME,
    CODEX_SKILL_NAMESPACE,
    CodexManagedArtifact,
    CodexPackagedAssets,
    install_codex_rules,
    install_codex_skills,
    load_packaged_codex_rules,
    resolve_packaged_codex_assets,
)


class TestInstallCodexRules:
    """Test installation of the packaged Codex rules asset."""

    @staticmethod
    def _write_rule(rules_dir: Path, rule_name: str, content: str) -> Path:
        rule_path = rules_dir / rule_name
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(content, encoding="utf-8")
        return rule_path

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
        assert installed_path.read_text(encoding="utf-8") == "# fresh rules\n"
        assert secondary_target_path.read_text(encoding="utf-8") == "# status rules\n"
        assert not rules_dir.joinpath("team.md").exists()

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
        assert installed_path.read_text(encoding="utf-8") == "# fresh rules\n"
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
        assert installed_path.read_text(encoding="utf-8") == "# upgraded rules\n"
        assert rules_dir.joinpath("ouroboros-status.md").read_text(encoding="utf-8") == (
            "# upgraded status\n"
        )
        assert not stale_namespaced_rule.exists()
        assert unrelated_rule.read_text(encoding="utf-8") == "keep me"


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
        assert installed_rule.read_text(encoding="utf-8") == "# codex rules v1\n"
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
        assert (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run" / "notes.txt"
        ).read_text(encoding="utf-8") == "seed path support"
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
        assert installed_rule.read_text(encoding="utf-8") == "# codex rules v2\n"
        assert installed_skills == (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}interview",
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run",
        )
        assert (
            codex_dir / "skills" / f"{CODEX_SKILL_NAMESPACE}run" / "notes.txt"
        ).read_text(encoding="utf-8") == "new run notes"
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
