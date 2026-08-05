"""Content-addressed storage and conservative GC for Disposable Memory.

Artifact bodies live outside the EventStore under
``.ouroboros/artifacts/<prefix>/<sha256>.json``.  Per-contract manifests keep
the replay reference and tombstone history without copying the body into the
main ledger.  Every mutation uses one cross-process store lock so a concurrent
reference cannot race a prune decision.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
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
_DIRECTORY_FD_PUBLICATION_SUPPORTED: Final[bool] = bool(
    os.name != "nt"
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)
_DIRECTORY_FD_UNLINK_SUPPORTED: Final[bool] = bool(
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)


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
    device_id: int
    inode: int
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
    _validate_json_native(body)
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
        self._directory_anchor = self._project_root or _nearest_existing_directory(self.root)
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
        with _pinned_directory_tree(
            self._contracts_root,
            anchor=self._directory_anchor,
            root=self.root,
            label="artifact store",
        ):
            pass
        self._validate_project_boundary()

    @contextmanager
    def _store_lock(self, *, exclusive: bool) -> Iterator[None]:
        """Hold the store directory authority together with its lockfile."""
        with _pinned_directory_tree(
            self.root,
            anchor=self._directory_anchor,
            root=self.root,
            label="artifact store lock",
        ) as directory_fd:
            with file_lock(
                self._lock_target,
                exclusive=exclusive,
                parent_fd=directory_fd,
            ):
                self._validate_project_boundary()
                yield

    @contextmanager
    def contract_execution_lock(
        self,
        contract_id: str,
        *,
        blocking: bool = True,
    ) -> Iterator[None]:
        """Serialize one contract's child effects across processes."""
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        lock_target = self._contract_execution_lock_target(contract_id)
        with _pinned_directory_tree(
            lock_target.parent,
            anchor=self._directory_anchor,
            root=self._contracts_root,
            label="contract execution lock",
        ) as directory_fd:
            with file_lock(
                lock_target,
                exclusive=True,
                blocking=blocking,
                parent_fd=directory_fd,
            ):
                self._validate_project_boundary()
                self._contract_execution_lock_target(contract_id)
                yield

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

        with self._store_lock(exclusive=True):
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

    def envelope_if_exists(self, contract_id: str) -> DisposableResultEnvelope | None:
        """Read only a contract's bounded envelope, never its artifact body."""
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        with self._store_lock(exclusive=False):
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
            return _envelope_from_event(event, contract_id=contract_id)

    def fetch_if_exists(self, contract_id: str) -> FetchedArtifact | None:
        """Fetch a durable contract, returning ``None`` only when no binding exists."""
        self.initialize()
        contract_id = _validate_contract_id(contract_id)
        with self._store_lock(exclusive=False):
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
        with self._store_lock(exclusive=True):
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
        with self._store_lock(exclusive=True):
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
                    # Tombstones become durable first. Deletion then pins the
                    # digest parent and validates the planned body identity
                    # through that same handle before a directory-relative
                    # unlink. The store lock excludes cooperating writers, not
                    # hostile or unrelated filesystem mutation.
                    self._unlink_blob_candidate_locked(candidate)
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
            self._validate_blob_candidate_locked(path)
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode):
                raise ArtifactIntegrityError(
                    "Artifact body must be a regular file",
                    operation="path_resolution",
                    details={"path": str(path)},
                )
            age_seconds = max(0.0, now.timestamp() - status.st_mtime)
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
                    size_bytes=status.st_size,
                    device_id=status.st_dev,
                    inode=status.st_ino,
                    age_seconds=age_seconds,
                    contract_ids=contract_ids,
                    reason=reason,
                )
            )
        return sorted(candidates, key=lambda item: item.artifact_ref)

    def _write_blob_locked(self, digest: str, payload: bytes) -> None:
        path = self._blob_path_from_digest(digest)
        _atomic_write_bytes(
            path,
            payload,
            root=self.root,
            anchor=self._directory_anchor,
            label="artifact body",
            matching_existing=payload,
        )

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
        if _is_link_like(prefix) or _is_link_like(path):
            raise ArtifactIntegrityError(
                "Artifact body path must not traverse a link or junction",
                operation="path_resolution",
                details={"path": str(path)},
            )
        return path

    def _manifest_path(self, contract_id: str) -> Path:
        self._validate_project_boundary()
        path = self._contracts_root / contract_id / _MANIFEST_FILENAME
        _require_contained(path, root=self._contracts_root, label="artifact manifest")
        if _is_link_like(path.parent) or _is_link_like(path):
            raise ArtifactIntegrityError(
                "Artifact manifest path must not traverse a link or junction",
                operation="path_resolution",
                details={"path": str(path)},
            )
        return path

    def _contract_execution_lock_target(self, contract_id: str) -> Path:
        self._validate_project_boundary()
        contract_root = self._contracts_root / contract_id
        target = contract_root / ".execution"
        lock_path = target.with_suffix(target.suffix + ".lock")
        if not target.is_relative_to(self._contracts_root):
            raise ArtifactIntegrityError(
                "Contract execution lock path escapes the artifact store",
                operation="path_resolution",
                details={"path": str(target), "root": str(self._contracts_root)},
            )
        if _is_link_like(contract_root) or _is_link_like(target) or _is_link_like(lock_path):
            raise ArtifactIntegrityError(
                "Contract execution lock path must not traverse a symlink",
                operation="path_resolution",
                details={"path": str(lock_path)},
            )
        return target

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
        _atomic_write_bytes(
            path,
            payload,
            root=self._contracts_root,
            anchor=self._directory_anchor,
            label="artifact manifest",
        )

    def _iter_blob_paths_locked(self) -> list[Path]:
        self._validate_project_boundary()
        paths: list[Path] = []
        for prefix in sorted(self.root.iterdir() if self.root.exists() else []):
            if _is_link_like(prefix):
                raise ArtifactIntegrityError(
                    "Artifact digest prefix must not be a link or junction",
                    operation="read",
                    details={"path": str(prefix)},
                )
            if not prefix.is_dir() or prefix.name == "contracts":
                continue
            if not re.fullmatch(r"[0-9a-f]{2}", prefix.name):
                continue
            _require_contained(prefix, root=self.root, label="artifact digest prefix")
            for path in sorted(prefix.glob("*.json")):
                if _DIGEST_PATTERN.fullmatch(path.stem) and path.stem.startswith(prefix.name):
                    self._validate_blob_candidate_locked(path)
                    paths.append(path)
        return paths

    def _validate_blob_candidate_locked(self, path: Path) -> None:
        """Fail closed unless one prune candidate is a local regular path.

        ``Path.is_symlink`` does not identify Windows directory junctions. Both
        the digest prefix and body are therefore checked with ``_is_link_like``
        and resolved containment is repeated at each destructive boundary.
        """
        self._validate_project_boundary()
        prefix = path.parent
        if _is_link_like(prefix):
            raise ArtifactIntegrityError(
                "Artifact digest prefix must not be a link or junction",
                operation="path_resolution",
                details={"path": str(prefix)},
            )
        if _is_link_like(path):
            raise ArtifactIntegrityError(
                "Artifact body must not be a link or junction",
                operation="path_resolution",
                details={"path": str(path)},
            )
        _require_contained(prefix, root=self.root, label="artifact digest prefix")
        _require_contained(path, root=self.root, label="artifact body")

    def _unlink_blob_candidate_locked(self, candidate: ArtifactPruneCandidate) -> None:
        """Delete exactly the planned local body without following parent swaps."""
        self._validate_blob_candidate_locked(candidate.path)
        if _supports_directory_fd_unlink():
            _unlink_blob_candidate_at(candidate, root=self.root)
            return
        if os.name != "nt":
            raise ArtifactIntegrityError(
                "Artifact deletion requires safe directory-relative filesystem operations",
                operation="path_resolution",
                details={"path": str(candidate.path), "root": str(self.root)},
            )
        _unlink_blob_candidate_guarded(candidate, root=self.root)

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


