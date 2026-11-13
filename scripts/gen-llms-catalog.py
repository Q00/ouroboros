#!/usr/bin/env python3
"""Render the registry catalogs in ``llms.txt`` and ``llms-full.txt``.

Usage:
    uv run python scripts/gen-llms-catalog.py          # rewrite stale blocks
    uv run python scripts/gen-llms-catalog.py --check  # exit 1 with a diff if stale

Only the lines between a pair of marker lines are generated:

    <!-- generated:<name> begin -->
    ...
    <!-- generated:<name> end -->

Everything outside the markers is hand-written and left byte-for-byte alone.
The markers are HTML comments on lines of their own. ``llms.txt`` follows the
llmstxt.org Markdown format, where a comment does not render; ``llms-full.txt``
is plain text read by models, where the line is inert.

Each block is rendered from the registry the code itself uses:

``cli-commands``
    The Click tree Typer builds from ``ouroboros.cli.main.app``: every top-level
    command ``ouroboros --help`` lists (hidden ones are left out) with its short
    help. A name that registers the same target as an earlier one is rendered
    as that command's alias: the same Typer app or callback for an eager
    registration, the same module and attribute for an entry of the lazy
    ``ouroboros.cli.main._COMMANDS`` table.
``skills``
    The ``SKILL.md`` bundles under ``skills/``, through
    ``ouroboros.router.registry.SkillDispatchRegistry``: each skill's name, the
    other identifiers ``ooo <identifier>`` resolves to it (its frontmatter
    ``name`` and aliases, as the registry resolves them), and its frontmatter
    ``description``. A skill whose frontmatter sets
    ``disable-model-invocation: true`` cannot be started by the model from a
    natural-language ``ooo <name>``, so it is rendered as the explicit
    ``/ouroboros:<name>`` command instead.
``mcp-tools``
    ``ouroboros.mcp.tool_manifest.BUILTIN_TOOL_NAMES``, the import-safe manifest
    that ``tests/unit/cli/test_mcp_doctor_probe_lifecycle.py`` pins to the
    production server composition. Names only: a description lives on each
    handler's ``definition``, and reading those means building the server
    (an EventStore plus project and state directories) or walking the AST,
    which cannot resolve the four tools whose names are not literals. The
    server reports descriptions through ``tools/list``.

Output is deterministic: entries are sorted by name, and nothing depends on
the clock, the environment, or the installed version.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import difflib
from functools import cache
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGENERATE = "uv run python scripts/gen-llms-catalog.py"

_MARKER = re.compile(r"^<!-- generated:(?P<name>[a-z0-9-]+) (?P<edge>begin|end) -->$")
# Claude Code's skill frontmatter key: the skill runs only when the user types
# its slash command, never from the model.
EXPLICIT_ONLY_KEY = "disable-model-invocation"
EXPLICIT_COMMAND = "/ouroboros:{name}"


class CatalogError(Exception):
    """A target file's markers are missing, unbalanced, or unknown."""


@dataclass(frozen=True)
class Entry:
    name: str
    text: str
    aliases: tuple[str, ...] = ()
    # The explicit command, for a skill that runs only when it is typed.
    explicit_command: str | None = None


# --------------------------------------------------------------------------
# Registries.


def cli_entries(app: Any = None, lazy: Iterable[Any] = ()) -> list[Entry]:
    """Top-level ``ouroboros`` commands, from the Click tree of ``app``.

    ``lazy`` is the lazy registration table behind ``app``. Without an ``app``,
    both come from ``ouroboros.cli.main``.
    """
    import typer.main

    if app is None:
        from ouroboros.cli import main as cli_main

        app, lazy = cli_main.app, cli_main._COMMANDS

    root = typer.main.get_command(app)
    aliases = _top_level_aliases(app, lazy)
    entries = []
    for name, command in sorted(root.commands.items()):
        if command.hidden:
            continue
        if name in aliases:
            text = f"Alias of `ouroboros {aliases[name]}`."
        else:
            text = _one_line(command.get_short_help_str(limit=500))
        entries.append(Entry(f"ouroboros {name}", text))
    return entries


