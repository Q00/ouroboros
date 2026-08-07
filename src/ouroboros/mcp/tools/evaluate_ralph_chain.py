"""Deterministic bridge from formal evaluation results to Ralph lineage state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import yaml

from ouroboros.config import get_auto_evolve_max_generations
from ouroboros.core.errors import ValidationError
from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import Seed, ac_text
from ouroboros.events.lineage import lineage_created, lineage_generation_completed
from ouroboros.evolution.loop_support import (
    LineageWinnerAdvanced,
    run_durable_lineage_single_flight,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


@dataclass(frozen=True, slots=True)
class RalphSuccessorReceipt:
    """Durable identity and immutable policy for one Ralph successor."""

    job_id: str
    max_generations: int


def _checklist_by_text(checklist: object) -> dict[str, list[Mapping[str, Any]]]:
    indexed: dict[str, list[Mapping[str, Any]]] = {}
    if not isinstance(checklist, Sequence) or isinstance(checklist, str | bytes):
        return indexed
    for raw_item in checklist:
        if not isinstance(raw_item, Mapping):
            continue
        raw_text = raw_item.get("ac_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        indexed.setdefault(raw_text.strip(), []).append(raw_item)
    return indexed


def _evidence_text(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    evidence = item.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, str | bytes):
        parts.extend(str(value).strip() for value in evidence if str(value).strip())
    for key in ("reasoning", "failure_reason"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return "; ".join(parts)


def ac_results_from_checklist(seed: Seed, checklist: object) -> tuple[ACResult, ...]:
    """Map evaluator checklist rows onto the Seed's stable AC order."""

    # Single-AC evaluations do not emit the checklist contract. Ralph still
    # converges via its full-graph fallback, so do not fabricate a verdict.
    if not isinstance(checklist, Sequence) or isinstance(checklist, str | bytes):
        return ()
    rows = _checklist_by_text(checklist)
    results: list[ACResult] = []
    for index, criterion in enumerate(seed.acceptance_criteria):
        content = ac_text(criterion)
        matching = rows.get(content.strip(), [])
        item = matching.pop(0) if matching else None
        if item is None:
            results.append(
                ACResult(
                    ac_index=index,
                    ac_content=content,
                    semantic_ac_key=criterion.semantic_ac_key,
                    passed=False,
                    score=0.0,
                    evidence="No formal evaluation checklist result was produced for this AC.",
                    verification_method="formal_evaluation",
                    ac_verdict_state="not_evaluated",
                    final_verdict="fail",
                    rendered_verdict="NOT_EVALUATED",
                )
            )
            continue
        passed = item.get("passed") is True
        results.append(
            ACResult(
                ac_index=index,
                ac_content=content,
                semantic_ac_key=criterion.semantic_ac_key,
                passed=passed,
                score=1.0 if passed else 0.0,
                evidence=_evidence_text(item) or "Formal evaluation produced no evidence details.",
                verification_method="formal_evaluation",
                ac_verdict_state="evaluated",
                final_verdict="pass" if passed else "fail",
                rendered_verdict="PASS" if passed else "FAIL",
            )
        )
    return tuple(results)


def evaluation_summary_from_eval_meta(
    seed: Seed,
    meta: Mapping[str, Any],
    *,
    execution_completion_status: object = "completed",
) -> EvaluationSummary:
    """Build the generation-1 evaluation snapshot consumed by focused evolve."""

    approved = meta.get("final_approved") is True
    score_value = meta.get("pass_rate")
    score = float(score_value) if isinstance(score_value, int | float) else None
    feedback = meta.get("run_feedback")
    failure_reason = None
    if not approved and isinstance(feedback, Sequence) and not isinstance(feedback, str | bytes):
        rendered = [str(item).strip() for item in feedback if str(item).strip()]
        if rendered:
            failure_reason = "; ".join(rendered)
    if not approved and failure_reason is None:
        failure_reason = "formal evaluation rejected the run"
    raw_highest_stage = meta.get("highest_stage")
    highest_stage = (
        raw_highest_stage
        if isinstance(raw_highest_stage, int)
        and not isinstance(raw_highest_stage, bool)
        and 1 <= raw_highest_stage <= 3
        else 1
    )
    source_status = (
        execution_completion_status
        if isinstance(execution_completion_status, str)
        and execution_completion_status in {"completed", "failed"}
        else "completed"
    )
    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=highest_stage,
        score=score,
        failure_reason=failure_reason,
        ac_results=ac_results_from_checklist(seed, meta.get("checklist")),
        execution_completion_status=source_status,
        approval_status="approved" if approved else "rejected",
    )


