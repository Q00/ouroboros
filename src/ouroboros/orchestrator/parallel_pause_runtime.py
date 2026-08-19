"""Durable pause projections shared by the parallel execution owner."""

from __future__ import annotations

from collections.abc import Callable

from ouroboros.core.seed import Seed, ac_text, derive_semantic_ac_key
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionDecisionRecord,
    DecompositionDisposition,
)
from ouroboros.orchestrator.evidence.runtime_metadata import _SiblingACRef
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.orchestrator.route_policy import RouteCandidate
from ouroboros.persistence.event_store import EventStore


async def persist_parallel_route_pause(
    *,
    event_store: EventStore,
    seed: Seed,
    result: ACExecutionResult,
    root_ac_index: int,
    session_id: str,
    execution_id: str,
    episode_id: str,
    prior_route_ids: tuple[str, ...],
    retry_prompt_extra: str,
    sibling_acs: tuple[_SiblingACRef, ...],
    route_id_override: str | None,
    expected_route_candidate: RouteCandidate | None,
    is_resumable_runtime_handle: Callable[[RuntimeHandle | None], bool],
) -> None:
    """Bind a quota pause to the exact capsule and resumable provider boundary."""

    candidate = result.route_candidate
    if candidate is None:
        raise RuntimeError("bounded route pause lost its active route candidate")
    attempt_budget_progress = result.attempt_budget_progress
    if attempt_budget_progress is None:
        raise RuntimeError("parallel route pause lost its finite budget progress")
    runtime_handle = result.runtime_handle
    runtime_metadata = runtime_handle.metadata if runtime_handle is not None else {}
    runtime_scope_id = runtime_metadata.get("session_scope_id")
    dispatch_id = runtime_metadata.get("ac_dispatch_id")
    capsule_fingerprint = runtime_metadata.get("ac_capsule_fingerprint")
    if (
        runtime_handle is None
        or not is_resumable_runtime_handle(runtime_handle)
        or not isinstance(runtime_scope_id, str)
        or not runtime_scope_id
        or not isinstance(dispatch_id, str)
        or len(dispatch_id) != 32
        or any(char not in "0123456789abcdef" for char in dispatch_id)
        or not isinstance(capsule_fingerprint, str)
        or len(capsule_fingerprint) != 71
        or not capsule_fingerprint.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in capsule_fingerprint[7:])
    ):
        raise RuntimeError("parallel route pause has no exact resumable provider boundary")
    if result.retry_attempt != len(prior_route_ids):
        raise RuntimeError("parallel route pause retry attempt crossed route history")
    if len(retry_prompt_extra) > 16_384:
        raise RuntimeError("parallel route pause retry prompt exceeds its durable bound")
    if len(sibling_acs) > len(seed.acceptance_criteria) or any(
        type(index) is not int
        or index < 0
        or index >= len(seed.acceptance_criteria)
        or content != ac_text(seed.acceptance_criteria[index])
        for index, content in sibling_acs
    ):
        raise RuntimeError("parallel route pause has an invalid sibling population")
    if prior_route_ids:
        if route_id_override != candidate.route_id or expected_route_candidate != candidate:
            raise RuntimeError("parallel successor pause lost its exact route override")
    elif route_id_override is not None or expected_route_candidate is not None:
        raise RuntimeError("parallel initial pause invented a route override")
    criterion = seed.acceptance_criteria[root_ac_index]
    semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
    await event_store.append(
        BaseEvent(
            type="execution.ac.route_paused",
            aggregate_type="execution",
            aggregate_id=execution_id or session_id,
            data={
                "schema_version": 3,
                "execution_id": execution_id,
                "session_id": session_id,
                "root_ac_index": root_ac_index,
                "semantic_ac_key": semantic_ac_key,
                "call_site": "parallel",
                "episode_id": episode_id,
                "attempt_index": len(prior_route_ids),
                "prior_route_ids": list(prior_route_ids),
                "route": candidate.to_contract_data(),
                "resume_state": {
                    "retry_attempt": result.retry_attempt,
                    "retry_prompt_extra": retry_prompt_extra,
                    "sibling_acs": [
                        {"ac_index": index, "content": content} for index, content in sibling_acs
                    ],
                    "route_id_override": route_id_override,
                    "expected_route_candidate": (
                        expected_route_candidate.to_contract_data()
                        if expected_route_candidate is not None
                        else None
                    ),
                    "runtime_scope_id": runtime_scope_id,
                    "dispatch_id": dispatch_id,
                    "capsule_fingerprint": capsule_fingerprint,
                    "attempt_budget_progress": attempt_budget_progress.to_contract_data(),
                },
                "recoverable_pause": True,
                "final_acceptance_declared": False,
            },
        )
    )


