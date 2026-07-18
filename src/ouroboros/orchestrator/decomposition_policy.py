"""Policy model for decomposition decisions.

This module is intentionally self-contained: it defines the persistence
contract for future decomposition wiring without importing the existing
failure taxonomy. Bounce causes describe why decomposition was considered;
they are not leaf execution failures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any

SCHEMA_VERSION = 1

MIN_CHILDREN = 2
MAX_CHILDREN = 5
MAX_REASON_COUNT = 8
MAX_EVIDENCE_REF_COUNT = 8
MAX_REASON_CHARS = 240
MAX_EVIDENCE_REF_CHARS = 160
MAX_CHILD_DESCRIPTION_CHARS = 500
MAX_COVERAGE_CLAIMS = 8
MAX_COVERAGE_CLAIM_CHARS = 240
# A child's ``expected_artifacts`` is a slice of the PARENT's own seed-authored
# ``expected_artifacts``. The union across at most MAX_CHILDREN children must
# equal the parent's full set, so a single child may legitimately need to carry
# most of an artifact-rich parent's contract -- bound generously, but bounded.
MAX_CHILD_EXPECTED_ARTIFACTS = 32
MAX_EXPECTED_ARTIFACT_CHARS = 500
MAX_VERIFICATION_HINT_CHARS = 300
MAX_RATIONALE_CHARS = 1_000
MAX_PROPOSAL_REPR_CHARS = 10_000
MAX_TRACE_SUMMARY_CHARS = 1_000

_SECRET_VALUE = "[REDACTED]"
_SECRET_KEY_PATTERN = (
    r"api[_-]?key|access[_-]?token|auth[_-]?token|authorization|"
    r"client[_-]?secret|password|passwd|secret|token"
)
_SECRET_QUOTED_VALUE_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?(?:{_SECRET_KEY_PATTERN})[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_SECRET_UNQUOTED_VALUE_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?(?:{_SECRET_KEY_PATTERN})[\"']?\s*[:=]\s*)"
    r"(?P<value>[^\s,;}\]\"']+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_WHITESPACE_RE = re.compile(r"\s+")
_FINGERPRINT_RE = re.compile(r"[^a-z0-9]+")


class DecompositionSource(StrEnum):
    """Where the decomposition decision originated."""

    PREFLIGHT = "preflight"
    BOUNCE = "bounce"


class DecompositionDisposition(StrEnum):
    """High-level decomposition outcome."""

    ATOMIC = "ATOMIC"
    SPLIT = "SPLIT"
    UNKNOWN = "UNKNOWN"
    ESCALATED = "ESCALATED"


class StructuralCheckStatus(StrEnum):
    """Syntactic/structural validation status for a proposed split."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class SemanticAttestationStatus(StrEnum):
    """Semantic attestation status for parent coverage and sibling separation."""

    ESTABLISHED = "ESTABLISHED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    NOT_RUN = "NOT_RUN"


