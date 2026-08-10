"""Keep the EN/KO TUI guides aligned with both runtime backends."""

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
    assert en_keys.count("does **not** resume") == 5
    for screen in (
        "실행 화면",
        "디버그 화면",
        "로그 화면",
        "계보 선택 화면",
        "계보 상세 화면",
    ):
        assert screen in ko_keys
    assert ko_keys.count("재개하지 않음") == 5
    assert "Dashboard and Session Selector expose resume" in en_keys
    assert "대시보드와 세션 선택 화면은 재개를 제공" in ko_keys
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
        assert "**Logs**" not in screens and "**Debug**" not in screens
        assert "**로그**" not in screens and "**디버그**" not in screens
        assert "--mock" in lifecycle
        assert "`p` / `r`" in lifecycle

    assert "`l` opens" in _read(EN_GUIDE)
    assert "when no modal owns the key, `Esc`\ncloses it" in _read(EN_GUIDE)
    assert "`Esc` closes the palette and\npreserves the underlying log panel and filter" in _read(
        EN_GUIDE
    )
    assert "While the filter has focus, `l`\nenters filter text" in _read(EN_GUIDE)
    assert "global `q`, `1`-`4`, and\n`Ctrl+P` shortcuts remain reserved" in _read(EN_GUIDE)
    assert "`l`은 실행" in _read(KO_GUIDE)
    assert "모달이 키를 소유하지 않을 때 `Esc`는 패널을 닫습니다" in _read(KO_GUIDE)
    assert "`Esc`는 팔레트만 닫고 아래의 로그 패널과 필터는\n그대로 보존합니다" in _read(KO_GUIDE)
    assert "필터에 포커스가 있을 때 `l`은 패널을 닫지 않고 필터 문자로\n입력됩니다" in _read(
        KO_GUIDE
    )
    assert "`q`, `1`-`4`, `Ctrl+P`는 계속 예약됩니다" in _read(KO_GUIDE)

    en_lifecycle = _contract_section(_read(EN_GUIDE), "<!-- tui-contract:slt-lifecycle -->")
    ko_lifecycle = _contract_section(_read(KO_GUIDE), "<!-- tui-contract:slt-lifecycle -->")
    assert (
        "| `Esc` | Close the command palette when active; return from Sessions to "
        "Dashboard; close the open log panel in Execution |"
    ) in en_lifecycle
    assert (
        "| `Esc` | 명령 팔레트가 열려 있으면 팔레트 닫기, 세션 화면에서는 대시보드로 "
        "돌아가기, 실행 화면에서는 열린 로그 패널 닫기 |" in ko_lifecycle
    )
    assert "| `Esc` | Close the open Execution log panel |" not in _read(EN_GUIDE)
    assert (
        "Close the command palette when active; otherwise close the open Execution log panel"
        not in _read(EN_GUIDE)
    )
    assert "| `Esc` | 열린 실행 로그 패널 닫기 |" not in _read(KO_GUIDE)
    assert "명령 팔레트가 열려 있으면 팔레트만 닫고, 그 외에는" not in _read(KO_GUIDE)

    assert "contains no events" in _read(EN_GUIDE)
    assert "cannot be opened" in _read(EN_GUIDE)
    assert "이벤트가 하나도 없는" in _read(KO_GUIDE)
    assert "열 수 없어" in _read(KO_GUIDE)

    slt_readme = _read(SLT_README)
    assert "global `q`, `1-4`, and `Ctrl+P` remain reserved" in slt_readme
    for row in (
        "| `1` | | Dashboard",
        "| `2` | | Execution",
        "| `3` | `e` | Lineage",
        "| `4` | `s` | Sessions",
        "| | `l` | Open the log panel",
        "| | `Esc` | Close the open log panel when no modal is active",
    ):
        assert row in slt_readme
    assert "`Esc` closes only the palette and preserves the underlying log panel and filter" in (
        slt_readme
    )
    assert "`Esc` close palette / return from Sessions / close Execution logs" in slt_readme
    assert "`1-5` screens" not in slt_readme


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
    assert en.count("|-----") == ko.count("|-----")


@pytest.mark.parametrize("guide", GUIDES)
def test_tui_guide_relative_links_resolve(guide: Path) -> None:
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(guide)):
        if target.startswith(("http://", "https://", "#")):
            continue
        path = (guide.parent / target.split("#", 1)[0]).resolve()
        assert path.exists(), f"broken link in {guide.relative_to(REPO_ROOT)}: {target}"
