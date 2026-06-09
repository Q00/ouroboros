"""Tests for setup-owned runtime instruction artifacts."""

from pathlib import Path

from ouroboros.backends.capabilities import render_backend_skill_capability_guide
from ouroboros.runtime_instruction_artifacts import (
    COPILOT_AGENTS_FILENAME,
    COPILOT_INSTRUCTIONS_DIRNAME,
    GUIDE_FILENAME,
    gjc_agent_dir,
    gjc_instruction_path,
    install_copilot_instruction_artifact,
    install_gemini_instruction_artifact,
    install_gjc_instruction_artifact,
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


def test_gjc_agent_dir_defaults_to_home_gjc_agent(tmp_path: Path) -> None:
    assert gjc_agent_dir(home=tmp_path, environ={}) == tmp_path / ".gjc" / "agent"


def test_gjc_agent_dir_respects_gjc_config_dir(tmp_path: Path) -> None:
    assert gjc_agent_dir(environ={"GJC_CONFIG_DIR": str(tmp_path / "custom-gjc")}) == (
        tmp_path / "custom-gjc" / "agent"
    )


def test_gjc_agent_dir_respects_explicit_agent_dir(tmp_path: Path) -> None:
    assert (
        gjc_agent_dir(
            home=tmp_path,
            environ={
                "GJC_CODING_AGENT_DIR": str(tmp_path / "agent-dir"),
                "GJC_CONFIG_DIR": str(tmp_path / "ignored-root"),
            },
        )
        == tmp_path / "agent-dir"
    )


def test_gjc_installs_rules_guide_exact_renderer_output_and_idempotent(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}

    first = install_gjc_instruction_artifact(environ=env)
    second = install_gjc_instruction_artifact(environ=env)

    assert first.backend == "gjc"
    assert first.path == second.path == gjc_instruction_path(environ=env)
    assert first.path == tmp_path / "agent" / "rules" / GUIDE_FILENAME
    assert first.path.read_text(encoding="utf-8") == render_backend_skill_capability_guide("gjc")


def test_gjc_agent_dir_precedence(tmp_path: Path) -> None:
    env = {
        "GJC_CODING_AGENT_DIR": str(tmp_path / "explicit-agent"),
        "GJC_CONFIG_DIR": str(tmp_path / "config-root"),
    }
    assert gjc_agent_dir(home=tmp_path, environ=env) == tmp_path / "explicit-agent"
    assert (
        gjc_agent_dir(home=tmp_path, environ={"GJC_CONFIG_DIR": str(tmp_path / "config-root")})
        == tmp_path / "config-root" / "agent"
    )
    assert gjc_agent_dir(home=tmp_path, environ={}) == tmp_path / ".gjc" / "agent"


def test_gjc_artifact_repairs_content_drift_and_matches_renderer(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}
    artifact = install_gjc_instruction_artifact(environ=env)
    artifact.path.write_text("corrupt", encoding="utf-8")

    repaired = install_gjc_instruction_artifact(environ=env)

    assert repaired.path == artifact.path
    assert repaired.path.read_text(encoding="utf-8") == render_backend_skill_capability_guide("gjc")


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
        f"# User instructions\n\n{duplicate_section}\nUSER CUSTOM LINE BETWEEN DUPLICATES\n\n{duplicate_section}\nKeep this line.\n",
        encoding="utf-8",
    )

    install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")

    content = path.read_text(encoding="utf-8")
    assert content.count("<!-- ouroboros:skill-capability-guide:start -->") == 1
    assert content.count("<!-- ouroboros:skill-capability-guide:end -->") == 1
    assert "stale guide" not in content
    assert "USER CUSTOM LINE BETWEEN DUPLICATES" in content
    assert content.startswith("# User instructions")
    assert "Keep this line." in content


def test_marked_section_refresh_preserves_text_after_stray_start_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opencode" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    valid_section = (
        "<!-- ouroboros:skill-capability-guide:start -->\n"
        "stale guide\n"
        "<!-- ouroboros:skill-capability-guide:end -->\n"
    )
    path.write_text(
        "# User instructions\n\n"
        "<!-- ouroboros:skill-capability-guide:start -->\n"
        "USER CUSTOM LINE THAT MUST SURVIVE\n\n"
        f"{valid_section}"
        "Keep this line.\n",
        encoding="utf-8",
    )

    install_opencode_instruction_artifact(config_dir=tmp_path / "opencode")

    content = path.read_text(encoding="utf-8")
    assert "USER CUSTOM LINE THAT MUST SURVIVE" in content
    assert "stale guide" not in content
    assert "Keep this line." in content
    assert "## Ouroboros Skill Capability Guide: Opencode" in content
