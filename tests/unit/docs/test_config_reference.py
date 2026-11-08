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

One tokenizer (``scan`` and ``_inline``) decides fences and code spans by
CommonMark rules, so every check reads the same document structure; the shared
corpus helper decides only which lines the ignore markers hide.

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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ouroboros.config.models import OuroborosConfig
from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

REFERENCE = "docs/config-reference.md"
MODELS = "src/ouroboros/config/models.py"

_KEY_TABLE_HEADERS = frozenset({"Option", "Field"})
_SECTION_TABLE_HEADER = "Section"
_CLASS_LABEL = re.compile(r"^\*\*`(?P<cls>[A-Za-z_]\w*)` fields\b")
_SEGMENT = r"[A-Za-z_][\w-]*(?:\[\])?"
_DOTTED_KEY = rf"{_SEGMENT}(?:\.(?:{_SEGMENT}|\*))+"
_KEY_REFERENCE = re.compile(rf"^(?P<key>{_DOTTED_KEY})(?:\s*:\s.*|\s*:)?$")
_CONFIG_SET = re.compile(r"\bouroboros\s+config\s+set\b")
_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_]*|<[^>]*>")
_SEPARATOR_ROW = re.compile(r"^\|?\s*:?-{3,}")


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
# Document side: one Markdown tokenizer for fences, inline code and prose.
# --------------------------------------------------------------------------

# A fence opener: three or more backticks or tildes (CommonMark section 4.5).
# Any indentation is accepted, as in the corpus helper, so fences nested in list
# items count. A backtick fence's info string may not contain a backtick (that
# line is inline code, not a fence).
_FENCE = re.compile(r"^\s*(?P<run>`{3,}|~{3,})(?P<info>.*)$")
_QUOTE_MARKER = re.compile(r"^ {0,3}> ?")


@dataclass
class _Block:
    """A fenced code block; ``lines`` are its content lines with quote markers removed."""

    info: str  # first word of the info string, lower-cased ("" if none)
    start: int  # line number of the first content line
    heading: str | None  # nearest heading above the opening fence
    lines: list[str] = field(default_factory=list)


def _strip_quotes(text: str, limit: int | None = None) -> tuple[int, str]:
    """Remove leading blockquote markers; return ``(depth, rest)``."""
    depth = 0
    while limit is None or depth < limit:
        match = _QUOTE_MARKER.match(text)
        if match is None:
            break
        text = text[match.end() :]
        depth += 1
    return depth, text


def _inline(text: str) -> list[tuple[bool, str]]:
    """Split one line into ``(is_code, text)`` runs (CommonMark code spans).

    A backtick string opens a span that the next backtick string of the same
    length closes; an unmatched one is literal text. A backslash escapes a
    backtick outside a span. One leading and trailing space is stripped from
    span content when both are present.
    """
    runs: list[tuple[bool, str]] = []
    prose: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            prose.append(text[index : index + 2])
            index += 2
            continue
        if char != "`":
            prose.append(char)
            index += 1
            continue
        rest = text[index:]
        width = len(rest) - len(rest.lstrip("`"))
        opener = text[index : index + width]
        closer = re.compile(rf"(?<!`){opener}(?!`)")
        found = closer.search(text, index + width)
        if found is None:
            prose.append(opener)
            index += width
            continue
        content = text[index + width : found.start()]
        if (
            len(content) > 1
            and content.startswith(" ")
            and content.endswith(" ")
            and content.strip()
        ):
            content = content[1:-1]
        if prose:
            runs.append((False, "".join(prose)))
            prose = []
        runs.append((True, content))
        index = found.end()
    if prose:
        runs.append((False, "".join(prose)))
    return runs


def _only_code_span(text: str) -> str | None:
    """The span content when ``text`` is exactly one code span, else ``None``."""
    runs = [run for run in _inline(text.strip()) if run[0] or run[1].strip()]
    return runs[0][1] if len(runs) == 1 and runs[0][0] else None


def _heading(line: DocLine) -> str | None:
    match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line.text)
    return match.group(1) if match else None