def mint_chain_lineage_id(seed_id: str, session_id: str) -> str:
    """Mint one storage-safe deterministic lineage per Seed/run tuple."""

    identity = f"{seed_id}\0{session_id}".encode()
    # events.aggregate_id is VARCHAR(36) on length-enforcing production
    # databases. Keep the human-readable prefix and use 120 digest bits so the
    # complete tuple identity remains collision-resistant without embedding an
    # unbounded raw Seed ID.
    return f"ralph-{hashlib.sha256(identity).hexdigest()[:30]}"


async def seed_gen1_lineage(
    event_store: EventStore,
    *,
    lineage_id: str,
    seed: Seed,
    summary: EvaluationSummary,
) -> bool:
    """Project a completed generation 1 once; return whether events were appended."""

    async def _already_completed() -> bool:
        events = await event_store.replay_lineage(lineage_id)
        return any(
            event.type == "lineage.generation.completed"
            and event.data.get("generation_number") == 1
            for event in events
        )

    if await _already_completed():
        return False

    seed_json = json.dumps(seed.to_dict(), ensure_ascii=False)
    summary_json = summary.model_dump(mode="json")
    request_key = hashlib.sha256(
        json.dumps(
            {"seed": seed.to_dict(), "evaluation_summary": summary_json},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    async def _append_gen1() -> bool:
        events = await event_store.replay_lineage(lineage_id)
        if any(
            event.type == "lineage.generation.completed"
            and event.data.get("generation_number") == 1
            for event in events
        ):
            return False
        created = any(event.type == "lineage.created" for event in events)
        batch = [] if created else [lineage_created(lineage_id, seed.goal)]
        batch.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                evaluation_summary=summary_json,
                seed_json=seed_json,
            )
        )
        await event_store.append_batch(batch)
        return True

    try:
        return await run_durable_lineage_single_flight(
            event_store,
            lineage_id,
            request_key,
            _append_gen1,
            generation_number=1,
            encode=lambda appended: {"appended": appended},
            decode=lambda payload: payload.get("appended") is True,
            scope="evaluation-gen1",
        )
    except LineageWinnerAdvanced:
        # Concurrent formal evaluations can render different evidence text for
        # the same run. The first complete Gen1 is authoritative; recompute from
        # that durable state instead of turning the losing evaluation into a
        # chaining error.
        if await _already_completed():
            return False
        raise


async def _find_chained_ralph_job(job_manager: Any, lineage_id: str) -> str | None:
    existing = await job_manager.find_active_job_by_lineage(
        lineage_id,
        job_type="ralph",
        include_terminal=True,
    )
    if existing is None:
        return None
    job_id = existing.job_id
    return job_id if isinstance(job_id, str) and job_id else None


def _decode_generation_budget(payload: Mapping[str, Any]) -> int:
    max_generations = payload.get("max_generations")
    if (
        not isinstance(max_generations, int)
        or isinstance(max_generations, bool)
        or not 1 <= max_generations <= 10
    ):
        raise RuntimeError("Durable Ralph policy receipt has an invalid generation budget")
    return max_generations


async def _bind_ralph_generation_budget(
    event_store: EventStore,
    *,
    lineage_id: str,
    proposed_max_generations: int,
) -> int:
    """Persist the first accepted budget before any Ralph side effect occurs."""

    async def _select_proposed_budget() -> int:
        return proposed_max_generations

    request_key = hashlib.sha256(f"ralph-policy\0{lineage_id}".encode()).hexdigest()
    return await run_durable_lineage_single_flight(
        event_store,
        lineage_id,
        request_key,
        _select_proposed_budget,
        generation_number=1,
        encode=lambda value: {"max_generations": value},
        decode=_decode_generation_budget,
        scope="evaluation-ralph-policy",
    )


def _decode_ralph_job_receipt(payload: Mapping[str, Any]) -> RalphSuccessorReceipt:
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Durable Ralph successor receipt has no job_id")
    return RalphSuccessorReceipt(
        job_id=job_id,
        max_generations=_decode_generation_budget(payload),
    )


