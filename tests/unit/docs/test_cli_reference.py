"""Bind ``docs/cli-reference.md`` to the commands and flags the CLI registers.

The reference is written by hand from ``ouroboros --help``, and nothing tied
it back to the code: a command could ship without a section, and an example
could keep a flag the command does not accept until someone typed it. These
checks read the expected side from the Click tree that Typer builds from
``ouroboros.cli.main.app``, the object the ``ouroboros`` entry point runs, so a
new command or option is picked up without editing a list here.

**Coverage.** Every top-level command ``ouroboros --help`` lists (every
non-hidden one) has a heading containing the code span ``ouroboros <name>``,
as in ``## `ouroboros auto` ``. An *alias*
is a top-level name registered from the same Typer app or callback as an
earlier name (``interview`` is ``init``). It needs no heading of its own;
instead the canonical command's section must show it as ``ouroboros <alias>``,
so a reader who types the alias can find it. In the other direction, a heading
that is a single code span naming a command (``### `init start` ``) must name
a registered one.

**Flags.** Every ``ouroboros ...`` invocation in a fenced block or an inline
code span, and every flag in the first column of an options table, is resolved
against the tree. A table row belongs to the nearest enclosing command
heading, or to ``ouroboros`` itself when there is none. Subcommands resolve as
Click resolves them: a default-subcommand group (``run``, ``init``, ``zcode``)
is asked which subcommand it dispatches the remaining words to, so
``ouroboros run seed.yaml --debug`` reaches ``run workflow`` while
``ouroboros init --debug ...`` stays at ``init``, as the CLI does. Each
``--long`` or ``-x`` flag must be an option of the command reached so far, of
one of its parent groups, or a help option. ``--flag=value`` is split at
``=``; an option that takes a value consumes the next word. An invocation ends
at a shell operator (``|``, ``&&``, ``;``, ``>``, ...) or a ``#`` comment.

Non-goals: option values and positional arguments are not validated, and a
word under a group that names no subcommand (a metavar such as ``COMMAND``, or
a plugin name) leaves the invocation at that group. Flags in prose outside an
invocation or an options table are not bound to a command. Combined short
flags (``-abc``) and an inline code span broken across two lines are not
checked. A fenced command continued with a trailing ``\\`` is joined and
reported at its first line. A missing section has no line of its own and is
reported at line 1. Other docs in the corpus are out of scope for this module.

Related, not duplicated: ``tests/unit/test_cli_surface_docs_contract.py``
checks ``(MCP only)`` claims; ``tests/unit/test_cli_reference_backend_parity.py``
checks the ``--llm-backend`` value lists.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
import re
import shlex
from typing import Annotated, Any

import pytest
import typer
import typer.core
import typer.main

from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

CLI_REFERENCE = "docs/cli-reference.md"
PROGRAM = "ouroboros"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
# ``<plugin|subprocess>``, ``<session_id>``: a placeholder, not shell syntax.
_PLACEHOLDER = re.compile(r"<[A-Za-z][^<>]*>")
_LONG_FLAG = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")
_SHORT_FLAG = re.compile(r"-[A-Za-z]")
_TABLE_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z])(?![\w-])")
# A blockquoted fence (``> ```bash``) is not a fence to the corpus reader.
_QUOTED_FENCE = re.compile(r"^\s*(?:>\s*)+(?:```|~~~)")
_QUOTE_PREFIX = re.compile(r"^\s*(?:>\s*)+")
_SHELL_PUNCTUATION = frozenset("();<>|&")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Programs that run the program named later on their command line.
_LAUNCHERS = frozenset({"uv", "uvx", "pipx", "sudo", "env", "exec", "time"})


# --------------------------------------------------------------------------
# The registered side, read from the Click tree.


def _subcommands(command: Any) -> Mapping[str, Any]:
    """Subcommands of a Click group; empty for a leaf command."""
    return getattr(command, "commands", None) or {}


def _option_arity(command: Any) -> dict[str, bool]:
    """Every option string ``command`` accepts -> whether it consumes a value."""
    arity: dict[str, bool] = {}
    for param in command.params:
        if getattr(param, "param_type_name", None) != "option":
            continue
        for opt in param.opts:
            arity[opt] = not (param.is_flag or param.count)
        for opt in param.secondary_opts:  # ``--no-x`` of a ``--x/--no-x`` pair
            arity[opt] = False
    for opt in command.context_settings.get("help_option_names", ["--help"]):
        arity[opt] = False
    return arity


def _default_dispatch(group: Any, name: str, words: Sequence[str]) -> str | None:
    """The subcommand a default-subcommand group dispatches ``words`` to, if any.

    ``run``, ``init``, and ``zcode`` prepend a default subcommand in their own
    ``parse_args``, each with its own rule for when. Asking the group keeps
    this module from restating those rules.
    """
    if not getattr(group, "default_cmd_name", None):
        return None
    ctx = group.make_context(name, list(words), resilient_parsing=True)
    # Click 8.2+ keeps the subcommand in ``_protected_args``, 8.1 in
    # ``protected_args``. ``test_real_default_subcommand_groups_dispatch``
    # fails if neither holds it any more.
    names = getattr(ctx, "_protected_args", None)
    if names is None:
        names = ctx.protected_args
    return names[0] if names else None


@cache
def registered_cli() -> Any:
    """The Click tree ``ouroboros`` runs."""
    from ouroboros.cli.main import app

    return typer.main.get_command(app)


@cache
def registered_aliases() -> dict[str, str]:
    from ouroboros.cli.main import app

    return top_level_aliases(app)


def top_level_aliases(app: typer.Typer) -> dict[str, str]:
    """Alias -> canonical name, for names registered from an earlier name's target."""
    registrations: list[tuple[str, object]] = [
        (info.name, info.typer_instance) for info in app.registered_groups if info.name
    ]
    registrations += [
        (info.name or typer.main.get_command_name(info.callback.__name__), info.callback)
        for info in app.registered_commands
        if info.callback is not None
    ]
    first_by_target: dict[int, str] = {}
    aliases: dict[str, str] = {}
    for name, target in registrations:
        canonical = first_by_target.setdefault(id(target), name)
        if canonical != name:
            aliases[name] = canonical
    return aliases


