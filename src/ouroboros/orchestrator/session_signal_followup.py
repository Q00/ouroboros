"""SessionSignal follow-up state and provider-boundary transitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalMode,
    bounded_session_signal_reply,
)
from ouroboros.events.session_signal import (
    create_session_signal_delivery_started_event,
    create_session_signal_rejected_event,
)
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.leaf_dispatcher import LeafDispatchState

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore


@dataclass(frozen=True, slots=True)
class CompletedProviderTurn:
    """Restorable state for a primary turn awaiting a follow-up boundary."""

    dispatch_id: str
    runtime_handle: RuntimeHandle
    ac_session_id: str | None
    message_count: int
    message_list_length: int
    final_message: str
    success: bool
    stalled: bool

    @classmethod
    def capture(
        cls,
        dispatch_id: str,
        runtime_handle: RuntimeHandle,
        state: LeafDispatchState,
    ) -> CompletedProviderTurn:
        """Freeze the completed primary state before publishing its child dispatch."""
        return cls(
            dispatch_id=dispatch_id,
            runtime_handle=runtime_handle,
            ac_session_id=state.ac_session_id,
            message_count=state.message_count,
            # ``message_count`` counts only runtime messages; the transcript
            # list may additionally hold synthetic harness entries (the
            # workspace observation). Truncation and follow-up slicing must
            # therefore use the actual list boundary, never the runtime
            # counter, or a queued signal reads the previous turn's tail.
            message_list_length=len(state.messages),
            final_message=state.final_message,
            success=state.success,
            stalled=state.stalled,
        )

    def restore(self, state: LeafDispatchState) -> str:
        """Restore the completed primary after a follow-up was aborted pre-entry."""
        del state.messages[self.message_list_length :]
        state.runtime_handle = self.runtime_handle
        state.ac_session_id = self.ac_session_id
        state.message_count = self.message_count
        state.final_message = self.final_message
        state.success = self.success
        state.stalled = self.stalled
        return self.dispatch_id


async def claim_follow_up_delivery(
    *,
    event_store: EventStore,
    signal: SessionSignal,
    effective_mode: SessionSignalMode,
    runtime_backend: str,
    orchestrator_session_id: str,
) -> None:
    """Claim signal delivery while the follow-up remains pre-provider-entry."""
    await event_store.append(
        create_session_signal_delivery_started_event(
            signal,
            effective_mode=effective_mode,
            runtime_backend=runtime_backend,
            orchestrator_session_id=orchestrator_session_id,
        )
    )


async def abort_unentered_follow_up(
    *,
    event_store: EventStore,
    signal: SessionSignal,
    effective_mode: SessionSignalMode,
    runtime_backend: str,
    orchestrator_session_id: str,
    follow_up_dispatch_id: str,
    primary: CompletedProviderTurn,
    state: LeafDispatchState,
    seal_dispatch: Callable[..., Awaitable[None]],
) -> str:
    """Abort one pre-entry child dispatch and return its restored primary id."""
    active_dispatch_id = primary.restore(state)
    reason, replayable = ACRuntimeHandleManager.cancellation_seal_policy(False)
    await seal_dispatch(
        follow_up_dispatch_id,
        reason=reason,
        replayable=replayable,
    )
    await event_store.append(
        create_session_signal_rejected_event(
            signal,
            rejection_code="batch_paused_before_delivery",
            detail=(
                "A sibling provider quota paused the batch before this "
                "SessionSignal entered the runtime delivery boundary."
            ),
            effective_mode=effective_mode,
            runtime_backend=runtime_backend,
            orchestrator_session_id=orchestrator_session_id,
        )
    )
    return active_dispatch_id


def is_application_acknowledgement(message: AgentMessage) -> bool:
    """Return whether a resumed-turn message proves provider context entry."""
    subtype = message.data.get("subtype")
    if message.type == "assistant":
        return bool(message.content.strip()) and subtype not in {"error", "runtime_error"}
    return message.type == "result" and subtype == "success"


def bounded_runtime_reply(messages: list[AgentMessage]) -> str | None:
    """Build one bounded provider reply without persisting a raw transcript."""
    assistant_messages = [
        message
        for message in messages
        if message.type == "assistant"
        and is_application_acknowledgement(message)
        and message.content.strip()
    ]
    completion_messages = [
        message for message in assistant_messages if message.data.get("subtype") == "completion"
    ]
    if completion_messages:
        return bounded_session_signal_reply(completion_messages[-1].content)
    if assistant_messages:
        return bounded_session_signal_reply(
            "".join(message.content for message in assistant_messages)
        )

    for message in reversed(messages):
        if (
            message.type == "result"
            and is_application_acknowledgement(message)
            and message.content.strip()
        ):
            return bounded_session_signal_reply(message.content)
    return None


# Compatibility aliases retained for direct tests and downstream imports.
_is_session_signal_application_acknowledgement = is_application_acknowledgement
_bounded_session_signal_runtime_reply = bounded_runtime_reply


__all__ = [
    "CompletedProviderTurn",
    "_bounded_session_signal_runtime_reply",
    "_is_session_signal_application_acknowledgement",
    "abort_unentered_follow_up",
    "claim_follow_up_delivery",
]
