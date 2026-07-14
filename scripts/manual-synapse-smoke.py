#!/usr/bin/env python3
"""Manual live-backend smoke test for Ouroboros Synapse.

This intentionally uses the real runtime adapter and the same ParallelACExecutor
delivery path as a run.  It does not edit the target repository.  For supported
backends it proves target discovery, durable queueing, provider-boundary claim,
same-native-session resume, application acknowledgement, and a bounded reply.
Hermes exercises the truthful capability-rejection path without spending a
provider turn.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock
from uuid import uuid4

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
    derive_session_signal_id,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.orchestrator import create_agent_runtime
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.synapse import (
    SessionSignalHub,
    SessionSignalMailbox,
    SessionSignalTarget,
)
from ouroboros.persistence.event_store import EventStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "backend",
        choices=("codex", "claude", "claude_mcp", "opencode", "goose", "pi", "hermes"),
    )
    parser.add_argument(
        "--mode",
        choices=("inform", "redirect"),
        default="inform",
        help="redirect always requests the explicit after_turn fallback",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--llm-backend", default=None)
    parser.add_argument("--cli-path", default=None)
    parser.add_argument("--timeout", type=float, default=360.0)
    return parser


def _signal_for_target(
    target: SessionSignalTarget,
    *,
    requested_mode: SessionSignalMode,
    marker: str,
) -> SessionSignal:
    idempotency_key = f"manual_{marker.lower()}"
    return SessionSignal(
        signal_id=derive_session_signal_id(
            expected_execution_id=target.execution_id,
            target_session_scope_id=target.session_scope_id,
            target_session_attempt_id=target.session_attempt_id,
            idempotency_key=idempotency_key,
        ),
        target_session_scope_id=target.session_scope_id,
        target_session_attempt_id=target.session_attempt_id,
        expected_execution_id=target.execution_id,
        mode=requested_mode,
        fallback_mode=(
            SessionSignalMode.AFTER_TURN if requested_mode is SessionSignalMode.REDIRECT else None
        ),
        message=(
            f"Reply with the exact token {marker}. Do not use tools or modify artifacts. "
            "Confirm briefly that this request reached the same resumed AC session."
        ),
        source=SessionSignalSource.USER,
        reason="Manual live Synapse same-session delivery proof.",
        idempotency_key=idempotency_key,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        expected_contract_version=target.contract_version,
    )


async def _wait_for_target(
    hub: SessionSignalHub,
    execution_id: str,
    *,
    timeout: float,
) -> SessionSignalTarget:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        targets = hub.list_targets(execution_id=execution_id)
        if len(targets) == 1:
            return targets[0]
        if len(targets) > 1:
            raise RuntimeError(f"Expected one live target, found {len(targets)}")
        await asyncio.sleep(0.01)
    raise TimeoutError("The live AC target was not registered before timeout")


async def _unsupported_smoke(args: argparse.Namespace, runtime: object) -> dict[str, object]:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub(event_store=store)
    try:
        capabilities = runtime.capabilities.session_signals
        target = SessionSignalTarget(
            execution_id="exec_manual_hermes",
            session_scope_id="exec_manual_hermes_ac_1",
            session_attempt_id="exec_manual_hermes_ac_1_attempt_1",
            runtime_backend=runtime.runtime_backend,
            capabilities=capabilities,
            ac_content="Prove unsupported Synapse delivery is rejected",
            display_label="AC 1",
        )
        await hub.register_replaying(target)
        mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
        marker = f"SYNAPSE_HERMES_{uuid4().hex[:8].upper()}"
        projection = await mailbox.request(
            _signal_for_target(target, requested_mode=SessionSignalMode.INFORM, marker=marker)
        )
        events = await store.replay("session_signal", projection.signal_id)
        if projection.state is not SessionSignalState.REJECTED:
            raise RuntimeError(f"Hermes unexpectedly accepted delivery: {projection.state.value}")
        return {
            "backend": args.backend,
            "runtime_backend": runtime.runtime_backend,
            "capabilities": capabilities.to_event_data(),
            "state": projection.state.value,
            "event_types": [event.type for event in events],
            "truthful_unsupported": True,
        }
    finally:
        await store.close()


async def _live_smoke(args: argparse.Namespace, runtime: object, cwd: Path) -> dict[str, object]:
    capabilities = runtime.capabilities.session_signals
    if not capabilities.inform_delivery or not capabilities.background_reply:
        raise RuntimeError(
            f"{args.backend} does not advertise inform/background-reply delivery: "
            f"{capabilities.to_event_data()}"
        )

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub(event_store=store)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    suffix = uuid4().hex[:8].upper()
    primary_marker = f"SYNAPSE_PRIMARY_{suffix}"
    reply_marker = f"SYNAPSE_REPLY_{suffix}"
    execution_id = f"exec_manual_{args.backend}_{suffix.lower()}"
    session_id = f"orch_manual_{args.backend}_{suffix.lower()}"
    requested_mode = SessionSignalMode(args.mode)

    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content=(
                f"Do not inspect files or use tools. Reply with the exact token "
                f"{primary_marker} and say the initial AC turn is complete."
            ),
            session_id=session_id,
            execution_id=execution_id,
            tools=[],
            system_prompt=(
                "You are a live runtime transport probe. Never use tools or modify files. "
                "Follow exact-token instructions and answer briefly."
            ),
            seed_goal="Prove same-session Ouroboros Synapse delivery without file changes",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        target = await _wait_for_target(hub, execution_id, timeout=min(args.timeout, 30.0))
        signal = _signal_for_target(
            target,
            requested_mode=requested_mode,
            marker=reply_marker,
        )
        queued = await mailbox.request(signal)
        if queued.state is not SessionSignalState.QUEUED:
            raise RuntimeError(f"Signal did not queue: {queued.state.value}")

        result = await asyncio.wait_for(execution_task, timeout=args.timeout)
        events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(events)
        event_types = [event.type for event in events]
        expected_events = [
            "control.session.signal.requested",
            "control.session.signal.accepted",
            "control.session.signal.queued",
            "control.session.signal.delivering",
            "control.session.signal.applied",
            "control.session.signal.completed",
        ]
        if event_types != expected_events:
            raise RuntimeError(f"Unexpected lifecycle: {event_types}")
        if projection.state is not SessionSignalState.COMPLETED:
            raise RuntimeError(f"Signal did not complete: {projection.state.value}")
        if projection.reply is None or reply_marker not in projection.reply:
            raise RuntimeError(
                f"Bounded reply did not contain {reply_marker}: {projection.reply!r}"
            )
        if not result.success:
            raise RuntimeError(f"Primary AC failed: {result.error or result.final_message}")

        native_session_ids = sorted(
            {
                message.resume_handle.native_session_id
                for message in result.messages
                if message.resume_handle is not None
                and message.resume_handle.native_session_id is not None
            }
        )
        if len(native_session_ids) != 1:
            raise RuntimeError(
                f"Expected one native session across both turns: {native_session_ids}"
            )
        tool_messages = [
            message
            for message in result.messages
            if message.tool_name is not None
            or message.type in {"tool", "tool_call", "tool_use", "tool_result"}
        ]
        if tool_messages:
            raise RuntimeError(f"No-tools smoke emitted {len(tool_messages)} tool messages")

        effective_mode = projection.effective_mode
        if requested_mode is SessionSignalMode.REDIRECT:
            if effective_mode is not SessionSignalMode.AFTER_TURN:
                raise RuntimeError(
                    f"Redirect did not use explicit after_turn fallback: {effective_mode}"
                )
        elif effective_mode is not SessionSignalMode.INFORM:
            raise RuntimeError(f"Inform resolved unexpectedly: {effective_mode}")

        return {
            "backend": args.backend,
            "runtime_backend": runtime.runtime_backend,
            "llm_backend": runtime.llm_backend,
            "requested_mode": requested_mode.value,
            "effective_mode": effective_mode.value if effective_mode else None,
            "capabilities": capabilities.to_event_data(),
            "target": target.to_discovery_data(),
            "queued_state": queued.state.value,
            "final_state": projection.state.value,
            "event_types": event_types,
            "bounded_reply": projection.reply,
            "native_session_ids": native_session_ids,
            "same_native_session": len(native_session_ids) == 1,
            "primary_success": result.success,
            "tool_message_count": len(tool_messages),
            "cwd": str(cwd),
        }
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()


async def _main(args: argparse.Namespace) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"ouroboros-synapse-{args.backend}-") as tmp:
        cwd = Path(tmp)
        runtime = create_agent_runtime(
            backend=args.backend,
            permission_mode="bypassPermissions",
            model=args.model,
            cli_path=args.cli_path,
            cwd=cwd,
            llm_backend=args.llm_backend,
            startup_output_timeout_seconds=min(args.timeout, 120.0),
            stdout_idle_timeout_seconds=args.timeout,
        )
        if args.backend == "hermes":
            return await _unsupported_smoke(args, runtime)
        return await _live_smoke(args, runtime, cwd)


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001 - manual harness reports exact backend failure.
        print(json.dumps({"backend": args.backend, "ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