def _top_level_aliases(app: Any, lazy: Iterable[Any] = ()) -> dict[str, str]:
    """Alias -> canonical name for names that register an earlier name's target.

    ``app.add_typer`` / ``app.command`` register an object, so their target is
    that object. ``lazy`` holds lazy registrations (``_Group`` / ``_Command``
    entries of ``ouroboros.cli.main._COMMANDS``), which name their target by
    ``module`` and ``attribute`` and import it only when Click looks it up; that
    pair is their target. Eager registrations come first, as in the root table.
    """
    import typer.main

    registrations: list[tuple[str, Hashable]] = [
        (info.name, id(info.typer_instance)) for info in app.registered_groups if info.name
    ]
    registrations += [
        (info.name or typer.main.get_command_name(info.callback.__name__), id(info.callback))
        for info in app.registered_commands
        if info.callback is not None
    ]
    registrations += [(entry.name, (entry.module, entry.attribute)) for entry in lazy]
    first: dict[Hashable, str] = {}
    aliases: dict[str, str] = {}
    for name, target in registrations:
        canonical = first.setdefault(target, name)
        if canonical != name:
            aliases[name] = canonical
    return aliases


def skill_entries(skills_dir: Path | None = None) -> list[Entry]:
    """Packaged skills: dispatch name, aliases, and frontmatter description.

    The aliases are the registry's own answer: every identifier it collects
    for a skill (frontmatter ``name`` and alias fields included) that
    ``registry.resolve`` maps back to that skill. An identifier another skill
    wins is not listed under this one. A skill with
    ``disable-model-invocation: true`` carries its ``/ouroboros:<name>``
    command as ``explicit_command``.
    """
    from ouroboros.router.dispatch import load_skill_frontmatter
    from ouroboros.router.registry import SkillDispatchRegistry

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir or ROOT / "skills")
    entries = []
    for target in sorted(registry.targets, key=lambda target: target.skill_name):
        frontmatter = load_skill_frontmatter(target.skill_path)
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            raise CatalogError(f"{target.skill_path}: frontmatter has no description")
        aliases = tuple(
            identifier
            for identifier in target.identifiers
            if identifier != target.skill_name and registry.resolve(identifier) is target
        )
        explicit_command = (
            EXPLICIT_COMMAND.format(name=target.skill_name)
            if frontmatter.get(EXPLICIT_ONLY_KEY) is True
            else None
        )
        entries.append(Entry(target.skill_name, _one_line(description), aliases, explicit_command))
    return entries


def mcp_tool_entries(names: Iterable[str] | None = None) -> list[Entry]:
    """Built-in MCP tool names, from the import-safe manifest."""
    if names is None:
        from ouroboros.mcp.tool_manifest import BUILTIN_TOOL_NAMES

        names = BUILTIN_TOOL_NAMES
    return [Entry(name, "") for name in sorted(names)]


