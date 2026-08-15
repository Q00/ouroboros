"""Tests for runtime skill capability guide coverage docs.

These assert structural anchors only (coverage table rows, section headings,
code identifiers) so that rewording the surrounding prose never breaks them.
"""

from pathlib import Path

from ouroboros.backends.capabilities import runtime_backend_choices


def test_runtime_skill_capability_guide_docs_cover_all_runtime_backends() -> None:
    docs = Path("docs/runtime-guides/skill-capability-guides.md").read_text(encoding="utf-8")

    coverage_section = docs.split("## Current coverage", 1)[1].split("## Seed generation", 1)[0]
    documented_runtime_names = {
        row.split("|")[1].strip().lower()
        for row in coverage_section.splitlines()
        if row.startswith("|")
        and not row.startswith("| ---")
        and "Generated artifact surface" not in row
    }
    assert set(runtime_backend_choices()) <= documented_runtime_names

    assert "render_backend_skill_capability_guide(<backend>)" in docs
    assert "## Capability graph contract" in docs
    assert "## Contributor checklist for capability changes" in docs
    assert "`src/ouroboros/backends/capabilities.py`" in docs
    assert "SkillExecutionCapability" in docs


def test_cli_reference_setup_runtime_list_includes_supported_runtime_backends() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")

    # MCP worker variants (codex_mcp, claude_mcp) are internal leader-driven
    # runtimes, not user-facing `ouroboros setup --runtime` choices.
    for backend in runtime_backend_choices():
        if backend.endswith("_mcp"):
            continue
        assert f"`{backend}`" in docs, f"cli-reference.md does not mention runtime `{backend}`"

    assert "ouroboros setup --runtime" in docs
    assert Path("docs/runtime-guides/zcode.md").is_file()