def _validate_json_native(
    value: Any,
    *,
    path: str = "$",
    ancestors: set[int] | None = None,
) -> None:
    """Reject Python values that JSON cannot replay without normalization."""
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if math.isfinite(value):
            return
        raise ArtifactStoreError(
            "Disposable artifact contains a non-finite JSON number",
            operation="serialize",
            details={"path": path},
        )

    if value_type not in {dict, list}:
        raise ArtifactStoreError(
            "Disposable artifact values must use JSON-native types",
            operation="serialize",
            details={"path": path, "type": value_type.__name__},
        )

    active = ancestors if ancestors is not None else set()
    marker = id(value)
    if marker in active:
        raise ArtifactStoreError(
            "Disposable artifact contains a circular JSON value",
            operation="serialize",
            details={"path": path},
        )
    active.add(marker)
    try:
        if value_type is list:
            for index, item in enumerate(value):
                _validate_json_native(item, path=f"{path}[{index}]", ancestors=active)
            return

        for key, item in value.items():
            if type(key) is not str:
                raise ArtifactStoreError(
                    "Disposable artifact object keys must be JSON strings",
                    operation="serialize",
                    details={"path": path, "key_type": type(key).__name__},
                )
            _validate_json_native(item, path=f"{path}.{key}", ancestors=active)
    finally:
        active.remove(marker)


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