def scan(rel_path: str) -> tuple[list[DocLine], list[_Block]]:
    """Tokenize a reference into checkable lines and fenced blocks.

    Which lines count is the corpus helper's decision (ignore markers); whether
    a line is prose or code is decided here, by CommonMark fence rules: a fence
    closes on a run of the same character at least as long as the opener, so a
    shorter fence inside a longer one is content; an unclosed fence runs to the
    end of the file; a fence inside a blockquote ends with the blockquote.
    Fence lines themselves are not returned.
    """
    kept = {line.lineno for line in iter_lines([rel_path])}
    raw = (REPO_ROOT / rel_path).read_text(encoding="utf-8").splitlines()
    lines: list[DocLine] = []
    blocks: list[_Block] = []
    heading: str | None = None
    fence: tuple[str, int, int] | None = None  # (char, width, quote depth)
    for lineno, text in enumerate(raw, start=1):
        if fence is not None:
            char, width, depth = fence
            found, body = _strip_quotes(text, depth)
            if found < depth:
                fence = None  # the enclosing blockquote ended
            else:
                stripped = body.strip()
                if stripped and set(stripped) == {char} and len(stripped) >= width:
                    fence = None
                    continue
                blocks[-1].lines.append(body if lineno in kept else "")
                if lineno in kept:
                    lines.append(DocLine(rel_path, lineno, text, in_code_block=True))
                continue
        depth, body = _strip_quotes(text)
        opener = _FENCE.match(body)
        if opener and not (opener["run"][0] == "`" and "`" in opener["info"]):
            words = opener["info"].split()
            blocks.append(_Block(words[0].lower() if words else "", lineno + 1, heading))
            fence = (opener["run"][0], len(opener["run"]), depth)
            continue
        if lineno in kept:
            line = DocLine(rel_path, lineno, text, in_code_block=False)
            lines.append(line)
            title = _heading(line)
            if title is not None:
                heading = title
    return lines, blocks


def _cells(text: str) -> list[str]:
    body = text.strip()
    body = body.removeprefix("|")
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


@dataclass
class _Table:
    header: DocLine
    rows: list[DocLine]
    label: str | None  # nearest non-blank line above the header
    heading: str | None  # nearest heading above the header


def _tables(lines: list[DocLine]) -> Iterator[_Table]:
    prose = [line for line in lines if not line.in_code_block]
    heading: str | None = None
    previous: str | None = None
    index = 0
    while index < len(prose):
        line = prose[index]
        if line.text.lstrip().startswith("|"):
            block = [line]
            while (
                index + 1 < len(prose)
                and prose[index + 1].lineno == block[-1].lineno + 1
                and prose[index + 1].text.lstrip().startswith("|")
            ):
                index += 1
                block.append(prose[index])
            rows = (
                block[2:] if len(block) > 1 and _SEPARATOR_ROW.match(block[1].text.strip()) else []
            )
            yield _Table(header=block[0], rows=rows, label=previous, heading=heading)
            previous = None
        else:
            title = _heading(line)
            if title is not None:
                heading = title
            if line.text.strip():
                previous = line.text.strip()
        index += 1


def _config_set_keys(text: str) -> Iterator[str]:
    """Keys given to ``ouroboros config set`` in one run of text.

    The run is a code span, a stretch of prose between code spans, or a code
    line, so a command in a span never takes its argument from outside it.
    Placeholders (``KEY``, ``<key>``) are skipped; options are passed over.
    """
    for match in _CONFIG_SET.finditer(text):
        for word in text[match.end() :].split():
            token = word.strip("\"'`").rstrip(".,;:!?)")
            if not token or token.startswith("-"):
                continue
            if not _PLACEHOLDER.fullmatch(token):
                yield token
            break


