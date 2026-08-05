"""WonderEngine - "What do we still not know?"

The Wonder phase is the philosophical heart of the evolutionary loop.
It examines the current ontology, evaluation results, and execution output
to identify gaps, tensions, and unanswered questions.

Inspired by Socrates' method: Wonder → "How should I live?" → "What IS 'live'?"
The WonderEngine asks: "Given what we learned, what do we still not know?"
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import json
import logging
import math
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import OntologySchema, Seed, ac_texts
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.provider_usage import tracked_complete
from ouroboros.evolution.regression import RegressionDetector
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)


def get_wonder_model(backend: str | None = None) -> str:
    """Compatibility wrapper for Reflect-stage Wonder model resolution."""
    return get_llm_model_for_role("wonder", backend=backend)


# Deterministic fallback for grounding free-floating question strings to ACs.
# Matches "AC 2", "AC#2", "ac 3" — the 1-based AC number the question challenges.
_AC_REF_PATTERN = re.compile(r"\bAC\s*#?(\d+)\b", re.IGNORECASE)


class GroundedQuestion(BaseModel, frozen=True):
    """A Wonder question tied to what it challenges.

    A grounded elenchus refutes a *specific* claim: either it challenges named
    acceptance criteria (``kind="challenge"`` with 0-based ``ac_indices``) or it
    names a gap the goal requires but no AC covers (``kind="gap"``).
    """

    question: str
    kind: Literal["challenge", "gap"] = "gap"
    ac_indices: tuple[int, ...] = ()


def _ground_ac_indices(refs: Iterable[object], total_acs: int | None) -> tuple[int, ...]:
    """Convert 1-based AC refs to deduped, in-range 0-based indices."""
    seen: list[int] = []
    for ref in refs:
        one_based = _coerce_finite_integer_ref(ref)
        if one_based is None:
            continue
        idx = one_based - 1
        if idx < 0:
            continue
        if total_acs is not None and idx >= total_acs:
            continue
        if idx not in seen:
            seen.append(idx)
    return tuple(seen)


def _coerce_finite_integer_ref(ref: object) -> int | None:
    """Return a finite integer AC ref, rejecting bools and non-finite numerics."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref
    if isinstance(ref, float):
        if math.isfinite(ref) and ref.is_integer():
            return int(ref)
        return None
    if isinstance(ref, str):
        try:
            value = float(ref)
        except ValueError:
            return None
        if math.isfinite(value) and value.is_integer():
            return int(value)
    return None


def ground_question_text(text: str, total_acs: int | None) -> GroundedQuestion:
    """Ground a plain question string via the deterministic AC-ref regex."""
    indices = _ground_ac_indices(_AC_REF_PATTERN.findall(text), total_acs)
    if indices:
        return GroundedQuestion(question=text, kind="challenge", ac_indices=indices)
    return GroundedQuestion(question=text, kind="gap")


def ground_questions(
    questions: Iterable[str], total_acs: int | None
) -> tuple[GroundedQuestion, ...]:
    """Ground a sequence of plain question strings (legacy/partial-state path)."""
    return tuple(ground_question_text(q, total_acs) for q in questions)


