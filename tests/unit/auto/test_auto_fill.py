"""Unit tests for the §I3 aggressive auto-fill substrate (#1263 PR-1).

These exercise ``auto_fill_remaining`` in isolation with an injected stub filler;
the driver wiring (#1263 PR-2) and seed ``auto_filled_slots`` metadata (PR-3) are
out of scope here.
"""

from __future__ import annotations

from ouroboros.auto.auto_fill import AutoFillProposal, auto_fill_remaining
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)


def _ledger_with_open_gaps() -> SeedDraftLedger:
    # ``from_goal`` resolves "goal" (plus any explicitly hydrated sections); the
    # remaining required sections start MISSING and therefore show as open gaps.
    ledger = SeedDraftLedger.from_goal("Build a habit-tracker CLI")
    assert not ledger.is_seed_ready()
    assert ledger.open_gaps()
    return ledger


def test_auto_fill_fills_open_gaps_and_makes_ledger_seed_ready() -> None:
    ledger = _ledger_with_open_gaps()
    gaps_before = set(ledger.open_gaps())

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
        return AutoFillProposal(value=f"auto value for {section}", confidence=0.7)

    filled = auto_fill_remaining(ledger, fill_slot=fill_slot)

    assert set(filled) == gaps_before
    assert ledger.is_seed_ready()
    for section in filled:
        auto_entries = [
            e
            for e in ledger.sections[section].entries
            if e.source == LedgerSource.AUTO_FILL_INFERENCE
        ]
        assert len(auto_entries) == 1
        assert auto_entries[0].status == LedgerStatus.DEFAULTED
        assert auto_entries[0].confidence == 0.7
        assert auto_entries[0].key == f"{section}.auto_fill_inference"


def test_auto_fill_declined_slot_stays_open() -> None:
    ledger = _ledger_with_open_gaps()
    target = ledger.open_gaps()[0]

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal | None:
        return None if section == target else AutoFillProposal(value=f"value {section}")

    filled = auto_fill_remaining(ledger, fill_slot=fill_slot)

    assert target not in filled
    assert target in ledger.open_gaps()
    assert not ledger.is_seed_ready()


def test_auto_fill_blank_value_is_skipped() -> None:
    ledger = _ledger_with_open_gaps()

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
        return AutoFillProposal(value="   ")  # whitespace-only is not content

    filled = auto_fill_remaining(ledger, fill_slot=fill_slot)

    assert filled == []
    assert not ledger.is_seed_ready()


def test_auto_fill_confidence_floor_when_unscored() -> None:
    ledger = _ledger_with_open_gaps()

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
        return AutoFillProposal(value=f"value {section}", confidence=0.0)

    auto_fill_remaining(ledger, fill_slot=fill_slot)

    auto_entries = [
        e
        for section in ledger.sections.values()
        for e in section.entries
        if e.source == LedgerSource.AUTO_FILL_INFERENCE
    ]
    assert auto_entries
    assert all(e.confidence == 0.5 for e in auto_entries)


def test_auto_fill_entries_surface_as_assumption_sources() -> None:
    ledger = _ledger_with_open_gaps()

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
        return AutoFillProposal(value=f"value {section}", confidence=0.6)

    auto_fill_remaining(ledger, fill_slot=fill_slot)

    records = ledger.assumption_sources()
    assert any(r.source == LedgerSource.AUTO_FILL_INFERENCE.value for r in records)


def test_auto_fill_leaves_already_resolved_sections_untouched() -> None:
    ledger = _ledger_with_open_gaps()
    target = ledger.open_gaps()[0]
    ledger.add_entry(
        target,
        LedgerEntry(
            key=f"{target}.user",
            value="user provided",
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    assert target not in ledger.open_gaps()

    offered: list[str] = []

    def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
        offered.append(section)
        return AutoFillProposal(value=f"value {section}")

    auto_fill_remaining(ledger, fill_slot=fill_slot)

    # A section that is already resolved is never offered to the filler, and no
    # auto-fill entry is appended to it.
    assert target not in offered
    assert all(
        e.source != LedgerSource.AUTO_FILL_INFERENCE for e in ledger.sections[target].entries
    )
