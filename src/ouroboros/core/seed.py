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
import os
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
_WINDOWS_FORBIDDEN_COMPONENT_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_COMPONENT_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
)
_EXPECTED_ARTIFACT_DELIMITER_RE = re.compile(r",\s+")
_PORTABLE_PATH_COMPONENT_MAX_BYTES = 255
_POSIX_PORTABLE_PATH_MAX_BYTES = 256
_WINDOWS_CONSERVATIVE_PATH_MAX_UTF16_UNITS = 260
MAX_AC_SUCCESS_CONTRACT_ARTIFACTS = 253
# The capsule locator allows 2,048 characters including ``workspace:``.
# Keep that historical character ceiling as an independent serialization
# contract; filesystem materializability uses the stricter byte ceiling below.
MAX_AC_SUCCESS_CONTRACT_ARTIFACT_CHARS = 2_038
# POSIX guarantees PATH_MAX >= 256 including the terminating NUL. Limiting the
# canonical relative payload to 255 bytes makes every schema-valid artifact
# materializable as a standalone relative pathname on a conforming target;
# capsule/runtime checks below also account for the actual workspace prefix.
MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES = _POSIX_PORTABLE_PATH_MAX_BYTES - 1
MAX_AC_SUCCESS_CONTRACT_CHARS = 64_000


def _is_none_sentinel(value: str) -> bool:
    return value.strip(" ").upper() == "NONE"


def _windows_component_error(component: str) -> str | None:
    if component.endswith((" ", ".")):
        return "contains a Windows path component ending in a space or dot"
    if any(character in _WINDOWS_FORBIDDEN_COMPONENT_CHARS for character in component):
        return "contains a Windows-forbidden path character"
    stem = component.split(".", 1)[0].rstrip(" ").upper()
    if stem in _WINDOWS_RESERVED_COMPONENT_STEMS:
        return "contains a Windows-reserved path component"
    return None


def expected_artifact_path_error(artifact: str) -> str | None:
    """Return why an expected-artifact value is not a portable relative path."""

    if not artifact or any(ord(character) < 32 or ord(character) == 127 for character in artifact):
        return "contains an empty value or control character"
    if _is_none_sentinel(artifact):
        return "contains NONE mixed with artifact paths; use NONE only as the entire field"
    if "\\" in artifact:
        return "contains a backslash; use POSIX '/' separators"
    if "," in artifact:
        return "contains a comma; artifact list strings use comma+whitespace delimiters"
    posix_path = PurePosixPath(artifact)
    windows_path = PureWindowsPath(artifact)
    if not posix_path.parts or not windows_path.parts:
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
    for component in windows_path.parts:
        component_error = _windows_component_error(component)
        if component_error is not None:
            return component_error
    for component in posix_path.parts:
        try:
            encoded_component = component.encode("utf-8")
        except UnicodeEncodeError:
            return "contains text that cannot be encoded as UTF-8"
        if len(encoded_component) > _PORTABLE_PATH_COMPONENT_MAX_BYTES:
            return "contains a path component longer than 255 filesystem bytes"
    encoded_artifact = posix_path.as_posix().encode("utf-8")
    if len(encoded_artifact) > MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES:
        return (
            "contains a canonical path longer than "
            f"{MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES} portable filesystem bytes"
        )
    if any(character.isspace() for character in artifact):
        has_explicit_relative_structure = "/" in artifact or "\\" in artifact
        if not has_explicit_relative_structure:
            return "is ambiguous prose; prefix a top-level spaced path with ./"
    return None


