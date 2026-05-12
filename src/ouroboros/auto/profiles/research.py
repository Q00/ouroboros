"""Research domain profile (#809 P3, PR 6/6).

A minimal-but-real second built-in profile, registered alongside the
``coding`` profile (PR-2).  Its purpose is acceptance: prove that
adding a new domain requires zero core changes — only a profile
module and a registry call.
"""

from __future__ import annotations

from pathlib import Path
import re

from ouroboros.auto.domain_profile import (
    DEFAULT_REGISTRY,
    DomainProfile,
)

__all__ = ["RESEARCH_PROFILE"]


class _SourceCountPredicate:
    code = "source_count"
    _SOURCE_COUNT_RE = re.compile(
        r"""
        (?:\b(?:at\s+least|minimum(?:\s+of)?|no\s+fewer\s+than)\s+\d+(?![\w.])\s+
            (?:sources?|citations?|references?)\b(?!\s+(?:files?|implementations?|modules?|maps?)))
        |
        (?:(?<![\w.])\d+(?![\w.])\s+
            (?:sources?|citations?|references?)\b(?!\s+(?:files?|implementations?|modules?|maps?)))
        """,
        re.I | re.X,
    )

    def matches(self, criterion: str) -> bool:
        return bool(self._SOURCE_COUNT_RE.search(criterion))

    def repair_template(self, criterion: str) -> str:
        return "Verify by counting cited sources; AC must specify a numeric minimum."


class _CitationFormatPredicate:
    code = "citation_format"
    _CITATION_FORMAT_RE = re.compile(
        r"""
        \b(?:citation|reference)\s+(?:style|format|section|list)\b
        |\b(?:citations|references)\s+section\b
        |\bbibliograph(?:y|ic|ies)\b
        """,
        re.I | re.X,
    )

    def matches(self, criterion: str) -> bool:
        return bool(self._CITATION_FORMAT_RE.search(criterion))

    def repair_template(self, criterion: str) -> str:
        return "Specify the citation style (APA / MLA / Chicago / IEEE) explicitly."


class _ResearchIntentClassifier:
    _PATTERNS = (
        ("runtime_context", re.compile(r"\b(survey|review|state of the art|sota)\b", re.I)),
        ("verification", re.compile(r"\b(hypothesis|claim|assertion|verify|validate)\b", re.I)),
        (
            "acceptance_criteria",
            re.compile(r"\b(evidence|finding|source|sources|citation|citations)\b", re.I),
        ),
    )

    def classify(self, question: str) -> str | None:
        for label, pat in self._PATTERNS:
            if pat.search(question):
                return label
        return None

    def supported_intents(self) -> frozenset[str]:
        return frozenset(label for label, _pat in self._PATTERNS)


class _ResearchRepoExtractor:
    def extract(self, cwd: Path) -> dict:
        return {
            "has_bibliography": (
                (cwd / ".bibliography").exists() or (cwd / "references.bib").exists()
            ),
            "has_paper": any(cwd.glob("*.tex")) or any(cwd.glob("paper*.md")),
        }


_VAGUE_RESEARCH_TERMS = frozenset(
    {
        "thorough",
        "comprehensive",
        "deep",
        "rigorous",
        "exhaustive",
        "robust",
    }
)


def _research_detector(cwd: Path) -> float:
    score = 0.0
    if (cwd / "references.bib").exists() or (cwd / ".bibliography").exists():
        score += 0.6
    if any(cwd.glob("*.tex")):
        score += 0.3
    if any(cwd.glob("paper*.md")):
        score += 0.2
    return min(score, 1.0)


RESEARCH_PROFILE: DomainProfile = DomainProfile(
    name="research",
    repo_context_extractor=_ResearchRepoExtractor(),
    verifiable_predicates=(_SourceCountPredicate(), _CitationFormatPredicate()),
    intent_classifier=_ResearchIntentClassifier(),
    vague_terms=_VAGUE_RESEARCH_TERMS,
    safe_defaults={"min_sources": 3, "default_citation_style": "APA"},
    detector=_research_detector,
)

DEFAULT_REGISTRY.register(RESEARCH_PROFILE)
