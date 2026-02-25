"""ReflectEngine - the core of ontological evolution.

The Reflect phase examines execution results + current ontology + wonder output
and produces refined ACs + ontology mutations for the next Seed.

This is where the Ouroboros eats its tail: the output of evaluation becomes
the input for the next generation's seed specification.

Replaces the "contextual interview" approach for Gen 2+. Interview is Gen 1 only;
Reflect handles all subsequent generations autonomously.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from pydantic import BaseModel, Field

from ouroboros.core.errors import ProviderError
from ouroboros.core.lineage import EvaluationSummary, MutationAction, OntologyLineage
from ouroboros.core.seed import Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)

_FALLBACK_MODEL = "claude-sonnet-4-20250514"


class OntologyMutation(BaseModel, frozen=True):
    """A specific proposed change to the ontology schema."""

    action: MutationAction
    field_name: str
    field_type: str | None = None
    description: str | None = None
    reason: str = ""


class ReflectOutput(BaseModel, frozen=True):
    """Output of the Reflect phase -- feeds directly into SeedGenerator.

    Contains everything needed to create the next generation's Seed:
    refined goal, constraints, acceptance criteria, and ontology mutations.
    """

    refined_goal: str
    refined_constraints: tuple[str, ...] = Field(default_factory=tuple)
    refined_acs: tuple[str, ...] = Field(default_factory=tuple)
    ontology_mutations: tuple[OntologyMutation, ...] = Field(default_factory=tuple)
    reasoning: str = ""


@dataclass
class ReflectEngine:
    """Reflects on execution results and proposes ontological evolution.

    This is where the Ouroboros eats its tail:
    - Examines what was built vs what was intended
    - Identifies ontology gaps exposed by execution
    - Proposes refined ACs that address wonder questions
    - Mutates ontology based on learned knowledge

    When evaluation is fully approved (score >= 0.8, no drift), outputs
    minimal changes to allow convergence.
    """

    llm_adapter: LLMAdapter
    model: str = _FALLBACK_MODEL

    async def reflect(
        self,
        current_seed: Seed,
        execution_output: str,
        evaluation_summary: EvaluationSummary,
        wonder_output: WonderOutput,
        lineage: OntologyLineage,
    ) -> Result[ReflectOutput, ProviderError]:
        """Reflect on execution results and propose evolution.

        Args:
            current_seed: The seed that was executed.
            execution_output: What was actually produced.
            evaluation_summary: How the execution was evaluated.
            wonder_output: What we still don't know (from WonderEngine).
            lineage: Full lineage for cross-generation context.

        Returns:
            Result containing ReflectOutput or ProviderError.
        """
        prompt = self._build_prompt(
            current_seed,
            execution_output,
            evaluation_summary,
            wonder_output,
            lineage,
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=self._system_prompt()),
            Message(role=MessageRole.USER, content=prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.5,
            max_tokens=3000,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            logger.error("ReflectEngine LLM call failed: %s", result.error)
            return Result.err(result.error)

        return Result.ok(self._parse_response(result.value.content, current_seed))

    def _system_prompt(self) -> str:
        return """You are the Reflect Engine of Ouroboros, an evolutionary development system.

Your role is to examine what was built, how it was evaluated, and what we still don't know,
then propose SPECIFIC changes to the ontology and acceptance criteria for the next generation.

You practice ontological thinking: not just "what went wrong" but "what IS the thing we're building,
and how should our understanding of it evolve?"

You must respond with a JSON object (no markdown, no code fences):
{
    "refined_goal": "the goal, possibly refined based on what we learned",
    "refined_constraints": ["constraint 1", "constraint 2", ...],
    "refined_acs": ["acceptance criterion 1", "criterion 2", ...],
    "ontology_mutations": [
        {"action": "add|modify|remove", "field_name": "name", "field_type": "type", "description": "desc", "reason": "why"},
        ...
    ],
    "reasoning": "explanation of why these changes are needed"
}

