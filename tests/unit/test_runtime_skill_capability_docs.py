"""Tests for runtime skill capability guide coverage docs."""

from pathlib import Path

from ouroboros.backends.capabilities import runtime_backend_choices


def test_runtime_skill_capability_guide_docs_cover_all_runtime_backends() -> None:
    docs = Path("docs/runtime-guides/skill-capability-guides.md").read_text(encoding="utf-8")

    coverage_section = docs.split("## Current coverage", 1)[1].split("## Seed generation", 1)[0]
    documented_runtime_names = {
        row.split("|")[1].strip().lower()
        for row in coverage_section.splitlines()
        if row.startswith("|") and not row.startswith("| ---") and "Generated artifact surface" not in row
    }
    assert set(runtime_backend_choices()) <= documented_runtime_names

    assert "Global `AGENTS.md`" in docs
    assert "`~/.gemini/GEMINI.md`" in docs
    assert "`~/.kiro/steering/ouroboros-skill-capability-guide.md`" in docs
    assert "`~/.copilot/ouroboros-instructions/AGENTS.md`" in docs
    assert "| Goose | No setup-owned capability artifact yet |" in docs
    assert "| Pi | No setup-owned capability artifact yet |" in docs
    assert "render_backend_skill_capability_guide(<backend>)" in docs
    assert "## Capability graph contract" in docs
    assert "## Contributor checklist for capability changes" in docs
    assert "`src/ouroboros/backends/capabilities.py`" in docs
    assert "SkillExecutionCapability" in docs
    compact = " ".join(docs.split())
    assert "must not copy long adapter sections into individual `SKILL.md` files" in compact


def test_cli_reference_setup_runtime_list_includes_supported_runtime_backends() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")

    assert "`claude`, `codex`, `opencode`, `hermes`, `gemini`, `kiro`, `copilot`, `goose`" in docs
    assert "Claude Code, Codex CLI, OpenCode, Hermes, Gemini, Kiro, Copilot, and Goose" in docs
    assert "`kiro-cli`, `copilot`, and `goose` CLI binaries" in docs