class WonderOutput(BaseModel, frozen=True):
    """Output of the Wonder phase.

    v1: Simplified output with questions and tensions.
    v1.1 will add IgnoranceMap with categories and confidence scores.
    """

    questions: tuple[str, ...] = Field(default_factory=tuple)
    grounded_questions: tuple[GroundedQuestion, ...] = Field(default_factory=tuple)
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

    frugality_provider_tracking: ClassVar[bool] = True

    llm_adapter: LLMAdapter
    model: str | None = None
    adapter_factory: Callable[[], LLMAdapter | None] | None = field(default=None)
    adapter_backend: str | None = None
    adapter_backend_factory: Callable[[], str | None] | None = field(default=None, repr=False)
    _captured_backend: str | None = field(default=None, init=False, repr=False)
    _model_is_explicit: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Track explicit model pins while allowing backend-aware implicit defaults."""
        self._model_is_explicit = self.model is not None
        try:
            self._captured_backend = self.adapter_backend or get_llm_backend_for_role("wonder")
        except Exception:  # noqa: BLE001
            self._captured_backend = None
        if self.model is None:
            self._refresh_model(self._captured_backend)

    def _refresh_model(self, backend: str | None) -> None:
        if not self._model_is_explicit:
            self.model = get_wonder_model(backend)

    def _completion_model(self) -> str:
        if self.model is None:
            self._refresh_model(self._selected_backend())
        assert self.model is not None
        return self.model

    def _resolve_adapter(self) -> LLMAdapter:
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
            except Exception:  # noqa: BLE001
                logger.exception("WonderEngine adapter_factory raised; using captured adapter")
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
                    "wonder.adapter_rebuilt_for_backend_drift",
                    extra={"new_backend": current_backend},
                )
                return rebuilt
            except Exception:  # noqa: BLE001
                logger.exception(
                    "WonderEngine failed to rebuild adapter for drifted backend; "
                    "falling back to captured adapter"
                )
                return self.llm_adapter

        return self.llm_adapter

    def _selected_backend(self) -> str | None:
        if self.adapter_backend_factory is not None:
            try:
                backend = self.adapter_backend_factory()
                if backend:
                    return backend
            except Exception:  # noqa: BLE001
                logger.exception("WonderEngine adapter_backend_factory raised")
        if self.adapter_backend is not None:
            return self.adapter_backend
        try:
            return get_llm_backend_for_role("wonder")
        except Exception:  # noqa: BLE001
            return None

    async def wonder(
        self,
        current_ontology: OntologySchema,
        evaluation_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
        seed: Seed | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
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
            current_ontology,
            evaluation_summary,
            execution_output,
            lineage,
            seed,
            active_ac_indices,
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=self._system_prompt()),
            Message(role=MessageRole.USER, content=prompt),
        ]

        adapter = self._resolve_adapter()
        config = CompletionConfig(
            model=self._completion_model(),
            role="wonder",
            model_is_explicit=self._model_is_explicit,
            temperature=0.7,
            max_tokens=2048,
        )

        result = await tracked_complete(adapter, messages, config)

        if result.is_err:
            logger.warning(
                "WonderEngine LLM call failed, using degraded mode: %s",
                result.error,
            )
            return Result.ok(
                self._degraded_output(
                    evaluation_summary,
                    current_ontology,
                    seed,
                    active_ac_indices,
                )
            )

        return Result.ok(self._parse_response(result.value.content, seed, active_ac_indices))

    def _system_prompt(self) -> str:
        return """You are the Wonder Engine of Ouroboros, an evolutionary development system.

Your role is to examine the current state of a project's ontology and its evaluation results,
then identify what we STILL DON'T KNOW. You practice Socratic questioning:
not just asking "what went wrong" but "what assumptions are we making?"

You must respond with a JSON object (no markdown, no code fences):
{
    "questions": [
        {"question": "Why does the OAuth flow assume a single provider?", "ac_refs": [2]},
        {"question": "What handles token refresh — no AC covers it?", "kind": "gap"}
    ],
    "ontology_tensions": ["tension 1", "tension 2", ...],
    "should_continue": true/false,
    "reasoning": "explanation of your analysis"
}

GROUNDED ELENCHUS — every question must name what it challenges:
- A question that challenges existing acceptance criteria MUST cite them with
  "ac_refs": [<1-based AC number(s)>]. A challenge is the only licensed way to
  reopen a settled (passing) claim, so only challenge a PASSING AC when you have
  concrete evidence from the evaluation or execution output that it is wrong.
- A question about something the goal requires but NO AC covers MUST declare
  "kind": "gap" (no ac_refs). Gaps may become new ACs downstream.
- A question that is neither grounded in an AC nor a real gap is not a question —
  it is a mood. Do not emit it.

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
- When an "Evolution Focus" is present, ONLY its active AC nodes may be
  questioned or changed. Do not reopen frozen ACs, emit unscoped gap questions,
  or add a new AC. The working set must shrink toward zero.
- If the current ontology covers the seed's acceptance criteria AND evaluation shows no regressions or failures, set should_continue to false.

