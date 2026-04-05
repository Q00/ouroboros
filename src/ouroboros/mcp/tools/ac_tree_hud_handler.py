"""Compact AC tree HUD renderer for MCP clients.

Returns a render-ready markdown snapshot for live execution monitoring in
Codex/Claude-style environments that do not have access to the Textual TUI.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_DEFAULT_MAX_NODES = 50
_SMALL_TREE_THRESHOLD = 12
_MAX_RENDER_DEPTH = 3
_ROOT_ID = "root"
_SESSION_STATUS_EVENT_TYPES = frozenset(
    {
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.paused",
        "orchestrator.session.cancelled",
    }
)

_STATUS_ICONS = {
    "completed": "●",
    "done": "●",
    "executing": "◐",
    "running": "◐",
    "in_progress": "◐",
    "failed": "✖",
    "pending": "○",
}

_FALLBACK_ACTIVITY_LABELS = {
    "missing": "working",
    "unavailable": "working",
}


def _coerce_int(value: object, default: int) -> int:
    """Return an integer when possible, otherwise a default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return default


def _coerce_non_empty_string(value: object) -> str | None:
    """Return a stripped string when present."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _coerce_children_ids(value: object) -> list[str]:
    """Normalize serialized child-ID values."""
    if not isinstance(value, list | tuple):
        return []
    child_ids: list[str] = []
    for item in value:
        child_id = _coerce_non_empty_string(item)
        if child_id is not None:
            child_ids.append(child_id)
    return child_ids


def _normalize_status(value: object) -> str:
    """Normalize status values onto the HUD status set."""
    normalized = _coerce_non_empty_string(value)
    if normalized is None:
        return "pending"
    lowered = normalized.lower()
    if lowered in {"completed", "done"}:
        return "completed"
    if lowered in {"executing", "running", "in_progress"}:
        return "executing"
    if lowered == "failed":
        return "failed"
    return "pending"


def _status_icon(status: object) -> str:
    """Return the compact glyph for a node status."""
    return _STATUS_ICONS.get(_normalize_status(status), "○")


def _extract_tool_input_path_hint(tool_input: object) -> str:
    """Return the best compact path-like hint from tool input."""
    if not isinstance(tool_input, Mapping):
        return ""

    for key in ("file_path", "path", "target", "uri"):
        path_hint = _coerce_non_empty_string(tool_input.get(key))
        if path_hint:
            return path_hint

    return ""


def _normalize_current_tool_activity(
    raw_activity: object,
    *,
    fallback_tool_name: object = None,
    fallback_tool_detail: object = None,
    fallback_tool_input: object = None,
) -> dict[str, str]:
    """Normalize raw tool-activity payloads into a stable compact summary shape."""
    raw_mapping = raw_activity if isinstance(raw_activity, Mapping) else None
    existing_state = (
        _coerce_non_empty_string(raw_mapping.get("state")) if raw_mapping is not None else None
    )
    if existing_state in {"active", "missing", "stale", "unavailable"}:
        tool_name = _coerce_non_empty_string(raw_mapping.get("tool_name")) or ""
        tool_detail = _coerce_non_empty_string(raw_mapping.get("tool_detail")) or ""
        path_hint = _coerce_non_empty_string(raw_mapping.get("path_hint")) or ""
        summary = _coerce_non_empty_string(raw_mapping.get("summary")) or ""

        if existing_state == "active":
            tool_name = tool_name or _coerce_non_empty_string(fallback_tool_name) or ""
            tool_detail = tool_detail or _coerce_non_empty_string(fallback_tool_detail) or ""
            if not path_hint:
                tool_input = raw_mapping.get("tool_input")
                if not isinstance(tool_input, Mapping):
                    tool_input = raw_mapping.get("input")
                if not isinstance(tool_input, Mapping):
                    tool_input = (
                        fallback_tool_input if isinstance(fallback_tool_input, Mapping) else {}
                    )
                path_hint = _extract_tool_input_path_hint(tool_input)
            if not summary:
                if tool_detail:
                    summary = tool_detail
                elif tool_name and path_hint:
                    summary = f"{tool_name} {path_hint}"
                else:
                    summary = tool_name

        return {
            "state": existing_state,
            "tool_name": tool_name,
            "tool_detail": tool_detail,
            "path_hint": path_hint,
            "summary": summary,
        }

    raw_detail = _coerce_non_empty_string(raw_activity)
    activity = raw_mapping or {}

    tool_name = (
        _coerce_non_empty_string(activity.get("tool_name"))
        or _coerce_non_empty_string(activity.get("current_tool"))
        or _coerce_non_empty_string(activity.get("active_tool"))
        or _coerce_non_empty_string(activity.get("tool"))
        or _coerce_non_empty_string(fallback_tool_name)
        or ""
    )

    tool_detail = (
        _coerce_non_empty_string(activity.get("tool_detail"))
        or _coerce_non_empty_string(activity.get("activity_detail"))
        or _coerce_non_empty_string(activity.get("detail"))
        or _coerce_non_empty_string(activity.get("summary"))
        or _coerce_non_empty_string(raw_activity)
        or _coerce_non_empty_string(fallback_tool_detail)
        or ""
    )

    tool_input = activity.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = activity.get("input")
    if not isinstance(tool_input, Mapping):
        tool_input = fallback_tool_input if isinstance(fallback_tool_input, Mapping) else {}

    path_hint = _extract_tool_input_path_hint(tool_input)
    message_type = _coerce_non_empty_string(activity.get("message_type")) or ""
    runtime_status = _coerce_non_empty_string(activity.get("runtime_status")) or ""
    has_tool_result = activity.get("tool_result") is not None

    is_stale = bool(
        has_tool_result
        or message_type.lower() in {"tool_result", "tool_result_chunk", "tool_completed"}
        or runtime_status.lower() in {"completed", "failed", "cancelled", "paused"}
    )
    raw_activity_missing = raw_activity is None
    raw_activity_unavailable = (
        raw_activity is not None and raw_mapping is None and raw_detail is None
    )
    payload_present_but_empty = bool(raw_mapping) is False and isinstance(raw_activity, Mapping)

    if is_stale:
        summary = ""
        state = "stale"
    elif tool_detail:
        summary = tool_detail
        state = "active"
    elif tool_name and path_hint:
        summary = f"{tool_name} {path_hint}"
        state = "active"
    else:
        summary = tool_name
        if summary:
            state = "active"
        elif raw_activity_missing:
            state = "missing"
        elif raw_activity_unavailable or payload_present_but_empty:
            state = "unavailable"
        else:
            state = "missing"

    return {
        "state": state,
        "tool_name": tool_name,
        "tool_detail": tool_detail,
        "path_hint": path_hint,
        "summary": summary,
    }


def _executing_tool_activity_fields(
    status: object,
    *,
    raw_activity: object,
    fallback_tool_name: object = None,
    fallback_tool_detail: object = None,
    fallback_tool_input: object = None,
) -> dict[str, Any]:
    """Attach normalized tool activity only for executing nodes."""
    if _normalize_status(status) != "executing":
        return {}

    tool_activity = _normalize_current_tool_activity(
        raw_activity,
        fallback_tool_name=fallback_tool_name,
        fallback_tool_detail=fallback_tool_detail,
        fallback_tool_input=fallback_tool_input,
    )
    return {
        "tool_activity_state": tool_activity["state"],
        "tool_name": tool_activity["tool_name"] or None,
        "tool_detail": tool_activity["tool_detail"] or None,
        "tool_activity": tool_activity,
        "tool_activity_summary": tool_activity["summary"] or None,
    }


def _resolve_node_tool_activity(raw_node: Mapping[str, Any]) -> object:
    """Prefer first-class normalized activity fields when present on a node."""
    raw_activity = raw_node.get("current_tool_activity")
    if raw_activity is None:
        raw_activity = raw_node.get("tool_activity")

    fallback_state = _coerce_non_empty_string(raw_node.get("tool_activity_state"))
    fallback_summary = _coerce_non_empty_string(raw_node.get("tool_activity_summary"))
    if not fallback_state and not fallback_summary:
        return raw_activity

    merged_activity: dict[str, Any]
    if isinstance(raw_activity, Mapping):
        merged_activity = dict(raw_activity)
    else:
        merged_activity = {}
        raw_detail = _coerce_non_empty_string(raw_activity)
        if raw_detail:
            merged_activity["detail"] = raw_detail

    if fallback_state and "state" not in merged_activity:
        merged_activity["state"] = fallback_state
    if fallback_summary and "summary" not in merged_activity:
        merged_activity["summary"] = fallback_summary

    return merged_activity


def _find_latest_progress_event(events: list[Any]) -> Any | None:
    """Return the newest workflow.progress.updated event from an event list."""
    latest = None
    for event in events:
        if getattr(event, "type", None) == "workflow.progress.updated":
            latest = event
    return latest


def _find_latest_prior_progress_event(
    events: list[Any], excluded_event_ids: set[str]
) -> Any | None:
    """Return the newest workflow.progress.updated event excluding newer cursor events."""
    latest = None
    for event in events:
        if getattr(event, "type", None) != "workflow.progress.updated":
            continue
        if getattr(event, "id", None) in excluded_event_ids:
            continue
        latest = event
    return latest


def _has_status_change_event(events: list[Any]) -> bool:
    """Return True when new session lifecycle events should force a rerender."""
    return any(getattr(event, "type", None) in _SESSION_STATUS_EVENT_TYPES for event in events)


def _valid_tree_payload(value: object) -> bool:
    """Return True when a mapping resembles a serialized AC tree."""
    if not isinstance(value, Mapping):
        return False
    root_id = value.get("root_id")
    nodes = value.get("nodes")
    return isinstance(root_id, str) and isinstance(nodes, Mapping)


def _normalize_explicit_tree(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a serialized tree payload for rendering."""
    raw_nodes = value.get("nodes")
    assert isinstance(raw_nodes, Mapping)

    nodes: dict[str, dict[str, Any]] = {}
    for raw_id, raw_node in raw_nodes.items():
        if not isinstance(raw_node, Mapping):
            continue
        node_id = _coerce_non_empty_string(raw_node.get("id")) or _coerce_non_empty_string(raw_id)
        if node_id is None:
            continue
        status = _normalize_status(raw_node.get("status"))
        node = {
            "id": node_id,
            "content": _coerce_non_empty_string(raw_node.get("content")) or node_id,
            "status": status,
            "depth": _coerce_int(raw_node.get("depth"), 0),
            "index": raw_node.get("index"),
            "parent_id": _coerce_non_empty_string(raw_node.get("parent_id")),
            "children_ids": _coerce_children_ids(raw_node.get("children_ids")),
            "_order": len(nodes),
        }
        node.update(
            _executing_tool_activity_fields(
                status,
                raw_activity=_resolve_node_tool_activity(raw_node),
                fallback_tool_name=raw_node.get("tool_name"),
                fallback_tool_detail=raw_node.get("tool_detail"),
                fallback_tool_input=raw_node.get("tool_input"),
            )
        )
        nodes[node_id] = node

    root_id = _coerce_non_empty_string(value.get("root_id")) or _ROOT_ID
    if root_id not in nodes:
        nodes[root_id] = {
            "id": root_id,
            "content": "Acceptance Criteria",
            "status": "pending",
            "depth": 0,
            "children_ids": [],
            "_order": len(nodes),
        }

    return {"root_id": root_id, "nodes": nodes}


