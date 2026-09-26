"""Typed, auditable input for future deterministic ambiguity calculation.

The model records only evidence classifications. Numeric ambiguity remains a
code-owned concern so a model response cannot assert a final readiness score.
"""

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any

from ouroboros.core.json_utils import extract_json_payload


class AmbiguityEvidenceStatus(StrEnum):
    """How well one requirement field is supported by its cited evidence."""

    CONFIRMED = "confirmed"
    MISSING = "missing"
    INFERRED = "inferred"
    CONFLICTING = "conflicting"


class AmbiguityEvidenceSourceKind(StrEnum):
    """Allowed provenance classes for a source quote."""

    INITIAL_CONTEXT = "initial_context"
    INTERVIEW_ANSWER = "interview_answer"
    REPOSITORY_EVIDENCE = "repository_evidence"
    MAINTAINER_POLICY = "maintainer_policy"


def _require_exact_keys(data: object, *, allowed: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    return data


def _required_non_blank_string(data: dict[str, Any], key: str, *, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires a non-blank {key}")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """A verbatim quote and the source class that supplied it."""

    kind: AmbiguityEvidenceSourceKind
    quote: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", AmbiguityEvidenceSourceKind(self.kind))
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise ValueError("source quote must not be blank")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "quote": self.quote}

    @classmethod
    def from_dict(cls, data: object) -> "EvidenceSource":
        item = _require_exact_keys(data, allowed=frozenset({"kind", "quote"}), label="source")
        try:
            kind = AmbiguityEvidenceSourceKind(item.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid source kind: {item.get('kind')!r}") from exc
        return cls(kind=kind, quote=_required_non_blank_string(item, "quote", label="source"))


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    """Contradictory provenance for an evidence entry."""

    source: EvidenceSource
    explanation: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise ValueError("conflict source must be an EvidenceSource")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("conflict explanation must not be blank")

    def to_dict(self) -> dict[str, object]:
        return {"source": self.source.to_dict(), "explanation": self.explanation}

    @classmethod
    def from_dict(cls, data: object) -> "EvidenceConflict":
        item = _require_exact_keys(
            data,
            allowed=frozenset({"source", "explanation"}),
            label="conflict",
        )
        return cls(
            source=EvidenceSource.from_dict(item.get("source")),
            explanation=_required_non_blank_string(item, "explanation", label="conflict"),
        )


@dataclass(frozen=True, slots=True)
class AmbiguityEvidenceEntry:
    """One typed field assessment, with provenance required when present."""

    dimension: str
    field: str
    status: AmbiguityEvidenceStatus
    source: EvidenceSource | None = None
    conflict: EvidenceConflict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, str) or not self.dimension.strip():
            raise ValueError("dimension must not be blank")
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("field must not be blank")
        object.__setattr__(self, "dimension", self.dimension.strip())
        object.__setattr__(self, "field", self.field.strip())
        object.__setattr__(self, "status", AmbiguityEvidenceStatus(self.status))

        if self.status is AmbiguityEvidenceStatus.MISSING:
            if self.source is not None or self.conflict is not None:
                raise ValueError("missing evidence must not claim provenance or conflict")
            return

        if not isinstance(self.source, EvidenceSource):
            raise ValueError(f"{self.status.value} evidence requires a source quote")
        if self.status is AmbiguityEvidenceStatus.CONFLICTING:
            if not isinstance(self.conflict, EvidenceConflict):
                raise ValueError("conflicting evidence requires a conflict")
            if self.conflict.source == self.source:
                raise ValueError("conflict source must differ from the primary source")
        elif self.conflict is not None:
            raise ValueError("only conflicting evidence may include a conflict")

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "field": self.field,
            "status": self.status.value,
            "source": self.source.to_dict() if self.source is not None else None,
            "conflict": self.conflict.to_dict() if self.conflict is not None else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> "AmbiguityEvidenceEntry":
        item = _require_exact_keys(
            data,
            allowed=frozenset({"dimension", "field", "status", "source", "conflict"}),
            label="evidence entry",
        )
        try:
            status = AmbiguityEvidenceStatus(item.get("status"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid evidence status: {item.get('status')!r}") from exc
        source_data = item.get("source")
        conflict_data = item.get("conflict")
        return cls(
            dimension=_required_non_blank_string(item, "dimension", label="evidence entry"),
            field=_required_non_blank_string(item, "field", label="evidence entry"),
            status=status,
            source=EvidenceSource.from_dict(source_data) if source_data is not None else None,
            conflict=EvidenceConflict.from_dict(conflict_data)
            if conflict_data is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class AmbiguityEvidenceLedger:
    """Canonical ambiguity evidence ordered independently of model output order."""

    entries: tuple[AmbiguityEvidenceEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if not entries:
            raise ValueError("ambiguity evidence ledger requires at least one entry")
        if not all(isinstance(entry, AmbiguityEvidenceEntry) for entry in entries):
            raise ValueError("ledger entries must be AmbiguityEvidenceEntry values")
        ordered = tuple(sorted(entries, key=lambda entry: (entry.dimension, entry.field)))
        keys = [(entry.dimension, entry.field) for entry in ordered]
        if len(set(keys)) != len(keys):
            raise ValueError("ledger entries must have unique dimension and field pairs")
        object.__setattr__(self, "entries", ordered)

    def canonical_json(self) -> str:
        """Return a stable JSON representation for persistence and future scoring."""
        return json.dumps(
            {"entries": [entry.to_dict() for entry in self.entries]},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, data: object) -> "AmbiguityEvidenceLedger":
        item = _require_exact_keys(data, allowed=frozenset({"entries"}), label="ledger")
        entries_data = item.get("entries")
        if not isinstance(entries_data, list):
            raise ValueError("ledger entries must be a list")
        return cls(entries=tuple(AmbiguityEvidenceEntry.from_dict(entry) for entry in entries_data))


def parse_ambiguity_evidence_ledger(response: str) -> AmbiguityEvidenceLedger:
    """Parse a model response without accepting a model-supplied numeric score."""
    text = extract_json_payload(response.strip())
    if text is None:
        raise ValueError("Invalid ambiguity evidence ledger: no unambiguous JSON payload")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ambiguity evidence ledger JSON: {exc}") from exc

    try:
        return AmbiguityEvidenceLedger.from_dict(payload)
    except ValueError as exc:
        raise ValueError(f"Invalid ambiguity evidence ledger: {exc}") from exc