Focus on ONTOLOGICAL questions (what IS the thing?) not implementation questions (how to code it)."""

    def _build_prompt(
        self,
        ontology: OntologySchema,
        eval_summary: EvaluationSummary | None,
        execution_output: str | None,
        lineage: OntologyLineage,
        seed: Seed | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> str:
        parts: list[str] = []
        focused = active_ac_indices is not None
        active = set(active_ac_indices or ())

        # Seed scope comes first — this is the boundary for all questions
        if seed:
            parts.append("## Seed Scope (boundary for ontology questions)")
            parts.append(f"Goal: {seed.goal}")
            if seed.constraints:
                parts.append("Constraints:")
                for c in seed.constraints:
                    parts.append(f"  - {c}")
            if seed.acceptance_criteria:
                seed_acs = ac_texts(seed.acceptance_criteria)
                if focused:
                    parts.append(
                        f"Evolution Focus: {len(active)} active / "
                        f"{len(seed_acs) - len(active)} frozen AC nodes"
                    )
                    parts.append(
                        "Frozen nodes are immutable in this generation and are omitted below."
                    )
                    for index in sorted(active):
                        if 0 <= index < len(seed_acs):
                            parts.append(f"  ACTIVE AC {index + 1}: {seed_acs[index]}")
                else:
                    parts.append(f"Acceptance Criteria: {len(seed_acs)}")
                    for i, ac in enumerate(seed_acs, 1):
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
                visible_results = [
                    ac for ac in eval_summary.ac_results if not focused or ac.ac_index in active
                ]
                unresolved_acs = [ac for ac in visible_results if ac.unresolved]
                if unresolved_acs:
                    parts.append(f"\n  Active unresolved ACs ({len(unresolved_acs)}):")
                    for ac in unresolved_acs:
                        parts.append(f"    - AC {ac.ac_index + 1}: {ac.ac_content}")
                passed_count = sum(1 for ac in visible_results if ac.authoritative_pass)
                parts.append(f"  Visible AC pass rate: {passed_count}/{len(visible_results)}")

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
            # Focused generations prefer verifier evidence attached to the open
            # nodes.  The full prior transcript would re-expand context with
            # already-settled work and defeat working-set convergence.
            evidence = []
            if focused and eval_summary is not None:
                evidence = [
                    f"AC {result.ac_index + 1}: {result.evidence}"
                    for result in eval_summary.ac_results
                    if result.ac_index in active and result.evidence
                ]
            truncated = (
                "\n".join(evidence)
                if evidence
                else truncate_head_tail(
                    execution_output,
                    head=200 if focused else 500,
                    tail=800 if focused else 2000,
                )
            )
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

    def _parse_response(
        self,
        content: str,
        seed: Seed | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> WonderOutput:
        """Parse LLM response into WonderOutput."""
        total_acs = len(seed.acceptance_criteria) if seed else None
        try:
            # Extract the JSON payload, tolerating markdown fences and prose
            # that surround it (e.g. Gemini-style ``Here is ...`` prefixes).
            json_str = extract_json_payload(content)
            if json_str is None:
                raise ValueError("No valid JSON payload found")
            data = json.loads(json_str)
            if not isinstance(data, dict):
                raise TypeError(f"Expected JSON object, got {type(data).__name__}")
            raw_questions = data.get("questions", [])
            if "questions" in data and not isinstance(raw_questions, list):
                raise TypeError("Expected questions to be a list when present")
            grounded = self._parse_grounded_questions(raw_questions, total_acs)
            if raw_questions and not grounded:
                raise TypeError("Expected questions to contain strings or question objects")
            ontology_tensions = data.get("ontology_tensions", [])
            if not isinstance(ontology_tensions, list) or not all(
                isinstance(tension, str) for tension in ontology_tensions
            ):
                raise TypeError("Expected ontology_tensions to be a list of strings")
            should_continue = data.get("should_continue", True)
            if not isinstance(should_continue, bool):
                raise TypeError("Expected should_continue to be a boolean")
            if ontology_tensions and not should_continue:
                raise TypeError("Expected should_continue=true when ontology_tensions are present")
            reasoning = data.get("reasoning", "")
            if not isinstance(reasoning, str):
                raise TypeError("Expected reasoning to be a string")
            if active_ac_indices is not None:
                active = set(active_ac_indices)
                grounded = tuple(
                    GroundedQuestion(
                        question=question.question,
                        kind="challenge",
                        ac_indices=tuple(i for i in question.ac_indices if i in active),
                    )
                    for question in grounded
                    if question.kind == "challenge"
                    and any(i in active for i in question.ac_indices)
                )
                # An empty focused response cannot justify a full-graph retry.
                # Keep one concrete open-node question so Reflect receives a
                # bounded target rather than inventing a new gap.
                if not grounded and active:
                    target = min(active)
                    text = f"What blocks ACTIVE AC {target + 1} from passing its verifier?"
                    grounded = (
                        GroundedQuestion(
                            question=text,
                            kind="challenge",
                            ac_indices=(target,),
                        ),
                    )
                ontology_tensions = []
                should_continue = bool(grounded)
            return WonderOutput(
                # ``questions`` stays a flat string tuple for events, lineage, and
                # the repetitive-feedback convergence check (unchanged contract).
                questions=tuple(gq.question for gq in grounded),
                grounded_questions=grounded,
                ontology_tensions=tuple(ontology_tensions),
                should_continue=should_continue,
                reasoning=reasoning,
            )
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            logger.warning("Failed to parse WonderEngine response: %s", e)
            if active_ac_indices:
                target = min(active_ac_indices)
                fallback = f"What blocks ACTIVE AC {target + 1} from passing its verifier?"
                return WonderOutput(
                    questions=(fallback,),
                    grounded_questions=(
                        GroundedQuestion(
                            question=fallback,
                            kind="challenge",
                            ac_indices=(target,),
                        ),
                    ),
                    ontology_tensions=(),
                    should_continue=True,
                    reasoning=f"Parse error, using focused fallback: {e}",
                )
            scope_hint = f" for goal: {seed.goal}" if seed else ""
            fallback = f"What assumptions remain untested{scope_hint}?"
            return WonderOutput(
                questions=(fallback,),
                grounded_questions=(GroundedQuestion(question=fallback, kind="gap"),),
                ontology_tensions=(),
                should_continue=True,
                reasoning=f"Parse error, using seed-scoped fallback: {e}",
            )

    @staticmethod
    def _parse_grounded_questions(
        raw_questions: object, total_acs: int | None
    ) -> tuple[GroundedQuestion, ...]:
        """Parse the ``questions`` payload, tolerating new and legacy shapes.

        New shape: list of objects carrying ``ac_refs`` (1-based) or
        ``kind="gap"``. Legacy shape: list of plain strings, grounded via the
        deterministic AC-ref regex.
        """
        if not isinstance(raw_questions, list):
            return ()
        grounded: list[GroundedQuestion] = []
        for item in raw_questions:
            if isinstance(item, str):
                grounded.append(ground_question_text(item, total_acs))
            elif isinstance(item, dict):
                text = item.get("question") or item.get("text")
                if not isinstance(text, str) or not text:
                    continue
                refs = item.get("ac_refs")
                if isinstance(refs, (list, tuple)) and refs:
                    indices = _ground_ac_indices(refs, total_acs)
                    if indices:
                        grounded.append(
                            GroundedQuestion(question=text, kind="challenge", ac_indices=indices)
                        )
                        continue
                # No usable refs (or all out of range): fall back to regex on the
                # text, then treat as a gap.
                grounded.append(ground_question_text(text, total_acs))
        return tuple(grounded)

    def _degraded_output(
        self,
        eval_summary: EvaluationSummary | None,
        ontology: OntologySchema,
        seed: Seed | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> WonderOutput:
        """Generate fallback output when LLM fails (degraded mode)."""
        if active_ac_indices:
            target = min(active_ac_indices)
            question = f"What blocks ACTIVE AC {target + 1} from passing its verifier?"
            return WonderOutput(
                questions=(question,),
                grounded_questions=(
                    GroundedQuestion(
                        question=question,
                        kind="challenge",
                        ac_indices=(target,),
                    ),
                ),
                ontology_tensions=(),
                should_continue=True,
                reasoning="Degraded mode: using the verifier-selected active node",
            )
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
            # Heuristic questions have no AC evidence to challenge, so they are gaps.
            grounded_questions=tuple(GroundedQuestion(question=q, kind="gap") for q in questions),
            ontology_tensions=tuple(tensions),
            should_continue=should_continue,
            reasoning="Degraded mode: LLM unavailable, using heuristic questions"
            if should_continue
            else "Degraded mode: evaluation passed, no in-scope gaps remain",
        )


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
