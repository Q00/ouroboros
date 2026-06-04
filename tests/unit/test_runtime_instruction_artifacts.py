"""Tests for setup-owned runtime instruction artifacts."""

from pathlib import Path

from ouroboros.runtime_instruction_artifacts import (
    COPILOT_AGENTS_FILENAME,
    COPILOT_INSTRUCTIONS_DIRNAME,
    GUIDE_FILENAME,
    install_copilot_instruction_artifact,
    install_gemini_instruction_artifact,
    install_kiro_instruction_artifact,
    install_opencode_instruction_artifact,
)


def test_opencode_installs_global_agents_section(tmp_path: Path) -> None:
    artifact = install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")

    assert artifact.backend == "opencode"
    assert artifact.path == tmp_path / "opencode" / "AGENTS.md"
    content = artifact.path.read_text(encoding="utf-8")
    assert "## Ouroboros Skill Capability Guide: Opencode" in content
    assert "### When a skill requires `run_lateral_review`" in content


def test_gemini_installs_global_gemini_memory_section(tmp_path: Path) -> None:
    artifact = install_gemini_instruction_artifact(home=tmp_path)

    assert artifact.path == tmp_path / ".gemini" / "GEMINI.md"
    content = artifact.path.read_text(encoding="utf-8")
    assert "## Ouroboros Skill Capability Guide: Gemini" in content
    assert "lateral_review_required=true" in content


def test_kiro_installs_global_steering_file(tmp_path: Path) -> None:
    artifact = install_kiro_instruction_artifact(home=tmp_path)

    assert artifact.path == tmp_path / ".kiro" / "steering" / GUIDE_FILENAME
    content = artifact.path.read_text(encoding="utf-8")
    assert "## Ouroboros Skill Capability Guide: Kiro" in content
    assert "### When a skill requires `run_lateral_review`" in content


def test_copilot_installs_custom_agents_file(tmp_path: Path) -> None:
    artifact = install_copilot_instruction_artifact(home=tmp_path)

    assert artifact.path == (
        tmp_path / ".copilot" / COPILOT_INSTRUCTIONS_DIRNAME / COPILOT_AGENTS_FILENAME
    )
    content = artifact.path.read_text(encoding="utf-8")
    assert "## Ouroboros Skill Capability Guide: Copilot" in content
    assert "### When a skill requires `run_lateral_review`" in content


def test_marked_section_refresh_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "opencode" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_text("# User instructions\n\nKeep this line.\n", encoding="utf-8")

    first = install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")
    second = install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")

    assert first.path == second.path
    content = path.read_text(encoding="utf-8")
    assert content.count("<!-- ouroboros:skill-capability-guide:start -->") == 1
    assert content.startswith("# User instructions")
    assert "Keep this line." in content


def test_marked_section_refresh_collapses_duplicate_managed_sections(tmp_path: Path) -> None:
    path = tmp_path / "opencode" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    duplicate_section = (
        "<!-- ouroboros:skill-capability-guide:start -->\n"
        "stale guide\n"
        "<!-- ouroboros:skill-capability-guide:end -->\n"
    )
    path.write_text(
        f"# User instructions\n\n{duplicate_section}\n{duplicate_section}\nKeep this line.\n",
        encoding="utf-8",
    )

    install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")

    content = path.read_text(encoding="utf-8")
    assert content.count("<!-- ouroboros:skill-capability-guide:start -->") == 1
    assert content.count("<!-- ouroboros:skill-capability-guide:end -->") == 1
    assert "stale guide" not in content
    assert content.startswith("# User instructions")
    assert "Keep this line." in content
