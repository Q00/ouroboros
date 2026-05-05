"""Auto-mode convergence primitives for ``ooo auto``.

The auto package is intentionally independent from the existing manual
``interview``/``seed``/``run`` surfaces.  It provides bounded, serializable
state plus deterministic quality gates that a higher-level supervisor can use
before starting execution.
"""

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
from ouroboros.auto.grading import GradeGate, GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewDriver, AutoInterviewResult, InterviewTurn
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