class BounceCause(StrEnum):
    """Reason a bounce path considered decomposition.

    This enum is deliberately separate from FailureClass. It describes a
    decomposition trigger, not the verifier's classification of a failed leaf.
    """

    TOO_BIG = "TOO_BIG"
    BAD_SPEC = "BAD_SPEC"
    ENVIRONMENT = "ENVIRONMENT"
    MODEL = "MODEL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DecompositionTraceSummary:
    """Bounded, redacted trace context for logging a decomposition decision."""

    summary: str
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str):
            msg = "summary must be a string"
            raise ValueError(msg)
        if not isinstance(self.evidence_refs, Sequence) or isinstance(
            self.evidence_refs, str | bytes
        ):
            msg = "evidence_refs must be a sequence of strings"
            raise ValueError(msg)
        object.__setattr__(
            self,
            "summary",
            redact_and_truncate_text(self.summary, max_chars=MAX_TRACE_SUMMARY_CHARS),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _bounded_trace_strings(
                self.evidence_refs[:MAX_EVIDENCE_REF_COUNT],
                field_name="evidence_refs",
                max_count=MAX_EVIDENCE_REF_COUNT,
                max_chars=MAX_EVIDENCE_REF_CHARS,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"summary": self.summary, "evidence_refs": list(self.evidence_refs)}

    @classmethod
    def from_dict(cls, data: object) -> DecompositionTraceSummary | None:
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(
                summary=data["summary"],
                evidence_refs=_read_string_list(data.get("evidence_refs", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class DecompositionChild:
    """One child task in a structured decomposition proposal.

    ``expected_artifacts`` is this child's assigned slice of the PARENT AC's
    own seed-authored ``expected_artifacts``. It is the child's per-child-local
    success contract: the orchestrator checks exactly these paths (with a
    pre-dispatch existence snapshot) to produce genuine per-sibling verify
    evidence for the gate-anchored decomposition attestation. Structural
    validation (:func:`validate_decomposition_proposal`) rejects any entry that
    is not an exact member of the parent's declared set, so the decomposer can
    never invent its own oracle. Empty for parents that declare no artifacts
    (and for all pre-existing serialized proposals -- fully backward
    compatible).
    """

    description: str
    coverage_claims: tuple[str, ...] = field(default_factory=tuple)
    verification_hint: str = ""
    expected_artifacts: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.description, str):
            msg = "description must be a string"
            raise ValueError(msg)
        if not isinstance(self.verification_hint, str):
            msg = "verification_hint must be a string"
            raise ValueError(msg)
        description = _bounded_nonblank_text(
            self.description,
            field_name="description",
            max_chars=MAX_CHILD_DESCRIPTION_CHARS,
        )
        verification_hint = _bounded_text(
            self.verification_hint,
            field_name="verification_hint",
            max_chars=MAX_VERIFICATION_HINT_CHARS,
        )
        claims = _bounded_strings(
            self.coverage_claims,
            field_name="coverage_claims",
            max_count=MAX_COVERAGE_CLAIMS,
            max_chars=MAX_COVERAGE_CLAIM_CHARS,
            require_nonblank=True,
        )
        if _has_duplicate_fingerprints(claims):
            msg = "coverage_claims must not contain duplicates"
            raise ValueError(msg)
        artifacts = _bounded_strings(
            self.expected_artifacts,
            field_name="expected_artifacts",
            max_count=MAX_CHILD_EXPECTED_ARTIFACTS,
            max_chars=MAX_EXPECTED_ARTIFACT_CHARS,
            require_nonblank=True,
        )
        # Artifact entries are filesystem paths: exact-string duplicates are
        # rejected (fingerprint collapsing would wrongly merge distinct paths).
        if len(set(artifacts)) != len(artifacts):
            msg = "expected_artifacts must not contain duplicates"
            raise ValueError(msg)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "coverage_claims", claims)
        object.__setattr__(self, "verification_hint", verification_hint)
        object.__setattr__(self, "expected_artifacts", artifacts)

    def to_dict(self) -> dict[str, object]:
        return {
            "description": self.description,
            "coverage_claims": list(self.coverage_claims),
            "verification_hint": self.verification_hint,
            "expected_artifacts": list(self.expected_artifacts),
        }

    @classmethod
    def from_dict(cls, data: object) -> DecompositionChild | None:
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(
                description=data["description"],
                coverage_claims=_read_string_list(data.get("coverage_claims", ())),
                verification_hint=data["verification_hint"],
                # Absent in older serialized proposals and in proposals for
                # parents that declare no artifacts -- default to empty.
                expected_artifacts=_read_string_list(data.get("expected_artifacts", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class DecompositionProposal:
    """Structured proposal returned by the decomposer."""

    children: tuple[DecompositionChild, ...]
    covers_parent: bool
    rationale: str

    def __post_init__(self) -> None:
        if type(self.covers_parent) is not bool:
            msg = "covers_parent must be a boolean"
            raise ValueError(msg)
        if not isinstance(self.rationale, str):
            msg = "rationale must be a string"
            raise ValueError(msg)
        children = _coerce_children(self.children)
        if not MIN_CHILDREN <= len(children) <= MAX_CHILDREN:
            msg = f"children must contain {MIN_CHILDREN}-{MAX_CHILDREN} items"
            raise ValueError(msg)
        object.__setattr__(self, "children", children)
        object.__setattr__(
            self,
            "rationale",
            _bounded_nonblank_text(
                self.rationale,
                field_name="rationale",
                max_chars=MAX_RATIONALE_CHARS,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "children": [child.to_dict() for child in self.children],
            "covers_parent": self.covers_parent,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: object) -> DecompositionProposal | None:
        if not isinstance(data, Mapping):
            return None
        try:
            children_raw = data["children"]
            if not isinstance(children_raw, Sequence) or isinstance(children_raw, str | bytes):
                return None
            children: list[DecompositionChild] = []
            for raw in children_raw:
                child = DecompositionChild.from_dict(raw)
                if child is None:
                    return None
                children.append(child)
            return cls(
                children=tuple(children),
                covers_parent=data["covers_parent"],
                rationale=data["rationale"],
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class DecompositionDecisionRecord:
    """Versioned decomposition decision persistence record."""

    node_id: str
    source: DecompositionSource
    disposition: DecompositionDisposition
    cause: BounceCause | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    children: tuple[DecompositionChild, ...] = field(default_factory=tuple)
    structural_status: StructuralCheckStatus = StructuralCheckStatus.NOT_RUN
    semantic_status: SemanticAttestationStatus = SemanticAttestationStatus.NOT_RUN
    repair_count: int = 0
    trustworthy: bool = False
    compromise_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SCHEMA_VERSION
        ):
            msg = f"schema_version must be {SCHEMA_VERSION}"
            raise ValueError(msg)
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            msg = "node_id must be a non-empty string"
            raise ValueError(msg)
        _require_enum(self.source, DecompositionSource, "source")
        _require_enum(self.disposition, DecompositionDisposition, "disposition")
        if self.cause is not None:
            _require_enum(self.cause, BounceCause, "cause")
        _require_enum(self.structural_status, StructuralCheckStatus, "structural_status")
        _require_enum(self.semantic_status, SemanticAttestationStatus, "semantic_status")
        if isinstance(self.repair_count, bool) or not isinstance(self.repair_count, int):
            msg = "repair_count must be an integer"
            raise ValueError(msg)
        if self.repair_count < 0:
            msg = "repair_count must be >= 0"
            raise ValueError(msg)
        if type(self.trustworthy) is not bool:
            msg = "trustworthy must be a boolean"
            raise ValueError(msg)
        if self.compromise_reason is not None and not isinstance(self.compromise_reason, str):
            msg = "compromise_reason must be a string when present"
            raise ValueError(msg)
        object.__setattr__(self, "node_id", self.node_id.strip())
        object.__setattr__(
            self,
            "reasons",
            _bounded_trace_strings(
                self.reasons,
                field_name="reasons",
                max_count=MAX_REASON_COUNT,
                max_chars=MAX_REASON_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _bounded_trace_strings(
                self.evidence_refs,
                field_name="evidence_refs",
                max_count=MAX_EVIDENCE_REF_COUNT,
                max_chars=MAX_EVIDENCE_REF_CHARS,
            ),
        )
        children = _coerce_children(self.children, allow_empty=True)
        if self.disposition is DecompositionDisposition.SPLIT:
            if not MIN_CHILDREN <= len(children) <= MAX_CHILDREN:
                msg = f"split decisions must contain {MIN_CHILDREN}-{MAX_CHILDREN} children"
                raise ValueError(msg)
        elif children:
            msg = "non-split decisions must not contain children"
            raise ValueError(msg)
        object.__setattr__(self, "children", children)
        if self.compromise_reason is not None:
            object.__setattr__(
                self,
                "compromise_reason",
                _bounded_text(
                    self.compromise_reason,
                    field_name="compromise_reason",
                    max_chars=MAX_REASON_CHARS,
                )
                or None,
            )
        if self.trustworthy and not self._meets_trust_invariant():
            msg = (
                "trustworthy split requires SPLIT disposition, structural PASSED, "
                "semantic ESTABLISHED, at least 2 children, and repair_count <= 1"
            )
            raise ValueError(msg)

    def _meets_trust_invariant(self) -> bool:
        return (
            self.disposition is DecompositionDisposition.SPLIT
            and self.structural_status is StructuralCheckStatus.PASSED
            and self.semantic_status is SemanticAttestationStatus.ESTABLISHED
            and len(self.children) >= MIN_CHILDREN
            and self.repair_count <= 1
            and _children_support_trust(self.children)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "source": self.source.value,
            "disposition": self.disposition.value,
            "cause": None if self.cause is None else self.cause.value,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
            "children": [child.to_dict() for child in self.children],
            "structural_status": self.structural_status.value,
            "semantic_status": self.semantic_status.value,
            "repair_count": self.repair_count,
            "trustworthy": self.trustworthy,
            "compromise_reason": self.compromise_reason,
        }

    @classmethod
    def from_dict(cls, data: object) -> DecompositionDecisionRecord | None:
        if not isinstance(data, Mapping):
            return None
        try:
            schema_version = data.get("schema_version")
            if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
                return None
            children_raw = data["children"]
            if not isinstance(children_raw, Sequence) or isinstance(children_raw, str | bytes):
                return None
            children: list[DecompositionChild] = []
            for raw in children_raw:
                child = DecompositionChild.from_dict(raw)
                if child is None:
                    return None
                children.append(child)
            cause_raw = data.get("cause")
            if cause_raw is not None and not isinstance(cause_raw, str):
                return None
            return cls(
                schema_version=data["schema_version"],
                node_id=data["node_id"],
                source=_parse_enum(DecompositionSource, data["source"]),
                disposition=_parse_enum(DecompositionDisposition, data["disposition"]),
                cause=None if cause_raw is None else _parse_enum(BounceCause, cause_raw),
                reasons=_read_string_list(data["reasons"]),
                evidence_refs=_read_string_list(data["evidence_refs"]),
                children=tuple(children),
                structural_status=_parse_enum(StructuralCheckStatus, data["structural_status"]),
                semantic_status=_parse_enum(SemanticAttestationStatus, data["semantic_status"]),
                repair_count=data["repair_count"],
                trustworthy=data["trustworthy"],
                compromise_reason=data.get("compromise_reason"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def parse_decomposition_proposal(
    data: object,
    *,
    parent_text: str = "",
    min_children: int = MIN_CHILDREN,
    max_children: int = MAX_CHILDREN,
    parent_expected_artifacts: tuple[str, ...] = (),
) -> DecompositionProposal | None:
    """Parse and structurally validate a decomposer proposal.

    This check is intentionally structural. A passing proposal has a valid
    shape, non-duplicate child text/claims, and an explicit parent coverage
    claim, but it does not prove semantic MECE. When
    ``parent_expected_artifacts`` is non-empty, the children's
    ``expected_artifacts`` must also form an exact partition of that set (see
    :func:`validate_decomposition_proposal`).
    """

    errors = validate_decomposition_proposal(
        data,
        parent_text=parent_text,
        min_children=min_children,
        max_children=max_children,
        parent_expected_artifacts=parent_expected_artifacts,
    )
    if errors:
        return None
    return DecompositionProposal.from_dict(data)


def validate_decomposition_proposal(
    data: object,
    *,
    parent_text: str = "",
    min_children: int = MIN_CHILDREN,
    max_children: int = MAX_CHILDREN,
    parent_expected_artifacts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return structural validation errors for a proposal payload.

    When ``parent_expected_artifacts`` is non-empty, the children's
    ``expected_artifacts`` fields must together form an exact partition of it:

    * every entry must be an exact string member of the parent's declared set
      (a child can never invent an artifact path -- the anti-self-grading
      safety property);
    * no artifact may be claimed by more than one child (mutual exclusivity,
      no double-crediting);
    * every parent artifact must be claimed by exactly one child (the split
      must actually cover the parent's whole artifact contract).

    When ``parent_expected_artifacts`` is empty, children's
    ``expected_artifacts`` are neither required nor partition-validated here.
    Note this is NOT byte-identical to the pre-feature behavior, though: a
    proposal whose children carry a malformed ``expected_artifacts`` field
    (duplicates, non-strings, blank/oversized entries, or too many of them)
    is still rejected at construction time by the
    :class:`DecompositionChild` ``__post_init__`` bounds (via ``from_dict``),
    forcing the whole proposal to bounce (atomic/escalation) where the old
    code silently ignored the unrecognized field and accepted the split. The
    delta is strictly MORE fail-closed, never less.
    """

    errors: list[str] = []
    if isinstance(data, str | bytes) or not isinstance(data, Mapping):
        return ("proposal must be an object",)
    if len(repr(data)) > MAX_PROPOSAL_REPR_CHARS:
        errors.append("proposal payload is too large")
    children_raw = data.get("children")
    if not isinstance(children_raw, Sequence) or isinstance(children_raw, str | bytes):
        errors.append("children must be a list")
        children_raw = ()
    if not min_children <= len(children_raw) <= max_children:
        errors.append(f"children must contain {min_children}-{max_children} items")
    if type(data.get("covers_parent")) is not bool:
        errors.append("covers_parent must be a boolean")
    elif data["covers_parent"] is False:
        errors.append("covers_parent must be true for a structural split")
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not _normalize_spaces(rationale):
        errors.append("rationale must be a non-empty string")
    elif len(rationale) > MAX_RATIONALE_CHARS:
        errors.append("rationale is too large")

    child_fingerprints: set[str] = set()
    coverage_fingerprints: set[str] = set()
    claimed_artifacts: set[str] = set()
    parent_artifact_set = set(parent_expected_artifacts)
    parent_fingerprint = _fingerprint(parent_text)
    for index, child_raw in enumerate(children_raw):
        if not isinstance(child_raw, Mapping):
            errors.append(f"child {index} must be an object")
            continue
        description = child_raw.get("description")
        if not isinstance(description, str) or not _normalize_spaces(description):
            errors.append(f"child {index} description must be a non-empty string")
            continue
        if len(description) > MAX_CHILD_DESCRIPTION_CHARS:
            errors.append(f"child {index} description is too large")
        child_fingerprint = _fingerprint(description)
        if parent_fingerprint and child_fingerprint == parent_fingerprint:
            errors.append(f"child {index} echoes the parent")
        if child_fingerprint in child_fingerprints:
            errors.append(f"child {index} duplicates another child")
        child_fingerprints.add(child_fingerprint)

        if parent_artifact_set:
            # Anti-self-grading partition check: each entry must exactly match
            # one of the PARENT's seed-authored expected_artifacts (fixed
            # before the decomposer ever ran), and no artifact may be claimed
            # twice. The exact-union leg runs after the loop.
            artifacts_raw = child_raw.get("expected_artifacts", ())
            if not isinstance(artifacts_raw, Sequence) or isinstance(artifacts_raw, str | bytes):
                errors.append(f"child {index} expected_artifacts must be a list")
            else:
                if len(artifacts_raw) > MAX_CHILD_EXPECTED_ARTIFACTS:
                    errors.append(f"child {index} has too many expected_artifacts")
                for artifact_index, artifact in enumerate(artifacts_raw):
                    if not isinstance(artifact, str) or not artifact.strip():
                        errors.append(
                            f"child {index} expected_artifacts entry {artifact_index} "
                            "must be a non-empty string"
                        )
                        continue
                    if artifact not in parent_artifact_set:
                        errors.append(
                            f"child {index} expected_artifacts entry {artifact_index} "
                            "is not one of the parent's declared expected_artifacts"
                        )
                        continue
                    if artifact in claimed_artifacts:
                        errors.append(
                            f"child {index} expected_artifacts entry {artifact_index} "
                            "is already claimed by another child"
                        )
                        continue
                    claimed_artifacts.add(artifact)

        claims = child_raw.get("coverage_claims")
        if not isinstance(claims, Sequence) or isinstance(claims, str | bytes):
            errors.append(f"child {index} coverage_claims must be a list")
            continue
        if not claims:
            errors.append(f"child {index} must declare at least one coverage claim")
        if len(claims) > MAX_COVERAGE_CLAIMS:
            errors.append(f"child {index} has too many coverage claims")
        child_claim_fingerprints: set[str] = set()
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, str) or not _normalize_spaces(claim):
                errors.append(f"child {index} claim {claim_index} must be a non-empty string")
                continue
            if len(claim) > MAX_COVERAGE_CLAIM_CHARS:
                errors.append(f"child {index} claim {claim_index} is too large")
            claim_fingerprint = _fingerprint(claim)
            if claim_fingerprint in child_claim_fingerprints:
                errors.append(f"child {index} repeats a coverage claim")
            if claim_fingerprint in coverage_fingerprints:
                errors.append(f"child {index} duplicates another child's coverage claim")
            child_claim_fingerprints.add(claim_fingerprint)
            coverage_fingerprints.add(claim_fingerprint)

        hint = child_raw.get("verification_hint")
        if not isinstance(hint, str):
            errors.append(f"child {index} verification_hint must be a string")
        elif not _normalize_spaces(hint):
            errors.append(f"child {index} verification_hint must be non-empty")
        elif len(hint) > MAX_VERIFICATION_HINT_CHARS:
            errors.append(f"child {index} verification_hint is too large")

    if parent_artifact_set:
        unclaimed = sorted(parent_artifact_set - claimed_artifacts)
        if unclaimed:
            # The union of all children's expected_artifacts must equal the
            # parent's declared set EXACTLY. A proposal that leaves any parent
            # artifact unassigned does not actually partition the parent's
            # contract and must not structurally pass as MECE-on-artifacts.
            errors.append(
                "parent expected_artifacts not fully assigned to children: " + ", ".join(unclaimed)
            )

    return tuple(errors)


def structural_decision_from_proposal(
    *,
    node_id: str,
    source: DecompositionSource,
    proposal: DecompositionProposal,
    repair_count: int = 0,
    reasons: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    cause: BounceCause | None = None,
) -> DecompositionDecisionRecord:
    """Create a structurally valid split decision without semantic trust."""

    return DecompositionDecisionRecord(
        node_id=node_id,
        source=source,
        disposition=DecompositionDisposition.SPLIT,
        cause=cause,
        reasons=tuple(reasons),
        evidence_refs=tuple(evidence_refs),
        children=proposal.children,
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.NOT_RUN,
        repair_count=repair_count,
        trustworthy=False,
    )


def legacy_unverified_split_decision(
    *,
    node_id: str,
    source: DecompositionSource,
    child_descriptions: Sequence[str],
    cause: BounceCause | None = None,
    reasons: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
) -> DecompositionDecisionRecord:
    """Create an untrusted split from legacy list[str] decomposition output."""

    children = tuple(
        DecompositionChild(description=description, coverage_claims=(), verification_hint="")
        for description in child_descriptions
    )
    return DecompositionDecisionRecord(
        node_id=node_id,
        source=source,
        disposition=DecompositionDisposition.SPLIT,
        cause=cause,
        reasons=tuple(reasons),
        evidence_refs=tuple(evidence_refs),
        children=children,
        structural_status=StructuralCheckStatus.NOT_RUN,
        semantic_status=SemanticAttestationStatus.NOT_RUN,
        trustworthy=False,
    )


def decision_from_legacy_children(
    node_id: str,
    source: DecompositionSource,
    child_descriptions: Sequence[str],
    *,
    cause: BounceCause | None = None,
    reasons: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
) -> DecompositionDecisionRecord:
    """Positional-friendly alias for legacy list[str] split output."""

    return legacy_unverified_split_decision(
        node_id=node_id,
        source=source,
        child_descriptions=child_descriptions,
        cause=cause,
        reasons=reasons,
        evidence_refs=evidence_refs,
    )


def redact_and_truncate_text(text: str, *, max_chars: int = MAX_REASON_CHARS) -> str:
    """Redact secret-like key/value material and bound text length."""

    if not isinstance(text, str):
        msg = "text must be a string"
        raise ValueError(msg)
    redacted = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", text)
    redacted = _SECRET_QUOTED_VALUE_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{_SECRET_VALUE}{match.group('quote')}"
        ),
        redacted,
    )
    redacted = _SECRET_UNQUOTED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{_SECRET_VALUE}",
        redacted,
    )
    if len(redacted) <= max_chars:
        return redacted
    marker = "...[truncated]"
    keep = max(0, max_chars - len(marker))
    return f"{redacted[:keep]}{marker}"


def summarize_decomposition_trace(
    text: str,
    *,
    evidence_refs: Sequence[str] = (),
    max_chars: int = MAX_TRACE_SUMMARY_CHARS,
) -> DecompositionTraceSummary:
    """Build bounded trace context for persistence or logs."""

    return DecompositionTraceSummary(
        summary=redact_and_truncate_text(text, max_chars=max_chars),
        evidence_refs=tuple(evidence_refs),
    )


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        msg = f"{field_name} must be {enum_type.__name__}"
        raise ValueError(msg)


def _parse_enum(enum_type: type[StrEnum], value: object) -> Any:
    if not isinstance(value, str):
        raise ValueError
    return enum_type(value)


def _read_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError
    if not all(isinstance(item, str) for item in value):
        raise ValueError
    return tuple(value)


def _bounded_nonblank_text(text: str, *, field_name: str, max_chars: int) -> str:
    text = _bounded_text(text, field_name=field_name, max_chars=max_chars)
    if not text:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)
    return text


def _bounded_text(text: str, *, field_name: str, max_chars: int) -> str:
    if not isinstance(text, str):
        msg = f"{field_name} must be a string"
        raise ValueError(msg)
    stripped = _normalize_spaces(text)
    if len(stripped) > max_chars:
        msg = f"{field_name} exceeds {max_chars} characters"
        raise ValueError(msg)
    return redact_and_truncate_text(stripped, max_chars=max_chars)


def _bounded_strings(
    values: Sequence[str],
    *,
    field_name: str,
    max_count: int,
    max_chars: int,
    require_nonblank: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        msg = f"{field_name} must be a sequence of strings"
        raise ValueError(msg)
    if len(values) > max_count:
        msg = f"{field_name} must contain at most {max_count} items"
        raise ValueError(msg)
    bounded: list[str] = []
    for value in values:
        if not isinstance(value, str):
            msg = f"{field_name} must contain only strings"
            raise ValueError(msg)
        normalized = _bounded_text(value, field_name=field_name, max_chars=max_chars)
        if require_nonblank and not normalized:
            msg = f"{field_name} must not contain blank strings"
            raise ValueError(msg)
        if normalized:
            bounded.append(normalized)
    return tuple(bounded)


def _bounded_trace_strings(
    values: Sequence[str],
    *,
    field_name: str,
    max_count: int,
    max_chars: int,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        msg = f"{field_name} must be a sequence of strings"
        raise ValueError(msg)
    if len(values) > max_count:
        msg = f"{field_name} must contain at most {max_count} items"
        raise ValueError(msg)
    bounded: list[str] = []
    for value in values:
        if not isinstance(value, str):
            msg = f"{field_name} must contain only strings"
            raise ValueError(msg)
        normalized = _normalize_spaces(value)
        if normalized:
            bounded.append(redact_and_truncate_text(normalized, max_chars=max_chars))
    return tuple(bounded)


def _coerce_children(
    children: Sequence[DecompositionChild],
    *,
    allow_empty: bool = False,
) -> tuple[DecompositionChild, ...]:
    if not isinstance(children, Sequence) or isinstance(children, str | bytes):
        msg = "children must be a sequence"
        raise ValueError(msg)
    if not allow_empty and not MIN_CHILDREN <= len(children) <= MAX_CHILDREN:
        msg = f"children must contain {MIN_CHILDREN}-{MAX_CHILDREN} items"
        raise ValueError(msg)
    coerced = tuple(children)
    if any(not isinstance(child, DecompositionChild) for child in coerced):
        msg = "children must contain DecompositionChild values"
        raise ValueError(msg)
    return coerced


def _normalize_spaces(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _fingerprint(text: str) -> str:
    return _FINGERPRINT_RE.sub("", _normalize_spaces(text).lower())


def _has_duplicate_fingerprints(values: Sequence[str]) -> bool:
    seen: set[str] = set()
    for value in values:
        fingerprint = _fingerprint(value)
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
    return False


def _children_support_trust(children: Sequence[DecompositionChild]) -> bool:
    descriptions: set[str] = set()
    coverage_claims: set[str] = set()
    for child in children:
        if not child.coverage_claims or not child.verification_hint:
            return False
        description = _fingerprint(child.description)
        if not description or description in descriptions:
            return False
        descriptions.add(description)
        for claim in child.coverage_claims:
            fingerprint = _fingerprint(claim)
            if not fingerprint or fingerprint in coverage_claims:
                return False
            coverage_claims.add(fingerprint)
    return True


__all__ = [
    "BounceCause",
    "DecompositionChild",
    "DecompositionDecisionRecord",
    "DecompositionDisposition",
    "DecompositionProposal",
    "DecompositionSource",
    "DecompositionTraceSummary",
    "SCHEMA_VERSION",
    "SemanticAttestationStatus",
    "StructuralCheckStatus",
    "decision_from_legacy_children",
    "legacy_unverified_split_decision",
    "parse_decomposition_proposal",
    "redact_and_truncate_text",
    "structural_decision_from_proposal",
    "summarize_decomposition_trace",
    "validate_decomposition_proposal",
]