@dataclass(frozen=True)
class Resolution:
    path: tuple[str, ...]
    #: ``(flag, command path where it was rejected)``, in order.
    unknown: tuple[tuple[str, tuple[str, ...]], ...]


def resolve(root: Any, words: Sequence[str]) -> Resolution:
    """Walk ``words`` (everything after ``ouroboros``) down the tree."""
    path: list[str] = []
    chain = [root]
    unknown: list[tuple[str, tuple[str, ...]]] = []
    asked_default: set[int] = set()
    index = 0
    while index < len(words):
        command = chain[-1]
        subcommands = _subcommands(command)
        word = words[index]
        if subcommands and word not in subcommands and id(command) not in asked_default:
            asked_default.add(id(command))
            target = _default_dispatch(command, path[-1] if path else PROGRAM, words[index:])
            if target in subcommands and target != word:
                path.append(target)
                chain.append(subcommands[target])
                continue
        if word == "--":
            break
        flag, has_inline_value, _ = word.partition("=")
        if _LONG_FLAG.fullmatch(flag) or _SHORT_FLAG.fullmatch(word):
            takes_value = next((a[flag] for a in map(_option_arity, chain) if flag in a), None)
            if takes_value is None:
                unknown.append((flag, tuple(path)))
            index += 2 if takes_value and not has_inline_value else 1
            continue
        if word in subcommands:
            path.append(word)
            chain.append(subcommands[word])
        index += 1
    return Resolution(tuple(path), tuple(unknown))


def _command_at(root: Any, path: Sequence[str]) -> Any:
    command = root
    for name in path:
        command = _subcommands(command)[name]
    return command


# --------------------------------------------------------------------------
# The documented side.


@dataclass(frozen=True)
class Invocation:
    """The words a doc passes to ``ouroboros`` at one location."""

    where: str
    words: tuple[str, ...]


def _segments(fragment: str) -> Iterator[list[str]]:
    """Shell words of each command in ``fragment``, split at shell operators.

    A word starting with ``#`` begins a comment and ends the fragment. A
    bracketed optional word (``[--all]``, ``[OPTIONS]``) loses its brackets.
    """
    text = _PLACEHOLDER.sub("PLACEHOLDER", fragment)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unbalanced quote: fall back to plain words
        words = text.split()
    segment: list[str] = []
    for word in words:
        if word.startswith("#"):
            break
        if set(word) <= _SHELL_PUNCTUATION:
            yield segment
            segment = []
            continue
        word = word.strip("[]").removesuffix("...")
        if word:
            segment.append(word)
    yield segment


