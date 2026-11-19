"""Auto-mode convergence primitives for ``ooo auto``.

The auto package is intentionally independent from the existing manual
``interview``/``seed``/``run`` surfaces.  It provides bounded, serializable
state, the auto interview, and the pipeline that writes a Seed and hands it to
the run.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
    from ouroboros.auto.interview_driver import (
        AutoInterviewDriver,
        AutoInterviewResult,
        InterviewTurn,
    )
    from ouroboros.auto.ledger import (
        AssumptionRecord,
        LedgerEntry,
        LedgerSection,
        SeedDraftLedger,
    )
    from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
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
    "AssumptionRecord",
    "InterviewTurn",
    "LedgerEntry",
    "LedgerSection",
    "SeedDraftLedger",
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
    "AssumptionRecord": "ouroboros.auto.ledger",
    "InterviewTurn": "ouroboros.auto.interview_driver",
    "LedgerEntry": "ouroboros.auto.ledger",
    "LedgerSection": "ouroboros.auto.ledger",
    "SeedDraftLedger": "ouroboros.auto.ledger",
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