def _build_tree_from_acceptance_criteria(
    acceptance_criteria: object,
    *,
    current_ac_index: int | None,
) -> dict[str, Any]:
    """Build a render tree from workflow.progress.updated acceptance_criteria."""
    nodes: dict[str, dict[str, Any]] = {
        _ROOT_ID: {
            "id": _ROOT_ID,
            "content": "Acceptance Criteria",
            "status": "executing" if current_ac_index is not None else "pending",
            "depth": 0,
            "children_ids": [],
            "_order": 0,
        }
    }
    if not isinstance(acceptance_criteria, list | tuple):
        return {"root_id": _ROOT_ID, "nodes": nodes}

    children_by_parent: dict[str, list[str]] = defaultdict(list)
    root_child_ids: list[str] = []

    for order, raw_node in enumerate(acceptance_criteria, start=1):
        if not isinstance(raw_node, Mapping):
            continue

        raw_index = raw_node.get("index")
        ac_index = _coerce_int(raw_index, 0)
        node_id = _coerce_non_empty_string(raw_node.get("id"))
        if node_id is None:
            node_id = f"ac_{ac_index}" if ac_index > 0 else f"ac_node_{order}"

        parent_id = _coerce_non_empty_string(raw_node.get("parent_id"))
        depth = _coerce_int(raw_node.get("depth"), 2 if parent_id else 1)
        status = _normalize_status(raw_node.get("status"))
        node = {
            "id": node_id,
            "content": _coerce_non_empty_string(raw_node.get("content")) or node_id,
            "status": status,
            "depth": depth,
            "index": ac_index if ac_index > 0 else None,
            "parent_id": parent_id,
            "children_ids": _coerce_children_ids(raw_node.get("children_ids")),
            "_order": order,
        }
        node.update(
            _executing_tool_activity_fields(
                status,
                raw_activity=_resolve_node_tool_activity(raw_node),
                fallback_tool_name=raw_node.get("tool_name"),
                fallback_tool_detail=raw_node.get("tool_detail"),
                fallback_tool_input=raw_node.get("tool_input"),
            )
        )
        nodes[node_id] = node

        if parent_id:
            children_by_parent[parent_id].append(node_id)
        else:
            root_child_ids.append(node_id)

    for parent_id, child_ids in children_by_parent.items():
        if parent_id in nodes:
            existing = list(nodes[parent_id].get("children_ids", []))
            for child_id in child_ids:
                if child_id not in existing:
                    existing.append(child_id)
            nodes[parent_id]["children_ids"] = existing

    nodes[_ROOT_ID]["children_ids"] = [
        child_id
        for child_id in sorted(
            root_child_ids,
            key=lambda cid: (
                _coerce_int(nodes[cid].get("index"), 10**9),
                _coerce_int(nodes[cid].get("_order"), 10**9),
            ),
        )
        if child_id in nodes
    ]

    if current_ac_index is not None:
        current_id = f"ac_{current_ac_index}"
        if current_id in nodes and nodes[current_id].get("status") in (
            "pending",
            "in_progress",
        ):
            nodes[current_id]["status"] = "executing"

    return {"root_id": _ROOT_ID, "nodes": nodes}


