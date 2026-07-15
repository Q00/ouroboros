"""Public MCP discovery and delivery handlers for Ouroboros Synapse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect
from typing import Any

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalContractEffect,
    SessionSignalMode,
    SessionSignalSource,
    derive_session_signal_id,
)
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.synapse import SessionSignalHub, SessionSignalMailbox

_TOOL_NAME = "ouroboros_session_signal"


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


def _optional_positive_int(arguments: dict[str, Any], name: str) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer when provided")
    return value


def _optional_datetime(arguments: dict[str, Any], name: str) -> datetime | None:
    value = _optional_text(arguments, name)
    if value is None:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    return parsed


@dataclass(slots=True)
class SynapseSignalHandler:
    """Validate a public SessionSignal request and delegate to the durable mailbox."""

    mailbox: SessionSignalMailbox

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=_TOOL_NAME,
            description=(
                "Send one audited Ouroboros Synapse intent signal to an exact active "
                "AC session attempt. A queued result does not mean the signal was applied."
            ),
            parameters=(
                MCPToolParameter(
                    name="target_session_scope_id",
                    type=ToolInputType.STRING,
                    description="Stable logical AC session scope ID.",
                ),
                MCPToolParameter(
                    name="target_session_attempt_id",
                    type=ToolInputType.STRING,
                    description="Exact runtime attempt ID; stale attempts fail closed.",
                ),
                MCPToolParameter(
                    name="expected_execution_id",
                    type=ToolInputType.STRING,
                    description="Execution generation guard.",
                ),
                MCPToolParameter(
                    name="mode",
                    type=ToolInputType.STRING,
                    description="Requested Synapse delivery mode.",
                    enum=tuple(mode.value for mode in SessionSignalMode),
                ),
                MCPToolParameter(
                    name="fallback_mode",
                    type=ToolInputType.STRING,
                    description="Explicit redirect fallback; only after_turn is valid.",
                    required=False,
                    enum=(SessionSignalMode.AFTER_TURN.value,),
                ),
                MCPToolParameter(
                    name="message",
                    type=ToolInputType.STRING,
                    description="Bounded additive implementation intent; no secrets or transcripts.",
                ),
                MCPToolParameter(
                    name="source",
                    type=ToolInputType.STRING,
                    description="Audited source authority.",
                    enum=tuple(source.value for source in SessionSignalSource),
                ),
                MCPToolParameter(
                    name="contract_effect",
                    type=ToolInputType.STRING,
                    description=(
                        "Whether this preserves the approved shared contract or changes "
                        "the goal, ACs, constraints, or non-goals."
                    ),
                    required=False,
                    default=SessionSignalContractEffect.ADDITIVE.value,
                    enum=tuple(effect.value for effect in SessionSignalContractEffect),
                ),
                MCPToolParameter(
                    name="reason",
                    type=ToolInputType.STRING,
                    description="Short user-visible rationale.",
                ),
                MCPToolParameter(
                    name="idempotency_key",
                    type=ToolInputType.STRING,
                    description="Stable key for this exact execution/scope/attempt intent.",
                ),
                MCPToolParameter(
                    name="expires_at",
                    type=ToolInputType.STRING,
                    description="Optional timezone-aware ISO-8601 expiry.",
                    required=False,
                ),
                MCPToolParameter(
                    name="user_approval_event_id",
                    type=ToolInputType.STRING,
                    description="Required approval receipt for replace mode.",
                    required=False,
                ),
                MCPToolParameter(
                    name="expected_contract_version",
                    type=ToolInputType.INTEGER,
                    description="Optional shared execution-contract generation guard.",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        try:
            target_scope = _required_text(arguments, "target_session_scope_id")
            target_attempt = _required_text(arguments, "target_session_attempt_id")
            execution_id = _required_text(arguments, "expected_execution_id")
            idempotency_key = _required_text(arguments, "idempotency_key")
            mode = SessionSignalMode(_required_text(arguments, "mode"))
            raw_fallback = _optional_text(arguments, "fallback_mode")
            fallback = SessionSignalMode(raw_fallback) if raw_fallback is not None else None
            source = SessionSignalSource(_required_text(arguments, "source"))

            signal = SessionSignal(
                signal_id=derive_session_signal_id(
                    expected_execution_id=execution_id,
                    target_session_scope_id=target_scope,
                    target_session_attempt_id=target_attempt,
                    idempotency_key=idempotency_key,
                ),
                target_session_scope_id=target_scope,
                target_session_attempt_id=target_attempt,
                expected_execution_id=execution_id,
                mode=mode,
                fallback_mode=fallback,
                message=_required_text(arguments, "message"),
                source=source,
                contract_effect=SessionSignalContractEffect(
                    arguments.get(
                        "contract_effect",
                        SessionSignalContractEffect.ADDITIVE.value,
                    )
                ),
                reason=_required_text(arguments, "reason"),
                idempotency_key=idempotency_key,
                expires_at=_optional_datetime(arguments, "expires_at"),
                user_approval_event_id=_optional_text(
                    arguments,
                    "user_approval_event_id",
                ),
                expected_contract_version=_optional_positive_int(
                    arguments,
                    "expected_contract_version",
                ),
            )
            projection = await self.mailbox.request(signal)
        except (TypeError, ValueError) as exc:
            return Result.err(MCPToolError(str(exc), tool_name=_TOOL_NAME))
        except Exception as exc:  # noqa: BLE001 - preserve the MCP error boundary.
            return Result.err(
                MCPToolError(
                    f"Synapse request failed: {exc}",
                    tool_name=_TOOL_NAME,
                )
            )

        effective = projection.effective_mode.value if projection.effective_mode else None
        if projection.state.value == "queued":
            text = (
                f"SessionSignal {projection.signal_id} is durably queued"
                f" with effective mode {effective}. Application is not yet proven."
            )
        elif projection.state.value == "completed":
            text = f"SessionSignal {projection.signal_id} completed."
            if projection.reply:
                text = f"{text}\n\nAC reply: {projection.reply}"
        else:
            text = (
                f"SessionSignal {projection.signal_id} finished request handling"
                f" with state {projection.state.value}."
            )

        is_error = projection.state.value in {"rejected", "delivery_uncertain"}
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=is_error,
                meta={
                    "signal_id": projection.signal_id,
                    "state": projection.state.value,
                    "requested_mode": projection.requested_mode.value,
                    "contract_effect": projection.contract_effect.value,
                    "effective_mode": effective,
                    "target_session_scope_id": projection.target_session_scope_id,
                    "target_session_attempt_id": projection.target_session_attempt_id,
                    "expected_execution_id": projection.expected_execution_id,
                    "idempotency_key": projection.idempotency_key,
                    "application_proven": projection.state.value in {"applied", "completed"},
                    "reply": projection.reply,
                    "summary": projection.summary,
                },
            )
        )


@dataclass(slots=True)
class SynapseTargetsHandler:
    """Expose exact live AC attempts so the main session can select semantically."""

    hub: SessionSignalHub

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_session_signal_targets",
            description=(
                "List exact active AC attempts for one execution before sending a "
                "Synapse signal. The main session should match the user's intent to "
                "AC content and ask only when multiple candidates remain genuinely ambiguous."
            ),
            parameters=(
                MCPToolParameter(
                    name="execution_id",
                    type=ToolInputType.STRING,
                    description="Execution ID returned by run/auto start or its job observer.",
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        try:
            execution_id = _required_text(arguments, "execution_id")
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name=self.definition.name))

        discovered = self.hub.list_targets(execution_id=execution_id)
        targets = await discovered if inspect.isawaitable(discovered) else discovered
        target_data = [target.to_discovery_data() for target in targets]
        if not targets:
            text = f"No active AC attempts found for {execution_id}."
        else:
            lines = [f"Active Synapse targets for {execution_id}: {len(targets)}"]
            for target in targets:
                label = target.display_label or target.display_path or target.ac_id
                content = target.ac_content or "(AC content unavailable)"
                modes: list[str] = []
                if target.capabilities.after_turn_delivery:
                    modes.append("after_turn")
                if target.capabilities.checkpoint_redirect:
                    modes.append("redirect")
                if target.capabilities.inform_delivery:
                    modes.append("inform")
                if target.capabilities.owned_turn_abort and target.capabilities.replacement_resume:
                    modes.append("replace")
                lines.append(
                    f"- {label or target.session_scope_id}: {content} "
                    f"[modes: {', '.join(modes) if modes else 'none'}]"
                )
            text = "\n".join(lines)

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "execution_id": execution_id,
                    "active_target_count": len(target_data),
                    "targets": target_data,
                    "selection_policy": (
                        "match_user_intent_to_ac_content; ask_only_if_genuinely_ambiguous"
                    ),
                },
            )
        )


__all__ = ["SynapseSignalHandler", "SynapseTargetsHandler"]
