"""WonderEngine - "What do we still not know?"

The Wonder phase is the philosophical heart of the evolutionary loop.
It examines the current ontology, evaluation results, and execution output
to identify gaps, tensions, and unanswered questions.

Inspired by Socrates' method: Wonder → "How should I live?" → "What IS 'live'?"
The WonderEngine asks: "Given what we learned, what do we still not know?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging

from pydantic import BaseModel, Field

from ouroboros.config import get_wonder_model
from ouroboros.core.errors import ProviderError
from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import OntologySchema, Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.regression import RegressionDetector
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)


class WonderOutput(BaseModel, frozen=True):
    """Output of the Wonder phase.

    v1: Simplified output with questions and tensions.
    v1.1 will add IgnoranceMap with categories and confidence scores.
    """

    questions: tuple[str, ...] = Field(default_factory=tuple)
    ontology_tensions: tuple[str, ...] = Field(default_factory=tuple)
    should_continue: bool = True
    reasoning: str = ""


@dataclass
class WonderEngine:
    """Generates wonder output for the next evolutionary generation.

    Takes the current ontology + evaluation results and produces questions
    about what we still don't know, plus tensions in the current ontology.

    Includes degraded mode: if the LLM call fails, falls back to generic
    questions derived from evaluation gaps rather than halting the loop.
    """

    llm_adapter: LLMAdapter
    model: str = field(default_factory=get_wonder_model)

    async def wonder(
        self,
        current_ontology: OntologySchema,
        evaluation_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
        seed: Seed | None = None,
    ) -> Result[WonderOutput, ProviderError]:
        """Generate wonder output for the next generation.

        Args:
            current_ontology: The current generation's ontology schema.
            evaluation_summary: Results from evaluating the current generation.
            execution_output: What was actually built/produced.
            lineage: Full lineage history for cross-generation context.
            seed: Original seed for scope-guarding ontology expansion.

        Returns:
            Result containing WonderOutput or ProviderError.
        """
        prompt = self._build_prompt(
            current_ontology, evaluation_summary, execution_output, lineage, seed
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=self._system_prompt()),
            Message(role=MessageRole.USER, content=prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.7,
            max_tokens=2048,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            logger.warning(
                "WonderEngine LLM call failed, using degraded mode: %s",
                result.error,
            )
            return Result.ok(self._degraded_output(evaluation_summary, current_ontology, seed))

        return Result.ok(self._parse_response(result.value.content, seed))

    def _system_prompt(self) -> str:
        return """You are the Wonder Engine of Ouroboros, an evolutionary development system.

Your role is to examine the current state of a project's ontology and its evaluation results,
then identify what we STILL DON'T KNOW. You practice Socratic questioning:
not just asking "what went wrong" but "what assumptions are we making?"

You must respond with a JSON object (no markdown, no code fences):
{
    "questions": ["question 1", "question 2", ...],
    "ontology_tensions": ["tension 1", "tension 2", ...],
    "should_continue": true/false,
    "reasoning": "explanation of your analysis"
}

Guidelines:
- questions: What gaps remain? What assumptions haven't been tested?
- ontology_tensions: Where does the current ontology CONTRADICT itself or the seed's goal?
- should_continue: Set to true if you generated ANY questions or tensions. Set to false ONLY if there are genuinely NO remaining questions within the seed's scope
- reasoning: Brief explanation of why these questions/tensions matter

SCOPE GUARD — this is critical:
- Only ask questions that are REQUIRED to satisfy the seed's goal and constraints.
- Do NOT propose ontology fields, concepts, or entities unrelated to the seed's goal and constraints.
- Concepts IMPLIED by the seed (not explicitly named but necessary to satisfy it) ARE allowed.
- An ontology is ALWAYS incomplete — that is normal, not a gap to fill.
- "This concept is not modeled" is NOT a valid tension unless the seed requires it (explicitly or implicitly).
- Prefer deepening existing fields over adding new ones.
- If the current ontology covers the seed's acceptance criteria AND evaluation shows no regressions or failures, set should_continue to false.

