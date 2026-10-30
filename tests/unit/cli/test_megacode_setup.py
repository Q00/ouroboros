"""Unit tests for ``ouroboros setup --runtime megacode``."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
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


def test_paths_preserves_spaces_and_uses_only_absolute_config(tmp_path: Path) -> None:
    harness = tmp_path / "harness stub"
    target = tmp_path / "config with spaces"
    harness.write_text(f"#!{sys.executable}\nprint({str('CONFIG ' + str(target))!r})\n")
    harness.chmod(0o700)
    assert megacode_setup.resolve_harness_config_dir(str(harness)) == target
    harness.write_text(f'#!{sys.executable}\nprint("CONFIG relative/path  (XDG_CONFIG_HOME)")\n')
    assert megacode_setup.resolve_harness_config_dir(str(harness)) is None


def test_paths_removes_only_the_final_source_annotation(tmp_path: Path) -> None:
    harness = tmp_path / "harness"
    target = tmp_path / "config  (default)"
    output = f"CONFIG   {target}  (HARNESS_HOME)"
    harness.write_text(f"#!{sys.executable}\nprint({output!r})\n")
    harness.chmod(0o700)
    assert megacode_setup.resolve_harness_config_dir(str(harness)) == target


def test_render_roundtrips_control_characters_and_quotes(tmp_path: Path) -> None:
    skills = tmp_path / 'quote" slash\\ tab\t newline\n 😀'
    command = ["uvx", 'quote"', "slash\\", "tab\t", "newline\n", "\x00", "\x7f", "😀"]
    block = megacode_setup.render_harness_block(skills_root=skills, mcp_command=command)
    data = tomllib.loads(block)
    assert data["skills"]["extra_roots"] == [str(skills)]
    assert data["mcp"]["servers"]["ouroboros"]["command"] == command


_NATIVE_PATHS = json.loads(
    (Path(__file__).parents[2] / "fixtures/megacode/harness-0.23.0-paths.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", _NATIVE_PATHS, ids=lambda case: case["name"])
def test_paths_from_native_023_fixture(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        megacode_setup.subprocess,
        "run",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args[0], 0, case["stdout"], ""),
    )
    assert megacode_setup.resolve_harness_config_dir("harness") == Path(case["config_dir"])


def test_invalid_render_does_not_replace_installed_skills(
    harness_config: Path, packaged_skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = install_megacode_skills(harness_config, skills_dir=packaged_skills)
    sentinel = root / "sentinel"
    sentinel.write_text("keep")
    monkeypatch.setattr(megacode_setup, "render_harness_block", lambda **_kwargs: "invalid = [")

    assert not setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    assert sentinel.read_text() == "keep"
    assert not (harness_config / "config.toml").exists()
    assert not (harness_config / "AGENTS.md").exists()


@pytest.mark.parametrize("name", ["../escaped", "ABSOLUTE", "..", ".", "a/b", "a\\b"])
def test_skill_name_cannot_escape_staging(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source"
    if name == "ABSOLUTE":
        name = str(tmp_path / "escaped")
    _write_skill(source, "evil", name)
    with pytest.raises(ValueError, match="skill.*name"):
        install_megacode_skills(tmp_path / "cfg", skills_dir=source)
    assert not (tmp_path / "cfg" / "ouroboros" / "escaped").exists()


def test_setup_through_paths_cli_is_idempotent(tmp_path: Path, packaged_skills: Path) -> None:
    target = tmp_path / 'config with spaces and "quotes"'
    target.mkdir()
    (target / "config.toml").write_text('mode = "manual"\n')
    (target / "AGENTS.md").write_text("# Mine\n")
    harness = tmp_path / "harness"
    harness.write_text(f"#!{sys.executable}\nprint({('CONFIG ' + str(target))!r})\n")
    harness.chmod(0o700)
    assert setup_megacode(str(harness), mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    first = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert setup_megacode(str(harness), mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    second = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert first == second
    assert not (tmp_path / "config").exists()
    parsed = tomllib.loads((target / "config.toml").read_text())
    assert parsed["skills"]["extra_roots"] == [str(target / "ouroboros" / "skills")]


@pytest.mark.parametrize(
    "destination", ["config.toml", "AGENTS.md", "ouroboros", "ouroboros/skills"]
)
def test_setup_refuses_symlinked_managed_destinations(
    harness_config: Path, packaged_skills: Path, tmp_path: Path, destination: str
) -> None:
    outside = tmp_path / "outside"
    is_dir = destination.startswith("ouroboros")
    if is_dir:
        outside.mkdir()
        (outside / "sentinel").write_text("keep")
    else:
        outside.write_text("# keep\n")
    link = harness_config / destination
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=is_dir)
    assert not setup_megacode("harness", mcp_entry=_MCP_ENTRY, skills_dir=packaged_skills)
    assert link.is_symlink()
    assert (
        (outside / "sentinel").read_text() == "keep"
        if is_dir
        else outside.read_text() == "# keep\n"
    )
