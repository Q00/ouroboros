"""Pure, ordered AC read model. Diagnostic identities never grant authority.

Callers supply EventStore delivery order, not wall-clock order. A fold stores
only projected state, so cold replay and ordered incremental delivery coincide.
Final acceptance uses the existing Foundation B write-time validator. Routing
and lifecycle are observations, not inputs to that decision. Routing A remains
deferred: no observed decomposition can manufacture trust.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import json
from typing import Final

from pydantic import JsonValue

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.event_store import validate_acceptance_finalization_payload

RUNTIME_EVENTS: Final = frozenset(
    {
        "workflow.progress.updated",
        "execution.node.created",
        "execution.node.updated",
        "execution.subtask.updated",
        "execution.session.started",
        "execution.ac.completed",
        "execution.ac.acceptance_finalized",
        "execution.verify.failed",
        "execution.ac.route_observed",
        "execution.ac.model_routed",
        "execution.ac.recovery_exhausted",
        "execution.ac.alt_harness_redispatched",
    }
)


@dataclass(frozen=True, slots=True)
class ACRuntime:
    """Six independent human-facing dimensions for one node or root."""

    lifecycle: str = "unknown"
    acceptance: str = "unfinalized"
    decomposition_trust: str = "unknown"
    route: str = "unknown"
    escalation: str = "unknown"
    blocked: bool | None = None
    generation: str | None = None
    final_receipt: str | None = None
    final_workspace_failed: bool = False

    def to_data(self) -> dict[str, JsonValue]:
        return {
            "lifecycle": self.lifecycle,
            "acceptance": self.acceptance,
            "decomposition_trust": self.decomposition_trust,
            "route": self.route,
            "escalation": self.escalation,
            "blocked": self.blocked,
            "generation": self.generation,
        }

    def display_status(self, fallback: str = "pending") -> str:
        """Keep existing board columns while exposing independent dimensions."""
        if self.acceptance in {"rejected", "conflict"}:
            return "failed"
        if self.acceptance == "accepted":
            return "completed"
        if self.final_workspace_failed:
            return "failed"
        return self.lifecycle if self.lifecycle != "unknown" else fallback


@dataclass(frozen=True, slots=True)
class RuntimeNode:
    node_id: str
    root_index: int | None
    parent_id: str | None
    runtime: ACRuntime = ACRuntime()


@dataclass(frozen=True, slots=True)
class ACRuntimeProjection:
    """Immutable accumulator, scoped by the consumer's selected run."""

    execution_id: str | None = None
    session_id: str | None = None
    nodes: tuple[RuntimeNode, ...] = ()
    roots: tuple[tuple[int, ACRuntime], ...] = ()
    run: ACRuntime = ACRuntime()

    def for_node(
        self, node_id: str, root_index: int | None = None, *, child: bool = False
    ) -> ACRuntime:
        node = next((node for node in self.nodes if node.node_id == node_id), None)
        if node is None and not node_id and root_index is not None:
            candidates = [
                node for node in self.nodes if node.root_index == root_index and not node.parent_id
            ]
            node = candidates[0] if len(candidates) == 1 else None
        if node is not None:
            root_index, child = node.root_index, bool(node.parent_id)
        if (
            root_index is not None
            and sum(n.root_index == root_index and not n.parent_id for n in self.nodes) > 1
        ):
            child = True
        runtime = node.runtime if node is not None else ACRuntime()
        if node is None and root_index is None and not node_id:
            return self.run
        root = dict(self.roots).get(root_index) if root_index is not None and not child else None
        if root is None:
            return runtime
        return replace(
            root,
            lifecycle=runtime.lifecycle,
            route=root.route if root.route != "unknown" else runtime.route,
            escalation=root.escalation if root.escalation != "unknown" else runtime.escalation,
            blocked=root.blocked if root.blocked is not None else runtime.blocked,
        )


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value.strip() == value and value else None


