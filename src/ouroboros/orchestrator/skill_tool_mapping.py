"""Skill-to-MCP-tool mappings derived from packaged skill frontmatter.

This module is intentionally separate from MCP tool descriptor definitions.
Skill dispatch mappings answer "which tool does this skill invoke?", while
tool descriptors answer "what is the MCP handler schema?". Keeping those
questions apart lets capability modeling verify skill usage without consulting
``get_ouroboros_tools()`` or descriptor metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class SkillToolMapping:
    """Frontmatter-backed mapping from one packaged skill to one MCP tool."""

    skill_name: str
    mcp_tool: str
    skill_path: str
    mcp_args: Mapping[str, Any]
    context_keys: tuple[str, ...]


def _packaged_skills_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "skills"


_TOOL_LINE_RE = re.compile(r"^\s*Tool:\s*`?(ouroboros_[A-Za-z0-9_]+)`?\s*$")
_ARGUMENTS_LINE_RE = re.compile(r"^(?P<indent>\s*)Arguments:\s*(?:#.*)?$")
_ARGUMENT_KEY_RE = re.compile(
    r"^(?P<indent>\s+)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<value>.*)$"
)
_INLINE_TOOL_CALL_RE = re.compile(r"\b(ouroboros_[A-Za-z0-9_]+)\(([^)]*)\)")
_INLINE_KWARG_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=")
_CONTEXTISH_WORD_RE = re.compile(
    r"\b(answer|artifact|context|cursor|cwd|goal|id|job|lineage|output|question|"
    r"response|seed|session)\b",
    re.IGNORECASE,
)


def _split_frontmatter_and_body(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text

    frontmatter_lines: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(frontmatter_lines), "\n".join(lines[index + 1 :])
        frontmatter_lines.append(line)
    return "", text


def _read_frontmatter(skill_path: Path) -> Mapping[str, Any]:
    text = skill_path.read_text(encoding="utf-8")
    frontmatter_text, _body = _split_frontmatter_and_body(text)
    if not frontmatter_text:
        return {}

    raw = yaml.safe_load(frontmatter_text)
    return raw if isinstance(raw, Mapping) else {}


def _ordered_merge_context_key(
    context_keys_by_tool: dict[str, list[str]],
    tool_name: str,
    key: str,
) -> None:
    normalized_key = key.strip()
    if not normalized_key:
        return
    existing = context_keys_by_tool.setdefault(tool_name, [])
    if normalized_key not in existing:
        existing.append(normalized_key)


def merge_tool_context_keys(
    frontmatter_context_keys: tuple[str, ...] = (),
    body_context_keys: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Merge frontmatter and body context keys for one MCP tool.

    Frontmatter is the skill dispatch contract, so it wins ordering. Body
    references are appended only when they add new runtime context keys. Empty
    and whitespace-only keys are discarded, and valid keys are stripped.
    """

    merged_by_tool: dict[str, list[str]] = {"": []}
    for key in (*frontmatter_context_keys, *body_context_keys):
        _ordered_merge_context_key(merged_by_tool, "", key)
    return tuple(merged_by_tool[""])


def _looks_like_context_argument(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if stripped in {"|", ">"}:
        return True
    if stripped == "...":
        return True
    if "$" in stripped:
        return True
    if "<" in stripped and ">" in stripped:
        return True
    if stripped.startswith(("response.", "meta.", "result.")):
        return True
    if stripped.startswith(("'", '"', "[", "{")):
        return False
    if stripped[0].isdigit():
        return False
    if stripped.lower().startswith(("true", "false", "null", "none")):
        return False
    return bool(_CONTEXTISH_WORD_RE.search(stripped)) and not stripped.startswith(
        ("'", '"')
    )


def _collect_tool_block_context_keys(
    lines: list[str],
    start_index: int,
) -> tuple[str, tuple[str, ...], int]:
    match = _TOOL_LINE_RE.match(lines[start_index])
    if match is None:
        return "", (), start_index + 1
    tool_name = match.group(1)

    index = start_index + 1
    while index < len(lines):
        arguments_match = _ARGUMENTS_LINE_RE.match(lines[index])
        if arguments_match is not None:
            break
        if _TOOL_LINE_RE.match(lines[index]) is not None:
            return tool_name, (), index
        index += 1
    if index >= len(lines):
        return tool_name, (), index

    arguments_indent = len(arguments_match.group("indent"))
    key_indent: int | None = None
    context_keys: list[str] = []
    index += 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _TOOL_LINE_RE.match(line) is not None:
            break

        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent <= arguments_indent:
            break

        key_match = _ARGUMENT_KEY_RE.match(line)
        if key_match is None:
            index += 1
            continue

        current_key_indent = len(key_match.group("indent"))
        if key_indent is None:
            key_indent = current_key_indent
        if current_key_indent == key_indent and _looks_like_context_argument(
            key_match.group("value")
        ):
            key = key_match.group("key")
            if key not in context_keys:
                context_keys.append(key)
        index += 1

    return tool_name, tuple(context_keys), index


def _split_inline_call_arguments(raw_arguments: str) -> tuple[str, ...]:
    arguments: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for index, char in enumerate(raw_arguments):
        if quote is not None:
            if char == quote and raw_arguments[index - 1 : index] != "\\":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            continue
        if char == "," and depth == 0:
            arguments.append(raw_arguments[start:index].strip())
            start = index + 1
    tail = raw_arguments[start:].strip()
    if tail:
        arguments.append(tail)
    return tuple(arguments)


def _extract_inline_call_context_keys(raw_arguments: str) -> tuple[str, ...]:
    context_keys: list[str] = []
    for raw_argument in _split_inline_call_arguments(raw_arguments):
        if not raw_argument:
            continue
        if "=" in raw_argument:
            keyword_match = _INLINE_KWARG_RE.match(raw_argument)
            if keyword_match is None:
                continue
            key = keyword_match.group(1)
            value = raw_argument[raw_argument.index("=") + 1 :]
            if _looks_like_context_argument(value) and key not in context_keys:
                context_keys.append(key)
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw_argument):
            context_keys.append(raw_argument)
    return tuple(context_keys)