Guidelines:
- If evaluation was mostly successful (score >= 0.8, approved), propose MINIMAL changes
- Each mutation must have a clear reason tied to evaluation findings or wonder questions
- refined_acs should address the wonder questions and ontology tensions
- Do NOT change things that are working well -- only evolve what needs evolution
- action must be exactly one of: "add", "modify", "remove"
"""

    def _build_prompt(
        self,
        seed: Seed,
        execution_output: str,
        eval_summary: EvaluationSummary,
        wonder: WonderOutput,
        lineage: OntologyLineage,
    ) -> str:
        parts = ["## Current Seed"]
        parts.append(f"Goal: {seed.goal}")
        parts.append(f"Constraints: {list(seed.constraints)}")
        parts.append(f"Acceptance Criteria: {list(seed.acceptance_criteria)}")

        parts.append(f"\n## Ontology: {seed.ontology_schema.name}")
        parts.append(f"Description: {seed.ontology_schema.description}")
        for f in seed.ontology_schema.fields:
            parts.append(f"  - {f.name} ({f.field_type}): {f.description}")

        parts.append("\n## Evaluation Results")
        parts.append(f"  Approved: {eval_summary.final_approved}")
        parts.append(f"  Score: {eval_summary.score}")
        parts.append(f"  Drift: {eval_summary.drift_score}")
        if eval_summary.failure_reason:
            parts.append(f"  Failure: {eval_summary.failure_reason}")

        parts.append("\n## Wonder Questions (what we still don't know)")
        for q in wonder.questions:
            parts.append(f"  - {q}")

        if wonder.ontology_tensions:
            parts.append("\n## Ontology Tensions")
            for t in wonder.ontology_tensions:
                parts.append(f"  - {t}")

        truncated = truncate_head_tail(execution_output)
        parts.append(f"\n## Execution Output (truncated)\n{truncated}")

        if len(lineage.generations) > 1:
            parts.append(f"\n## Evolution History ({len(lineage.generations)} generations)")
            for gen in lineage.generations[-3:]:
                parts.append(
                    f"  Gen {gen.generation_number}: "
                    f"{len(gen.ontology_snapshot.fields)} fields, "
                    f"approved={gen.evaluation_summary.final_approved if gen.evaluation_summary else 'N/A'}"
                )

        parts.append("\n## Your Task")
        parts.append(
            "Based on the evaluation results and wonder questions, propose specific "
            "changes to the goal, constraints, acceptance criteria, and ontology "
            "for the next generation. Be precise and actionable."
        )

        return "\n".join(parts)

    def _parse_response(self, content: str, current_seed: Seed) -> ReflectOutput:
        """Parse LLM response into ReflectOutput."""
        try:
            cleaned = content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])

            data = json.loads(cleaned)

            mutations: list[OntologyMutation] = []
            for m in data.get("ontology_mutations", []):
                try:
                    action = MutationAction(m.get("action", "modify"))
                except ValueError:
                    action = MutationAction.MODIFY
                mutations.append(
                    OntologyMutation(
                        action=action,
                        field_name=m.get("field_name", "unknown"),
                        field_type=m.get("field_type"),
                        description=m.get("description"),
                        reason=m.get("reason", ""),
                    )
                )

            return ReflectOutput(
                refined_goal=data.get("refined_goal", current_seed.goal),
                refined_constraints=tuple(
                    data.get("refined_constraints", list(current_seed.constraints))
                ),
                refined_acs=tuple(data.get("refined_acs", list(current_seed.acceptance_criteria))),
                ontology_mutations=tuple(mutations),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse ReflectEngine response: %s", e)
            # Return current seed values with no mutations (safe fallback)
            return ReflectOutput(
                refined_goal=current_seed.goal,
                refined_constraints=current_seed.constraints,
                refined_acs=current_seed.acceptance_criteria,
                ontology_mutations=(),
                reasoning=f"Parse error, keeping current: {e}",
            )