def project_partial_composite_pause(
    *,
    result: ACExecutionResult,
    node_identity: ExecutionNodeIdentity,
    workspace_root: str,
    node_budget: list[int],
    canonical_decomposition_decision: Callable[
        [object], tuple[DecompositionDecisionRecord, dict[str, object], str]
    ],
    serialize_composite_result_tree: Callable[..., dict[str, object]],
    has_usage_limit_pause: Callable[[ACExecutionResult], bool],
    is_resumable_runtime_handle: Callable[[RuntimeHandle | None], bool],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Project every composite frame down to one exact paused leaf."""

    decision = result.decomposition_decision
    if not result.is_decomposed or decision is None or not result.sub_results:
        raise RuntimeError("partial composite pause lost its split result tree")
    parsed, decision_data, fingerprint = canonical_decomposition_decision(decision.to_dict())
    if (
        parsed.disposition is not DecompositionDisposition.SPLIT
        or parsed.node_id != node_identity.node_id
        or result.depth != node_identity.depth
    ):
        raise RuntimeError("partial composite pause crossed decomposition node identity")

    paused_child_index = len(result.sub_results) - 1
    paused_child = result.sub_results[paused_child_index]
    completed_prefix = result.sub_results[:paused_child_index]
    if (
        paused_child_index >= len(parsed.children)
        or tuple(child.description for child in parsed.children[:paused_child_index])
        != tuple(child.ac_content for child in completed_prefix)
        or any(
            child.ac_index != result.ac_index * 100 + child_index or child.depth != result.depth + 1
            for child_index, child in enumerate(completed_prefix)
        )
        or parsed.children[paused_child_index].description != paused_child.ac_content
        or paused_child.ac_index != result.ac_index * 100 + paused_child_index
        or paused_child.depth != result.depth + 1
        or not has_usage_limit_pause(paused_child)
        or any(has_usage_limit_pause(child) for child in completed_prefix)
    ):
        raise RuntimeError("partial composite pause has an invalid child prefix")

    expected_child = node_identity.child(paused_child_index)
    frame: dict[str, object] = {
        "completed_children": [
            serialize_composite_result_tree(
                child,
                node_budget=node_budget,
                workspace_root=workspace_root,
            )
            for child in completed_prefix
        ],
        "paused_child_index": paused_child_index,
        "paused_child_node_id": expected_child.node_id,
        "paused_child_ac_index": paused_child.ac_index,
        "paused_child_content": paused_child.ac_content,
        "paused_child_retry_attempt": paused_child.retry_attempt,
        "decomposition_decision": decision_data,
        "decomposition_fingerprint": fingerprint,
    }
    if paused_child.is_decomposed:
        nested_frames, paused_leaf = project_partial_composite_pause(
            result=paused_child,
            node_identity=expected_child,
            workspace_root=workspace_root,
            node_budget=node_budget,
            canonical_decomposition_decision=canonical_decomposition_decision,
            serialize_composite_result_tree=serialize_composite_result_tree,
            has_usage_limit_pause=has_usage_limit_pause,
            is_resumable_runtime_handle=is_resumable_runtime_handle,
        )
        return [frame, *nested_frames], paused_leaf

    runtime_handle = paused_child.runtime_handle
    runtime_metadata = runtime_handle.metadata if runtime_handle is not None else {}
    runtime_scope_id = runtime_metadata.get("session_scope_id")
    dispatch_id = runtime_metadata.get("ac_dispatch_id")
    capsule_fingerprint = runtime_metadata.get("ac_capsule_fingerprint")
    if (
        runtime_handle is None
        or not is_resumable_runtime_handle(runtime_handle)
        or runtime_metadata.get("node_id") != expected_child.node_id
        or not isinstance(runtime_scope_id, str)
        or not runtime_scope_id
        or not isinstance(dispatch_id, str)
        or len(dispatch_id) != 32
        or any(char not in "0123456789abcdef" for char in dispatch_id)
        or not isinstance(capsule_fingerprint, str)
        or len(capsule_fingerprint) != 71
        or not capsule_fingerprint.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in capsule_fingerprint[7:])
    ):
        raise RuntimeError("partial composite pause has no exact resumable leaf provider boundary")
    attempt_budget_progress = paused_child.attempt_budget_progress
    if attempt_budget_progress is None:
        raise RuntimeError("partial composite pause lost its finite budget progress")
    return [frame], {
        "node_id": expected_child.node_id,
        "ac_index": paused_child.ac_index,
        "ac_content": paused_child.ac_content,
        "retry_attempt": paused_child.retry_attempt,
        "runtime_scope_id": runtime_scope_id,
        "dispatch_id": dispatch_id,
        "capsule_fingerprint": capsule_fingerprint,
        "attempt_budget_progress": attempt_budget_progress.to_contract_data(),
    }


__all__ = ["persist_parallel_route_pause", "project_partial_composite_pause"]
