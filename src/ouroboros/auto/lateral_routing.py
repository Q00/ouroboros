"""Deterministic persona routing for the UNSTUCK_LATERAL phase.

RFC #809 Phase 2.2 classifies a QA failure (differences + suggestions text
returned by ``ouroboros_qa``) into one of four
:class:`~ouroboros.resilience.stagnation.StagnationPattern` buckets via pure
Python keyword matching, then defers to the existing
:func:`~ouroboros.resilience.recovery.suggest_lateral_persona_for_pattern`
to pick the most affinity-matched persona. The classification is fully
deterministic — same input always yields the same persona — so the resume
contract holds without persisting an extra "selected persona" hint that
could drift from the classifier's output.

Pattern mapping (in priority order, first match wins):

* **SPINNING** — "tool/path/environment unavailable", "command not found",
  "repeated same error". Affinity: ``hacker`` (finds workarounds).
* **OSCILLATION** — "ambiguous", "unclear", "conflicting requirements",
  "alternating outputs". Affinity: ``architect`` (restructures).
* **NO_DRIFT** — "missing context", "can't determine", "no information",
  "unknown". Affinity: ``researcher`` (seeks information).
* **DIMINISHING_RETURNS** — "over-engineered", "too complex", "unnecessary
  abstraction". Affinity: ``simplifier`` (reduces complexity).
* No keyword match → fall back to :class:`StagnationPattern.SPINNING` which
  is the most common QA-fail shape (the run produced an output but it
  didn't satisfy the AC for some reason the QA judge could not categorize).
  The persona selector then routes that to ``hacker`` and finally
  ``contrarian`` if hacker was already tried.
"""

from __future__ import annotations

from collections.abc import Sequence
import re

from ouroboros.resilience.lateral import ThinkingPersona
from ouroboros.resilience.stagnation import StagnationPattern

# Explicit pattern → persona mapping for QA-failure routing. Distinct from
# the shared ``suggest_lateral_persona_for_pattern`` (which iterates affinity
# tuples in declaration order and would route DIMINISHING_RETURNS to the
# wrong persona for our use case). The mapping mirrors the per-pattern
# docstring guidance on ``ThinkingPersona`` itself.
_PATTERN_PERSONA: dict[StagnationPattern, ThinkingPersona] = {
    StagnationPattern.SPINNING: ThinkingPersona.HACKER,
    StagnationPattern.OSCILLATION: ThinkingPersona.ARCHITECT,
    StagnationPattern.NO_DRIFT: ThinkingPersona.RESEARCHER,
    StagnationPattern.DIMINISHING_RETURNS: ThinkingPersona.SIMPLIFIER,
}

# Compiled regex banks per pattern. Each list runs ``re.search`` against the
# joined lowercased text of QA differences + suggestions. First pattern with
# a match wins.
_SPINNING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(xcode|tool|binary|command|cli|sdk|simulator)\b.*\b(unavailable|not (found|installed|available)|missing|not present)\b"
    ),
    re.compile(r"\b(repeated|same|identical)\b.*\b(error|failure|output|result)\b"),
    re.compile(r"\bcannot (run|execute|invoke|launch|start)\b"),
    re.compile(r"\bblocked by (a |the )?missing\b"),
    re.compile(r"\bsame error\b"),
)

_OSCILLATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bambiguous\b"),
    re.compile(r"\bunclear (whether|how|if|requirement)\b"),
    re.compile(r"\bconflicting (requirements?|outputs?|expectations?)\b"),
    re.compile(r"\balternating\b"),
    re.compile(r"\b(flip[- ]?flop|back and forth)\b"),
)

_NO_DRIFT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmissing (context|information|documentation|docs?)\b"),
    re.compile(r"\bcan(?:not|'t) determine\b"),
    re.compile(r"\bno (information|context|evidence|signal)\b"),
    re.compile(r"\bunknown (tool|behavior|expectation)\b"),
    re.compile(r"\bneed (more|additional) (context|information|details)\b"),
)

