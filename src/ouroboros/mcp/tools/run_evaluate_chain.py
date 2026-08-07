"""Run-to-evaluate successor construction and fail-open result projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.core.errors import ValidationError
from ouroboros.core.seed import Seed, ac_texts
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

log = structlog.get_logger(__name__)


def snapshot_run_successor_policy(
    arguments: dict[str, Any],
    *,
    configured_auto_evaluate: bool,
    configured_auto_evolve: bool,
) -> tuple[dict[str, Any], bool, bool]:
    """Freeze run successor policy before detached execute re-entry."""

    snapshotted = dict(arguments)
    auto_evaluate = arguments.get("auto_evaluate")
    if not isinstance(auto_evaluate, bool):
        auto_evaluate = configured_auto_evaluate
    auto_evolve = arguments.get("auto_evolve")
    if not isinstance(auto_evolve, bool):
        auto_evolve = configured_auto_evolve
    snapshotted.update(auto_evaluate=auto_evaluate, auto_evolve=auto_evolve)
    return snapshotted, auto_evaluate, auto_evolve


def is_evaluable_run_result(result: MCPToolResult) -> bool:
    """Return whether a terminal run produced evidence formal evaluation can judge."""

    status = result.meta.get("status")
    return status in {"completed", "failed"} and bool(result.text_content.strip())


def _append_text(result: MCPToolResult, text: str, meta: dict[str, Any]) -> MCPToolResult:
    return MCPToolResult(
        content=(*result.content, MCPContentItem(type=ContentType.TEXT, text=text)),
        is_error=result.is_error,
        meta=meta,
        structured_content=result.structured_content,
    )


def _retry_step(session_id: str | None) -> str:
    return f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"


def _failure(result: MCPToolResult, session_id: str | None, error: str) -> MCPToolResult:
    meta = dict(result.meta)
    if "verification_status" not in meta:
        meta.update(
            evaluated=False,
            verification_status="executed_unverified",
            formal_evaluation_required=True,
        )
    meta.update(
        evaluation_status="enqueue_failed",
        evaluation_error=error[:1000],
        next_step=_retry_step(session_id),
    )
    return _append_text(
        result,
        "\nFormal Evaluation: enqueue failed; run verdict remains unchanged.\n"
        f"Evaluation Error: {error[:1000]}\nNext: {_retry_step(session_id)}\n",
        meta,
    )


def _evaluation_arguments(
    result: MCPToolResult,
    *,
    session_id: str | None,
    seed_content: str,
    working_dir: Path,
    auto_evolve: bool,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "session_id": session_id,
        "artifact": result.text_content or "Execution completed successfully.",
        "artifact_type": "code",
        "seed_content": seed_content,
        "working_dir": str(working_dir),
        "auto_evolve": auto_evolve,
        "_source_execution_status": result.meta.get("status"),
    }
    try:
        parsed = yaml.safe_load(seed_content)
        if isinstance(parsed, dict):
            criteria = [
                text.strip() for text in ac_texts(Seed.from_dict(parsed).acceptance_criteria)
            ]
            if criteria:
                arguments["acceptance_criteria"] = criteria
        else:
            log.warning(
                "mcp.tool.start_execute_seed.chained_evaluate.seed_not_mapping",
                session_id=session_id,
            )
    except yaml.YAMLError as exc:
        log.warning(
            "mcp.tool.start_execute_seed.chained_evaluate.yaml_error",
            session_id=session_id,
            error=str(exc),
        )
    except (ValidationError, PydanticValidationError) as exc:
        log.warning(
            "mcp.tool.start_execute_seed.chained_evaluate.seed_validation_error",
            session_id=session_id,
            error=str(exc),
        )
    return arguments


async def enqueue_chained_evaluation(
    run_result: MCPToolResult,
    *,
    session_id: str | None,
    seed_content: str,
    working_dir: Path,
    auto_evolve: bool,
    start_evaluate_handler: Any | None,
) -> MCPToolResult:
    """Start a pollable formal evaluation without changing the run verdict."""

    arguments = _evaluation_arguments(
        run_result,
        session_id=session_id,
        seed_content=seed_content,
        working_dir=working_dir,
        auto_evolve=auto_evolve,
    )
    try:
        if start_evaluate_handler is None:
            raise RuntimeError(
                "Automatic evaluation chaining is not configured: "
                "StartEvaluateHandler dependency is missing"
            )
        started = await start_evaluate_handler.handle(arguments)
        if started.is_err:
            raise RuntimeError(started.error.message)
        job_id = started.value.meta.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("StartEvaluateHandler did not return a pollable evaluation job_id")
    except Exception as exc:  # noqa: BLE001 - evaluation must never flip run success.
        return _failure(run_result, session_id, str(exc))

    retry = _retry_step(session_id)
    meta = {
        **run_result.meta,
        "evaluated": False,
        "verification_status": "evaluation_enqueued",
        "formal_evaluation_required": True,
        "chained_evaluate_job_id": job_id,
        "evaluation_status": "enqueued",
        "next_step": f"ouroboros_job_wait {job_id}, then ouroboros_job_result {job_id}",
        "manual_retry_next_step": retry,
    }
    return _append_text(
        run_result,
        "\nFormal Evaluation: queued as a bounded background job\n"
        f"Chained Evaluation Job ID: {job_id}\n"
        f"Next: poll ouroboros_job_wait(job_id={job_id}) and "
        f"then ouroboros_job_result(job_id={job_id}).\nManual Retry: {retry}\n",
        meta,
    )
