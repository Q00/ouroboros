"""Explicit consent and lookup for non-authoritative local Seed references."""

import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.core.types import Result
from ouroboros.interview_adapters.inline_materials import MaterialEvidenceDecision
from ouroboros.interview_adapters.material_evidence import (
    confirmation_prompt,
    draft_digest,
    item_text,
    revision,
    source_digest,
    verified_active,
)
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

if TYPE_CHECKING:
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

_KEYS = {
    "material_evidence_action",
    "material_evidence_kind",
    "material_evidence_index",
    "material_evidence_revision",
    "material_evidence_prompt",
    "material_evidence_response",
    "material_evidence_source_id",
    "material_evidence_id",
}


def _error(message: str) -> Result[MCPToolResult, MCPServerError]:
    return Result.err(MCPToolError(message, tool_name="ouroboros_interview"))


def _resolve_prompt(session_id: str, source_id: str, evidence_id: str) -> str:
    return (
        f"Show local material reference {evidence_id} from source {source_id} in session {session_id}? "
        "The approved excerpt or model assumption will be returned to the calling host, "
        "which may display or transmit it elsewhere. It is not verified truth or a requirement. "
        "Reply exactly SHOW to continue."
    )


def _unknown() -> Result[MCPToolResult, MCPServerError]:
    return Result.ok(
        MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=(
                        "Material reference unavailable or no longer approved; its content and truth are unknown."
                    ),
                ),
            ),
            meta={"material_evidence_status": "unknown"},
        )
    )


async def dispatch_material_evidence(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError] | None:
    if not any(arguments.get(key) is not None for key in _KEYS):
        return None
    allowed = _KEYS | {"session_id", "_normalized_interview_turn_context"}
    if any(key not in allowed and value is not None for key, value in arguments.items()):
        return _error("Material evidence actions cannot be combined with other interview inputs")
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if should_dispatch_via_plugin(handler.agent_runtime_backend, handler.opencode_mode):
        return _error("Material evidence requires in-process handling")
    action = arguments.get("material_evidence_action")
    session_id = arguments.get("session_id")
    if (
        action not in ("include", "remove", "resolve")
        or not isinstance(session_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None
    ):
        return _error("Provide a valid session and material evidence action")
    if action == "resolve":
        if any(
            arguments.get(key) is not None
            for key in (
                "material_evidence_kind",
                "material_evidence_index",
                "material_evidence_revision",
            )
        ):
            return _error("Resolution cannot include a decision")
        source_id, evidence_id = (
            arguments.get("material_evidence_source_id"),
            arguments.get("material_evidence_id"),
        )
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
            for value in (source_id, evidence_id)
        ):
            return _error("Provide valid material reference IDs")
        expected = _resolve_prompt(session_id, source_id, evidence_id)
        prompt, response = (
            arguments.get("material_evidence_prompt"),
            arguments.get("material_evidence_response"),
        )
        if prompt is None and response is None:
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=expected),),
                    meta={
                        "material_evidence_status": "confirmation_required",
                        "material_evidence_prompt": expected,
                    },
                )
            )
        if prompt != expected or response != "SHOW":
            return _error("Show the exact material retrieval prompt and provide SHOW")
    engine, _ = handler._create_interview_engine()
    loaded = await engine.load_state(session_id)
    if loaded.is_err or loaded.value.interview_id != session_id:
        return _unknown() if action == "resolve" else _error("Could not load material session")
    state = loaded.value
    source = state.inline_material
    if source is None or source.draft is None:
        return _unknown() if action == "resolve" else _error("No material draft is available")
    active = verified_active(source, session_id)
    if active is None:
        return _unknown() if action == "resolve" else _error("Material evidence history is invalid")
    if action == "resolve":
        event = next(
            (
                event
                for event in active.values()
                if event.source_id == source_id and event.evidence_id == evidence_id
            ),
            None,
        )
        if event is None:
            return _unknown()
        text = item_text(source, event.kind, event.index)
        label = (
            "Source quotation" if event.kind == "source_quote" else "Unverified model assumption"
        )
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"{label}: {text}\n"
                            f"Recorded inclusion prompt: {event.presented_prompt}\n"
                            f"Recorded inclusion response: {event.user_response}. "
                            "The host reported a local-reference approval, not confirmation of truth or a requirement. "
                            "The host sees this content and may transmit it elsewhere."
                        ),
                    ),
                ),
                meta={"material_evidence_status": "resolved", "material_evidence_kind": event.kind},
            )
        )
    kind, index = arguments.get("material_evidence_kind"), arguments.get("material_evidence_index")
    previous = arguments.get("material_evidence_revision")
    prompt, response = (
        arguments.get("material_evidence_prompt"),
        arguments.get("material_evidence_response"),
    )
    if (
        kind not in ("source_quote", "model_assumption")
        or type(index) is not int
        or item_text(source, kind, index) is None
        or not isinstance(previous, str)
        or re.fullmatch(r"[0-9a-f]{64}", previous) is None
        or prompt
        != confirmation_prompt(
            source, kind, index, action, session_id=session_id, revision_token=previous
        )
        or response != action.upper()
        or arguments.get("material_evidence_source_id") is not None
        or arguments.get("material_evidence_id") is not None
    ):
        return _error("Show the exact material confirmation and provide the exact decision")
    if previous != revision(source):
        if source.evidence_decisions:
            last = source.evidence_decisions[-1]
            if (
                last.previous_revision == previous
                and last.kind == kind
                and last.index == index
                and last.action == action
                and last.presented_prompt == prompt
                and last.user_response == response
            ):
                return _decision_result(source, last)
        return _error("Material changed; resume before retrying")
    existing = active.get((kind, index))
    if action == "include" and existing is not None:
        return _decision_result(source, existing)
    if action == "remove" and existing is None:
        return _error("That material reference is not active")
    if len(source.evidence_decisions) >= 50:
        return _error("Material evidence decision limit reached")
    source_id = source.evidence_decisions[0].source_id if source.evidence_decisions else uuid4().hex
    evidence_id = existing.evidence_id if existing else uuid4().hex
    event = MaterialEvidenceDecision(
        event_id=uuid4().hex,
        source_id=source_id,
        evidence_id=evidence_id,
        kind=kind,
        index=index,
        action=action,
        source_digest=source_digest(source),
        draft_digest=draft_digest(source),
        previous_revision=previous,
        presented_prompt=prompt,
        user_response=response,
    )
    state.inline_material = source.model_copy(
        update={"evidence_decisions": (*source.evidence_decisions, event)}
    )
    state.mark_updated()
    saved = await engine.save_state(state)
    if saved.is_err:
        return _error("Material decision could not be saved; resume before retrying")
    return _decision_result(state.inline_material, event)


def _decision_result(
    source: Any, event: MaterialEvidenceDecision
) -> Result[MCPToolResult, MCPServerError]:
    return Result.ok(
        MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=(
                        f"Material reference {event.action} recorded. Source ID: {event.source_id}; "
                        f"Evidence ID: {event.evidence_id}; material revision: {revision(source)}. "
                        "This records a local reference, not truth or a requirement."
                    ),
                ),
            ),
            meta={
                "material_evidence_status": event.action,
                "material_evidence_source_id": event.source_id,
                "material_evidence_id": event.evidence_id,
                "material_evidence_revision": revision(source),
            },
        )
    )
