"""WonderEngine - "What do we still not know?"

The Wonder phase is the philosophical heart of the evolutionary loop.
It examines the current ontology, evaluation results, and execution output
to identify gaps, tensions, and unanswered questions.

Inspired by Socrates' method: Wonder → "How should I live?" → "What IS 'live'?"
The WonderEngine asks: "Given what we learned, what do we still not know?"
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from pydantic import BaseModel, Field

from ouroboros.core.errors import ProviderError
from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import OntologySchema
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)

_FALLBACK_MODEL = "claude-sonnet-4-20250514"


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
    model: str = _FALLBACK_MODEL

    async def wonder(
        self,
        current_ontology: OntologySchema,
        evaluation_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
    ) -> Result[WonderOutput, ProviderError]:
        """Generate wonder output for the next generation.

        Args:
            current_ontology: The current generation's ontology schema.
            evaluation_summary: Results from evaluating the current generation.
            execution_output: What was actually built/produced.
            lineage: Full lineage history for cross-generation context.

        Returns:
            Result containing WonderOutput or ProviderError.
        """
        prompt = self._build_prompt(current_ontology, evaluation_summary, execution_output, lineage)

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
            return Result.ok(self._degraded_output(evaluation_summary, current_ontology))

        return Result.ok(self._parse_response(result.value.content))

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
- ontology_tensions: Where does the current ontology contradict itself or miss something?
- should_continue: false ONLY if the ontology is complete and evaluation is fully satisfied
- reasoning: Brief explanation of why these questions/tensions matter

Focus on ONTOLOGICAL questions (what IS the thing?) not implementation questions (how to code it)."""

    def _build_prompt(
        self,
        ontology: OntologySchema,
        eval_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
    ) -> str:
        parts = [f"## Current Ontology: {ontology.name}"]
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

        if execution_output:
            # Truncate to avoid token explosion
            truncated = execution_output[:2000]
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
            "Identify what we still don't know about this domain. "
            "What ontological gaps exist? What assumptions are hidden?"
        )

        return "\n".join(parts)

    def _parse_response(self, content: str) -> WonderOutput:
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
            return WonderOutput(
                questions=("What aspects of this domain are we not modeling?",),
                ontology_tensions=(),
                should_continue=True,
                reasoning=f"Parse error, using fallback: {e}",
            )

    def _degraded_output(
        self,
        eval_summary: EvaluationSummary | None,
        ontology: OntologySchema,
    ) -> WonderOutput:
        """Generate fallback output when LLM fails (degraded mode)."""
        questions: list[str] = []
        tensions: list[str] = []

        if eval_summary:
            if not eval_summary.final_approved:
                questions.append("What fundamental requirement is the current ontology missing?")
            if eval_summary.drift_score and eval_summary.drift_score > 0.3:
                questions.append("Why has the implementation drifted from the original intent?")
                tensions.append("The ontology describes one thing but execution produces another")
            if eval_summary.failure_reason:
                questions.append(f"What ontological gap caused: {eval_summary.failure_reason}?")
        else:
            questions.append("Is the current ontology complete enough to define this domain?")

        if len(ontology.fields) < 3:
            questions.append("Are there missing entities or relationships in this ontology?")

        return WonderOutput(
            questions=tuple(questions)
            if questions
            else ("What are we assuming about this domain?",),
            ontology_tensions=tuple(tensions),
            should_continue=True,
            reasoning="Degraded mode: LLM unavailable, using heuristic questions",
        )
