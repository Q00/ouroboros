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
            ('"+ouroboros execute"', "ouroboros_ac_tree_hud"),
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


def test_packaged_skills_gate_fallback_on_callability_not_empty_discovery() -> None:
    """No packaged skill may route to fallback/Path B on empty discovery alone.

    ``render_mcp_server_instructions()`` declares that an empty discovery result
    for an already-exposed tool is a no-op (not unavailability). Skill bodies
    must therefore gate fallback on whether the MCP tool is *callable*, not on
    whether discovery returned a match — otherwise direct-exposure or
    already-loaded runtimes skip a callable tool. This guards that contract for
    every packaged skill in both trees.
    """
    repo_root = Path(__file__).resolve().parents[3]
    # Bare "empty discovery -> fallback" routing that predated the server contract.
    forbidden = (
        "If not → proceed to **Path B**",
        "If not → skip to **Fallback**",
        "returns no matching tools → proceed to **Path B**",
    )
    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill_path in root.glob("*/SKILL.md"):
            text = skill_path.read_text(encoding="utf-8")
            for phrase in forbidden:
                assert phrase not in text, (
                    f"{skill_path}: bare empty-discovery fallback `{phrase}` — "
                    "gate on tool callability instead"
                )
            # Every deferred-schema-guard "no matching tool" fallback must carry
            # a callability qualifier (an empty load for an exposed tool is fine).
            if "no matching tool" in text:
                assert "not already callable" in text, (
                    f"{skill_path}: `no matching tool` fallback not gated on callability"
                )


def test_background_skills_delegate_one_exclusive_job_observer() -> None:
    """Run/auto should free the main session when native child sessions exist."""
    repo_root = Path(__file__).resolve().parents[3]

    for skill in ("run", "auto", "ralph"):
        text = (repo_root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        normalized = text.lower()
        assert "response.meta.job_observer" in normalized
        assert "exactly one" in normalized
        assert "read-only" in normalized
        assert "main session must not poll the same job" in normalized
        assert "attention_required" in normalized
        assert "isolated worktree" in normalized
        assert "tui open" in normalized
        assert "conversation" in normalized and "available" in normalized
