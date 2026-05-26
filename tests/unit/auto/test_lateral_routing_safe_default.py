"""Unit tests for the safe-default unsafe-context lateral persona selector.

Issue #1248 — substrate-only PR (no behavior change). The selector picks
``ThinkingPersona`` values to escalate a ``_unsafe_context_reason()`` fire
through, instead of letting the safe-default closure die in place. These
tests pin the selection contract; the behavior PR that consumes the
selector lands separately.
"""

from __future__ import annotations

from ouroboros.auto.lateral_routing import select_persona_for_safe_default_block
from ouroboros.resilience.lateral import ThinkingPersona


def test_selects_contrarian_first_when_nothing_tried() -> None:
    """First escalation prefers CONTRARIAN — best fit for matcher false positives."""
    assert select_persona_for_safe_default_block() is ThinkingPersona.CONTRARIAN


def test_selects_architect_after_contrarian_tried() -> None:
    """Second escalation prefers ARCHITECT once CONTRARIAN is exhausted."""
    assert (
        select_persona_for_safe_default_block(
            already_tried_personas=(ThinkingPersona.CONTRARIAN,),
        )
        is ThinkingPersona.ARCHITECT
    )


def test_returns_none_when_chain_exhausted() -> None:
    """No further persona is offered after both escalation slots fire.

    The caller transitions to BLOCKED with ``unstuck_exhausted`` instead
    of recycling a stale persona.
    """
    assert (
        select_persona_for_safe_default_block(
            already_tried_personas=(
                ThinkingPersona.CONTRARIAN,
                ThinkingPersona.ARCHITECT,
            ),
        )
        is None
    )


def test_extra_unrelated_personas_in_history_do_not_advance_chain() -> None:
    """Irrelevant personas (HACKER/RESEARCHER/SIMPLIFIER) in history are ignored.

    The safe-default chain only consumes its own two slots — the chain is
    independent of the QA-failure chain so concurrent EVALUATE rounds do
    not bleed exhaustion state across.
    """
    assert (
        select_persona_for_safe_default_block(
            already_tried_personas=(
                ThinkingPersona.HACKER,
                ThinkingPersona.RESEARCHER,
                ThinkingPersona.SIMPLIFIER,
            ),
        )
        is ThinkingPersona.CONTRARIAN
    )


def test_selector_is_deterministic_across_calls() -> None:
    """Same input always yields the same output — required by the resume contract."""
    first = select_persona_for_safe_default_block()
    second = select_persona_for_safe_default_block()
    assert first is second


def test_order_of_already_tried_does_not_matter() -> None:
    """Selection is set-based, not order-based."""
    forward = select_persona_for_safe_default_block(
        already_tried_personas=(ThinkingPersona.CONTRARIAN,),
    )
    reverse = select_persona_for_safe_default_block(
        already_tried_personas=(ThinkingPersona.ARCHITECT,),
    )
    # CONTRARIAN tried -> ARCHITECT next; ARCHITECT tried -> CONTRARIAN next.
    assert forward is ThinkingPersona.ARCHITECT
    assert reverse is ThinkingPersona.CONTRARIAN
