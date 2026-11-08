"""``docs/config-reference.md`` documents exactly the keys the config models accept.

Item D6 of #2921. Policy and rationale: docs/contributing/docs-freshness.md.

Expected side, derived from code: every field reachable from ``OuroborosConfig``
(the model ``load_config`` validates ``config.yaml`` against), walked through
pydantic ``model_fields``. Optional, Union and Annotated wrappers are unwrapped.
The keys of a ``dict[str, Model]`` are chosen by the user, so they become a
``*`` segment and the nested model's fields are checked under it; the items of
a list of models become a ``[]`` segment. A field's key is the name
``model_validate`` accepts, which is its validation alias when it has one.
Fields excluded from serialization or marked deprecated are still accepted,
but need no row.

Document side, in the reference:

* **Key tables**: a table whose first header cell is ``Option`` or ``Field``.
  Its rows are keys under the config path named by the nearest heading when
  that heading is a single code span (``## `orchestrator` ``), or under every
  path of the model class named by a label line right above the table
  (``**`TierConfig` fields:**``). The ``Section`` table lists top-level keys.
* **YAML examples**: every fenced ``yaml``/``yml`` block, whatever its fence
  length or character and inside blockquotes too, except one under a heading
  that names another file (``credentials.yaml``, ``backend_limits.yaml``).
* **Key references**: an inline code span that is a dotted key whose first
  segment is a top-level section (``models.pin``, ``llm.backend: codex``),
  and the key given to ``ouroboros config set`` wherever the command appears:
  in a code span, in plain prose, or on a code line. A dotted name that the
  source spells as a complete string literal (an event type such as
  ``boundary.acceptance.reconciled``) is a code identifier, not a config key.

The page is read through ``markdown-it-py`` (CommonMark plus GFM tables, the
parser ``rich`` already depends on), so fences, indented code, tables, headings
and code spans are what a renderer makes of them, with the parser's line maps;
the shared corpus helper decides only which lines the ignore markers hide.

Checks: (a) every field has exactly one key-table row; (b) every key in a
table, a YAML example, or a key reference exists in the models. Defaults, types
and descriptions are not compared here; model-id defaults are guarded by
``tests/unit/config/test_model_document_contract.py``. Whether a documented key
is actually read by code is out of scope (#1998).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
import re
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from markdown_it import MarkdownIt, rules_inline
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ouroboros.config.models import OuroborosConfig
from tests.unit.docs._corpus import REPO_ROOT, corpus_files, iter_lines

REFERENCE = "docs/config-reference.md"
MODELS = "src/ouroboros/config/models.py"

_KEY_TABLE_HEADERS = frozenset({"Option", "Field"})
_SECTION_TABLE_HEADER = "Section"
_CLASS_LABEL = re.compile(r"^\*\*`(?P<cls>[A-Za-z_]\w*)` fields\b")
_SEGMENT = r"[A-Za-z_][\w-]*(?:\[\])?"
_DOTTED_KEY = rf"{_SEGMENT}(?:\.(?:{_SEGMENT}|\*))+"
_KEY_REFERENCE = re.compile(rf"^(?P<key>{_DOTTED_KEY})(?:\s*:\s.*|\s*:)?$")
_CONFIG_SET = re.compile(r"\bouroboros\s+config\s+set\b")
# The shape of a dotted config key; a `config set` followed by anything else is prose.
_CONFIG_KEY_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_*<>-]+)+")


# --------------------------------------------------------------------------
# Expected side: what config.yaml may contain, derived from the models.
# --------------------------------------------------------------------------


@dataclass(eq=False)
class KeyNode:
    """One place in config.yaml.

    ``path`` is canonical: ``*`` stands for a user-chosen map key and ``[]``
    for a list item. A ``model`` node has ``keys`` (every key it accepts,
    aliases included, mapped to the field's node); a ``map`` or ``list`` node
    has an ``item``; a ``value`` node holds a scalar or a scalar collection.
    """

    path: str
    kind: Literal["model", "map", "list", "value"]
    is_field: bool
    needs_row: bool
    keys: dict[str, KeyNode] = field(default_factory=dict)
    item: KeyNode | None = None
    accepts_unknown_keys: bool = False
    models: tuple[type[BaseModel], ...] = ()


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _members(annotation: Any) -> tuple[Any, ...]:
    """Unwrap Annotated, Optional and Union into the types a value may have."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _members(get_args(annotation)[0])
    if origin is Union or origin is types.UnionType:
        return tuple(
            member
            for arg in get_args(annotation)
            if arg is not type(None)
            for member in _members(arg)
        )
    return (annotation,)


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _yaml_keys(model: type[BaseModel], name: str) -> tuple[str, ...]:
    """Keys ``model_validate`` accepts for one field; the first is canonical."""
    info = model.model_fields[name]
    keys: list[str] = []
    alias = info.validation_alias
    if isinstance(alias, str):
        keys.append(alias)
    elif isinstance(alias, AliasChoices):
        # An AliasPath choice reaches into nested input; it names no key here.
        keys.extend(choice for choice in alias.choices if isinstance(choice, str))
    elif info.alias:
        keys.append(info.alias)
    config = model.model_config
    if not keys or config.get("populate_by_name") or config.get("validate_by_name"):
        keys.append(name)
    return tuple(dict.fromkeys(keys))


def _node(
    annotation: Any,
    path: str,
    *,
    is_field: bool,
    needs_row: bool,
    ancestry: frozenset[type[BaseModel]] = frozenset(),
) -> KeyNode:
    members = _members(annotation)
    models = tuple(member for member in members if _is_model(member))
    if models:
        node = KeyNode(path, "model", is_field=is_field, needs_row=needs_row, models=models)
        for model in models:
            if model in ancestry:
                raise TypeError(f"recursive config model {model.__name__} at {path}")
            node.accepts_unknown_keys |= model.model_config.get("extra") == "allow"
            for name, info in model.model_fields.items():
                keys = _yaml_keys(model, name)
                child = _node(
                    info.annotation,
                    _join(path, keys[0]),
                    is_field=True,
                    needs_row=needs_row and not info.exclude and info.deprecated is None,
                    ancestry=ancestry | {model},
                )
                for key in keys:
                    node.keys.setdefault(key, child)
        return node
    for member in members:
        origin = get_origin(member)
        args = get_args(member)
        if origin in (dict, Mapping, MutableMapping) and len(args) == 2:
            item = _node(
                args[1], f"{path}.*", is_field=False, needs_row=needs_row, ancestry=ancestry
            )
            return KeyNode(path, "map", is_field=is_field, needs_row=needs_row, item=item)
        if origin in (list, tuple, set, frozenset, Sequence) and args:
            item = _node(
                args[0], f"{path}[]", is_field=False, needs_row=needs_row, ancestry=ancestry
            )
            return KeyNode(path, "list", is_field=is_field, needs_row=needs_row, item=item)
    return KeyNode(path, "value", is_field=is_field, needs_row=needs_row)


def config_tree(root: type[BaseModel]) -> KeyNode:
    return _node(root, "", is_field=False, needs_row=True)


def iter_fields(node: KeyNode) -> Iterator[tuple[KeyNode, KeyNode]]:
    """Yield ``(parent, field)`` for every field below ``node``, once each."""
    seen: set[int] = set()
    for child in node.keys.values():
        if id(child) in seen:  # aliases of one field share its node
            continue
        seen.add(id(child))
        yield node, child
        yield from iter_fields(child)
    if node.item is not None:
        yield from iter_fields(node.item)


def _model_nodes(node: KeyNode) -> Iterator[KeyNode]:
    if node.kind == "model":
        yield node
    for _parent, child in iter_fields(node):
        if child.kind == "model":
            yield child
        if child.item is not None and child.item.kind == "model":
            yield child.item


def _segments(token: str) -> tuple[str, ...]:
    segments: list[str] = []
    for part in token.split("."):
        while part.endswith("[]"):
            part = part[:-2]
            if part:
                segments.append(part)
            segments.append("[]")
            part = ""
        if part:
            segments.append(part)
    return tuple(segments)


def resolve(node: KeyNode, segments: Sequence[str]) -> KeyNode | None:
    """The node a key path names, or ``None`` when the models do not have it."""
    current = node
    for segment in segments:
        if current.kind == "model":
            nxt = current.keys.get(segment)
            if nxt is None and current.accepts_unknown_keys:
                return KeyNode(_join(current.path, segment), "value", False, False)
        elif current.kind == "map" or (
            current.kind == "list" and (segment == "[]" or segment.isdecimal())
        ):
            nxt = current.item
        else:
            nxt = None
        if nxt is None:
            return None
        current = nxt
    return current


# --------------------------------------------------------------------------
# Document side: what a CommonMark renderer with GFM tables makes of the page.
# --------------------------------------------------------------------------


def _backticks_with_offsets(state: StateInline, silent: bool) -> bool:
    """The parser's own code-span rule, recording where each span starts and ends."""
    start, first_new = state.pos, len(state.tokens)
    if not rules_inline.backtick(state, silent):
        return False
    for token in state.tokens[first_new:]:
        if token.type == "code_inline":
            token.meta["start"], token.meta["end"] = start, state.pos
    return True


_MARKDOWN = MarkdownIt("commonmark").enable("table")
# Inline tokens carry no positions; the offsets give each span its own line.
_MARKDOWN.inline.ruler.at("backticks", _backticks_with_offsets)


@dataclass(frozen=True)
class _Run:
    """A code span, or the prose between code spans and line breaks."""

    lineno: int
    code: bool
    text: str


@dataclass(frozen=True)
class _Heading:
    lineno: int
    text: str  # plain text, code-span content included
    key: str | None  # the content when the heading is exactly one code span


@dataclass
class _Table:
    lineno: int
    header: str  # plain text of the first header cell
    rows: list[tuple[int, list[list[_Run]]]]  # (line, runs of each cell)
    label: str | None  # the last paragraph since the last heading or table
    heading: _Heading | None


@dataclass
class _Block:
    lineno: int  # first content line
    info: str  # first word of a fence's info string, lower-cased; "" otherwise
    lines: list[str]  # content, "" where an ignore marker hides the line
    heading: _Heading | None


@dataclass
class _Doc:
    runs: list[_Run] = field(default_factory=list)  # outside tables
    tables: list[_Table] = field(default_factory=list)
    blocks: list[_Block] = field(default_factory=list)


def _runs(inline: Token) -> list[_Run]:
    """The code spans and prose runs of one inline token, each with its line."""
    assert inline.map is not None
    first = inline.map[0] + 1
    line, prose, prose_line = first, "", first
    runs: list[_Run] = []
    for child in inline.children or ():
        if child.type == "text":
            if not prose:
                prose_line = line
            prose += child.content
            continue
        if child.type not in ("code_inline", "softbreak", "hardbreak"):
            continue  # emphasis and link markers; their text is in "text" children
        if prose:
            runs.append(_Run(prose_line, False, prose))
            prose = ""
        if child.type == "code_inline":
            start = first + inline.content[: child.meta["start"]].count("\n")
            runs.append(_Run(start, True, child.content))
            line = first + inline.content[: child.meta["end"]].count("\n")
        else:
            line += 1
    if prose:
        runs.append(_Run(prose_line, False, prose))
    return runs


def parse(rel_path: str) -> _Doc:
    """Tables, code blocks and inline runs of one reference, by its parse tree.

    Which lines count is the corpus helper's decision: a line its ignore
    markers hide yields no run, row or code line, but stays in the parse, so
    the page keeps the structure a reader sees.
    """
    kept = {line.lineno for line in iter_lines([rel_path])}
    tokens = _MARKDOWN.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    doc = _Doc()
    heading: _Heading | None = None
    label: str | None = None
    table: _Table | None = None
    cells: list[list[_Run]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[index + 1]
            parts = [c for c in inline.children or () if c.type != "text" or c.content.strip()]
            only_span = len(parts) == 1 and parts[0].type == "code_inline"
            text = "".join(c.content for c in inline.children or ())
            heading = _Heading(token.map[0] + 1, text, parts[0].content if only_span else None)
            label = None
        elif token.type == "table_open" and token.map is not None:
            table = _Table(token.map[0] + 1, "", [], label, heading)
            label = None
        elif token.type == "tr_open" and table is not None and token.map is not None:
            cells = []
            table.rows.append((token.map[0] + 1, cells))
        elif token.type == "inline" and table is not None:
            cells.append(_runs(token))
        elif token.type == "table_close" and table is not None:
            header, *rows = table.rows
            table.header = "".join(run.text for run in header[1][0]).strip() if header[1] else ""
            table.rows = [row for row in rows if row[0] in kept]
            if table.lineno in kept:
                doc.tables.append(table)
            table = None
        elif token.type == "inline":
            doc.runs.extend(run for run in _runs(token) if run.lineno in kept)
            if tokens[index - 1].type == "paragraph_open":
                label = token.content.strip()
        elif token.type in ("fence", "code_block") and token.map is not None:
            first = token.map[0] + (2 if token.type == "fence" else 1)
            body = token.content.splitlines()
            lines = [text if first + k in kept else "" for k, text in enumerate(body)]
            words = token.info.split() if token.type == "fence" else []
            doc.blocks.append(_Block(first, words[0].lower() if words else "", lines, heading))
    return doc


def _config_set_keys(text: str) -> Iterator[str]:
    """Keys given to ``ouroboros config set`` in one run of text.

    The run is a code span, a stretch of prose, or a code line, so a command in
    a span never takes its argument from outside it. Only an argument shaped
    like a dotted config key counts; any other next word (``to``, ``KEY``,
    ``<key>``, ``--help``) is prose and names no key.
    """
    for match in _CONFIG_SET.finditer(text):
        words = text[match.end() :].split()
        token = words[0].strip("\"'`").rstrip(".,;:!?)") if words else ""
        if _CONFIG_KEY_SHAPE.fullmatch(token):
            yield token


def _documents_other_file(heading: _Heading | None) -> bool:
    """Whether the heading above a block names a YAML file other than config.yaml."""
    names = (
        [Path(name).name for name in re.findall(r"[\w.~/-]+\.ya?ml\b", heading.text)]
        if heading
        else []
    )
    return bool(names) and "config.yaml" not in names


# --------------------------------------------------------------------------
# The check.
# --------------------------------------------------------------------------


@cache
def _source_text() -> str:
    root = REPO_ROOT / "src" / "ouroboros"
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py")))


def spelled_in_source(token: str) -> bool:
    """Whether ``src/ouroboros`` has ``token`` as a complete string literal."""
    return re.search(rf"""(["']){re.escape(token)}\1""", _source_text()) is not None


def check_reference(
    rel_path: str,
    root: type[BaseModel],
    *,
    is_code_identifier: Callable[[str], bool] = spelled_in_source,
) -> list[str]:
    """Every violation in one reference, as ``path:line: token — remedy`` lines."""
    tree = config_tree(root)
    doc = parse(rel_path)
    violations: list[str] = []
    documented: dict[int, str] = {}
    table_for: dict[int, str] = {}
    section_table: str | None = None
    nodes_by_class: dict[str, list[KeyNode]] = {}
    for node in _model_nodes(tree):
        for model in node.models:
            nodes_by_class.setdefault(model.__name__, []).append(node)
    reference_runs = list(doc.runs)

    # Key tables.
    for table in doc.tables:
        where = f"{rel_path}:{table.lineno}"
        anchors: list[KeyNode]
        if table.header == _SECTION_TABLE_HEADER:
            anchors = [tree]
            section_table = where
        elif table.header in _KEY_TABLE_HEADERS:
            label = _CLASS_LABEL.match(table.label or "")
            heading_key = table.heading.key if table.heading else None
            if label:
                anchors = nodes_by_class.get(label["cls"], [])
                if not anchors:
                    violations.append(
                        f"{where}: {label['cls']} — the label names no model in the config "
                        f"tree of {MODELS}; name the class whose fields the table lists"
                    )
                    continue
            elif heading_key:
                anchor = resolve(tree, _segments(heading_key))
                if anchor is None or anchor.kind not in ("model", "map"):
                    violations.append(
                        f"{where}: {heading_key} — the heading above this key table names no "
                        f"config section in {MODELS}; fix the heading"
                    )
                    continue
                anchors = [anchor.item if anchor.kind == "map" and anchor.item else anchor]
            else:
                violations.append(
                    f"{where}: | {table.header} | — key table without a config path; put it "
                    "under a heading that is one code span (## `section`) or label it "
                    "**`ClassName` fields:**"
                )
                continue
        else:
            reference_runs += [run for _, cells in table.rows for cell in cells for run in cell]
            continue
        for anchor in anchors:
            table_for.setdefault(id(anchor), where)
        for lineno, cells in table.rows:
            row = f"{rel_path}:{lineno}"
            reference_runs += [run for cell in cells[1:] for run in cell]
            first = [run for run in cells[0] if run.code or run.text.strip()] if cells else []
            if len(first) != 1 or not first[0].code:
                shown = "".join(run.text for run in first) or "(empty)"
                violations.append(
                    f"{row}: {shown} — the first cell of a key-table row must be one `key` "
                    "code span"
                )
                continue
            key = first[0].text
            for anchor in anchors:
                target = resolve(anchor, _segments(key))
                if target is None:
                    violations.append(
                        f"{row}: {_join(anchor.path, key)} — documented, but {MODELS} has no "
                        "such key; remove the row or rename it to the current key "
                        f"(git log -S'{_segments(key)[-1]}' -- {MODELS} finds a rename)"
                    )
                elif target.is_field:
                    if id(target) in documented:
                        violations.append(
                            f"{row}: {target.path} — documented twice (also at "
                            f"{documented[id(target)]}); keep one row"
                        )
                    else:
                        documented[id(target)] = row

    # Key references: dotted keys in code spans, and every `config set` argument
    # in a span, in prose, or on a code line. Value: may a source literal excuse it?
    found: dict[tuple[int, str], bool] = {}
    for run in reference_runs:
        if run.code:
            match = _KEY_REFERENCE.match(run.text.strip())
            if match and match["key"].split(".")[0] in tree.keys:
                found.setdefault((run.lineno, match["key"]), True)
        for key in _config_set_keys(run.text):
            found[(run.lineno, key)] = False
    for block in doc.blocks:
        for offset, text in enumerate(block.lines):
            for key in _config_set_keys(text):
                found[(block.lineno + offset, key)] = False
    for (lineno, token), may_be_identifier in sorted(found.items()):
        if resolve(tree, _segments(token)) is not None:
            continue
        if may_be_identifier and is_code_identifier(token):
            continue
        violations.append(
            f"{rel_path}:{lineno}: {token} — not a config key in {MODELS}; correct the "
            "reference (an event or record name must be spelled as a string literal in "
            "src/ouroboros)"
        )

    # YAML examples of config.yaml.
    for block in doc.blocks:
        if block.info not in ("yaml", "yml") or _documents_other_file(block.heading):
            continue
        try:
            document = yaml.compose("\n".join(block.lines), Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or type(exc).__name__
            violations.append(
                f"{rel_path}:{block.lineno}: yaml example — does not parse ({problem}); fix "
                "the example"
            )
            continue
        for lineno, token in _yaml_unknown_keys(document, tree, block.lineno):
            violations.append(
                f"{rel_path}:{lineno}: {token} — YAML example key that {MODELS} does not "
                "have (load_config ignores it silently); fix or remove it"
            )

    # Every field has a row.
    parents: dict[int, KeyNode] = {}
    for parent, child in iter_fields(tree):
        parents[id(child)] = parent
        if child.item is not None:
            parents[id(child.item)] = child
        if not child.needs_row or id(child) in documented:
            continue
        location = None
        cursor: KeyNode | None = parent
        while cursor is not None and location is None:
            location = table_for.get(id(cursor))
            cursor = parents.get(id(cursor))
        owner = parent.path or "the top-level Section table"
        violations.append(
            f"{location or section_table or f'{rel_path}:1'}: {child.path} — no key-table "
            f"row documents it; add `{child.path.rsplit('.', 1)[-1]}` to the key table for "
            f"{owner} with its type, default and meaning from {MODELS}"
        )
    return violations


def _yaml_unknown_keys(node: Node | None, tree: KeyNode, start: int) -> Iterator[tuple[int, str]]:
    def walk(ynode: Node, knode: KeyNode) -> Iterator[tuple[int, str]]:
        if knode.kind == "model" and isinstance(ynode, MappingNode):
            for key_node, value in ynode.value:
                key = key_node.value if isinstance(key_node, ScalarNode) else None
                child = knode.keys.get(key) if isinstance(key, str) else None
                if child is not None:
                    yield from walk(value, child)
                elif not knode.accepts_unknown_keys:
                    yield start + key_node.start_mark.line, _join(knode.path, str(key))
        elif knode.item is not None and isinstance(ynode, MappingNode) and knode.kind == "map":
            for _key, value in ynode.value:
                yield from walk(value, knode.item)
        elif knode.item is not None and isinstance(ynode, SequenceNode) and knode.kind == "list":
            for value in ynode.value:
                yield from walk(value, knode.item)

    if node is not None:
        yield from walk(node, tree)


# --------------------------------------------------------------------------
# The real reference.
# --------------------------------------------------------------------------


def test_reference_is_a_maintained_doc() -> None:
    assert REFERENCE in corpus_files()


def test_config_reference_matches_config_models() -> None:
    violations = check_reference(REFERENCE, OuroborosConfig)
    assert not violations, "\n" + "\n".join(violations)


def test_walk_reaches_every_top_level_section_of_the_real_models() -> None:
    tree = config_tree(OuroborosConfig)
    walked = {child.path for parent, child in iter_fields(tree) if parent is tree}
    assert walked == set(OuroborosConfig.model_fields)


# --------------------------------------------------------------------------
# Meta-tests: synthetic models and documents.
# --------------------------------------------------------------------------


class _Tier(BaseModel):
    weight: int = 1


class _Leaf(BaseModel):
    size: int = 1
    legacy: int = Field(default=0, deprecated="use size")
    internal: int = Field(default=0, exclude=True)


class _Section(BaseModel):
    name: str = "x"
    renamed: str = Field(default="", validation_alias="yaml-name")
    tiers: dict[str, _Tier] = Field(default_factory=dict)
    items: list[_Tier] = Field(default_factory=list)
    maybe: _Leaf | None = None


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")

    known: bool = True


class _Root(BaseModel):
    section: _Section = Field(default_factory=_Section)
    labels: dict[str, str] = Field(default_factory=dict)
    open: _Open = Field(default_factory=_Open)


_VALID = """\
# Synthetic reference

| Section | Class | Purpose |
|---------|-------|---------|
| `section` | `_Section` | A section |
| `labels` | `dict[str, str]` | User labels |
| `open` | `_Open` | Accepts anything |

## `section`

```yaml
section:
  name: x
  yaml-name: y
  tiers:
    gold: {weight: 2}
  items:
    - weight: 3
  maybe:
    size: 4
labels:
  anything: goes
open:
  known: true
  whatever: 1
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `name` | `str` | `"x"` | Uses `section.tiers.gold.weight` and `section.items[].weight`. |
| `yaml-name` | `str` | `""` | Run `ouroboros config set section.name y`. |
| `tiers` | `dict` | `{}` | Map. |
| `items` | `list` | `[]` | List. |
| `maybe` | `object` | `null` | Nested. |

**`_Tier` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `weight` | `int` | Weight. |

**`_Leaf` fields** (the value of `section.maybe`):

| Field | Type | Description |
|-------|------|-------------|
| `size` | `int` | Size. |

## `open`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `known` | `bool` | `true` | Known. |

## `other.yaml`

```yaml
not_a_section: true
```
"""


def _check(tmp_path: Path, text: str, **kwargs: Any) -> list[str]:
    document = tmp_path / "reference.md"
    document.write_text(text, encoding="utf-8")
    kwargs.setdefault("is_code_identifier", lambda _token: False)
    return check_reference(str(document), _Root, **kwargs)


def _lineno(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.splitlines(), 1) if needle in line)


def test_walk_derives_wildcards_list_items_aliases_and_exemptions() -> None:
    tree = config_tree(_Root)
    fields = {child.path: child for _parent, child in iter_fields(tree)}
    assert set(fields) == {
        "section",
        "section.name",
        "section.yaml-name",
        "section.tiers",
        "section.tiers.*.weight",
        "section.items",
        "section.items[].weight",
        "section.maybe",
        "section.maybe.size",
        "section.maybe.legacy",
        "section.maybe.internal",
        "labels",
        "open",
        "open.known",
    }
    assert not fields["section.maybe.legacy"].needs_row
    assert not fields["section.maybe.internal"].needs_row
    assert resolve(tree, _segments("section.renamed")) is None  # only the alias validates
    assert resolve(tree, _segments("labels.anything")) is not None
    assert resolve(tree, _segments("open.unknown")) is not None
    assert resolve(tree, _segments("section.name.deeper")) is None


def test_valid_reference_reports_nothing(tmp_path: Path) -> None:
    assert _check(tmp_path, _VALID) == []


def test_missing_row_is_reported_at_its_table(tmp_path: Path) -> None:
    # _Tier is both a map value and a list item, so its one table documents both.
    text = _VALID.replace("| `weight` | `int` | Weight. |\n", "")
    violations = _check(tmp_path, text)
    line = _lineno(text, "| Field | Type | Description |")
    assert violations == [
        f"{tmp_path / 'reference.md'}:{line}: {path}.weight — no key-table row documents "
        f"it; add `weight` to the key table for {path} with its type, default and meaning "
        f"from {MODELS}"
        for path in ("section.tiers.*", "section.items[]")
    ]


def test_missing_section_row_is_reported(tmp_path: Path) -> None:
    text = _VALID.replace("| `labels` | `dict[str, str]` | User labels |\n", "")
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and ": labels — no key-table row" in violations[0]


def test_stale_row_is_reported(tmp_path: Path) -> None:
    text = _VALID.replace("| `tiers` |", "| `old_tiers` |")
    violations = _check(tmp_path, text)
    line = _lineno(text, "| `old_tiers` |")
    assert f"reference.md:{line}: section.old_tiers — documented, but" in violations[0]
    assert any("section.tiers — no key-table row" in item for item in violations)


def test_field_name_is_stale_when_only_its_alias_validates(tmp_path: Path) -> None:
    violations = _check(tmp_path, _VALID.replace("| `yaml-name` |", "| `renamed` |"))
    assert any("section.renamed — documented, but" in item for item in violations)


def test_duplicate_row_is_reported(tmp_path: Path) -> None:
    row = '| `name` | `str` | `"x"` | Dup. |\n'
    text = _VALID.replace("| `tiers` |", row + "| `tiers` |")
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and "section.name — documented twice" in violations[0]


def test_stale_yaml_key_is_reported_and_other_files_are_skipped(tmp_path: Path) -> None:
    text = _VALID.replace("    size: 4", "    size: 4\n    sise: 5")
    violations = _check(tmp_path, text)
    line = _lineno(text, "sise: 5")
    assert violations == [
        f"{tmp_path / 'reference.md'}:{line}: section.maybe.sise — YAML example key that "
        f"{MODELS} does not have (load_config ignores it silently); fix or remove it"
    ]


def test_yaml_that_does_not_parse_is_reported(tmp_path: Path) -> None:
    violations = _check(tmp_path, _VALID.replace("  name: x\n", "  name: [x\n"))
    assert len(violations) == 1 and "yaml example — does not parse" in violations[0]


def test_stale_key_reference_is_reported(tmp_path: Path) -> None:
    text = _VALID.replace("`section.items[].weight`", "`section.item.weight`")
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and ": section.item.weight — not a config key" in violations[0]


def test_config_set_argument_is_checked(tmp_path: Path) -> None:
    text = _VALID.replace("config set section.name", "config set section.nme")
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and ": section.nme — not a config key" in violations[0]
    # A source literal never excuses a `config set` argument: it only takes keys.
    assert _check(tmp_path, text, is_code_identifier=lambda _token: True) == violations


def test_config_set_in_a_code_block_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\n```bash\nouroboros config set section.tiers.gold.wieght 2\n```\n"
    violations = _check(tmp_path, text)
    line = _lineno(text, "wieght")
    assert violations == [
        f"{tmp_path / 'reference.md'}:{line}: section.tiers.gold.wieght — not a config key "
        f"in {MODELS}; correct the reference (an event or record name must be spelled as a "
        "string literal in src/ouroboros)"
    ]


def test_code_identifier_is_not_a_stale_key(tmp_path: Path) -> None:
    text = _VALID.replace("| Map. |", "| Emits `section.tiers_changed`. |")
    assert _check(tmp_path, text, is_code_identifier={"section.tiers_changed"}.__contains__) == []
    assert len(_check(tmp_path, text)) == 1


def test_real_source_literals_count_as_code_identifiers() -> None:
    assert spelled_in_source("boundary.acceptance.reconciled")
    assert not spelled_in_source("boundary.no_such_record_name")


def test_unanchored_table_and_unknown_class_label_are_reported(tmp_path: Path) -> None:
    text = _VALID.replace("## `open`", "## Open things").replace(
        "**`_Leaf` fields**", "**`_Missing` fields**"
    )
    violations = _check(tmp_path, text)
    assert any("| Option | — key table without a config path" in item for item in violations)
    assert any(": _Missing — the label names no model" in item for item in violations)


def test_ignore_marker_skips_a_deliberate_counter_example(tmp_path: Path) -> None:
    line = "There is no `section.nothing` key. <!-- docs-drift: ignore -->\n"
    assert _check(tmp_path, _VALID + line) == []
    assert len(_check(tmp_path, _VALID + line.replace(" <!-- docs-drift: ignore -->", ""))) == 1


def test_ignore_marker_inside_a_yaml_example_skips_only_that_line(tmp_path: Path) -> None:
    text = _VALID.replace("    size: 4", "    size: 4\n    sise: 5  # <!-- docs-drift: ignore -->")
    assert _check(tmp_path, text) == []


def test_yaml_under_a_config_yaml_heading_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\n### `~/.ouroboros/config.yaml`\n\n```yaml\nsection:\n  nmae: x\n```\n"
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and ": section.nmae — YAML example key" in violations[0]


# The scanner reads every form CommonMark gives these inputs: a `config set`
# command in prose as well as in code, and fences of any length, character, or
# blockquote depth. Each test plants one stale key in one form.


def _violation_lines(tmp_path: Path, text: str) -> list[int]:
    return [int(item.split(":")[1]) for item in _check(tmp_path, text)]


def test_config_set_in_prose_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\nRun ouroboros config set section.nme y.\n"
    violations = _check(tmp_path, text)
    assert len(violations) == 1 and ": section.nme — not a config key" in violations[0]
    assert _violation_lines(tmp_path, text) == [_lineno(text, "Run ouroboros")]


@pytest.mark.parametrize(
    "line",
    [
        "Use `ouroboros config set` to change a key.",  # the span holds no argument
        "Run `ouroboros config set KEY VALUE`.",
        "Run `ouroboros config set <key> <value>`.",
        "Run ouroboros config set section.name, then restart.",
        "Run `ouroboros config set --help`.",
    ],
)
def test_config_set_forms_that_name_no_stale_key(tmp_path: Path, line: str) -> None:
    assert _check(tmp_path, _VALID + "\n" + line + "\n") == []


def test_quoted_config_set_argument_is_checked(tmp_path: Path) -> None:
    text = _VALID + '\nRun `ouroboros config set "section.nme" y`.\n'
    assert _violation_lines(tmp_path, text) == [_lineno(text, '"section.nme"')]


@pytest.mark.parametrize(
    ("opener", "closer"),
    [("````yaml", "````"), ("~~~yaml", "~~~"), ("`````yml title=x", "``````")],
)
def test_yaml_fence_of_any_length_or_character_is_checked(
    tmp_path: Path, opener: str, closer: str
) -> None:
    text = _VALID + f"\n### Examples\n\n{opener}\nsection: {{nmae: x}}\n{closer}\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "nmae")]


def test_a_shorter_fence_inside_a_longer_one_is_content(tmp_path: Path) -> None:
    # The ``` lines belong to a block scalar; closing the block on them would
    # drop the stale key below them.
    text = _VALID + (
        "\n### Examples\n\n````yaml\nsection:\n  name: |\n    ```\n    literal\n    ```\n"
        "  nmae: x\n````\n"
    )
    assert _violation_lines(tmp_path, text) == [_lineno(text, "nmae")]


def test_a_yaml_example_inside_a_markdown_example_is_not_checked(tmp_path: Path) -> None:
    text = _VALID + (
        "\n### Examples\n\n````markdown\n```yaml\nsection: {nmae: x}\n```\n````\n\n"
        "Then `section.nope`.\n"
    )
    assert _violation_lines(tmp_path, text) == [_lineno(text, "section.nope")]


def test_yaml_fence_in_a_blockquote_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\n### Examples\n\n> ```yaml\n> section:\n>   nmae: x\n> ```\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "nmae")]


def test_blockquote_end_closes_its_fence(tmp_path: Path) -> None:
    text = _VALID + "\n### Examples\n\n> ```yaml\n> section: {}\n\nThen `section.nope`.\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "section.nope")]


def test_unclosed_yaml_fence_runs_to_the_end(tmp_path: Path) -> None:
    text = _VALID + "\n### Examples\n\n```yaml\nsection:\n  nmae: x\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "nmae")]


@pytest.mark.parametrize("span", ["``section.nope``", "`` section.nope ``", "```section.nope```"])
def test_code_spans_of_any_width_are_key_references(tmp_path: Path, span: str) -> None:
    # A one-line ```...``` is inline code, not a fence: the rest of the file stays prose.
    text = _VALID + f"\nSet {span} here.\n\nAnd `section.nada`.\n"
    assert _violation_lines(tmp_path, text) == [
        _lineno(text, "Set "),
        _lineno(text, "section.nada"),
    ]


# Review round 2: forms that are valid inputs and name no stale key. The page is
# read by its parse tree, so none of them may be reported.


def test_config_set_followed_by_prose_names_no_key(tmp_path: Path) -> None:
    assert _check(tmp_path, _VALID + "\nRun ouroboros config set to change a key.\n") == []


def test_yaml_under_a_plain_heading_naming_another_file_is_skipped(tmp_path: Path) -> None:
    text = _VALID + "\n## credentials.yaml\n\n```yaml\nproviders:\n  openai: {api_key: x}\n```\n"
    assert _check(tmp_path, text) == []


def test_indented_markdown_example_is_code_not_a_fence(tmp_path: Path) -> None:
    text = _VALID + (
        "\n### Examples\n\nA reference page may show:\n\n"
        "    ```yaml\n    section: {nmae: x}\n    ```\n"
    )
    assert _check(tmp_path, text) == []


def test_yaml_fence_in_a_list_item_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\n### Examples\n\n- Set it:\n\n  ```yaml\n  section: {nmae: x}\n  ```\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "nmae")]


def test_config_set_in_indented_code_is_checked(tmp_path: Path) -> None:
    text = _VALID + "\n### Examples\n\n    ouroboros config set section.nme y\n"
    assert _violation_lines(tmp_path, text) == [_lineno(text, "section.nme")]