def invocations_in(fragment: str) -> Iterator[tuple[str, ...]]:
    """The words after ``ouroboros`` for each run of it in ``fragment``.

    ``ouroboros`` counts only as the command word: first in its segment after
    a ``$`` prompt and ``NAME=value`` assignments, or the program a launcher
    such as ``uv run`` or ``uvx`` starts. In ``plugin list --marketplace
    ouroboros`` it is a value.
    """
    for words in _segments(fragment):
        start = 0
        while start < len(words) and (words[start] == "$" or _ASSIGNMENT.match(words[start])):
            start += 1
        head = words[start : start + 1]
        if head == [PROGRAM]:
            yield tuple(words[start + 1 :])
        elif head and head[0] in _LAUNCHERS and PROGRAM in words[start + 1 :]:
            rest = words[start + 1 :]
            yield tuple(rest[rest.index(PROGRAM) + 1 :])


def _code_fragments(lines: Iterable[DocLine]) -> Iterator[tuple[DocLine, str]]:
    """Each fenced-code command, and each inline code span of a prose line.

    A fenced line ending in ``\\`` continues on the next line; the joined
    command is reported at its first line.
    """
    in_quoted_fence = False
    pending: tuple[DocLine, str] | None = None
    for line in lines:
        if not line.in_code_block and _QUOTED_FENCE.match(line.text):
            in_quoted_fence = not in_quoted_fence
            continue
        if line.in_code_block or in_quoted_fence:
            text = line.text if line.in_code_block else _QUOTE_PREFIX.sub("", line.text)
            start, joined = pending if pending else (line, "")
            joined += text
            if joined.rstrip().endswith("\\"):
                pending = (start, joined.rstrip()[:-1] + " ")
                continue
            pending = None
            yield start, joined
            continue
        if pending:  # a continuation that ran into the closing fence
            yield pending
            pending = None
        for match in _CODE_SPAN.finditer(line.text):
            yield line, match.group(1)
    if pending:
        yield pending


def _heading(line: DocLine) -> tuple[int, str] | None:
    if line.in_code_block:
        return None
    match = _HEADING.match(line.text)
    return (len(match.group(1)), match.group(2)) if match else None


def _heading_command(root: Any, text: str) -> tuple[str, ...] | None:
    """The command path a heading such as ``### `init start` `` is the section for.

    Only a heading that is one code span starting with ``ouroboros`` or with a
    registered top-level name counts; ``### Global Options`` returns ``None``.
    """
    match = re.fullmatch(r"`([^`]+)`", text.strip())
    if not match:
        return None
    words = next(_segments(match.group(1)))
    if words[:1] == [PROGRAM]:
        words = words[1:]
    elif words[:1] and words[0] not in _subcommands(root):
        return None
    return tuple(word for word in words if not word.startswith("-"))


def _first_cell(row: str) -> str:
    """The first cell of a table row; a ``|`` inside a code span does not end it."""
    in_code = False
    for index, char in enumerate(row[1:], start=1):
        if char == "`":
            in_code = not in_code
        elif char == "|" and not in_code and row[index - 1] != "\\":
            return row[1:index]
    return row[1:]


def _sections(lines: Sequence[DocLine]) -> dict[str, list[DocLine]]:
    """Top-level name -> the lines of its ``ouroboros <name>`` heading's section."""
    sections: dict[str, list[DocLine]] = {}
    current: str | None = None
    current_level = 0
    for line in lines:
        heading = _heading(line)
        if heading:
            level, text = heading
            if current is not None and level <= current_level:
                current = None
            for span in _CODE_SPAN.findall(text):
                for words in invocations_in(span):
                    if words and current is None:
                        current, current_level = words[0], level
                        sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def documented_invocations(root: Any, lines: Iterable[DocLine]) -> Iterator[Invocation]:
    """Every invocation, and every options-table row as one, in ``lines``."""
    lines = list(lines)
    for line, fragment in _code_fragments(lines):
        for words in invocations_in(fragment):
            yield Invocation(line.where(), words)

    # The nearest enclosing command heading owns a table row. A sub-heading
    # that is not a command (``### Brownfield Subcommands``) keeps the
    # enclosing one; a heading naming an unregistered command owns its rows so
    # they are not misattributed (``coverage_violations`` reports the heading).
    stack: list[tuple[int, tuple[str, ...] | None]] = []
    for line in lines:
        heading = _heading(line)
        if heading:
            level, text = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _heading_command(root, text)))
            continue
        if line.in_code_block or not line.text.startswith("| `-"):
            continue
        owner = next((command for _, command in reversed(stack) if command is not None), ())
        if resolve(root, owner).path != owner:
            continue
        # One invocation per flag: in ``-o, --output TEXT`` the second flag is
        # not the value of the first.
        for span in _CODE_SPAN.findall(_first_cell(line.text)):
            for flag in _TABLE_FLAG.findall(span):
                yield Invocation(line.where(), (*owner, flag))


