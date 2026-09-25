"""EventStore-backed ordering guard for the check-package boundary.

A boundary is one (Seed, verifier source) slot, for example one task and one
check variant. Its lifecycle is enforced at write time:

1. exactly one seal: ``record_package_frozen`` (package SHA-256 persisted) or
   ``record_construction_failed``; a second package for the same boundary is
   refused, so admission feedback can never produce a regenerated package;
2. for a frozen package, exactly one ``record_admission`` whose receipt names
   the frozen digest, recorded before any actor starts;
3. ``record_actor_started`` refuses to start a worker until every boundary it
   binds is sealed (and, with a package, admitted), optionally refusing a
   workspace that contains generated check files;
4. ``record_candidate_verification`` and ``record_selection`` must cite the
   frozen digest.

``verify_boundary_order`` re-checks the same rules over replayed events for
audit and replay. The ledger assumes one writer per boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ouroboros.boundary.admission import AdmissionResult, CandidateVerification
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    SELECTION_DECIDED,
    actor_started_event,
    admission_completed_event,
    candidate_verified_event,
    construction_failed_event,
    package_frozen_event,
    selection_decided_event,
)
from ouroboros.boundary.package import (
    CheckPackage,
    find_workspace_leaks,
    validate_package_for_seed,
)
from ouroboros.boundary.selection import SelectionDecision
from ouroboros.core.errors import OuroborosError
from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore


class BoundaryOrderError(OuroborosError):
    """A boundary write would violate the seal, admission, or actor ordering."""


class BoundaryLeakError(BoundaryOrderError):
    """A worker workspace contains generated check files."""


def _utc(value: datetime) -> datetime:
    """Replayed SQLite timestamps are naive UTC; compare everything as aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _first(events: Sequence[BaseEvent], event_type: str) -> BaseEvent | None:
    return next((event for event in events if event.type == event_type), None)


