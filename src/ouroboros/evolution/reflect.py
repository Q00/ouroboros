"""ReflectEngine - the core of ontological evolution.

The Reflect phase examines execution results + current ontology + wonder output
and produces refined ACs + ontology mutations for the next Seed.

This is where the Ouroboros eats its tail: the output of evaluation becomes
the input for the next generation's seed specification.

Replaces the "contextual interview" approach for Gen 2+. Interview is Gen 1 only;
Reflect handles all subsequent generations autonomously.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging

from pydantic import BaseModel, Field

from ouroboros.config import get_llm_backend, get_reflect_model
from ouroboros.core.errors import ProviderError
from ouroboros.core.lineage import EvaluationSummary, MutationAction, OntologyDelta, OntologyLineage
from ouroboros.core.seed import Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.regression import RegressionDetector
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)


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

    Adapter freshness:
        ``llm_adapter`` is captured at MCP server startup. If the user
        changes ``llm.backend`` in ``~/.ouroboros/config.yaml`` after the
        server has started, the captured adapter is stale and every Reflect
        call still hits the previous backend's adapter (issue #562). The
        ``adapter_factory`` field lets callers supply a zero-arg factory
        the engine invokes per call so Reflect always honors the live
        config; if no factory is supplied the engine falls back to the
        captured adapter (preserving today's behavior for tests and direct
        consumers).
    """

    llm_adapter: LLMAdapter
    model: str | None = None
    adapter_factory: Callable[[], LLMAdapter | None] | None = field(default=None)
    adapter_backend: str | None = None
    _captured_backend: str | None = field(default=None, init=False, repr=False)
    _model_is_explicit: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Track explicit model pins while allowing backend-aware implicit defaults."""
        self._model_is_explicit = self.model is not None
        try:
            self._captured_backend = self.adapter_backend or get_llm_backend()
        except Exception:  # noqa: BLE001 — never fail engine init on config read
            self._captured_backend = None
        if self.model is None:
            self._refresh_model(self._captured_backend)

    def _refresh_model(self, backend: str | None) -> None:
        if not self._model_is_explicit:
            self.model = get_reflect_model(backend)

    def _completion_model(self) -> str:
        if self.model is None:
            self._refresh_model(self._selected_backend())
        assert self.model is not None
        return self.model

    def _resolve_adapter(self) -> LLMAdapter:
        """Return the adapter the next ``complete()`` call should use."""
        current_backend = self._selected_backend()
        backend_drifted = (
            self._captured_backend is not None
            and current_backend
            and current_backend != self._captured_backend
        )

        if self.adapter_factory is not None:
            try:
                fresh = self.adapter_factory()
                if fresh is not None:
                    # Treat the factory result as the latest known-good adapter so
                    # a later transient factory failure does not fall back to a
                    # stale startup adapter after backend/model state has moved.
                    self.llm_adapter = fresh
                    if current_backend:
                        self._captured_backend = current_backend
                        self._refresh_model(current_backend)
                    return fresh
            except Exception:  # noqa: BLE001 — fall through to captured adapter
                logger.exception("ReflectEngine adapter_factory raised; using captured adapter")
                return self.llm_adapter

        if backend_drifted:
            try:
                from ouroboros.providers.factory import create_llm_adapter

                rebuilt = create_llm_adapter(
                    backend=current_backend,
                    **_adapter_rebuild_kwargs(self.llm_adapter),
                )
                self.llm_adapter = rebuilt
                self._captured_backend = current_backend
                self._refresh_model(current_backend)
                logger.info(
                    "reflect.adapter_rebuilt_for_backend_drift",
                    extra={"new_backend": current_backend},
                )
                return rebuilt
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ReflectEngine failed to rebuild adapter for drifted backend; "
                    "falling back to captured adapter"
                )
                return self.llm_adapter

        return self.llm_adapter

    def _selected_backend(self) -> str | None:
        if self.adapter_backend is not None:
            return self.adapter_backend
        try:
            return get_llm_backend()
        except Exception:  # noqa: BLE001
            return None

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

        adapter = self._resolve_adapter()
        config = CompletionConfig(
            model=self._completion_model(),
            role="reflect",
            model_is_explicit=self._model_is_explicit,
            temperature=0.5,
            max_tokens=3000,
        )

        result = await adapter.complete(messages, config)

        if result.is_err:
            logger.error("ReflectEngine LLM call failed: %s", result.error)
            return Result.err(result.error)

        raw_content = result.value.content
        logger.info(
            "reflect.raw_response",
            extra={
                "content_length": len(raw_content),
                "content_preview": raw_content[:500],
            },
        )

        parsed = self._parse_response(raw_content, current_seed)
        if parsed is None:
            return Result.err(
                ProviderError(
                    message="Reflect failed to parse LLM response",
                    provider="reflect",
                )
            )
        return Result.ok(parsed)

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
- If Wonder questions exist, you MUST propose at least one ontology_mutation that addresses them
- If evaluation score >= 0.8 and approved, keep changes focused but still evolve the ontology based on Wonder insights
- If evaluation score < 0.8 or not approved, propose more aggressive mutations to address failures
- Each mutation must have a clear reason tied to evaluation findings or wonder questions
- refined_acs should address the wonder questions and ontology tensions
- Do NOT change things that are working well -- only evolve what needs evolution
- action must be exactly one of: "add", "modify", "remove"
- An empty ontology_mutations list is ONLY acceptable when there are no Wonder questions
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
            parts.append("\n  Per-AC Breakdown:")
            for ac in eval_summary.ac_results:
                status = "PASS" if ac.passed else "FAIL"
                parts.append(f"    AC {ac.ac_index + 1} [{status}]: {ac.ac_content}")
            failed_acs = [ac for ac in eval_summary.ac_results if not ac.passed]
            if failed_acs:
                parts.append(
                    f"\n  PRIORITY: Fix {len(failed_acs)} failing AC(s) while preserving passing ones."
                )

        # Regression context
        if lineage and len(lineage.generations) >= 2:
            report = RegressionDetector().detect(lineage)
            if report.has_regressions:
                parts.append(f"\n## REGRESSIONS ({len(report.regressions)})")
                for reg in report.regressions:
                    parts.append(
                        f"  - AC {reg.ac_index + 1} (Gen {reg.passed_in_generation}→Gen {reg.failed_in_generation}): "
                        f"{reg.ac_text}"
                    )
                parts.append(
                    "  CRITICAL: These ACs previously passed. Preserve their behavior while fixing other issues."
                )

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

            # Stagnation warning: detect consecutive identical ontologies
            stagnant_count = 0
            gens = lineage.generations
            for i in range(len(gens) - 1, 0, -1):
                if (
                    OntologyDelta.compute(
                        gens[i - 1].ontology_snapshot, gens[i].ontology_snapshot
                    ).similarity
                    >= 0.99
                ):
                    stagnant_count += 1
                else:
                    break
            if stagnant_count >= 1:
                parts.append(
                    f"\n## WARNING: STAGNATION DETECTED"
                    f"\n  The ontology has NOT changed for {stagnant_count} consecutive generation(s)."
                    f"\n  Previous Reflect phases produced ZERO effective mutations."
                    f"\n  You MUST propose concrete ontology mutations based on the Wonder questions above."
                    f"\n  Translate each Wonder question into at least one add/modify/remove mutation."
                )

        parts.append("\n## Your Task")
        parts.append(
            "Based on the evaluation results and wonder questions, propose specific "
            "changes to the goal, constraints, acceptance criteria, and ontology "
            "for the next generation. Be precise and actionable."
        )

        return "\n".join(parts)

    def _parse_response(self, content: str, current_seed: Seed) -> ReflectOutput | None:
        """Parse LLM response into ReflectOutput.

        Returns None on parse failure so the caller can retry or propagate error.
        """
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
            logger.warning(
                "reflect.parse_failed",
                extra={
                    "error": str(e),
                    "raw_content": content[:1000],
                },
            )
            return None


def _adapter_rebuild_kwargs(adapter: LLMAdapter) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "cwd": _adapter_cwd(adapter),
        "max_turns": _adapter_max_turns(adapter),
    }
    for key, attr in (
        ("permission_mode", "_permission_mode"),
        ("allowed_tools", "_allowed_tools"),
        ("cli_path", "_cli_path"),
        ("timeout", "_timeout"),
        ("max_retries", "_max_retries"),
        ("on_message", "_on_message"),
        ("api_key", "_api_key"),
        ("api_base", "_api_base"),
    ):
        if hasattr(adapter, attr):
            value = getattr(adapter, attr)
            if value is not None:
                kwargs[key] = value
    return kwargs


def _adapter_cwd(adapter: LLMAdapter) -> str | None:
    value = getattr(adapter, "_cwd", None)
    return str(value) if value is not None else None


def _adapter_max_turns(adapter: LLMAdapter) -> int:
    value = getattr(adapter, "_max_turns", 1)
    return value if isinstance(value, int) and value > 0 else 1