def _extract_tree_snapshot(progress_data: Mapping[str, Any]) -> dict[str, Any]:
    """Extract or synthesize a render tree from progress-event data."""
    for key in ("ac_tree", "tree"):
        candidate = progress_data.get(key)
        if _valid_tree_payload(candidate):
            return _normalize_explicit_tree(candidate)

    return _build_tree_from_acceptance_criteria(
        progress_data.get("acceptance_criteria"),
        current_ac_index=_coerce_int(progress_data.get("current_ac_index"), 0) or None,
    )


def _tree_snapshot_changed(
    previous_progress_data: Mapping[str, Any] | None,
    current_progress_data: Mapping[str, Any],
) -> bool:
    """Return True when the render-relevant AC tree snapshot changed."""
    if previous_progress_data is None:
        return True
    return _extract_tree_snapshot(previous_progress_data) != _extract_tree_snapshot(
        current_progress_data
    )


def _format_activity(activity: object, detail: object) -> str | None:
    """Format overall activity text for the HUD header."""
    activity_text = _coerce_non_empty_string(activity)
    detail_text = _coerce_non_empty_string(detail)
    if activity_text and detail_text:
        return f"{activity_text} | {detail_text}"
    return activity_text or detail_text


def _format_tool_activity(
    tool_name: object,
    tool_detail: object,
    tool_input: object,
    *,
    raw_activity: object = None,
) -> str | None:
    """Format inline tool activity for the executing node."""
    normalized_activity = _normalize_current_tool_activity(
        raw_activity,
        fallback_tool_name=tool_name,
        fallback_tool_detail=tool_detail,
        fallback_tool_input=tool_input,
    )
    summary = normalized_activity["summary"]
    if summary:
        return summary
    return _FALLBACK_ACTIVITY_LABELS.get(normalized_activity["state"])