async def _claim_or_start_chained_ralph(
    *,
    lineage_id: str,
    arguments: Mapping[str, Any],
    max_generations: int,
    event_store: EventStore,
    job_manager: Any,
    start_ralph_handler: Any | None,
) -> RalphSuccessorReceipt:
    """Reconnect or durably elect the sole Ralph successor for a lineage."""

    bound_max_generations = await _bind_ralph_generation_budget(
        event_store,
        lineage_id=lineage_id,
        proposed_max_generations=max_generations,
    )

    existing_job_id = await _find_chained_ralph_job(job_manager, lineage_id)
    if existing_job_id is not None:
        return RalphSuccessorReceipt(existing_job_id, bound_max_generations)

    async def _start_once() -> RalphSuccessorReceipt:
        recovered_job_id = await _find_chained_ralph_job(job_manager, lineage_id)
        if recovered_job_id is not None:
            return RalphSuccessorReceipt(recovered_job_id, bound_max_generations)
        if start_ralph_handler is None:
            raise RuntimeError(
                "Automatic Ralph chaining is not configured: "
                "StartRalphHandler dependency is missing"
            )
        started = await start_ralph_handler.handle(
            {
                "lineage_id": lineage_id,
                "execute": True,
                "parallel": True,
                "skip_qa": False,
                "project_dir": arguments.get("working_dir"),
                "max_generations": bound_max_generations,
            }
        )
        if started.is_err:
            raise RuntimeError(started.error.message)
        job_id = started.value.meta.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("StartRalphHandler did not return a pollable Ralph job_id")
        return RalphSuccessorReceipt(job_id, bound_max_generations)

    request_key = hashlib.sha256(f"ralph-successor\0{lineage_id}".encode()).hexdigest()
    return await run_durable_lineage_single_flight(
        event_store,
        lineage_id,
        request_key,
        _start_once,
        generation_number=1,
        encode=lambda receipt: {
            "job_id": receipt.job_id,
            "max_generations": receipt.max_generations,
        },
        decode=_decode_ralph_job_receipt,
        scope="evaluation-ralph",
    )


async def enqueue_chained_ralph(
    evaluation_result: MCPToolResult,
    *,
    session_id: str,
    arguments: Mapping[str, Any],
    event_store: EventStore,
    job_manager: Any,
    start_ralph_handler: Any | None,
) -> MCPToolResult:
    """Project a rejection and start the configured pollable Ralph successor."""

    seed_content = arguments.get("seed_content")
    try:
        seed_data = yaml.safe_load(seed_content) if isinstance(seed_content, str) else None
        if not isinstance(seed_data, dict):
            raise ValueError("seed_content is unavailable or not a mapping")
        seed = Seed.from_dict(seed_data)
    except (ValueError, yaml.YAMLError, ValidationError, PydanticValidationError) as exc:
        return append_result_text(
            evaluation_result,
            "\nAutomatic Ralph: skipped because the original Seed could not be loaded.\n",
            meta={
                **evaluation_result.meta,
                "chained_ralph_skipped": "seed_unavailable",
                "chained_ralph_skip_detail": str(exc)[:1000],
            },
        )

    lineage_id = mint_chain_lineage_id(seed.metadata.seed_id, session_id)
    raw_max_generations = arguments.get("_auto_evolve_max_generations")
    max_generations = (
        raw_max_generations
        if isinstance(raw_max_generations, int)
        and not isinstance(raw_max_generations, bool)
        and 1 <= raw_max_generations <= 10
        else get_auto_evolve_max_generations()
    )
    try:
        await seed_gen1_lineage(
            event_store,
            lineage_id=lineage_id,
            seed=seed,
            summary=evaluation_summary_from_eval_meta(
                seed,
                evaluation_result.meta,
                execution_completion_status=arguments.get("_source_execution_status"),
            ),
        )
        ralph_receipt = await _claim_or_start_chained_ralph(
            lineage_id=lineage_id,
            arguments=arguments,
            max_generations=max_generations,
            event_store=event_store,
            job_manager=job_manager,
            start_ralph_handler=start_ralph_handler,
        )
    except Exception as exc:  # noqa: BLE001 - chaining must never flip rejection.
        return append_result_text(
            evaluation_result,
            "\nAutomatic Ralph: enqueue failed; the evaluation verdict is unchanged.\n",
            meta={
                **evaluation_result.meta,
                "chained_ralph_error": str(exc)[:1000],
                "chained_ralph_lineage_id": lineage_id,
            },
        )

    return append_result_text(
        evaluation_result,
        (
            "\nAutomatic Ralph: queued bounded convergence loop\n"
            f"Ralph Job ID: {ralph_receipt.job_id}\nLineage ID: {lineage_id}\n"
        ),
        meta={
            **evaluation_result.meta,
            "chained_ralph_job_id": ralph_receipt.job_id,
            "chained_ralph_lineage_id": lineage_id,
            "chained_ralph_max_generations": ralph_receipt.max_generations,
        },
    )


def append_result_text(
    result: MCPToolResult,
    text: str,
    *,
    meta: Mapping[str, Any],
) -> MCPToolResult:
    """Return a result with additive chain status while preserving the verdict."""

    return MCPToolResult(
        content=(*result.content, MCPContentItem(type=ContentType.TEXT, text=text)),
        is_error=result.is_error,
        meta=dict(meta),
        structured_content=result.structured_content,
    )
