"""Immutable Seed schema for workflow execution.

The Seed is the "constitution" of a workflow - an immutable specification
generated from the Big Bang interview phase when ambiguity score <= 0.2.

Key properties:
- Seed.direction (goal, constraints, acceptance_criteria) is IMMUTABLE
- Effective ontology can evolve with consensus during iterations
- Contains all information needed to execute and evaluate the workflow

This module defines:
- Seed: The immutable Pydantic model with frozen=True
- SeedMetadata: Version and creation metadata
- Supporting types for seed components
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from ouroboros.core.conductor import ConductorDirective

_OUTPUT_ASSERTION_CONDITION_RE = re.compile(
    r"^(?:exit\s*(?:code|status)?\s*0|returns?\s*0|success|succeeds|passed|passes|ok exit|no errors?)$",
    re.IGNORECASE,
)


def expected_artifact_path_error(artifact: str) -> str | None:
    """Return why an expected-artifact value is not a safe relative path."""
    if not artifact or any(ord(character) < 32 or ord(character) == 127 for character in artifact):
        return "contains an empty value or control character"

    posix_path = PurePosixPath(artifact)
    windows_path = PureWindowsPath(artifact)
    if not posix_path.parts:
        return "resolves to the workspace root"
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        return "is absolute or escapes workspace"
    if any(character.isspace() for character in artifact):
        has_explicit_relative_structure = "/" in artifact or "\\" in artifact
        if not has_explicit_relative_structure:
            return "is ambiguous prose; prefix a top-level spaced path with ./"
    return None


class ExitCondition(BaseModel, frozen=True):
    """Defines when the workflow should terminate.

    Attributes:
        name: Short identifier for the condition.
        description: Detailed explanation of the exit condition.
        evaluation_criteria: How to determine if condition is met.
    """

    model_config = {"populate_by_name": True}

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    evaluation_criteria: str = Field(..., min_length=1, alias="criteria")


class EvaluationPrinciple(BaseModel, frozen=True):
    """A principle for evaluating workflow outputs.

    Attributes:
        name: Short identifier for the principle.
        description: What this principle evaluates.
        weight: Relative importance (0.0 to 1.0).
    """

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class OntologyField(BaseModel, frozen=True):
    """A field in an ontology schema.

    Attributes:
        name: Field identifier.
        field_type: Field type (string, number, boolean, array, object).
        description: Purpose of this field.
        required: Whether this field is required.
    """

    model_config = {"populate_by_name": True}

    name: str = Field(..., min_length=1)
    field_type: str = Field(..., min_length=1, alias="type")
    description: str = Field(..., min_length=1)
    required: bool = Field(default=True)


class OntologySchema(BaseModel, frozen=True):
    """Schema describing workflow domain structure.

    The ontology schema names the domain fields and boundaries the workflow
    should preserve throughout iterations. It is not, by itself, a mandatory
    output shape.

    Attributes:
        name: Name of the ontology.
        description: Purpose, scope, and perspective of this ontology.
        fields: Fields in the ontology.
    """

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    fields: tuple[OntologyField, ...] = Field(default_factory=tuple)


class ContextReference(BaseModel, frozen=True):
    """Reference to an existing codebase directory.

    Attributes:
        path: Absolute path to the codebase directory.
        role: 'primary' (modify this) or 'reference' (read-only).
        summary: Auto-generated codebase summary from exploration.
    """

    path: str = Field(..., min_length=1, description="Absolute path to codebase")
    role: str = Field(..., description="'primary' (modify this) or 'reference' (read-only)")
    summary: str = Field(default="", description="Auto-generated codebase summary")


class BrownfieldContext(BaseModel, frozen=True):
    """Context for brownfield projects.

    For greenfield projects, this remains at defaults (project_type="greenfield",
    empty references). For brownfield, it carries discovered codebase context.

    Attributes:
        project_type: 'greenfield' or 'brownfield'.
        context_references: Referenced codebase directories with roles.
        existing_patterns: Key patterns discovered in existing code.
        existing_dependencies: Key dependencies discovered in existing code.
    """

    project_type: str = Field(
        default="greenfield",
        description="'greenfield' or 'brownfield'",
    )
    context_references: tuple[ContextReference, ...] = Field(
        default_factory=tuple,
        description="Referenced codebase directories",
    )
    existing_patterns: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Key patterns discovered in existing code",
    )
    existing_dependencies: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Key dependencies discovered in existing code",
    )


class SeedMetadata(BaseModel, frozen=True):
    """Metadata about the Seed generation.

    Attributes:
        seed_id: Unique identifier for this seed.
        version: Schema version for forward compatibility.
        created_at: When this seed was generated.
        ambiguity_score: The ambiguity score at generation time.
        interview_id: Reference to the source interview.
        generation_mode: Provenance label for how the Seed was synthesized.
            ``"normal"`` is the legacy ledger-complete path; degraded recovery
            paths (e.g. ``"partial_seed_from_evidence"``) MUST set this so
            grading/run gates can distinguish a fully-resolved Seed from one
            built under deadline pressure (#1257).
        degraded: ``True`` when the Seed was synthesized under a recovery path
            rather than a clean low-ambiguity closure, so grade/run gates route
            it to a typed partial-product terminal instead of auto-RUN. Always
            pairs with a non-default ``generation_mode``. Two cases:
            (a) incomplete ledger (``partial_seed_from_evidence``) — carries at
            least one entry in ``unresolved_slots``; (b) complete ledger closed
            via the interview-phase deadline (#1302) — ``unresolved_slots`` is
            empty because the ledger is fully resolved, but no backend-confirmed
            low ambiguity exists.
        unresolved_slots: Ledger sections that were still
            MISSING/WEAK/CONFLICTING/BLOCKED at synthesis time. Empty for normal
            seeds and for complete-ledger deadline-recovery seeds. Surfaced
            verbatim so downstream gates can transform them into ``next_step``
            hints instead of terminal blockers.
        recovery_reason: Free-form description of why the degraded path was
            taken (e.g. ``"interview_phase_deadline"``). ``None`` for normal
            seeds.
        decision_provenance: Histogram of how the ledger decisions behind this
            Seed were reached, keyed by decision-origin class (``user_confirmed``
            / ``model_inferred`` / ``timeout_default`` / ``lateral_consensus`` /
            ``maintainer_policy``) → count (A1 / #1485). Empty for Seeds not
            synthesized from an auto ledger. Additive and greppable so later
            phases can audit how many gated decisions a seed rests on.
    """

    seed_id: str = Field(default_factory=lambda: f"seed_{uuid4().hex[:12]}")
    version: str = Field(default="1.0.0")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ambiguity_score: float = Field(default=0.15, ge=0.0, le=1.0)
    interview_id: str | None = Field(default=None)
    parent_seed_id: str | None = Field(default=None)
    generation_mode: str = Field(default="normal", min_length=1)
    degraded: bool = Field(default=False)
    unresolved_slots: tuple[str, ...] = Field(default_factory=tuple)
    recovery_reason: str | None = Field(default=None)
    decision_provenance: dict[str, int] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize additively: omit ``decision_provenance`` when empty.

        Keeps legacy Seed JSON byte-identical after a round-trip (the histogram
        is only present for ledger-synthesized seeds) while still emitting it
        whenever an auto ledger populated it.
        """
        data = handler(self)
        if not self.decision_provenance:
            data.pop("decision_provenance", None)
        return data


InvestmentLevel = Literal["low", "medium", "high"]
InvestmentProvenance = Literal["declared", "measured", "inferred", "absent"]
InvestmentConfidence = Literal["low", "medium", "high"]


class InvestmentSpec(BaseModel, frozen=True):
    """Bounded AC-level authority for execution investment decisions.

    The schema intentionally carries declarations and provenance, not inferred
    text features. Missing axes remain unknown at assessment time. Confidence
    defaults to ``low`` so an incomplete declaration cannot silently authorize
    cheaper execution.
    """

    difficulty: InvestmentLevel | None = None
    stakes: InvestmentLevel | None = None
    provenance: InvestmentProvenance = "declared"
    confidence: InvestmentConfidence = "low"


class AcceptanceCriterionSpec(BaseModel, frozen=True):
    """Structured success contract for one acceptance criterion.

    ``expected_artifacts`` entries are exact file or directory paths relative
    to the run workspace. They are not descriptive artifact labels: the
    execution verifier resolves every entry literally and requires it to
    exist.
    """

    description: str
    semantic_ac_key: str | None = Field(default=None, pattern=r"^ac_[a-f0-9]{16}$")
    verify_command: str | None = Field(default=None)
    expected_artifacts: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Exact file or directory paths relative to the run workspace; "
            "each path is resolved literally and must exist"
        ),
    )
    output_assertion: str | None = Field(
        default=None,
        description=("Literal string required in the verify command's combined stdout and stderr"),
    )
    investment: InvestmentSpec | None = Field(default=None)

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("verify_command", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.upper() == "NONE":
                return None
            return stripped
        return value

    @field_validator("output_assertion", mode="before")
    @classmethod
    def _normalize_output_assertion(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.upper() == "NONE":
                return None
            if _OUTPUT_ASSERTION_CONDITION_RE.fullmatch(stripped):
                return None
            return stripped
        return value

    @field_validator("expected_artifacts", mode="before")
    @classmethod
    def _coerce_expected_artifacts(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            if not value.strip() or value.strip().upper() == "NONE":
                return ()
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, list | tuple | set):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return value

    @property
    def has_success_contract(self) -> bool:
        """Return True when explicit evidence fields are populated."""
        return bool(self.verify_command or self.expected_artifacts or self.output_assertion)

    def with_materialized_semantic_key(self) -> AcceptanceCriterionSpec:
        """Return this criterion with a deterministic legacy semantic identity."""
        if self.semantic_ac_key is not None:
            return self
        return self.model_copy(update={"semantic_ac_key": derive_semantic_ac_key(self)})

    def to_seed_value(self) -> str | dict[str, Any]:
        """Return the stable persisted representation for this AC."""
        if (
            not self.has_success_contract
            and self.investment is None
            and self.semantic_ac_key is None
        ):
            return self.description
        data: dict[str, Any] = {"description": self.description}
        if self.semantic_ac_key:
            data["semantic_ac_key"] = self.semantic_ac_key
        if self.verify_command:
            data["verify_command"] = self.verify_command
        if self.expected_artifacts:
            data["expected_artifacts"] = list(self.expected_artifacts)
        if self.output_assertion:
            data["output_assertion"] = self.output_assertion
        if self.investment is not None:
            data["investment"] = self.investment.model_dump(mode="json", exclude_none=True)
        return data

    def __str__(self) -> str:
        return self.description


AcceptanceCriterionInput = str | AcceptanceCriterionSpec


def derive_semantic_ac_key(criterion: AcceptanceCriterionInput) -> str:
    """Derive a stable semantic identity for a legacy or newly created AC.

    The digest intentionally excludes list position, runtime/session identity,
    and volatile Seed metadata. Preserved structured criteria keep their
    explicit key across retries and successors; a semantically replaced
    criterion gets a new key when materialized from its changed contract.
    """
    if isinstance(criterion, AcceptanceCriterionSpec):
        payload = {
            "description": criterion.description,
            "verify_command": criterion.verify_command,
            "expected_artifacts": list(criterion.expected_artifacts),
            "output_assertion": criterion.output_assertion,
        }
    else:
        payload = {"description": str(criterion).strip()}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"ac_{hashlib.sha256(encoded).hexdigest()[:16]}"


def ac_text(criterion: AcceptanceCriterionInput) -> str:
    """Return the human-readable description for an acceptance criterion."""
    if isinstance(criterion, AcceptanceCriterionSpec):
        return criterion.description
    return str(criterion)


def ac_texts(criteria: Any) -> tuple[str, ...]:
    """Return AC descriptions while tolerating legacy string iterables."""
    return tuple(ac_text(criterion) for criterion in criteria)


class Seed(BaseModel, frozen=True):
    """Immutable specification for workflow execution.

    The Seed is the "constitution" of the workflow - once generated, it cannot
    be modified. This ensures consistency throughout the workflow lifecycle.

    Direction (goal, constraints, acceptance_criteria) is IMMUTABLE:
    - These define WHAT the workflow should achieve
    - Cannot be changed after generation
    - Serves as the ground truth for evaluation

    Attributes:
        goal: The primary objective of the workflow.
        constraints: Hard constraints that must be satisfied.
        acceptance_criteria: Specific criteria for success.
        ontology_schema: Conceptual lens for workflow coherence.
        evaluation_principles: Principles for evaluating outputs.
        exit_conditions: Conditions for terminating the workflow.
        metadata: Generation metadata (version, timestamp, etc.).

    Example:
        seed = Seed(
            goal="Build a CLI task management tool",
            constraints=("Python 3.14+", "No external database"),
            acceptance_criteria=("Tasks can be created", "Tasks can be listed"),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management ontology",
                fields=(
                    OntologyField(
                        name="tasks",
                        field_type="array",
                        description="List of tasks",
                    ),
                ),
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="completeness",
                    description="All requirements are met",
                ),
            ),
            exit_conditions=(
                ExitCondition(
                    name="all_criteria_met",
                    description="All acceptance criteria satisfied",
                    evaluation_criteria="100% criteria pass",
                ),
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        # Attempting to modify raises an error:
        seed.goal = "New goal"  # Raises ValidationError (frozen)
    """

    # Direction - IMMUTABLE. Extra fields are reserved for plugin-owned,
    # structured handoff data and must stay JSON/YAML serializable.
    model_config = {"extra": "allow"}

    goal: str = Field(..., min_length=1, description="Primary objective of the workflow")
    task_type: str = Field(
        default="code",
        description=(
            "Type of task execution: 'code', 'research', 'analysis', "
            "'artifact', 'document', 'documentation', or 'presentation'"
        ),
    )
    brownfield_context: BrownfieldContext = Field(
        default_factory=BrownfieldContext,
        description="Brownfield project context (empty for greenfield)",
    )
    constraints: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Hard constraints that must be satisfied",
    )
    acceptance_criteria: tuple[AcceptanceCriterionSpec, ...] = Field(
        default_factory=tuple,
        description="Specific criteria for success evaluation",
    )

    # Conceptual lens
    ontology_schema: OntologySchema = Field(
        ...,
        description="Ontology defining the workflow's conceptual lens",
    )

    # Evaluation
    evaluation_principles: tuple[EvaluationPrinciple, ...] = Field(
        default_factory=tuple,
        description="Principles for evaluating workflow outputs",
    )

    # Termination
    exit_conditions: tuple[ExitCondition, ...] = Field(
        default_factory=tuple,
        description="Conditions for terminating the workflow",
    )

    # Metadata
    metadata: SeedMetadata = Field(
        ...,
        description="Generation metadata (version, timestamp, etc.)",
    )

    @field_validator("evaluation_principles", mode="before")
    @classmethod
    def _coerce_string_evaluation_principles(cls, value: Any) -> Any:
        """Accept prose lists for hand-written seeds.

        Human-authored seed drafts often express evaluation principles as a
        simple YAML string list. Lift those entries into the documented object
        shape so manual ``ooo run`` users get a usable seed instead of an
        opaque Pydantic ``model_type`` error.
        """
        if isinstance(value, list | tuple):
            return tuple(
                {"name": f"principle_{index}", "description": item}
                if isinstance(item, str)
                else item
                for index, item in enumerate(value, start=1)
            )
        return value

    @field_validator("exit_conditions", mode="before")
    @classmethod
    def _coerce_string_exit_conditions(cls, value: Any) -> Any:
        """Accept prose lists for hand-written seed exit conditions."""
        if isinstance(value, list | tuple):
            return tuple(
                {
                    "name": f"condition_{index}",
                    "description": item,
                    "criteria": item,
                }
                if isinstance(item, str)
                else item
                for index, item in enumerate(value, start=1)
            )
        return value

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def _coerce_acceptance_criteria(cls, value: Any) -> Any:
        """Accept legacy string ACs and structured AC contracts."""
        if value is None:
            return ()
        if isinstance(value, list | tuple):
            normalized: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    normalized.append({"description": item})
                    continue
                if isinstance(item, dict) and "description" not in item:
                    if isinstance(item.get("criterion"), str):
                        normalized.append({**item, "description": item["criterion"]})
                        continue
                    if isinstance(item.get("content"), str):
                        normalized.append({**item, "description": item["content"]})
                        continue
                normalized.append(item)
            return tuple(normalized)
        return value

    @field_serializer("acceptance_criteria")
    def _serialize_acceptance_criteria(
        self,
        value: tuple[AcceptanceCriterionInput, ...],
    ) -> tuple[str | dict[str, Any], ...]:
        """Persist materialized AC contracts, retaining legacy fallback handling."""
        return tuple(
            criterion.to_seed_value()
            if isinstance(criterion, AcceptanceCriterionSpec)
            else ac_text(criterion)
            for criterion in value
        )

    @model_validator(mode="after")
    def _validate_plugin_extra_fields(self) -> Seed:
        """Keep plugin-owned extra fields safe for Seed persistence paths."""
        materialized_criteria = tuple(
            criterion.with_materialized_semantic_key()
            if isinstance(criterion, AcceptanceCriterionSpec)
            else AcceptanceCriterionSpec(
                description=ac_text(criterion),
                semantic_ac_key=derive_semantic_ac_key(criterion),
            )
            for criterion in self.acceptance_criteria
        )
        object.__setattr__(self, "acceptance_criteria", materialized_criteria)
        frozen_extra: dict[str, Any] = {}
        for key, value in (self.model_extra or {}).items():
            if not isinstance(key, str):
                raise ValueError("Seed extra field keys must be strings")
            if key == "conductor_directive":
                if not isinstance(value, dict):
                    raise ValueError("Seed conductor_directive must be a structured object")
                value = ConductorDirective.from_mapping(value).to_event_data()
            frozen_extra[key] = _freeze_seed_extra_value(value, path=key)
        object.__setattr__(self, "__pydantic_extra__", _FrozenDict(frozen_extra))
        return self

    def to_dict(self) -> dict[str, Any]:
        """Convert seed to dictionary for serialization.

        Returns:
            Dictionary representation of the seed.
        """
        extra = self.model_extra or {}
        data = self.model_dump(mode="json", by_alias=True, exclude=set(extra))
        data.update({key: _thaw_seed_extra_value(value) for key, value in extra.items()})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Seed:
        """Create seed from dictionary.

        Args:
            data: Dictionary representation of the seed.

        Returns:
            Seed instance.
        """
        return cls.model_validate(data)


def _freeze_seed_extra_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Seed extra field {path!r} must be a finite float")
        return value
    if isinstance(value, list):
        return tuple(
            _freeze_seed_extra_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, dict):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Seed extra field {path!r} dict keys must be strings")
            frozen[key] = _freeze_seed_extra_value(item, path=f"{path}.{key}")
        return _FrozenDict(frozen)
    raise ValueError(f"Seed extra field {path!r} must be JSON/YAML-serializable structured data")


def _thaw_seed_extra_value(value: Any) -> Any:
    if isinstance(value, _FrozenDict):
        return {key: _thaw_seed_extra_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_seed_extra_value(item) for item in value]
    return value


class _FrozenDict(dict[str, Any]):
    def __copy__(self) -> _FrozenDict:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenDict:
        return self

    def _readonly(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Seed extra fields are immutable")

    __setitem__ = _readonly
    __delitem__ = _readonly
    clear = _readonly
    pop = _readonly
    popitem = _readonly
    setdefault = _readonly
    update = _readonly
    __ior__ = _readonly
