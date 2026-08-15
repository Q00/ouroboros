"""Keep the EN/KO TUI guides aligned with both runtime backends.

Source-derived contracts (key bindings, screen mappings) are asserted against
the actual source files. Docs assertions are limited to structural anchors —
contract markers, key names, and screen names inside the marked sections — so
rewording guide prose never breaks them.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EN_GUIDE = REPO_ROOT / "docs/guides/tui-usage.md"
KO_GUIDE = REPO_ROOT / "docs/guides/tui-usage.ko.md"
GUIDES = (EN_GUIDE, KO_GUIDE)
SLT_README = REPO_ROOT / "crates/ouroboros-tui/README.md"
SLT_SESSION_SELECTOR = REPO_ROOT / "crates/ouroboros-tui/src/views/session_selector.rs"

CONTRACT_MARKERS = (
    "<!-- tui-contract:textual-screens -->",
    "<!-- tui-contract:slt-screens -->",
    "<!-- tui-contract:textual-keys -->",
    "<!-- tui-contract:slt-lifecycle -->",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _binding_actions(path: str, class_name: str) -> dict[str, str]:
    """Extract literal ``Binding(key, action, ...)`` entries from one class."""
    module = ast.parse(_read(REPO_ROOT / path))
    target_class = next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    assignment = next(
        node
        for node in target_class.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "BINDINGS"
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "BINDINGS"
            )
        )
    )
    value = assignment.value
    assert isinstance(value, ast.List)

    bindings: dict[str, str] = {}
    for item in value.elts:
        if not isinstance(item, ast.Call) or len(item.args) < 2:
            continue
        key, action = item.args[:2]
        if isinstance(key, ast.Constant) and isinstance(action, ast.Constant):
            assert isinstance(key.value, str)
            assert isinstance(action.value, str)
            bindings[key.value] = action.value
    return bindings


def _contract_section(text: str, marker: str) -> str:
    start = text.index(marker)
    later_markers = [
        text.find(candidate, start + len(marker))
        for candidate in CONTRACT_MARKERS
        if text.find(candidate, start + len(marker)) >= 0
    ]
    next_h2 = text.find("\n## ", start + len(marker))
    ends = later_markers + ([next_h2] if next_h2 >= 0 else [])
    return text[start : min(ends) if ends else len(text)]


def _heading_levels(text: str) -> list[int]:
    return [len(match.group(1)) for match in re.finditer(r"^(#{1,6}) ", text, re.MULTILINE)]


def test_textual_binding_contract_matches_documented_screen_overrides() -> None:
    assert _binding_actions("src/ouroboros/tui/app.py", "OuroborosTUI") == {
        "q": "quit",
        "p": "pause",
        "r": "resume",
        "d": "show_debug",
        "l": "show_logs",
        "s": "show_selector",
        "e": "show_lineages",
        "1": "show_dashboard",
        "2": "show_execution",
        "3": "show_logs",
        "4": "show_debug",
    }
    assert (
        _binding_actions("src/ouroboros/tui/screens/dashboard_v3.py", "DashboardScreenV3")["r"]
        == "resume"
    )
    assert (
        _binding_actions("src/ouroboros/tui/screens/execution.py", "ExecutionScreen")["r"]
        == "refresh"
    )
    assert _binding_actions("src/ouroboros/tui/screens/debug.py", "DebugScreen")["r"] == ("refresh")
    assert (
        _binding_actions("src/ouroboros/tui/screens/lineage_selector.py", "LineageSelectorScreen")[
            "r"
        ]
        == "refresh"
    )
    assert (
        _binding_actions("src/ouroboros/tui/screens/lineage_detail.py", "LineageDetailScreen")["r"]
        == "rewind"
    )

    en_keys = _contract_section(_read(EN_GUIDE), "<!-- tui-contract:textual-keys -->")
    ko_keys = _contract_section(_read(KO_GUIDE), "<!-- tui-contract:textual-keys -->")
    for screen in ("Execution", "Debug", "Logs", "Lineage selector", "Lineage detail"):
        assert screen in en_keys
    for screen in (
        "실행 화면",
        "디버그 화면",
        "로그 화면",
        "계보 선택 화면",
        "계보 상세 화면",
    ):
        assert screen in ko_keys
    assert "rewind" in en_keys and "rewind" in ko_keys


def test_slt_screen_and_mock_fallback_contract_matches_source() -> None:
    source = _read(REPO_ROOT / "crates/ouroboros-tui/src/main.rs")
    mapping_match = re.search(
        r"state\.screen = match state\.tabs\.selected \{(?P<body>.*?)\n\s*\};",
        source,
        re.DOTALL,
    )
    assert mapping_match is not None
    assert dict(re.findall(r"(\d) => Screen::(\w+)", mapping_match.group("body"))) == {
        "0": "Dashboard",
        "1": "Execution",
        "2": "Lineage",
        "3": "SessionSelector",
    }
    assert source.count("mock::init_mock_state(&mut state);") == 3, (
        "explicit --mock, empty DB, and DB-open failure must remain the three "
        "documented demo entry paths"
    )
    assert "if event_count == 0" in source
    assert "Err(e) =>" in source
    assert "state.disable_lifecycle_controls();" in source
    session_selector_source = _read(SLT_SESSION_SELECTOR)
    assert re.search(
        r"if ui\.key_code\(KeyCode::Esc\) \{\s*state\.tabs\.selected = 0;\s*\}",
        session_selector_source,
    )
    assert re.search(
        r"Screen::SessionSelector => \{.*?\(\"Esc\", \"Back\"\)",
        source,
        re.DOTALL,
    )

    for guide in GUIDES:
        text = _read(guide)
        screens = _contract_section(text, "<!-- tui-contract:slt-screens -->")
        lifecycle = _contract_section(text, "<!-- tui-contract:slt-lifecycle -->")
        for key in ("`1`", "`2`", "`3`", "`4`", "`e`", "`s`", "`l`", "`Esc`"):
            assert key in screens
        assert "--mock" in lifecycle
        assert "`p`" in lifecycle and "`r`" in lifecycle

    slt_readme = _read(SLT_README)
    for key in ("`1`", "`2`", "`3`", "`4`", "`e`", "`s`", "`l`", "`Esc`"):
        assert key in slt_readme


def test_localized_guides_keep_the_same_contract_structure() -> None:
    en = _read(EN_GUIDE)
    ko = _read(KO_GUIDE)
    for marker in CONTRACT_MARKERS:
        assert en.count(marker) == 1
        assert ko.count(marker) == 1
    assert [en.index(marker) for marker in CONTRACT_MARKERS] == sorted(
        en.index(marker) for marker in CONTRACT_MARKERS
    )
    assert [ko.index(marker) for marker in CONTRACT_MARKERS] == sorted(
        ko.index(marker) for marker in CONTRACT_MARKERS
    )
    assert _heading_levels(en) == _heading_levels(ko)
    assert en.count("```") == ko.count("```")


@pytest.mark.parametrize("guide", GUIDES)
def test_tui_guide_relative_links_resolve(guide: Path) -> None:
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(guide)):
        if target.startswith(("http://", "https://", "#")):
            continue
        path = (guide.parent / target.split("#", 1)[0]).resolve()
        assert path.exists(), f"broken link in {guide.relative_to(REPO_ROOT)}: {target}"
