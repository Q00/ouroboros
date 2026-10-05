"""Run the evidence turn through the executor's own provider boundary.

The evidence turn (``evidence/evidence_turn.py``) is one tool-less follow-up
turn on the worker's own session, after the worker's turn completed. It is a
durable dispatch like a SessionSignal follow-up: the controller captures the
completed primary turn, records an ``execution.ac.attempt.dispatched`` event
with ``dispatch_kind="evidence_turn"`` and the digest of the prompt it sends,
binds the runtime handle to the new dispatch, enters the provider through the
executor's admitted stream, and seals the primary once the follow-up completed.

The worker's transcript is frozen at the end of its own turn: the numbered
list is built from the primary messages, the typed evidence record is still
read from the primary final message, and once the reply is read the dispatch
state is put back to the primary transcript, final message and outcome. What
the evidence turn itself did never becomes transcript evidence; a reply whose
turn ran any tool is unusable.

When no evidence turn can run (not in fat-harness mode, nothing to cite, no
capsule-bound resumable handle, the primary turn failed or paused), or its
reply cites nothing usable (not a JSON object of call-number lists, or the
turn ran a tool), the record keeps the command-string path. A failure across the provider boundary seals
the follow-up dispatch and restores the primary turn, so the criterion falls
back to that path as well.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
from typing import Any
from uuid import uuid4

import anyio

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.evidence.ac_classification import _effective_evidence_schema_for_ac
from ouroboros.orchestrator.evidence.cited_evidence import CitedEvidence
from ouroboros.orchestrator.evidence.evidence_turn import (
    EvidenceTurnSettings,
    ReplayPermission,
    build_cited_evidence,
    citable_fields,
    evidence_turn_prompt_for,
)
from ouroboros.orchestrator.evidence.relevance_veto import LLMRelevanceJudge, RelevanceJudge
from ouroboros.orchestrator.leaf_dispatcher import LeafDispatchState
from ouroboros.orchestrator.recoverable_failure import is_usage_limit_pause_message
from ouroboros.orchestrator.session_signal_followup import CompletedProviderTurn

log = get_logger(__name__)

EVIDENCE_TURN_DISPATCH_KIND = "evidence_turn"


@dataclass(frozen=True, slots=True)
class EvidenceTurnBoundary:
    """The executor's dispatch-capsule state the evidence turn runs inside."""

    active_dispatch_id: str
    capsule_fingerprint: str
    request_authority_digest: str
    runtime_identity: Any
    execution_id: str
    session_id: str
    stream: Callable[..., Awaitable[bool]]
    seal: Callable[..., Awaitable[None]]
    remember: Callable[[RuntimeHandle], RuntimeHandle | None]
    pause_error: type[BaseException]


@dataclass(frozen=True, slots=True)
class EvidenceTurnOutcome:
    """``cited`` is None when the record keeps the command-string path.

    ``route_drift`` asks the executor to terminalize ``active_dispatch_id``
    (the provider route changed before the follow-up entered).
    """

    cited: CitedEvidence | None
    active_dispatch_id: str
    route_drift: bool = False


def _fields(executor: Any, ac_content: str, spec: Any) -> tuple[str, ...]:
    profile = getattr(executor, "_execution_profile", None)
    if profile is None or not getattr(executor, "_fat_harness_mode", False):
        return ()
    schema = _effective_evidence_schema_for_ac(
        profile,
        ac_content,
        has_success_contract=bool(getattr(spec, "has_success_contract", False)),
        has_expected_artifacts=bool(getattr(spec, "expected_artifacts", ())),
        verify_gate_active=bool(getattr(executor, "_run_verify_commands", False)),
    )
    return citable_fields(schema.required)


def _relevance_judge(executor: Any, settings: EvidenceTurnSettings, cwd: str | None) -> Any:
    if not settings.relevance_veto:
        return None
    judge: RelevanceJudge | None = getattr(executor, "evidence_relevance_judge", None)
    return judge if judge is not None else LLMRelevanceJudge(cwd=cwd)


def _replay_permission(executor: Any, tools: list[str], cwd: str | None) -> ReplayPermission:
    if (
        cwd is None
        or "Bash" not in tools
        or getattr(executor, "_run_verify_commands", False) is not True
    ):
        return ReplayPermission(allowed=False)
    from ouroboros.orchestrator.verify_shell import project_verify_environment

    return ReplayPermission(
        allowed=True,
        env=dict(project_verify_environment(cwd)),
        timeout_seconds=float(getattr(executor, "_verify_command_timeout_seconds", 600)),
        sandbox_enabled=getattr(executor, "_exec_sandbox_enabled", None),
    )


def _restore_primary_transcript(state: LeafDispatchState, primary: CompletedProviderTurn) -> None:
    """Put the primary transcript and outcome back; keep the follow-up's session handle."""
    del state.messages[primary.message_list_length :]
    state.message_count = primary.message_count
    state.final_message = primary.final_message
    state.success = primary.success
    state.stalled = primary.stalled


