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

    evidence_tokens = _normalize_tokens(evidence_text)
    missing: list[str] = []
    for term in terms:
        alternatives = tuple(
            tokens
            for value in _term_value_alternatives(term)
            if (tokens := _normalize_tokens(value))
        )
        if not alternatives or not any(
            _contains_token_sequence(evidence_tokens, tokens) for tokens in alternatives
        ):
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
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "`'\"":
        return stripped[1:-1].strip()
    return stripped.rstrip(".,;:!?")


def _term_value_alternatives(term: _StructuredTerm) -> tuple[str, ...]:
    alternatives = [term.value, term.value.replace("_", " ")]
    normalized_key = term.key.lower()
    normalized_value = term.value.lower()
    if normalized_key == "result":
        alternatives.extend(_RESULT_VALUE_ALIASES.get(normalized_value, ()))
    return tuple(dict.fromkeys(alternatives))


_RESULT_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    # Existing deliver-claim vocabulary records outcome labels in the claim while
    # journal evidence often stores natural-language previews. Keep aliases
    # narrow and result-key scoped so behavior/path terms remain exact.
    "test_passed": ("tests passed", "test passed", "pytest passed"),
    "file_modified": ("file modified", "modified file", "file updated", "docs updated"),
}


def _normalize_tokens(value: str) -> tuple[str, ...]:
    return tuple(_tokenize(value))


def _contains_token_sequence(tokens: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if len(needle) > len(tokens):
        return False
    return any(
        tokens[index : index + len(needle)] == needle
        for index in range(len(tokens) - len(needle) + 1)
    )


def _tokenize(value: str) -> Iterable[str]:
    return re.findall(r"[a-z0-9_./:-]+", value.lower())


__all__ = [
    "ClaimTermGuard",
    "ClaimTermGuardFact",
    "ClaimTermGuardVerdict",
    "deterministic_claim_term_guard",
]
