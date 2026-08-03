"""Content-addressed storage and conservative GC for Disposable Memory.

Artifact bodies live outside the EventStore under
``.ouroboros/artifacts/<prefix>/<sha256>.json``.  Per-contract manifests keep
the replay reference and tombstone history without copying the body into the
main ledger.  Every mutation uses one cross-process store lock so a concurrent
reference cannot race a prune decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Final

from ouroboros.core.disposable_memory import (
    ARTIFACT_REF_PATTERN,
    DISPOSABLE_CONTRACT_ID_PATTERN,
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    DisposableResultEnvelope,
    DisposableResultStatus,
    DisposableResultSummary,
)
from ouroboros.core.errors import PersistenceError
from ouroboros.core.file_lock import file_lock

DEFAULT_ARTIFACT_TTL = timedelta(days=90)
DEFAULT_REPLAY_RETENTION = timedelta(days=90)
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_VERSION: Final[int] = 1
_MANIFEST_FILENAME: Final[str] = "events.json"


class ArtifactStoreError(PersistenceError):
    """Base error for deterministic artifact-store failures."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when a contract or content-addressed body does not exist."""


class ArtifactTombstonedError(ArtifactStoreError):
    """Raised when deterministic replay points at an intentionally pruned body."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when stored bytes do not match their content address."""


class ArtifactManifestError(ArtifactStoreError):
    """Raised when reachability cannot be established from a durable manifest."""


class ArtifactContractConflictError(ArtifactStoreError):
    """Raised when one contract id is reused for a different artifact."""


class ArtifactTooLargeError(ArtifactStoreError):
    """Raised when the encoded artifact exceeds the disposable output cap."""


@dataclass(frozen=True, slots=True)
class FetchedArtifact:
    """Explicit-fetch result.  This body never appears on the normal envelope."""

    envelope: DisposableResultEnvelope
    body: Any


@dataclass(frozen=True, slots=True)
class ArtifactPruneCandidate:
    """One immutable prune decision produced under the store lock."""

    artifact_ref: str
    path: Path
    size_bytes: int
    age_seconds: float
    contract_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ArtifactPruneReport:
    """Dry-run or applied GC result."""

    applied: bool
    candidates: tuple[ArtifactPruneCandidate, ...]
    removed_refs: tuple[str, ...] = ()
    removed_bytes: int = 0


def canonical_artifact_bytes(body: Any) -> bytes:
    """Encode a JSON artifact deterministically for hashing and exact limits."""
    try:
        rendered = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactStoreError(
            f"Disposable artifact is not canonical JSON: {exc}",
            operation="serialize",
        ) from exc
    return rendered.encode("utf-8")


