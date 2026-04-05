"""Compact markdown rendering helpers for AC tree HUD snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_AC_TREE_RENDER_DEPTH = 3
DEFAULT_AC_TREE_MAX_NODES = 50

STATUS_ICONS: dict[str, str] = {
    "pending": "○",
    "blocked": "⊘",
    "atomic": "◆",
    "decomposed": "◇",
    "executing": "◐",
    "running": "◐",
    "completed": "●",
    "complete": "●",
    "done": "●",
    "failed": "✖",
    "cancelled": "⊘",
}

FALLBACK_ACTIVITY_LABELS: dict[str, str] = {
    "missing": "working",
    "unavailable": "working",
}


@dataclass
class _RenderState:
    lines: list[str]
    rendered_nodes: int = 0


def render_ac_tree_markdown(
    snapshot: Mapping[str, Any],
    *,
    max_depth: int = MAX_AC_TREE_RENDER_DEPTH,
    max_nodes: int = DEFAULT_AC_TREE_MAX_NODES,
) -> str:
    """Render a tree snapshot as compact markdown.

    The renderer is intentionally pure and tolerant of partial data so MCP
    handlers can reuse it for both live and terminal snapshots.
    """
    tree = _resolve_tree(snapshot)
    nodes = tree.get("nodes")
    if not isinstance(nodes, Mapping) or not nodes:
        return ""

    root_id = tree.get("root_id", "root")
    root = nodes.get(root_id)
    if not isinstance(root, Mapping):
        return ""

    root_content = _coerce_text(root.get("content")) or "Acceptance Criteria"
    lines = [f"◇ {root_content}"]
    state = _RenderState(lines=lines)

    _render_children(
        nodes=nodes,
        child_ids=_child_ids(root),
        depth=1,
        prefix="",
        state=state,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    footer = _render_footer(snapshot)
    if footer:
        lines.extend(("", footer))

    return "\n".join(lines)


def _resolve_tree(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    tree = snapshot.get("tree")
    if isinstance(tree, Mapping):
        return tree
    return snapshot


def _render_children(
    *,
    nodes: Mapping[str, Any],
    child_ids: Sequence[str],
    depth: int,
    prefix: str,
    state: _RenderState,
    max_depth: int,
    max_nodes: int,
) -> None:
    remaining = [child_id for child_id in child_ids if child_id in nodes]
    for index, child_id in enumerate(remaining):
        is_last = index == len(remaining) - 1
        if state.rendered_nodes >= max_nodes:
            hidden_count = _count_descendants(nodes, remaining[index:])
            if hidden_count:
                branch = "└─ " if is_last else "├─ "
                state.lines.append(f"{prefix}{branch}... (+{hidden_count} sub-tasks)")
            return

        _render_node(
            nodes=nodes,
            node_id=child_id,
            depth=depth,
            prefix=prefix,
            is_last=is_last,
            state=state,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )


def _render_node(
    *,
    nodes: Mapping[str, Any],
    node_id: str,
    depth: int,
    prefix: str,
    is_last: bool,
    state: _RenderState,
    max_depth: int,
    max_nodes: int,
) -> None:
    raw_node = nodes.get(node_id)
    if not isinstance(raw_node, Mapping):
        return

    branch = "└─ " if is_last else "├─ "
    state.lines.append(f"{prefix}{branch}{_format_node_label(raw_node)}")
    state.rendered_nodes += 1

    child_ids = _child_ids(raw_node)
    if not child_ids:
        return

    child_prefix = prefix + ("   " if is_last else "│  ")
    if depth >= max_depth:
        hidden_count = _count_descendants(nodes, child_ids)
        if hidden_count:
            state.lines.append(f"{child_prefix}└─ ... (+{hidden_count} sub-tasks)")
        return

    _render_children(
        nodes=nodes,
        child_ids=child_ids,
        depth=depth + 1,
        prefix=child_prefix,
        state=state,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def _format_node_label(node: Mapping[str, Any]) -> str:
    status = _coerce_text(node.get("status")).lower() or "pending"
    icon = STATUS_ICONS.get(status, STATUS_ICONS["pending"])
    content = _coerce_text(node.get("content")) or _coerce_text(node.get("id")) or "Untitled"
    activity = _inline_activity(node)
    if activity:
        return f"{icon} {content} [{activity}]"
    return f"{icon} {content}"


def _inline_activity(node: Mapping[str, Any]) -> str:
    status = _coerce_text(node.get("status")).lower()
    if status not in {"executing", "running"}:
        return ""

    summary = _coerce_text(node.get("tool_activity_summary"))
    if summary:
        return summary

    for key in ("tool_activity", "current_tool_activity"):
        value = node.get(key)
        if isinstance(value, Mapping):
            summary = _summarize_tool_activity(value)
            if summary:
                return summary

    for key in ("tool_detail", "current_tool", "active_tool", "activity_detail"):
        value = _coerce_text(node.get(key))
        if value:
            return value

    last_update = node.get("last_update")
    if isinstance(last_update, Mapping):
        summary = _summarize_tool_activity(last_update)
        if summary:
            return summary
        thinking = _coerce_text(last_update.get("thinking"))
        if thinking:
            return thinking

    fallback_state = _coerce_text(node.get("tool_activity_state")).lower()
    if not fallback_state:
        tool_activity = node.get("tool_activity")
        if isinstance(tool_activity, Mapping):
            fallback_state = _coerce_text(tool_activity.get("state")).lower()

    return FALLBACK_ACTIVITY_LABELS.get(fallback_state, "")


def _summarize_tool_activity(activity: Mapping[str, Any]) -> str:
    for key in ("summary", "tool_detail", "activity_detail", "detail"):
        value = _coerce_text(activity.get(key))
        if value:
            return value

    tool_name = ""
    for key in ("tool_name", "current_tool", "active_tool", "tool"):
        value = _coerce_text(activity.get(key))
        if value:
            tool_name = value
            break

    tool_input = activity.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = activity.get("input")

    path_hint = _extract_tool_path_hint(tool_input)
    if tool_name and path_hint:
        return f"{tool_name} {path_hint}"
    return tool_name


def _extract_tool_path_hint(tool_input: object) -> str:
    if not isinstance(tool_input, Mapping):
        return ""

    for key in ("file_path", "path", "target", "uri"):
        value = _coerce_text(tool_input.get(key))
        if value:
            return value

    return ""


def _render_footer(snapshot: Mapping[str, Any]) -> str:
    parts: list[str] = []

    elapsed = _coerce_text(snapshot.get("elapsed_display"))
    if elapsed:
        parts.append(elapsed)

    messages = snapshot.get("messages_count")
    if isinstance(messages, int) and messages > 0:
        parts.append(f"{messages} msgs")

    tool_calls = snapshot.get("tool_calls_count")
    if isinstance(tool_calls, int) and tool_calls > 0:
        parts.append(f"{tool_calls} tools")

    cost = snapshot.get("estimated_cost_usd")
    if isinstance(cost, int | float) and cost > 0:
        parts.append(f"${cost:.2f}")

    return " | ".join(parts)


def _child_ids(node: Mapping[str, Any]) -> list[str]:
    raw_ids = node.get("children_ids")
    if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, str | bytes | bytearray):
        return []
    return [str(child_id) for child_id in raw_ids if str(child_id)]


def _count_descendants(nodes: Mapping[str, Any], node_ids: Sequence[str]) -> int:
    count = 0
    stack = [node_id for node_id in node_ids if node_id in nodes]
    while stack:
        node_id = stack.pop()
        raw_node = nodes.get(node_id)
        if not isinstance(raw_node, Mapping):
            continue
        count += 1
        stack.extend(_child_ids(raw_node))
    return count


def _coerce_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""