def _validate_publication_path(path: Path, *, root: Path, label: str) -> None:
    """Reject link traversal and resolved escapes at the actual write boundary."""
    if _is_link_like(root) or _is_link_like(path.parent) or _is_link_like(path):
        raise ArtifactIntegrityError(
            f"{label} publication must not traverse a link or junction",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )
    _require_contained(path.parent, root=root, label=f"{label} parent")
    _require_contained(path, root=root, label=label)


def _validate_directory_binding(
    descriptor: int,
    path: Path,
    *,
    root: Path,
    label: str,
) -> None:
    """Prove a held directory handle still names the validated local parent."""
    _validate_publication_path(path, root=root, label=label)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"{label} publication parent changed during the write",
            operation="path_resolution",
            details={"path": str(path.parent), "root": str(root)},
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ArtifactIntegrityError(
            f"{label} publication parent changed during the write",
            operation="path_resolution",
            details={"path": str(path.parent), "root": str(root)},
        )


def _supports_directory_fd_unlink() -> bool:
    """Return whether Python exposes the required unlinkat-style primitives."""
    return _DIRECTORY_FD_UNLINK_SUPPORTED


def _validate_prune_identity(
    status: os.stat_result,
    candidate: ArtifactPruneCandidate,
    *,
    root: Path,
) -> None:
    """Require one observed body to be the regular file selected at planning."""
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_size != candidate.size_bytes
        or status.st_dev != candidate.device_id
        or status.st_ino != candidate.inode
    ):
        raise ArtifactIntegrityError(
            "Artifact body changed after prune planning",
            operation="path_resolution",
            details={"path": str(candidate.path), "root": str(root)},
        )


