"""Bind the documented CLI surface to the registry in ``cli/main.py``.

Runtime guides carry a table mapping each skill to its terminal equivalent, and
a cell reading ``(MCP only)`` asserts that no ``ouroboros`` subcommand exists.
Nothing enforced that, so the claim could stay behind after a command shipped.

The expectation here is derived from the source rather than restated: the
registry is read with ``ast``, so a new command is picked up without editing a
list. Only the docs assertions are literal.

``cli/main.py`` declares its commands in the ``_COMMANDS`` table and loads each
one on first lookup. The extractor reads that table and, so that a command
registered directly on ``app`` is not reported missing, the three direct
registration shapes too:

1. ``app.add_typer(mod.app, name="run")`` for a subcommand group.
2. ``@app.command()`` (optionally ``@app.command("name")``) on a function.
3. ``app.command(name="qa", ...)(qa.qa_command)`` applied as a direct call.
4. ``_Group("run", ...)`` / ``_Command("qa", ...)`` entries of ``_COMMANDS``.

Shape 3 has no decorator and no local function, so a decorator-only walk misses
it, and shape 4 is neither a call on ``app`` nor a decorator. The extractor is
checked against the runtime command table, so it can neither miss a registered
command nor report an unregistered one.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_MAIN = REPO_ROOT / "src" / "ouroboros" / "cli" / "main.py"
DOCS_ROOT = REPO_ROOT / "docs"

# A cell asserting that no terminal subcommand exists, in each shipped language.
_MCP_ONLY = re.compile(r"\(MCP only\)|\(MCP 전용\)|\(仅 MCP\)")
# ``ooo qa`` / ``ouroboros run`` inside a table cell.
_SKILL_REF = re.compile(r"`(?:ooo|ouroboros)\s+([a-z][a-z0-9-]*)")


def _string_name(call: ast.Call) -> str | None:
    """Return the literal ``name`` of a typer registration call, if given."""
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    for keyword in call.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str):
                return value
    return None


# Registration types of the ``_COMMANDS`` table in ``cli/main.py``.
_TABLE_ENTRY_TYPES = frozenset({"_Group", "_Command"})


def _table_entries(tree: ast.Module) -> list[ast.Call]:
    """Return the ``_Group(...)`` / ``_Command(...)`` calls inside ``_COMMANDS``."""
    entries: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        is_table = any(isinstance(t, ast.Name) and t.id == "_COMMANDS" for t in targets)
        if not is_table or node.value is None:
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in _TABLE_ENTRY_TYPES
            ):
                entries.append(call)
    return entries


def registered_cli_surface(source: str | None = None) -> frozenset[str]:
    """Return every top-level name ``ouroboros`` accepts, read from the source."""
    tree = ast.parse(CLI_MAIN.read_text(encoding="utf-8") if source is None else source)
    names: set[str] = set()

    # Shape 4: _Group("run", ...) / _Command("qa", ...) in the _COMMANDS table.
    for entry in _table_entries(tree):
        name = _string_name(entry)
        if name:
            names.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Shape 1: app.add_typer(mod.app, name="run")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "add_typer":
                name = _string_name(node)
                if name:
                    names.add(name)
            # Shape 3: app.command(name="qa", ...)(qa.qa_command)
            if isinstance(node.func, ast.Call):
                inner = node.func
                if isinstance(inner.func, ast.Attribute) and inner.func.attr == "command":
                    name = _string_name(inner)
                    if name:
                        names.add(name)
        # Shape 2: @app.command(...) on a function
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                attribute = call.func if call else decorator
                if isinstance(attribute, ast.Attribute) and attribute.attr == "command":
                    explicit = _string_name(call) if call else None
                    names.add(explicit or node.name.replace("_", "-"))

    return frozenset(names)


_EVERY_SHAPE = """
app.add_typer(run.app, name="run")


@app.command(hidden=True)
def monitor() -> None: ...


app.command(name="qa", help="QA.")(qa.qa_command)
_COMMANDS = (
    _Group("job", "ouroboros.cli.commands.job"),
    _Command("dispatch", "ouroboros.cli.commands.dispatch", "dispatch_command", hidden=True),
)
# Not registrations: a table entry outside ``_COMMANDS`` and a bare string.
_UNUSED = _Command("evolve", "ouroboros.cli.commands.evolve", "evolve_command")
_NOTE = "evaluate"
"""


def test_all_registration_shapes_are_extracted() -> None:
    """Guard the extractor itself, since a partial one fails open."""
    surface = registered_cli_surface(_EVERY_SHAPE)

    # Shape 1: a subcommand group.
    assert "run" in surface
    # Shape 2: a decorated function.
    assert "monitor" in surface
    # Shape 3: direct-call registration, invisible to a decorator-only walk.
    assert "qa" in surface
    # Shape 4: entries of the lazily loaded ``_COMMANDS`` table.
    assert {"job", "dispatch"} <= surface
    # An unregistered name is not reported as registered.
    assert surface == {"run", "monitor", "qa", "job", "dispatch"}


def test_extracted_surface_is_the_runtime_command_table() -> None:
    """Every name Click resolves without plugin dispatch, and no other name."""
    import typer.main

    from ouroboros.cli.main import app

    runtime = frozenset(typer.main.get_command(app).commands)

    assert registered_cli_surface() == runtime
    assert {"run", "monitor", "qa", "dispatch"} <= runtime


def test_mcp_only_claims_name_no_registered_command() -> None:
    """A guide may not say ``(MCP only)`` for a shipped terminal subcommand."""
    surface = registered_cli_surface()
    violations: list[str] = []

    for path in sorted(DOCS_ROOT.rglob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not _MCP_ONLY.search(line):
                continue
            for name in _SKILL_REF.findall(line):
                if name in surface:
                    relative = path.relative_to(REPO_ROOT)
                    violations.append(
                        f"{relative}:{lineno} claims '{name}' is MCP-only, "
                        f"but 'ouroboros {name}' is registered in cli/main.py"
                    )

    assert not violations, "Documented CLI surface is behind the registry:\n" + "\n".join(
        violations
    )


def test_genuinely_mcp_only_skills_stay_unregistered() -> None:
    """The other direction: these have no terminal equivalent, so guides may say so.

    If one of these ships as a subcommand, the guides claiming ``(MCP only)``
    become wrong and this test names the skill to fix.
    """
    surface = registered_cli_surface()
    mcp_only = {"evaluate", "evolve", "unstuck", "tutorial", "welcome"}
    newly_registered = sorted(mcp_only & surface)

    assert not newly_registered, (
        "These now have terminal subcommands, so every '(MCP only)' cell naming "
        f"them is stale: {newly_registered}"
    )