def _index(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _lifecycle(value: JsonValue | None) -> str:
    aliases = {
        "done": "completed",
        "running": "executing",
        "active": "executing",
        "in_progress": "executing",
        "success": "completed",
        "succeeded": "completed",
        "error": "failed",
        "errored": "failed",
        "cancelled": "failed",
        "canceled": "failed",
        "atomic": "pending",
    }
    value = value.strip().lower() if isinstance(value, str) else value
    status = aliases.get(value, value) if isinstance(value, str) else None
    return (
        status
        if status in {"pending", "executing", "completed", "failed", "blocked"}
        else "unknown"
    )


def fold_ac_runtime(
    state: ACRuntimeProjection,
    event_type: str,
    data: Mapping[str, JsonValue],
    *,
    aggregate_id: str | None = None,
) -> ACRuntimeProjection:
    """Fold one delivered record without modifying inputs or authorizing effects."""
    if event_type not in RUNTIME_EVENTS:
        return state
    execution = _text(data.get("execution_id"))
    session = _text(data.get("session_id"))
    if (state.execution_id and execution and execution != state.execution_id) or (
        state.session_id and session and session != state.session_id
    ):
        return state
    nodes = {node.node_id: node for node in state.nodes}
    roots = dict(state.roots)
    run = state.run
    node_id = _text(data.get("node_id")) or _text(data.get("ac_id"))
    root_index = _index(data.get("root_ac_index"))

    def update_node(
        item: Mapping[str, JsonValue], ordinal: int | None = None, *, coarse: bool = False
    ) -> None:
        identifier = (
            _text(item.get("node_id")) or _text(item.get("ac_id")) or _text(item.get("sub_task_id"))
        )
        if identifier is None and ordinal is not None:
            identifier = f"ac_{ordinal + 1}"
        if identifier is None:
            return
        old = nodes.get(identifier, RuntimeNode(identifier, None, None))
        number = _index(item.get("root_ac_number")) or _index(item.get("index"))
        index = number - 1 if number is not None and number > 0 else old.root_index
        if index is None:
            index = ordinal
        parent = _text(item.get("parent_node_id")) or _text(item.get("parent_id")) or old.parent_id
        if parent is None and item.get("sub_task_id"):
            parent_number = _index(item.get("ac_index"))
            if parent_number is not None and parent_number > 0:
                parent = f"ac_{parent_number}"
        if old.root_index is not None and index != old.root_index:
            return
        if old.parent_id is not None and parent != old.parent_id:
            return
        lifecycle = _lifecycle(item.get("status"))
        if lifecycle == "unknown" or (
            coarse
            and old.runtime.lifecycle in {"completed", "failed"}
            and lifecycle not in {"completed", "failed"}
        ):
            lifecycle = old.runtime.lifecycle
        nodes[identifier] = RuntimeNode(
            identifier, index, parent, replace(old.runtime, lifecycle=lifecycle)
        )

    match event_type:
        case "workflow.progress.updated":
            criteria = data.get("acceptance_criteria")
            if isinstance(criteria, list):
                for ordinal, item in enumerate(criteria):
                    if isinstance(item, dict):
                        update_node(item, ordinal, coarse=True)
                    elif isinstance(item, str):
                        update_node(
                            {"node_id": f"ac_{ordinal + 1}", "status": "pending"},
                            ordinal,
                            coarse=True,
                        )
            tree = data.get("ac_tree") or data.get("tree")
            if isinstance(tree, dict) and isinstance(tree.get("nodes"), dict):
                tree_root = tree.get("root_id")
                for identifier, raw_node in tree["nodes"].items():
                    if identifier != tree_root and isinstance(raw_node, dict):
                        parent = raw_node.get("parent_id")
                        update_node(
                            {
                                **raw_node,
                                "node_id": identifier,
                                "parent_node_id": None if parent == tree_root else parent,
                            },
                            coarse=True,
                        )
        case "execution.node.created" | "execution.node.updated" | "execution.subtask.updated":
            update_node(data)
        case "execution.ac.completed" | "execution.session.started":
            if node_id:
                old = nodes.get(node_id, RuntimeNode(node_id, None, None))
                lifecycle = "executing"
                if event_type == "execution.ac.completed":
                    lifecycle = "completed" if data.get("success") is True else "failed"
                nodes[node_id] = replace(old, runtime=replace(old.runtime, lifecycle=lifecycle))
        case "execution.ac.acceptance_finalized":
            try:
                generation, root_index = validate_acceptance_finalization_payload(
                    data, aggregate_id=aggregate_id
                )
            except PersistenceError:
                return state
            old = roots.get(root_index, ACRuntime())
            receipt = json.dumps(dict(data), sort_keys=True, separators=(",", ":"))
            conflict = old.acceptance == "conflict" or (
                old.final_receipt is not None and old.final_receipt != receipt
            )
            roots[root_index] = replace(
                old,
                acceptance="conflict"
                if conflict
                else ("accepted" if data["accepted"] is True else "rejected"),
                generation=generation,
                final_receipt=old.final_receipt or receipt,
            )
        case "execution.verify.failed":
            root_index = _index(data.get("ac_index"))
            if root_index is not None and data.get("final_workspace_revalidation") is True:
                roots[root_index] = replace(
                    roots.get(root_index, ACRuntime()), final_workspace_failed=True
                )
        case "execution.ac.route_observed":
            if root_index is not None or data.get("root_ac_index") is None:
                observation = data.get("observation")
                decision = data.get("decision")
                old = roots.get(root_index, ACRuntime()) if root_index is not None else run
                route = (
                    _text(observation.get("route_id")) if isinstance(observation, dict) else None
                )
                action = _text(decision.get("action")) if isinstance(decision, dict) else None
                handoff = data.get("human_handoff_required")
                observed = replace(
                    old,
                    route=route or "unknown",
                    escalation=action or "unknown",
                    blocked=handoff if isinstance(handoff, bool) else None,
                )
                if root_index is None:
                    run = observed
                else:
                    roots[root_index] = observed
        case (
            "execution.ac.model_routed"
            | "execution.ac.alt_harness_redispatched"
            | "execution.ac.recovery_exhausted"
        ):
            if node_id or root_index is not None or data.get("call_site") == "runner":
                old_node = nodes.get(node_id) if node_id else None
                if old_node is not None:
                    root_index = old_node.root_index
                old = (
                    old_node.runtime
                    if old_node is not None
                    else roots.get(root_index, ACRuntime())
                    if root_index is not None
                    else run
                )
                if event_type == "execution.ac.recovery_exhausted":
                    runtime = replace(old, blocked=True, escalation="exhausted")
                elif event_type == "execution.ac.alt_harness_redispatched":
                    runtime = replace(
                        old,
                        route=_text(data.get("to_backend")) or "unknown",
                        escalation="harness_changed",
                    )
                else:
                    runtime = replace(
                        old,
                        route=_text(data.get("route_id")) or _text(data.get("model")) or "unknown",
                        escalation="model_escalated"
                        if data.get("model_escalated") is True
                        else "unknown",
                    )
                if node_id:
                    nodes[node_id] = replace(
                        old_node or RuntimeNode(node_id, root_index, None), runtime=runtime
                    )
                if root_index is not None and (old_node is None or not old_node.parent_id):
                    roots[root_index] = replace(
                        roots.get(root_index, ACRuntime()),
                        route=runtime.route,
                        escalation=runtime.escalation,
                        blocked=runtime.blocked,
                    )
                elif not node_id and root_index is None:
                    run = runtime
        case _:
            return state
    return replace(state, nodes=tuple(nodes.values()), roots=tuple(sorted(roots.items())), run=run)


def project_ac_runtime(
    events: Iterable[tuple[str, Mapping[str, JsonValue], str | None]],
    *,
    execution_id: str | None = None,
    session_id: str | None = None,
) -> ACRuntimeProjection:
    """Cold replay of the same fold used by live consumers."""
    state = ACRuntimeProjection(execution_id=execution_id, session_id=session_id)
    for event_type, data, aggregate_id in events:
        state = fold_ac_runtime(state, event_type, data, aggregate_id=aggregate_id)
    return state