def extract_skill_body_tool_context_keys(skill_body: str) -> Mapping[str, tuple[str, ...]]:
    """Return tool-specific context keys referenced in a skill body.

    The extraction is intentionally syntax-based. It reads the explicit MCP
    invocation formats used in packaged skills: ``Tool: ouroboros_*`` blocks
    followed by an ``Arguments:`` mapping, plus compact inline examples such as
    ``ouroboros_job_wait(job_id, cursor, timeout_seconds=120)``. Literal defaults
    are ignored; placeholder, variable, and block-scalar values are treated as
    runtime context keys.
    """

    context_keys_by_tool: dict[str, list[str]] = {}
    lines = skill_body.splitlines()
    index = 0
    while index < len(lines):
        if _TOOL_LINE_RE.match(lines[index]) is not None:
            tool_name, context_keys, next_index = _collect_tool_block_context_keys(
                lines,
                index,
            )
            for key in context_keys:
                _ordered_merge_context_key(context_keys_by_tool, tool_name, key)
            index = max(next_index, index + 1)
            continue

        for inline_match in _INLINE_TOOL_CALL_RE.finditer(lines[index]):
            tool_name = inline_match.group(1)
            for key in _extract_inline_call_context_keys(inline_match.group(2)):
                _ordered_merge_context_key(context_keys_by_tool, tool_name, key)
        index += 1

    return {
        tool_name: tuple(context_keys)
        for tool_name, context_keys in context_keys_by_tool.items()
    }


