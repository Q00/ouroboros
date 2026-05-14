"""Tests for runtime skill capability guide coverage docs."""

from pathlib import Path


def test_runtime_skill_capability_guide_docs_cover_all_runtime_backends() -> None:
    docs = Path("docs/runtime-guides/skill-capability-guides.md").read_text(encoding="utf-8")

    for backend in ("Codex", "Hermes", "Claude", "OpenCode", "Gemini", "Kiro", "Copilot"):
        assert f"| {backend} |" in docs

    assert "render_backend_skill_capability_guide(<backend>)" in docs
    compact = " ".join(docs.split())
    assert "must not copy long adapter sections into individual `SKILL.md` files" in compact