# --------------------------------------------------------------------------
# The checks. Each returns one ``path:line: token — remedy`` line per violation.


def coverage_violations(
    root: Any, aliases: Mapping[str, str], lines: Iterable[DocLine], doc: str
) -> list[str]:
    """Commands with no section, aliases their section omits, unregistered headings."""
    lines = list(lines)
    sections = _sections(lines)
    violations: list[str] = []

    for line in lines:
        heading = _heading(line)
        command = _heading_command(root, heading[1]) if heading else None
        if command is not None and resolve(root, command).path != command:
            shown = " ".join((PROGRAM, *command))
            violations.append(
                f"{line.where()}: `{shown}` — heading names a command that is not "
                f"registered; rename it to the command `{PROGRAM} --help` lists, or remove it"
            )

    for name, command in sorted(_subcommands(root).items()):
        if command.hidden:
            continue
        canonical = aliases.get(name)
        if canonical is None:
            if name not in sections:
                violations.append(
                    f"{doc}:1: `{PROGRAM} {name}` — registered command has no section; add "
                    f"a `## \\`{PROGRAM} {name}\\`` section written from "
                    f"`{PROGRAM} {name} --help`"
                )
            continue
        shows_alias = any(
            words[:1] == (name,)
            for _, fragment in _code_fragments(sections.get(canonical, []))
            for words in invocations_in(fragment)
        )
        if not shows_alias:
            violations.append(
                f"{doc}:1: `{PROGRAM} {name}` — alias of `{PROGRAM} {canonical}` that its "
                f"section never shows; show `{PROGRAM} {name}` in the "
                f"`{PROGRAM} {canonical}` section"
            )
    return violations


def flag_violations(root: Any, invocations: Iterable[Invocation]) -> list[str]:
    """Flags that neither the command reached nor a parent group accepts."""
    violations: list[str] = []
    for invocation in invocations:
        for flag, path in resolve(root, invocation.words).unknown:
            shown = " ".join((PROGRAM, *path))
            remedy = f"`{shown}` has no such option; use one `{shown} --help` lists, or drop it"
            owners = [
                name
                for name, sub in _subcommands(_command_at(root, path)).items()
                if flag in _option_arity(sub)
            ]
            if owners:
                remedy += f" (`{shown} {owners[0]}` accepts it: name the subcommand first)"
            violations.append(f"{invocation.where}: `{flag}` — {remedy}")
    return violations


def _report(violations: Sequence[str]) -> str:
    return f"{len(violations)} stale CLI reference(s):\n" + "\n".join(violations)


# --------------------------------------------------------------------------
# The shipped reference.


def test_cli_reference_is_a_maintained_doc() -> None:
    # If the corpus rules ever drop the reference, these checks must not pass
    # by scanning nothing.
    assert (REPO_ROOT / CLI_REFERENCE).is_file()
    assert CLI_REFERENCE in corpus_files()
    invocations = list(documented_invocations(registered_cli(), iter_lines([CLI_REFERENCE])))
    assert len(invocations) > 100


def test_every_registered_command_has_a_section() -> None:
    violations = coverage_violations(
        registered_cli(), registered_aliases(), iter_lines([CLI_REFERENCE]), CLI_REFERENCE
    )
    assert not violations, _report(violations)


def test_documented_flags_exist() -> None:
    root = registered_cli()
    violations = flag_violations(root, documented_invocations(root, iter_lines([CLI_REFERENCE])))
    assert not violations, _report(violations)


def test_real_default_subcommand_groups_dispatch() -> None:
    """Pin the dispatch this module asks the real groups for."""
    root = registered_cli()

    assert resolve(root, ["run", "seed.yaml", "--debug"]) == Resolution(("run", "workflow"), ())
    assert resolve(root, ["init", "Build an API", "--debug"]).path == ("init", "start")
    assert resolve(root, ["zcode", "--debug", "Build an API"]).path == ("zcode", "start")
    # ``run`` and ``init`` dispatch only on a positional, so the CLI rejects a
    # leading subcommand flag: ``ouroboros init --debug`` is "No such option".
    assert resolve(root, ["init", "--debug", "Build an API"]) == Resolution(
        ("init",), (("--debug", ("init",)),)
    )


