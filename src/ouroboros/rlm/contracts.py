"""Structured contracts for RLM Hermes sub-call payloads.

The RLM layer treats Hermes as a bounded inner language model. Hermes returns
JSON-compatible objects; Ouroboros validates and serializes those objects before
they can influence AC tree state or trace replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Literal

RLM_HERMES_OUTPUT_SCHEMA_VERSION = "rlm.hermes.output.v1"
RLM_HERMES_DECOMPOSE_AC_MODE = "decompose_ac"
RLM_HERMES_EXECUTE_ATOMIC_MODE = "execute_atomic"
RLM_HERMES_SYNTHESIZE_PARENT_MODE = "synthesize_parent"
RLM_DECOMPOSITION_ARTIFACT_TYPE = "decomposition"
RLM_MIN_DECOMPOSITION_CHILDREN = 2
RLM_MAX_DECOMPOSITION_CHILDREN = 5
RLM_HERMES_DECOMPOSITION_VERDICTS = frozenset(
    {"atomic", "decomposed", "failed", "partial", "retryable"}
)
RLM_HERMES_NEXT_MODES = frozenset(
    {"decompose_ac", "execute_atomic", "summarize_chunk", "synthesize_parent", "none"}
)


class RLMHermesContractError(ValueError):
    """Raised when a Hermes RLM response violates the structured contract."""


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"{field_name} must be an object"
        raise RLMHermesContractError(msg)
    return value


def _require_str(data: Mapping[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} must be a non-empty string"
        raise RLMHermesContractError(msg)
    return value


def _optional_str(data: Mapping[str, Any], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{field_name} must be a string when present"
        raise RLMHermesContractError(msg)
    return value


def _require_sequence(value: object, field_name: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        msg = f"{field_name} must be an array"
        raise RLMHermesContractError(msg)
    return value


def _tuple_of_strings(value: object, field_name: str, *, min_items: int = 0) -> tuple[str, ...]:
    items = _require_sequence(value, field_name)
    strings: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            msg = f"{field_name}[{index}] must be a non-empty string"
            raise RLMHermesContractError(msg)
        strings.append(item)
    if len(strings) < min_items:
        msg = f"{field_name} must contain at least {min_items} item(s)"
        raise RLMHermesContractError(msg)
    return tuple(strings)


def _tuple_of_ints(value: object, field_name: str) -> tuple[int, ...]:
    items = _require_sequence(value, field_name)
    ints: list[int] = []
    for index, item in enumerate(items):
        if isinstance(item, bool) or not isinstance(item, int):
            msg = f"{field_name}[{index}] must be an integer"
            raise RLMHermesContractError(msg)
        ints.append(item)
    return tuple(ints)


@dataclass(frozen=True, slots=True)
class RLMHermesEvidenceReference:
    """Evidence citation emitted by Hermes for one supplied context item."""

    chunk_id: str
    claim: str
    source_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON object shape stored in traces."""
        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "claim": self.claim,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesEvidenceReference:
        """Deserialize and validate one evidence reference."""
        start_line = data.get("start_line")
        end_line = data.get("end_line")
        for field_name, value in (("start_line", start_line), ("end_line", end_line)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                msg = f"{field_name} must be an integer or null"
                raise RLMHermesContractError(msg)

        return cls(
            chunk_id=_require_str(data, "chunk_id"),
            source_path=_optional_str(data, "source_path"),
            start_line=start_line,
            end_line=end_line,
            claim=_require_str(data, "claim"),
        )


@dataclass(frozen=True, slots=True)
class RLMHermesResidualGap:
    """Residual uncertainty or blocker reported by Hermes."""

    gap: str
    impact: str
    suggested_next_step: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to the JSON object shape stored in traces."""
        return {
            "gap": self.gap,
            "impact": self.impact,
            "suggested_next_step": self.suggested_next_step,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesResidualGap:
        """Deserialize and validate one residual gap."""
        return cls(
            gap=_require_str(data, "gap"),
            impact=_require_str(data, "impact"),
            suggested_next_step=_require_str(data, "suggested_next_step"),
        )


@dataclass(frozen=True, slots=True)
class RLMHermesACSubQuestion:
    """One child AC proposal returned by Hermes during decomposition."""

    title: str
    statement: str
    success_criteria: tuple[str, ...]
    rationale: str
    depends_on: tuple[int, ...] = field(default_factory=tuple)
    estimated_chunk_needs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON object shape stored in traces."""
        return {
            "title": self.title,
            "statement": self.statement,
            "success_criteria": list(self.success_criteria),
            "rationale": self.rationale,
            "depends_on": list(self.depends_on),
            "estimated_chunk_needs": list(self.estimated_chunk_needs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesACSubQuestion:
        """Deserialize and validate one child AC proposal."""
        return cls(
            title=_require_str(data, "title"),
            statement=_require_str(data, "statement"),
            success_criteria=_tuple_of_strings(
                data.get("success_criteria", []),
                "success_criteria",
                min_items=1,
            ),
            rationale=_require_str(data, "rationale"),
            depends_on=_tuple_of_ints(data.get("depends_on", []), "depends_on"),
            estimated_chunk_needs=_tuple_of_strings(
                data.get("estimated_chunk_needs", []),
                "estimated_chunk_needs",
            ),
        )


@dataclass(frozen=True, slots=True)
class RLMHermesACDecompositionArtifact:
    """Mode-specific artifact for a ``decompose_ac`` Hermes response."""

    is_atomic: bool
    atomic_rationale: str | None = None
    proposed_child_acs: tuple[RLMHermesACSubQuestion, ...] = field(default_factory=tuple)
    artifact_type: Literal["decomposition"] = RLM_DECOMPOSITION_ARTIFACT_TYPE

    def __post_init__(self) -> None:
        if self.artifact_type != RLM_DECOMPOSITION_ARTIFACT_TYPE:
            msg = "decomposition artifact_type must be 'decomposition'"
            raise RLMHermesContractError(msg)
        if self.is_atomic:
            if not self.atomic_rationale or not self.atomic_rationale.strip():
                msg = "atomic decomposition artifacts require atomic_rationale"
                raise RLMHermesContractError(msg)
            if self.proposed_child_acs:
                msg = "atomic decomposition artifacts must not include proposed_child_acs"
                raise RLMHermesContractError(msg)
            return

        child_count = len(self.proposed_child_acs)
        if child_count > RLM_MAX_DECOMPOSITION_CHILDREN:
            msg = (
                "non-atomic decomposition artifacts allow at most "
                f"{RLM_MAX_DECOMPOSITION_CHILDREN} proposed_child_acs"
            )
            raise RLMHermesContractError(msg)
        for child_index, child in enumerate(self.proposed_child_acs):
            for dependency in child.depends_on:
                if dependency < 0 or dependency >= child_index:
                    msg = (
                        "depends_on entries must reference prior sibling indices; "
                        f"child {child_index} has invalid dependency {dependency}"
                    )
                    raise RLMHermesContractError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON object shape stored in traces."""
        return {
            "artifact_type": self.artifact_type,
            "is_atomic": self.is_atomic,
            "atomic_rationale": self.atomic_rationale,
            "proposed_child_acs": [child.to_dict() for child in self.proposed_child_acs],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesACDecompositionArtifact:
        """Deserialize and validate the decomposition artifact."""
        artifact_type = data.get("artifact_type")
        if artifact_type != RLM_DECOMPOSITION_ARTIFACT_TYPE:
            msg = "artifacts must contain exactly one decomposition artifact"
            raise RLMHermesContractError(msg)

        is_atomic = data.get("is_atomic")
        if not isinstance(is_atomic, bool):
            msg = "is_atomic must be a boolean"
            raise RLMHermesContractError(msg)

        proposed_items = _require_sequence(
            data.get("proposed_child_acs", []),
            "proposed_child_acs",
        )
        proposed_child_acs = tuple(
            RLMHermesACSubQuestion.from_dict(_require_mapping(item, "proposed_child_acs[]"))
            for item in proposed_items
        )

        return cls(
            is_atomic=is_atomic,
            atomic_rationale=_optional_str(data, "atomic_rationale"),
            proposed_child_acs=proposed_child_acs,
        )


@dataclass(frozen=True, slots=True)
class RLMHermesControl:
    """Advisory local control fields emitted by Hermes."""

    requires_retry: bool = False
    suggested_next_mode: str = "none"
    must_not_recurse: bool = False

    def __post_init__(self) -> None:
        if self.suggested_next_mode not in RLM_HERMES_NEXT_MODES:
            msg = f"suggested_next_mode must be one of {sorted(RLM_HERMES_NEXT_MODES)}"
            raise RLMHermesContractError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON object shape stored in traces."""
        return {
            "requires_retry": self.requires_retry,
            "suggested_next_mode": self.suggested_next_mode,
            "must_not_recurse": self.must_not_recurse,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesControl:
        """Deserialize and validate local control fields."""
        requires_retry = data.get("requires_retry", False)
        must_not_recurse = data.get("must_not_recurse", False)
        if not isinstance(requires_retry, bool):
            msg = "requires_retry must be a boolean"
            raise RLMHermesContractError(msg)
        if not isinstance(must_not_recurse, bool):
            msg = "must_not_recurse must be a boolean"
            raise RLMHermesContractError(msg)
        return cls(
            requires_retry=requires_retry,
            suggested_next_mode=_optional_str(data, "suggested_next_mode") or "none",
            must_not_recurse=must_not_recurse,
        )


@dataclass(frozen=True, slots=True)
class RLMHermesACDecompositionResult:
    """Validated Hermes result for an AC decomposition sub-question."""

    rlm_node_id: str
    ac_node_id: str
    verdict: str
    confidence: float
    result: dict[str, Any]
    artifact: RLMHermesACDecompositionArtifact
    evidence_references: tuple[RLMHermesEvidenceReference, ...] = field(default_factory=tuple)
    residual_gaps: tuple[RLMHermesResidualGap, ...] = field(default_factory=tuple)
    control: RLMHermesControl = field(default_factory=RLMHermesControl)
    schema_version: Literal["rlm.hermes.output.v1"] = RLM_HERMES_OUTPUT_SCHEMA_VERSION
    mode: Literal["decompose_ac"] = RLM_HERMES_DECOMPOSE_AC_MODE

    def __post_init__(self) -> None:
        if self.schema_version != RLM_HERMES_OUTPUT_SCHEMA_VERSION:
            msg = f"schema_version must be {RLM_HERMES_OUTPUT_SCHEMA_VERSION}"
            raise RLMHermesContractError(msg)
        if self.mode != RLM_HERMES_DECOMPOSE_AC_MODE:
            msg = f"mode must be {RLM_HERMES_DECOMPOSE_AC_MODE}"
            raise RLMHermesContractError(msg)
        if self.verdict not in RLM_HERMES_DECOMPOSITION_VERDICTS:
            msg = f"verdict must be one of {sorted(RLM_HERMES_DECOMPOSITION_VERDICTS)}"
            raise RLMHermesContractError(msg)
        if not 0.0 <= self.confidence <= 1.0:
            msg = "confidence must be between 0.0 and 1.0"
            raise RLMHermesContractError(msg)
        if self.artifact.is_atomic and self.verdict != "atomic":
            msg = "atomic decomposition artifacts require verdict 'atomic'"
            raise RLMHermesContractError(msg)
        if not self.artifact.is_atomic and self.verdict == "atomic":
            msg = "non-atomic decomposition artifacts cannot use verdict 'atomic'"
            raise RLMHermesContractError(msg)
        if self.verdict == "decomposed":
            child_count = len(self.artifact.proposed_child_acs)
            if child_count < RLM_MIN_DECOMPOSITION_CHILDREN:
                msg = (
                    "decomposed Hermes results require at least "
                    f"{RLM_MIN_DECOMPOSITION_CHILDREN} proposed_child_acs"
                )
                raise RLMHermesContractError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the exact JSON object persisted in RLM traces."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "rlm_node_id": self.rlm_node_id,
            "ac_node_id": self.ac_node_id,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "result": self.result,
            "evidence_references": [reference.to_dict() for reference in self.evidence_references],
            "residual_gaps": [gap.to_dict() for gap in self.residual_gaps],
            "artifacts": [self.artifact.to_dict()],
            "control": self.control.to_dict(),
        }

    def to_json(self) -> str:
        """Serialize deterministically for trace storage and prompt examples."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(
        cls,
        payload: str,
        *,
        expected_rlm_node_id: str | None = None,
        expected_ac_node_id: str | None = None,
    ) -> RLMHermesACDecompositionResult:
        """Deserialize a Hermes JSON string and validate optional active IDs."""
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = f"Hermes decomposition output is not valid JSON: {exc.msg}"
            raise RLMHermesContractError(msg) from exc
        return cls.from_dict(
            _require_mapping(raw, "payload"),
            expected_rlm_node_id=expected_rlm_node_id,
            expected_ac_node_id=expected_ac_node_id,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        expected_rlm_node_id: str | None = None,
        expected_ac_node_id: str | None = None,
    ) -> RLMHermesACDecompositionResult:
        """Deserialize and validate the AC decomposition output contract."""
        schema_version = _require_str(data, "schema_version")
        mode = _require_str(data, "mode")
        rlm_node_id = _require_str(data, "rlm_node_id")
        ac_node_id = _require_str(data, "ac_node_id")
        verdict = _require_str(data, "verdict")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            msg = "confidence must be a number"
            raise RLMHermesContractError(msg)

        if expected_rlm_node_id is not None and rlm_node_id != expected_rlm_node_id:
            msg = f"rlm_node_id mismatch: expected {expected_rlm_node_id}, got {rlm_node_id}"
            raise RLMHermesContractError(msg)
        if expected_ac_node_id is not None and ac_node_id != expected_ac_node_id:
            msg = f"ac_node_id mismatch: expected {expected_ac_node_id}, got {ac_node_id}"
            raise RLMHermesContractError(msg)

        result = _require_mapping(data.get("result"), "result")
        evidence_items = _require_sequence(
            data.get("evidence_references", []),
            "evidence_references",
        )
        residual_items = _require_sequence(data.get("residual_gaps", []), "residual_gaps")
        artifacts = _require_sequence(data.get("artifacts"), "artifacts")
        if len(artifacts) != 1:
            msg = "artifacts must contain exactly one decomposition artifact"
            raise RLMHermesContractError(msg)

        control_raw = data.get("control", {})
        control = RLMHermesControl.from_dict(_require_mapping(control_raw, "control"))

        return cls(
            schema_version=schema_version,
            mode=mode,
            rlm_node_id=rlm_node_id,
            ac_node_id=ac_node_id,
            verdict=verdict,
            confidence=float(confidence),
            result=dict(result),
            evidence_references=tuple(
                RLMHermesEvidenceReference.from_dict(
                    _require_mapping(item, "evidence_references[]")
                )
                for item in evidence_items
            ),
            residual_gaps=tuple(
                RLMHermesResidualGap.from_dict(_require_mapping(item, "residual_gaps[]"))
                for item in residual_items
            ),
            artifact=RLMHermesACDecompositionArtifact.from_dict(
                _require_mapping(artifacts[0], "artifacts[0]")
            ),
            control=control,
        )
