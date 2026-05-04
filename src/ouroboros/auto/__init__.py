"""Auto-mode convergence primitives for ``ooo auto``.

The auto package is intentionally independent from the existing manual
``interview``/``seed``/``run`` surfaces.  It provides bounded, serializable
state plus deterministic quality gates that a higher-level supervisor can use
before starting execution.
"""

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
from ouroboros.auto.grading import GradeGate, GradeResult, SeedGrade
from ouroboros.auto.ledger import LedgerEntry, LedgerSection, SeedDraftLedger
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoPolicy, AutoStore

__all__ = [
    "AutoAnswer",
    "AutoAnswerSource",
    "AutoAnswerer",
    "AutoPhase",
    "AutoPipelineState",
    "AutoPolicy",
    "AutoStore",
    "GradeGate",
    "GradeResult",
    "LedgerEntry",
    "LedgerSection",
    "SeedDraftLedger",
    "SeedGrade",
]