def _tool_activity_state(node: Mapping[str, Any]) -> str | None:
    """Return the normalized inline activity state stored on a render node."""
    state = _coerce_non_empty_string(node.get("tool_activity_state"))
    if state:
        return state

    tool_activity = node.get("tool_activity")
    if isinstance(tool_activity, Mapping):
        return _coerce_non_empty_string(tool_activity.get("state"))

    return None


def _node_sort_key(node: Mapping[str, Any]) -> tuple[int, int]:
    """Sort nodes by index first, then insertion order."""
    raw_index = node.get("index")
    index = raw_index if isinstance(raw_index, int) and raw_index > 0 else 10**9
    order = _coerce_int(node.get("_order"), 10**9)
    return index, order


def _top_level_focus_ids(
    root_child_ids: list[str],
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    max_nodes: int,
) -> list[str]:
    """Select the visible top-level nodes for a compact render."""
    if len(root_child_ids) <= _SMALL_TREE_THRESHOLD:
        return root_child_ids[:max_nodes]

    executing_index = next(
        (
            idx
            for idx, child_id in enumerate(root_child_ids)
            if _normalize_status(nodes.get(child_id, {}).get("status")) == "executing"
        ),
        None,
    )

    preferred_positions: list[int] = []
    last_index = len(root_child_ids) - 1
    if executing_index is None:
        preferred_positions.extend((0, last_index))
        preferred_positions.extend(range(1, min(len(root_child_ids), 10)))
    else:
        preferred_positions.extend((executing_index, 0, last_index))
        preferred_positions.extend(
            (
                executing_index - 1,
                executing_index + 1,
                executing_index - 2,
                executing_index + 2,
            )
        )

    selected_positions: list[int] = []
    seen_positions: set[int] = set()
    for position in preferred_positions:
        if position < 0 or position >= len(root_child_ids) or position in seen_positions:
            continue
        selected_positions.append(position)
        seen_positions.add(position)
        if len(selected_positions) >= max_nodes:
            break

    return [root_child_ids[idx] for idx in sorted(selected_positions)]