def expected_artifact_workspace_path_error(
    artifact: str,
    workspace: str,
    *,
    platform: Literal["posix", "windows"] | None = None,
    path_capacity: int | None = None,
) -> str | None:
    """Return why an artifact cannot materialize under a concrete workspace.

    Schema validation owns the portable relative-path ceiling. Capsule and
    runtime callers additionally use this function to include the workspace
    prefix and the target platform's pathname capacity. Optional platform and
    capacity parameters make Windows/POSIX fail-closed behavior deterministic
    in tests without hard-coding the current machine.
    """
    path_error = expected_artifact_path_error(artifact)
    if path_error is not None:
        return path_error

    selected_platform = platform or ("windows" if os.name == "nt" else "posix")
    parts = PurePosixPath(artifact).parts
    if selected_platform == "windows":
        capacity = (
            path_capacity
            if path_capacity is not None
            else _WINDOWS_CONSERVATIVE_PATH_MAX_UTF16_UNITS
        )
        candidate = str(PureWindowsPath(workspace, *parts))
        path_units = len(candidate.encode("utf-16-le")) // 2 + 1
        if path_units > capacity:
            return f"workspace path exceeds Windows capacity ({capacity} UTF-16 units)"
        return None

    capacity = path_capacity
    if capacity is None:
        try:
            discovered = os.pathconf(workspace, "PC_PATH_MAX")
        except (AttributeError, OSError, ValueError):
            discovered = _POSIX_PORTABLE_PATH_MAX_BYTES
        capacity = discovered if discovered > 0 else _POSIX_PORTABLE_PATH_MAX_BYTES
    candidate = str(PurePosixPath(workspace, *parts))
    try:
        path_bytes = len(os.fsencode(candidate)) + 1
    except UnicodeEncodeError:
        return "workspace path cannot be encoded for the POSIX filesystem"
    if path_bytes > capacity:
        return f"workspace path exceeds POSIX capacity ({capacity} bytes)"
    return None


def parse_expected_artifact_list(value: str) -> tuple[str, ...]:
    """Parse a generated expected-artifacts list using the shared path grammar."""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("expected_artifacts entries cannot contain control characters")
    stripped = value.strip(" ")
    if not stripped or _is_none_sentinel(stripped):
        return ()
    artifacts = tuple(item.strip(" ") for item in _EXPECTED_ARTIFACT_DELIMITER_RE.split(value))
    if any(not artifact for artifact in artifacts):
        raise ValueError("expected_artifacts entries cannot be empty")
    return artifacts


def validate_ac_success_contract_values(
    *,
    verify_command: str | None,
    expected_artifacts: tuple[str, ...],
    output_assertion: str | None,
) -> None:
    """Enforce the execution capsule's complete success-contract budget.

    This validator lives at the Seed boundary so every schema-valid contract
    is guaranteed to be materializable by the downstream execution capsule.
    The capsule calls the same function for low-level and replay inputs rather
    than maintaining a second set of limits.
    """
    if verify_command is not None and not isinstance(verify_command, str):
        raise ValueError("success contract verify command is invalid")
    if output_assertion is not None and not isinstance(output_assertion, str):
        raise ValueError("success contract output assertion is invalid")
    try:
        artifact_count = len(expected_artifacts)
    except TypeError as exc:
        raise ValueError("success contract artifacts are invalid") from exc
    if artifact_count > MAX_AC_SUCCESS_CONTRACT_ARTIFACTS:
        raise ValueError(
            f"success contract artifact limit exceeded ({MAX_AC_SUCCESS_CONTRACT_ARTIFACTS})"
        )

    contract_chars = len(verify_command or "") + len(output_assertion or "")
    for path in expected_artifacts:
        try:
            encoded_path = (
                PurePosixPath(path).as_posix().encode("utf-8") if isinstance(path, str) else b""
            )
        except UnicodeEncodeError as exc:
            raise ValueError("success contract artifacts are invalid") from exc
        if (
            not isinstance(path, str)
            or not path
            or len(path) > MAX_AC_SUCCESS_CONTRACT_ARTIFACT_CHARS
            or len(encoded_path) > MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES
        ):
            raise ValueError("success contract artifacts are invalid")
        contract_chars += len(path)
    if contract_chars > MAX_AC_SUCCESS_CONTRACT_CHARS:
        raise ValueError(
            f"success contract character budget exceeded ({MAX_AC_SUCCESS_CONTRACT_CHARS})"
        )


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


