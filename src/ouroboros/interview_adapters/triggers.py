"""Conservative v1 trigger policy for glossary injection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re

from ouroboros.interview_adapters.manifest import GlossaryManifest, GlossaryTerm
from ouroboros.interview_adapters.models import InterviewTurnContext
from ouroboros.interview_adapters.registry import BuiltinGlossaryRegistry, builtin_registry

MAX_INJECTED_TERMS = 3

_WHAT_DOES_MEAN_PATTERN = re.compile(
    r"\bwhat does\s+[\"'“‘]?(?P<term>[A-Za-z][A-Za-z0-9 /_-]{0,79}?)[\"'”’]?\s+mean\b",
    re.IGNORECASE,
)
_DO_NOT_UNDERSTAND_PATTERN = re.compile(
    r"\b(?:i\s+)?(?:do not|don't)\s+understand\s+"
    r"[\"'“‘]?(?P<term>[A-Za-z][A-Za-z0-9 /_-]{0,79})[\"'”’]?\??",
    re.IGNORECASE,
)
_QUOTED_CONFUSION_PATTERN = re.compile(
    r"\b(?:what is|what's|define|meaning of|confused (?:about|by))\s+"
    r"[\"'“‘]?(?P<term>[A-Za-z][A-Za-z0-9 /_-]{0,79})[\"'”’]?\??",
    re.IGNORECASE,
)
_TERM_CONFUSION_PATTERN = re.compile(
    r"\b(?P<term>[A-Za-z][A-Za-z0-9 /_-]{0,79})\s+"
    r"(?:means what|mean\?|is confusing|confuses me)\b",
    re.IGNORECASE,
)
_DECISION_ONLY_PATTERN = re.compile(
    r"\b(?:should we|which should|what should|choose|decide|pick|recommend)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GlossaryInjection:
    """Bounded glossary text selected for a single non-initial turn."""

    pack_name: str
    terms: tuple[GlossaryTerm, ...]

    def render(self) -> str:
        lines = [f"Glossary help ({self.pack_name}):"]
        lines.extend(f"- {term.term}: {term.explanation}" for term in self.terms)
        return "\n".join(lines)


def detect_explicit_confusion_terms(text: str) -> tuple[str, ...]:
    """Return terms from conservative explicit-confusion phrasing only."""

    if _DECISION_ONLY_PATTERN.search(text):
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (
        _WHAT_DOES_MEAN_PATTERN,
        _DO_NOT_UNDERSTAND_PATTERN,
        _QUOTED_CONFUSION_PATTERN,
        _TERM_CONFUSION_PATTERN,
    ):
        for match in pattern.finditer(text):
            term = match.group("term").strip(" \"'“”‘’?.:")
            if not term:
                continue
            key = term.casefold()
            if key not in seen:
                found.append(term)
                seen.add(key)
    return tuple(found)


def _term_matches(query: str, glossary_term: GlossaryTerm) -> bool:
    query_key = query.casefold()
    candidates = (glossary_term.term, *glossary_term.aliases)
    return any(query_key == candidate.casefold() for candidate in candidates)


def _matching_terms(
    confused_terms: Iterable[str],
    manifests: Iterable[GlossaryManifest],
) -> tuple[tuple[GlossaryManifest, tuple[GlossaryTerm, ...]], ...]:
    matches: list[tuple[GlossaryManifest, tuple[GlossaryTerm, ...]]] = []
    for manifest in manifests:
        terms: list[GlossaryTerm] = []
        for confused_term in confused_terms:
            for glossary_term in manifest.glossary_terms:
                if _term_matches(confused_term, glossary_term) and glossary_term not in terms:
                    terms.append(glossary_term)
                    break
            if len(terms) >= MAX_INJECTED_TERMS:
                break
        if terms:
            matches.append((manifest, tuple(terms)))
    return tuple(matches)


def select_glossary_injection(
    *,
    context: InterviewTurnContext | None = None,
    user_text: str = "",
    base_question_answered: bool,
    registry: BuiltinGlossaryRegistry | None = None,
) -> GlossaryInjection | None:
    """Select one bounded glossary injection for this turn.

    V1 never activates on the start turn and never uses vocabulary density or
    broad domain inference. Only explicit structured confusion or conservative
    text confusion can activate a pack.
    """

    if not base_question_answered:
        return None
    terms = context.confused_terms if context is not None else ()
    if not terms and user_text:
        terms = detect_explicit_confusion_terms(user_text)
    if not terms:
        return None
    active_registry = registry or builtin_registry()
    matches = _matching_terms(terms, active_registry.manifests)
    if not matches:
        return None
    manifest, glossary_terms = matches[0]
    return GlossaryInjection(pack_name=manifest.name, terms=glossary_terms[:MAX_INJECTED_TERMS])


__all__ = [
    "GlossaryInjection",
    "MAX_INJECTED_TERMS",
    "detect_explicit_confusion_terms",
    "select_glossary_injection",
]
