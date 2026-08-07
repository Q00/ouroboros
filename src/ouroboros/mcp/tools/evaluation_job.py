"""Plugin dispatch and background execution helpers for formal evaluation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.config import get_auto_evolve_max_generations
from ouroboros.core.errors import ValidationError
from ouroboros.core.seed import Seed, ac_text
from ouroboros.mcp.tools.evaluate_ralph_chain import enqueue_chained_ralph
from ouroboros.mcp.tools.seed_handoff import project_worker_safe_seed
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_PLUGIN,
    build_evaluate_subagent,
    dispatch_plugin_terminal,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerSafeEvaluationInputs:
    seed_content: str | None
    acceptance_criterion: str | None
    artifact: str


def worker_safe_evaluation_inputs(
    seed_content: object,
    acceptance_criterion: str | None,
    artifact: str,
) -> WorkerSafeEvaluationInputs:
    """Project every parent-owned evaluation field crossing a worker boundary."""

    if not isinstance(seed_content, str):
        return WorkerSafeEvaluationInputs(None, acceptance_criterion, artifact)
    projection = project_worker_safe_seed(seed_content)
    return WorkerSafeEvaluationInputs(
        projection.seed_content,
        projection.redact(acceptance_criterion),
        projection.redact(artifact) or "[REDACTED ARTIFACT]",
    )


def restore_seed_handoff(
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    registry: Any | None,
) -> dict[str, Any]:
    """Restore a raw Seed only inside the parent process that minted its handle."""

    restored = dict(arguments)
    handoff_id = arguments.get("seed_handoff_id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return restored
    seed_content = registry.resolve(handoff_id, session_id=session_id) if registry else None
    if seed_content is None:
        raise ValueError("seed_handoff_id is unknown or does not belong to this session")
    restored["seed_content"] = seed_content
    # The opaque handle is a one-process plugin boundary, not durable worker
    # input.  Consume it before StartEvaluate serializes detached arguments so
    # a fresh worker can use the restored parent-owned Seed without consulting
    # an empty process-local registry.
    restored.pop("seed_handoff_id", None)
    return restored


def snapshot_auto_evolve_policy(
    arguments: Mapping[str, Any],
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Freeze effect-bearing evaluation policy before durable job handoff."""

    snapshotted = dict(arguments)
    snapshotted["auto_evolve"] = enabled
    if not enabled:
        return snapshotted
    raw_budget = snapshotted.get("_auto_evolve_max_generations")
    if raw_budget is None:
        snapshotted["_auto_evolve_max_generations"] = get_auto_evolve_max_generations()
    elif (
        not isinstance(raw_budget, int) or isinstance(raw_budget, bool) or not 1 <= raw_budget <= 10
    ):
        raise ValueError("invalid internal auto-evolve generation budget")
    return snapshotted


def resolve_auto_evolve_policy(
    arguments: Mapping[str, Any],
    *,
    configured_enabled: bool,
) -> tuple[dict[str, Any], bool]:
    """Resolve the public override and snapshot its effect-bearing budget."""

    from ouroboros.mcp.tools.execution_handlers import resolve_auto_evaluate

    enabled = resolve_auto_evaluate(configured_enabled, arguments.get("auto_evolve"))
    return snapshot_auto_evolve_policy(arguments, enabled=enabled), enabled


def _acceptance_criteria(arguments: Mapping[str, Any]) -> tuple[tuple[str, ...], Seed | None]:
    raw = arguments.get("acceptance_criteria")
    criteria = (
        tuple(
            str(item).strip()
            for item in raw
            if isinstance(item, (str, int, float)) and str(item).strip()
        )
        if isinstance(raw, list)
        else ()
    )
    singular = arguments.get("acceptance_criterion")
    if not criteria and singular and str(singular).strip():
        criteria = (str(singular).strip(),)
    seed = None
    seed_content = arguments.get("seed_content")
    if seed_content:
        try:
            seed = Seed.from_dict(yaml.safe_load(seed_content))
            if not criteria:
                criteria = tuple(
                    text.strip()
                    for criterion in seed.acceptance_criteria
                    if (text := ac_text(criterion).strip())
                )
        except (yaml.YAMLError, ValidationError, PydanticValidationError) as exc:
            log.warning("mcp.tool.start_evaluate.seed_parse_warning", error=str(exc))
    return criteria, seed


async def dispatch_plugin_evaluation(
    *,
    arguments: Mapping[str, Any],
    session_id: str,
    artifact: str,
    event_store: Any,
    resolve_working_dir: Callable[[str | None, Seed | None], Awaitable[Path]],
) -> Any:
    """Delegate a non-evolving evaluation with normalized checklist context."""

    criteria, seed = _acceptance_criteria(arguments)
    rendered_ac = (
        "\n".join(f"{index + 1}. {criterion}" for index, criterion in enumerate(criteria))
        if len(criteria) > 1
        else criteria[0]
        if criteria
        else None
    )
    worker_inputs = worker_safe_evaluation_inputs(
        arguments.get("seed_content"), rendered_ac, artifact
    )
    working_dir = await resolve_working_dir(arguments.get("working_dir"), seed)
    payload = build_evaluate_subagent(
        session_id=session_id,
        artifact=worker_inputs.artifact,
        artifact_type=arguments.get("artifact_type", "code"),
        seed_content=worker_inputs.seed_content,
        acceptance_criterion=worker_inputs.acceptance_criterion,
        working_dir=str(working_dir),
        trigger_consensus=arguments.get("trigger_consensus", False),
    )
    return await dispatch_plugin_terminal(
        event_store,
        session_id=session_id,
        payload=payload,
        response_shape={
            "job_id": None,
            "session_id": session_id,
            "status": DELEGATED_TO_PLUGIN,
            "dispatch_mode": "plugin",
            "artifact_type": arguments.get("artifact_type", "code"),
            "trigger_consensus": arguments.get("trigger_consensus", False),
        },
    )


async def run_evaluation_job(
    *,
    evaluate_handler: Any,
    arguments: dict[str, Any],
    session_id: str,
    deadline_seconds: float,
    force_in_process: bool,
    auto_evolve: bool,
    event_store: Any,
    job_manager: Any,
    start_ralph_handler: Any | None,
) -> MCPToolResult:
    """Run formal evaluation and, on explicit rejection, enqueue Ralph."""

    evaluation_arguments = (
        {**arguments, "_force_in_process": True} if force_in_process else arguments
    )
    try:
        result = (
            await asyncio.wait_for(
                evaluate_handler.handle(evaluation_arguments), timeout=deadline_seconds
            )
            if deadline_seconds > 0
            else await evaluate_handler.handle(evaluation_arguments)
        )
    except TimeoutError:
        retry = f"ooo evaluate {session_id}"
        return MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=f"Evaluation timed out before the formal verdict completed.\nRetry: {retry}",
                ),
            ),
            is_error=True,
            meta={
                "session_id": session_id,
                "status": "timed_out",
                "evaluation_status": "timed_out",
                "next_step": retry,
            },
        )
    if result.is_err:
        raise RuntimeError(str(result.error))
    if auto_evolve and result.value.meta.get("final_approved") is False:
        return await enqueue_chained_ralph(
            result.value,
            session_id=session_id,
            arguments=arguments,
            event_store=event_store,
            job_manager=job_manager,
            start_ralph_handler=start_ralph_handler,
        )
    return result.value
