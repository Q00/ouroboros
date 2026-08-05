"""Best-effort drift observation at the generation boundary."""

from __future__ import annotations

import logging

from ouroboros.core.seed import Seed
from ouroboros.observability.drift import DriftMeasuredEvent, DriftMeasurement
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)


async def record_generation_drift(
    event_store: EventStore,
    lineage_id: str,
    generation_number: int,
    seed: Seed,
    execution_output: str | None,
) -> None:
    """Persist drift telemetry without making observation a generation gate."""
    if not execution_output:
        return
    try:
        metrics = DriftMeasurement().measure(
            current_output=execution_output,
            constraint_violations=[],
            current_concepts=[],
            seed=seed,
        )
        await event_store.append(
            DriftMeasuredEvent(
                execution_id=lineage_id,
                seed_id=seed.metadata.seed_id,
                iteration=generation_number,
                metrics=metrics,
            )
        )
    except Exception as exc:  # noqa: BLE001 - telemetry cannot fail evolution
        logger.warning(
            "evolution.drift.measurement_failed",
            extra={"error": str(exc), "generation": generation_number},
        )


__all__ = ["record_generation_drift"]
