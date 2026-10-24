"""Check the finite model-default examples in config-reference, not runtime wiring."""

from dataclasses import dataclass
import json
import re
from typing import Final, assert_never

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ouroboros.config import _model_defaults
from ouroboros.config.model_selection import SHIPPED_CONSENSUS_ROSTER

DIRECT_PATHS: Final = {
    "### Portable Task Profiles": (("llm_profiles", "deep", "providers", "claude_code", "model"),),
    "## `economics`": (("economics", "tiers", "frontier", "models", "anthropic", "model"),),
    "### Full Config Skeleton": (
        ("economics", "tiers", "frontier", "models", "anthropic", "model"),
        ("clarification", "default_model"),
        ("resilience", "wonder_model"),
        ("resilience", "reflect_model"),
        ("evaluation", "semantic_model"),
    ),
}
PROVIDER_PATHS: Final = {
    "### Portable Task Profiles": (("llm_profiles", "deep", "providers", "litellm", "model"),),
    "### Full Config Skeleton": (("consensus", "advocate_model"),),
}


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str
    blocks: tuple[str, ...]
    rows: tuple[str, ...]


def _sections(markdown: str) -> tuple[_Section, ...]:
    """Keep fences and table rows attached to their heading, including duplicates."""
    sections: list[_Section] = []
    heading, fence, language = "", "", ""
    body: list[str] = []
    blocks: list[str] = []
    rows: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        marker = re.fullmatch(r"(`{3,}|~{3,})(.*)", stripped)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                if language in {"yaml", "yml"}:
                    blocks.append("\n".join(body))
                fence = ""
            else:
                body.append(line)
        elif marker:
            fence, language = marker[1], marker[2].strip()
            body = []
        elif stripped.startswith("#"):
            sections.append(_Section(heading, tuple(blocks), tuple(rows)))
            heading, blocks, rows = stripped, [], []
        elif stripped.startswith("|"):
            rows.append(stripped)
    sections.append(_Section(heading, tuple(blocks), tuple(rows)))
    return tuple(sections)


def _nodes(node: Node | None, path: tuple[str, ...]) -> tuple[Node, ...]:
    """Select exact provider entries without losing duplicate YAML mapping keys."""
    if not path:
        return (node,) if node is not None else ()
    match node:
        case MappingNode(value=entries):
            return tuple(
                value
                for key, child in entries
                if key.tag == "tag:yaml.org,2002:str" and key.value == path[0]
                for value in _nodes(child, path[1:])
            )
        case SequenceNode(value=items):
            return tuple(
                value
                for child in items
                if _strings(_nodes(child, ("provider",))) == (path[0],)
                for value in _nodes(child, path[1:])
            )
        case Node() | None:
            return ()
        case unreachable:
            assert_never(unreachable)


def _strings(nodes: tuple[Node, ...]) -> tuple[str, ...] | None:
    values: list[str] = []
    for node in nodes:
        match node:
            case ScalarNode(tag="tag:yaml.org,2002:str", value=value):
                values.append(value)
            case Node():
                return None
            case unreachable:
                assert_never(unreachable)
    return tuple(values)


def model_default_errors(markdown: str) -> tuple[str, ...]:
    """Compare named examples and consensus table claims to current source identities."""
    errors: list[str] = []
    sections = _sections(markdown)
    for heading in (*DIRECT_PATHS, "## `consensus`"):
        matches = [section for section in sections if section.heading == heading]
        if len(matches) != 1:
            errors.append(f"MODEL_DOCUMENT: {heading}: expected one section, got {len(matches)}")
            continue
        section = matches[0]
        if heading == "## `consensus`":
            errors.extend(_consensus_table_errors(section.rows))
            continue
        if len(section.blocks) != 1:
            errors.append(
                f"MODEL_DOCUMENT: {heading}: expected one YAML example, got {len(section.blocks)}"
            )
            continue
        try:
            document = yaml.compose(section.blocks[0], Loader=yaml.SafeLoader)
        except yaml.YAMLError as error:
            errors.append(f"MODEL_DOCUMENT: {heading}: {error}")
            continue
        expectations = [
            (path, (_model_defaults.DEFAULT_OPUS_MODEL,)) for path in DIRECT_PATHS[heading]
        ]
        expectations.extend(
            (path, (_model_defaults.DEFAULT_CONSENSUS_OPUS_MODEL,))
            for path in PROVIDER_PATHS.get(heading, ())
        )
        if heading == "### Full Config Skeleton":
            expectations.append((("consensus", "models"), SHIPPED_CONSENSUS_ROSTER))
        for path, expected in expectations:
            nodes = _nodes(document, path)
            if path == ("consensus", "models"):
                match nodes:
                    case (SequenceNode(value=items),):
                        actual = _strings(tuple(items))
                    case tuple():
                        actual = None
                    case unreachable:
                        assert_never(unreachable)
            else:
                actual = _strings(nodes)
            if actual != expected:
                errors.append(
                    f"MODEL_DEFAULT: {heading}: {'.'.join(path)}: {actual!r} != {expected!r}"
                )
    return tuple(errors)


def _consensus_table_errors(rows: tuple[str, ...]) -> tuple[str, ...]:
    errors: list[str] = []
    for field in ("models", "advocate_model"):
        cells = [tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", row)[1:-1]) for row in rows]
        matches = [row for row in cells if row and row[0] == f"`{field}`"]
        if len(matches) != 1 or len(matches[0]) != 4:
            errors.append(f"MODEL_DOCUMENT: consensus.{field}: expected one four-cell table row")
            continue
        row = matches[0]
        if field == "models":
            rosters = re.findall(r"on `litellm`[^()]*\(([^()]*)\)", row[3])
            actual = tuple(re.findall(r"`([^`]+)`", rosters[0])) if len(rosters) == 1 else ()
            expected = SHIPPED_CONSENSUS_ROSTER
        else:
            actual = tuple(re.findall(r"\(`litellm`:\s*`([^`]+)`\)", row[2]))
            expected = (json.dumps(_model_defaults.DEFAULT_CONSENSUS_OPUS_MODEL),)
        if actual != expected:
            errors.append(f"MODEL_DEFAULT: consensus.{field} table: {actual!r} != {expected!r}")
    return tuple(errors)