async def run_evidence_turn(
    executor: Any,
    state: LeafDispatchState,
    boundary: EvidenceTurnBoundary,
    *,
    ac_content: str,
    spec: Any,
    tools: list[str],
) -> EvidenceTurnOutcome:
    """Run the evidence turn when it applies; see the module docstring."""
    unchanged = EvidenceTurnOutcome(cited=None, active_dispatch_id=boundary.active_dispatch_id)
    handle = state.runtime_handle
    if (
        not state.success
        or state.stalled
        or any(is_usage_limit_pause_message(message) for message in state.messages)
        or handle is None
        or not ACRuntimeHandleManager._is_resumable_runtime_handle(handle)
        or handle.metadata.get("ac_capsule_fingerprint") != boundary.capsule_fingerprint
        or handle.metadata.get("ac_dispatch_id") != boundary.active_dispatch_id
    ):
        return unchanged
    fields = _fields(executor, ac_content, spec)
    cwd = getattr(executor, "_task_cwd", None) or executor._adapter.working_directory
    backend = str(getattr(executor._adapter, "runtime_backend", ""))
    settings = EvidenceTurnSettings.from_config()
    primary_messages = tuple(state.messages)
    prompt = evidence_turn_prompt_for(
        primary_messages=primary_messages,
        ac_content=ac_content,
        fields=fields,
        task_cwd=cwd,
        runtime_backend=backend,
        settings=settings,
    )
    if prompt is None:
        return unchanged

    primary = CompletedProviderTurn.capture(boundary.active_dispatch_id, handle, state)
    follow_up_id = uuid4().hex
    candidate = replace(handle, metadata={**dict(handle.metadata), "ac_dispatch_id": follow_up_id})
    await executor._event_emitter.emit_ac_attempt_dispatched(
        runtime_identity=boundary.runtime_identity,
        dispatch_id=follow_up_id,
        previous_dispatch_id=primary.dispatch_id,
        execution_id=boundary.execution_id,
        session_id=boundary.session_id,
        capsule_fingerprint=boundary.capsule_fingerprint,
        request_authority_digest=boundary.request_authority_digest,
        session_origin="restored_same_attempt",
        runtime_handle=candidate,
        dispatch_kind=EVIDENCE_TURN_DISPATCH_KIND,
        follow_up_input_digest="sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )
    remembered = boundary.remember(candidate)
    if remembered is None:
        raise RuntimeError("evidence turn lost its capsule-bound runtime handle")
    state.runtime_handle = remembered
    try:
        entered = await boundary.stream(call_prompt=prompt, call_tools=[])
    except boundary.pause_error:
        primary.restore(state)
        reason, replayable = ACRuntimeHandleManager.cancellation_seal_policy(False)
        await boundary.seal(follow_up_id, reason=reason, replayable=replayable)
        return unchanged
    except anyio.get_cancelled_exc_class():
        with anyio.CancelScope(shield=True):
            reason, replayable = ACRuntimeHandleManager.cancellation_seal_policy(True)
            await boundary.seal(follow_up_id, reason=reason, replayable=replayable)
        raise
    except Exception as exc:
        log.warning("evidence_turn.provider_failed", error=type(exc).__name__)
        await boundary.seal(follow_up_id, reason="evidence turn crossed an uncertain boundary")
        primary.restore(state)
        return unchanged
    if not entered:
        return EvidenceTurnOutcome(cited=None, active_dispatch_id=follow_up_id, route_drift=True)
    await boundary.seal(
        primary.dispatch_id, reason="completed provider turn superseded by an evidence turn"
    )
    reply_messages = tuple(state.messages[primary.message_list_length :])
    turn_succeeded = state.success and not state.stalled
    reply = state.final_message
    _restore_primary_transcript(state, primary)
    if not turn_succeeded:
        return EvidenceTurnOutcome(cited=None, active_dispatch_id=follow_up_id)
    cited = await build_cited_evidence(
        reply=reply,
        reply_messages=reply_messages,
        primary_messages=primary_messages,
        ac_content=ac_content,
        fields=fields,
        task_cwd=cwd,
        runtime_backend=backend,
        settings=settings,
        judge=_relevance_judge(executor, settings, cwd),
        replay=_replay_permission(executor, tools, cwd),
    )
    if cited.citations is None:
        # No citation at all (not JSON numbers, or the turn ran a tool): the
        # record keeps the command-string path, which still rejects claims
        # the transcript contradicts.
        log.info("evidence_turn.reply_unusable", reason=cited.reply_error)
        return EvidenceTurnOutcome(cited=None, active_dispatch_id=follow_up_id)
    return EvidenceTurnOutcome(cited=cited, active_dispatch_id=follow_up_id)


def cited_evidence_summary(cited: CitedEvidence | None) -> Mapping[str, Any] | None:
    """The evidence turn's result for the typed-evidence event (numbers and decisions only)."""
    if cited is None:
        return None
    citations = cited.citations
    return {
        "tests_passed": list(citations.tests_passed) if citations is not None else None,
        "commands_run": list(citations.commands_run) if citations is not None else None,
        "reply_error": cited.reply_error,
        "result_source": "is_error" if cited.is_error_is_exit_verdict else "exit_status",
        "replays": [
            {"number": replay.number, "succeeded": replay.succeeded} for replay in cited.replays
        ],
        "relevance": [
            {"number": decision.number, "status": decision.status} for decision in cited.relevance
        ],
    }


__all__ = [
    "EVIDENCE_TURN_DISPATCH_KIND",
    "EvidenceTurnBoundary",
    "EvidenceTurnOutcome",
    "cited_evidence_summary",
    "run_evidence_turn",
]