def _unlink_blob_candidate_at(
    candidate: ArtifactPruneCandidate,
    *,
    root: Path,
) -> None:
    """Unlink a planned body relative to its pinned, non-link parent."""
    path = candidate.path
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ArtifactIntegrityError(
            "Artifact digest parent is not a safe local directory",
            operation="path_resolution",
            details={"path": str(path.parent), "root": str(root)},
        ) from exc

    body_fd = -1
    try:
        _validate_directory_binding(
            directory_fd,
            path,
            root=root,
            label="artifact body deletion",
        )
        entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_prune_identity(entry, candidate, root=root)

        body_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        body_flags |= getattr(os, "O_NONBLOCK", 0)
        body_fd = os.open(path.name, body_flags, dir_fd=directory_fd)
        opened = os.fstat(body_fd)
        _validate_prune_identity(opened, candidate, root=root)
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise ArtifactIntegrityError(
                "Artifact body changed while it was opened for deletion",
                operation="path_resolution",
                details={"path": str(path), "root": str(root)},
            )
        os.close(body_fd)
        body_fd = -1

        _validate_directory_binding(
            directory_fd,
            path,
            root=root,
            label="artifact body deletion",
        )
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_prune_identity(current, candidate, root=root)
        os.unlink(path.name, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except ArtifactIntegrityError:
        raise
    except OSError as exc:
        raise ArtifactIntegrityError(
            "Artifact body could not be deleted through its pinned parent",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        ) from exc
    finally:
        if body_fd >= 0:
            os.close(body_fd)
        os.close(directory_fd)


def _unlink_blob_candidate_guarded(
    candidate: ArtifactPruneCandidate,
    *,
    root: Path,
) -> None:
    """Windows deletion guarded by a no-share-delete parent lease."""
    path = candidate.path
    with _windows_directory_lease(path.parent, root=root, label="artifact body deletion"):
        _validate_publication_path(path, root=root, label="artifact body deletion")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_prune_identity(opened, candidate, root=root)
        current = path.stat(follow_symlinks=False)
        _validate_prune_identity(current, candidate, root=root)
        _validate_publication_path(path, root=root, label="artifact body deletion")
        path.unlink()


def _nearest_existing_directory(path: Path) -> Path:
    """Choose the nearest lexical ancestor that can anchor safe creation."""
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _validate_pinned_directory(
    directory_fd: int,
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> None:
    """Prove one held directory is still the live descendant of its anchor."""
    try:
        path.relative_to(anchor)
        opened = os.fstat(directory_fd)
        current = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise ArtifactIntegrityError(
            f"{label} directory ancestor changed during creation",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor)},
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ArtifactIntegrityError(
            f"{label} directory ancestor changed during creation",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor)},
        )


@contextmanager
def _pinned_directory_tree(
    path: Path,
    *,
    anchor: Path,
    root: Path,
    label: str,
) -> Iterator[int | None]:
    """Create and retain every directory from one trusted lexical anchor."""
    if _supports_directory_fd_publication():
        with _pinned_directory_tree_at(path, anchor=anchor, label=label) as directory_fd:
            yield directory_fd
        return
    if os.name != "nt":
        raise ArtifactIntegrityError(
            f"{label} directory creation requires safe directory-relative operations",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor), "root": str(root)},
        )
    with _pinned_directory_tree_guarded(path, anchor=anchor, label=label):
        yield None


