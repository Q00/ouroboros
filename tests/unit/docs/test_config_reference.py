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
* **YAML examples**: every ``yaml`` block, except one under a heading that
  names another file (``credentials.yaml``, ``backend_limits.yaml``).
* **Key references**: an inline code span that is a dotted key whose first
  segment is a top-level section (``models.pin``, ``llm.backend: codex``),
  and the key given to ``ouroboros config set``. A dotted name that the source
  spells as a complete string literal (an event type such as
  ``boundary.acceptance.reconciled``) is a code identifier, not a config key.

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
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ouroboros.config.models import OuroborosConfig
from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

REFERENCE = "docs/config-reference.md"
MODELS = "src/ouroboros/config/models.py"

_KEY_TABLE_HEADERS = frozenset({"Option", "Field"})
_SECTION_TABLE_HEADER = "Section"
_CLASS_LABEL = re.compile(r"^\*\*`(?P<cls>[A-Za-z_]\w*)` fields\b")
_SINGLE_SPAN = re.compile(r"^`(?P<key>[^`]+)`$")
_INLINE_SPAN = re.compile(r"`([^`\n]+)`")
_SEGMENT = r"[A-Za-z_][\w-]*(?:\[\])?"
_DOTTED_KEY = rf"{_SEGMENT}(?:\.(?:{_SEGMENT}|\*))+"
_KEY_REFERENCE = re.compile(rf"^(?P<key>{_DOTTED_KEY})(?:\s*:\s.*|\s*:)?$")
_CONFIG_SET = re.compile(r"\bouroboros config set\s+(?P<key>[A-Za-z_][\w.*\[\]-]*)")
_FOREIGN_FILE = re.compile(r"`(?P<name>[^`]*\.ya?ml)`")
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
# Document side.
# --------------------------------------------------------------------------


def _cells(text: str) -> list[str]:
    body = text.strip()
    body = body.removeprefix("|")
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _heading(line: DocLine) -> str | None:
    match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line.text)
    return match.group(1) if match else None


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


def _yaml_blocks(rel_path: str, lines: list[DocLine]) -> Iterator[tuple[int, str, str | None]]:
    """Yield ``(first content line, text, nearest heading)`` for each yaml block.

    Fence lines are read from the file (the corpus helper does not yield them);
    lines the corpus helper skipped because of an ignore marker are blanked so
    the block keeps its line numbers.
    """
    kept = {line.lineno for line in lines}
    headings = {line.lineno: _heading(line) for line in lines if not line.in_code_block}
    raw = (REPO_ROOT / rel_path).read_text(encoding="utf-8").splitlines()
    fence = ""
    info = ""
    body: list[str] = []
    start = 0
    heading: str | None = None
    for lineno, text in enumerate(raw, start=1):
        stripped = text.lstrip()
        if not fence and headings.get(lineno):
            heading = headings[lineno]
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fence:
                fence, info, body, start = marker, stripped[3:].strip().lower(), [], lineno + 1
                continue
            if marker == fence:
                if info.split()[:1] in (["yaml"], ["yml"]):
                    yield start, "\n".join(body), heading
                fence = ""
                continue
        if fence:
            body.append(text if lineno in kept else "")


def _documents_other_file(heading: str | None) -> bool:
    names = [Path(match["name"]).name for match in _FOREIGN_FILE.finditer(heading or "")]
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
    lines = list(iter_lines([rel_path]))
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
            heading = _SINGLE_SPAN.match(table.heading or "")
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
                anchor = resolve(tree, _segments(heading["key"]))
                if anchor is None or anchor.kind not in ("model", "map"):
                    violations.append(
                        f"{table.header.where()}: {heading['key']} — the heading above this "
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
            key = _SINGLE_SPAN.match(cell)
            if key is None:
                violations.append(
                    f"{row.where()}: {cell or '(empty)'} — the first cell of a key-table row "
                    "must be one `key` code span"
                )
                continue
            for anchor in anchors:
                token = _join(anchor.path, key["key"])
                target = resolve(anchor, _segments(key["key"]))
                if target is None:
                    violations.append(
                        f"{row.where()}: {token} — documented, but {MODELS} has no such key; "
                        "remove the row or rename it to the current key "
                        f"(git log -S'{_segments(key['key'])[-1]}' -- {MODELS} finds a rename)"
                    )
                elif target.is_field:
                    if id(target) in documented:
                        violations.append(
                            f"{row.where()}: {target.path} — documented twice (also at "
                            f"{documented[id(target)].where()}); keep one row"
                        )
                    else:
                        documented[id(target)] = row

    # Key references in prose and tables.
    for line in lines:
        if line.in_code_block:
            spans: list[str] = []
        elif line.lineno in key_cells:
            spans = _INLINE_SPAN.findall("|".join(_cells(line.text)[1:]))
        else:
            spans = _INLINE_SPAN.findall(line.text)
        # token -> whether a matching source string literal excuses it
        tokens: dict[str, bool] = {}
        for span in spans:
            match = _KEY_REFERENCE.match(span.strip())
            if match and match["key"].split(".")[0] in tree.keys:
                tokens.setdefault(match["key"], True)
        for source in [line.text] if line.in_code_block else spans:
            for match in _CONFIG_SET.finditer(source):
                tokens[match["key"]] = False  # `config set` only ever takes a key
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
    for start, text, heading in _yaml_blocks(rel_path, lines):
        if _documents_other_file(heading):
            continue
        where = f"{lines[0].path if lines else rel_path}:{start}"
        try:
            document = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or type(exc).__name__
            violations.append(
                f"{where}: yaml example — does not parse ({problem}); fix the example"
            )
            continue
        for lineno, token in _yaml_unknown_keys(document, tree, start):
            violations.append(
                f"{lines[0].path}:{lineno}: {token} — YAML example key that {MODELS} does "
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
