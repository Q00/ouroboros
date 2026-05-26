"""Unit tests for the safe-default unsafe-context lateral persona selector.

Issue #1248 — these tests pin the *selector contract* (which persona is
chosen at each chain position) for the safe-default unsafe-context
escalation. The selector itself remained substrate-only when it landed
in PR-A (#1250); the consuming behaviour shipped in PR-B (#1251), where
the interview driver invokes the selector at the matcher-fire site and
gates downstream ledger demotion on a machine-checkable clearance
marker. Tests covering the routed behaviour live in
``test_safe_default_lateral_escalation.py``; this file stays focused on
the deterministic CONTRARIAN → ARCHITECT chain order.
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
