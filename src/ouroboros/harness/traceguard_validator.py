"""Deterministic, LLM-free TraceGuard validator for the #978 deliver gate.

``deliver_gate.evaluate_deliver_claim`` accepts an injected
:class:`~ouroboros.harness.deliver_gate.TraceGuardValidator` but the repository
shipped only the Protocol, leaving the gate inert in production. This module is
the concrete, in-repo implementation of that contract.

The validator is a pure set-membership judge. It never calls a model, performs
no IO, and imports nothing from ``orchestrator``. Given the canonical evidence
manifest (``fact_id`` / ``chunk_id`` pairs) the gate derives from the journal and
the ``parent_synthesis`` claim surface, it decides which claims are grounded in
cited evidence and which are not. Because the decision is exact set membership,
the verdict cannot be reward-hacked.

Rejection reasons are drawn from the exact vocabulary
``ouroboros.harness.deliver_routing`` routes on (``unsupported_fact_id``,
``evidence_handle_mismatch``, ``chunk_handle_without_fact``,
``malformed_evidence_claim``) plus ``no_claim_bearing_surface`` for a synthesis
that carries no claims at all. Each reason is emitted as the bare code; the
downstream gate joins it with the human-readable detail as ``"code: detail"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ouroboros.harness.deliver_gate import TraceGuardEvidenceInput

# Reason codes must match the vocabulary deliver_routing._REDISPATCH_REASONS and
# deliver_gate rejection handling route on. Only the code (text before the first
# ``:``) is routed; the detail is human-facing context.
_REASON_UNSUPPORTED_FACT = "unsupported_fact_id"
_REASON_HANDLE_MISMATCH = "evidence_handle_mismatch"
_REASON_CHUNK_WITHOUT_FACT = "chunk_handle_without_fact"
_REASON_MALFORMED = "malformed_evidence_claim"
_REASON_NO_SURFACE = "no_claim_bearing_surface"

_DETAIL_UNSUPPORTED_FACT = "fact is not present in manifest"
_DETAIL_HANDLE_MISMATCH = "claim cited an evidence handle not bound to this fact"
_DETAIL_CHUNK_WITHOUT_FACT = "chunk was cited without a supported fact"
_DETAIL_MALFORMED = "claim is missing a required fact or evidence identifier"
_DETAIL_NO_SURFACE = "parent synthesis exposed no retained facts or evidence references"

# Claim-bearing surfaces the gate may hand us. The live gate emits
# ``result.observed_facts`` (see deliver_gate._parent_synthesis_from_claim); the
# additional paths accept the retained-fact / evidence-reference vocabulary the
# wider proof machinery speaks so a genuine claim is never misread as "no
# surface".
_CLAIM_SURFACE_PATHS: tuple[tuple[str, ...], ...] = (
    ("result", "observed_facts"),
    ("result", "retained_facts"),
    ("result", "evidence_references"),
    ("retained_facts",),
    ("evidence_references",),
    ("observed_facts",),
)
_FACT_ID_KEYS: tuple[str, ...] = ("fact_id",)
_CHUNK_ID_KEYS: tuple[str, ...] = ("chunk_id", "evidence_handle", "evidence", "chunk")


@dataclass(frozen=True, slots=True)
class TraceGuardClaimRef:
    """Identifier pair for a single accepted/rejected claim.

    Exposes ``fact_id`` and ``chunk_id`` in the shape the deliver gate's
    ``_claim_fact_ids`` / ``_claim_chunk_ids`` / ``_rejected_claim_summaries``
    read.
    """

    fact_id: str | None
    chunk_id: str | None


@dataclass(frozen=True, slots=True)
class TraceGuardRejection:
    """One rejected claim with its routed reason code and human detail.

    The deliver gate reads ``.claim`` (a :class:`TraceGuardClaimRef`), ``.reason``
    (bare code) and ``.detail`` (context), then joins them as ``"reason: detail"``.
    """

    reason: str
    detail: str
    claim: TraceGuardClaimRef


@dataclass(frozen=True, slots=True)
class TraceGuardValidationResult:
    """Deterministic verdict satisfying ``deliver_gate.TraceGuardResultLike``."""

    accepted: bool
    unsupported_claim_rate: float
    accepted_claims: tuple[TraceGuardClaimRef, ...] = field(default_factory=tuple)
    rejected_claims: tuple[TraceGuardRejection, ...] = field(default_factory=tuple)
    allowed_fact_ids: tuple[str, ...] = field(default_factory=tuple)
    allowed_chunk_ids: tuple[str, ...] = field(default_factory=tuple)


def validate_evidence_claims(
    *,
    evidence_manifest: tuple[TraceGuardEvidenceInput, ...],
    parent_synthesis: dict[str, Any],
) -> TraceGuardValidationResult:
    """Judge each parent-synthesis claim against the evidence manifest.

    The signature matches ``deliver_gate.TraceGuardValidator`` exactly so this
    callable can be injected as ``traceguard_validator=validate_evidence_claims``.

    A claim carrying fact ``f`` and cited handle ``h`` is:

    * accepted when ``(f, h)`` is a manifest pair;
    * rejected ``evidence_handle_mismatch`` when ``f`` exists in the manifest but
      not under ``h``;
    * rejected ``unsupported_fact_id`` when ``f`` is absent from the manifest;
    * rejected ``chunk_handle_without_fact`` when it has no fact but cites a known
      chunk ``h``;
    * rejected ``malformed_evidence_claim`` when it is missing identifiers.

    A synthesis with no claim-bearing surface at all yields an unaccepted result
    with a single ``no_claim_bearing_surface`` rejection.

    Args:
        evidence_manifest: Canonical ``fact_id`` / ``chunk_id`` evidence entries.
        parent_synthesis: The gate's claim surface (``result.observed_facts``).

    Returns:
        A frozen :class:`TraceGuardValidationResult`.
    """
    pair_set, fact_ids, chunk_ids = _index_manifest(evidence_manifest)
    allowed_fact_ids = _ordered_unique(_clean(entry.fact_id) for entry in evidence_manifest)
    allowed_chunk_ids = _ordered_unique(_clean(entry.chunk_id) for entry in evidence_manifest)

    claims = _iter_claim_entries(parent_synthesis)
    if not claims:
        return TraceGuardValidationResult(
            accepted=False,
            unsupported_claim_rate=0.0,
            rejected_claims=(
                TraceGuardRejection(
                    reason=_REASON_NO_SURFACE,
                    detail=_DETAIL_NO_SURFACE,
                    claim=TraceGuardClaimRef(fact_id=None, chunk_id=None),
                ),
            ),
            allowed_fact_ids=allowed_fact_ids,
            allowed_chunk_ids=allowed_chunk_ids,
        )

    accepted: list[TraceGuardClaimRef] = []
    rejected: list[TraceGuardRejection] = []
    for fact_id, chunk_id in claims:
        _classify_claim(
            fact_id=fact_id,
            chunk_id=chunk_id,
            pair_set=pair_set,
            fact_ids=fact_ids,
            chunk_ids=chunk_ids,
            accepted=accepted,
            rejected=rejected,
        )

    total = len(accepted) + len(rejected)
    unsupported_claim_rate = round(len(rejected) / total, 4) if total else 0.0
    return TraceGuardValidationResult(
        accepted=bool(accepted) and not rejected,
        unsupported_claim_rate=unsupported_claim_rate,
        accepted_claims=tuple(accepted),
        rejected_claims=tuple(rejected),
        allowed_fact_ids=allowed_fact_ids,
        allowed_chunk_ids=allowed_chunk_ids,
    )


def _classify_claim(
    *,
    fact_id: str | None,
    chunk_id: str | None,
    pair_set: frozenset[tuple[str, str]],
    fact_ids: frozenset[str],
    chunk_ids: frozenset[str],
    accepted: list[TraceGuardClaimRef],
    rejected: list[TraceGuardRejection],
) -> None:
    if fact_id is not None and chunk_id is not None:
        if (fact_id, chunk_id) in pair_set:
            accepted.append(TraceGuardClaimRef(fact_id=fact_id, chunk_id=chunk_id))
        elif fact_id in fact_ids:
            rejected.append(
                _reject(_REASON_HANDLE_MISMATCH, _DETAIL_HANDLE_MISMATCH, fact_id, chunk_id)
            )
        else:
            rejected.append(
                _reject(_REASON_UNSUPPORTED_FACT, _DETAIL_UNSUPPORTED_FACT, fact_id, chunk_id)
            )
        return
    if fact_id is None and chunk_id is not None and chunk_id in chunk_ids:
        rejected.append(
            _reject(_REASON_CHUNK_WITHOUT_FACT, _DETAIL_CHUNK_WITHOUT_FACT, None, chunk_id)
        )
        return
    rejected.append(_reject(_REASON_MALFORMED, _DETAIL_MALFORMED, fact_id, chunk_id))


def _reject(
    reason: str,
    detail: str,
    fact_id: str | None,
    chunk_id: str | None,
) -> TraceGuardRejection:
    return TraceGuardRejection(
        reason=reason,
        detail=detail,
        claim=TraceGuardClaimRef(fact_id=fact_id, chunk_id=chunk_id),
    )


def _index_manifest(
    evidence_manifest: tuple[TraceGuardEvidenceInput, ...],
) -> tuple[frozenset[tuple[str, str]], frozenset[str], frozenset[str]]:
    pairs: set[tuple[str, str]] = set()
    fact_ids: set[str] = set()
    chunk_ids: set[str] = set()
    for entry in evidence_manifest:
        fact_id = _clean(entry.fact_id)
        chunk_id = _clean(entry.chunk_id)
        if fact_id is not None:
            fact_ids.add(fact_id)
        if chunk_id is not None:
            chunk_ids.add(chunk_id)
        if fact_id is not None and chunk_id is not None:
            pairs.add((fact_id, chunk_id))
    return frozenset(pairs), frozenset(fact_ids), frozenset(chunk_ids)


def _iter_claim_entries(
    parent_synthesis: Mapping[str, Any],
) -> tuple[tuple[str | None, str | None], ...]:
    if not isinstance(parent_synthesis, Mapping):
        return ()
    claims: list[tuple[str | None, str | None]] = []
    for path in _CLAIM_SURFACE_PATHS:
        node: Any = parent_synthesis
        for key in path:
            if isinstance(node, Mapping) and key in node:
                node = node[key]
            else:
                node = None
                break
        if isinstance(node, (list, tuple)):
            claims.extend(_normalize_entry(entry) for entry in node)
    return tuple(claims)


def _normalize_entry(entry: object) -> tuple[str | None, str | None]:
    if isinstance(entry, Mapping):
        return _first_str(entry, _FACT_ID_KEYS), _first_str(entry, _CHUNK_ID_KEYS)
    return None, None


def _first_str(entry: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _clean(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _ordered_unique(values: Any) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is not None and value not in seen:
            ordered.append(value)
            seen.add(value)
    return tuple(ordered)


__all__ = [
    "TraceGuardClaimRef",
    "TraceGuardRejection",
    "TraceGuardValidationResult",
    "validate_evidence_claims",
]
