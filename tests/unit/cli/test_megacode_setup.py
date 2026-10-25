"""Unit tests for ``ouroboros setup --runtime megacode``."""

from __future__ import annotations

from pathlib import Path
import tomllib

import pytest
import typer

from ouroboros.cli.commands import megacode_setup
from ouroboros.cli.commands.megacode_setup import (
    find_config_conflicts,
    install_megacode_skills,
    merge_managed_block,
    select_megacode_runtime,
    setup_megacode,
    strip_managed_block,
)

_BEGIN = "# >>> begin >>>"
_END = "# <<< end <<<"
_MCP_ENTRY = {
    "command": "uvx",
    "args": ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
}


def _write_skill(root: Path, folder: str, name: str, body: str = "") -> None:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def packaged_skills(tmp_path: Path) -> Path:
    root = tmp_path / "packaged"
    _write_skill(root, "run", "ouroboros-run")
    _write_skill(root, "seed", "seed", body="Next: read `../run/SKILL.md`.")
    return root


@pytest.fixture
def harness_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "harness-config"
    config_dir.mkdir()
    monkeypatch.setattr(megacode_setup, "resolve_harness_config_dir", lambda _path: config_dir)
    return config_dir


def test_merge_appends_once_and_replaces_in_place() -> None:
    first = merge_managed_block('mode = "manual"\n', f"{_BEGIN}\nA\n{_END}", _BEGIN, _END)
    second = merge_managed_block(first, f"{_BEGIN}\nB\n{_END}", _BEGIN, _END)

    assert first == f'mode = "manual"\n\n{_BEGIN}\nA\n{_END}\n'
    assert second == f'mode = "manual"\n\n{_BEGIN}\nB\n{_END}\n'


def test_strip_removes_only_the_managed_block() -> None:
    text = f'mode = "manual"\n\n{_BEGIN}\n[skills]\n{_END}\n\n[ui]\nsplash = "x"\n'
    assert strip_managed_block(text, _BEGIN, _END) == 'mode = "manual"\n\n[ui]\nsplash = "x"\n'


def test_conflicts_name_user_tables_the_block_would_redefine() -> None:
    user = '[skills]\nextra_roots = []\n[modes.ouroboros-worker]\nask = []\n[mcp.servers.ouroboros]\nurl = "x"\n'
    assert find_config_conflicts(user) == [
        "[skills]",
        "[modes.ouroboros-worker]",
        "[mcp.servers.ouroboros]",
    ]
    assert find_config_conflicts('[mcp.servers.other]\nurl = "x"\n') == []


def test_skills_are_copied_into_folders_named_by_frontmatter(
    tmp_path: Path, packaged_skills: Path
) -> None:
    root = install_megacode_skills(tmp_path / "cfg", skills_dir=packaged_skills)

    assert sorted(path.name for path in root.iterdir()) == ["ouroboros-run", "seed"]
    assert "../ouroboros-run/SKILL.md" in (root / "seed" / "SKILL.md").read_text()


def test_skill_reinstall_replaces_the_managed_root(tmp_path: Path, packaged_skills: Path) -> None:
    root = install_megacode_skills(tmp_path / "cfg", skills_dir=packaged_skills)
    (root / "stale").mkdir()

    install_megacode_skills(tmp_path / "cfg", skills_dir=packaged_skills)

    assert not (root / "stale").exists()


def test_setup_writes_block_skills_and_routing_and_keeps_user_text(
    harness_config: Path, packaged_skills: Path
) -> None:
    (harness_config / "config.toml").write_text('mode = "manual"\n', encoding="utf-8")
    (harness_config / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")

    assert setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    assert setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)

    text = (harness_config / "config.toml").read_text()
    config = tomllib.loads(text)
    assert config["mode"] == "manual"
    assert config["skills"]["extra_roots"] == [str(harness_config / "ouroboros" / "skills")]
    assert config["modes"]["ouroboros-worker"]["ask"] == []
    assert "mcp" not in config["modes"]["ouroboros-worker"]["tools"]
    assert "skill" not in config["modes"]["ouroboros-worker"]["tools"]
    assert config["modes"]["ouroboros-llm"]["tools"] == ["read_file", "glob", "search"]
    server = config["mcp"]["servers"]["ouroboros"]
    assert server["command"][-4:] == ["--runtime", "megacode", "--llm-backend", "megacode"]
    assert server["sandbox"] is False
    assert text.count("managed by `ouroboros setup --runtime megacode`") == 1

    agents = (harness_config / "AGENTS.md").read_text()
    assert agents.startswith("# Mine\n")
    assert agents.count("## Ouroboros") == 1
    assert "load the `ouroboros-run` skill" in agents


def test_setup_refuses_to_redefine_user_tables(harness_config: Path, packaged_skills: Path) -> None:
    original = '[skills]\nextra_roots = ["/mine"]\n'
    (harness_config / "config.toml").write_text(original, encoding="utf-8")

    assert not setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    assert (harness_config / "config.toml").read_text() == original
    assert not (harness_config / "ouroboros").exists()


def test_setup_refuses_invalid_toml(harness_config: Path, packaged_skills: Path) -> None:
    (harness_config / "config.toml").write_text("mode = \n", encoding="utf-8")

    assert not setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)


def test_select_runtime_requires_the_harness_cli() -> None:
    with pytest.raises(typer.Exit):
        select_megacode_runtime({"megacode": None})
