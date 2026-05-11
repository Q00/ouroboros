"""Tests for AutoAnswerer domain-profile routing (#809 P3, PR 4/6).

Verifies the dual-path behavior introduced by PR-4:
- With an active profile, intent classification, vague-term detection, and
  verifiable-predicate repair all delegate to the profile.
- Without a profile (``active_profile=None``) the hardcoded coding-domain
  safety hatch runs verbatim.

All fixtures are tiny inline stubs; CODING_PROFILE is intentionally NOT
imported because PR-2 may not be merged yet.
"""

from __future__ import annotations

from ouroboros.auto.answerer import AutoAnswerer
from ouroboros.auto.domain_profile import DomainProfile  # noqa: F401
from ouroboros.auto.ledger import SeedDraftLedger

# ---------------------------------------------------------------------------
# Inline stubs — no real profile imported
# ---------------------------------------------------------------------------


class _AlwaysVerificationClassifier:
    """Classifies every question as 'verification'."""

    def classify(self, question: str) -> str | None:
        return "verification"

    def supported_intents(self) -> frozenset[str]:
        return frozenset({"verification"})


class _NeverClassifier:
    """Always returns None — no intent recognised."""

    def classify(self, question: str) -> str | None:
        return None

    def supported_intents(self) -> frozenset[str]:
        return frozenset()


class _ContrastPredicate:
    """Matches questions mentioning 'contrast'; repair uses WCAG language."""

    code = "wcag_contrast"

    def matches(self, criterion: str) -> bool:
        return "contrast" in criterion.lower()

    def repair_template(self, criterion: str) -> str:
        return f"Contrast ratio must be ≥ 4.5:1 for: {criterion}"


class _ExitCodePredicate:
    """Matches questions mentioning 'exit'; repair uses exit-code language."""

    code = "exit_code"

    def matches(self, criterion: str) -> bool:
        return "exit" in criterion.lower()

    def repair_template(self, criterion: str) -> str:
        return f"Command must exit 0 for: {criterion}"


class _NoopExtractor:
    def extract(self, cwd):  # type: ignore[override]
        return {}


def _never_detector(cwd):  # type: ignore[override]
    return 0.0


def _make_profile(
    *,
    classifier=None,
    predicates=(),
    vague_terms=frozenset(),
    name="test",
) -> DomainProfile:
    return DomainProfile(
        name=name,
        repo_context_extractor=_NoopExtractor(),
        verifiable_predicates=tuple(predicates),
        intent_classifier=classifier or _NeverClassifier(),
        vague_terms=frozenset(vague_terms),
        safe_defaults={},
        detector=_never_detector,
    )


def _ledger() -> SeedDraftLedger:
    return SeedDraftLedger.from_goal("Build a feature")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClassifyRoutesThroughProfileWhenActive:
    """Profile classifier is consulted when active_profile is set."""

    def test_classify_routes_through_profile_when_active(self):
        profile = _make_profile(classifier=_AlwaysVerificationClassifier())
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        # Any question should be routed to _verification_answer via the profile
        answer = answerer.answer("What runtime do you use?", ledger)
        # Verification answer has CONSERVATIVE_DEFAULT source
        assert answer.source.value == "conservative_default"
        # The answer text mentions verifiable/observable behavior
        assert "verif" in answer.text.lower() or "observ" in answer.text.lower()

    def test_classify_falls_back_to_hardcoded_when_no_profile(self):
        answerer = AutoAnswerer(active_profile=None)
        ledger = _ledger()
        # A verification question should still be routed via hardcoded path
        answer = answerer.answer("How should we verify the tests pass?", ledger)
        assert answer.source.value == "conservative_default"
        assert "verif" in answer.text.lower() or "observ" in answer.text.lower()

    def test_unknown_intent_returns_default_answer(self):
        profile = _make_profile(classifier=_NeverClassifier())
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        answer = answerer.answer("Something completely unrelated xyzzy", ledger)
        # Falls through to _default_answer: generic_default=True
        assert answer.generic_default is True


class TestVagueTermLookupUsesProfile:
    """Profile vague_terms set is consulted when active_profile is set."""

    def test_vague_term_triggers_verification_route(self):
        profile = _make_profile(
            classifier=_NeverClassifier(),  # would return default without vague-term
            vague_terms={"easy", "clean"},
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        # "clean" appears in question — should inject VERIFICATION intent
        answer = answerer.answer("The UI should look clean and easy to use", ledger)
        # Routed to _verification_answer, not _default_answer
        assert answer.generic_default is False

    def test_no_vague_term_no_injection(self):
        profile = _make_profile(
            classifier=_NeverClassifier(),
            vague_terms={"easy", "clean"},
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        answer = answerer.answer("Something completely unrelated xyzzy", ledger)
        assert answer.generic_default is True

    def test_vague_term_does_not_match_inside_larger_word(self):
        profile = _make_profile(
            classifier=_NeverClassifier(),
            vague_terms={"easy", "clean"},
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()

        answer = answerer.answer("How should we cleanup the easiest path?", ledger)

        assert answer.generic_default is True

    def test_vague_term_absent_without_profile(self):
        """Without a profile, vague-term injection never happens."""
        answerer = AutoAnswerer(active_profile=None)
        ledger = _ledger()
        # "clean" alone with no profile should fall to default (not verification)
        answer = answerer.answer("The code should be clean", ledger)
        # Hardcoded path: no vague-term logic — routes to default
        assert answer.generic_default is True


class TestVerifiablePredicateIterationPicksFirstMatch:
    """Profile predicates are iterated and the first match is used."""

    def test_verifiable_predicate_iteration_picks_first_match(self):
        # _ContrastPredicate matches "contrast", _ExitCodePredicate matches "exit"
        profile = _make_profile(
            classifier=_AlwaysVerificationClassifier(),
            predicates=[_ContrastPredicate(), _ExitCodePredicate()],
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        # "contrast" question — first predicate matches
        answer = answerer.answer("Does the UI contrast meet accessibility standards?", ledger)
        # The AC ledger entry should use the contrast predicate repair_template
        ac_entries = [
            entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
        ]
        assert ac_entries, "Expected an acceptance_criteria ledger entry"
        assert "4.5:1" in ac_entries[0].value

    def test_second_predicate_used_when_first_does_not_match(self):
        profile = _make_profile(
            classifier=_AlwaysVerificationClassifier(),
            predicates=[_ContrastPredicate(), _ExitCodePredicate()],
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        # "exit" question — first predicate does NOT match, second does
        answer = answerer.answer("The command should exit successfully", ledger)
        ac_entries = [
            entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
        ]
        assert ac_entries
        assert "exit 0" in ac_entries[0].value

    def test_no_predicate_match_uses_hardcoded_fallback(self):
        profile = _make_profile(
            classifier=_AlwaysVerificationClassifier(),
            predicates=[_ContrastPredicate()],  # only contrast predicate
        )
        answerer = AutoAnswerer(active_profile=profile)
        ledger = _ledger()
        # No predicate matches "runtime behavior" question
        answer = answerer.answer("What runtime behavior verifies the feature?", ledger)
        ac_entries = [
            entry for section, entry in answer.ledger_updates if section == "acceptance_criteria"
        ]
        assert ac_entries
        # Falls back to hardcoded "exit code 0" text
        assert "exit code 0" in ac_entries[0].value
