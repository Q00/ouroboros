"""Bounded durable references for cross-process evolve waiters."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import EvaluationSummary, GenerationPhase, OntologyDelta
from ouroboros.core.seed import Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.loop import GenerationResult, StepAction, StepResult
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ReflectOutput
from ouroboros.evolution.wonder import WonderOutput, ground_questions
from ouroboros.persistence.event_store import EventStore

MAX_RECEIPT_TEXT = 2_000
MAX_VALIDATION_TEXT = 5_000


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return truncate_head_tail(value, head=limit // 2, tail=limit // 2)


def encode_step_result(result: Result[StepResult, OuroborosError]) -> dict[str, Any]:
    """Persist only bounded decision metadata and durable event references."""
    if result.is_err:
        return {"ok": False, "error": _bounded(str(result.error), MAX_RECEIPT_TEXT)}
    step = result.value
    generation = step.generation_result
    signal = step.convergence_signal
    return {
        "ok": True,
        "lineage_id": step.lineage.lineage_id,
        "generation_number": generation.generation_number,
        "generation_phase": generation.phase.value,
        "generation_success": generation.success,
        "action": step.action.value,
        "next_generation": step.next_generation,
        "wonder_should_continue": (
            generation.wonder_output.should_continue
            if generation.wonder_output is not None
            else None
        ),
        "validation_output": _bounded(generation.validation_output, MAX_VALIDATION_TEXT),
        "ontology_delta": (
            generation.ontology_delta.model_dump(mode="json")
            if generation.ontology_delta is not None
            else None
        ),
        "signal": {
            "converged": signal.converged,
            "reason": _bounded(signal.reason, MAX_RECEIPT_TEXT),
            "ontology_similarity": signal.ontology_similarity,
            "generation": signal.generation,
            "failed_acs": list(signal.failed_acs),
            "should_stop": signal.should_stop,
            "ontology_stable": signal.ontology_stable,
        },
    }


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Durable evolve receipt {field} must be an object")
    return value


async def decode_step_result(
    event_store: EventStore,
    payload: Mapping[str, Any],
) -> Result[StepResult, OuroborosError]:
    """Rebuild the winner from its durable generation events and bounded receipt."""
    if not bool(payload.get("ok")):
        return Result.err(OuroborosError(str(payload.get("error") or "evolve_step failed")))
    lineage_id = str(payload["lineage_id"])
    generation_number = int(payload["generation_number"])
    events = await event_store.replay_lineage(lineage_id)
    lineage = LineageProjector().project(events)
    if lineage is None:
        raise ValueError("Durable evolve receipt lineage cannot be projected")
    record = next(
        (
            generation
            for generation in reversed(lineage.generations)
            if generation.generation_number == generation_number
        ),
        None,
    )
    if record is None or not record.seed_json:
        raise ValueError("Durable evolve receipt generation has no structured Seed")
    seed = Seed.from_dict(json.loads(record.seed_json))
    partial_state = record.partial_state if isinstance(record.partial_state, Mapping) else {}
    partial_questions = partial_state.get("wonder_questions")
    wonder_questions = (
        tuple(str(question) for question in partial_questions)
        if isinstance(partial_questions, (list, tuple))
        else record.wonder_questions
    )
    should_continue = payload.get("wonder_should_continue")
    wonder = (
        WonderOutput(
            questions=wonder_questions,
            grounded_questions=ground_questions(wonder_questions, len(seed.acceptance_criteria)),
            should_continue=bool(should_continue),
            reasoning="replayed from durable generation receipt",
        )
        if wonder_questions or should_continue is not None
        else None
    )
    reflect_data = partial_state.get("reflect_output")
    reflect = (
        ReflectOutput.model_validate(reflect_data) if isinstance(reflect_data, Mapping) else None
    )
    if reflect is not None and reflect.ac_patches:
        # Explicit parser provenance is process-local by design. A durable
        # waiter must not turn serialized patches back into identity authority.
        reflect = None
    execution_output = partial_state.get("execution_output", record.execution_output)
    evaluation_data = partial_state.get("evaluation_summary")
    evaluation_summary = (
        EvaluationSummary.model_validate(evaluation_data)
        if isinstance(evaluation_data, Mapping)
        else record.evaluation_summary
    )
    delta_data = payload.get("ontology_delta")
    signal_data = _mapping(payload.get("signal"), "signal")
    generation = GenerationResult(
        generation_number=generation_number,
        seed=seed,
        execution_output=(execution_output if isinstance(execution_output, str) else None),
        evaluation_summary=evaluation_summary,
        wonder_output=wonder,
        reflect_output=reflect,
        ontology_delta=(
            OntologyDelta.model_validate(delta_data) if delta_data is not None else None
        ),
        validation_output=payload.get("validation_output"),
        active_ac_indices=record.active_ac_indices,
        frozen_ac_indices=record.frozen_ac_indices,
        phase=GenerationPhase(str(payload["generation_phase"])),
        success=bool(payload["generation_success"]),
    )
    signal = ConvergenceSignal(
        converged=bool(signal_data["converged"]),
        reason=str(signal_data["reason"]),
        ontology_similarity=float(signal_data["ontology_similarity"]),
        generation=int(signal_data["generation"]),
        failed_acs=tuple(int(value) for value in signal_data["failed_acs"]),
        should_stop=bool(signal_data["should_stop"]),
        ontology_stable=bool(signal_data["ontology_stable"]),
    )
    return Result.ok(
        StepResult(
            generation_result=generation,
            convergence_signal=signal,
            lineage=lineage,
            action=StepAction(str(payload["action"])),
            next_generation=int(payload["next_generation"]),
        )
    )