# --------------------------------------------------------------------------
# The checks themselves, on a synthetic CLI and synthetic docs.


class _DefaultStartGroup(typer.core.TyperGroup):
    """Same shape as ``run`` / ``init``: a positional falls through to ``start``."""

    default_cmd_name = "start"

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [self.default_cmd_name, *args]
        return super().parse_args(ctx, args)


def _synthetic_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def main(verbose: Annotated[bool, typer.Option("--verbose")] = False) -> None: ...

    @app.command()
    def build(
        fast: Annotated[bool, typer.Option("--fast", "-f")] = False,
        out: Annotated[str, typer.Option("--out", "-o")] = "",
        cache: Annotated[bool, typer.Option("--cache/--no-cache")] = True,
    ) -> None: ...

    interview = typer.Typer(cls=_DefaultStartGroup)

    @interview.command("start")
    def start(
        context: Annotated[str, typer.Argument()] = "",
        resume: Annotated[str, typer.Option("--resume")] = "",
    ) -> None: ...

    @interview.command("list")
    def list_() -> None: ...

    app.add_typer(interview, name="init")
    app.add_typer(interview, name="interview")

    @app.command(hidden=True)
    def secret() -> None: ...

    return app


@pytest.fixture(scope="module")
def synthetic() -> tuple[Any, dict[str, str]]:
    app = _synthetic_app()
    return typer.main.get_command(app), top_level_aliases(app)


def _doc(tmp_path: Path, text: str) -> list[DocLine]:
    """Lines of ``text`` as the corpus reader yields them."""
    path = tmp_path / "doc.md"
    path.write_text(text, encoding="utf-8")
    return list(iter_lines([str(path)]))


def _flags(synthetic: tuple[Any, dict[str, str]], tmp_path: Path, text: str) -> list[str]:
    root, _ = synthetic
    return flag_violations(root, documented_invocations(root, _doc(tmp_path, text)))


def _coverage(synthetic: tuple[Any, dict[str, str]], tmp_path: Path, text: str) -> list[str]:
    root, aliases = synthetic
    return coverage_violations(root, aliases, _doc(tmp_path, text), "doc.md")


_FULL_COVERAGE = """\
## `ouroboros build`

## `ouroboros init`

`ouroboros interview` is the same command.

### `init start`
"""


def test_alias_detection_reads_the_typer_registry() -> None:
    assert top_level_aliases(_synthetic_app()) == {"interview": "init"}


def test_coverage_accepts_a_reference_with_every_command(synthetic, tmp_path) -> None:
    assert _coverage(synthetic, tmp_path, _FULL_COVERAGE) == []