_DEFAULT_GENERATION_MODE = "normal"
"""``SeedMetadata.generation_mode`` for the legacy ledger-complete path."""

_PARTIAL_SEED_GENERATION_MODE = "partial_seed_from_evidence"
"""``SeedMetadata.generation_mode`` for incomplete-ledger recovery Seeds.

Mirrors ``ouroboros.auto.ledger_seed.PARTIAL_SEED_GENERATION_MODE``; duplicated
here because ``ledger_seed`` imports this module. The two are pinned equal by a
regression test.
"""

_LEGACY_V104_VERSION = "1.0.4"
_LEGACY_V104_GENERATION_MODE = "revised_after_qa"
_LEGACY_V104_SEMANTIC_AC_KEY_RE = re.compile(r"^ac_[a-z][a-z0-9_]*$")
_CANONICAL_SEMANTIC_AC_KEY_RE = re.compile(r"^ac_[a-f0-9]{16}$")


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
    generation_mode: str = Field(default=_DEFAULT_GENERATION_MODE, min_length=1)
    degraded: bool = Field(default=False)
    unresolved_slots: tuple[str, ...] = Field(default_factory=tuple)
    recovery_reason: str | None = Field(default=None)
    decision_provenance: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_recovery_invariants(self) -> SeedMetadata:
        """Enforce the degraded-provenance invariants stated in the docstring.

        Without these checks a Seed synthesized on a recovery path could carry
        ``degraded`` unset and still pass the run gate, auto-RUNning a partial
        product — the exact outcome the field exists to prevent. Also freezes
        ``decision_provenance`` so the only mutable collection on this
        ``frozen=True`` model cannot be edited in place.
        """
        if self.degraded and self.generation_mode == _DEFAULT_GENERATION_MODE:
            raise ValueError(
                "degraded seeds must declare a non-default generation_mode "
                f"(got {_DEFAULT_GENERATION_MODE!r})"
            )
        if (
            self.degraded
            and self.generation_mode == _PARTIAL_SEED_GENERATION_MODE
            and not self.unresolved_slots
        ):
            raise ValueError(
                f"generation_mode {_PARTIAL_SEED_GENERATION_MODE!r} with degraded=True "
                "requires at least one unresolved_slots entry"
            )
        if self.unresolved_slots and not self.degraded:
            raise ValueError("unresolved_slots requires degraded=True")
        if not isinstance(self.decision_provenance, _FrozenDict):
            object.__setattr__(
                self,
                "decision_provenance",
                _FrozenDict(self.decision_provenance),
            )
        return self

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

BUILTIN_TASK_TYPES: frozenset[str] = frozenset(
    {
        "code",
        "research",
        "analysis",
        "artifact",
        "document",
        "documentation",
        "presentation",
    }
)
"""Built-in execution task types.

Typos against these values are rejected unconditionally.  Custom identifiers
registered via ``register_strategy()`` are also accepted so the Seed schema
stays compatible with the documented pluggable strategy contract.
"""