class BoundaryLedger:
    """Write-time ordering guard over an initialized ``EventStore``."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    async def events(self, boundary_id: str) -> list[BaseEvent]:
        """Replay one boundary's events in journal order."""
        return await self._store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)

    async def _require_unsealed(self, boundary_id: str) -> None:
        events = await self.events(boundary_id)
        if _first(events, PACKAGE_FROZEN) or _first(events, CONSTRUCTION_FAILED):
            raise BoundaryOrderError(
                "boundary already sealed; a package cannot be regenerated or replaced",
                details={"boundary_id": boundary_id},
            )

    async def record_package_frozen(
        self,
        boundary_id: str,
        package: CheckPackage,
        *,
        seed: Seed | None = None,
    ) -> BaseEvent:
        """Persist the package SHA-256 and manifest; the boundary's only seal."""
        if seed is not None:
            validate_package_for_seed(package, seed)
        await self._require_unsealed(boundary_id)
        event = package_frozen_event(boundary_id, package)
        await self._store.append(event)
        return event

    async def record_construction_failed(
        self,
        boundary_id: str,
        *,
        seed_digest: str,
        input_digest: str,
        reason: str,
    ) -> BaseEvent:
        """Seal a boundary whose generation produced no valid package."""
        await self._require_unsealed(boundary_id)
        event = construction_failed_event(
            boundary_id, seed_digest=seed_digest, input_digest=input_digest, reason=reason
        )
        await self._store.append(event)
        return event

    async def record_admission(self, boundary_id: str, result: AdmissionResult) -> BaseEvent:
        """Persist the single whole-package admission receipt."""
        events = await self.events(boundary_id)
        frozen = _first(events, PACKAGE_FROZEN)
        if frozen is None:
            raise BoundaryOrderError(
                "admission requires a frozen package", details={"boundary_id": boundary_id}
            )
        if frozen.data.get("package_sha256") != result.package_sha256:
            raise BoundaryOrderError(
                "admission receipt names a different package",
                details={"boundary_id": boundary_id},
            )
        if _first(events, ADMISSION_COMPLETED) is not None:
            raise BoundaryOrderError(
                "admission already recorded", details={"boundary_id": boundary_id}
            )
        if _first(events, ACTOR_STARTED) is not None:
            raise BoundaryOrderError(
                "admission must be recorded before any actor starts",
                details={"boundary_id": boundary_id},
            )
        event = admission_completed_event(boundary_id, result)
        await self._store.append(event)
        return event

    async def record_actor_started(
        self,
        actor_id: str,
        boundary_ids: Sequence[str],
        *,
        workspace: Path | None = None,
        runtime: str | None = None,
    ) -> list[BaseEvent]:
        """Record a worker start on every boundary it is bound to.

        Raises ``BoundaryOrderError`` unless each boundary is sealed and, when
        it holds a package, admitted. Raises ``BoundaryLeakError`` when
        ``workspace`` contains a generated check file (by path or by content
        digest). Call this before launching the worker; launch only on success.
        """
        if not boundary_ids:
            raise BoundaryOrderError("an actor must be bound to at least one boundary")
        seals: list[BaseEvent] = []
        manifests: list[dict] = []
        package_by_boundary: dict[str, str | None] = {}
        for boundary_id in boundary_ids:
            events = await self.events(boundary_id)
            frozen = _first(events, PACKAGE_FROZEN)
            failed = _first(events, CONSTRUCTION_FAILED)
            admitted = _first(events, ADMISSION_COMPLETED)
            if frozen is None and failed is None:
                raise BoundaryOrderError(
                    "actor cannot start before the boundary is sealed",
                    details={"boundary_id": boundary_id, "actor_id": actor_id},
                )
            if frozen is not None and admitted is None:
                raise BoundaryOrderError(
                    "actor cannot start before package admission is recorded",
                    details={"boundary_id": boundary_id, "actor_id": actor_id},
                )
            seals.extend(event for event in (frozen, failed, admitted) if event is not None)
            if frozen is not None:
                manifests.append(frozen.data["manifest"])
                package_by_boundary[boundary_id] = frozen.data["package_sha256"]
            else:
                package_by_boundary[boundary_id] = None
        if workspace is not None:
            leaks = find_workspace_leaks(workspace, manifests)
            if leaks:
                raise BoundaryLeakError(
                    "worker workspace contains generated check files",
                    details={"actor_id": actor_id, "paths": list(leaks)},
                )
        started = [
            actor_started_event(
                boundary_id,
                actor_id=actor_id,
                package_sha256=package_by_boundary[boundary_id],
                runtime=runtime,
            )
            for boundary_id in boundary_ids
        ]
        latest_seal = max(_utc(event.timestamp) for event in seals)
        if any(_utc(event.timestamp) <= latest_seal for event in started):
            raise BoundaryOrderError(
                "actor start timestamp does not follow the boundary seal",
                details={"actor_id": actor_id},
            )
        await self._store.append_batch(started)
        return started

    async def record_candidate_verification(
        self, boundary_id: str, verification: CandidateVerification
    ) -> BaseEvent:
        """Persist a candidate run of the frozen package."""
        events = await self.events(boundary_id)
        frozen = _first(events, PACKAGE_FROZEN)
        if frozen is None or _first(events, ADMISSION_COMPLETED) is None:
            raise BoundaryOrderError(
                "candidate verification requires a frozen, admitted package",
                details={"boundary_id": boundary_id},
            )
        if frozen.data.get("package_sha256") != verification.package_sha256:
            raise BoundaryOrderError(
                "verification ran a package other than the frozen one",
                details={"boundary_id": boundary_id},
            )
        event = candidate_verified_event(boundary_id, verification)
        await self._store.append(event)
        return event

    async def record_selection(self, boundary_id: str, decision: SelectionDecision) -> BaseEvent:
        """Persist the selection reason and all digests."""
        events = await self.events(boundary_id)
        frozen = _first(events, PACKAGE_FROZEN)
        if frozen is None or frozen.data.get("package_sha256") != decision.package_sha256:
            raise BoundaryOrderError(
                "selection must cite the boundary's frozen package",
                details={"boundary_id": boundary_id},
            )
        event = selection_decided_event(boundary_id, decision)
        await self._store.append(event)
        return event


def verify_boundary_order(events: Sequence[BaseEvent]) -> tuple[str, ...]:
    """Return ordering violations in one boundary's replayed events.

    An empty result means: one seal, the seal precedes admission, admission
    precedes every actor start, and every receipt cites the frozen digest.
    """
    violations: list[str] = []
    seals = [e for e in events if e.type in {PACKAGE_FROZEN, CONSTRUCTION_FAILED}]
    if len(seals) != 1:
        violations.append(f"expected exactly one seal, found {len(seals)}")
    seal = seals[0] if seals else None
    frozen_sha = seal.data.get("package_sha256") if seal and seal.type == PACKAGE_FROZEN else None
    admissions = [e for e in events if e.type == ADMISSION_COMPLETED]
    if frozen_sha is not None and len(admissions) != 1:
        violations.append(f"expected exactly one admission, found {len(admissions)}")
    if frozen_sha is None and admissions:
        violations.append("admission recorded without a frozen package")
    position = {id(event): index for index, event in enumerate(events)}
    for event in events:
        if event.type in {ADMISSION_COMPLETED, CANDIDATE_VERIFIED, SELECTION_DECIDED}:
            if frozen_sha is None or event.data.get("package_sha256") != frozen_sha:
                violations.append(f"{event.type} does not cite the frozen package")
        if event.type == ACTOR_STARTED:
            if seal is None or position[id(seal)] > position[id(event)]:
                violations.append("actor started before the seal")
            elif _utc(seal.timestamp) >= _utc(event.timestamp):
                violations.append("actor start timestamp does not follow the seal")
            if frozen_sha is not None and (
                not admissions or position[id(admissions[0])] > position[id(event)]
            ):
                violations.append("actor started before admission")
    return tuple(violations)
