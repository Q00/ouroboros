"""Main TUI application using Textual framework.

OuroborosTUI is the main application class that:
- Manages screens (session selector, dashboard, execution, logs, debug)
- Handles global keybindings
- Subscribes to EventStore for live updates
- Provides pause/resume execution control
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App
from textual.binding import Binding

from ouroboros.dashboard.board import ProviderLedger, fold_provider_event
from ouroboros.tui.events import (
    ACModelRouted,
    ACTokenAttribution,
    ACUpdated,
    AgentThinkingUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    FrugalityProofEvaluated,
    FrugalityRetrospectiveReported,
    LogMessage,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    SubtaskUpdated,
    ToolCallCompleted,
    ToolCallStarted,
    TUIState,
    WorkflowProgressUpdated,
    create_message_from_event,
    format_frugality_retrospective_summary,
    format_frugality_summary,
)
from ouroboros.tui.screens import (
    DashboardScreenV3,
    DebugScreen,
    ExecutionScreen,
    LineageDetailScreen,
    LogsScreen,
)
from ouroboros.tui.screens.lineage_selector import LineageSelectorScreen
from ouroboros.tui.screens.session_selector import SessionSelectorScreen

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent
    from ouroboros.persistence.event_store import EventStore


@dataclass(frozen=True)
class _EventSubscriptionContext:
    """Immutable polling context for a single monitored execution/session."""

    execution_id: str
    session_id: str = ""
    generation: int = 0


def _coerce_non_empty_string(value: object) -> str | None:
    """Return a stripped non-empty string when present."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


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


def _find_node_id_for_ac_index(nodes: dict[str, Any], ac_index: int) -> str | None:
    """Resolve the top-level tree node associated with a 1-based AC index."""
    if ac_index <= 0:
        return None

    conventional_id = f"ac_{ac_index}"
    if conventional_id in nodes:
        return conventional_id

    for node_id, raw_node in nodes.items():
        if not isinstance(raw_node, dict):
            continue
        if _coerce_int(raw_node.get("index"), 0) != ac_index:
            continue
        if _coerce_int(raw_node.get("depth"), 0) > 1:
            continue
        return node_id

    return None


def _legacy_ac_node_aliases(ac: dict[str, Any], canonical_id: str) -> list[str]:
    """Return legacy top-level AC node IDs that may alias ``canonical_id``."""
    aliases: list[str] = []
    for value in (
        ac.get("ac_id"),
        f"ac_{ac.get('index', 0)}",
        f"ac_{ac.get('root_ac_index') + 1}"
        if isinstance(ac.get("root_ac_index"), int) and ac.get("root_ac_index") >= 0
        else None,
    ):
        alias = _coerce_non_empty_string(value)
        if alias is not None and alias != canonical_id and alias not in aliases:
            aliases.append(alias)
    return aliases


def _merge_legacy_ac_node_alias(
    nodes: dict[str, Any],
    *,
    canonical_id: str,
    aliases: list[str],
) -> None:
    """Move mixed-history root AC state from legacy aliases to ``canonical_id``."""
    canonical_node = nodes.get(canonical_id)
    if not isinstance(canonical_node, dict):
        canonical_node = {"id": canonical_id, "children_ids": []}
        nodes[canonical_id] = canonical_node

    canonical_children = list(canonical_node.get("children_ids", []))
    for alias in aliases:
        legacy_node = nodes.pop(alias, None)
        if not isinstance(legacy_node, dict):
            continue
        for child_id in legacy_node.get("children_ids", []):
            if child_id not in canonical_children:
                canonical_children.append(child_id)
            child = nodes.get(child_id)
            if isinstance(child, dict) and child.get("parent_id") == alias:
                child["parent_id"] = canonical_id
        for key, value in legacy_node.items():
            if key in {"id", "children_ids"}:
                continue
            canonical_node.setdefault(key, value)

    canonical_node["id"] = canonical_id
    canonical_node["children_ids"] = canonical_children


def _subtask_message_may_fallback_to_ac_index(message: SubtaskUpdated) -> bool:
    """Return whether AC-index fallback is safe for a subtask update."""
    return message.node_depth is None or message.node_depth <= 1


def _resolve_subtask_parent_id(
    nodes: dict[str, Any],
    message: SubtaskUpdated,
) -> str | None:
    """Resolve a Sub-AC parent from canonical, legacy, then index metadata."""
    candidates: list[str] = []
    for value in (
        message.parent_node_id,
        message.legacy_parent_node_id,
    ):
        candidate = _coerce_non_empty_string(value)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)

    for value in message.legacy_parent_node_aliases:
        candidate = _coerce_non_empty_string(value)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate in nodes:
            return candidate

    if not _subtask_message_may_fallback_to_ac_index(message):
        return None

    ac_indexes: list[int] = []
    for value in (message.ac_index, message.root_ac_number):
        ac_index = _coerce_int(value, 0)
        if ac_index > 0 and ac_index not in ac_indexes:
            ac_indexes.append(ac_index)

    if message.root_ac_index is not None and message.root_ac_index >= 0:
        root_ac_number = message.root_ac_index + 1
        if root_ac_number not in ac_indexes:
            ac_indexes.append(root_ac_number)

    for ac_index in ac_indexes:
        parent_id = _find_node_id_for_ac_index(nodes, ac_index)
        if parent_id is not None:
            return parent_id

    return None


