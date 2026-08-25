"""Tests for setup-owned runtime instruction artifacts."""

from pathlib import Path

import pytest

from ouroboros.gjc import gjc_agent_dir, gjc_instruction_path
from ouroboros.runtime_instruction_artifacts import (
    COPILOT_AGENTS_FILENAME,
    COPILOT_INSTRUCTIONS_DIRNAME,
    GUIDE_FILENAME,
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


def test_gjc_agent_dir_resolves_config_dir_name_under_home(tmp_path: Path) -> None:
    # gjc treats GJC_CONFIG_DIR as a directory *name* under home, not a path.
    assert gjc_agent_dir(home=tmp_path, environ={"GJC_CONFIG_DIR": "custom-gjc"}) == (
        tmp_path / "custom-gjc" / "agent"
    )


def test_gjc_agent_dir_rejects_config_dir_escape(tmp_path: Path) -> None:
    assert gjc_agent_dir(home=tmp_path, environ={"GJC_CONFIG_DIR": "../outside"}) == (
        tmp_path / ".gjc" / "agent"
    )


def test_gjc_agent_dir_rejects_backslash_config_dir_escape(tmp_path: Path) -> None:
    assert gjc_agent_dir(home=tmp_path, environ={"GJC_CONFIG_DIR": "..\\outside"}) == (
        tmp_path / ".gjc" / "agent"
    )


def test_gjc_agent_dir_falls_back_to_pi_config_dir_name(tmp_path: Path) -> None:
    assert gjc_agent_dir(home=tmp_path, environ={"PI_CONFIG_DIR": "custom-pi"}) == (
        tmp_path / "custom-pi" / "agent"
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


def test_gjc_installs_always_apply_command_routes_and_capability_guide(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}

    first = install_gjc_instruction_artifact(environ=env)
    second = install_gjc_instruction_artifact(environ=env)

    assert first.backend == "gjc"
    assert first.path == second.path == gjc_instruction_path(environ=env)
    assert first.path == tmp_path / "agent" / "rules" / GUIDE_FILENAME
    content = first.path.read_text(encoding="utf-8")
    assert content.startswith("---\nalwaysApply: true\n")
    assert (
        "`ooo interview [arguments]` → invoke `/skill:ouroboros-interview [arguments]`" in content
    )
    assert "Bare `ooo` → invoke `/skill:ouroboros-ooo`" in content
    assert "route to GJC's bundled `deep-interview`" in content
    assert "## Ouroboros Skill Capability Guide: Gjc" in content
    assert first.path.read_text(encoding="utf-8") == second.path.read_text(encoding="utf-8")


def test_gjc_preserves_operator_owned_routing_guide(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}
    path = gjc_instruction_path(environ=env)
    path.parent.mkdir(parents=True)
    path.write_text("operator routing rules\n", encoding="utf-8")

    with pytest.raises(OSError, match="preserved user-managed GJC instruction guide"):
        install_gjc_instruction_artifact(environ=env)

    assert path.read_text(encoding="utf-8") == "operator routing rules\n"


def test_gjc_guide_publication_never_writes_through_symlink(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}
    external = tmp_path / "operator-rules.md"
    external.write_text("operator routing rules\n", encoding="utf-8")
    path = gjc_instruction_path(environ=env)
    path.parent.mkdir(parents=True)
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(OSError, match="preserved user-managed GJC instruction guide"):
        install_gjc_instruction_artifact(environ=env)

    assert external.read_text(encoding="utf-8") == "operator routing rules\n"
    assert path.is_symlink()


def test_gjc_rejects_modified_setup_owned_routing_guide(tmp_path: Path) -> None:
    env = {"GJC_CODING_AGENT_DIR": str(tmp_path / "agent")}
    path = install_gjc_instruction_artifact(environ=env).path
    path.write_text(
        path.read_text(encoding="utf-8").replace("Bare `ooo`", "Modified `ooo`", 1),
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="preserved user-managed GJC instruction guide"):
        install_gjc_instruction_artifact(environ=env)


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