def _count_descendants(node_id: str, nodes: Mapping[str, Mapping[str, Any]]) -> int:
    """Count descendants beneath a node for collapsed-depth summaries."""
    total = 0
    stack = list(_coerce_children_ids(nodes.get(node_id, {}).get("children_ids")))
    while stack:
        current = stack.pop()
        total += 1
        stack.extend(_coerce_children_ids(nodes.get(current, {}).get("children_ids")))
    return total


def _format_node_label(node: Mapping[str, Any]) -> str:
    """Format a compact node label for HUD output."""
    content = _coerce_non_empty_string(node.get("content")) or node.get("id", "AC")
    if len(content) > 88:
        content = content[:85] + "..."

    index = node.get("index")
    depth = _coerce_int(node.get("depth"), 0)
    if isinstance(index, int) and index > 0 and depth <= 1:
        return f"AC {index}: {content}"
    return content


def _render_tree_lines(
    snapshot: Mapping[str, Any],
    *,
    current_ac_index: int | None,
    last_tool_activity: Mapping[str, str],
    max_nodes: int,
) -> list[str]:
    """Render the tree portion of the HUD as compact markdown lines."""
    nodes = snapshot.get("nodes")
    root_id = snapshot.get("root_id")
    if not isinstance(nodes, Mapping) or not isinstance(root_id, str) or root_id not in nodes:
        return ["○ Waiting for AC tree..."]

    visible_lines: list[str] = ["◇ Acceptance Criteria"]
    rendered_nodes = 0
    remaining_budget = max(1, max_nodes)

    def render_children(
        parent_id: str,
        prefix: str,
        depth: int,
        *,
        child_ids_override: list[str] | None = None,
    ) -> None:
        nonlocal rendered_nodes, remaining_budget
        if remaining_budget <= 0:
            return

        raw_child_ids = child_ids_override or _coerce_children_ids(
            nodes[parent_id].get("children_ids")
        )
        child_ids = [child_id for child_id in raw_child_ids if child_id in nodes]
        if not child_ids:
            return

        if depth == 0:
            focused_child_ids = _top_level_focus_ids(child_ids, nodes, max_nodes=max_nodes)
            focused_set = set(focused_child_ids)
        else:
            focused_child_ids = sorted(child_ids, key=lambda cid: _node_sort_key(nodes[cid]))
            focused_set = set(focused_child_ids)

        focused_positions = [
            idx for idx, child_id in enumerate(child_ids) if child_id in focused_set
        ]
        for position_index, child_position in enumerate(focused_positions):
            if remaining_budget <= 0:
                break

            previous_position = (
                focused_positions[position_index - 1] if position_index > 0 else None
            )
            if previous_position is not None and child_position - previous_position > 1:
                skipped = child_position - previous_position - 1
                # Gap marker uses ├─ unless no more children follow (the last
                # focused node will render its own └─ branch on the next iteration).
                branch = "├─" if position_index < len(focused_positions) else "└─"
                visible_lines.append(f"{prefix}{branch} ... (+{skipped} tasks)")

            child_id = child_ids[child_position]
            child = nodes[child_id]
            is_last = position_index == len(focused_positions) - 1
            branch = "└─" if is_last else "├─"
            child_prefix = f"{prefix}{'   ' if is_last else '│  '}"

            icon = _status_icon(child.get("status"))
            label = _format_node_label(child)
            status = _normalize_status(child.get("status"))
            inline_activity = None
            if status == "executing":
                inline_activity = _format_tool_activity(
                    child.get("tool_name"),
                    child.get("tool_detail"),
                    None,
                    raw_activity=child.get("tool_activity"),
                )

                child_index = child.get("index")
                child_activity_state = _tool_activity_state(child)
                if (
                    isinstance(child_index, int)
                    and child_index == current_ac_index
                    and (
                        inline_activity is None or child_activity_state in _FALLBACK_ACTIVITY_LABELS
                    )
                ):
                    current_activity = _format_tool_activity(
                        last_tool_activity.get("tool_name"),
                        last_tool_activity.get("tool_detail"),
                        {"path": last_tool_activity.get("path_hint")}
                        if last_tool_activity.get("path_hint")
                        else None,
                        raw_activity=last_tool_activity,
                    )
                    if current_activity is not None:
                        inline_activity = current_activity

            suffix = f"  [{inline_activity}]" if inline_activity else ""
            visible_lines.append(f"{prefix}{branch} {icon} {label}{suffix}")
            rendered_nodes += 1
            remaining_budget -= 1

            if remaining_budget <= 0:
                break

            child_depth = _coerce_int(child.get("depth"), depth + 1)
            if child_depth >= _MAX_RENDER_DEPTH and _coerce_children_ids(child.get("children_ids")):
                hidden_count = _count_descendants(child_id, nodes)
                continuation_branch = "└─" if is_last else "├─"
                visible_lines.append(
                    f"{child_prefix}{continuation_branch} ... (+{hidden_count} sub-tasks)"
                )
                continue

            render_children(child_id, child_prefix, depth + 1)

    render_children(root_id, "", 0)
    return visible_lines