def _one_line(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Renderers: one per (file, block). ``llms.txt`` is Markdown, ``llms-full.txt``
# an indented plain-text layout.


def _markdown_list(entries: Sequence[Entry]) -> str:
    lines = []
    for entry in entries:
        line = f"- `{entry.name}`"
        if entry.explicit_command:
            line += f" (invoke as `{entry.explicit_command}`)"
        if entry.aliases:
            line += " (aliases: " + ", ".join(f"`{alias}`" for alias in entry.aliases) + ")"
        lines.append(f"{line}: {entry.text}" if entry.text else line)
    return "\n".join(lines)


def _plain_columns(entries: Sequence[Entry], indent: int = 2) -> str:
    names = []
    for entry in entries:
        name = entry.explicit_command or entry.name
        names.append(f"{name} ({', '.join(entry.aliases)})" if entry.aliases else name)
    width = max(map(len, names)) + 2
    pad = " " * indent
    return "\n".join(
        f"{pad}{name.ljust(width)}{entry.text}".rstrip()
        for name, entry in zip(names, entries, strict=True)
    )


Renderer = Callable[[], str]


def renderers() -> dict[str, dict[str, Renderer]]:
    """Target file (relative to the root) -> block name -> renderer."""
    cli = cache(cli_entries)
    skills = cache(skill_entries)
    return {
        "llms.txt": {
            "cli-commands": lambda: _markdown_list(cli()),
            "skills": lambda: _markdown_list(skills()),
        },
        "llms-full.txt": {
            "skills": lambda: _plain_columns(skills()),
            "cli-commands": lambda: _plain_columns(cli()),
            "mcp-tools": lambda: _plain_columns(mcp_tool_entries(), indent=4),
        },
    }


# --------------------------------------------------------------------------
# Marker handling.


def replace_blocks(text: str, blocks: Mapping[str, str], *, path: str) -> str:
    """Return ``text`` with each marked block's body replaced by ``blocks[name]``.

    Every block in ``blocks`` must appear exactly once, and every marker pair in
    ``text`` must name a block in ``blocks``; anything else is a
    :class:`CatalogError`, so a renamed or deleted marker cannot silently stop
    a block from being checked.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    seen: set[str] = set()
    open_name: str | None = None
    open_line = 0
    for lineno, line in enumerate(lines, start=1):
        match = _MARKER.match(line.strip())
        if match is None:
            if open_name is None:
                out.append(line)
            continue
        name, edge = match.group("name"), match.group("edge")
        if edge == "begin":
            if open_name is not None:
                raise CatalogError(f"{path}:{lineno}: `{name}` begins inside `{open_name}`")
            if name not in blocks:
                raise CatalogError(f"{path}:{lineno}: no generator for block `{name}`")
            if name in seen:
                raise CatalogError(f"{path}:{lineno}: block `{name}` appears twice")
            seen.add(name)
            open_name, open_line = name, lineno
            out.append(line)
            out.append(blocks[name].rstrip("\n") + "\n")
        else:
            if open_name != name:
                raise CatalogError(f"{path}:{lineno}: `{name}` ends but `{open_name}` is open")
            open_name = None
            out.append(line)
    if open_name is not None:
        raise CatalogError(f"{path}:{open_line}: block `{open_name}` never ends")
    missing = sorted(set(blocks) - seen)
    if missing:
        raise CatalogError(f"{path}: missing marker block(s): {', '.join(missing)}")
    return "".join(out)


def render(root: Path = ROOT) -> dict[Path, tuple[str, str]]:
    """Target path -> (current text, regenerated text)."""
    results: dict[Path, tuple[str, str]] = {}
    for rel, file_renderers in renderers().items():
        path = root / rel
        current = path.read_text(encoding="utf-8")
        blocks = {name: renderer() for name, renderer in file_renderers.items()}
        results[path] = (current, replace_blocks(current, blocks, path=rel))
    return results


def stale_diffs(root: Path = ROOT) -> list[str]:
    """A unified diff for each target whose generated blocks are out of date."""
    diffs = []
    for path, (current, expected) in render(root).items():
        if current != expected:
            rel = path.relative_to(root).as_posix()
            diffs.append(
                "".join(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        expected.splitlines(keepends=True),
                        fromfile=f"{rel} (committed)",
                        tofile=f"{rel} (generated)",
                    )
                )
            )
    return diffs


def write(root: Path = ROOT) -> list[Path]:
    """Rewrite every stale target atomically. Returns the paths written."""
    written = []
    for path, (current, expected) in render(root).items():
        if current == expected:
            continue
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(expected)
            os.chmod(tmp, path.stat().st_mode & 0o777)  # mkstemp creates it 0600
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 with a diff instead of rewriting"
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            diffs = stale_diffs(root)
            if diffs:
                sys.stdout.write("".join(diffs))
                print(f"\nGenerated catalogs are stale. Regenerate with:\n  {REGENERATE}")
                return 1
            print("Generated catalogs are current.")
            return 0
        for path in write(root):
            print(f"rewrote {path.relative_to(root).as_posix()}")
        return 0
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
