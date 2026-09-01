"""Tests for runtime skill capability guide coverage docs.

These assert structural anchors only (coverage table rows, section headings,
code identifiers) so that rewording the surrounding prose never breaks them.
"""

from pathlib import Path
import re

import pytest

from ouroboros.backends.capabilities import (
    get_backend_capability,
    runtime_backend_choices,
)
from ouroboros.config.loader import get_agent_runtime_backend
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend


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


def test_architecture_runtime_inventory_matches_backend_registry() -> None:
    docs = Path("docs/architecture.md").read_text(encoding="utf-8")
    section = docs.split("### Shipped adapters", 1)[1].split("### Runtime factory", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]

    documented: dict[str, set[str]] = {}
    for row in rows:
        columns = [column.strip() for column in row.strip("|").split("|")]
        backend = columns[0].strip("`")
        documented[backend] = set(re.findall(r"`([^`]+)`", columns[2]))

    assert set(documented) == set(runtime_backend_choices())
    for backend, aliases in documented.items():
        capability = get_backend_capability(backend)
        assert capability is not None
        assert aliases == set(capability.aliases)


def test_architecture_runtime_factory_precedence_matches_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = Path("docs/architecture.md").read_text(encoding="utf-8")
    section = docs.split("### Runtime factory", 1)[1].split("The public CLI additionally", 1)[0]
    expected_order = (
        "1. Explicit `backend=` parameter",
        "2. `OUROBOROS_AGENT_RUNTIME` environment variable",
        "3. Legacy `OUROBOROS_RUNTIME` environment variable",
        "4. `orchestrator.runtime_backend` in `~/.ouroboros/config.yaml`",
        "5. Default `claude` runtime",
    )

    cursor = 0
    for item in expected_order:
        position = section.find(item, cursor)
        assert position >= 0, f"missing or out-of-order runtime precedence item: {item}"
        cursor = position + len(item)

    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "goose")
    assert resolve_agent_runtime_backend("grok") == "grok"
    assert get_agent_runtime_backend() == "codex"

    monkeypatch.delenv("OUROBOROS_AGENT_RUNTIME")
    assert get_agent_runtime_backend() == "goose"


def test_readme_runtime_summary_defers_to_canonical_registry() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    summary = next(
        line for line in readme.splitlines() if line.startswith("- **Runtime backends**")
    )

    # The public summary deliberately names the registry as the exhaustive
    # source instead of maintaining a second list that can drift.
    assert "every canonical backend returned by `runtime_backend_choices()`" in summary
    highlighted_backends = {"gjc", "antigravity", "grok", "zcode"}
    assert highlighted_backends <= set(runtime_backend_choices())
    assert all(backend.casefold() in summary.casefold() for backend in highlighted_backends)


def test_repository_inventory_docs_derive_counts_from_the_checkout() -> None:
    readme_ko = Path("README.ko.md").read_text(encoding="utf-8")
    backlog = Path("backlog.md").read_text(encoding="utf-8")

    inventory_summary = next(
        line
        for line in readme_ko.splitlines()
        if line.startswith("<summary><strong>") and "Python 3.12+" in line
    )
    assert not re.search(r"\d+개 (?:tracked Python )?모듈|\d+개 테스트 파일", inventory_summary)
    assert "git ls-files ':(glob)src/ouroboros/**/*.py'" in readme_ko
    assert "git ls-files ':(glob)tests/**/test_*.py'" in readme_ko
    assert "live tracked-module counts are intentionally omitted" in backlog
    assert not re.search(r"\d+ tracked Python modules", backlog)


def test_cli_reference_documents_recovery_and_inspection_commands() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    overview = docs.split("## Commands Overview", 1)[1].split("\n---\n", 1)[0]

    assert "| `init` / `interview` |" in overview
    assert "| `resume` |" in overview
    assert "write validated Stage 1 commands" in overview
    assert "Inspect and compare exported auto-interview traces" in overview
    assert "## `ouroboros detect`" in docs
    assert "## `ouroboros harness`" in docs
    assert "## `ouroboros resume`" in docs

    compact_docs = " ".join(docs.split())
    assert "does not detect installed runtime backends" in compact_docs
    assert "does not manage runtime harness configuration" in compact_docs
    assert "does not resume a session by itself" in compact_docs