class ContentAddressedArtifactStore:
    """Project-local SHA-256 store with explicit replay and tombstoned GC."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        max_artifact_bytes: int = MAX_DISPOSABLE_ARTIFACT_BYTES,
        project_root: Path | None = None,
    ) -> None:
        if not 0 < max_artifact_bytes <= MAX_DISPOSABLE_ARTIFACT_BYTES:
            raise ValueError(
                "max_artifact_bytes must be positive and cannot exceed the 1 MiB hard cap"
            )
        self.root = Path(os.path.abspath(artifact_root.expanduser()))
        self._project_root = project_root.expanduser().resolve() if project_root else None
        self.max_artifact_bytes = max_artifact_bytes
        self._contracts_root = self.root / "contracts"
        self._lock_target = self.root / ".artifact-store"
        self._validate_project_boundary()

    @classmethod
    def for_project(
        cls,
        project_dir: Path,
        *,
        max_artifact_bytes: int = MAX_DISPOSABLE_ARTIFACT_BYTES,
    ) -> ContentAddressedArtifactStore:
        """Build the RFC-standard store below one project root."""
        return cls(
            project_dir.expanduser().resolve() / ".ouroboros" / "artifacts",
            max_artifact_bytes=max_artifact_bytes,
            project_root=project_dir.expanduser().resolve(),
        )

    def initialize(self) -> None:
        """Create the content and contract directories idempotently."""
        self._validate_project_boundary()
        self._contracts_root.mkdir(parents=True, exist_ok=True)
        self._validate_project_boundary()

    def put_for_contract(
        self,
        *,
        contract_id: str,
        body: Any,
        runtime_id: str,
        duration_ms: int,
        events_emitted_count: int,
        status: DisposableResultStatus = DisposableResultStatus.COMPLETED,
        active: bool = False,
        retain_until: datetime | None = None,
        now: datetime | None = None,
    ) -> DisposableResultEnvelope:
        """Publish a body, then durably bind its small envelope to a contract.

        A contract may be retried idempotently with the same body.  Reusing it
        for different content is rejected; intentional reruns need a new id.
        """
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        payload = canonical_artifact_bytes(body)
        if len(payload) > self.max_artifact_bytes:
            raise ArtifactTooLargeError(
                "Disposable artifact exceeds the encoded output limit",
                operation="write",
                details={
                    "size_bytes": len(payload),
                    "max_artifact_bytes": self.max_artifact_bytes,
                },
            )
        digest = hashlib.sha256(payload).hexdigest()
        artifact_ref = f"sha256:{digest}"
        timestamp = _as_utc(now or datetime.now(UTC))
        retention = _as_utc(retain_until or (timestamp + DEFAULT_REPLAY_RETENTION))
        envelope = DisposableResultEnvelope(
            contract_id=contract_id,
            artifact_ref=artifact_ref,
            result=DisposableResultSummary(status=status),
            runtime_id=runtime_id,
            duration_ms=duration_ms,
            events_emitted_count=events_emitted_count,
        )

        with file_lock(self._lock_target, exclusive=True):
            manifest = self._load_manifest_locked(contract_id, missing_ok=True)
            existing = _latest_artifact_event(manifest)
            if existing is not None:
                existing_ref = existing.get("artifact_ref")
                if existing.get("type") == "artifact.tombstoned":
                    raise ArtifactTombstonedError(
                        "Contract artifact was pruned; allocate a new contract id to rerun",
                        operation="write",
                        details={"contract_id": contract_id, "artifact_ref": existing_ref},
                    )
                if existing_ref != artifact_ref:
                    raise ArtifactContractConflictError(
                        "Contract id is already bound to a different artifact",
                        operation="write",
                        details={
                            "contract_id": contract_id,
                            "existing_artifact_ref": existing_ref,
                            "new_artifact_ref": artifact_ref,
                        },
                    )
                self._read_blob_locked(artifact_ref)
                return _envelope_from_event(existing, contract_id=contract_id)

            self._write_blob_locked(digest, payload)
            manifest["active"] = bool(active)
            manifest["retain_until"] = retention.isoformat()
            manifest["updated_at"] = timestamp.isoformat()
            manifest["events"].append(
                {
                    "type": "artifact.referenced",
                    "timestamp": timestamp.isoformat(),
                    "artifact_ref": artifact_ref,
                    "size_bytes": len(payload),
                    "envelope": envelope.model_dump(mode="json"),
                }
            )
            self._write_manifest_locked(contract_id, manifest)
        return envelope

    def fetch(self, contract_id: str) -> FetchedArtifact:
        """Explicitly fetch and verify the body referenced by one contract."""
        fetched = self.fetch_if_exists(contract_id)
        if fetched is None:
            raise ArtifactNotFoundError(
                "Artifact contract manifest does not exist",
                operation="read",
                details={"contract_id": contract_id},
            )
        return fetched

    def fetch_if_exists(self, contract_id: str) -> FetchedArtifact | None:
        """Fetch a durable contract, returning ``None`` only when no binding exists."""
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        with file_lock(self._lock_target, exclusive=False):
            manifest = self._load_manifest_locked(contract_id, missing_ok=True)
            event = _latest_artifact_event(manifest)
            if event is None:
                return None
            artifact_ref = _event_artifact_ref(event, contract_id=contract_id)
            if event.get("type") == "artifact.tombstoned":
                raise ArtifactTombstonedError(
                    "Artifact was pruned; use an explicit force-rerun path to recompute it",
                    operation="read",
                    details={
                        "contract_id": contract_id,
                        "artifact_ref": artifact_ref,
                        "tombstoned_at": event.get("timestamp"),
                    },
                )
            payload = self._read_blob_locked(artifact_ref)
            if event.get("size_bytes") != len(payload):
                raise ArtifactIntegrityError(
                    "Artifact body size does not match its manifest",
                    operation="read",
                    details={
                        "contract_id": contract_id,
                        "artifact_ref": artifact_ref,
                        "manifest_size_bytes": event.get("size_bytes"),
                        "actual_size_bytes": len(payload),
                    },
                )
            try:
                body = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ArtifactIntegrityError(
                    "Artifact body is not valid JSON",
                    operation="read",
                    details={"contract_id": contract_id, "artifact_ref": artifact_ref},
                ) from exc
            return FetchedArtifact(
                envelope=_envelope_from_event(event, contract_id=contract_id),
                body=body,
            )

    def replay(self, contract_id: str) -> FetchedArtifact:
        """Deterministically replay from storage without executing any work."""
        return self.fetch(contract_id)

    def set_contract_retention(
        self,
        contract_id: str,
        *,
        active: bool,
        retain_until: datetime,
        now: datetime | None = None,
    ) -> None:
        """Update reachability state without changing artifact history."""
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        timestamp = _as_utc(now or datetime.now(UTC))
        with file_lock(self._lock_target, exclusive=True):
            manifest = self._load_manifest_locked(contract_id, missing_ok=False)
            manifest["active"] = bool(active)
            manifest["retain_until"] = _as_utc(retain_until).isoformat()
            manifest["updated_at"] = timestamp.isoformat()
            self._write_manifest_locked(contract_id, manifest)

    def prune(
        self,
        *,
        ttl: timedelta = DEFAULT_ARTIFACT_TTL,
        apply: bool = False,
        allow_replay_tombstone: bool = False,
        now: datetime | None = None,
    ) -> ArtifactPruneReport:
        """Plan or apply reachability-first, TTL-bounded artifact pruning."""
        if ttl.total_seconds() < 0:
            raise ValueError("ttl must not be negative")
        self.initialize()
        timestamp = _as_utc(now or datetime.now(UTC))
        with file_lock(self._lock_target, exclusive=True):
            manifests = self._load_all_manifests_locked()
            candidates = self._plan_prune_locked(
                manifests,
                ttl=ttl,
                allow_replay_tombstone=allow_replay_tombstone,
                now=timestamp,
            )
            if not apply:
                return ArtifactPruneReport(applied=False, candidates=tuple(candidates))

            removed_refs: list[str] = []
            removed_bytes = 0
            for candidate in candidates:
                for contract_id in candidate.contract_ids:
                    manifest = manifests[contract_id]
                    latest = _latest_artifact_event(manifest)
                    if latest is None or latest.get("type") != "artifact.referenced":
                        continue
                    if latest.get("artifact_ref") != candidate.artifact_ref:
                        continue
                    manifest["events"].append(
                        {
                            "type": "artifact.tombstoned",
                            "timestamp": timestamp.isoformat(),
                            "artifact_ref": candidate.artifact_ref,
                            "reason": candidate.reason,
                        }
                    )
                    manifest["updated_at"] = timestamp.isoformat()
                    self._write_manifest_locked(contract_id, manifest)

                try:
                    candidate.path.unlink()
                except FileNotFoundError:
                    pass
                removed_refs.append(candidate.artifact_ref)
                removed_bytes += candidate.size_bytes

            return ArtifactPruneReport(
                applied=True,
                candidates=tuple(candidates),
                removed_refs=tuple(removed_refs),
                removed_bytes=removed_bytes,
            )

    def _plan_prune_locked(
        self,
        manifests: dict[str, dict[str, Any]],
        *,
        ttl: timedelta,
        allow_replay_tombstone: bool,
        now: datetime,
    ) -> list[ArtifactPruneCandidate]:
        reverse: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for contract_id, manifest in manifests.items():
            latest = _latest_artifact_event(manifest)
            if latest is None or latest.get("type") != "artifact.referenced":
                continue
            artifact_ref = _event_artifact_ref(latest, contract_id=contract_id)
            reverse.setdefault(artifact_ref, []).append((contract_id, manifest))

        candidates: list[ArtifactPruneCandidate] = []
        for path in self._iter_blob_paths_locked():
            stat = path.stat()
            age_seconds = max(0.0, now.timestamp() - stat.st_mtime)
            if age_seconds < ttl.total_seconds():
                continue
            digest = path.stem
            artifact_ref = f"sha256:{digest}"
            references = reverse.get(artifact_ref, [])
            if any(bool(manifest.get("active")) for _, manifest in references):
                continue

            retained = [
                contract_id
                for contract_id, manifest in references
                if _manifest_retained(manifest, now=now)
            ]
            if retained and not allow_replay_tombstone:
                continue
            contract_ids = tuple(sorted(contract_id for contract_id, _ in references))
            if not references:
                reason = "unreferenced artifact exceeded TTL"
            elif retained:
                reason = "operator allowed replay tombstone before retention expiry"
            else:
                reason = "all referencing contracts exceeded replay retention and artifact TTL"
            candidates.append(
                ArtifactPruneCandidate(
                    artifact_ref=artifact_ref,
                    path=path,
                    size_bytes=stat.st_size,
                    age_seconds=age_seconds,
                    contract_ids=contract_ids,
                    reason=reason,
                )
            )
        return sorted(candidates, key=lambda item: item.artifact_ref)

    def _write_blob_locked(self, digest: str, payload: bytes) -> None:
        path = self._blob_path_from_digest(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if existing != payload:
                raise ArtifactIntegrityError(
                    "Content-addressed path contains different bytes; refusing overwrite",
                    operation="write",
                    details={"artifact_ref": f"sha256:{digest}"},
                )
            return
        _atomic_write_bytes(path, payload)

    def _read_blob_locked(self, artifact_ref: str) -> bytes:
        digest = _digest_from_ref(artifact_ref)
        path = self._blob_path_from_digest(digest)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(
                "Referenced artifact body is missing without a tombstone",
                operation="read",
                details={"artifact_ref": artifact_ref},
            ) from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            raise ArtifactIntegrityError(
                "Artifact body hash does not match its content address",
                operation="read",
                details={"artifact_ref": artifact_ref, "actual_sha256": actual},
            )
        if len(payload) > self.max_artifact_bytes:
            raise ArtifactIntegrityError(
                "Stored artifact exceeds the configured disposable output limit",
                operation="read",
                details={
                    "artifact_ref": artifact_ref,
                    "size_bytes": len(payload),
                    "max_artifact_bytes": self.max_artifact_bytes,
                },
            )
        return payload

    def _blob_path_from_digest(self, digest: str) -> Path:
        self._validate_project_boundary()
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("invalid SHA-256 digest")
        prefix = self.root / digest[:2]
        path = prefix / f"{digest}.json"
        _require_contained(path, root=self.root, label="artifact body")
        if prefix.is_symlink() or path.is_symlink():
            raise ArtifactIntegrityError(
                "Artifact body path must not traverse a symlink",
                operation="path_resolution",
                details={"path": str(path)},
            )
        return path

    def _manifest_path(self, contract_id: str) -> Path:
        self._validate_project_boundary()
        path = self._contracts_root / contract_id / _MANIFEST_FILENAME
        _require_contained(path, root=self._contracts_root, label="artifact manifest")
        if path.parent.is_symlink() or path.is_symlink():
            raise ArtifactIntegrityError(
                "Artifact manifest path must not traverse a symlink",
                operation="path_resolution",
                details={"path": str(path)},
            )
        return path

    def _load_manifest_locked(
        self,
        contract_id: str,
        *,
        missing_ok: bool,
    ) -> dict[str, Any]:
        path = self._manifest_path(contract_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not missing_ok:
                raise ArtifactNotFoundError(
                    "Artifact contract manifest does not exist",
                    operation="read",
                    details={"contract_id": contract_id},
                )
            return {
                "schema_version": _MANIFEST_VERSION,
                "contract_id": contract_id,
                "active": False,
                "retain_until": None,
                "updated_at": None,
                "events": [],
            }
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactManifestError(
                "Artifact manifest is unreadable; pruning is unsafe",
                operation="read",
                details={"contract_id": contract_id, "path": str(path)},
            ) from exc
        return _validate_manifest(raw, contract_id=contract_id, path=path)

    def _load_all_manifests_locked(self) -> dict[str, dict[str, Any]]:
        manifests: dict[str, dict[str, Any]] = {}
        if not self._contracts_root.exists():
            return manifests
        for path in sorted(self._contracts_root.glob(f"*/{_MANIFEST_FILENAME}")):
            _require_contained(path, root=self._contracts_root, label="artifact manifest")
            if path.parent.is_symlink() or path.is_symlink():
                raise ArtifactIntegrityError(
                    "Artifact manifest path must not traverse a symlink",
                    operation="path_resolution",
                    details={"path": str(path)},
                )
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactManifestError(
                    "Artifact manifest is unreadable; pruning aborted fail-closed",
                    operation="read",
                    details={"path": str(path)},
                ) from exc
            raw_contract_id = raw.get("contract_id") if isinstance(raw, dict) else None
            if not isinstance(raw_contract_id, str):
                raise ArtifactManifestError(
                    "Artifact manifest is missing contract_id",
                    operation="read",
                    details={"path": str(path)},
                )
            try:
                contract_id = _validate_contract_id(raw_contract_id)
            except ValueError as exc:
                raise ArtifactManifestError(
                    "Artifact manifest contains an unsafe contract_id",
                    operation="read",
                    details={"path": str(path)},
                ) from exc
            if path.parent.name != contract_id:
                raise ArtifactManifestError(
                    "Artifact manifest path does not match contract_id",
                    operation="read",
                    details={"path": str(path), "contract_id": contract_id},
                )
            manifests[contract_id] = _validate_manifest(
                raw,
                contract_id=contract_id,
                path=path,
            )
        return manifests

    def _write_manifest_locked(self, contract_id: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(contract_id)
        validated = _validate_manifest(manifest, contract_id=contract_id, path=path)
        payload = (json.dumps(validated, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write_bytes(path, payload)

    def _iter_blob_paths_locked(self) -> list[Path]:
        self._validate_project_boundary()
        paths: list[Path] = []
        for prefix in sorted(self.root.iterdir() if self.root.exists() else []):
            if not prefix.is_dir() or prefix.name == "contracts":
                continue
            if not re.fullmatch(r"[0-9a-f]{2}", prefix.name):
                continue
            if prefix.is_symlink():
                raise ArtifactIntegrityError(
                    "Artifact digest prefix must not be a symlink",
                    operation="read",
                    details={"path": str(prefix)},
                )
            for path in sorted(prefix.glob("*.json")):
                if path.is_symlink():
                    raise ArtifactIntegrityError(
                        "Artifact body must not be a symlink",
                        operation="read",
                        details={"path": str(path)},
                    )
                if _DIGEST_PATTERN.fullmatch(path.stem) and path.stem.startswith(prefix.name):
                    paths.append(path)
        return paths

    def _validate_project_boundary(self) -> None:
        project_root = self._project_root
        if project_root is None:
            return
        try:
            relative_root = self.root.relative_to(project_root)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                "Project artifact store path escapes the project root",
                operation="path_resolution",
                details={"path": str(self.root), "project_root": str(project_root)},
            ) from exc

        current = project_root
        for component in relative_root.parts:
            current /= component
            if _is_link_like(current):
                raise ArtifactIntegrityError(
                    "Project artifact store path must not traverse a symlink",
                    operation="path_resolution",
                    details={"path": str(current), "project_root": str(project_root)},
                )

        if _is_link_like(self._contracts_root):
            raise ArtifactIntegrityError(
                "Project artifact contracts path must not be a symlink",
                operation="path_resolution",
                details={"path": str(self._contracts_root), "project_root": str(project_root)},
            )
        try:
            resolved_root = self.root.resolve()
            resolved_contracts = self._contracts_root.resolve()
        except (OSError, RuntimeError) as exc:
            raise ArtifactIntegrityError(
                "Project artifact store path could not be resolved safely",
                operation="path_resolution",
                details={"path": str(self.root), "project_root": str(project_root)},
            ) from exc
        if not resolved_root.is_relative_to(project_root) or not resolved_contracts.is_relative_to(
            resolved_root
        ):
            raise ArtifactIntegrityError(
                "Project artifact store path escapes the project-owned store",
                operation="path_resolution",
                details={
                    "path": str(resolved_contracts),
                    "root": str(resolved_root),
                    "project_root": str(project_root),
                },
            )


def _validate_contract_id(contract_id: str) -> str:
    if not DISPOSABLE_CONTRACT_ID_PATTERN.fullmatch(contract_id):
        raise ValueError(
            "contract_id must be 1-128 path-safe ASCII characters beginning with alphanumeric"
        )
    return contract_id


def _digest_from_ref(artifact_ref: str) -> str:
    if not ARTIFACT_REF_PATTERN.fullmatch(artifact_ref):
        raise ValueError("invalid artifact_ref")
    return artifact_ref.removeprefix("sha256:")


def _event_artifact_ref(event: dict[str, Any], *, contract_id: str) -> str:
    artifact_ref = event.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not ARTIFACT_REF_PATTERN.fullmatch(artifact_ref):
        raise ArtifactManifestError(
            "Artifact manifest event has an invalid artifact_ref",
            operation="read",
            details={"contract_id": contract_id},
        )
    return artifact_ref


def _latest_artifact_event(manifest: dict[str, Any]) -> dict[str, Any] | None:
    for event in reversed(manifest["events"]):
        if event.get("type") in {"artifact.referenced", "artifact.tombstoned"}:
            return event
    return None


def _envelope_from_event(
    event: dict[str, Any],
    *,
    contract_id: str,
) -> DisposableResultEnvelope:
    envelope = event.get("envelope")
    try:
        parsed = DisposableResultEnvelope.model_validate(envelope)
    except (TypeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact reference event has an invalid bounded envelope",
            operation="read",
        ) from exc
    artifact_ref = _event_artifact_ref(event, contract_id=contract_id)
    if parsed.contract_id != contract_id or parsed.artifact_ref != artifact_ref:
        raise ArtifactManifestError(
            "Artifact reference envelope does not match its manifest event",
            operation="read",
            details={"contract_id": contract_id, "artifact_ref": artifact_ref},
        )
    size_bytes = event.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= MAX_DISPOSABLE_ARTIFACT_BYTES
    ):
        raise ArtifactManifestError(
            "Artifact reference event has an invalid size_bytes",
            operation="read",
            details={"contract_id": contract_id, "artifact_ref": artifact_ref},
        )
    return parsed


def _validate_manifest(
    raw: Any,
    *,
    contract_id: str,
    path: Path,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ArtifactManifestError(
            "Artifact manifest must be a JSON object",
            operation="read",
            details={"path": str(path)},
        )
    if raw.get("schema_version") != _MANIFEST_VERSION or raw.get("contract_id") != contract_id:
        raise ArtifactManifestError(
            "Artifact manifest identity or schema version is invalid",
            operation="read",
            details={"path": str(path), "contract_id": contract_id},
        )
    if not isinstance(raw.get("active"), bool) or not isinstance(raw.get("events"), list):
        raise ArtifactManifestError(
            "Artifact manifest state is invalid",
            operation="read",
            details={"path": str(path), "contract_id": contract_id},
        )
    for event in raw["events"]:
        if not isinstance(event, dict):
            raise ArtifactManifestError(
                "Artifact manifest events must be objects",
                operation="read",
                details={"path": str(path), "contract_id": contract_id},
            )
        event_type = event.get("type")
        if event_type not in {"artifact.referenced", "artifact.tombstoned"}:
            raise ArtifactManifestError(
                "Artifact manifest contains an unknown event type",
                operation="read",
                details={"path": str(path), "event_type": event_type},
            )
        _event_artifact_ref(event, contract_id=contract_id)
        _parse_datetime(event.get("timestamp"), field="event.timestamp", path=path)
        if event_type == "artifact.referenced":
            _envelope_from_event(event, contract_id=contract_id)
    retain_until = raw.get("retain_until")
    if retain_until is not None:
        _parse_datetime(retain_until, field="retain_until", path=path)
    return raw


def _manifest_retained(manifest: dict[str, Any], *, now: datetime) -> bool:
    retain_until = manifest.get("retain_until")
    if retain_until is None:
        return True
    return (
        _parse_datetime(
            retain_until,
            field="retain_until",
            path=Path(f"contract:{manifest.get('contract_id', 'unknown')}"),
        )
        > now
    )


def _parse_datetime(value: Any, *, field: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise ArtifactManifestError(
            f"Artifact manifest {field} must be an ISO-8601 string",
            operation="read",
            details={"path": str(path)},
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ArtifactManifestError(
            f"Artifact manifest {field} is not valid ISO-8601",
            operation="read",
            details={"path": str(path)},
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactManifestError(
            f"Artifact manifest {field} must include a timezone",
            operation="read",
            details={"path": str(path)},
        )
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime values must include a timezone")
    return value.astimezone(UTC)


def _require_contained(path: Path, *, root: Path, label: str) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ArtifactIntegrityError(
            f"{label} path escapes the artifact store",
            operation="path_resolution",
            details={"path": str(resolved_path), "root": str(resolved_root)},
        )


def _is_link_like(path: Path) -> bool:
    """Return whether an existing path is a symlink or Windows junction."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows cannot fsync directories
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DEFAULT_ARTIFACT_TTL",
    "DEFAULT_REPLAY_RETENTION",
    "ArtifactContractConflictError",
    "ArtifactIntegrityError",
    "ArtifactManifestError",
    "ArtifactNotFoundError",
    "ArtifactPruneCandidate",
    "ArtifactPruneReport",
    "ArtifactStoreError",
    "ArtifactTombstonedError",
    "ArtifactTooLargeError",
    "ContentAddressedArtifactStore",
    "FetchedArtifact",
    "canonical_artifact_bytes",
]