_DIMINISHING_RETURNS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bover[- ]?engineered\b"),
    re.compile(r"\btoo (complex|complicated|abstract)\b"),
    re.compile(r"\bunnecessary (abstraction|layer|complexity)\b"),
    re.compile(r"\bscope (creep|too broad|exceeds)\b"),
    re.compile(r"\bsimplif(y|ication)\b"),
)


def classify_qa_failure_to_pattern(
    differences: Sequence[str], suggestions: Sequence[str]
) -> StagnationPattern:
    """Classify a QA failure's free-form text into a stagnation pattern.

    The match is first-in-priority (SPINNING > OSCILLATION > NO_DRIFT >
    DIMINISHING_RETURNS). A miss returns ``SPINNING`` because that is the
    most common QA-fail shape — "the run produced something that didn't
    meet the bar but we can't tell why categorically". The persona
    selector then takes it from there.
    """
    haystack = " ".join((*differences, *suggestions)).lower()
    if not haystack.strip():
        return StagnationPattern.SPINNING
    if any(pattern.search(haystack) for pattern in _SPINNING_PATTERNS):
        return StagnationPattern.SPINNING
    if any(pattern.search(haystack) for pattern in _OSCILLATION_PATTERNS):
        return StagnationPattern.OSCILLATION
    if any(pattern.search(haystack) for pattern in _NO_DRIFT_PATTERNS):
        return StagnationPattern.NO_DRIFT
    if any(pattern.search(haystack) for pattern in _DIMINISHING_RETURNS_PATTERNS):
        return StagnationPattern.DIMINISHING_RETURNS
    return StagnationPattern.SPINNING


_FALLBACK_CHAIN: tuple[ThinkingPersona, ...] = (
    ThinkingPersona.CONTRARIAN,
    ThinkingPersona.HACKER,
    ThinkingPersona.ARCHITECT,
    ThinkingPersona.RESEARCHER,
    ThinkingPersona.SIMPLIFIER,
)


def select_persona_for_qa_failure(
    differences: Sequence[str],
    suggestions: Sequence[str],
    *,
    already_tried_personas: Sequence[ThinkingPersona] = (),
) -> ThinkingPersona | None:
    """Pick a persona for a QA failure, excluding already-tried personas.

    RFC #809 Phase 2.2b — the closed-loop recovery dispatcher invokes a
    different persona on each EVALUATE → UNSTUCK_LATERAL round (the
    "each persona may be invoked at most once per evaluate session"
    guard). The pipeline persists every routed persona in
    ``AutoPipelineState.personas_invoked`` and forwards the deduplicated
    set here as ``already_tried_personas``.

    Selection order:

    1. The pattern-based primary persona (HACKER/ARCHITECT/RESEARCHER/
       SIMPLIFIER) chosen by :func:`classify_qa_failure_to_pattern`.
    2. CONTRARIAN as the universal fallback.
    3. Any remaining persona from the deterministic fallback chain
       (HACKER → ARCHITECT → RESEARCHER → SIMPLIFIER), so the loop can
       keep exploring distinct angles after the obvious two have been
       tried.

    Returns ``None`` when every persona in steps 1–3 is already in
    ``already_tried_personas``. The caller (``pipeline._run_lateral`` in
    P2.2b) transitions to ``BLOCKED`` with a "personas exhausted"
    reason rather than picking a stale persona.
    """
    tried = tuple(already_tried_personas)
    pattern = classify_qa_failure_to_pattern(differences, suggestions)
    primary = _PATTERN_PERSONA[pattern]
    if primary not in tried:
        return primary
    for fallback in _FALLBACK_CHAIN:
        if fallback not in tried:
            return fallback
    return None


__all__ = [
    "classify_qa_failure_to_pattern",
    "select_persona_for_qa_failure",
]
