"""Public MCP schema tests for Ouroboros Synapse."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import SessionSignalProjection
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.synapse_handler import SynapseSignalHandler, SynapseTargetsHandler
from ouroboros.orchestrator.synapse import (
    SessionSignalHub,
    SessionSignalMailbox,
    SessionSignalTarget,
)


@dataclass
class _Mailbox:
    state: SessionSignalState = SessionSignalState.QUEUED
    effective_mode: SessionSignalMode | None = SessionSignalMode.AFTER_TURN
    reply: str | None = None
    received: list[SessionSignal] = field(default_factory=list)

    async def request(self, signal: SessionSignal) -> SessionSignalProjection:
        self.received.append(signal)
        return SessionSignalProjection(
            signal_id=signal.signal_id,
            target_session_scope_id=signal.target_session_scope_id,
            target_session_attempt_id=signal.target_session_attempt_id,
            expected_execution_id=signal.expected_execution_id,
            requested_mode=signal.mode,
            effective_mode=self.effective_mode,
            source=signal.source,
            idempotency_key=signal.idempotency_key,
            message_digest=signal.message_digest,
            state=self.state,
            event_ids=("evt_1",),
            reply=self.reply,
        )


@dataclass
class _Store:
    events: list[BaseEvent] = field(default_factory=list)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in self.events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def append_batch(self, events: list[BaseEvent]) -> None:
        self.events.extend(events)


def _arguments(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "target_session_scope_id": "scope_1",
        "target_session_attempt_id": "scope_1_attempt_1",
        "expected_execution_id": "exec_1",
        "mode": "redirect",
        "fallback_mode": "after_turn",
        "message": "Apply the clarified local intent.",
        "source": "user",
        "reason": "User clarification.",
        "idempotency_key": "turn_7_scope_1",
    }
    values.update(overrides)
    return values


def test_definition_exposes_clean_room_vocabulary() -> None:
    handler = SynapseSignalHandler(_Mailbox())  # type: ignore[arg-type]
    definition = handler.definition
    params = {parameter.name: parameter for parameter in definition.parameters}

    assert definition.name == "ouroboros_session_signal"
    assert params["mode"].enum == ("inform", "after_turn", "redirect", "replace")
    assert params["fallback_mode"].enum == ("after_turn",)
    assert params["source"].enum == ("user", "conductor", "worker")
    assert params["contract_effect"].enum == ("additive", "specification_change")


def test_public_schema_distinguishes_shipped_reserved_and_fallback_modes() -> None:
    signal = SynapseSignalHandler(_Mailbox()).definition  # type: ignore[arg-type]
    targets = SynapseTargetsHandler(SessionSignalHub()).definition
    schema = signal.to_input_schema()
    mode = schema["properties"]["mode"]
    fallback = schema["properties"]["fallback_mode"]
    description = signal.description

    assert "advertise and apply only inform and after_turn" in description
    assert "direct requested modes require" in description
    assert "redirect and replace remain reserved" in description
    assert "redirect nevertheless remains a valid requested mode" in description
    assert "fallback_mode=after_turn must be present" in description
    assert "effective queued mode" in description
    assert "unsupported requests fail closed" in description
    assert "selected exact attempt's live capabilities" in description
    assert mode["enum"] == ["inform", "after_turn", "redirect", "replace"]
    assert "For the direct path" in mode["description"]
    assert "For the sole fallback path" in mode["description"]
    assert "redirect may be requested while omitted from discovery" in mode["description"]
    assert "effective mode is then after_turn" in mode["description"]
    assert "advertise/effect only inform and after_turn" in mode["description"]
    assert "ouroboros_session_signal_targets" in mode["description"]
    assert fallback["enum"] == ["after_turn"]
    assert "Explicit redirect-only fallback" in fallback["description"]
    assert "live SessionSignal capabilities" in targets.description
    assert "Advertised modes are valid direct requested modes" in targets.description
    assert "redirect may be requested while omitted from discovery" in targets.description
    assert "effective queued mode is after_turn" in targets.description
    assert "other unsupported requests fail closed" in targets.description
    assert "select only an advertised mode" not in targets.description


@pytest.mark.asyncio
async def test_public_discovery_guidance_and_mailbox_share_redirect_fallback_contract() -> None:
    store = _Store()
    hub = SessionSignalHub()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(
            after_turn_delivery=True,
            checkpoint_redirect=False,
        ),
    )
    hub.register(target)
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)  # type: ignore[arg-type]
    signal_handler = SynapseSignalHandler(mailbox)
    targets_handler = SynapseTargetsHandler(hub)

    discovered = await targets_handler.handle({"execution_id": "exec_1"})
    queued = await signal_handler.handle(_arguments())
    rejected = await signal_handler.handle(
        _arguments(fallback_mode=None, idempotency_key="turn_8_scope_1")
    )

    assert discovered.is_ok
    assert "[modes: after_turn]" in discovered.value.text_content
    capabilities = discovered.value.meta["targets"][0]["capabilities"]
    assert capabilities["after_turn_delivery"] is True
    assert capabilities["checkpoint_redirect"] is False
    mode_description = next(
        parameter.description
        for parameter in signal_handler.definition.parameters
        if parameter.name == "mode"
    )
    assert "redirect may be requested while omitted from discovery" in mode_description
    assert "redirect may be requested while omitted from discovery" in (
        targets_handler.definition.description
    )
    assert queued.is_ok
    assert queued.value.meta["requested_mode"] == "redirect"
    assert queued.value.meta["effective_mode"] == "after_turn"
    pending = hub.pop_pending(target)
    assert pending is not None
    assert pending.signal.mode is SessionSignalMode.REDIRECT
    assert pending.effective_mode is SessionSignalMode.AFTER_TURN
    assert rejected.is_ok
    assert rejected.value.is_error is True
    assert rejected.value.meta["state"] == "rejected"
    assert rejected.value.meta["effective_mode"] is None
    rejected_events = await store.replay(
        "session_signal",
        rejected.value.meta["signal_id"],
    )
    assert rejected_events[-1].data["rejection_code"] == "capability_unsupported"


@pytest.mark.asyncio
async def test_valid_request_returns_queued_not_applied() -> None:
    mailbox = _Mailbox()
    handler = SynapseSignalHandler(mailbox)  # type: ignore[arg-type]

    result = await handler.handle(_arguments())

    assert result.is_ok
    assert result.value.is_error is False
    assert result.value.meta["state"] == "queued"
    assert result.value.meta["effective_mode"] == "after_turn"
    assert result.value.meta["application_proven"] is False
    assert "Application is not yet proven" in result.value.text_content
    assert mailbox.received[0].signal_id.startswith("sig_")


@pytest.mark.asyncio
async def test_same_idempotency_identity_derives_same_signal_id() -> None:
    mailbox = _Mailbox()
    handler = SynapseSignalHandler(mailbox)  # type: ignore[arg-type]

    first = await handler.handle(_arguments())
    second = await handler.handle(_arguments())

    assert first.value.meta["signal_id"] == second.value.meta["signal_id"]


@pytest.mark.asyncio
async def test_completed_request_surfaces_bounded_ac_reply() -> None:
    mailbox = _Mailbox(
        state=SessionSignalState.COMPLETED,
        reply="The confirmation assertion now passes.",
    )
    result = await SynapseSignalHandler(mailbox).handle(_arguments())  # type: ignore[arg-type]

    assert result.is_ok
    assert result.value.meta["application_proven"] is True
    assert result.value.meta["reply"] == "The confirmation assertion now passes."
    assert "AC reply: The confirmation assertion now passes." in result.value.text_content


@pytest.mark.asyncio
async def test_invalid_mode_fails_before_mailbox() -> None:
    mailbox = _Mailbox()
    handler = SynapseSignalHandler(mailbox)  # type: ignore[arg-type]

    result = await handler.handle(_arguments(mode="interrupt"))

    assert result.is_err
    assert mailbox.received == []


@pytest.mark.asyncio
async def test_replace_requires_approval_receipt() -> None:
    mailbox = _Mailbox(effective_mode=SessionSignalMode.REPLACE)
    handler = SynapseSignalHandler(mailbox)  # type: ignore[arg-type]

    rejected = await handler.handle(_arguments(mode="replace", fallback_mode=None))
    accepted = await handler.handle(
        _arguments(
            mode="replace",
            fallback_mode=None,
            user_approval_event_id="hitl_evt_1",
        )
    )

    assert rejected.is_err
    assert accepted.is_ok
    assert mailbox.received[-1].user_approval_event_id == "hitl_evt_1"


@pytest.mark.asyncio
async def test_rejected_projection_is_tool_error_without_transport_exception() -> None:
    mailbox = _Mailbox(
        state=SessionSignalState.REJECTED,
        effective_mode=None,
    )
    handler = SynapseSignalHandler(mailbox)  # type: ignore[arg-type]

    result = await handler.handle(_arguments())

    assert result.is_ok
    assert result.value.is_error is True
    assert result.value.meta["state"] == "rejected"


@pytest.mark.asyncio
async def test_targets_handler_exposes_active_ac_content_and_exact_attempt() -> None:
    hub = SessionSignalHub()
    hub.register(
        SessionSignalTarget(
            execution_id="exec_1",
            session_scope_id="exec_1_ac_2",
            session_attempt_id="exec_1_ac_2_attempt_1",
            runtime_backend="codex_cli",
            capabilities=SessionSignalCapabilities(after_turn_delivery=True),
            ac_id="exec_1_ac_2",
            ac_content="Improve the confirmation copy",
            display_label="AC 2",
            ac_index=1,
            depth=0,
        )
    )
    handler = SynapseTargetsHandler(hub)

    result = await handler.handle({"execution_id": "exec_1"})

    assert result.is_ok
    assert result.value.meta["active_target_count"] == 1
    target = result.value.meta["targets"][0]
    assert target["target_session_scope_id"] == "exec_1_ac_2"
    assert target["target_session_attempt_id"] == "exec_1_ac_2_attempt_1"
    assert target["ac_content"] == "Improve the confirmation copy"
    assert target["ac_number"] == 2
    assert target["capabilities"]["after_turn_delivery"] is True
    assert "Improve the confirmation copy" in result.value.text_content


@pytest.mark.asyncio
async def test_targets_handler_is_scoped_to_requested_execution() -> None:
    hub = SessionSignalHub()
    for execution_id in ("exec_1", "exec_2"):
        hub.register(
            SessionSignalTarget(
                execution_id=execution_id,
                session_scope_id=f"{execution_id}_ac_1",
                session_attempt_id=f"{execution_id}_ac_1_attempt_1",
                runtime_backend="codex_cli",
            )
        )

    result = await SynapseTargetsHandler(hub).handle({"execution_id": "exec_2"})

    assert result.is_ok
    assert result.value.meta["active_target_count"] == 1
    assert result.value.meta["targets"][0]["execution_id"] == "exec_2"


@pytest.mark.asyncio
async def test_targets_handler_accepts_durable_async_catalog() -> None:
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="checkout_scope",
        session_attempt_id="checkout_scope_attempt_1",
        runtime_backend="codex_cli",
        ac_content="Create the checkout confirmation copy",
        ac_index=0,
    )

    class _DurableCatalog:
        async def list_targets(self, *, execution_id: str) -> tuple[SessionSignalTarget, ...]:
            assert execution_id == "exec_1"
            return (target,)

    result = await SynapseTargetsHandler(_DurableCatalog()).handle(  # type: ignore[arg-type]
        {"execution_id": "exec_1"}
    )

    assert result.is_ok
    assert result.value.meta["active_target_count"] == 1
    assert result.value.meta["targets"][0]["ac_content"] == (
        "Create the checkout confirmation copy"
    )
