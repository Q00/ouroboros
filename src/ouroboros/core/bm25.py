"""Okapi BM25 lexical ranking in pure Python.

A small, dependency-free ranker for short corpora (tens to a few thousand
documents) such as the brownfield repository registry. Ranking is fully
deterministic: equal scores are ordered by the caller-supplied tie key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import math
import re

# Okapi BM25 defaults (Robertson & Zaragoza).
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[^\W\d_a-zA-Z]+")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)


def tokenize(text: str) -> list[str]:
    """Split text into lowercase terms.

    Words are split on non-alphanumeric characters and on camelCase
    boundaries (``BrownfieldHandler`` yields ``brownfield``, ``handler`` and
    ``brownfieldhandler``). Single-character ASCII terms and a short English
    stopword list are dropped. Non-Latin scripts are kept as whole words.
    """
    terms: list[str] = []
    for word in _WORD_RE.findall(text):
        parts = _CAMEL_RE.findall(word)
        lowered = word.lower()
        if len(parts) > 1:
            terms.extend(p.lower() for p in parts)
            terms.append(lowered)
        else:
            terms.append(lowered)
    return [t for t in terms if t not in _STOPWORDS and not (len(t) == 1 and t.isascii())]


@dataclass(frozen=True, slots=True)
class ScoredDocument:
    """One ranked document: its index in the input corpus and its score."""

    index: int
    score: float


def rank(
    query: str,
    documents: Sequence[str],
    *,
    tie_keys: Sequence[str] | None = None,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> list[ScoredDocument]:
    """Rank ``documents`` against ``query`` with Okapi BM25.

    Only documents sharing at least one query term (score > 0) are returned,
    ordered by score descending, then by ``tie_keys[index]`` ascending (when
    given), then by input index ascending.

    The IDF term is ``ln(1 + (N - df + 0.5) / (df + 0.5))``, which stays
    positive even when a term occurs in every document, so a match never
    scores below a non-match.
    """
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not documents:
        return []

    doc_terms = [Counter(tokenize(doc)) for doc in documents]
    return rank_tokenized(query_terms, doc_terms, tie_keys=tie_keys, k1=k1, b=b)


def rank_tokenized(
    query_terms: Iterable[str],
    doc_terms: Sequence[Counter[str]],
    *,
    tie_keys: Sequence[str] | None = None,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> list[ScoredDocument]:
    """BM25 over pre-tokenized documents (term-frequency counters)."""
    terms = list(dict.fromkeys(query_terms))
    n_docs = len(doc_terms)
    if not terms or n_docs == 0:
        return []

    lengths = [sum(tf.values()) for tf in doc_terms]
    avg_len = (sum(lengths) / n_docs) or 1.0

    idf: dict[str, float] = {}
    for term in terms:
        df = sum(1 for tf in doc_terms if term in tf)
        if df:
            idf[term] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

    scored: list[ScoredDocument] = []
    for index, tf in enumerate(doc_terms):
        norm = k1 * (1.0 - b + b * lengths[index] / avg_len)
        score = 0.0
        for term, term_idf in idf.items():
            freq = tf.get(term, 0)
            if freq:
                score += term_idf * freq * (k1 + 1.0) / (freq + norm)
        if score > 0.0:
            scored.append(ScoredDocument(index=index, score=score))

    def _order(item: ScoredDocument) -> tuple[float, str, int]:
        tie = tie_keys[item.index] if tie_keys is not None else ""
        # Rounded so float noise cannot reorder documents that tie on score.
        return (-round(item.score, 9), tie, item.index)

    scored.sort(key=_order)
    return scored


__all__ = ["DEFAULT_B", "DEFAULT_K1", "ScoredDocument", "rank", "rank_tokenized", "tokenize"]