def _documents_other_file(heading: str | None) -> bool:
    names = [
        Path(content).name
        for is_code, content in _inline(heading or "")
        if is_code and re.search(r"\.ya?ml$", content)
    ]
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
    lines, blocks = scan(rel_path)
    violations: list[str] = []
    documented: dict[int, DocLine] = {}
    table_for: dict[int, DocLine] = {}
    section_table: DocLine | None = None
    nodes_by_class: dict[str, list[KeyNode]] = {}
    for node in _model_nodes(tree):
        for model in node.models:
            nodes_by_class.setdefault(model.__name__, []).append(node)
    key_cells: set[int] = set()

    # Key tables.
    for table in _tables(lines):
        first = _cells(table.header.text)[0]
        if first == _SECTION_TABLE_HEADER:
            anchors = [tree]
            section_table = table.header
        elif first in _KEY_TABLE_HEADERS:
            label = _CLASS_LABEL.match(table.label or "")
            heading = _only_code_span(table.heading or "")
            if label:
                anchors = nodes_by_class.get(label["cls"], [])
                if not anchors:
                    violations.append(
                        f"{table.header.where()}: {label['cls']} — the label names no model "
                        f"in the config tree of {MODELS}; name the class whose fields the "
                        "table lists"
                    )
                    continue
            elif heading:
                anchor = resolve(tree, _segments(heading))
                if anchor is None or anchor.kind not in ("model", "map"):
                    violations.append(
                        f"{table.header.where()}: {heading} — the heading above this "
                        f"key table names no config section in {MODELS}; fix the heading"
                    )
                    continue
                anchors = [anchor.item if anchor.kind == "map" and anchor.item else anchor]
            else:
                violations.append(
                    f"{table.header.where()}: | {first} | — key table without a config "
                    "path; put it under a heading that is one code span (## `section`) or "
                    "label it **`ClassName` fields:**"
                )
                continue
        else:
            continue
        for anchor in anchors:
            table_for.setdefault(id(anchor), table.header)
        for row in table.rows:
            key_cells.add(row.lineno)
            cell = _cells(row.text)[0]
            key = _only_code_span(cell)
            if key is None:
                violations.append(
                    f"{row.where()}: {cell or '(empty)'} — the first cell of a key-table row "
                    "must be one `key` code span"
                )
                continue
            for anchor in anchors:
                token = _join(anchor.path, key)
                target = resolve(anchor, _segments(key))
                if target is None:
                    violations.append(
                        f"{row.where()}: {token} — documented, but {MODELS} has no such key; "
                        "remove the row or rename it to the current key "
                        f"(git log -S'{_segments(key)[-1]}' -- {MODELS} finds a rename)"
                    )
                elif target.is_field:
                    if id(target) in documented:
                        violations.append(
                            f"{row.where()}: {target.path} — documented twice (also at "
                            f"{documented[id(target)].where()}); keep one row"
                        )
                    else:
                        documented[id(target)] = row

    # Key references: dotted keys in code spans, and every `config set` argument
    # (in a span, in prose, or on a code line).
    for line in lines:
        if line.in_code_block:
            runs = [(True, line.text)]
        elif line.lineno in key_cells:
            runs = _inline("|".join(_cells(line.text)[1:]))
        else:
            runs = _inline(line.text)
        # token -> whether a matching source string literal excuses it
        tokens: dict[str, bool] = {}
        for is_code, text in runs:
            if is_code and not line.in_code_block:
                match = _KEY_REFERENCE.match(text.strip())
                if match and match["key"].split(".")[0] in tree.keys:
                    tokens.setdefault(match["key"], True)
            for key in _config_set_keys(text):
                tokens[key] = False  # `config set` only ever takes a key
        for token, may_be_identifier in tokens.items():
            if resolve(tree, _segments(token)) is not None:
                continue
            if may_be_identifier and is_code_identifier(token):
                continue
            violations.append(
                f"{line.where()}: {token} — not a config key in {MODELS}; correct the "
                "reference (an event or record name must be spelled as a string literal "
                "in src/ouroboros)"
            )

    # YAML examples of config.yaml.
    for block in blocks:
        if block.info not in ("yaml", "yml") or _documents_other_file(block.heading):
            continue
        where = f"{rel_path}:{block.start}"
        try:
            document = yaml.compose("\n".join(block.lines), Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or type(exc).__name__
            violations.append(
                f"{where}: yaml example — does not parse ({problem}); fix the example"
            )
            continue
        for lineno, token in _yaml_unknown_keys(document, tree, block.start):
            violations.append(
                f"{rel_path}:{lineno}: {token} — YAML example key that {MODELS} does "
                "not have (load_config ignores it silently); fix or remove it"
            )

    # Every field has a row.
    parents: dict[int, KeyNode] = {}
    for parent, child in iter_fields(tree):
        parents[id(child)] = parent
        if child.item is not None:
            parents[id(child.item)] = child
        if not child.needs_row or id(child) in documented:
            continue
        anchor_line = None
        cursor: KeyNode | None = parent
        while cursor is not None and anchor_line is None:
            anchor_line = table_for.get(id(cursor))
            cursor = parents.get(id(cursor))
        where = anchor_line or section_table or (lines[0] if lines else None)
        location = where.where() if where else f"{rel_path}:1"
        owner = parent.path or "the top-level Section table"
        violations.append(
            f"{location}: {child.path} — no key-table row documents it; add "
            f"`{child.path.rsplit('.', 1)[-1]}` to the key table for {owner} with its "
            f"type, default and meaning from {MODELS}"
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


def test_inline_tokenizer_runs() -> None:
    assert _inline("a `b` c ``d`e`` \\`f` g") == [
        (False, "a "),
        (True, "b"),
        (False, " c "),
        (True, "d`e"),
        (False, " \\`f` g"),
    ]
