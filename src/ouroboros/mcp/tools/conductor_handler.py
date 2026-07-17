"""MCP audit surface for Active Conductor decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.core.conductor import (
    MAX_RECEIPT_BYTES,
    ConductorActorMode,
    ConductorDecisionPhase,
    ConductorDirective,
    ConductorEffect,
    EngineOwnershipState,
)
from ouroboros.core.types import Result
from ouroboros.events.conductor import (
    create_conductor_decision_selected_event,
    create_conductor_decision_terminal_event,
)
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore

_TOOL_NAME = "ouroboros_record_conductor_decision"
_MAX_MUTATING_PER_ATTENTION = 1
_MAX_MUTATING_PER_ROOT_JOB = 2


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required and must be a non-empty string")
    return value.strip()


def _optional_text(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when provided")
    return value.strip()


def _string_array(arguments: dict[str, Any], name: str) -> tuple[str, ...]:
    value = arguments.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(value)


@dataclass(slots=True)
class RecordConductorDecisionHandler:
    """Persist idempotent selected and terminal conductor decision events."""

    event_store: EventStore

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=_TOOL_NAME,
            description=(
                "Record an audited Active Conductor decision. Record phase=selected before "
                "an action, then completed, failed, or declined after the outcome. Mutating "
                "successors require engine_ownership_state=closed."
            ),
            parameters=(
                MCPToolParameter(
                    "decision_id", ToolInputType.STRING, "Stable idempotent decision ID."
                ),
                MCPToolParameter(
                    "phase",
                    ToolInputType.STRING,
                    "Decision phase.",
                    enum=tuple(phase.value for phase in ConductorDecisionPhase),
                ),
                MCPToolParameter(
                    "attention_event_id",
                    ToolInputType.STRING,
                    "Source attention relay/event ID; required for selected.",
                    required=False,
                ),
                MCPToolParameter(
                    "evidence_event_ids",
                    ToolInputType.ARRAY,
                    "Bounded evidence event IDs; required for selected.",
                    required=False,
                ),
                MCPToolParameter(
                    "verification_summary",
                    ToolInputType.STRING,
                    "Short read-only verifier conclusion; required for selected.",
                    required=False,
                ),
                MCPToolParameter(
                    "selected_action",
                    ToolInputType.STRING,
                    "Selected menu action; required for selected.",
                    required=False,
                ),
                MCPToolParameter(
                    "selected_effect",
                    ToolInputType.STRING,
                    "Action effect class; required for selected.",
                    required=False,
                    enum=tuple(effect.value for effect in ConductorEffect),
                ),
                MCPToolParameter(
                    "actor_mode",
                    ToolInputType.STRING,
                    "Host policy mode; required for selected.",
                    required=False,
                    enum=tuple(mode.value for mode in ConductorActorMode),
                ),
                MCPToolParameter(
                    "engine_ownership_state",
                    ToolInputType.STRING,
                    "Authoritative ownership state from the attention envelope.",
                    required=False,
                    enum=tuple(state.value for state in EngineOwnershipState),
                ),
                MCPToolParameter(
                    "action_arguments",
                    ToolInputType.OBJECT,
                    "Action arguments; only a digest and bounded key list are persisted.",
                    required=False,
                ),
                MCPToolParameter(
                    "conductor_directive",
                    ToolInputType.OBJECT,
                    "Optional bounded corrective successor directive.",
                    required=False,
                ),
                MCPToolParameter(
                    "root_job_id",
                    ToolInputType.STRING,
                    "Root job used for the two-successor budget.",
                    required=False,
                ),
                MCPToolParameter(
                    "predecessor_execution_id",
                    ToolInputType.STRING,
                    "Execution that the selected successor will follow.",
                    required=False,
                ),
                MCPToolParameter(
                    "user_approval_event_id",
                    ToolInputType.STRING,
                    "Required approval receipt for specification changes.",
                    required=False,
                ),
                MCPToolParameter(
                    "result_receipt",
                    ToolInputType.STRING,
                    "Bounded outcome receipt or failure/decline reason for terminal phases.",
                    required=False,
                ),
                MCPToolParameter(
                    "successor_execution_id",
                    ToolInputType.STRING,
                    "New execution ID returned by a completed successor action.",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        try:
            await self.event_store.initialize()
            decision_id = _required_text(arguments, "decision_id")
            phase = ConductorDecisionPhase(_required_text(arguments, "phase"))
            existing = await self.event_store.replay("conductor_decision", decision_id)
            if phase is ConductorDecisionPhase.SELECTED:
                event = await self._selected_event(arguments, decision_id, existing)
                if existing:
                    return await self._result(event, replayed=True, selected_event=event)
                await self.event_store.append(event)
                return await self._result(event, replayed=False, selected_event=event)
            event = self._terminal_event(arguments, decision_id, phase, existing)
            terminal = next(
                (
                    item
                    for item in existing
                    if item.type.startswith("conductor.decision.")
                    and item.type != "conductor.decision.selected"
                ),
                None,
            )
            selected = next(
                (item for item in existing if item.type == "conductor.decision.selected"),
                None,
            )
            if terminal is not None:
                if terminal.type != event.type or terminal.data.get(
                    "outcome_digest"
                ) != event.data.get("outcome_digest"):
                    raise ValueError("decision_id already has a different terminal outcome")
                return await self._result(terminal, replayed=True, selected_event=selected)
            await self.event_store.append(event)
            return await self._result(event, replayed=False, selected_event=selected)
        except (TypeError, ValueError) as exc:
            return Result.err(MCPToolError(str(exc), tool_name=_TOOL_NAME))
        except Exception as exc:  # noqa: BLE001 - preserve MCP boundary.
            return Result.err(
                MCPToolError(f"Conductor decision audit failed: {exc}", tool_name=_TOOL_NAME)
            )

    async def _selected_event(
        self,
        arguments: dict[str, Any],
        decision_id: str,
        existing: list[Any],
    ) -> Any:
        effect = ConductorEffect(_required_text(arguments, "selected_effect"))
        actor_mode = ConductorActorMode(_required_text(arguments, "actor_mode"))
        ownership = EngineOwnershipState(_required_text(arguments, "engine_ownership_state"))
        approval_id = _optional_text(arguments, "user_approval_event_id")
        if effect.mutates and ownership is not EngineOwnershipState.CLOSED:
            raise ValueError("Mutating conductor actions require engine_ownership_state=closed")
        predecessor_execution_id = _optional_text(arguments, "predecessor_execution_id")
        if effect.mutates and predecessor_execution_id is None:
            raise ValueError("Mutating conductor actions require predecessor_execution_id")
        if effect is ConductorEffect.SPECIFICATION_CHANGE and approval_id is None:
            raise ValueError("Specification-changing conductor actions require user approval")
        raw_directive = arguments.get("conductor_directive")
        directive = (
            ConductorDirective.from_mapping(raw_directive)
            if isinstance(raw_directive, dict)
            else None
        )
        if raw_directive is not None and directive is None:
            raise TypeError("conductor_directive must be an object")
        if effect.mutates and directive is None:
            raise ValueError("Mutating conductor actions require conductor_directive")
        if directive is not None:
            directive.validate_actor_policy(actor_mode)
            if not directive.is_non_relaxing and effect is not ConductorEffect.SPECIFICATION_CHANGE:
                raise ValueError(
                    "A relaxing directive requires selected_effect=specification_change"
                )
            if (
                effect is ConductorEffect.SPECIFICATION_CHANGE
                and (directive.user_approval_event_id or approval_id) is None
            ):
                raise ValueError("Specification-changing directive requires user approval")
            if (
                effect is ConductorEffect.SPECIFICATION_CHANGE
                and directive.user_approval_event_id != approval_id
            ):
                raise ValueError(
                    "Specification-changing directive approval must match user_approval_event_id"
                )
        event = create_conductor_decision_selected_event(
            decision_id=decision_id,
            attention_event_id=_required_text(arguments, "attention_event_id"),
            evidence_event_ids=_string_array(arguments, "evidence_event_ids"),
            verification_summary=_required_text(arguments, "verification_summary"),
            selected_action=_required_text(arguments, "selected_action"),
            selected_effect=effect,
            actor_mode=actor_mode,
            engine_ownership_state=ownership,
            action_arguments=arguments.get("action_arguments"),
            root_job_id=_optional_text(arguments, "root_job_id"),
            predecessor_execution_id=predecessor_execution_id,
            conductor_directive=directive,
            user_approval_event_id=approval_id,
        )
        if existing:
            selected = next((item for item in existing if item.type == event.type), None)
            if selected is None or selected.data.get("selection_digest") != event.data.get(
                "selection_digest"
            ):
                raise ValueError("decision_id already belongs to a different selection")
            return selected
        if effect.mutates:
            await self._enforce_budget(event)
        return event

    async def _enforce_budget(self, selected_event: Any) -> None:
        selected_events = await self.event_store.query_events(
            event_type="conductor.decision.selected",
            limit=10_000,
        )
        attention_id = selected_event.data.get("attention_event_id")
        root_job_id = selected_event.data.get("root_job_id")
        mutating = [event for event in selected_events if event.data.get("mutating") is True]
        if (
            sum(event.data.get("attention_event_id") == attention_id for event in mutating)
            >= _MAX_MUTATING_PER_ATTENTION
        ):
            raise ValueError("Conductor successor budget exhausted for this attention event")
        if (
            root_job_id
            and sum(event.data.get("root_job_id") == root_job_id for event in mutating)
            >= _MAX_MUTATING_PER_ROOT_JOB
        ):
            raise ValueError("Conductor successor budget exhausted for this root job")

    @staticmethod
    def _terminal_event(
        arguments: dict[str, Any],
        decision_id: str,
        phase: ConductorDecisionPhase,
        existing: list[Any],
    ) -> Any:
        selected = next(
            (event for event in existing if event.type == "conductor.decision.selected"),
            None,
        )
        if selected is None:
            raise ValueError("Record phase=selected before a terminal conductor outcome")
        successor_execution_id = _optional_text(arguments, "successor_execution_id")
        if (
            phase is ConductorDecisionPhase.COMPLETED
            and selected.data.get("mutating") is True
            and successor_execution_id is None
        ):
            raise ValueError(
                "Completed mutating conductor decisions require successor_execution_id"
            )
        return create_conductor_decision_terminal_event(
            decision_id=decision_id,
            phase=phase,
            result_receipt=_optional_text(arguments, "result_receipt"),
            successor_execution_id=successor_execution_id,
        )

    async def _trust_escalation_summary(self, execution_id: str) -> str | None:
        """Bounded Task 1/2 summary for the AC(s) this decision followed.

        Queries the SAME durable events Kanban/HUD already read
        (``execution.ac.decomposition_attested`` / ``execution.ac.parked_for_operator``),
        scoped to ``execution_id`` (the execution the selected successor
        follows) — the concrete link between this conductor decision and the
        AC(s) it is "relevant for". Fails closed to ``None`` on any query
        error so an audit-surface hiccup never fails the decision recording
        itself (the caller's ``try`` around this whole method already treats
        any raised exception as a hard failure of the MCP call).
        """
        try:
            attested = await self.event_store.query_events(
                aggregate_id=execution_id,
                event_type="execution.ac.decomposition_attested",
                limit=200,
            )
            parked = await self.event_store.query_events(
                aggregate_id=execution_id,
                event_type="execution.ac.parked_for_operator",
                limit=200,
            )
        except Exception:  # noqa: BLE001 - optional audit enrichment, never fatal.
            return None

        untrustworthy_nodes = {
            item.data.get("node_id")
            for item in attested
            if isinstance(item.data, dict) and item.data.get("trustworthy") is False
        }
        parked_nodes = {
            item.data.get("node_id")
            for item in parked
            if isinstance(item.data, dict) and item.data.get("node_id")
        }
        parts: list[str] = []
        if untrustworthy_nodes:
            plural = "s" if len(untrustworthy_nodes) != 1 else ""
            parts.append(f"{len(untrustworthy_nodes)} untrustworthy decomposition{plural}")
        if parked_nodes:
            plural = "s" if len(parked_nodes) != 1 else ""
            parts.append(f"{len(parked_nodes)} AC{plural} parked for operator")
        if not parts:
            return None

        summary = " · ".join(parts)
        encoded = summary.encode("utf-8")
        if len(encoded) > MAX_RECEIPT_BYTES:
            summary = encoded[: MAX_RECEIPT_BYTES - 3].decode("utf-8", errors="ignore") + "..."
        return summary

    async def _result(
        self,
        event: Any,
        *,
        replayed: bool,
        selected_event: Any | None,
    ) -> Result[MCPToolResult, MCPServerError]:
        phase = event.type.rsplit(".", 1)[-1]
        selected_data = selected_event.data if selected_event is not None else {}
        verification_summary = (
            selected_data.get("verification_summary") if isinstance(selected_data, dict) else None
        )
        selected_action = (
            selected_data.get("selected_action") if isinstance(selected_data, dict) else None
        )
        predecessor_execution_id = (
            selected_data.get("predecessor_execution_id")
            if isinstance(selected_data, dict)
            else None
        )
        trust_escalation_summary = (
            await self._trust_escalation_summary(predecessor_execution_id)
            if isinstance(predecessor_execution_id, str) and predecessor_execution_id
            else None
        )

        text_lines = [
            f"Conductor decision {event.aggregate_id} recorded as {phase}."
            + (" Existing idempotent receipt returned." if replayed else "")
        ]
        if selected_action:
            text_lines.append(f"Selected action: {selected_action}")
        if verification_summary:
            text_lines.append(f"Verification summary: {verification_summary}")
        if trust_escalation_summary:
            text_lines.append(f"Trust/escalation: {trust_escalation_summary}")

        meta: dict[str, Any] = {
            "decision_id": event.aggregate_id,
            "phase": phase,
            "event_id": event.id,
            "replayed": replayed,
            "successor_execution_id": event.data.get("successor_execution_id"),
        }
        if selected_action:
            meta["selected_action"] = selected_action
        if verification_summary:
            meta["verification_summary"] = verification_summary
        if trust_escalation_summary:
            meta["trust_escalation_summary"] = trust_escalation_summary

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="\n".join(text_lines),
                    ),
                ),
                is_error=phase == ConductorDecisionPhase.FAILED.value,
                meta=meta,
            )
        )


__all__ = ["RecordConductorDecisionHandler"]
