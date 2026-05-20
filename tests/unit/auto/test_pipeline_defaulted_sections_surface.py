"""Tests that AutoPipelineResult surfaces defaulted_sections from the ledger.

Verifies that the ``defaulted_sections`` field on ``AutoPipelineResult`` is
correctly populated from ``ledger.summary()["defaulted_sections"]`` and that it
excludes entries with statuses other than DEFAULTED.
"""

from __future__ import annotations

from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.state import AutoPipelineState


def _make_pipeline() -> AutoPipeline:
    return AutoPipeline(
        interview_driver=None,  # type: ignore[arg-type]
        seed_generator=lambda _goal: None,  # type: ignore[arg-type]
    )


def _make_state(goal: str = "Build a local CLI") -> AutoPipelineState:
    return AutoPipelineState(goal=goal, cwd="/tmp/proj")


def _defaulted_entry(section: str) -> LedgerEntry:
    return LedgerEntry(
        key=f"{section}.test",
        value=f"Safe default value for {section}",
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.85,
        status=LedgerStatus.DEFAULTED,
    )


def _confirmed_entry(section: str) -> LedgerEntry:
    return LedgerEntry(
        key=f"{section}.test",
        value=f"Confirmed value for {section}",
        source=LedgerSource.USER_GOAL,
        confidence=0.95,
        status=LedgerStatus.CONFIRMED,
    )


def _weak_entry(section: str) -> LedgerEntry:
    return LedgerEntry(
        key=f"{section}.test",
        value=f"Weak value for {section}",
        source=LedgerSource.ASSUMPTION,
        confidence=0.3,
        status=LedgerStatus.WEAK,
    )


def _inferred_entry(section: str) -> LedgerEntry:
    return LedgerEntry(
        key=f"{section}.test",
        value=f"Inferred value for {section}",
        source=LedgerSource.INFERENCE,
        confidence=0.7,
        status=LedgerStatus.INFERRED,
    )


# ---------------------------------------------------------------------------
# Test 1: no DEFAULTED entries → defaulted_sections is empty
# ---------------------------------------------------------------------------


def test_envelope_omits_defaulted_sections_when_none_defaulted() -> None:
    """Result defaulted_sections is empty when ledger has no DEFAULTED entries."""
    pipeline = _make_pipeline()
    state = _make_state()
    ledger = SeedDraftLedger.from_goal(state.goal)

    # Add only CONFIRMED entries — no DEFAULTED ones.
    ledger.add_entry("actors", _confirmed_entry("actors"))
    ledger.add_entry("inputs", _confirmed_entry("inputs"))

    result = pipeline._result(state, ledger)

    assert isinstance(result, AutoPipelineResult)
    assert result.defaulted_sections == ()


# ---------------------------------------------------------------------------
# Test 2: DEFAULTED entries → defaulted_sections surfaces them
# ---------------------------------------------------------------------------


def test_envelope_surfaces_defaulted_sections_when_safe_default_filled() -> None:
    """Result defaulted_sections contains sections filled by safe-default policy."""
    pipeline = _make_pipeline()
    state = _make_state()
    ledger = SeedDraftLedger.from_goal(state.goal)

    ledger.add_entry("runtime_context", _defaulted_entry("runtime_context"))
    ledger.add_entry("failure_modes", _defaulted_entry("failure_modes"))

    result = pipeline._result(state, ledger)

    assert set(result.defaulted_sections) == {"runtime_context", "failure_modes"}


# ---------------------------------------------------------------------------
# Test 3: mixed ledger → only DEFAULTED sections appear
# ---------------------------------------------------------------------------


def test_envelope_defaulted_sections_excludes_confirmed_and_weak() -> None:
    """defaulted_sections contains only the DEFAULTED section, not CONFIRMED/WEAK/INFERRED."""
    pipeline = _make_pipeline()
    state = _make_state()
    ledger = SeedDraftLedger.from_goal(state.goal)

    ledger.add_entry("actors", _confirmed_entry("actors"))
    ledger.add_entry("inputs", _weak_entry("inputs"))
    ledger.add_entry("runtime_context", _defaulted_entry("runtime_context"))
    ledger.add_entry("outputs", _inferred_entry("outputs"))

    result = pipeline._result(state, ledger)

    assert result.defaulted_sections == ("runtime_context",)