class OuroborosTUI(App[None]):
    """Main Textual application for Ouroboros TUI."""

    TITLE = "Ouroboros TUI"
    SUB_TITLE = "Workflow Monitor"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $primary;
        color: $text;
        text-style: bold;
        dock: top;
        height: 3;
    }

    Footer {
        background: $surface;
        color: $text-muted;
        dock: bottom;
        height: 1;
    }

    .hidden {
        display: none;
    }

    /* Global scrollbar styling */
    *:focus {
        border: round $accent;
    }

    /* Ensure smooth transitions */
    * {
        transition: background 150ms;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("d", "show_debug", "Debug"),
        Binding("l", "show_logs", "Logs"),
        Binding("s", "show_selector", "Select Session"),
        Binding("e", "show_lineages", "Lineages"),
        Binding("1", "show_dashboard", "Dashboard", show=False),
        Binding("2", "show_execution", "Execution", show=False),
        Binding("3", "show_logs", "Logs", show=False),
        Binding("4", "show_debug", "Debug", show=False),
    ]

    def __init__(
        self,
        event_store: EventStore | None = None,
        *,
        execution_id: str | None = None,
        driver_class: type | None = None,
    ) -> None:
        """Initialize OuroborosTUI.

        Args:
            event_store: EventStore for live updates (optional for offline mode).
            execution_id: Optional execution ID to monitor initially.
            driver_class: Optional Textual driver class for testing.
        """
        super().__init__(driver_class=driver_class)
        self._event_store = event_store
        self._execution_id: str | None = execution_id
        self._state = TUIState()
        # Provider identity ledger — the SAME derivation reduce_board uses for the
        # web Kanban, folded incrementally (O(1) per event). It wraps the state's
        # ``provider_by_node`` dict so folds merge in place, never replace it.
        self._provider_ledger = ProviderLedger(provider_by_node=self._state.provider_by_node)
        self._subscription_task: asyncio.Task[None] | None = None
        self._subscription_generation = 0
        self._poll_interval_seconds = 0.5
        self._is_paused = False
        self._pause_callback: Any | None = None
        self._resume_callback: Any | None = None

    @property
    def state(self) -> TUIState:
        """Get current TUI state."""
        return self._state

    def on_mount(self) -> None:
        """Handle application mount."""
        # Install screens - session/lineage selectors only if event_store is available
        if self._event_store is not None:
            self.install_screen(SessionSelectorScreen(self._event_store), name="session_selector")
            self.install_screen(LineageSelectorScreen(self._event_store), name="lineage_selector")
        self.install_screen(DashboardScreenV3(self._state), name="dashboard")
        self.install_screen(ExecutionScreen(self._state), name="execution")
        self.install_screen(LogsScreen(self._state), name="logs")
        self.install_screen(DebugScreen(self._state), name="debug")

        # Start with session selector if available, otherwise dashboard
        if self._event_store is not None:
            self.push_screen("session_selector")
        else:
            self.push_screen("dashboard")

    async def on_session_selector_screen_session_selected(
        self, message: SessionSelectorScreen.SessionSelected
    ) -> None:
        """Handle session selection and switch to the dashboard."""
        self.set_execution(message.execution_id, message.session_id)
        self.push_screen("dashboard")

    def _start_event_subscription(self) -> None:
        """Start background task for event subscription."""
        # Skip if no event loop (e.g., during testing) or no event store
        if self._event_store is None or not self._execution_id:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # No event loop running
        self._subscription_generation += 1
        context = _EventSubscriptionContext(
            execution_id=self._execution_id,
            session_id=self._state.session_id,
            generation=self._subscription_generation,
        )
        if self._subscription_task is not None:
            self._subscription_task.cancel()
        self._subscription_task = asyncio.create_task(self._subscribe_to_events(context))

    def _is_subscription_active(self, context: _EventSubscriptionContext) -> bool:
        """Return True when the polling task still matches the active context."""
        if self._subscription_generation != context.generation:
            return False
        if self._execution_id != context.execution_id:
            return False
        return not (context.session_id and self._state.session_id != context.session_id)

    def _process_subscription_event(self, event: BaseEvent) -> None:
        """Forward a subscribed event into TUI state and installed screens."""
        self._state.add_log(
            "info",
            "tui.events",
            f"Received: {event.type}",
            {"aggregate_id": event.aggregate_id},
        )
        # Also forward to logs screen
        try:
            logs_screen = self.get_screen("logs")
            if logs_screen and hasattr(logs_screen, "add_log"):
                logs_screen.add_log(
                    "info",
                    "tui.events",
                    f"Received: {event.type}",
                    {"aggregate_id": event.aggregate_id},
                )
        except Exception:
            pass

        message = create_message_from_event(event)
        if message is not None:
            self.post_message(message)
            self._update_state_from_event(event)

        # Fold provider identity through the SHARED board derivation (the exact
        # rules the web Kanban's reduce_board applies). Additive: it only annotates
        # provider tags and never touches pause/resume/log/debug flows above.
        self._ingest_board_event(event)

        # Forward raw event to debug screen
        try:
            debug_screen = self.get_screen("debug")
            if debug_screen and hasattr(debug_screen, "add_raw_event"):
                debug_screen.add_raw_event(
                    {
                        "type": event.type,
                        "aggregate_type": event.aggregate_type,
                        "aggregate_id": event.aggregate_id,
                        "data": event.data,
                        "timestamp": str(event.timestamp),
                    }
                )
        except Exception:
            pass  # Screen might not be installed yet

    def _ingest_board_event(self, event: BaseEvent) -> None:
        """Fold one event's provider identity through the shared board derivation.

        The TUI keeps its own hierarchical ``ac_tree`` for layout, but derives
        per-node PROVIDER (runtime_backend) via the exact same rules the web
        Kanban's ``reduce_board`` applies (``fold_provider_event`` is called by
        both) — so the two surfaces can never drift on who ran what. The fold is
        O(1) per event and mutates the ledger in place; no event list is kept.
        Rendering is refreshed ONLY when provider identity actually changed — a
        handful of times per run — never on node/status/tool chatter.
        """
        data = event.data if isinstance(event.data, dict) else {}
        if not fold_provider_event(event.type, data, ledger=self._provider_ledger):
            return

        self._state.board_providers[:] = self._provider_ledger.providers()
        # Stamp providers onto any tree nodes that already exist. Nodes created
        # later (by queued Textual messages) get stamped at their own
        # ``_notify_ac_tree_updated`` via ``_apply_provider_tags``.
        self._notify_ac_tree_updated()

    async def _subscribe_to_events(self, context: _EventSubscriptionContext) -> None:
        """Subscribe to EventStore for live updates.

        Uses incremental fetching via query_session_related_events_after() to
        avoid replaying the full event history on every poll cycle, keeping each
        poll at O(new_events) instead of O(total_events).

        The related-events query matches the session/execution aggregates AND any
        worker-scoped aggregate whose payload references this run's session_id /
        execution_id (the exact predicates the web Kanban reader applies). This is
        what lets per-AC telemetry — model_routed / token_attribution and per-worker
        provider identity, all persisted under ``exec_<id>_node_<NODEID>`` scopes
        with the run id only in the payload — reach the HUD. A single rowid cursor
        advances across all matched scopes, so each event is delivered exactly once.
        """
        if self._event_store is None or not context.execution_id:
            return

        last_row_id = 0

        while self._is_subscription_active(context):
            try:
                (
                    new_events,
                    last_row_id,
                ) = await self._event_store.query_session_related_events_after(
                    session_id=context.session_id,
                    execution_id=context.execution_id,
                    last_row_id=last_row_id,
                )

                if new_events:
                    for event in sorted(new_events, key=lambda item: (item.timestamp, item.id)):
                        if not self._is_subscription_active(context):
                            break
                        self._process_subscription_event(event)

                await asyncio.sleep(self._poll_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._state.add_log("error", "tui.subscription", f"Event subscription error: {e}")
                try:
                    logs_screen = self.get_screen("logs")
                    if logs_screen and hasattr(logs_screen, "add_log"):
                        logs_screen.add_log(
                            "error", "tui.subscription", f"Event subscription error: {e}"
                        )
                except Exception:
                    pass
                await asyncio.sleep(self._poll_interval_seconds)

    def _update_state_from_event(self, event: BaseEvent) -> None:
        """Update internal state from an event."""
        event_type = event.type
        data = event.data

        if event_type == "orchestrator.session.started":
            self._state.execution_id = data.get("execution_id", "")
            self._state.session_id = event.aggregate_id
            self._state.status = "running"
        elif event_type == "orchestrator.session.completed":
            self._state.status = "completed"
            self._state.is_paused = False
        elif event_type == "orchestrator.session.failed":
            self._state.status = "failed"
            self._state.is_paused = False
        elif event_type == "orchestrator.session.cancelled":
            self._state.status = "cancelled"
            self._state.is_paused = False
        elif event_type == "execution.terminal":
            # Mirror event from the execution stream — ensures TUI sees
            # terminal transitions even when only polling "execution".
            terminal_status = data.get("status", "completed")
            if terminal_status in {"completed", "failed", "cancelled", "paused"}:
                self._state.status = terminal_status
                self._state.is_paused = terminal_status == "paused"
        elif event_type == "orchestrator.session.paused":
            self._state.status = "paused"
            self._state.is_paused = True
        elif event_type == "execution.phase.completed":
            self._state.current_phase = data.get("phase", "")
            self._state.iteration = data.get("iteration", 0)
        elif event_type == "observability.drift.measured":
            self._state.goal_drift = data.get("goal_drift", 0.0)
            self._state.constraint_drift = data.get("constraint_drift", 0.0)
            self._state.ontology_drift = data.get("ontology_drift", 0.0)
            self._state.combined_drift = data.get("combined_drift", 0.0)
        elif event_type == "observability.cost.updated":
            self._state.total_tokens = data.get("total_tokens", 0)
            self._state.total_cost_usd = data.get("total_cost_usd", 0.0)
        elif event_type == "ac.decomposition.completed":
            # Handle AC decomposition - add children to tree
            parent_ac_id = event.aggregate_id
            child_ac_ids = data.get("child_ac_ids", [])
            child_contents = data.get("child_contents", [])
            depth = data.get("depth", 0)

            # Update ac_tree with new children
            nodes = self._state.ac_tree.get("nodes", {})
            if parent_ac_id in nodes:
                # Update parent to show decomposed status
                nodes[parent_ac_id]["status"] = "decomposed"
                nodes[parent_ac_id]["children_ids"] = child_ac_ids

                # Add child nodes
                for _i, (child_id, child_content) in enumerate(
                    zip(child_ac_ids, child_contents, strict=False)
                ):
                    nodes[child_id] = {
                        "id": child_id,
                        "content": child_content,
                        "status": "pending",
                        "depth": depth + 1,
                        "parent_id": parent_ac_id,
                        "is_atomic": False,
                        "children_ids": [],
                    }

                self._state.ac_tree["nodes"] = nodes

                # Notify dashboard to update tree
                self._notify_ac_tree_updated()
        elif event_type == "ac.marked_atomic":
            # Handle AC marked as atomic
            ac_id = event.aggregate_id
            nodes = self._state.ac_tree.get("nodes", {})
            if ac_id in nodes:
                nodes[ac_id]["status"] = "atomic"
                nodes[ac_id]["is_atomic"] = True
                self._state.ac_tree["nodes"] = nodes
                self._notify_ac_tree_updated()

    def set_execution(self, execution_id: str, session_id: str = "") -> None:
        """Set the execution to monitor."""
        self._execution_id = execution_id
        self._state.execution_id = execution_id
        self._state.session_id = session_id
        self._state.status = "running"
        self._state.current_phase = ""
        self._state.iteration = 0
        self._state.goal_drift = 0.0
        self._state.constraint_drift = 0.0
        self._state.ontology_drift = 0.0
        self._state.combined_drift = 0.0
        self._state.total_tokens = 0
        self._state.total_cost_usd = 0.0
        self._state.is_paused = False
        self._state.ac_tree = {}
        self._state.logs = []
        self._state.active_tools.clear()
        self._state.tool_history.clear()
        self._state.thinking.clear()
        # Wipe folded provider identity for the next run. The ledger wraps the
        # state's provider_by_node dict, so resetting it clears both.
        self._provider_ledger.reset()
        self._state.board_providers.clear()
        # Wipe frugality telemetry so the next run starts from zero.
        self._state.tier_by_node.clear()
        self._state.model_by_node.clear()
        self._state.tokens_by_node.clear()
        self._state.run_total_tokens = 0.0
        self._state.frugality_summary = None
        self._state.frugality_retrospective = None
        self._state.frugality_retrospective_summary = None
        self._state.add_log("info", "tui.main", f"Monitoring execution: {execution_id}")
        # Forward to logs screen
        try:
            logs_screen = self.get_screen("logs")
            if logs_screen and hasattr(logs_screen, "add_log"):
                logs_screen.add_log("info", "tui.main", f"Monitoring execution: {execution_id}")
        except Exception:
            pass
        self._notify_ac_tree_updated()
        self._start_event_subscription()

    def action_show_selector(self) -> None:
        """Show session selector screen."""
        self.push_screen("session_selector")

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        self._state.execution_id = message.execution_id
        self._state.session_id = message.session_id
        self._state.status = message.status
        self._state.is_paused = message.status == "paused"
        self._forward_to_dashboard("on_execution_updated", message)

    def on_phase_changed(self, message: PhaseChanged) -> None:
        self._state.current_phase = message.current_phase
        self._state.iteration = message.iteration
        self._forward_to_dashboard("on_phase_changed", message)

    def on_drift_updated(self, message: DriftUpdated) -> None:
        self._state.goal_drift = message.goal_drift
        self._state.constraint_drift = message.constraint_drift
        self._state.ontology_drift = message.ontology_drift
        self._state.combined_drift = message.combined_drift
        self._forward_to_dashboard("on_drift_updated", message)

    def on_cost_updated(self, message: CostUpdated) -> None:
        self._state.total_tokens = message.total_tokens
        self._state.total_cost_usd = message.total_cost_usd
        self._forward_to_dashboard("on_cost_updated", message)

    def on_ac_updated(self, message: ACUpdated) -> None:
        if message.ac_id:
            nodes = self._state.ac_tree.get("nodes", {})
            if message.ac_id in nodes:
                nodes[message.ac_id]["status"] = message.status
                nodes[message.ac_id]["is_atomic"] = message.is_atomic
        self._forward_to_dashboard("on_ac_updated", message)

    def on_subtask_updated(self, message: SubtaskUpdated) -> None:
        """Handle sub-task updates and add to AC tree (SSOT)."""
        nodes = self._state.ac_tree.get("nodes", {})
        resolved_parent_id = _resolve_subtask_parent_id(nodes, message)
        parent_ac_id = (
            resolved_parent_id
            or message.parent_node_id
            or message.legacy_parent_node_id
            or (f"ac_{message.ac_index}" if message.ac_index > 0 else "")
        )
        sub_task_id = message.node_id or message.sub_task_id
        existing_node = nodes.get(sub_task_id, {})

        # Add or update subtask node
        subtask_node = {
            "id": sub_task_id,
            "content": message.content or existing_node.get("content", ""),
            "status": message.status,
            "depth": message.node_depth
            if message.node_depth is not None
            else existing_node.get("depth", 2),
            "parent_id": parent_ac_id,
            "is_atomic": True,
            "children_ids": existing_node.get("children_ids", []),
            "node_id": message.node_id,
            "path": message.path,
            "display_path": message.display_path,
            "ordinal": message.ordinal,
            "root_ac_index": message.root_ac_index,
            "identity_model": message.identity_model,
        }
        if message.current_tool_activity:
            subtask_node["current_tool_activity"] = dict(message.current_tool_activity)
        elif existing_node.get("current_tool_activity") is not None:
            subtask_node["current_tool_activity"] = existing_node["current_tool_activity"]

        if message.last_update:
            subtask_node["last_update"] = dict(message.last_update)
        elif existing_node.get("last_update") is not None:
            subtask_node["last_update"] = existing_node["last_update"]

        nodes[sub_task_id] = subtask_node

        # Update parent's children_ids (add if not present)
        previous_parent_id = existing_node.get("parent_id")
        if (
            isinstance(previous_parent_id, str)
            and previous_parent_id != parent_ac_id
            and previous_parent_id in nodes
        ):
            nodes[previous_parent_id]["children_ids"] = [
                child_id
                for child_id in nodes[previous_parent_id].get("children_ids", [])
                if child_id != sub_task_id
            ]
        if resolved_parent_id is not None and resolved_parent_id in nodes:
            parent_ac_id = resolved_parent_id
            parent = nodes[parent_ac_id]
            children = parent.get("children_ids", [])
            if sub_task_id not in children:
                children.append(sub_task_id)
                parent["children_ids"] = children
            parent["is_atomic"] = False

        self._state.ac_tree["nodes"] = nodes
        self._notify_ac_tree_updated()
        self._forward_to_dashboard("on_subtask_updated", message)

    def on_tool_call_started(self, message: ToolCallStarted) -> None:
        """Handle tool call started - track active tools."""
        self._state.active_tools[message.ac_id] = {
            "tool_name": message.tool_name,
            "tool_detail": message.tool_detail,
            "call_index": str(message.call_index),
        }
        self._notify_ac_tree_updated()
        self._forward_to_dashboard("on_tool_call_started", message)

    def on_tool_call_completed(self, message: ToolCallCompleted) -> None:
        """Handle tool call completed - move to history."""
        self._state.active_tools.pop(message.ac_id, None)
        history = self._state.tool_history.setdefault(message.ac_id, [])
        history.append(
            {
                "tool_name": message.tool_name,
                "tool_detail": message.tool_detail,
                "call_index": message.call_index,
                "duration_seconds": message.duration_seconds,
                "success": message.success,
            }
        )
        # Keep last 20 entries per AC
        if len(history) > 20:
            self._state.tool_history[message.ac_id] = history[-20:]
        self._forward_to_dashboard("on_tool_call_completed", message)

    def on_agent_thinking_updated(self, message: AgentThinkingUpdated) -> None:
        """Handle agent thinking update."""
        self._state.thinking[message.ac_id] = message.thinking_text
        self._forward_to_dashboard("on_agent_thinking_updated", message)

    @staticmethod
    def _telemetry_node_key(node_id: str | None, ac_index: int) -> str | None:
        """Resolve the tree-node key a frugality event should stamp.

        Mirrors the tree's own node-key resolution: the stable ``node_id`` when
        present, otherwise the ``ac_<index>`` fallback the tree uses for a bare AC.
        Returns None when neither is usable so we never stamp the wrong node.
        """
        if node_id:
            return node_id
        if ac_index >= 0:
            return f"ac_{ac_index}"
        return None

    def on_acmodel_routed(self, message: ACModelRouted) -> None:
        """Fold per-AC model-tier routing (latest-wins per node) into state."""
        key = self._telemetry_node_key(message.node_id, message.ac_index)
        if key is None:
            return
        if message.model_tier:
            self._state.tier_by_node[key] = message.model_tier
        if message.model:
            self._state.model_by_node[key] = message.model
        self._notify_ac_tree_updated()

    def on_actoken_attribution(self, message: ACTokenAttribution) -> None:
        """Fold per-AC token spend into state: per-node accumulate + run total."""
        if message.token_spend < 0:
            return
        self._state.run_total_tokens += message.token_spend
        key = self._telemetry_node_key(message.node_id, message.ac_index)
        if key is not None:
            self._state.tokens_by_node[key] = (
                self._state.tokens_by_node.get(key, 0.0) + message.token_spend
            )
        self._notify_ac_tree_updated()

    def on_frugality_proof_evaluated(self, message: FrugalityProofEvaluated) -> None:
        """Fold the run-end frugality verdict once, then hand it to the dashboard."""
        if self._state.frugality_summary is None:
            self._state.frugality_summary = format_frugality_summary(
                message.status, message.token_reduction_pct
            )
        self._forward_to_dashboard("on_frugality_proof_evaluated", message)

    def on_frugality_retrospective_reported(
        self,
        message: FrugalityRetrospectiveReported,
    ) -> None:
        """Fold the execution-finalized evidence report once for live display."""
        if self._state.frugality_retrospective is None:
            self._state.frugality_retrospective = dict(message.summary)
            self._state.frugality_retrospective_summary = format_frugality_retrospective_summary(
                message.summary
            )
        self._forward_to_dashboard("on_frugality_retrospective_reported", message)

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        # Update state with AC tree from workflow progress (smart merge)
        if message.acceptance_criteria:
            self._merge_ac_progress(
                message.acceptance_criteria,
                message.current_ac_index,
            )

        # Update cost/tokens in state
        self._state.total_tokens = message.estimated_tokens
        self._state.total_cost_usd = message.estimated_cost_usd

        # Update phase in state
        if message.current_phase:
            self._state.current_phase = message.current_phase.lower()

        # Forward to dashboard, execution, and debug screens
        self._forward_to_dashboard("on_workflow_progress_updated", message)

        for screen_name, method in (
            ("execution", "on_workflow_progress_updated"),
            ("debug", "update_state"),
        ):
            try:
                s = self.get_screen(screen_name)
                if s and hasattr(s, method):
                    arg = self._state if method == "update_state" else message
                    getattr(s, method)(arg)
            except Exception:
                pass

    def _convert_ac_list_to_tree(
        self,
        acceptance_criteria: list[dict[str, Any]],
        current_ac_index: int | None,
    ) -> dict[str, Any]:
        """Convert flat AC list to tree format for ACTreeWidget.

        Creates a simple tree with root node containing all ACs as children.

        Args:
            acceptance_criteria: List of AC dicts with index, content, status.
            current_ac_index: Index of current AC being worked on.

        Returns:
            Tree data dict with root_id and nodes.
        """
        nodes: dict[str, Any] = {}
        child_ids = []

        # Create root node
        root_id = "root"

        # Create child nodes for each AC
        for ac in acceptance_criteria:
            ac_index = ac.get("index", 0)
            ac_id = ac.get("node_id") or ac.get("ac_id") or f"ac_{ac_index}"
            child_ids.append(ac_id)

            # Map status from workflow to tree status
            status = ac.get("status", "pending")
            if status == "in_progress":
                status = "executing"
            elif status == "completed":
                status = "completed"
            else:
                status = "pending"

            nodes[ac_id] = {
                "id": ac_id,
                "content": ac.get("content", ""),
                "status": status,
                "depth": 1,
                "is_atomic": True,  # Flat list = all atomic
                "children_ids": [],
                "node_id": ac.get("node_id"),
                "path": ac.get("path", []),
                "display_path": ac.get("display_path"),
                "ordinal": ac.get("ordinal"),
                "root_ac_index": ac.get("root_ac_index"),
                "identity_model": ac.get("identity_model"),
            }

        # Create root node
        nodes[root_id] = {
            "id": root_id,
            "content": "Acceptance Criteria",
            "status": "executing" if current_ac_index is not None else "pending",
            "depth": 0,
            "is_atomic": False,
            "children_ids": child_ids,
        }

        return {
            "root_id": root_id,
            "nodes": nodes,
        }

    def _merge_ac_progress(
        self,
        acceptance_criteria: list[dict[str, Any]],
        current_ac_index: int | None,
    ) -> None:
        """Merge AC progress into existing tree, preserving subtree children.

        Unlike _convert_ac_list_to_tree which rebuilds from scratch,
        this method updates status of existing nodes while preserving
        children_ids and subtask nodes added by decomposition events.

        Args:
            acceptance_criteria: List of AC dicts with index, content, status.
            current_ac_index: Index of current AC being worked on.
        """
        existing_nodes = self._state.ac_tree.get("nodes", {})

        if not existing_nodes:
            # No existing tree - build from scratch
            self._state.ac_tree = self._convert_ac_list_to_tree(
                acceptance_criteria,
                current_ac_index,
            )
            self._notify_ac_tree_updated()
            return

        # Smart merge: update status/content but preserve children
        for ac in acceptance_criteria:
            ac_index = ac.get("index", 0)
            ac_id = ac.get("node_id") or ac.get("ac_id") or f"ac_{ac_index}"
            legacy_aliases = _legacy_ac_node_aliases(ac, ac_id)
            if legacy_aliases:
                _merge_legacy_ac_node_alias(
                    existing_nodes,
                    canonical_id=ac_id,
                    aliases=legacy_aliases,
                )

            status = ac.get("status", "pending")
            if status == "in_progress":
                status = "executing"
            elif status not in ("completed", "failed", "executing"):
                status = "pending"

            if ac_id in existing_nodes:
                # Update existing node - preserve children_ids and is_atomic
                existing_nodes[ac_id]["status"] = status
                existing_nodes[ac_id]["content"] = ac.get("content", "")
                existing_nodes[ac_id]["node_id"] = ac.get("node_id")
                existing_nodes[ac_id]["path"] = ac.get("path", [])
                existing_nodes[ac_id]["display_path"] = ac.get("display_path")
                existing_nodes[ac_id]["ordinal"] = ac.get("ordinal")
                existing_nodes[ac_id]["root_ac_index"] = ac.get("root_ac_index")
                existing_nodes[ac_id]["identity_model"] = ac.get("identity_model")
            else:
                # New AC node - add it
                existing_nodes[ac_id] = {
                    "id": ac_id,
                    "content": ac.get("content", ""),
                    "status": status,
                    "depth": 1,
                    "is_atomic": True,
                    "children_ids": [],
                    "node_id": ac.get("node_id"),
                    "path": ac.get("path", []),
                    "display_path": ac.get("display_path"),
                    "ordinal": ac.get("ordinal"),
                    "root_ac_index": ac.get("root_ac_index"),
                    "identity_model": ac.get("identity_model"),
                }

        # Ensure root exists and keep its children_ids in sync
        root_id = self._state.ac_tree.get("root_id", "root")
        expected_child_ids = [
            ac.get("node_id") or ac.get("ac_id") or f"ac_{ac.get('index', 0)}"
            for ac in acceptance_criteria
        ]

        if root_id not in existing_nodes:
            existing_nodes[root_id] = {
                "id": root_id,
                "content": "Acceptance Criteria",
                "status": "executing" if current_ac_index is not None else "pending",
                "depth": 0,
                "is_atomic": False,
                "children_ids": expected_child_ids,
            }
        else:
            existing_nodes[root_id]["status"] = (
                "executing" if current_ac_index is not None else "pending"
            )
            # Sync children_ids to the current canonical root AC identities.
            # This removes mixed-history legacy ``ac_<n>`` roots once a
            # node-aware progress snapshot introduces the canonical ``node_*``
            # ID, preventing duplicate root entries after resume replay.
            current_children = [
                child_id
                for child_id in existing_nodes[root_id].get("children_ids", [])
                if child_id in expected_child_ids
            ]
            for child_id in expected_child_ids:
                if child_id not in current_children:
                    current_children.append(child_id)
            existing_nodes[root_id]["children_ids"] = current_children

        self._state.ac_tree["nodes"] = existing_nodes
        self._notify_ac_tree_updated()

    def on_log_message(self, message: LogMessage) -> None:
        self._state.add_log(message.level, message.source, message.message, message.data)
        try:
            logs_screen = self.get_screen("logs")
            if logs_screen and hasattr(logs_screen, "add_log"):
                logs_screen.add_log(message.level, message.source, message.message, message.data)
        except Exception:
            pass  # Screen might not be ready

    def on_pause_requested(self, message: PauseRequested) -> None:
        self._state.is_paused = True
        self._state.status = "paused"
        if self._pause_callback is not None:
            asyncio.create_task(self._call_pause_callback(message.execution_id))
        self._state.add_log(
            "info", "tui.control", f"Pause requested for execution {message.execution_id}"
        )

    def on_resume_requested(self, message: ResumeRequested) -> None:
        self._state.is_paused = False
        self._state.status = "running"
        if self._resume_callback is not None:
            asyncio.create_task(self._call_resume_callback(message.execution_id))
        self._state.add_log(
            "info", "tui.control", f"Resume requested for execution {message.execution_id}"
        )

    async def _call_pause_callback(self, execution_id: str) -> None:
        if self._pause_callback is not None:
            try:
                result = self._pause_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log("error", "tui.control", f"Pause callback failed: {e}")

    async def _call_resume_callback(self, execution_id: str) -> None:
        if self._resume_callback is not None:
            try:
                result = self._resume_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log("error", "tui.control", f"Resume callback failed: {e}")

    def action_pause(self) -> None:
        if self._state.execution_id and not self._state.is_paused:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        if self._state.execution_id and self._state.is_paused:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_show_dashboard(self) -> None:
        self.switch_screen("dashboard")

    def action_show_execution(self) -> None:
        self.push_screen("execution")

    def action_show_logs(self) -> None:
        self.push_screen("logs")

    def action_show_debug(self) -> None:
        self.push_screen("debug")

    def action_show_lineages(self) -> None:
        """Show lineage selector screen."""
        if self._event_store is not None:
            self.push_screen("lineage_selector")
        else:
            self.notify("No event store available", severity="warning")

    async def on_lineage_selector_screen_lineage_selected(
        self, message: LineageSelectorScreen.LineageSelected
    ) -> None:
        """Handle lineage selection and push the detail screen."""
        assert self._event_store is not None
        from ouroboros.evolution.loop import EvolutionaryLoop
        from ouroboros.plugin.rewind import build_lockfile_rewind_observer

        rewind_committer = EvolutionaryLoop(
            self._event_store,
            rewind_observer=build_lockfile_rewind_observer(self._event_store),
        )
        self.push_screen(
            LineageDetailScreen(
                message.lineage,
                event_store=self._event_store,
                rewind_committer=rewind_committer,
            )
        )

    def set_pause_callback(self, callback: Any) -> None:
        self._pause_callback = callback

    def set_resume_callback(self, callback: Any) -> None:
        self._resume_callback = callback

    def update_ac_tree(self, tree_data: dict[str, Any]) -> None:
        self._state.ac_tree = tree_data
        self._notify_ac_tree_updated()

    def _get_dashboard_screen(self) -> DashboardScreenV3 | None:
        """Return the installed dashboard screen regardless of which screen is active."""
        try:
            screen = self.get_screen("dashboard")
            if isinstance(screen, DashboardScreenV3):
                return screen
        except Exception:
            pass
        return None

    def _forward_to_dashboard(self, method_name: str, message: Any) -> None:
        """Forward a message to the dashboard screen even when it's not active."""
        dashboard = self._get_dashboard_screen()
        if dashboard is not None and hasattr(dashboard, method_name):
            getattr(dashboard, method_name)(message)

    def _apply_provider_tags(self) -> None:
        """Stamp per-node provider onto ac_tree nodes from the shared derivation.

        Single choke point: whatever built/mutated the tree, the provider tag is
        applied here right before the widget renders, keyed by node_id — so a
        provider learned before OR after its node was created still lands.
        Resolution matches reduce_board's per-card rule exactly (per-worker
        provider wins, run-level backend is the fallback); the structural root is
        not a board card and is never tagged.
        """
        ledger = self._provider_ledger
        state = self._state
        if (
            not ledger.provider_by_node
            and not ledger.run_provider
            and not state.tier_by_node
            and not state.model_by_node
            and not state.tokens_by_node
        ):
            return
        nodes = self._state.ac_tree.get("nodes")
        if not isinstance(nodes, dict):
            return
        root_id = self._state.ac_tree.get("root_id", "root")
        for node_id, node in nodes.items():
            if node_id == root_id or not isinstance(node, dict):
                continue
            embedded_id = str(node.get("node_id") or "")
            provider = (
                ledger.provider_by_node.get(node_id)
                or ledger.provider_by_node.get(embedded_id)
                or ledger.run_provider
            )
            if provider:
                node["provider"] = provider
            # Frugality telemetry — identical resolution to provider (node_id key,
            # then the embedded node_id), so a tier/model/token learned before OR
            # after the node was created still lands on it.
            tier = state.tier_by_node.get(node_id) or state.tier_by_node.get(embedded_id)
            if tier:
                node["model_tier"] = tier
            model = state.model_by_node.get(node_id) or state.model_by_node.get(embedded_id)
            if model:
                node["model"] = model
            tokens = state.tokens_by_node.get(node_id)
            if tokens is None:
                tokens = state.tokens_by_node.get(embedded_id)
            if tokens is not None:
                node["tokens"] = tokens

    def _notify_ac_tree_updated(self) -> None:
        """Notify dashboard that AC tree has been updated."""
        self._apply_provider_tags()
        dashboard = self._get_dashboard_screen()
        if dashboard is not None and hasattr(dashboard, "_tree") and dashboard._tree is not None:
            dashboard._tree.update_tree(self._state.ac_tree)

    async def on_unmount(self) -> None:
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscription_task
        if self._event_store is not None:
            await self._event_store.close()


__all__ = ["OuroborosTUI"]