TaskType = str
"""Execution task type identifier.

Built-in values are the seven keys in :data:`BUILTIN_TASK_TYPES`.  Additional
identifiers are valid if and only if they have been registered via
``ouroboros.orchestrator.execution_strategy.register_strategy`` before Seed
construction.
"""


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
    execution verifier resolves every entry literally and requires it to exist.
    """

    description: str = Field(..., min_length=1)
    semantic_ac_key: str | None = Field(default=None, pattern=r"^ac_[a-f0-9]{16}$")
    verify_command: str | None = Field(default=None)
    expected_artifacts: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Exact file or directory paths relative to the run workspace; "
            "each path is resolved literally and must exist; each component "
            "is at most 255 UTF-8 bytes and the complete canonical path at most 255; "
            "prefix top-level paths containing spaces with ./"
        ),
    )
    output_assertion: str | None = Field(
        default=None,
        description="Literal string required in verify_command combined stdout and stderr",
    )
    verify_exemption_reason: str | None = Field(
        default=None,
        description=(
            "Why this criterion cannot carry a verify_command. Required by the "
            "seed verify-command gate for criteria the orchestrator cannot "
            "judge deterministically; it is a per-AC, explicit escape hatch, "
            "never a blanket opt-out"
        ),
    )
    investment: InvestmentSpec | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_success_contract(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_assertion = data.get("output_assertion")
        if (
            not isinstance(raw_assertion, str)
            or not raw_assertion.strip(" ")
            or _is_none_sentinel(raw_assertion)
        ):
            return data
        raw_verify = data.get("verify_command")
        if (
            not isinstance(raw_verify, str)
            or not raw_verify.strip(" ")
            or _is_none_sentinel(raw_verify)
        ):
            raise ValueError("output_assertion requires verify_command")
        return data

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: Any) -> Any:
        """Normalize and reject blank criterion text.

        ``description`` is the only input to :func:`derive_semantic_ac_key` for
        legacy string ACs and the dominant term for structured ones, so a
        whitespace-only value would collapse every such criterion onto one
        identical ``semantic_ac_key`` and cross-contaminate AC-keyed retry and
        successor state. Mirrors ``Seed.goal``'s ``min_length=1`` while
        producing a message that names the field.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError(
                    "acceptance criterion description cannot be empty or whitespace-only"
                )
            return stripped
        return value

    @field_validator("verify_exemption_reason", mode="before")
    @classmethod
    def _strip_verify_exemption_reason(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or _is_none_sentinel(stripped):
                return None
            return stripped
        return value

    @field_validator("verify_command", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or _is_none_sentinel(stripped):
                return None
            return stripped
        return value

    @field_validator("output_assertion", mode="before")
    @classmethod
    def _normalize_output_assertion(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or _is_none_sentinel(stripped):
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
            return parse_expected_artifact_list(value)
        if isinstance(value, list | tuple | set):
            if any(not isinstance(item, str) for item in value):
                raise ValueError("expected_artifacts entries must be strings")
            artifacts = tuple(value)
            if any(
                not artifact
                or any(ord(character) < 32 or ord(character) == 127 for character in artifact)
                for artifact in artifacts
            ):
                raise ValueError(
                    "expected_artifacts entries cannot be empty or contain control characters"
                )
            return artifacts
        return value

    @model_validator(mode="after")
    def _validate_success_contract(self) -> AcceptanceCriterionSpec:
        if self.verify_command and self.verify_exemption_reason:
            raise ValueError("verify_exemption_reason is mutually exclusive with verify_command")
        if self.output_assertion and not self.verify_command:
            raise ValueError("output_assertion requires verify_command")
        invalid_artifacts = tuple(
            (artifact, error)
            for artifact in self.expected_artifacts
            if (error := expected_artifact_path_error(artifact)) is not None
        )
        if invalid_artifacts:
            rendered = ", ".join(f"{artifact!r} ({error})" for artifact, error in invalid_artifacts)
            raise ValueError(
                f"expected_artifacts entries must be portable workspace-relative paths: {rendered}"
            )
        validate_ac_success_contract_values(
            verify_command=self.verify_command,
            expected_artifacts=self.expected_artifacts,
            output_assertion=self.output_assertion,
        )
        return self

    @property
    def has_success_contract(self) -> bool:
        """Return whether the criterion declares deterministic verification fields.

        A contract adds evidence but never removes transcript obligations or
        overrides the worker execution outcome.
        """
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
            and self.verify_exemption_reason is None
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
        if self.verify_exemption_reason:
            data["verify_exemption_reason"] = self.verify_exemption_reason
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


def _migrate_legacy_v104_seed_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate the pre-hash v1.0.4 parser shape into the strict Seed schema.

    The v1.0.4 Seed generator emitted human-readable ``semantic_ac_key``
    labels and omitted ontology-field descriptions.  The current schema
    intentionally rejects both shapes, so migration is limited to that exact
    version/provenance envelope and then delegates all values to the existing
    strict Pydantic validation.  Invalid labels, blank values, and malformed
    fields are not repaired.
    """
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return data
    if (
        metadata.get("version") != _LEGACY_V104_VERSION
        or metadata.get("generation_mode") != _LEGACY_V104_GENERATION_MODE
    ):
        return data

    normalized = dict(data)

    criteria = data.get("acceptance_criteria")
    if isinstance(criteria, list | tuple):
        normalized_criteria: list[Any] = []
        criteria_changed = False
        for item in criteria:
            if isinstance(item, dict):
                semantic_ac_key = item.get("semantic_ac_key")
                if (
                    isinstance(semantic_ac_key, str)
                    and _LEGACY_V104_SEMANTIC_AC_KEY_RE.fullmatch(semantic_ac_key)
                    and not _CANONICAL_SEMANTIC_AC_KEY_RE.fullmatch(semantic_ac_key)
                ):
                    item = {**item, "semantic_ac_key": None}
                    criteria_changed = True
            normalized_criteria.append(item)
        if criteria_changed:
            normalized["acceptance_criteria"] = (
                tuple(normalized_criteria) if isinstance(criteria, tuple) else normalized_criteria
            )

    ontology_schema = data.get("ontology_schema")
    if isinstance(ontology_schema, dict):
        fields = ontology_schema.get("fields")
        if isinstance(fields, list | tuple):
            normalized_fields: list[Any] = []
            fields_changed = False
            for item in fields:
                if (
                    isinstance(item, dict)
                    and "description" not in item
                    and isinstance(item.get("name"), str)
                    and bool(item["name"].strip())
                ):
                    item = {**item, "description": item["name"]}
                    fields_changed = True
                normalized_fields.append(item)
            if fields_changed:
                normalized_schema = dict(ontology_schema)
                normalized_schema["fields"] = (
                    tuple(normalized_fields) if isinstance(fields, tuple) else normalized_fields
                )
                normalized["ontology_schema"] = normalized_schema

    return normalized


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
    task_type: TaskType = Field(
        default="code",
        description=(
            "Execution strategy identifier. Built-in types: 'code', 'research', "
            "'analysis', 'artifact', 'document', 'documentation', 'presentation'. "
            "Custom identifiers are accepted if registered via register_strategy()."
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

    @field_validator("task_type", mode="before")
    @classmethod
    def _normalize_task_type(cls, value: Any) -> Any:
        """Normalize and validate task_type against the strategy registry.

        Folds case and surrounding whitespace for hand-authored seeds, then
        rejects identifiers that are neither a built-in task type nor a
        dynamically registered custom strategy.  This preserves typo rejection
        for the seven built-in types while honouring the documented
        ``register_strategy()`` extension contract.
        """
        if not isinstance(value, str):
            return value
        # Late import to avoid circular dependency (core -> orchestrator).
        from ouroboros.orchestrator.execution_strategy import (
            _canonicalize_task_type,
            is_registered_task_type,
        )

        normalized = _canonicalize_task_type(value)
        if not normalized:
            msg = "task_type must be a non-empty string"
            raise ValueError(msg)
        if normalized in BUILTIN_TASK_TYPES:
            return normalized

        if is_registered_task_type(normalized):
            return normalized
        valid = ", ".join(sorted(BUILTIN_TASK_TYPES))
        msg = (
            f"Unknown task_type: {value!r}. "
            f"Built-in types: {valid}. "
            f"Custom types must be registered via register_strategy() "
            f"before Seed construction."
        )
        raise ValueError(msg)

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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_v104_seed_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return _migrate_legacy_v104_seed_dict(value)
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
