"""Typed execution-event emission helpers for the parallel executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import ac_text
from ouroboros.events.base import BaseEvent
from ouroboros.harness.decomposition_attestation import DecompositionAttestation
from ouroboros.orchestrator.decomposition_policy import DecompositionDecisionRecord
from ouroboros.orchestrator.events import (
    create_heartbeat_event,
    create_progress_event,
    create_tool_called_event,
    create_workflow_progress_event,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
    build_ac_runtime_scope,
    build_level_coordinator_runtime_scope,
)
from ouroboros.orchestrator.frugality_proof import (
    EVENT_DELIVER_VERDICT,
    EVENT_SHADOW_REPLAY,
    EVENT_TOKEN_ATTRIBUTION,
)
from ouroboros.orchestrator.parallel_executor_models import StageExecutionOutcome
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message
from ouroboros.orchestrator.workflow_state import coerce_ac_marker_update

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.adapter import AgentMessage
    from ouroboros.orchestrator.coordinator import CoordinatorReview
    from ouroboros.persistence.event_store import EventStore


SafeEmitEvent = Callable[[Any], Awaitable[bool]]
ToolDetailFormatter = Callable[[str, dict[str, Any]], str]


class ExecutionEventEmitter:
    """Build and persist executor events without owning execution decisions."""

    def __init__(
        self,
        event_store: EventStore,
        *,
        safe_emit_event: SafeEmitEvent,
    ) -> None:
        self._event_store = event_store
        self._safe_emit_event = safe_emit_event
        self._last_ac_phase: dict[str, str] = {}
        self._last_discovery_signature: dict[str, tuple[tuple[str, ...], str]] = {}

    @staticmethod
    def runtime_event_metadata(message: AgentMessage) -> dict[str, Any]:
        """Serialize shared runtime/tool metadata for execution-scoped events."""
        projected = project_runtime_message(message)
        return dict(projected.runtime_metadata)

    @staticmethod
    def message_tool_input_preview(tool_input: dict[str, Any]) -> str | None:
        """Build a compact preview string for shared session tool-call events."""
        if not tool_input:
            return None

        parts: list[str] = []
        for key, value in tool_input.items():
            rendered = str(value).strip()
            if rendered:
                parts.append(f"{key}: {rendered}")
        preview = ", ".join(parts)
        return preview[:100] if preview else None

    async def emit_decomposition_decision_finalized(
        self,
        *,
        execution_id: str,
        session_id: str,
        mode: str,
        node_identity: ExecutionNodeIdentity,
        decision: DecompositionDecisionRecord,
    ) -> None:
        """Persist one finalized decomposition decision as best-effort audit data."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.decomposition.decision_finalized",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    **node_identity.to_event_metadata(),
                    **decision.to_dict(),
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "mode": mode,
                    "child_count": len(decision.children),
                },
            )
        )

    async def emit_decomposition_attested(
        self,
        *,
        execution_id: str,
        session_id: str,
        node_identity: ExecutionNodeIdentity,
        attestation: DecompositionAttestation,
        retry_attempt: int,
    ) -> bool:
        """Persist the gate-anchored decomposition trust verdict (Task 1).

        Computed once a decomposition round's siblings have all finished
        dispatch and the parent's own verify gate has been re-run over the
        resulting shared workspace. Durable so Kanban/HUD/conductor surfaces
        (Task 3) and the frugality proof's ``decomposition_trustworthy`` axis
        can read it without recomputing anything.

        ``retry_attempt`` is the root AC's retry index for the round just
        attested -- the SAME value forwarded to every child dispatched this
        round (see ``_execute_decomposition_children``), so the frugality
        proof (Fix 3, round 3) can correlate this event with the exact
        per-attempt rows it should (re-)authorize or invalidate, the same way
        every other per-child event already correlates by ``retry_attempt``.
        ``node_identity.to_event_metadata()`` carries no retry information
        (a node id is structural/path-based and stable across retries), so
        this field is threaded explicitly rather than folded into it.

        Returns:
            Whether the write was durably persisted (Fix 5, round 3,
            BLOCKING) -- correctness-bearing state, so the caller must check
            this rather than silently proceeding as if it always succeeds.
        """
        return await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.decomposition_attested",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    **node_identity.to_event_metadata(),
                    **attestation.to_event_data(),
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "retry_attempt": retry_attempt,
                },
            )
        )

    async def emit_ac_parked_for_operator(
        self,
        *,
        execution_id: str,
        session_id: str,
        node_id: str,
        root_ac_index: int,
        personas_tried: tuple[str, ...],
        consecutive_terminal_failures: int,
        backoff_seconds: float,
        reason: str,
    ) -> bool:
        """Persist that a root AC exhausted the lateral-escalation ladder (Task 2).

        This AC has failed at its most expensive configuration, cycled
        through every lateral-thinking persona, and is still failing. It
        NEVER surfaces a final FAILED status — the executor keeps retrying it
        at ``backoff_seconds`` cadence — but this durable event is the
        operator-visible signal that it needs a human look, replacing what
        would otherwise be silent infinite identical retries.

        ``node_id`` is the SAME canonical ``ExecutionNodeIdentity.root(...)
        .node_id`` every other per-node event for this root AC carries, so
        Kanban/HUD consumers can key off it exactly like any other event.

        Returns:
            Whether the write was durably persisted (Fix 5, round 3,
            BLOCKING) -- correctness-bearing state, so the caller must check
            this rather than silently proceeding as if it always succeeds.
        """
        return await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "node_id": node_id,
                    "root_ac_index": root_ac_index,
                    "personas_tried": list(personas_tried),
                    "consecutive_terminal_failures": consecutive_terminal_failures,
                    "backoff_seconds": backoff_seconds,
                    "reason": reason,
                },
            )
        )

    async def emit_ac_parked_resolved(
        self,
        *,
        execution_id: str,
        session_id: str,
        node_id: str,
        root_ac_index: int,
    ) -> bool:
        """Persist that a previously-parked root AC has succeeded (Task 2/Fix 8).

        ``execution.ac.parked_for_operator`` has no companion "un-parked"
        event, so a projection that folds the event log (Kanban/HUD/
        conductor) has no durable signal telling it to clear the parked
        badge once the AC that triggered it actually finishes successfully —
        it would otherwise show ``completed`` AND still-``parked`` on the
        same card forever. This is that companion event, emitted exactly
        once, at the moment the lateral-escalation ladder's in-memory streak
        for this root AC is cleared on a breakthrough.

        Round-5 Finding #3 (BLOCKING): emitted on EVERY successful ladder
        exit — parked or still mid-persona-cycling. Each ladder iteration
        durably records a ``lateral_escalation_progressed`` event before its
        redispatch, so a persona that succeeds BEFORE parking would otherwise
        leave that ``progressed`` event as the node's latest replayable
        record, and replay-based projections would show a completed AC as
        still "escalating" forever. Consumers already treat this event as
        "this node's escalation episode is over" (reducers clear all
        escalation badges; the state loader resets reconstruction to fresh),
        so it closes both exits. A plain ordinary success that never engaged
        the ladder still must not emit it — there is no escalation episode to
        resolve.

        ``node_id`` is the SAME canonical ``ExecutionNodeIdentity.root(...)
        .node_id`` the original ``parked_for_operator`` event carried, so a
        projection can pair the two by node id exactly like any other
        per-node event.

        Returns:
            Whether the write was durably persisted (Fix 5, round 3,
            BLOCKING) -- correctness-bearing state, so the caller must check
            this rather than silently proceeding as if it always succeeds.
        """
        return await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.parked_resolved",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "node_id": node_id,
                    "root_ac_index": root_ac_index,
                },
            )
        )

    async def emit_lateral_escalation_progressed(
        self,
        *,
        execution_id: str,
        session_id: str,
        node_id: str,
        root_ac_index: int,
        personas_tried: tuple[str, ...],
        consecutive_terminal_failures: int,
        parked: bool,
        persona: str | None,
    ) -> bool:
        """Persist EVERY persona-escalation streak advancement (Fix 5, round 2).

        ``execution.ac.parked_for_operator`` only records state at the
        moment an AC actually reaches full parking — every earlier streak
        advancement (the identical-retry count building up, and each
        individual persona tried before parking) previously lived ONLY in
        ``self._lateral_escalation_states``, an in-memory dict. A process
        cancelled/restarted mid-persona-attempt (before parking) silently
        lost that in-flight progress: ``_load_lateral_escalation_state``
        would reconstruct a fresh, never-escalated state on resume, and the
        ladder would restart persona cycling from scratch instead of
        continuing where it left off.

        Emitted once per loop iteration, right after
        ``advance_lateral_escalation`` computes the new state and BEFORE the
        (possibly long-running, possibly crash-prone) redispatch that
        follows — so even a crash mid-redispatch leaves the streak/persona
        progress that led to it durably recorded. ``_load_lateral_escalation_state``
        folds this event into its reconstruction the same way it already
        folds ``parked_for_operator``/``parked_resolved``, keyed by the
        latest event (of any of the three types) for this node id.

        ``node_id`` is the SAME canonical ``ExecutionNodeIdentity.root(...)
        .node_id`` every other per-node event for this root AC carries.

        Returns:
            Whether the write was durably persisted (Fix 5, round 3,
            BLOCKING) -- correctness-bearing state, so the caller must check
            this rather than silently proceeding as if it always succeeds.
        """
        return await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "node_id": node_id,
                    "root_ac_index": root_ac_index,
                    "personas_tried": list(personas_tried),
                    "consecutive_terminal_failures": consecutive_terminal_failures,
                    "parked": parked,
                    "persona": persona,
                },
            )
        )

    async def emit_bounce_classified(
        self,
        *,
        execution_id: str,
        session_id: str,
        node_identity: ExecutionNodeIdentity,
        cause: str,
        rationale: str,
        failure_class: str | None,
        retry_admission: str | None,
        evidence_refs: tuple[str, ...],
        trace_summary: str,
    ) -> None:
        """Persist a bounded cause-matched recovery classification."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.decomposition.bounce_classified",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    **node_identity.to_event_metadata(),
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "cause": cause,
                    "rationale": rationale,
                    "failure_class": failure_class,
                    "retry_admission": retry_admission,
                    "evidence_refs": list(evidence_refs),
                    "trace_summary": trace_summary,
                },
            )
        )

    @staticmethod
    def coordinator_aggregate_id(execution_id: str, level: int) -> str:
        """Build a deterministic level-scoped aggregate ID for coordinator work."""
        return f"{execution_id}:l{level - 1}:coord"

    async def emit_atomic_context_governed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str | None,
        ac_content: str,
        profile: str,
        decomposition_profile_metadata: dict[str, Any],
        context_audit: dict[str, Any],
    ) -> None:
        """Persist observe-only context-governor metadata for profile-backed leaves."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.context_governed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    **decomposition_profile_metadata,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "acceptance_criterion": ac_content,
                    "profile": profile,
                    **context_audit,
                },
            )
        )

    def build_session_progress_event(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        projected: Any,
    ) -> Any:
        """Create a shared session progress event from an AC runtime message."""
        message_type = projected.message_type
        event = create_progress_event(
            session_id=session_id,
            message_type=message_type,
            content_preview=projected.content,
            tool_name=projected.tool_name if message_type in {"tool", "tool_result"} else None,
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
            "progress": {
                "last_message_type": message_type,
                "last_content_preview": projected.content[:200],
            },
        }
        runtime = event_data.get("runtime")
        if isinstance(runtime, dict):
            event_data["progress"]["runtime"] = runtime
        runtime_event_type = event_data.get("runtime_event_type")
        if isinstance(runtime_event_type, str) and runtime_event_type:
            event_data["progress"]["runtime_event_type"] = runtime_event_type
        runtime_signal = event_data.get("runtime_signal")
        if isinstance(runtime_signal, str) and runtime_signal:
            event_data["progress"]["runtime_signal"] = runtime_signal
        runtime_status = event_data.get("runtime_status")
        if isinstance(runtime_status, str) and runtime_status:
            event_data["progress"]["runtime_status"] = runtime_status
        thinking = event_data.get("thinking")
        if isinstance(thinking, str) and thinking:
            event_data["progress"]["thinking"] = thinking
        ac_tracking = coerce_ac_marker_update(event_data.get("ac_tracking"))
        if not ac_tracking.is_empty:
            event_data["progress"]["ac_tracking"] = ac_tracking.to_dict()
        return event.model_copy(update={"data": event_data})

    def build_session_tool_called_event(
        self,
        session_id: str,
        *,
        projected: Any,
    ) -> Any:
        """Create a shared session tool-call event from an AC runtime message."""
        if projected.tool_name is None:
            return None

        event = create_tool_called_event(
            session_id=session_id,
            tool_name=projected.tool_name,
            tool_input_preview=self.message_tool_input_preview(projected.tool_input),
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
        }
        return event.model_copy(update={"data": event_data})

    async def emit_coordinator_started(
        self,
        execution_id: str,
        session_id: str,
        level: int,
        conflicts: list[Any],
    ) -> None:
        """Emit a level-scoped event when coordinator reconciliation starts."""
        runtime_scope = build_level_coordinator_runtime_scope(execution_id, level)
        event = BaseEvent(
            type="execution.coordinator.started",
            aggregate_type="execution",
            aggregate_id=self.coordinator_aggregate_id(execution_id, level),
            data={
                "execution_id": execution_id,
                "session_id": session_id,
                "scope": "level",
                "session_role": "coordinator",
                "stage_index": level - 1,
                "level_number": level,
                "session_scope_id": runtime_scope.aggregate_id,
                "session_state_path": runtime_scope.state_path,
                "conflict_count": len(conflicts),
                "conflicts": [
                    {
                        "file_path": conflict.file_path,
                        "ac_indices": list(conflict.ac_indices),
                    }
                    for conflict in conflicts
                ],
            },
        )
        await self._event_store.append(event)

    async def emit_coordinator_runtime_events(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
        *,
        format_tool_detail: ToolDetailFormatter,
    ) -> None:
        """Persist normalized coordinator runtime audit events at level scope."""
        aggregate_id = self.coordinator_aggregate_id(execution_id, review.level_number)
        base_data = {
            "execution_id": execution_id,
            "session_id": session_id,
            "coordinator_session_id": review.session_id,
            "scope": review.scope,
            "session_role": review.session_role,
            "stage_index": review.stage_index,
            "level_number": review.level_number,
            "session_scope_id": review.artifact_owner_id,
            "session_state_path": review.artifact_state_path,
        }

        for message in review.messages:
            projected = project_runtime_message(message)

            if projected.is_tool_call and projected.tool_name is not None:
                tool_input = projected.tool_input
                tool_event = BaseEvent(
                    type="execution.coordinator.tool.started",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "tool_name": projected.tool_name,
                        "tool_detail": format_tool_detail(projected.tool_name, tool_input),
                        "tool_input": tool_input,
                        **self.runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(tool_event)

            if projected.is_tool_result and projected.tool_name is not None:
                tool_result_event = BaseEvent(
                    type="execution.coordinator.tool.completed",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "tool_name": projected.tool_name,
                        "tool_result_text": projected.content,
                        **self.runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(tool_result_event)

            if projected.thinking:
                thinking_event = BaseEvent(
                    type="execution.coordinator.thinking",
                    aggregate_type="execution",
                    aggregate_id=aggregate_id,
                    data={
                        **base_data,
                        "thinking_text": projected.thinking,
                        **self.runtime_event_metadata(message),
                    },
                )
                await self._event_store.append(thinking_event)

    async def emit_coordinator_completed(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
    ) -> None:
        """Persist the coordinator reconciliation result as a level-scoped artifact."""
        event = BaseEvent(
            type="execution.coordinator.completed",
            aggregate_type="execution",
            aggregate_id=self.coordinator_aggregate_id(execution_id, review.level_number),
            data={
                "execution_id": execution_id,
                "session_id": session_id,
                "coordinator_session_id": review.session_id,
                **review.to_artifact_payload(),
                "conflicts_detected": [
                    {
                        "file_path": conflict.file_path,
                        "ac_indices": list(conflict.ac_indices),
                        "resolved": conflict.resolved,
                        "resolution_description": conflict.resolution_description,
                    }
                    for conflict in review.conflicts_detected
                ],
                "review_summary": review.review_summary,
                "fixes_applied": list(review.fixes_applied),
                "warnings_for_next_level": list(review.warnings_for_next_level),
                "duration_seconds": review.duration_seconds,
            },
        )
        await self._event_store.append(event)

    async def emit_investment_assessed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str,
        ac_index: int,
        is_sub_ac: bool,
        assessment: dict[str, Any],
        runtime_backend: str | None,
    ) -> None:
        """Persist the exact AC investment inputs used by effort policy."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.investment_assessed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "ac_id": runtime_identity.ac_id,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "ac_index": ac_index,
                    "is_decomposed_child": is_sub_ac,
                    **assessment,
                    "runtime_backend": runtime_backend,
                },
            )
        )

    async def emit_effort_routed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str,
        ac_index: int,
        is_sub_ac: bool,
        effort_level: str,
        effort_mode: str,
        base_reasoning_effort: str | None,
        runtime_backend: str | None,
        investment_assessment: dict[str, Any] | None = None,
    ) -> None:
        """Persist per-AC effort-routing telemetry."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.effort_routed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "ac_id": runtime_identity.ac_id,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "ac_index": ac_index,
                    "is_decomposed_child": is_sub_ac,
                    "effort_level": effort_level,
                    "effort_mode": effort_mode,
                    "base_reasoning_effort": base_reasoning_effort,
                    "runtime_backend": runtime_backend,
                    **(
                        {"investment_assessment": investment_assessment}
                        if investment_assessment is not None
                        else {}
                    ),
                },
            )
        )

    async def emit_model_routed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str,
        ac_index: int,
        is_sub_ac: bool,
        model_tier: str | None,
        model: str | None,
        model_mode: str,
        retry_attempt: int,
        runtime_backend: str | None,
        decomposition_trustworthy: bool = False,
        semantic_ac_key: str | None = None,
        base_model_tier: str | None = None,
        escalation_retry_threshold: int | None = None,
        model_escalated: bool = False,
    ) -> None:
        """Persist per-AC model-tier-routing telemetry.

        The sibling of :meth:`emit_effort_routed`: where that records how much
        reasoning a unit got, this records which model tier ran it. Both feed the
        frugality proof, and both are observe-only telemetry routed through
        ``_safe_emit_event`` so a degraded event store never blocks dispatch.
        """
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.model_routed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "ac_id": runtime_identity.ac_id,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "ac_index": ac_index,
                    "is_decomposed_child": is_sub_ac,
                    "decomposition_trustworthy": decomposition_trustworthy is True,
                    "child_downgrade_authorized": (is_sub_ac and decomposition_trustworthy is True),
                    "model_tier": model_tier,
                    "model": model,
                    "model_mode": model_mode,
                    "retry_attempt": retry_attempt,
                    "runtime_backend": runtime_backend,
                    "semantic_ac_key": semantic_ac_key,
                    "base_model_tier": base_model_tier,
                    "escalation_retry_threshold": escalation_retry_threshold,
                    "model_escalated": model_escalated,
                },
            )
        )

    async def emit_token_attribution(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str,
        ac_index: int,
        is_sub_ac: bool,
        retry_attempt: int,
        token_spend: float,
        usage_breakdown: dict[str, float],
        model: str | None,
        model_tier: str | None,
        model_mode: str | None,
        effort_level: str | None,
        runtime_backend: str | None,
    ) -> None:
        """Persist per-AC runtime-measured token spend (frugality-proof token axis).

        The token sibling of :meth:`emit_effort_routed` / :meth:`emit_model_routed`:
        the deterministic frugality proof joins this ``token_spend`` with the effort
        and grounding axes by ``ac_id`` + run anchor, so the payload carries
        ``execution_id`` exactly like the effort event does. ``token_source`` is
        pinned to ``"runtime_usage"`` because the spend is harvested from real
        runtime usage telemetry, never a character proxy. Observe-only: routed
        through ``_safe_emit_event`` so a degraded event store never blocks dispatch.
        """
        await self._safe_emit_event(
            BaseEvent(
                type=EVENT_TOKEN_ATTRIBUTION,
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "ac_id": runtime_identity.ac_id,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "ac_index": ac_index,
                    "is_decomposed_child": is_sub_ac,
                    "retry_attempt": retry_attempt,
                    "token_spend": token_spend,
                    "usage_breakdown": usage_breakdown,
                    "token_source": "runtime_usage",
                    "model": model,
                    "model_tier": model_tier,
                    "model_mode": model_mode,
                    "effort_level": effort_level,
                    "runtime_backend": runtime_backend,
                },
            )
        )

    async def emit_deliver_verdict(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str | None,
        is_sub_ac: bool,
        traceguard_verdict: str,
        unsupported_claim_rate: float,
        rejected_reasons: list[str],
        accepted_fact_count: int,
        semantic_ac_key: str | None = None,
        grounding_regression: bool | None = None,
        grounding_regression_mode: str | None = None,
    ) -> None:
        """Persist a per-AC TraceGuard deliver verdict (frugality-proof grounding axis).

        Observe-only telemetry: it records whether the leaf's delivered claim was
        grounded in cited evidence, never changing AC success/failure. Routed
        through ``_safe_emit_event``.

        ``grounding_regression`` is included only when the caller has an explicit
        deterministic policy for it.  The live path uses the documented
        ``fail_closed_live_traceguard`` policy: accepted means ``False`` (the
        lower-tier child is not rejected), while any rejection becomes ``True``
        because the isolated replay cannot prove that the parent-tier baseline
        would also reject it.  This is conservative rather than fabricated and
        lets the proof reach PASS only on journal-grounded accepted children.
        ``grounding_regression_mode`` records that provenance for audit consumers.
        """
        data: dict[str, Any] = {
            **runtime_identity.to_metadata(),
            "ac_id": runtime_identity.ac_id,
            "execution_id": execution_id,
            "session_id": session_id,
            "is_decomposed_child": is_sub_ac,
            "traceguard_verdict": traceguard_verdict,
            "unsupported_claim_rate": unsupported_claim_rate,
            "rejected_reasons": rejected_reasons,
            "accepted_fact_count": accepted_fact_count,
            "semantic_ac_key": semantic_ac_key,
        }
        # Strict bool only: a non-bool would be dropped by the proof's
        # ``_strict_bool`` guard anyway, so never smuggle a truthy string here.
        if isinstance(grounding_regression, bool):
            data["grounding_regression"] = grounding_regression
            if isinstance(grounding_regression_mode, str) and grounding_regression_mode.strip():
                data["grounding_regression_mode"] = grounding_regression_mode.strip()
        await self._safe_emit_event(
            BaseEvent(
                type=EVENT_DELIVER_VERDICT,
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data=data,
            )
        )

    async def observe_ac_activity(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str,
        semantic_ac_key: str,
        projected: Any,
        is_final: bool,
    ) -> None:
        """Emit bounded semantic phase/discovery events on material changes."""
        tool_name = projected.tool_name if isinstance(projected.tool_name, str) else ""
        normalized_tool = tool_name.casefold()
        tool_input = projected.tool_input if isinstance(projected.tool_input, dict) else {}

        phase: str | None = None
        purpose: str | None = None
        targets: list[str] = []
        if is_final:
            phase = "deliver"
        elif normalized_tool in {"read", "glob", "grep", "search", "list", "ls"}:
            phase = "discover"
            purpose = "Inspect the relevant repository surface for this acceptance criterion."
            for key in ("file_path", "path", "glob", "pattern"):
                value = tool_input.get(key)
                if isinstance(value, str) and value.strip():
                    label = " ".join(value.split())[:160]
                    if key == "pattern" and normalized_tool in {"grep", "search"}:
                        label = f"search:{label}"
                    targets.append(label)
        elif normalized_tool in {"write", "edit", "apply_patch", "multiedit"}:
            phase = "implement"
        elif normalized_tool in {"bash", "shell", "exec", "exec_command"}:
            command = tool_input.get("command") or tool_input.get("cmd")
            command_text = command.casefold() if isinstance(command, str) else ""
            if any(
                marker in command_text
                for marker in (
                    "pytest",
                    " ruff",
                    "ruff ",
                    "mypy",
                    "npm test",
                    "pnpm test",
                    "cargo test",
                    "go test",
                    "typecheck",
                    "lint",
                )
            ):
                phase = "verify"
            elif command_text and any(
                marker in command_text
                for marker in ("rg ", "grep ", "find ", "ls ", "sed -n", "git status")
            ):
                phase = "discover"
                purpose = "Inspect repository state needed to plan or verify this AC."
                targets = ["repository state"]

        scope_key = runtime_identity.ac_id
        if phase is not None and self._last_ac_phase.get(scope_key) != phase:
            self._last_ac_phase[scope_key] = phase
            await self._safe_emit_event(
                BaseEvent(
                    type="execution.ac.phase_changed",
                    aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                    aggregate_id=runtime_identity.session_scope_id,
                    data={
                        **runtime_identity.to_metadata(),
                        "schema_version": 1,
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "semantic_ac_key": semantic_ac_key,
                        "phase": phase,
                        "source": "deterministic_tool_classifier",
                    },
                )
            )

        bounded_targets = tuple(dict.fromkeys(targets))[:5]
        if phase != "discover" or not bounded_targets or purpose is None:
            return
        signature = (bounded_targets, purpose)
        if self._last_discovery_signature.get(scope_key) == signature:
            return
        self._last_discovery_signature[scope_key] = signature
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.discovery.updated",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "semantic_ac_key": semantic_ac_key,
                    "ac_index": runtime_identity.root_ac_index
                    if runtime_identity.root_ac_index is not None
                    else runtime_identity.ac_index,
                    "targets": list(bounded_targets),
                    "purpose": purpose[:240],
                    "source": "deterministic_tool_classifier",
                },
            )
        )

    async def emit_shadow_replay(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str | None,
        session_id: str | None,
        ac_index: int,
        is_sub_ac: bool,
        baseline_token_spend: float,
        baseline_mode: str,
        baseline_model: str | None,
        baseline_tier: str | None,
        decomposition_trustworthy: bool,
    ) -> None:
        """Persist a decomposed child's parent-tier baseline (frugality-proof AC5).

        The baseline axis the deterministic proof joins by ``ac_id`` + run anchor:
        ``baseline_token_spend`` is what the SAME child cost when re-executed at
        the PARENT's model tier / reasoning effort in an isolated workspace (see
        :mod:`ouroboros.orchestrator.shadow_replay`). ``decomposition_trustworthy``
        excludes forced-atomic children from the proof. Observe-only, routed
        through ``_safe_emit_event`` so a degraded event store never blocks the
        (already-completed) real AC.
        """
        await self._safe_emit_event(
            BaseEvent(
                type=EVENT_SHADOW_REPLAY,
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "ac_id": runtime_identity.ac_id,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "ac_index": ac_index,
                    "is_decomposed_child": is_sub_ac,
                    "baseline_token_spend": baseline_token_spend,
                    "baseline_mode": baseline_mode,
                    "baseline_model": baseline_model,
                    "baseline_tier": baseline_tier,
                    "decomposition_trustworthy": decomposition_trustworthy,
                },
            )
        )

    async def emit_heartbeat(
        self,
        *,
        session_id: str,
        ac_index: int,
        ac_id: str,
        elapsed_seconds: float,
        message_count: int,
        node_identity: ExecutionNodeIdentity | None,
    ) -> None:
        """Emit liveness heartbeat with optional node identity metadata."""
        heartbeat_event = create_heartbeat_event(
            session_id=session_id,
            ac_index=ac_index,
            ac_id=ac_id,
            elapsed_seconds=elapsed_seconds,
            message_count=message_count,
        )
        if node_identity is not None:
            heartbeat_event.data.update(node_identity.to_event_metadata())
        await self._safe_emit_event(heartbeat_event)

    async def emit_atomic_tool_started(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        tool_name: str,
        tool_detail: str,
        tool_input: dict[str, Any],
        runtime_metadata: dict[str, Any],
    ) -> None:
        """Emit AC-scoped tool start event for TUI consumers."""
        await self._event_store.append(
            BaseEvent(
                type="execution.tool.started",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "tool_name": tool_name,
                    "tool_detail": tool_detail,
                    "tool_input": tool_input,
                    **runtime_metadata,
                },
            )
        )

    async def emit_atomic_tool_completed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        tool_name: str,
        tool_result_text: str,
        runtime_metadata: dict[str, Any],
    ) -> None:
        """Emit AC-scoped tool completion event for TUI consumers."""
        await self._event_store.append(
            BaseEvent(
                type="execution.tool.completed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "tool_name": tool_name,
                    "tool_result_text": tool_result_text,
                    **runtime_metadata,
                },
            )
        )

    async def emit_atomic_thinking(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        thinking_text: str,
        runtime_metadata: dict[str, Any],
    ) -> None:
        """Emit AC-scoped thinking event for TUI consumers."""
        await self._event_store.append(
            BaseEvent(
                type="execution.agent.thinking",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "thinking_text": thinking_text,
                    **runtime_metadata,
                },
            )
        )

    async def emit_atomic_typed_evidence_observed(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        data: dict[str, Any],
    ) -> None:
        """Persist typed-evidence metadata for atomic AC completion."""
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.typed_evidence.observed",
                aggregate_type=runtime_identity.runtime_scope.aggregate_type,
                aggregate_id=runtime_identity.session_scope_id,
                data=data,
            )
        )

    async def emit_subtask_event(
        self,
        execution_id: str,
        ac_index: int,
        sub_task_index: int,
        sub_task_content: str,
        status: str,
        node_identity: ExecutionNodeIdentity | None,
        *,
        label: str,
    ) -> None:
        """Emit sub-task event for TUI tree updates."""
        ac_index_1 = ac_index + 1
        node_metadata = node_identity.to_event_metadata() if node_identity is not None else {}
        node_event_type = (
            "execution.node.created" if status == "pending" else "execution.node.updated"
        )
        if node_identity is not None:
            node_event = BaseEvent(
                type=node_event_type,
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    **node_metadata,
                    "node_kind": "sub_ac",
                    "content": sub_task_content,
                    "label": label,
                    "status": status,
                    "legacy_ac_index": ac_index_1,
                    "legacy_sub_task_index": sub_task_index,
                    "legacy_sub_task_id": f"ac_{ac_index_1}_sub_{sub_task_index}",
                },
            )
            await self._event_store.append(node_event)

        event = BaseEvent(
            type="execution.subtask.updated",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                **node_metadata,
                "ac_index": ac_index_1,
                "sub_task_index": sub_task_index,
                "sub_task_id": f"ac_{ac_index_1}_sub_{sub_task_index}",
                "content": sub_task_content,
                "label": label,
                "status": status,
            },
        )
        await self._event_store.append(event)

    async def emit_level_started(
        self,
        session_id: str,
        level: int,
        ac_indices: list[int],
        total_levels: int,
        *,
        decomposition_profile_metadata: dict[str, Any],
    ) -> None:
        """Emit event when a parallel level starts."""
        event = BaseEvent(
            type="execution.decomposition.level_started",
            aggregate_type="execution",
            aggregate_id=session_id,
            data={
                "level": level - 1,
                "total_levels": total_levels,
                "child_indices": ac_indices,
                "ac_count": len(ac_indices),
                **decomposition_profile_metadata,
            },
        )
        await self._event_store.append(event)

    async def emit_level_completed(
        self,
        session_id: str,
        level: int,
        success_count: int,
        failure_count: int,
        blocked_count: int = 0,
        started: bool = True,
        outcome: str | None = None,
    ) -> None:
        """Emit event when a parallel level completes."""
        event = BaseEvent(
            type="execution.decomposition.level_completed",
            aggregate_type="execution",
            aggregate_id=session_id,
            data={
                "level": level - 1,
                "successful": success_count,
                "failed": failure_count,
                "blocked": blocked_count,
                "started": started,
                "outcome": outcome or StageExecutionOutcome.SUCCEEDED.value,
                "total": success_count + failure_count + blocked_count,
            },
        )
        await self._event_store.append(event)

    async def emit_workflow_progress(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        ac_retry_attempts: dict[int, int] | None,
        executing_indices: list[int],
        completed_count: int,
        current_level: int,
        total_levels: int,
        activity: str = "Executing",
        messages_count: int = 0,
        tool_calls_count: int = 0,
    ) -> None:
        """Emit workflow progress event for TUI updates."""
        acceptance_criteria = []
        for i, ac_content in enumerate(ac_text(seed_ac) for seed_ac in seed.acceptance_criteria):
            status = ac_statuses.get(i, "pending")
            retry_attempt = (ac_retry_attempts or {}).get(i, 0)
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id or session_id,
                ac_index=i,
            )
            runtime_scope = build_ac_runtime_scope(
                i,
                execution_context_id=execution_id or session_id,
                retry_attempt=retry_attempt,
                node_id=node_identity.node_id,
                node_path=node_identity.path,
            )
            acceptance_criteria.append(
                {
                    **node_identity.to_event_metadata(),
                    "index": i + 1,
                    "ac_id": runtime_scope.aggregate_id,
                    "content": ac_content,
                    "status": status,
                    "retry_attempt": retry_attempt,
                    "attempt_number": runtime_scope.attempt_number,
                    "elapsed": "",
                }
            )

        current_ac_index = executing_indices[0] if executing_indices else None

        if executing_indices:
            activity_detail = (
                f"Level {current_level}/{total_levels}: ACs {[i + 1 for i in executing_indices]}"
            )
        else:
            activity_detail = f"Level {current_level}/{total_levels}"

        event = create_workflow_progress_event(
            execution_id=execution_id,
            session_id=session_id,
            acceptance_criteria=acceptance_criteria,
            completed_count=completed_count,
            total_count=len(seed.acceptance_criteria),
            current_ac_index=current_ac_index,
            current_phase="Deliver",
            activity=activity,
            activity_detail=activity_detail,
            messages_count=messages_count,
            tool_calls_count=tool_calls_count,
        )
        await self._event_store.append(event)
