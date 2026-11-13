"""The registry catalogs in ``llms.txt`` and ``llms-full.txt`` are current.

``scripts/gen-llms-catalog.py`` renders the blocks between
``<!-- generated:<name> begin -->`` and ``<!-- generated:<name> end -->`` from
the CLI registry, the skill registry, and the MCP tool manifest. This module
runs the same check in-process, so a PR that adds a command, skill, or tool
without regenerating fails here with the command that fixes it.

Policy: docs/contributing/docs-freshness.md ("Generate").
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sys
from types import ModuleType
from typing import Annotated

import pytest
import typer

from tests.unit.docs._corpus import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "gen-llms-catalog.py"
TARGETS = ("llms.txt", "llms-full.txt")


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_llms_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: ``dataclass`` resolves the defining module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load_generator()


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    """A scratch root holding copies of the real target files."""
    for name in TARGETS:
        shutil.copy2(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def test_generated_blocks_are_current(gen: ModuleType) -> None:
    diffs = gen.stale_diffs(REPO_ROOT)
    assert not diffs, (
        "".join(diffs) + f"\nGenerated catalogs are stale. Regenerate with:\n  {gen.REGENERATE}"
    )


# --------------------------------------------------------------------------
# The generator itself, on scratch copies and synthetic registries.


def test_check_reports_a_stale_block_with_the_regenerate_command(
    gen: ModuleType, copy_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = copy_root / "llms.txt"
    text = target.read_text(encoding="utf-8")
    begin = "<!-- generated:cli-commands begin -->\n"
    stale_line = text.split(begin, 1)[1].splitlines(keepends=True)[0]
    assert stale_line.startswith("- `ouroboros ")
    target.write_text(text.replace(begin + stale_line, begin), encoding="utf-8")

    assert gen.main(["--check"], root=copy_root) == 1
    out = capsys.readouterr().out
    assert f"+{stale_line}" in out
    assert gen.REGENERATE in out
    # --check never writes.
    assert stale_line not in target.read_text(encoding="utf-8")


def test_write_restores_the_block_and_keeps_hand_written_text(
    gen: ModuleType, copy_root: Path
) -> None:
    target = copy_root / "llms-full.txt"
    original = target.read_text(encoding="utf-8")
    mode = target.stat().st_mode
    prose = "Hand-written prose outside every marker stays as it is.\n"
    begin = "<!-- generated:mcp-tools begin -->\n"
    stale = original.replace(begin, prose + begin + "    ouroboros_removed_tool\n")
    target.write_text(stale, encoding="utf-8")

    assert gen.main([], root=copy_root) == 0
    assert target.read_text(encoding="utf-8") == original.replace(begin, prose + begin)
    assert target.stat().st_mode == mode
    assert gen.main(["--check"], root=copy_root) == 0


def test_current_files_are_left_untouched(gen: ModuleType, copy_root: Path) -> None:
    assert gen.write(copy_root) == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("<!-- generated:a begin -->\nx\n", "block `a` never ends"),
        ("<!-- generated:a begin -->\n<!-- generated:b end -->\n", "`b` ends but `a` is open"),
        ("<!-- generated:zz begin -->\n<!-- generated:zz end -->\n", "no generator for block `zz`"),
        (
            "<!-- generated:a begin -->\n<!-- generated:a end -->\n" * 2,
            "block `a` appears twice",
        ),
        ("no markers at all\n", "missing marker block(s): a"),
        (
            "<!-- generated:a begin -->\n<!-- generated:a begin -->\n",
            "`a` begins inside `a`",
        ),
    ],
)
def test_marker_errors_fail_loudly(gen: ModuleType, text: str, error: str) -> None:
    with pytest.raises(gen.CatalogError, match=r"^doc\.md"):
        gen.replace_blocks(text, {"a": "A"}, path="doc.md")
    with pytest.raises(gen.CatalogError) as excinfo:
        gen.replace_blocks(text, {"a": "A"}, path="doc.md")
    assert error in str(excinfo.value)


def test_marker_error_exits_2(
    gen: ModuleType, copy_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = copy_root / "llms.txt"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("<!-- generated:skills end -->\n", ""), encoding="utf-8")

    assert gen.main(["--check"], root=copy_root) == 2
    assert "block `skills` never ends" in capsys.readouterr().err


def test_replace_blocks_rewrites_only_between_markers(gen: ModuleType) -> None:
    text = "head\n<!-- generated:a begin -->\nold\nlines\n<!-- generated:a end -->\ntail\n"

    assert gen.replace_blocks(text, {"a": "new"}, path="doc.md") == (
        "head\n<!-- generated:a begin -->\nnew\n<!-- generated:a end -->\ntail\n"
    )


def test_cli_entries_list_visible_commands_and_aliases(gen: ModuleType) -> None:
    app = typer.Typer()

    @app.callback()
    def main() -> None: ...

    @app.command(help="Build things. More detail here.")
    def build(fast: Annotated[bool, typer.Option("--fast")] = False) -> None: ...

    @app.command(hidden=True)
    def secret() -> None: ...

    group = typer.Typer(help="Talk to the user.")

    @group.command("start")
    def start() -> None: ...

    app.add_typer(group, name="init")
    app.add_typer(group, name="interview")

    entries = gen.cli_entries(app)

    assert [(e.name, e.text) for e in entries] == [
        ("ouroboros build", "Build things."),
        ("ouroboros init", "Talk to the user."),
        ("ouroboros interview", "Alias of `ouroboros init`."),
    ]


def test_aliases_are_read_from_the_lazy_registry(gen: ModuleType) -> None:
    from ouroboros.cli.main import _Command, _Group

    lazy = (
        _Group("init", "pkg.init"),
        _Command("seed", "pkg.seed", "seed_command"),
        _Group("interview", "pkg.init"),
        # Same module, another attribute; same attribute, another module.
        _Group("tools", "pkg.init", "tools_app"),
        _Group("other", "pkg.other"),
        _Command("again", "pkg.seed", "seed_command", hidden=True),
    )

    assert gen._top_level_aliases(typer.Typer(), lazy) == {"interview": "init", "again": "seed"}


def test_real_catalog_renders_the_lazily_registered_alias(gen: ModuleType) -> None:
    entries = {entry.name: entry.text for entry in gen.cli_entries()}

    assert entries["ouroboros interview"] == "Alias of `ouroboros init`."
    assert entries["ouroboros init"] != entries["ouroboros interview"]


def _skill(root: Path, directory: str, frontmatter: str) -> None:
    (root / directory).mkdir(parents=True)
    (root / directory / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# Body\n")


def test_skill_entries_come_from_the_dispatch_registry(gen: ModuleType, tmp_path: Path) -> None:
    _skill(
        tmp_path, "run", 'name: ouroboros-run\ndescription: "Run  a\n  Seed."\naliases: [execute]'
    )
    _skill(tmp_path, "auto", "name: auto\ndescription: Go.")
    (tmp_path / "not-a-skill").mkdir()

    entries = gen.skill_entries(tmp_path)

    # Sorted; whitespace collapsed; a frontmatter name that differs from the
    # directory is an alias, because ``ooo ouroboros-run`` resolves to ``run``.
    assert [(e.name, e.text, e.aliases) for e in entries] == [
        ("auto", "Go.", ()),
        ("run", "Run a Seed.", ("ouroboros-run", "execute")),
    ]
    assert gen._markdown_list(entries) == (
        "- `auto`: Go.\n- `run` (aliases: `ouroboros-run`, `execute`): Run a Seed."
    )
    assert gen._plain_columns(entries) == (
        "  auto                          Go.\n  run (ouroboros-run, execute)  Run a Seed."
    )


def test_skill_aliases_are_exactly_what_the_registry_resolves(
    gen: ModuleType, tmp_path: Path
) -> None:
    from ouroboros.router.registry import SkillDispatchRegistry

    # ``eval`` is claimed by both; the registry gives it to ``evaluate`` (first
    # claimant), and ``auto`` is a direct skill name, which always wins.
    _skill(tmp_path, "auto", "name: auto\ndescription: Go.")
    _skill(tmp_path, "evaluate", "name: evaluate\ndescription: Check.\naliases: [eval]")
    _skill(tmp_path, "qa", "name: qa\ndescription: Judge.\naliases: [eval, auto, verdict]")

    entries = {entry.name: entry.aliases for entry in gen.skill_entries(tmp_path)}
    registry = SkillDispatchRegistry.from_skills_dir(tmp_path)

    assert entries == {"auto": (), "evaluate": ("eval",), "qa": ("verdict",)}
    for target in registry.targets:
        listed = {target.skill_name, *entries[target.skill_name]}
        resolving = {i for i, t in registry.mapping.items() if t is target}
        assert listed == resolving


def test_skill_without_model_invocation_renders_as_its_explicit_command(
    gen: ModuleType, tmp_path: Path
) -> None:
    # ``disable-model-invocation: true`` means the model cannot start the skill
    # from ``ooo <name>``; only the typed ``/ouroboros:<name>`` runs it.
    _skill(tmp_path, "setup", "name: setup\ndescription: Set up.\ndisable-model-invocation: true")
    _skill(tmp_path, "run", "name: run\ndescription: Run.")
    _skill(tmp_path, "qa", "name: qa\ndescription: Judge.\ndisable-model-invocation: false")

    entries = gen.skill_entries(tmp_path)

    assert [(e.name, e.explicit_command) for e in entries] == [
        ("qa", None),
        ("run", None),
        ("setup", "/ouroboros:setup"),
    ]
    assert gen._markdown_list(entries) == (
        "- `qa`: Judge.\n- `run`: Run.\n- `setup` (invoke as `/ouroboros:setup`): Set up."
    )
    assert gen._plain_columns(entries) == (
        "  qa                Judge.\n  run               Run.\n  /ouroboros:setup  Set up."
    )


def test_every_bundled_skill_identifier_is_in_the_catalog(gen: ModuleType) -> None:
    from ouroboros.router.registry import SkillDispatchRegistry

    registry = SkillDispatchRegistry.from_skills_dir(REPO_ROOT / "skills")
    listed = {
        identifier for entry in gen.skill_entries() for identifier in (entry.name, *entry.aliases)
    }

    assert listed == set(registry.mapping)


def test_skill_without_a_description_is_an_error(gen: ModuleType, tmp_path: Path) -> None:
    _skill(tmp_path, "bare", "name: bare")

    with pytest.raises(gen.CatalogError, match="no description"):
        gen.skill_entries(tmp_path)


def test_mcp_tool_entries_are_sorted_names(gen: ModuleType) -> None:
    entries = gen.mcp_tool_entries({"ouroboros_b", "ouroboros_a"})

    assert gen._plain_columns(entries, indent=4) == "    ouroboros_a\n    ouroboros_b"


def test_rendering_is_deterministic(gen: ModuleType, copy_root: Path) -> None:
    first = {path.name: expected for path, (_, expected) in gen.render(copy_root).items()}
    second = {path.name: expected for path, (_, expected) in gen.render(copy_root).items()}

    assert first == second
    assert set(first) == set(TARGETS)
