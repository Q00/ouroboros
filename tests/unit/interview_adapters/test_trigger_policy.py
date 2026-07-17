from __future__ import annotations

from ouroboros.interview_adapters.models import InterviewTurnContext
from ouroboros.interview_adapters.triggers import (
    detect_explicit_confusion_terms,
    select_glossary_injection,
)


def test_default_turn_activates_no_pack() -> None:
    assert select_glossary_injection(base_question_answered=True) is None


def test_start_turn_confusion_is_queued_until_base_question_answered() -> None:
    context = InterviewTurnContext(confused_terms=("affordance",))

    assert select_glossary_injection(context=context, base_question_answered=False) is None
    injection = select_glossary_injection(context=context, base_question_answered=True)

    assert injection is not None
    assert [term.term for term in injection.terms] == ["affordance"]


def test_conservative_text_confusion_detection_activates() -> None:
    injection = select_glossary_injection(
        user_text='What does "affordance" mean?',
        base_question_answered=True,
    )

    assert injection is not None
    assert injection.terms[0].term == "affordance"


def test_common_unquoted_confusion_phrases_are_detected() -> None:
    assert detect_explicit_confusion_terms("What does affordance mean?") == ("affordance",)
    assert detect_explicit_confusion_terms("I do not understand affordance") == ("affordance",)


def test_similar_non_confusion_and_missing_decisions_do_not_activate() -> None:
    assert detect_explicit_confusion_terms("We need affordance in the UI.") == ()
    assert (
        select_glossary_injection(
            user_text="Should we choose hierarchy or a flat layout?",
            base_question_answered=True,
        )
        is None
    )


def test_at_most_three_matching_terms_are_injected() -> None:
    context = InterviewTurnContext(
        confused_terms=("affordance", "hierarchy", "information architecture", "microcopy")
    )

    injection = select_glossary_injection(context=context, base_question_answered=True)

    assert injection is not None
    assert [term.term for term in injection.terms] == [
        "affordance",
        "hierarchy",
        "information architecture",
    ]


def test_vocabulary_density_alone_does_not_activate() -> None:
    dense_text = "affordance hierarchy information architecture microcopy layout interaction"

    assert select_glossary_injection(user_text=dense_text, base_question_answered=True) is None
