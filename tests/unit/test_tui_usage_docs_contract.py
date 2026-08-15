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


def _slt_screens_table_rows(text: str) -> list[tuple[str, str, str]]:
    """Parse the marked SLT screens table into (key, alt_key, screen) rows."""
    section = _contract_section(text, "<!-- tui-contract:slt-screens -->")
    return re.findall(r"\| `(\d)` \|(?: `(\w)`)? \| \*\*(\w+)\*\* \|", section)


def _textual_screens_table_rows(text: str) -> list[tuple[str, str, str]]:
    """Parse the marked Textual table into (key, shortcut, screen) rows."""
    section = _contract_section(text, "<!-- tui-contract:textual-screens -->")
    rows: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[2].startswith("**"):
            continue
        key = cells[0].strip("`")
        shortcut = cells[1].strip("`")
        screen = cells[2].strip("*")
        rows.append((key, shortcut, screen))
    return rows


def _markdown_table(section: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and not set("".join(cells)) <= {"-", ":"}:
            rows.append((cells[0], cells[1]))
    return rows[1:]  # header


def _documented_textual_actions(path: Path) -> dict[str, str]:
    section = _contract_section(_read(path), "<!-- tui-contract:textual-keys -->")
    documented: dict[str, str] = {}
    contexts = {
        "dashboard": ("Dashboard", "대시보드"),
        "session_selector": ("Session Selector", "세션 선택 화면"),
        "execution": ("Execution", "실행 화면"),
        "debug": ("Debug", "디버그 화면"),
        "logs": ("Logs", "로그 화면"),
        "lineage_selector": ("Lineage selector", "계보 선택 화면"),
        "lineage_detail": ("Lineage detail", "계보 상세 화면"),
    }
    for key_cell, action_cell in _markdown_table(section):
        if "`r`" not in key_cell:
            continue
        for context, labels in contexts.items():
            if any(label.lower() in key_cell.lower() for label in labels):
                lowered = action_cell.lower()
                if "rewind" in lowered:
                    action = "rewind"
                elif "no active binding" in lowered or "활성 바인딩 없음" in action_cell:
                    action = "unbound"
                elif "refresh" in lowered or "새로고침" in action_cell:
                    action = "refresh"
                elif "resume" in lowered or "재개 요청" in action_cell:
                    action = "resume"
                else:
                    action = "unknown"
                documented[context] = action
    return documented


def test_textual_binding_contract_matches_documented_screen_overrides() -> None:
    app_actions = _binding_actions("src/ouroboros/tui/app.py", "OuroborosTUI")
    assert app_actions == {
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
    en_screen_rows = _textual_screens_table_rows(_read(EN_GUIDE))
    ko_screen_rows = _textual_screens_table_rows(_read(KO_GUIDE))
    screen_name_by_action = {
        "show_dashboard": "Dashboard",
        "show_execution": "Execution",
        "show_logs": "Logs",
        "show_debug": "Debug",
        "show_selector": "Session Selector",
        "show_lineages": "Lineage",
    }
    documented_screen_by_key = {
        binding: screen
        for key, shortcut, screen in en_screen_rows
        for binding in (key, shortcut)
        if binding
    }
    assert documented_screen_by_key == {
        key: screen_name_by_action[action]
        for key, action in app_actions.items()
        if action in screen_name_by_action
    }
    # Localized screen labels differ, but every key and shortcut must retain
    # its row position and source-derived mapping.
    assert [(key, shortcut) for key, shortcut, _screen in ko_screen_rows] == [
        (key, shortcut) for key, shortcut, _screen in en_screen_rows
    ]
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

    source_actions = {
        "dashboard": _binding_actions(
            "src/ouroboros/tui/screens/dashboard_v3.py", "DashboardScreenV3"
        )["r"],
        "session_selector": app_actions["r"],
        "execution": _binding_actions("src/ouroboros/tui/screens/execution.py", "ExecutionScreen")[
            "r"
        ],
        "debug": _binding_actions("src/ouroboros/tui/screens/debug.py", "DebugScreen")["r"],
        "logs": "unbound",
        "lineage_selector": _binding_actions(
            "src/ouroboros/tui/screens/lineage_selector.py", "LineageSelectorScreen"
        )["r"],
        "lineage_detail": _binding_actions(
            "src/ouroboros/tui/screens/lineage_detail.py", "LineageDetailScreen"
        )["r"],
    }
    assert _documented_textual_actions(EN_GUIDE) == source_actions
    assert _documented_textual_actions(KO_GUIDE) == source_actions


def test_slt_screen_and_mock_fallback_contract_matches_source() -> None:
    source = _read(REPO_ROOT / "crates/ouroboros-tui/src/main.rs")
    mapping_match = re.search(
        r"state\.screen = match state\.tabs\.selected \{(?P<body>.*?)\n\s*\};",
        source,
        re.DOTALL,
    )
    assert mapping_match is not None
    source_screen_by_tab = dict(re.findall(r"(\d) => Screen::(\w+)", mapping_match.group("body")))
    assert source_screen_by_tab == {
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

    # The documented key -> screen mapping must match the source's tab-index
    # -> Screen mapping (tab index is the key minus one), not just contain the
    # right tokens somewhere in the section -- otherwise swapping which key
    # opens which screen in the docs would go undetected.
    source_screen_by_key = {
        str(int(tab_index) + 1): screen for tab_index, screen in source_screen_by_tab.items()
    }
    doc_screen_name_normalization = {"Sessions": "SessionSelector"}
    en_rows = _slt_screens_table_rows(_read(EN_GUIDE))
    en_screen_by_key = {
        key: doc_screen_name_normalization.get(screen, screen) for key, _alt, screen in en_rows
    }
    assert en_screen_by_key == source_screen_by_key

    # KO must document the identical key/alt-key structure as EN (screen
    # names differ by locale, so compare structure, not translated text).
    ko_rows = _slt_screens_table_rows(_read(KO_GUIDE))
    assert [(key, alt) for key, alt, _screen in ko_rows] == [
        (key, alt) for key, alt, _screen in en_rows
    ]

    for guide in GUIDES:
        text = _read(guide)
        lifecycle = _contract_section(text, "<!-- tui-contract:slt-lifecycle -->")
        compact_lifecycle = " ".join(lifecycle.split())
        assert "--mock" in lifecycle
        if guide == EN_GUIDE:
            assert "contains no events" in compact_lifecycle
            assert "database cannot be opened" in compact_lifecycle
            assert "observer" in compact_lifecycle
            assert "lifecycle controls are removed" in compact_lifecycle
        else:
            assert "이벤트가 하나도 없는" in compact_lifecycle
            assert "데이터베이스를 열 수 없어" in compact_lifecycle
            assert "관찰자" in compact_lifecycle
            assert "생명주기 제어가 사라집니다" in compact_lifecycle
        assert "`p`" in lifecycle and "`r`" in lifecycle

    slt_readme = _read(SLT_README)
    readme_screen_by_key = {
        key: doc_screen_name_normalization.get(screen, screen)
        for key, _alt, screen in re.findall(r"\| `(\d)` \|(?: `?(\w*)`?)? \| (\w+)", slt_readme)
    }
    assert readme_screen_by_key == source_screen_by_key
    for key in ("`e`", "`s`", "`l`", "`Esc`"):
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
