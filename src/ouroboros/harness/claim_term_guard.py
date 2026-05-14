"""Deterministic claim-term guard for evidence-backed deliver claims.

TraceGuard answers the structural question: did the claim cite an admissible
evidence handle? This module adds a small, read-only harness check for the next
question: does the cited evidence text contain the structured term values the
claim itself says are required?

The guard is intentionally deterministic and conservative. It only enforces
explicit ``key=value`` terms in claim statements, so prose-only claims are left
to later LLM or profile-specific semantic evaluators. The ``key`` identifies the
required term in diagnostics; only the normalized ``value`` is required to
appear in evidence text because journal evidence often stores compressed
``args_preview`` / ``result_preview`` text without the original claim key.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ClaimTermGuardFact:
    """One evidence-backed fact checked by the claim-term guard."""

    fact_id: str
    evidence_handle: str
    statement: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class ClaimTermGuardVerdict:
    """Claim-term guard result for an already TraceGuard-backed claim."""

    accepted: bool
    rejected_fact_ids: tuple[str, ...] = ()
    rejected_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.accepted and (self.rejected_fact_ids or self.rejected_reasons):
            msg = "accepted ClaimTermGuardVerdict cannot carry rejections"
            raise ValueError(msg)
        if not self.accepted and not self.rejected_reasons:
            msg = "rejected ClaimTermGuardVerdict must include rejection reasons"
            raise ValueError(msg)


class ClaimTermGuard(Protocol):
    """Callable shape for deterministic or profile-specific claim-term guards."""

    def __call__(
        self,
        *,
        ac_id: str,
        facts: tuple[ClaimTermGuardFact, ...],
    ) -> ClaimTermGuardVerdict:
        raise NotImplementedError


_STRUCTURED_TERM_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_.:-]*)=(?P<value>`[^`]+`|\"[^\"]+\"|'[^']+'|[^\s;,\)\]\}]+)"
)


def deterministic_claim_term_guard(
    *,
    ac_id: str,
    facts: tuple[ClaimTermGuardFact, ...],
) -> ClaimTermGuardVerdict:
    """Reject evidence-backed claims whose structured term values are absent.

    ``ac_id`` is accepted for parity with richer future guards. The current
    deterministic implementation is fact-local and does not inspect global AC
    context.
    """
    del ac_id
    rejected_fact_ids: list[str] = []
    rejected_reasons: list[str] = []

    for fact in facts:
        missing = _missing_structured_terms(
            statement=fact.statement,
            evidence_text=fact.evidence_text,
        )
        if not missing:
            continue
        rejected_fact_ids.append(fact.fact_id)
        rejected_reasons.append(
            "semantic_miss: "
            f"{fact.fact_id} cites {fact.evidence_handle} but evidence text lacks "
            f"required term(s): {', '.join(missing)}"
        )

    if rejected_reasons:
        return ClaimTermGuardVerdict(
            accepted=False,
            rejected_fact_ids=tuple(rejected_fact_ids),
            rejected_reasons=tuple(rejected_reasons),
        )
    return ClaimTermGuardVerdict(accepted=True)


def _missing_structured_terms(*, statement: str, evidence_text: str) -> tuple[str, ...]:
    terms = _structured_terms(statement)
    if not terms:
        return ()

    normalized_evidence = _normalize_text(evidence_text)
    missing: list[str] = []
    for term in terms:
        if _normalize_text(term.value) not in normalized_evidence:
            missing.append(f"{term.key}={term.value}")
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class _StructuredTerm:
    key: str
    value: str


def _structured_terms(statement: str) -> tuple[_StructuredTerm, ...]:
    terms: list[_StructuredTerm] = []
    for match in _STRUCTURED_TERM_RE.finditer(statement):
        key = match.group("key").strip()
        value = _strip_literal(match.group("value"))
        if key and value:
            terms.append(_StructuredTerm(key=key, value=value))
    return tuple(terms)


def _strip_literal(value: str) -> str:
    return value.strip().strip("`'\"")


def _normalize_text(value: str) -> str:
    return " ".join(_tokenize(value))


def _tokenize(value: str) -> Iterable[str]:
    return re.findall(r"[a-z0-9_./:-]+", value.lower())


__all__ = [
    "ClaimTermGuard",
    "ClaimTermGuardFact",
    "ClaimTermGuardVerdict",
    "deterministic_claim_term_guard",
]