Focus on ONTOLOGICAL questions (what IS the thing?) not implementation questions (how to code it)."""

    def _build_prompt(
        self,
        ontology: OntologySchema,
        eval_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
        seed: Seed | None = None,
    ) -> str:
        parts: list[str] = []

        # Seed scope comes first — this is the boundary for all questions
        if seed:
            parts.append("## Seed Scope (boundary for ontology questions)")
            parts.append(f"Goal: {seed.goal}")
            if seed.constraints:
                parts.append("Constraints:")
                for c in seed.constraints:
                    parts.append(f"  - {c}")
            if seed.acceptance_criteria:
                parts.append(f"Acceptance Criteria: {len(seed.acceptance_criteria)}")
                for i, ac in enumerate(seed.acceptance_criteria, 1):
                    parts.append(f"  AC {i}: {ac}")
            parts.append("")

        parts.append(f"## Current Ontology: {ontology.name}")
        parts.append(f"Description: {ontology.description}")
        parts.append("Fields:")
        for f in ontology.fields:
            parts.append(f"  - {f.name} ({f.field_type}): {f.description}")

        if eval_summary:
            parts.append("\n## Evaluation Results")
            parts.append(f"  Approved: {eval_summary.final_approved}")
            parts.append(f"  Score: {eval_summary.score}")
            parts.append(f"  Drift: {eval_summary.drift_score}")
            if eval_summary.failure_reason:
                parts.append(f"  Failure: {eval_summary.failure_reason}")
            if eval_summary.feedback_metadata:
                parts.append("  Feedback Signals:")
                for feedback in eval_summary.feedback_metadata:
                    details: list[str] = []
                    max_depth = feedback.details.get("max_depth")
                    if isinstance(max_depth, int):
                        details.append(f"max_depth={max_depth}")
                    affected_count = feedback.details.get("affected_count")
                    if isinstance(affected_count, int):
                        details.append(f"affected_count={affected_count}")
                    detail_suffix = f" ({', '.join(details)})" if details else ""
                    parts.append(
                        f"    - [{feedback.severity.upper()}] {feedback.code}: "
                        f"{feedback.message}{detail_suffix}"
                    )
            if eval_summary.ac_results:
                failed_acs = [ac for ac in eval_summary.ac_results if not ac.passed]
                if failed_acs:
                    parts.append(f"\n  Failed ACs ({len(failed_acs)}):")
                    for ac in failed_acs:
                        parts.append(f"    - AC {ac.ac_index + 1}: {ac.ac_content}")
                passed_count = sum(1 for ac in eval_summary.ac_results if ac.passed)
                parts.append(f"  AC pass rate: {passed_count}/{len(eval_summary.ac_results)}")

        # Regression context
        if lineage and len(lineage.generations) >= 2:
            report = RegressionDetector().detect(lineage)
            if report.has_regressions:
                parts.append(f"\n## REGRESSIONS ({len(report.regressions)})")
                for reg in report.regressions:
                    parts.append(
                        f"  - AC {reg.ac_index + 1}: passed in Gen {reg.passed_in_generation}, "
                        f"failing since Gen {reg.failed_in_generation} "
                        f"({reg.consecutive_failures} consecutive): {reg.ac_text}"
                    )
                parts.append("  WHY did these previously-passing ACs start failing?")

        if execution_output:
            truncated = truncate_head_tail(execution_output)
            parts.append(f"\n## Execution Output (truncated)\n{truncated}")

        if lineage.generations:
            parts.append(f"\n## Evolution History ({len(lineage.generations)} generations)")
            for gen in lineage.generations[-3:]:  # Last 3 for context
                parts.append(
                    f"  Gen {gen.generation_number}: {gen.ontology_snapshot.name} "
                    f"({len(gen.ontology_snapshot.fields)} fields)"
                )
                if gen.wonder_questions:
                    parts.append(f"    Wonder: {gen.wonder_questions[:2]}")

        parts.append("\n## Your Task")
        parts.append(
            "Within the seed's goal and constraints, identify what we still don't know. "
            "What assumptions are hidden? Where does the ontology contradict the seed? "
            "Do NOT propose concepts beyond the seed's scope — incompleteness is normal."
        )

        return "\n".join(parts)

    def _parse_response(self, content: str, seed: Seed | None = None) -> WonderOutput:
        """Parse LLM response into WonderOutput."""
        try:
            # Strip markdown fences if present
            cleaned = content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])

            data = json.loads(cleaned)
            return WonderOutput(
                questions=tuple(data.get("questions", [])),
                ontology_tensions=tuple(data.get("ontology_tensions", [])),
                should_continue=data.get("should_continue", True),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse WonderEngine response: %s", e)
            scope_hint = f" for goal: {seed.goal}" if seed else ""
            return WonderOutput(
                questions=(f"What assumptions remain untested{scope_hint}?",),
                ontology_tensions=(),
                should_continue=True,
                reasoning=f"Parse error, using seed-scoped fallback: {e}",
            )

    def _degraded_output(
        self,
        eval_summary: EvaluationSummary | None,
        ontology: OntologySchema,
        seed: Seed | None = None,
    ) -> WonderOutput:
        """Generate fallback output when LLM fails (degraded mode)."""
        questions: list[str] = []
        tensions: list[str] = []
        scope_hint = f" (within scope: {seed.goal})" if seed else ""

        if eval_summary:
            if not eval_summary.final_approved:
                questions.append(f"What requirement is the current ontology missing{scope_hint}?")
            if eval_summary.drift_score and eval_summary.drift_score > 0.3:
                questions.append("Why has the implementation drifted from the original intent?")
                tensions.append("The ontology describes one thing but execution produces another")
            if eval_summary.failure_reason:
                questions.append(f"What ontological gap caused: {eval_summary.failure_reason}?")
        else:
            questions.append(
                f"Does the current ontology cover the seed's acceptance criteria{scope_hint}?"
            )

        if len(ontology.fields) < 3 and seed:
            questions.append(
                f"Are there concepts implied by the seed goal that are not yet modeled{scope_hint}?"
            )

        # If evaluation passed and no questions were generated, allow convergence
        should_continue = bool(questions)
        if eval_summary and not eval_summary.final_approved:
            should_continue = True

        return WonderOutput(
            questions=tuple(questions),
            ontology_tensions=tuple(tensions),
            should_continue=should_continue,
            reasoning="Degraded mode: LLM unavailable, using heuristic questions"
            if should_continue
            else "Degraded mode: evaluation passed, no in-scope gaps remain",
        )
