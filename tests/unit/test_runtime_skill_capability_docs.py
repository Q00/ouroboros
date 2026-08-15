"""Tests for runtime skill capability guide coverage docs.

These assert structural anchors only (coverage table rows, section headings,
code identifiers) so that rewording the surrounding prose never breaks them.
"""

from pathlib import Path
import re

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
    assert documented_runtime_names == set(runtime_backend_choices())

    assert "render_backend_skill_capability_guide(<backend>)" in docs
    assert "## Capability graph contract" in docs
    assert "## Contributor checklist for capability changes" in docs
    assert "`src/ouroboros/backends/capabilities.py`" in docs
    assert "SkillExecutionCapability" in docs


def test_cli_reference_setup_runtime_list_includes_supported_runtime_backends() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")

    # Scoped to the `-r, --runtime` option row itself (not the whole file) so
    # a mutation that drops the shipped-runtime list from that row can't hide
    # behind mentions of the same names elsewhere in the doc.
    option_row = next(
        line for line in docs.splitlines() if line.startswith("| `-r, --runtime TEXT`")
    )
    shipped_values = option_row.split("Shipped values:", 1)[1].split("Auto-detected", 1)[0]
    documented_backends = set(re.findall(r"`([\w-]+)`", shipped_values))

    # MCP worker variants (codex_mcp, claude_mcp) are internal leader-driven
    # runtimes, not user-facing `ouroboros setup --runtime` choices.
    user_facing_backends = {b for b in runtime_backend_choices() if not b.endswith("_mcp")}
    user_facing_backends |= {"claude-sdk", "claude-cli"}
    assert documented_backends == user_facing_backends

    assert "ouroboros setup --runtime" in docs
    assert Path("docs/runtime-guides/zcode.md").is_file()
