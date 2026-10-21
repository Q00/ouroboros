"""Acceptance state shared by the evaluation pipeline and the lineage read model.

It lives in ``core`` so that ``core.lineage`` (below ``evaluation`` in the
import graph) can expose the same enum the pipeline produces, and every
display surface reads one tri-state instead of re-deriving it.
"""

from __future__ import annotations

from enum import StrEnum


class AcceptanceState(StrEnum):
    """Outcome of the acceptance decision for one evaluation.

    Attributes:
        APPROVED: Executed verification passed and no model review withheld.
        REJECTED: An executed check failed, or executed checks passed and a
            model review withheld approval.
        UNVERIFIED: No executed verification evidence exists (Stage 1 did not
            run, or ran no configured check). Model review may have been
            favorable; it cannot grant acceptance, so it is attached as
            feedback instead.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"
