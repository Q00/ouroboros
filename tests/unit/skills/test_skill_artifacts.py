"""Unit tests for shared packaged skill resolution helpers."""

from __future__ import annotations

from pathlib import Path

from ouroboros.skills.artifacts import resolve_packaged_skills_dir


def test_resolve_packaged_skills_dir_falls_back_to_repo_root_bundle_when_package_is_stub(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Editable installs should skip package stubs that do not contain skill bundles."""
    package_stub_dir = tmp_path / "repo" / "src" / "ouroboros" / "skills"
    package_stub_dir.mkdir(parents=True)
    package_stub_dir.joinpath("__init__.py").write_text("# package stub\n", encoding="utf-8")

    repo_skills_dir = tmp_path / "repo" / "skills"
    run_skill_dir = repo_skills_dir / "run"
    run_skill_dir.mkdir(parents=True)
    run_skill_dir.joinpath("SKILL.md").write_text("---\nname: run\n---\n", encoding="utf-8")

    anchor_file = tmp_path / "repo" / "src" / "ouroboros" / "codex" / "artifacts.py"
    anchor_file.parent.mkdir(parents=True)
    anchor_file.write_text("# anchor\n", encoding="utf-8")

    monkeypatch.setattr(
        "ouroboros.skills.artifacts.importlib.resources.files",
        lambda _package: package_stub_dir,
    )

    with resolve_packaged_skills_dir(anchor_file=anchor_file) as resolved_dir:
        assert resolved_dir == repo_skills_dir


def test_multitool_deferred_schema_guards_name_each_discovery_query() -> None:
    """Multi-tool skill guards must not reuse the wrong deferred schema query."""
    repo_root = Path(__file__).resolve().parents[3]
    expected = {
        "brownfield": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "setup": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "seed": [
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros qa"', "ouroboros_qa"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "interview": [
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "evaluate": [
            ('"+ouroboros evaluate"', "ouroboros_evaluate"),
        ],
        "evolve": [
            ('"+ouroboros evolve"', "ouroboros_evolve_step"),
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "pm": [
            ('"+ouroboros pm_interview"', "ouroboros_pm_interview"),
        ],
        "run": [
            ('"+ouroboros execute"', "ouroboros_start_execute_seed"),
            ('"+ouroboros execute"', "ouroboros_job_wait"),
            ('"+ouroboros execute"', "ouroboros_job_result"),
        ],
    }

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        assert "the same tool-discovery load query you used above" not in "\n".join(
            skill_path.read_text(encoding="utf-8") for skill_path in root.glob("*/SKILL.md")
        )
        for skill, pairs in expected.items():
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            assert "deferred-schema guard" in text
            for query, tool in pairs:
                assert query in text
                assert tool in text
