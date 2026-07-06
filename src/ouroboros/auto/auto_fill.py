"""Aggressive LLM auto-fill substrate for non-converging interviews.

RFC #1256 §I3 (#1263 PR-1). When the interview cannot converge — max rounds
reached, ambiguity oscillating, phase deadline approaching — the engine must
aggressively fill the remaining open ledger slots through inference rather than
declaring non-convergence as a terminal cause. The user's absence of an answer
is never a sufficient reason to halt: every ``ooo auto`` session should be able
to reach a seed-ready ledger.

This module is the **pure substrate primitive only** and is intentionally NOT
wired into the interview driver loop yet (that is #1263 PR-2). It takes an
injected ``fill_slot`` callable (the LLM call) so it performs no IO itself and
is therefore safe to unit test and to run inside the bounded interview loop
without risking a hang.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)

# Documented confidence floor for an auto-filled slot whose proposal carries no
# usable confidence of its own (RFC #1256 §I3).
_DEFAULT_AUTO_FILL_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class AutoFillProposal:
    """One inference-proposed fill for a single open ledger section.

    ``value`` is the best-guess content for the section. ``confidence`` is
    carried onto the ledger entry; a non-positive value is replaced by the
    documented 0.5 floor so a model that declines to score still produces an
    audited, low-confidence default. ``rationale`` is optional provenance text.
    """

    value: str
    confidence: float = _DEFAULT_AUTO_FILL_CONFIDENCE
    rationale: str = ""


# ``fill_slot(section_name, ledger)`` returns a proposal for the named open
# section, or ``None`` to decline (the caller's next closure-ladder rung — RFC
# #1256 §I2 ``partial_seed_from_evidence`` — then handles the still-open gap).
FillSlot = Callable[[str, SeedDraftLedger], "AutoFillProposal | None"]


def auto_fill_remaining(ledger: SeedDraftLedger, *, fill_slot: FillSlot) -> list[str]:
    """Fill open ledger gaps via injected inference so a non-converging interview
    can still reach a seed-ready ledger.

    Iterates the ledger's currently-open required sections (a snapshot taken
    before any mutation, so each originally-open gap is attempted exactly once)
    and asks ``fill_slot`` for a best-guess value. Each accepted proposal is
    appended as a single :class:`LedgerEntry` with
    ``source = LedgerSource.AUTO_FILL_INFERENCE`` and
    ``status = LedgerStatus.DEFAULTED``; its confidence is carried from the
    proposal (clamped by ``LedgerEntry`` to ``[0, 1]``, floored at 0.5 when the
    proposal omits it).

    The ledger is mutated in place. Returns the section names that were actually
    *resolved* by the fill (a proposal that does not clear the gap — e.g. the
    section is still BLOCKED/CONFLICTING — is not reported). Sections the filler
    declines (``None`` or blank value) are left open for the caller's next rung.

    This function performs no IO and makes no model calls of its own; all
    inference is delegated to ``fill_slot``.
    """
    filled: list[str] = []
    for section_name in ledger.open_gaps():
        proposal = fill_slot(section_name, ledger)
        if proposal is None:
            continue
        value = proposal.value.strip()
        if not value:
            continue
        confidence = proposal.confidence
        if confidence <= 0.0:
            confidence = _DEFAULT_AUTO_FILL_CONFIDENCE
        ledger.add_entry(
            section_name,
            LedgerEntry(
                key=f"{section_name}.auto_fill_inference",
                value=value,
                source=LedgerSource.AUTO_FILL_INFERENCE,
                confidence=confidence,
                status=LedgerStatus.DEFAULTED,
                reversible=True,
                # Deadline non-convergence backstop → the exact #1485 origin;
                # stamp it distinctly so the histogram and gate see it as a
                # timeout default rather than an ordinary model inference.
                provenance=DecisionProvenance.TIMEOUT_DEFAULT,
                rationale=(
                    proposal.rationale
                    or "Auto-filled by inference (RFC #1256 §I3) because the "
                    "interview did not converge before its deadline."
                ),
            ),
        )
        if section_name not in ledger.open_gaps():
            filled.append(section_name)
    return filled