def _format_footer(progress_data: Mapping[str, Any]) -> str | None:
    """Format compact footer metrics, omitting empty values."""
    parts: list[str] = []

    elapsed = _coerce_non_empty_string(progress_data.get("elapsed_display"))
    if elapsed:
        parts.append(f"elapsed {elapsed}")

    messages_count = _coerce_int(progress_data.get("messages_count"), 0)
    if messages_count > 0:
        parts.append(f"{messages_count} msgs")

    tool_calls_count = _coerce_int(progress_data.get("tool_calls_count"), 0)
    if tool_calls_count > 0:
        parts.append(f"{tool_calls_count} tools")

    estimated_cost = progress_data.get("estimated_cost_usd")
    if isinstance(estimated_cost, int | float) and estimated_cost > 0:
        parts.append(f"${estimated_cost:.2f}")

    if not parts:
        return None
    return " · ".join(parts)


def render_ac_tree_hud_markdown(
    *,
    session_id: str,
    execution_id: str,
    session_status: str,
    progress_data: Mapping[str, Any],
    max_nodes: int = _DEFAULT_MAX_NODES,
) -> str:
    """Render a compact markdown HUD from workflow progress data."""
    completed_count = _coerce_int(progress_data.get("completed_count"), 0)
    total_count = _coerce_int(progress_data.get("total_count"), 0)
    current_ac_index = _coerce_int(progress_data.get("current_ac_index"), 0) or None
    last_update = (
        dict(progress_data.get("last_update"))
        if isinstance(progress_data.get("last_update"), Mapping)
        else {}
    )
    last_tool_activity = _normalize_current_tool_activity(last_update)

    lines = [
        f"Session: {session_id}",
        f"Execution: {execution_id}",
        f"Status: {session_status}",
    ]

    phase = _coerce_non_empty_string(progress_data.get("current_phase"))
    if phase:
        lines.append(f"Phase: {phase}")

    lines.append(f"Progress: {completed_count}/{total_count} AC complete")

    activity = _format_activity(
        progress_data.get("activity"),
        progress_data.get("activity_detail"),
    )
    if activity:
        lines.append(f"Activity: {activity}")

    footer = _format_footer(progress_data)
    if footer:
        lines.append(f"Metrics: {footer}")

    lines.append("")
    lines.extend(
        _render_tree_lines(
            _extract_tree_snapshot(progress_data),
            current_ac_index=current_ac_index,
            last_tool_activity=last_tool_activity,
            max_nodes=max_nodes,
        )
    )
    return "\n".join(lines)