def test_coverage_reports_a_command_without_a_section(synthetic, tmp_path) -> None:
    text = _FULL_COVERAGE.replace("## `ouroboros build`\n", "## Build\n\nRun `ouroboros build`.\n")
    violations = _coverage(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert violations[0].startswith(
        "doc.md:1: `ouroboros build` — registered command has no section"
    )


def test_coverage_requires_the_alias_in_the_canonical_section(synthetic, tmp_path) -> None:
    # Named, but only outside the ``init`` section: still a violation.
    text = _FULL_COVERAGE.replace("`ouroboros interview` is the same command.\n", "")
    text = text.replace("## `ouroboros build`\n", "## `ouroboros build`\n\n`ouroboros interview`\n")
    violations = _coverage(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert "`ouroboros interview` — alias of `ouroboros init`" in violations[0]


def test_coverage_skips_hidden_commands(synthetic, tmp_path) -> None:
    assert not any("secret" in v for v in _coverage(synthetic, tmp_path, _FULL_COVERAGE))


def test_coverage_reports_a_heading_for_an_unregistered_command(synthetic, tmp_path) -> None:
    text = _FULL_COVERAGE + "\n### `init frobnicate`\n\n## `ouroboros gone`\n\n## `ooo auto`\n"
    violations = _coverage(synthetic, tmp_path, text)

    assert [v.split(" — ")[0] for v in violations] == [
        f"{tmp_path / 'doc.md'}:9: `ouroboros init frobnicate`",
        f"{tmp_path / 'doc.md'}:11: `ouroboros gone`",
    ]


def test_flags_accepts_valid_invocations(synthetic, tmp_path) -> None:
    text = """\
```bash
ouroboros build --fast --out dist
ouroboros build -f -o dist --no-cache
ouroboros build --out=dist --verbose
ouroboros --verbose build
ouroboros init "Build an API" --resume abc
ouroboros init list --help
$ uv run ouroboros build --fast
```

Run `ouroboros build --out <dir>` or `OUROBOROS_X=1 ouroboros build`.
"""
    assert _flags(synthetic, tmp_path, text) == []


def test_flags_reports_each_unknown_flag_with_its_location(synthetic, tmp_path) -> None:
    text = """\
```bash
ouroboros build --fast --slow
ouroboros build --colour=red -z
```

Prose with `ouroboros init start --resume x --forget`.
"""
    violations = _flags(synthetic, tmp_path, text)
    where = tmp_path / "doc.md"

    assert [v.split(" — ")[0] for v in violations] == [
        f"{where}:2: `--slow`",
        f"{where}:3: `--colour`",
        f"{where}:3: `-z`",
        f"{where}:6: `--forget`",
    ]
    assert "`ouroboros build` has no such option" in violations[0]


def test_flags_treats_an_option_value_as_a_value(synthetic, tmp_path) -> None:
    # ``init`` is the value of ``--out``, not a subcommand, so ``--resume``
    # is still checked against ``build`` and rejected.
    violations = _flags(synthetic, tmp_path, "`ouroboros build --out init --resume x`\n")

    assert len(violations) == 1
    assert "`--resume` — `ouroboros build` has no such option" in violations[0]


def test_flags_stop_at_shell_operators_and_comments(synthetic, tmp_path) -> None:
    text = """\
```bash
ouroboros build --fast | grep --nope
ouroboros build && tool --nope
ouroboros build; tool --nope
ouroboros build > out.txt --nope
ouroboros build  # --nope is a comment
# ouroboros build --nope
ouroboros build --fast && ouroboros build --slow
```
"""
    violations = _flags(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert violations[0].split(" — ")[0].endswith(":8: `--slow`")


def test_flags_ignore_ouroboros_that_is_not_the_command(synthetic, tmp_path) -> None:
    text = "`plugin list --marketplace ouroboros --json` and `~/.ouroboros/x --nope`\n"
    assert _flags(synthetic, tmp_path, text) == []


def test_flags_follow_default_subcommand_dispatch(synthetic, tmp_path) -> None:
    # A positional reaches ``init start``; a leading flag stays at ``init``,
    # which has no ``--resume``, exactly as the CLI parses it.
    text = "`ouroboros init idea --resume x` `ouroboros init --resume x idea`\n"
    violations = _flags(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert "`--resume` — `ouroboros init` has no such option" in violations[0]
    assert "`ouroboros init start` accepts it" in violations[0]


def test_flags_read_blockquoted_fences(synthetic, tmp_path) -> None:
    text = "> **Note:**\n> ```bash\n> ouroboros build --slow\n> ```\n"
    violations = _flags(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert violations[0].split(" — ")[0].endswith(":3: `--slow`")


def test_options_tables_bind_to_the_enclosing_command(synthetic, tmp_path) -> None:
    text = """\
## Usage

| Option | Description |
|---|---|
| `--verbose` | root option |
| `--fast` | not a root option |

## `ouroboros build`

### Notes

| Option | Description |
|---|---|
| `-f, --fast` | ok |
| `--cache/--no-cache` | ok |
| `--out [dist|build]` | ok: a pipe inside code does not end the cell |
| `-o, --oops TEXT` | `-o` takes a value, but `--oops` is still checked |
| `--slow` | unknown |

### `init start`

| `--resume TEXT` | ok |
| `--fast` | belongs to build |

### `init frobnicate`

| `--anything` | under an unregistered heading: reported by coverage instead |
"""
    violations = _flags(synthetic, tmp_path, text)

    assert [v.split(" — ")[0].rsplit(":", 2)[1:] for v in violations] == [
        ["6", " `--fast`"],
        ["17", " `--oops`"],
        ["18", " `--slow`"],
        ["23", " `--fast`"],
    ]


def test_flags_follow_line_continuations(synthetic, tmp_path) -> None:
    text = "```bash\nouroboros build --fast \\\n  --out dist \\\n  --slow\n```\n"
    violations = _flags(synthetic, tmp_path, text)

    assert len(violations) == 1
    assert violations[0].split(" — ")[0].endswith(":2: `--slow`")
