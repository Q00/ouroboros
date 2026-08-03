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
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, field_validator

from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.lineage import EvaluationSummary, MutationAction, OntologyDelta, OntologyLineage
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed, ac_texts
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.evolution.regression import RegressionDetector, RegressionReport
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

logger = logging.getLogger(__name__)


def get_reflect_model(backend: str | None = None) -> str:
    """Compatibility wrapper for Reflect-stage model resolution."""
    return get_llm_model_for_role("reflect", backend=backend)


def _clean_required_text(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise TypeError(f"Expected {field_name} to be a non-empty string")
    return text


class OntologyMutation(BaseModel, frozen=True):
    """A specific proposed change to the ontology schema."""

    action: MutationAction
    field_name: str
    field_type: str | None = None
    description: str | None = None
    reason: str = ""


class ACPatch(BaseModel, frozen=True):
    """A single delta against the parent AC list.

    ``keep``/``revise`` target an existing parent AC by ``index`` (0-based);
    ``add`` appends a new AC (``index`` is None). ``remove`` is not offered in
    v1 — deleting an AC would shift positional identity that regression
    detection and the per-AC gate depend on.
    """

    op: Literal["keep", "revise", "add"]
    index: int | None = None
    content: str | None = None
    reason: str = ""

    @field_validator("index", mode="before")
    @classmethod
    def _normalize_index(cls, value: object) -> int | None:
        """Match persisted patch coercion to the strict fresh-response parser."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


class ReflectOutput(BaseModel, frozen=True):
    """Output of the Reflect phase -- feeds directly into SeedGenerator.

    Contains everything needed to create the next generation's Seed:
    refined goal, constraints, acceptance criteria, and ontology mutations.
    """

    refined_goal: str
    refined_constraints: tuple[str, ...] = Field(default_factory=tuple)
    refined_acs: tuple[str, ...] = Field(default_factory=tuple)
    ac_patches: tuple[ACPatch, ...] = Field(default_factory=tuple)
    settled_ac_indices: tuple[int, ...] = Field(default_factory=tuple)
    ontology_mutations: tuple[OntologyMutation, ...] = Field(default_factory=tuple)
    reasoning: str = ""
    _ac_patch_provenance: tuple[tuple[str, ...], tuple[ACPatch, ...]] | None = PrivateAttr(
        default=None
    )

    @property
    def ac_patch_identity_explicit(self) -> bool:
        """Whether this output carries parser-issued, non-serialized patch provenance."""
        return self._ac_patch_provenance == (self.refined_acs, self.ac_patches)

    @field_validator("refined_acs", mode="before")
    @classmethod
    def _coerce_refined_acs(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            if any(not isinstance(item, str | AcceptanceCriterionSpec) for item in value):
                raise TypeError("Expected refined acceptance criteria to contain strings")
            descriptions = tuple(
                _clean_required_text(description, "refined acceptance criterion")
                for description in ac_texts(value)
            )
            if any(
                ord(character) < 32 or ord(character) == 127
                for description in descriptions
                for character in description
            ):
                raise TypeError("Expected refined acceptance criteria without control characters")
            return descriptions
        return value


def _parse_ontology_mutations(data: dict[str, object]) -> list[OntologyMutation]:
    raw_mutations = data.get("ontology_mutations", [])
    if not isinstance(raw_mutations, list):
        raise TypeError("Expected ontology_mutations to be a list")

    mutations: list[OntologyMutation] = []
    for item in raw_mutations:
        if not isinstance(item, dict):
            raise TypeError("Expected ontology_mutations to contain objects")

        raw_field_name = item.get("field_name")
        if not isinstance(raw_field_name, str) or not raw_field_name.strip():
            raise TypeError("Expected ontology mutation field_name to be a non-empty string")
        field_name = raw_field_name.strip()

        if "action" not in item:
            raise TypeError("Expected ontology mutation action to be explicit")
        raw_action = item["action"]
        try:
            action = MutationAction(raw_action)
        except (TypeError, ValueError):
            raise TypeError(f"Expected valid ontology mutation action, got {raw_action!r}")

        raw_field_type = item.get("field_type")
        if raw_field_type is not None and not isinstance(raw_field_type, str):
            raise TypeError("Expected ontology mutation field_type to be a string or null")
        field_type = (
            _clean_required_text(raw_field_type, "ontology mutation field_type")
            if raw_field_type is not None
            else None
        )
        raw_description = item.get("description")
        if raw_description is not None and not isinstance(raw_description, str):
            raise TypeError("Expected ontology mutation description to be a string or null")
        description = (
            _clean_required_text(raw_description, "ontology mutation description")
            if raw_description is not None
            else None
        )
        raw_reason = item.get("reason", "")
        if not isinstance(raw_reason, str):
            raise TypeError("Expected ontology mutation reason to be a string")
        reason = raw_reason.strip()
        if action is MutationAction.ADD and not (description or reason):
            raise TypeError("Expected add ontology mutation description or reason to be non-empty")

        mutations.append(
            OntologyMutation(
                action=action,
                field_name=field_name,
                field_type=field_type,
                description=description,
                reason=reason,
            )
        )
    return mutations


def _parse_ac_patches(raw_patches: object) -> list[ACPatch]:
    """Parse raw LLM patch objects, coercing unknown/``remove`` ops to keep."""
    patches: list[ACPatch] = []
    if not isinstance(raw_patches, list):
        raise TypeError("Expected ac_patches to be a list")
    for item in raw_patches:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op", "")).lower()
        if op not in ("keep", "revise", "add"):
            logger.warning("reflect.patch.op_coerced_to_keep", extra={"op": op})
            op = "keep"
        raw_index = item.get("index")
        index = (
            raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        )
        raw_content = item.get("content")
        content = None
        if isinstance(raw_content, str):
            content = _clean_required_text(raw_content, "acceptance criterion patch content")
        raw_reason = item.get("reason", "")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        patches.append(ACPatch(op=op, index=index, content=content, reason=reason))
    return patches


def _validate_explicit_ac_patch_identity(raw_patches: object, parent_count: int) -> None:
    """Require a complete, unique parent-index map before trusting patch provenance."""
    if not isinstance(raw_patches, list):
        raise TypeError("Expected ac_patches to be a list")

    seen_parent_indices: set[int] = set()
    for item in raw_patches:
        if not isinstance(item, dict):
            raise TypeError("Expected each ac_patch to be an object")
        op = item.get("op")
        if op in ("keep", "revise"):
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise TypeError("Expected keep/revise ac_patch index to be an integer")
            if index < 0 or index >= parent_count:
                raise ValueError("Explicit ac_patch index is outside the parent AC range")
            if index in seen_parent_indices:
                raise ValueError("Explicit ac_patches contain a duplicate parent AC index")
            seen_parent_indices.add(index)
            if op == "keep":
                if item.get("content") is not None:
                    raise ValueError("Explicit keep ac_patch cannot carry replacement content")
            else:
                content = item.get("content")
                if not isinstance(content, str):
                    raise TypeError("Expected revise ac_patch content to be a string")
                _clean_required_text(content, "acceptance criterion patch content")
            continue
        if op == "add":
            if item.get("index") is not None:
                raise ValueError("Explicit add ac_patch cannot carry a parent AC index")
            content = item.get("content")
            if not isinstance(content, str):
                raise TypeError("Expected add ac_patch content to be a string")
            _clean_required_text(content, "acceptance criterion patch content")
            continue
        raise ValueError("Explicit ac_patch op must be keep, revise, or add")

    if seen_parent_indices != set(range(parent_count)):
        raise ValueError("Explicit ac_patches must identify every parent AC exactly once")


def _derive_legacy_patches(
    refined_acs: tuple[str, ...], parent_acs: tuple[str, ...]
) -> list[ACPatch] | None:
    """Derive patches from a full refined-AC list (old JSON shape).

    Verbatim positional diff: identical text → keep, different → revise, extra
    tail entries → add. A shorter list or an entry moved from another parent
    position returns None because positional identity would be ambiguous.
    """
    if len(refined_acs) < len(parent_acs):
        return None
    if any(
        new_text != parent_acs[index] and new_text in parent_acs
        for index, new_text in enumerate(refined_acs[: len(parent_acs)])
    ) or any(new_text in parent_acs for new_text in refined_acs[len(parent_acs) :]):
        return None
    patches: list[ACPatch] = []
    for i, parent_text in enumerate(parent_acs):
        new_text = refined_acs[i]
        if new_text == parent_text:
            patches.append(ACPatch(op="keep", index=i))
        else:
            patches.append(ACPatch(op="revise", index=i, content=new_text))
    for j in range(len(parent_acs), len(refined_acs)):
        patches.append(ACPatch(op="add", content=refined_acs[j]))
    return patches


def _apply_satisficing_backstop(
    parent_acs: tuple[str, ...],
    patches: list[ACPatch],
    protected: set[int],
    passed_indices: set[int],
    *,
    allow_additions: bool = True,
    reject_ambiguous_identity: bool = False,
) -> tuple[tuple[str, ...], tuple[ACPatch, ...], tuple[int, ...]]:
    """Deterministically enforce the satisficing invariant.

    The LLM proposes; this disposes. Protected indices (passed AND not
    challenged AND not regressed) are forced to verbatim keep. Missing indices
    keep implicitly. Malformed/duplicate/out-of-range patches are dropped so the
    composed list holds every parent index exactly once (keeps/revises in place,
    adds appended in order), preserving positional AC identity. Fresh Reflect
    output rejects patches that reuse a parent identity so the provider retry
    boundary can recover. Persisted legacy output instead drops those unsafe
    patches and falls back to an implicit keep, preserving replayability.

    Returns ``(refined_acs, final_patches, settled_ac_indices)``.
    """
    n = len(parent_acs)
    keep_revise: dict[int, ACPatch] = {}
    adds: list[ACPatch] = []

    for patch in patches:
        if patch.op == "add":
            if not allow_additions:
                logger.info(
                    "reflect.patch.dropped",
                    extra={"reason": "focused_evolution_disallows_new_node"},
                )
                continue
            if not patch.content:
                logger.warning("reflect.patch.dropped", extra={"reason": "add_without_content"})
                continue
            if patch.content in parent_acs:
                if reject_ambiguous_identity:
                    raise ValueError("acceptance criterion patch reuses another parent identity")
                logger.warning(
                    "reflect.patch.dropped",
                    extra={"reason": "add_reuses_parent_identity"},
                )
                continue
            if patch.index is not None:
                logger.info(
                    "reflect.patch.normalized",
                    extra={"reason": "add_index_removed", "index": patch.index},
                )
                patch = patch.model_copy(update={"index": None})
            adds.append(patch)
            continue
        # keep / revise must target a valid, not-yet-seen parent index.
        if patch.index is None or not (0 <= patch.index < n):
            logger.warning(
                "reflect.patch.dropped",
                extra={"reason": "index_out_of_range", "index": patch.index, "op": patch.op},
            )
            continue
        if patch.op == "revise" and not patch.content:
            logger.warning(
                "reflect.patch.dropped",
                extra={"reason": "revise_without_content", "index": patch.index},
            )
            continue
        if (
            patch.op == "revise"
            and patch.content != parent_acs[patch.index]
            and patch.content in parent_acs
        ):
            if reject_ambiguous_identity:
                raise ValueError("acceptance criterion patch reuses another parent identity")
            logger.warning(
                "reflect.patch.dropped",
                extra={"reason": "revise_reuses_parent_identity", "index": patch.index},
            )
            continue
        if patch.index in keep_revise:
            logger.warning(
                "reflect.patch.dropped", extra={"reason": "duplicate_index", "index": patch.index}
            )
            continue
        keep_revise[patch.index] = patch

    # Backstop: a protected AC may not be revised — force verbatim keep.
    for i, patch in list(keep_revise.items()):
        if i in protected and patch.op == "revise":
            logger.info("reflect.backstop.forced_keep", extra={"index": i})
            keep_revise[i] = ACPatch(
                op="keep", index=i, reason="satisficing backstop: protected AC"
            )

    # Implicit keep for any parent index the LLM omitted.
    for i in range(n):
        if i not in keep_revise:
            keep_revise[i] = ACPatch(op="keep", index=i, reason="implicit keep")

    refined: list[str] = []
    final_patches: list[ACPatch] = []
    settled: list[int] = []
    for i in range(n):
        patch = keep_revise[i]
        if patch.op == "keep":
            refined.append(parent_acs[i])
            if i in passed_indices:
                settled.append(i)
        else:
            refined.append(patch.content or parent_acs[i])
        final_patches.append(patch)
    for add in adds:
        refined.append(add.content or "")
        final_patches.append(add)

    return tuple(refined), tuple(final_patches), tuple(settled)


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
    adapter_backend_factory: Callable[[], str | None] | None = field(default=None, repr=False)
    _captured_backend: str | None = field(default=None, init=False, repr=False)
    _model_is_explicit: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Track explicit model pins while allowing backend-aware implicit defaults."""
        self._model_is_explicit = self.model is not None
        try:
            self._captured_backend = self.adapter_backend or get_llm_backend_for_role("reflect")
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
        if self.adapter_backend_factory is not None:
            try:
                backend = self.adapter_backend_factory()
                if backend:
                    return backend
            except Exception:  # noqa: BLE001
                logger.exception("ReflectEngine adapter_backend_factory raised")
        if self.adapter_backend is not None:
            return self.adapter_backend
        try:
            return get_llm_backend_for_role("reflect")
        except Exception:  # noqa: BLE001
            return None

    async def reflect(
        self,
        current_seed: Seed,
        execution_output: str,
        evaluation_summary: EvaluationSummary | None,
        wonder_output: WonderOutput,
        lineage: OntologyLineage,
        regression_report: RegressionReport | None = None,
        conductor_directive: ConductorDirective | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> Result[ReflectOutput, ProviderError]:
        """Reflect on execution results and propose evolution.

        Args:
            current_seed: The seed that was executed.
            execution_output: What was actually produced.
            evaluation_summary: How the execution was evaluated, or ``None``
                during ontology-only exploration. Missing evaluation never
                creates PASS, protection, or settling authority.
            wonder_output: What we still don't know (from WonderEngine).
            lineage: Full lineage for cross-generation context.
            regression_report: Precomputed regressions (from the loop). When
                None, computed once here and reused for both the prompt and the
                satisficing backstop.

        Returns:
            Result containing ReflectOutput or ProviderError.
        """
        if regression_report is None:
            regression_report = RegressionDetector().detect(lineage)

        prompt = self._build_prompt(
            current_seed,
            execution_output,
            evaluation_summary,
            wonder_output,
            lineage,
            regression_report,
            conductor_directive,
            active_ac_indices,
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

        parsed = self._parse_response(
            raw_content,
            current_seed,
            evaluation_summary,
            wonder_output,
            regression_report,
            active_ac_indices,
        )
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
    "ac_patches": [
        {"op": "keep", "index": 0, "reason": "passed, unchallenged"},
        {"op": "revise", "index": 2, "content": "the corrected acceptance criterion", "reason": "failed / challenged"},
        {"op": "add", "content": "a new acceptance criterion for an uncovered gap", "reason": "gap question"}
    ],
    "ontology_mutations": [
        {"action": "add|modify|remove", "field_name": "name", "field_type": "type", "description": "desc", "reason": "why"},
        ...
    ],
    "reasoning": "explanation of why these changes are needed"
}

SATISFICING DELTA — patch the AC list, do NOT rewrite it:
- The AC list is addressed by 0-based "index" against the CURRENT seed's ACs.
- Emit ONE patch per current AC, plus "add" patches for new ACs.
- "keep" (index only): an AC that PASSED evaluation AND is not named by any
  grounded challenge AND is not regressed MUST be kept VERBATIM. Do not reword a
  settled AC — rational agents with bounded resources do not re-derive satisficed
  commitments without evidence. (A deterministic backstop will force-keep these
  even if you try to revise them.)
- "revise" (index + content): only for ACs that FAILED, were CHALLENGED by a
  grounded Wonder question, or REGRESSED. Provide the full corrected AC text.
- "add" (content only): for gap questions — something the goal requires that no
  AC covers yet.
- "remove" is NOT available in v1. Never delete an AC; it would break positional
  AC identity.

Guidelines:
- If Wonder questions exist, you MUST propose at least one ontology_mutation that addresses them
- If Evaluation Results says "Not evaluated", do not infer PASS, protection, or
  settlement for any AC; evolve only from Wonder questions and ontology context.
- If evaluation score >= 0.8 and approved, keep changes focused but still evolve the ontology based on Wonder insights
- If evaluation score < 0.8 or not approved, propose more aggressive mutations to address failures
- Each mutation must have a clear reason tied to evaluation findings or wonder questions
- Patches should address the wonder questions and ontology tensions
- When an "Evolution Focus" is present, only ACTIVE AC indices may be revised.
  Keep every frozen node verbatim, do not add ACs, and do not rewrite the goal
  or constraints. The next generation's working set must not expand.
- Do NOT change things that are working well -- only evolve what needs evolution
- action must be exactly one of: "add", "modify", "remove"
- An empty ontology_mutations list is ONLY acceptable when there are no Wonder questions
"""

    def _build_prompt(
        self,
        seed: Seed,
        execution_output: str,
        eval_summary: EvaluationSummary | None,
        wonder: WonderOutput,
        lineage: OntologyLineage,
        regression_report: RegressionReport,
        conductor_directive: ConductorDirective | None = None,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> str:
        parts = ["## Current Seed"]
        parts.append(f"Goal: {seed.goal}")
        parts.append(f"Constraints: {list(seed.constraints)}")
        seed_acs = ac_texts(seed.acceptance_criteria)
        focused = active_ac_indices is not None
        active = set(active_ac_indices or ())
        if focused:
            parts.append(
                f"Evolution Focus: {len(active)} active / {len(seed_acs) - len(active)} frozen"
            )
            parts.append("Only these nodes may change; all omitted nodes are immutable:")
            for index in sorted(active):
                if 0 <= index < len(seed_acs):
                    parts.append(f"  ACTIVE AC {index + 1}: {seed_acs[index]}")
        else:
            parts.append(f"Acceptance Criteria: {list(seed_acs)}")

        if conductor_directive is not None:
            parts.append("\n## Active Conductor Successor Directive")
            parts.append(f"Instruction: {conductor_directive.instruction}")
            if conductor_directive.rejected_reasons:
                parts.append("Rejected evidence reasons:")
                parts.extend(f"  - {reason}" for reason in conductor_directive.rejected_reasons)
            parts.append(
                "Preserve approved direction exactly where these flags are true: "
                f"goal={conductor_directive.preserve_goal}, "
                "acceptance_criteria="
                f"{conductor_directive.preserve_acceptance_criteria}, "
                f"constraints={conductor_directive.preserve_constraints}, "
                f"non_goals={conductor_directive.preserve_non_goals}."
            )
            parts.append(
                "Use the directive to correct implementation or evidence. Do not relax a "
                "preserved field to make evaluation easier."
            )

        parts.append(f"\n## Ontology: {seed.ontology_schema.name}")
        parts.append(f"Description: {seed.ontology_schema.description}")
        for f in seed.ontology_schema.fields:
            parts.append(f"  - {f.name} ({f.field_type}): {f.description}")

        parts.append("\n## Evaluation Results")
        if eval_summary is None:
            parts.append("  Not evaluated (ontology-only exploration).")
            parts.append(
                "  No AC has PASS, protection, or settling authority; use only "
                "the Wonder questions and ontology context."
            )
        else:
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
                visible_results = [
                    ac for ac in eval_summary.ac_results if not focused or ac.ac_index in active
                ]
                for ac in visible_results:
                    status = ac.authority_state.upper()
                    parts.append(f"    AC {ac.ac_index + 1} [{status}]: {ac.ac_content}")
                unresolved_acs = [ac for ac in visible_results if ac.unresolved]
                if unresolved_acs:
                    parts.append(
                        f"\n  PRIORITY: Resolve {len(unresolved_acs)} open AC(s) while preserving passing ones."
                    )

        # Regression context (precomputed once by the caller and reused here).
        if regression_report.has_regressions:
            parts.append(f"\n## REGRESSIONS ({len(regression_report.regressions)})")
            for reg in regression_report.regressions:
                parts.append(
                    f"  - AC {reg.ac_index + 1} (Gen {reg.passed_in_generation}→Gen {reg.failed_in_generation}): "
                    f"{reg.ac_text}"
                )
            parts.append(
                "  CRITICAL: These ACs previously passed. Preserve their behavior while fixing other issues."
            )

        # Grounded challenges: which ACs a Wonder question explicitly reopened.
        challenged: set[int] = set()
        for gq in wonder.grounded_questions:
            if gq.kind == "challenge":
                challenged.update(gq.ac_indices)
        if challenged:
            challenged_labels = ", ".join(f"AC {i + 1}" for i in sorted(challenged))
            parts.append(f"\n## Grounded Challenges (ACs reopened by Wonder): {challenged_labels}")
            parts.append("  Only these passing ACs may be revised; keep all other passing ACs.")

        parts.append("\n## Wonder Questions (what we still don't know)")
        for q in wonder.questions:
            parts.append(f"  - {q}")

        if wonder.ontology_tensions:
            parts.append("\n## Ontology Tensions")
            for t in wonder.ontology_tensions:
                parts.append(f"  - {t}")

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

    def _parse_response(
        self,
        content: str,
        current_seed: Seed,
        evaluation_summary: EvaluationSummary | None,
        wonder_output: WonderOutput,
        regression_report: RegressionReport,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> ReflectOutput | None:
        """Parse LLM response into ReflectOutput.

        Parses the AC delta (new ``ac_patches`` shape or legacy full-list diff),
        then applies the deterministic satisficing backstop before composing the
        final ``refined_acs``. Returns None on JSON parse failure so the caller
        can retry or propagate error.
        """
        try:
            # Extract the JSON payload, tolerating markdown fences and prose
            # that surround it (e.g. Gemini-style ``Here is ...`` prefixes).
            json_str = extract_json_payload(content)
            if json_str is None:
                raise ValueError("No valid JSON payload found")
            data = json.loads(json_str)
            if not isinstance(data, dict):
                raise TypeError(f"Expected JSON object, got {type(data).__name__}")

            mutations = _parse_ontology_mutations(data)

            parent_acs = ac_texts(current_seed.acceptance_criteria)
            explicit_patch_identity = "ac_patches" in data
            if explicit_patch_identity:
                _validate_explicit_ac_patch_identity(data["ac_patches"], len(parent_acs))
            refined_acs, ac_patches, settled = self._compose_acs(
                data,
                parent_acs,
                evaluation_summary,
                wonder_output,
                regression_report,
                active_ac_indices,
            )
            if explicit_patch_identity and "refined_acs" in data:
                raw_refined_acs = data["refined_acs"]
                if not isinstance(raw_refined_acs, list | tuple) or not all(
                    isinstance(criterion, str) for criterion in raw_refined_acs
                ):
                    raise TypeError("Expected refined_acs to be a list of strings")
                supplied_refined_acs = tuple(
                    _clean_required_text(criterion, "refined acceptance criterion")
                    for criterion in raw_refined_acs
                )
                if supplied_refined_acs != refined_acs:
                    raise ValueError("Explicit ac_patches conflict with refined_acs")
            refined_goal = data.get("refined_goal", current_seed.goal)
            if not isinstance(refined_goal, str):
                raise TypeError("Expected refined_goal to be a string")
            if not refined_goal.strip():
                raise TypeError("Expected refined_goal to be a non-empty string")
            raw_refined_constraints = data.get(
                "refined_constraints", list(current_seed.constraints)
            )
            if not isinstance(raw_refined_constraints, list | tuple) or not all(
                isinstance(constraint, str) for constraint in raw_refined_constraints
            ):
                raise TypeError("Expected refined_constraints to be a list of strings")
            refined_constraints = tuple(
                _clean_required_text(constraint, "refined constraint")
                for constraint in raw_refined_constraints
            )
            if active_ac_indices is not None:
                # Node-focused evolution cannot drift global direction while
                # repairing a bounded AC working set.
                refined_goal = current_seed.goal
                refined_constraints = current_seed.constraints
            reasoning = data.get("reasoning", "")
            if not isinstance(reasoning, str):
                raise TypeError("Expected reasoning to be a string")

            output = ReflectOutput(
                refined_goal=refined_goal,
                refined_constraints=refined_constraints,
                refined_acs=refined_acs,
                ac_patches=ac_patches,
                settled_ac_indices=settled,
                ontology_mutations=tuple(mutations),
                reasoning=reasoning,
            )
            if explicit_patch_identity:
                output._ac_patch_provenance = (output.refined_acs, output.ac_patches)
            return output
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            logger.warning(
                "reflect.parse_failed",
                extra={
                    "error": str(e),
                    "raw_content": content[:1000],
                },
            )
            return None

    @staticmethod
    def _compose_acs(
        data: dict[str, object],
        parent_acs: tuple[str, ...],
        evaluation_summary: EvaluationSummary | None,
        wonder_output: WonderOutput,
        regression_report: RegressionReport,
        active_ac_indices: tuple[int, ...] | None = None,
    ) -> tuple[tuple[str, ...], tuple[ACPatch, ...], tuple[int, ...]]:
        """Compose the next AC list from LLM patches under the satisficing backstop."""
        passed_indices = (
            {ac.ac_index for ac in evaluation_summary.ac_results if ac.authoritative_pass}
            if evaluation_summary is not None
            else set()
        )
        challenged: set[int] = set()
        for gq in wonder_output.grounded_questions:
            if gq.kind == "challenge":
                challenged.update(gq.ac_indices)
        regressed = set(regression_report.regressed_ac_indices)
        protected = {
            i
            for i in passed_indices
            if 0 <= i < len(parent_acs) and i not in challenged and i not in regressed
        }
        # Invariant: a regressed AC is never settled, even if kept — subtract it
        # from the settleable set before the backstop decides settling.
        settleable = passed_indices - regressed - challenged
        if active_ac_indices is not None:
            active = {index for index in active_ac_indices if 0 <= index < len(parent_acs)}
            protected.update(set(range(len(parent_acs))) - active)

        if "ac_patches" in data:
            raw_patches = data["ac_patches"]
            patches = _parse_ac_patches(raw_patches)
            return _apply_satisficing_backstop(
                parent_acs,
                patches,
                protected,
                settleable,
                allow_additions=active_ac_indices is None,
                reject_ambiguous_identity=True,
            )

        # Legacy full-list shape: derive patches by positional diff.
        raw_refined_acs = data.get("refined_acs", list(parent_acs))
        if not isinstance(raw_refined_acs, list) or not all(
            isinstance(ac, str) for ac in raw_refined_acs
        ):
            raise TypeError("Expected refined_acs to be a list of strings")
        llm_refined_acs = tuple(
            _clean_required_text(ac, "refined acceptance criterion") for ac in raw_refined_acs
        )
        legacy_patches = _derive_legacy_patches(llm_refined_acs, parent_acs)
        if legacy_patches is None:
            raise ValueError(
                "legacy refined_acs cannot delete or reorder parent criteria; use ac_patches"
            )
        return _apply_satisficing_backstop(
            parent_acs,
            legacy_patches,
            protected,
            settleable,
            allow_additions=active_ac_indices is None,
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