def _warning_result(
    *,
    session_id: str,
    cursor: int,
    message: str,
    execution_id: str | None = None,
    status: str | None = None,
    changed: bool = False,
) -> MCPToolResult:
    """Create a graceful warning result instead of a structured error."""
    lines = [f"Session: {session_id or 'unknown'}"]
    if execution_id:
        lines.append(f"Execution: {execution_id}")
    if status:
        lines.append(f"Status: {status}")
    lines.append(f"Warning: {message}")
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),),
        is_error=False,
        meta={
            "session_id": session_id,
            "execution_id": execution_id,
            "status": status,
            "cursor": cursor,
            "changed": changed,
            "warning": message,
        },
    )


@dataclass
class ACTreeHUDHandler:
    """Return a compact, cursor-aware AC tree HUD for a session."""

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize handler-owned resources."""
        self._owns_event_store = self.event_store is None
        self._event_store = self.event_store or EventStore()
        self._session_repo = SessionRepository(self._event_store)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Initialize the event store lazily."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def close(self) -> None:
        """Close the event store when this handler owns it."""
        if self._owns_event_store:
            await self._event_store.close()
            self._initialized = False

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the MCP tool definition."""
        return MCPToolDefinition(
            name="ouroboros_ac_tree_hud",
            description=(
                "Return a compact, render-ready markdown snapshot of the live "
                "acceptance-criteria tree for an Ouroboros session."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Session ID to inspect.",
                    required=True,
                ),
                MCPToolParameter(
                    name="cursor",
                    type=ToolInputType.INTEGER,
                    description="Previous EventStore row ID cursor.",
                    required=False,
                    default=0,
                ),
                MCPToolParameter(
                    name="max_nodes",
                    type=ToolInputType.INTEGER,
                    description="Maximum nodes to render before truncation.",
                    required=False,
                    default=_DEFAULT_MAX_NODES,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Render the current AC tree HUD for a session."""
        session_id = _coerce_non_empty_string(arguments.get("session_id")) or ""
        cursor = max(0, _coerce_int(arguments.get("cursor"), 0))
        max_nodes = max(1, _coerce_int(arguments.get("max_nodes"), _DEFAULT_MAX_NODES))

        if not session_id:
            return Result.ok(
                _warning_result(
                    session_id="",
                    cursor=cursor,
                    message="session_id is required.",
                )
            )

        try:
            await self._ensure_initialized()

            session_result = await self._session_repo.reconstruct_session(session_id)
            if session_result.is_err:
                return Result.ok(
                    _warning_result(
                        session_id=session_id,
                        cursor=cursor,
                        message="session not found.",
                    )
                )

            tracker = session_result.value
            execution_id = _coerce_non_empty_string(tracker.execution_id)
            if execution_id is None:
                return Result.ok(
                    _warning_result(
                        session_id=session_id,
                        cursor=cursor,
                        status=tracker.status.value,
                        message="no execution linked to this session yet.",
                    )
                )

            session_events: list[Any] = []
            session_cursor = cursor
            if cursor > 0:
                session_events, session_cursor = await self._event_store.get_events_after(
                    "session",
                    session_id,
                    cursor,
                )
            new_events, execution_cursor = await self._event_store.get_events_after(
                "execution",
                execution_id,
                cursor,
            )
            new_cursor = max(session_cursor, execution_cursor)
            latest_new_progress_event = _find_latest_progress_event(new_events)
            has_status_change_event = _has_status_change_event(session_events)

            if latest_new_progress_event is None:
                latest_historical_events = await self._event_store.query_events(
                    aggregate_id=execution_id,
                    event_type="workflow.progress.updated",
                    limit=1,
                )
                latest_progress_event = (
                    latest_historical_events[0] if latest_historical_events else None
                )
                if latest_progress_event is None:
                    return Result.ok(
                        _warning_result(
                            session_id=session_id,
                            execution_id=execution_id,
                            status=tracker.status.value,
                            cursor=new_cursor,
                            message="waiting for the first AC tree update.",
                        )
                    )

                if cursor > 0 and not has_status_change_event:
                    return Result.ok(
                        MCPToolResult(
                            content=(
                                MCPContentItem(
                                    type=ContentType.TEXT,
                                    text=f"No AC tree change since cursor {cursor}.",
                                ),
                            ),
                            is_error=False,
                            meta={
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "status": tracker.status.value,
                                "cursor": new_cursor,
                                "changed": False,
                            },
                        )
                    )

                progress_data = latest_progress_event.data
            else:
                if cursor > 0 and not has_status_change_event:
                    execution_history = await self._event_store.replay("execution", execution_id)
                    new_event_ids = {
                        getattr(event, "id", "")
                        for event in new_events
                        if _coerce_non_empty_string(getattr(event, "id", None))
                    }
                    previous_progress_event = _find_latest_prior_progress_event(
                        execution_history,
                        new_event_ids,
                    )
                    if not _tree_snapshot_changed(
                        previous_progress_event.data
                        if previous_progress_event is not None
                        and isinstance(previous_progress_event.data, Mapping)
                        else None,
                        latest_new_progress_event.data,
                    ):
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=f"No AC tree change since cursor {cursor}.",
                                    ),
                                ),
                                is_error=False,
                                meta={
                                    "session_id": session_id,
                                    "execution_id": execution_id,
                                    "status": tracker.status.value,
                                    "cursor": new_cursor,
                                    "changed": False,
                                },
                            )
                        )
                progress_data = latest_new_progress_event.data

            markdown = render_ac_tree_hud_markdown(
                session_id=session_id,
                execution_id=execution_id,
                session_status=tracker.status.value,
                progress_data=progress_data,
                max_nodes=max_nodes,
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=markdown),),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "execution_id": execution_id,
                        "status": tracker.status.value,
                        "cursor": new_cursor,
                        "changed": (
                            latest_new_progress_event is not None
                            or has_status_change_event
                            or cursor == 0
                        ),
                    },
                )
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            log.exception("mcp.tool.ac_tree_hud.error", session_id=session_id, error=str(exc))
            return Result.ok(
                _warning_result(
                    session_id=session_id,
                    cursor=cursor,
                    message="unable to render AC tree HUD right now.",
                )
            )
        finally:
            if self._owns_event_store:
                await self.close()