@contextmanager
def _pinned_directory_tree_at(
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> Iterator[int]:
    """Create descendants with mkdirat while retaining every opened parent."""
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ArtifactIntegrityError(
            f"{label} directory escapes its creation anchor",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor)},
        ) from exc

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(anchor, directory_flags)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"{label} creation anchor is not a safe local directory",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor)},
        ) from exc

    current_path = anchor
    try:
        _validate_pinned_directory(
            directory_fd,
            current_path,
            anchor=anchor,
            label=label,
        )
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ArtifactIntegrityError(
                    f"{label} directory contains an unsafe component",
                    operation="path_resolution",
                    details={"path": str(path), "anchor": str(anchor)},
                )
            try:
                os.mkdir(component, 0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = -1
            try:
                child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                entry = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(entry.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise ArtifactIntegrityError(
                        f"{label} directory changed while it was opened",
                        operation="path_resolution",
                        details={"path": str(current_path / component)},
                    )
                current_path /= component
                _validate_pinned_directory(
                    child_fd,
                    current_path,
                    anchor=anchor,
                    label=label,
                )
                os.close(directory_fd)
                directory_fd = child_fd
                child_fd = -1
            except OSError as exc:
                raise ArtifactIntegrityError(
                    f"{label} directory is not a safe local directory",
                    operation="path_resolution",
                    details={"path": str(current_path / component)},
                ) from exc
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
        yield directory_fd
    finally:
        os.close(directory_fd)


@contextmanager
def _pinned_directory_tree_guarded(
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> Iterator[None]:
    """Create a Windows directory tree while every ancestor is leased."""
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ArtifactIntegrityError(
            f"{label} directory escapes its creation anchor",
            operation="path_resolution",
            details={"path": str(path), "anchor": str(anchor)},
        ) from exc

    with ExitStack() as stack:
        stack.enter_context(_windows_directory_lease(anchor, root=anchor, label=label))
        current = anchor
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ArtifactIntegrityError(
                    f"{label} directory contains an unsafe component",
                    operation="path_resolution",
                    details={"path": str(path), "anchor": str(anchor)},
                )
            current /= component
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            stack.enter_context(_windows_directory_lease(current, root=anchor, label=label))
        yield


def _matching_existing_payload_at(
    directory_fd: int,
    path: Path,
    payload: bytes,
    *,
    root: Path,
    label: str,
) -> bool:
    """Verify a deduplicated body through the same pinned parent handle."""
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ArtifactIntegrityError(
                f"{label} existing destination must be a regular file",
                operation="path_resolution",
                details={"path": str(path), "root": str(root)},
            )
        with open(file_fd, "rb", closefd=False) as handle:
            existing = handle.read()
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_directory_binding(directory_fd, path, root=root, label=label)
        if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise ArtifactIntegrityError(
                f"{label} existing destination changed during verification",
                operation="path_resolution",
                details={"path": str(path), "root": str(root)},
            )
        if existing != payload:
            raise ArtifactIntegrityError(
                "Content-addressed path contains different bytes; refusing overwrite",
                operation="write",
                details={"path": str(path)},
            )
        return True
    finally:
        os.close(file_fd)


def _matching_existing_payload_guarded(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    label: str,
) -> bool:
    """Verify an existing Windows body while all ancestors are leased."""
    if not path.exists():
        return False
    _validate_publication_path(path, root=root, label=label)
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        existing = handle.read()
    current = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ArtifactIntegrityError(
            f"{label} existing destination changed during verification",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )
    if existing != payload:
        raise ArtifactIntegrityError(
            "Content-addressed path contains different bytes; refusing overwrite",
            operation="write",
            details={"path": str(path)},
        )
    return True


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    anchor: Path,
    label: str,
    matching_existing: bytes | None = None,
) -> None:
    with _pinned_directory_tree(
        path.parent,
        anchor=anchor,
        root=root,
        label=label,
    ) as directory_fd:
        _validate_publication_path(path, root=root, label=label)
        if directory_fd is not None:
            if matching_existing is not None and _matching_existing_payload_at(
                directory_fd,
                path,
                matching_existing,
                root=root,
                label=label,
            ):
                return
            _atomic_write_bytes_at(
                directory_fd,
                path,
                payload,
                root=root,
                label=label,
            )
            return
        if matching_existing is not None and _matching_existing_payload_guarded(
            path,
            matching_existing,
            root=root,
            label=label,
        ):
            return
        _atomic_write_bytes_guarded(path, payload, root=root, label=label)


def _supports_directory_fd_publication() -> bool:
    """Return whether Python exposes the required openat-style primitives."""
    return _DIRECTORY_FD_PUBLICATION_SUPPORTED


def _backup_existing_destination_at(
    directory_fd: int,
    path: Path,
    *,
    root: Path,
    label: str,
) -> str | None:
    """Hard-link an existing regular destination before atomic replacement."""
    try:
        destination = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"{label} existing destination cannot be inspected safely",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        ) from exc
    if not stat.S_ISREG(destination.st_mode):
        raise ArtifactIntegrityError(
            f"{label} existing destination must be a regular file",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )
    if os.link not in os.supports_dir_fd or os.link not in os.supports_follow_symlinks:
        raise ArtifactIntegrityError(
            f"{label} replacement requires a no-follow directory-relative backup",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )

    backup_name = f".{path.name}.{os.urandom(16).hex()}.rollback"
    try:
        os.link(
            path.name,
            backup_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    except (NotImplementedError, OSError, TypeError) as exc:
        try:
            os.unlink(backup_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise ArtifactIntegrityError(
            f"{label} existing destination could not be preserved before replacement",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        ) from exc
    return backup_name


def _restore_published_destination_at(
    directory_fd: int,
    path: Path,
    backup_name: str | None,
    *,
    root: Path,
    label: str,
) -> None:
    """Rollback a rejected publication through the same pinned directory."""
    try:
        if backup_name is None:
            try:
                os.unlink(path.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        else:
            os.rename(
                backup_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        os.fsync(directory_fd)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"{label} rejected publication could not restore its prior destination",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        ) from exc


def _atomic_write_bytes_at(
    directory_fd: int,
    path: Path,
    payload: bytes,
    *,
    root: Path,
    label: str,
) -> None:
    """Publish relative to a pinned, non-link parent directory handle."""
    temporary_name = f".{path.name}.{os.urandom(16).hex()}.tmp"
    temporary_fd = -1
    backup_name: str | None = None
    published = False
    try:
        _validate_directory_binding(directory_fd, path, root=root, label=label)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(
            temporary_name,
            file_flags,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_directory_binding(directory_fd, path, root=root, label=label)
        backup_name = _backup_existing_destination_at(
            directory_fd,
            path,
            root=root,
            label=label,
        )
        _validate_directory_binding(directory_fd, path, root=root, label=label)
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        published = True
        try:
            _validate_directory_binding(directory_fd, path, root=root, label=label)
            os.fsync(directory_fd)
        except BaseException:
            _restore_published_destination_at(
                directory_fd,
                path,
                backup_name,
                root=root,
                label=label,
            )
            backup_name = None
            raise
        if backup_name is not None:
            try:
                os.unlink(backup_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            else:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    # The new destination was already durably committed. A
                    # failed cleanup fsync can at worst retain the hidden old
                    # hard link after a crash; reporting publication failure
                    # here would incorrectly imply the old state was restored.
                    pass
            backup_name = None
    except BaseException:
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            if backup_name is not None:
                try:
                    os.unlink(backup_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        raise
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)


def _atomic_write_bytes_guarded(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    label: str,
) -> None:
    """Windows fallback pinned by a no-share-delete directory handle."""
    with _windows_directory_lease(path.parent, root=root, label=label):
        _validate_publication_path(path, root=root, label=label)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            _validate_publication_path(temporary, root=root, label=f"{label} temporary")
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_publication_path(path, root=root, label=label)
            os.replace(temporary, path)
            _validate_publication_path(path, root=root, label=label)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


@contextmanager
def _windows_directory_lease(
    path: Path,
    *,
    root: Path,
    label: str,
) -> Iterator[None]:
    """Pin one non-reparse directory so Windows cannot swap it during publish."""
    if os.name != "nt":  # pragma: no cover - called only by the Windows fallback
        raise ArtifactIntegrityError(
            f"{label} publication cannot acquire a safe directory lease",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )

    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    # Deliberately omit FILE_SHARE_DELETE: Windows then rejects any rename,
    # deletion, or junction replacement until the publication is complete.
    handle = create_file(
        str(path),
        0,
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise ArtifactIntegrityError(
            f"{label} publication parent is not a safe local directory",
            operation="path_resolution",
            details={"path": str(path), "root": str(root)},
        )

    try:
        information = _ByHandleFileInformation()
        if (
            not get_information(handle, ctypes.byref(information))
            or (
                information.dwFileAttributes & 0x00000400  # FILE_ATTRIBUTE_REPARSE_POINT
            )
            or not (
                information.dwFileAttributes & 0x00000010  # FILE_ATTRIBUTE_DIRECTORY
            )
        ):
            raise ArtifactIntegrityError(
                f"{label} publication parent must not be a link or junction",
                operation="path_resolution",
                details={"path": str(path), "root": str(root)},
            )
        _validate_publication_path(path / "publication", root=root, label=label)
        yield
        _validate_publication_path(path / "publication", root=root, label=label)
    finally:
        close_handle(handle)


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
