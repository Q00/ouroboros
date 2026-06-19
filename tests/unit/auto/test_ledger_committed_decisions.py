"""Tests for ``SeedDraftLedger.committed_decisions`` — the anchor snapshot the
answer refiner consumes to stay consistent across interview rounds."""

from __future__ import annotations

from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)


def _entry(key: str, value: str, status: LedgerStatus) -> LedgerEntry:
    return LedgerEntry(
        key=key,
        value=value,
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        status=status,
    )


def test_returns_active_decisions_excluding_goal() -> None:
    ledger = SeedDraftLedger.from_goal("build a todo CLI")
    ledger.add_entry("outputs", _entry("outputs.add", "Added #1: X", LedgerStatus.DEFAULTED))
    ledger.add_entry(
        "constraints", _entry("constraints.exit", "invalid exits 1", LedgerStatus.CONFIRMED)
    )

    decided = ledger.committed_decisions()

    assert ("outputs", "outputs.add", "Added #1: X") in decided
    assert ("constraints", "constraints.exit", "invalid exits 1") in decided
    # The goal echo is surfaced to the refiner separately, never as a contract.
    assert all(section != "goal" for section, _key, _value in decided)


def test_omits_weak_conflicting_and_blank_entries() -> None:
    ledger = SeedDraftLedger.from_goal("g")
    ledger.add_entry("outputs", _entry("outputs.weak", "superseded", LedgerStatus.WEAK))
    ledger.add_entry("outputs", _entry("outputs.conflict", "A", LedgerStatus.CONFLICTING))
    ledger.add_entry("outputs", _entry("outputs.blank", "   ", LedgerStatus.DEFAULTED))
    ledger.add_entry("outputs", _entry("outputs.ok", "keep me", LedgerStatus.DEFAULTED))

    values = {value for _s, _k, value in ledger.committed_decisions()}

    assert "keep me" in values
    assert "superseded" not in values
    assert "A" not in values
    assert "   " not in values


def test_dedupes_repeated_same_key_commitment() -> None:
    ledger = SeedDraftLedger.from_goal("g")
    ledger.add_entry("outputs", _entry("outputs.add", "Added #1: X", LedgerStatus.DEFAULTED))
    # The freeze backstop re-commits the SAME value on a later round
    # (matching-prior reconciliation keeps both active). The snapshot must
    # collapse them to a single contract, not report a duplicate.
    ledger.add_entry("outputs", _entry("outputs.add", "Added #1: X", LedgerStatus.DEFAULTED))

    matches = [
        v for s, k, v in ledger.committed_decisions() if (s, k) == ("outputs", "outputs.add")
    ]

    assert matches == ["Added #1: X"]
