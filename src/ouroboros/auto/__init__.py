"""Auto-mode convergence primitives for ``ooo auto``.

The auto package is intentionally independent from the existing manual
``interview``/``seed``/``run`` surfaces.  It provides bounded, serializable
state plus deterministic quality gates that a higher-level supervisor can use
before starting execution.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
    from ouroboros.auto.grading import GradeGate, GradeResult, SeedGrade
    from ouroboros.auto.interview_driver import (
        AutoInterviewDriver,
        AutoInterviewResult,
        InterviewTurn,
    )
    from ouroboros.auto.ledger import LedgerEntry, LedgerSection, SeedDraftLedger
    from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
    from ouroboros.auto.seed_repairer import RepairResult, SeedRepairer
    from ouroboros.auto.seed_reviewer import ReviewFinding, SeedReview, SeedReviewer
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoPolicy, AutoStore

__all__ = [
    "AutoAnswer",
    "AutoAnswerSource",
    "AutoAnswerer",
    "AutoInterviewDriver",
    "AutoInterviewResult",
    "AutoPhase",
    "AutoPipeline",
    "AutoPipelineResult",
    "AutoPipelineState",
    "AutoPolicy",
    "AutoStore",
    "GradeGate",
    "InterviewTurn",
    "GradeResult",
    "LedgerEntry",
    "LedgerSection",
    "RepairResult",
    "ReviewFinding",
    "SeedDraftLedger",
    "SeedReview",
    "SeedReviewer",
    "SeedGrade",
    "SeedRepairer",
]

_EXPORTS = {
    "AutoAnswer": "ouroboros.auto.answerer",
    "AutoAnswerSource": "ouroboros.auto.answerer",
    "AutoAnswerer": "ouroboros.auto.answerer",
    "AutoInterviewDriver": "ouroboros.auto.interview_driver",
    "AutoInterviewResult": "ouroboros.auto.interview_driver",
    "AutoPhase": "ouroboros.auto.state",
    "AutoPipeline": "ouroboros.auto.pipeline",
    "AutoPipelineResult": "ouroboros.auto.pipeline",
    "AutoPipelineState": "ouroboros.auto.state",
    "AutoPolicy": "ouroboros.auto.state",
    "AutoStore": "ouroboros.auto.state",
    "GradeGate": "ouroboros.auto.grading",
    "GradeResult": "ouroboros.auto.grading",
    "InterviewTurn": "ouroboros.auto.interview_driver",
    "LedgerEntry": "ouroboros.auto.ledger",
    "LedgerSection": "ouroboros.auto.ledger",
    "RepairResult": "ouroboros.auto.seed_repairer",
    "ReviewFinding": "ouroboros.auto.seed_reviewer",
    "SeedDraftLedger": "ouroboros.auto.ledger",
    "SeedGrade": "ouroboros.auto.grading",
    "SeedRepairer": "ouroboros.auto.seed_repairer",
    "SeedReview": "ouroboros.auto.seed_reviewer",
    "SeedReviewer": "ouroboros.auto.seed_reviewer",
}


def __getattr__(name: str) -> Any:
    """Lazily expose the public auto package API without eager submodule imports."""
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
