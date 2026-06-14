"""Tests for the bundled skill breadcrumb footer contract."""

from __future__ import annotations

from pathlib import Path


def test_all_bundled_skills_define_breadcrumb_footer_contract() -> None:
    skills_dir = Path("skills")
    skill_paths = sorted(skills_dir.glob("*/SKILL.md"))

    assert skill_paths
    for skill_path in skill_paths:
        text = skill_path.read_text(encoding="utf-8")
        assert "## RFC #1392 State Breadcrumb Footer" in text, skill_path
        assert "◆ <current state> → next: <recommended action>" in text, skill_path
        assert "session state via `ouroboros_session_status`" in text, skill_path
        assert "Never use a linear `Step N of M` footer" in text, skill_path


def test_bundled_skills_do_not_use_legacy_pin_footer() -> None:
    for skill_path in sorted(Path("skills").glob("*/SKILL.md")):
        text = skill_path.read_text(encoding="utf-8")
        assert "📍" not in text, skill_path