def extract_skill_frontmatter_context_keys(
    frontmatter: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return ordered MCP argument keys declared by skill frontmatter.

    The frontmatter ``mcp_args`` mapping describes which runtime context values
    a skill forwards to its tool-specific MCP input. The mapping keys are the
    tool input/context keys capability modeling needs; placeholder values such
    as ``$1`` or ``$CWD`` are runtime binding expressions, not capability keys.
    """

    raw_mcp_args = frontmatter.get("mcp_args")
    if not isinstance(raw_mcp_args, Mapping):
        return ()
    return tuple(
        key.strip()
        for key in raw_mcp_args
        if isinstance(key, str) and key.strip()
    )


def discover_skill_tool_mappings(
    skills_dir: str | Path | None = None,
) -> tuple[SkillToolMapping, ...]:
    """Discover skill-to-tool mappings from skill frontmatter only.

    The discovery path deliberately does not import or call MCP tool definition
    factories. Unknown or temporarily unavailable tool descriptors should not
    affect the recorded skill usage contract.
    """

    root = Path(skills_dir).expanduser() if skills_dir is not None else _packaged_skills_dir()
    mappings: list[SkillToolMapping] = []
    for skill_path in sorted(root.glob("*/SKILL.md")):
        frontmatter = _read_frontmatter(skill_path)
        skill_name = frontmatter.get("name")
        mcp_tool = frontmatter.get("mcp_tool")
        if not isinstance(skill_name, str) or not skill_name.strip():
            continue
        if not isinstance(mcp_tool, str) or not mcp_tool.strip():
            continue
        raw_mcp_args = frontmatter.get("mcp_args")
        mcp_args = dict(raw_mcp_args) if isinstance(raw_mcp_args, Mapping) else {}
        mappings.append(
            SkillToolMapping(
                skill_name=skill_name.strip(),
                mcp_tool=mcp_tool.strip(),
                skill_path=str(skill_path.relative_to(root.parent)),
                mcp_args=mcp_args,
                context_keys=extract_skill_frontmatter_context_keys(frontmatter),
            )
        )
    return tuple(mappings)


def discover_skill_body_context_keys(
    skills_dir: str | Path | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Discover tool context keys from packaged skill body instructions."""

    root = Path(skills_dir).expanduser() if skills_dir is not None else _packaged_skills_dir()
    context_keys_by_tool: dict[str, list[str]] = {}
    for skill_path in sorted(root.glob("*/SKILL.md")):
        _frontmatter, body = _split_frontmatter_and_body(
            skill_path.read_text(encoding="utf-8")
        )
        for tool_name, context_keys in extract_skill_body_tool_context_keys(body).items():
            for key in context_keys:
                _ordered_merge_context_key(context_keys_by_tool, tool_name, key)
    return {
        tool_name: tuple(context_keys)
        for tool_name, context_keys in context_keys_by_tool.items()
    }


@lru_cache(maxsize=1)
def get_packaged_skill_body_context_keys() -> Mapping[str, tuple[str, ...]]:
    """Return cached packaged skill body context usage by MCP tool."""

    return discover_skill_body_context_keys()


@lru_cache(maxsize=1)
def get_packaged_skill_tool_mappings() -> tuple[SkillToolMapping, ...]:
    """Return cached packaged skill mappings for capability verification."""

    return discover_skill_tool_mappings()


def skill_tool_mapping_by_skill(
    mappings: tuple[SkillToolMapping, ...] | None = None,
) -> Mapping[str, SkillToolMapping]:
    """Index skill mappings by skill name."""

    resolved_mappings = mappings if mappings is not None else get_packaged_skill_tool_mappings()
    return {mapping.skill_name: mapping for mapping in resolved_mappings}


def skill_frontmatter_context_keys_by_tool(
    mappings: tuple[SkillToolMapping, ...] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Index frontmatter-derived context keys by MCP tool name.

    If multiple skills point at the same MCP tool, keys are merged in discovery
    order with duplicates removed. This keeps skill usage facts separate from
    MCP descriptor metadata while still making tool-specific context usage easy
    to test.
    """

    resolved_mappings = mappings if mappings is not None else get_packaged_skill_tool_mappings()
    context_keys_by_tool: dict[str, tuple[str, ...]] = {}
    for mapping in resolved_mappings:
        existing = list(context_keys_by_tool.get(mapping.mcp_tool, ()))
        for key in mapping.context_keys:
            if key not in existing:
                existing.append(key)
        context_keys_by_tool[mapping.mcp_tool] = tuple(existing)
    return context_keys_by_tool


def merge_skill_context_keys_by_tool(
    frontmatter_context_keys_by_tool: Mapping[str, tuple[str, ...]],
    body_context_keys_by_tool: Mapping[str, tuple[str, ...]],
) -> Mapping[str, tuple[str, ...]]:
    """Merge tool-specific context keys from skill frontmatter and bodies.

    The return value is keyed by MCP tool name. Tools that appear only in body
    instructions are retained so companion polling/status/result usage remains
    visible to capability verification.
    """

    tool_names = tuple(
        dict.fromkeys(
            (
                *frontmatter_context_keys_by_tool.keys(),
                *body_context_keys_by_tool.keys(),
            )
        )
    )
    return {
        tool_name: merge_tool_context_keys(
            frontmatter_context_keys_by_tool.get(tool_name, ()),
            body_context_keys_by_tool.get(tool_name, ()),
        )
        for tool_name in tool_names
    }


def discover_skill_context_keys(
    skills_dir: str | Path | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Discover merged frontmatter/body context usage by MCP tool."""

    mappings = discover_skill_tool_mappings(skills_dir)
    return merge_skill_context_keys_by_tool(
        skill_frontmatter_context_keys_by_tool(mappings),
        discover_skill_body_context_keys(skills_dir),
    )


@lru_cache(maxsize=1)
def get_packaged_skill_context_keys() -> Mapping[str, tuple[str, ...]]:
    """Return cached merged packaged skill context usage by MCP tool."""

    return discover_skill_context_keys()


def get_skill_tool_mapping(
    skill_name: str,
    *,
    mappings: tuple[SkillToolMapping, ...] | None = None,
) -> SkillToolMapping | None:
    """Return the frontmatter-backed MCP tool mapping for one skill.

    This is the narrow query API capability checks can use when they only need
    skill usage facts. It intentionally delegates only to frontmatter discovery
    and never consults MCP tool descriptors.
    """

    normalized_skill_name = skill_name.strip()
    if not normalized_skill_name:
        return None
    return skill_tool_mapping_by_skill(mappings).get(normalized_skill_name)


__all__ = [
    "SkillToolMapping",
    "discover_skill_body_context_keys",
    "discover_skill_tool_mappings",
    "extract_skill_body_tool_context_keys",
    "extract_skill_frontmatter_context_keys",
    "discover_skill_context_keys",
    "get_packaged_skill_body_context_keys",
    "get_packaged_skill_context_keys",
    "get_skill_tool_mapping",
    "get_packaged_skill_tool_mappings",
    "merge_skill_context_keys_by_tool",
    "merge_tool_context_keys",
    "skill_frontmatter_context_keys_by_tool",
    "skill_tool_mapping_by_skill",
]
