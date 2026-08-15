"""Contract bindings anchored outside the replaceable artifact generation.

Per-contract manifests, binding files, and ``.tombstoned`` files are
recoverable projections.  Initial publication, immutable lifecycle epochs, and
an independently committed expected head are the trusted monotonic authority
in the stable parent.  This matches Disposable Memory's cooperative local
trust model; coherent rollback of the trusted head and its matching authority
tail requires an external counter or journal and is not a security guarantee
made by this filesystem store.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final, Protocol

from ouroboros.persistence.artifact_errors import ArtifactManifestError
from ouroboros.persistence.artifact_validation import validate_manifest

BINDING_MAX_BYTES: Final[int] = 8 * 1024
BINDING_VERSION: Final[int] = 2
TOMBSTONE_VERSION: Final[int] = 1
LIFECYCLE_VERSION: Final[int] = 1
LIFECYCLE_MAX_EPOCHS: Final[int] = 4096
BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "artifact_ref",
        "size_bytes",
        "envelope",
        "referenced_at",
        "initial_active",
        "initial_retain_until",
    }
)
TOMBSTONE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "artifact_ref",
        "timestamp",
        "reason",
        "authority_sha256",
    }
)
EPOCH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "kind",
        "contract_id",
        "authority_sha256",
        "sequence",
        "previous_sha256",
        "timestamp",
        "active",
        "retain_until",
        "terminal",
    }
)
HEAD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "authority_sha256",
        "sequence",
        "head_sha256",
    }
)
_EPOCH_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<sequence>[0-9]{20})\.(?P<digest>[0-9a-f]{64})\.epoch"
)


class AuthorityStore(Protocol):
    root: Path
    _bindings_root: Path
    _directory_anchor: Path
    _lock_directory_anchor: Path

    def _anchor_path(self, contract_id: str) -> Path: ...

    def _binding_path(self, contract_id: str) -> Path: ...

    def _manifest_path(self, contract_id: str) -> Path: ...

    def _read_authority_locked(self, contract_id: str) -> dict[str, Any] | None: ...

    def _write_record_locked(
        self,
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Callable[[], None],
        replace_existing: bool = False,
    ) -> None: ...

    def _write_manifest_locked(
        self,
        contract_id: str,
        manifest: dict[str, Any],
        *,
        authority_check: Callable[[], None] | None = None,
    ) -> None: ...


class BoundedReader(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        max_bytes: int,
        root: Path,
        anchor: Path,
        label: str,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LifecycleState:
    active: bool
    retain_until: str
    updated_at: str
    terminal: dict[str, Any] | None
    sequence: int
    head_sha256: str


def _digest(value: str, *, length: int = 64) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def binding_path(root: Path, contract_id: str) -> Path:
    """Return the replaceable store-generation cache path for one contract."""
    return root / f"{_digest(contract_id)}.json"


def contract_path_component(contract_id: str) -> str:
    """Map public contract identity to one portable filesystem component."""
    return _digest(contract_id)


def authority_prefix(store_root: Path) -> str:
    """Namespace anchors for one artifact store inside its trusted parent."""
    return f".ouroboros-artifact-authority-{_digest(str(store_root), length=24)}-"


def authority_path(parent: Path, store_root: Path, contract_id: str) -> Path:
    """Return one write-once authority path in the stable parent boundary."""
    return parent / f"{authority_prefix(store_root)}{_digest(contract_id)}.json"


def completion_path(anchor: Path) -> Path:
    """Return the stable marker proving initial publication once completed."""
    return anchor.with_suffix(".committed")


def tombstone_path(anchor: Path) -> Path:
    """Return the recoverable terminal projection for one contract."""
    return anchor.with_suffix(".tombstoned")


def lifecycle_path(anchor: Path) -> Path:
    """Return the immutable lifecycle genesis for one contract."""
    return anchor.with_suffix(".lifecycle")


def lifecycle_head_path(anchor: Path) -> Path:
    """Return the mutable expected head for one immutable lifecycle chain."""
    return anchor.with_suffix(".lifecycle.head")


def lifecycle_epoch_prefix(anchor: Path) -> str:
    """Return the bounded stable prefix for one contract's lifecycle epochs."""
    return f".ouroboros-lifecycle-{_digest(str(anchor), length=32)}"


def lifecycle_epoch_path(anchor: Path, sequence: int, digest: str) -> Path:
    """Return a content-addressed immutable lifecycle epoch path."""
    return anchor.parent / f"{lifecycle_epoch_prefix(anchor)}.{sequence:020d}.{digest}.epoch"


def binding_record(
    event: dict[str, Any],
    *,
    contract_id: str,
    active: bool,
    retain_until: str,
) -> dict[str, Any]:
    """Build the complete immutable publication record needed for recovery."""
    return {
        "schema_version": BINDING_VERSION,
        "contract_id": contract_id,
        "artifact_ref": event.get("artifact_ref"),
        "size_bytes": event.get("size_bytes"),
        "envelope": event.get("envelope"),
        "referenced_at": event.get("timestamp"),
        "initial_active": active,
        "initial_retain_until": retain_until,
    }


def encode_record(record: dict[str, Any]) -> bytes:
    """Encode one immutable record for byte-identical retry comparison."""
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


def completion_payload(record: dict[str, Any]) -> bytes:
    """Bind the completion marker to the exact authority bytes."""
    return (hashlib.sha256(encode_record(record)).hexdigest() + "\n").encode("ascii")


def tombstone_record(
    event: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Bind terminal lifecycle evidence to the immutable initial authority."""
    return {
        "schema_version": TOMBSTONE_VERSION,
        "contract_id": authority["contract_id"],
        "artifact_ref": authority["artifact_ref"],
        "timestamp": event.get("timestamp"),
        "reason": event.get("reason"),
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
    }


def lifecycle_genesis_record(authority: dict[str, Any]) -> dict[str, Any]:
    """Build immutable genesis state from the initial publication authority."""
    return {
        "schema_version": LIFECYCLE_VERSION,
        "kind": "genesis",
        "contract_id": authority["contract_id"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
        "sequence": 0,
        "previous_sha256": None,
        "timestamp": authority["referenced_at"],
        "active": authority["initial_active"],
        "retain_until": authority["initial_retain_until"],
        "terminal": None,
    }


def lifecycle_epoch_record(
    authority: dict[str, Any],
    state: LifecycleState,
    *,
    timestamp: str,
    active: bool | None = None,
    retain_until: str | None = None,
    terminal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the next immutable retention or terminal lifecycle epoch."""
    is_terminal = terminal is not None
    return {
        "schema_version": LIFECYCLE_VERSION,
        "kind": "terminal" if is_terminal else "retention",
        "contract_id": authority["contract_id"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
        "sequence": state.sequence + 1,
        "previous_sha256": state.head_sha256,
        "timestamp": timestamp,
        "active": state.active if active is None else active,
        "retain_until": state.retain_until if retain_until is None else retain_until,
        "terminal": terminal,
    }


def lifecycle_head_record(
    authority: dict[str, Any],
    state: LifecycleState | None = None,
) -> dict[str, Any]:
    """Bind the expected lifecycle sequence and digest to initial authority."""
    if state is None:
        genesis = lifecycle_genesis_record(authority)
        state = LifecycleState(
            active=genesis["active"],
            retain_until=genesis["retain_until"],
            updated_at=genesis["timestamp"],
            terminal=None,
            sequence=0,
            head_sha256=hashlib.sha256(encode_record(genesis)).hexdigest(),
        )
    return {
        "schema_version": LIFECYCLE_VERSION,
        "contract_id": authority["contract_id"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
        "sequence": state.sequence,
        "head_sha256": state.head_sha256,
    }


def validate_authority(raw: Any, *, contract_id: str) -> dict[str, Any]:
    """Validate the exact independently anchored record schema."""
    if not isinstance(raw, dict) or frozenset(raw) != BINDING_FIELDS:
        raise ValueError("authority fields do not match the exact versioned schema")
    if raw.get("schema_version") != BINDING_VERSION or raw.get("contract_id") != contract_id:
        raise ValueError("authority identity or schema version is invalid")
    if not isinstance(raw.get("initial_active"), bool):
        raise ValueError("authority initial_active must be boolean")
    for field in ("artifact_ref", "referenced_at", "initial_retain_until"):
        if not isinstance(raw.get(field), str):
            raise ValueError(f"authority {field} must be a string")
    size = raw.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("authority size_bytes must be a non-negative integer")
    if not isinstance(raw.get("envelope"), dict):
        raise ValueError("authority envelope must be an object")
    return raw


def validate_binding(raw: Any, authority: dict[str, Any]) -> None:
    """Require the replaceable cache to equal the stable authority exactly."""
    if raw != authority:
        raise ValueError("binding does not match the independently anchored authority")


def validate_tombstone_authority(
    raw: Any,
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Validate exact monotonic terminal state against initial authority."""
    if not isinstance(raw, dict) or frozenset(raw) != TOMBSTONE_FIELDS:
        raise ValueError("tombstone authority fields do not match the exact schema")
    expected = {
        "schema_version": TOMBSTONE_VERSION,
        "contract_id": authority["contract_id"],
        "artifact_ref": authority["artifact_ref"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
    }
    if any(raw.get(field) != value for field, value in expected.items()):
        raise ValueError("tombstone authority does not match initial authority")
    if not isinstance(raw.get("timestamp"), str) or not isinstance(raw.get("reason"), str):
        raise ValueError("tombstone authority timestamp and reason must be strings")
    return raw


def _validate_lifecycle_record(
    raw: Any,
    authority: dict[str, Any],
    *,
    expected_kind: str,
    expected_sequence: int,
    previous_sha256: str | None,
) -> dict[str, Any]:
    """Validate one exact lifecycle record and its chain position."""
    if not isinstance(raw, dict) or frozenset(raw) != EPOCH_FIELDS:
        raise ValueError("lifecycle record fields do not match the exact schema")
    expected = {
        "schema_version": LIFECYCLE_VERSION,
        "kind": expected_kind,
        "contract_id": authority["contract_id"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
        "sequence": expected_sequence,
        "previous_sha256": previous_sha256,
    }
    if any(raw.get(field) != value for field, value in expected.items()):
        raise ValueError("lifecycle record does not match its authority or chain position")
    if not isinstance(raw.get("active"), bool):
        raise ValueError("lifecycle active state must be boolean")
    for field in ("timestamp", "retain_until"):
        value = raw.get(field)
        if not isinstance(value, str):
            raise ValueError(f"lifecycle {field} must be a string")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"lifecycle {field} must include a timezone")
    terminal = raw.get("terminal")
    if expected_kind == "terminal":
        terminal = validate_tombstone_authority(terminal, authority)
        if raw["timestamp"] != terminal["timestamp"]:
            raise ValueError("terminal lifecycle timestamp does not match its record")
    elif terminal is not None:
        raise ValueError("non-terminal lifecycle record contains terminal state")
    if expected_kind == "genesis":
        genesis = lifecycle_genesis_record(authority)
        if raw != genesis:
            raise ValueError("lifecycle genesis does not match initial authority")
    return raw


def _validate_lifecycle_head(
    raw: Any,
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Validate the independently committed expected lifecycle head."""
    if not isinstance(raw, dict) or frozenset(raw) != HEAD_FIELDS:
        raise ValueError("lifecycle head fields do not match the exact schema")
    expected = {
        "schema_version": LIFECYCLE_VERSION,
        "contract_id": authority["contract_id"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
    }
    if any(raw.get(field) != value for field, value in expected.items()):
        raise ValueError("lifecycle head does not match initial authority")
    sequence = raw.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 <= sequence <= LIFECYCLE_MAX_EPOCHS
    ):
        raise ValueError("lifecycle head sequence is invalid")
    head_sha256 = raw.get("head_sha256")
    if not isinstance(head_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", head_sha256) is None:
        raise ValueError("lifecycle head digest is invalid")
    return raw


def _write_lifecycle_head(
    store: AuthorityStore,
    authority: dict[str, Any],
    state: LifecycleState,
    *,
    authority_check: Callable[[], None],
    replace_existing: bool,
) -> None:
    """Atomically publish the expected head after its immutable record exists."""
    store._write_record_locked(
        lifecycle_head_path(store._anchor_path(authority["contract_id"])),
        encode_record(lifecycle_head_record(authority, state)),
        stable=True,
        authority_check=authority_check,
        replace_existing=replace_existing,
    )


def read_lifecycle_state(
    store: AuthorityStore,
    authority: dict[str, Any],
    *,
    read_bounded: BoundedReader,
    authority_check: Callable[[], None],
) -> LifecycleState:
    """Read and verify the complete append-only lifecycle epoch chain."""
    anchor = store._anchor_path(authority["contract_id"])
    genesis_path = lifecycle_path(anchor)
    genesis = _validate_lifecycle_record(
        json.loads(
            read_bounded(
                genesis_path,
                max_bytes=BINDING_MAX_BYTES,
                root=store.root.parent,
                anchor=store._lock_directory_anchor,
                label="artifact lifecycle genesis",
            )
        ),
        authority,
        expected_kind="genesis",
        expected_sequence=0,
        previous_sha256=None,
    )
    head_sha256 = hashlib.sha256(encode_record(genesis)).hexdigest()
    state = LifecycleState(
        active=genesis["active"],
        retain_until=genesis["retain_until"],
        updated_at=genesis["timestamp"],
        terminal=None,
        sequence=0,
        head_sha256=head_sha256,
    )
    expected_head = _validate_lifecycle_head(
        json.loads(
            read_bounded(
                lifecycle_head_path(anchor),
                max_bytes=BINDING_MAX_BYTES,
                root=store.root.parent,
                anchor=store._lock_directory_anchor,
                label="artifact lifecycle head",
            )
        ),
        authority,
    )
    committed_head_matches = (
        expected_head["sequence"] == 0 and expected_head["head_sha256"] == state.head_sha256
    )
    epoch_prefix = lifecycle_epoch_prefix(anchor)
    prefix = f"{epoch_prefix}."
    paths: list[Path] = []
    for path in genesis_path.parent.glob(f"{epoch_prefix}.*.epoch"):
        if len(paths) >= LIFECYCLE_MAX_EPOCHS:
            raise ValueError("lifecycle epoch count exceeds the bounded limit")
        paths.append(path)
    paths.sort()
    for path in paths:
        suffix = path.name.removeprefix(prefix)
        match = _EPOCH_NAME_PATTERN.fullmatch(suffix)
        if match is None:
            raise ValueError("lifecycle epoch path does not match the exact schema")
        sequence = int(match.group("sequence"))
        if sequence != state.sequence + 1:
            raise ValueError("lifecycle epoch chain is forked or non-contiguous")
        if state.terminal is not None:
            raise ValueError("lifecycle epoch exists after terminal state")
        raw = json.loads(
            read_bounded(
                path,
                max_bytes=BINDING_MAX_BYTES,
                root=store.root.parent,
                anchor=store._lock_directory_anchor,
                label="artifact lifecycle epoch",
            )
        )
        kind = raw.get("kind") if isinstance(raw, dict) else None
        if kind not in {"retention", "terminal"}:
            raise ValueError("lifecycle epoch kind is invalid")
        epoch = _validate_lifecycle_record(
            raw,
            authority,
            expected_kind=kind,
            expected_sequence=sequence,
            previous_sha256=state.head_sha256,
        )
        digest = hashlib.sha256(encode_record(epoch)).hexdigest()
        if match.group("digest") != digest:
            raise ValueError("lifecycle epoch content address does not match its record")
        state = LifecycleState(
            active=epoch["active"],
            retain_until=epoch["retain_until"],
            updated_at=epoch["timestamp"],
            terminal=epoch["terminal"],
            sequence=sequence,
            head_sha256=digest,
        )
        if sequence == expected_head["sequence"]:
            committed_head_matches = digest == expected_head["head_sha256"]
    if state.sequence < expected_head["sequence"]:
        raise ValueError("lifecycle epoch chain is shorter than its committed head")
    if not committed_head_matches:
        raise ValueError("lifecycle epoch chain does not match its committed head")
    if state.sequence > expected_head["sequence"] + 1:
        raise ValueError("lifecycle epoch chain has multiple uncommitted successors")
    if state.sequence > expected_head["sequence"]:
        _write_lifecycle_head(
            store,
            authority,
            state,
            authority_check=authority_check,
            replace_existing=True,
        )
    return state


def append_lifecycle_epoch(
    store: AuthorityStore,
    authority: dict[str, Any],
    state: LifecycleState,
    *,
    timestamp: str,
    active: bool | None = None,
    retain_until: str | None = None,
    terminal: dict[str, Any] | None = None,
    authority_check: Callable[[], None],
) -> LifecycleState:
    """Publish one immutable content-addressed lifecycle transition."""
    if state.sequence >= LIFECYCLE_MAX_EPOCHS:
        raise ArtifactManifestError(
            "Lifecycle epoch count exceeds the bounded limit",
            operation="write",
            details={"contract_id": authority["contract_id"]},
        )
    epoch = lifecycle_epoch_record(
        authority,
        state,
        timestamp=timestamp,
        active=active,
        retain_until=retain_until,
        terminal=terminal,
    )
    payload = encode_record(epoch)
    digest = hashlib.sha256(payload).hexdigest()
    store._write_record_locked(
        lifecycle_epoch_path(
            store._anchor_path(authority["contract_id"]), epoch["sequence"], digest
        ),
        payload,
        stable=True,
        authority_check=authority_check,
    )
    next_state = LifecycleState(
        active=epoch["active"],
        retain_until=epoch["retain_until"],
        updated_at=epoch["timestamp"],
        terminal=epoch["terminal"],
        sequence=epoch["sequence"],
        head_sha256=digest,
    )
    _write_lifecycle_head(
        store,
        authority,
        next_state,
        authority_check=authority_check,
        replace_existing=True,
    )
    return next_state


def tombstone_event(record: dict[str, Any]) -> dict[str, Any]:
    """Return the exact manifest projection of stable terminal authority."""
    return {
        "type": "artifact.tombstoned",
        "timestamp": record["timestamp"],
        "artifact_ref": record["artifact_ref"],
        "reason": record["reason"],
    }


def validate_manifest_authority(
    manifest: dict[str, Any],
    authority: dict[str, Any],
    terminal: dict[str, Any] | None = None,
) -> bool:
    """Require replay and tombstone history to match stable authority."""
    references = [
        event for event in manifest["events"] if event.get("type") == "artifact.referenced"
    ]
    if len(references) != 1:
        raise ValueError("manifest must contain exactly one artifact reference")
    reference = references[0]
    expected = {
        "artifact_ref": authority["artifact_ref"],
        "size_bytes": authority["size_bytes"],
        "envelope": authority["envelope"],
        "timestamp": authority["referenced_at"],
    }
    if any(reference.get(field) != value for field, value in expected.items()):
        raise ValueError("manifest reference does not match independently anchored authority")
    tombstones = [
        event for event in manifest["events"] if event.get("type") == "artifact.tombstoned"
    ]
    if terminal is None:
        if tombstones:
            raise ValueError("manifest tombstone is missing monotonic terminal authority")
        if manifest["events"] != references:
            raise ValueError("manifest history does not match initial authority")
        return False
    expected_tombstone = tombstone_event(terminal)
    if tombstones:
        if tombstones != [expected_tombstone] or manifest["events"] != [
            reference,
            expected_tombstone,
        ]:
            raise ValueError("manifest tombstone does not match terminal authority")
        return False
    if manifest["events"] != references:
        raise ValueError("manifest history does not match terminal authority")
    manifest["events"].append(expected_tombstone)
    manifest["updated_at"] = terminal["timestamp"]
    return True


def manifest_from_authority(authority: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct mutable metadata after a partial initial publication."""
    timestamp = authority["referenced_at"]
    return {
        "schema_version": 1,
        "contract_id": authority["contract_id"],
        "active": authority["initial_active"],
        "retain_until": authority["initial_retain_until"],
        "updated_at": timestamp,
        "events": [
            {
                "type": "artifact.referenced",
                "timestamp": timestamp,
                "artifact_ref": authority["artifact_ref"],
                "size_bytes": authority["size_bytes"],
                "envelope": authority["envelope"],
            }
        ],
    }


def reconcile_contract_authority(
    store: AuthorityStore,
    contract_id: str,
    manifest: dict[str, Any],
    *,
    authority_check: Callable[[], None],
    read_bounded: BoundedReader,
) -> dict[str, Any]:
    """Reconcile replaceable metadata with monotonic stable authorities."""
    anchor_path = store._anchor_path(contract_id)
    lifecycle = lifecycle_path(anchor_path)
    head = lifecycle_head_path(anchor_path)
    terminal_path = tombstone_path(anchor_path)
    anchor = store._read_authority_locked(contract_id)
    binding = store._binding_path(contract_id)
    if anchor is None:
        epoch_prefix = lifecycle_epoch_prefix(anchor_path)
        epochs_exist = any(lifecycle.parent.glob(f"{epoch_prefix}.*.epoch"))
        if (
            manifest["events"]
            or binding.exists()
            or lifecycle.exists()
            or head.exists()
            or epochs_exist
            or terminal_path.exists()
        ):
            raise ArtifactManifestError(
                "Contract metadata is missing independently anchored authority",
                operation="read",
                details={"contract_id": contract_id},
            )
        return manifest
    marker = completion_path(anchor_path)
    try:
        state = read_lifecycle_state(
            store,
            anchor,
            read_bounded=read_bounded,
            authority_check=authority_check,
        )
    except FileNotFoundError:
        if marker.exists():
            raise ArtifactManifestError(
                "Committed contract lifecycle authority is missing",
                operation="read",
                details={"contract_id": contract_id, "path": str(lifecycle)},
            ) from None
        genesis = lifecycle_genesis_record(anchor)
        initial_state = LifecycleState(
            active=genesis["active"],
            retain_until=genesis["retain_until"],
            updated_at=genesis["timestamp"],
            terminal=None,
            sequence=0,
            head_sha256=hashlib.sha256(encode_record(genesis)).hexdigest(),
        )
        if not lifecycle.exists():
            store._write_record_locked(
                lifecycle,
                encode_record(genesis),
                stable=True,
                authority_check=authority_check,
            )
        if not head.exists():
            _write_lifecycle_head(
                store,
                anchor,
                initial_state,
                authority_check=authority_check,
                replace_existing=False,
            )
        try:
            state = read_lifecycle_state(
                store,
                anchor,
                read_bounded=read_bounded,
                authority_check=authority_check,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactManifestError(
                "Artifact lifecycle authority is invalid",
                operation="read",
                details={"contract_id": contract_id, "path": str(lifecycle)},
            ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact lifecycle authority is invalid",
            operation="read",
            details={"contract_id": contract_id, "path": str(lifecycle)},
        ) from exc
    if state.terminal is not None and not marker.exists():
        raise ArtifactManifestError(
            "Terminal lifecycle requires completed initial publication",
            operation="read",
            details={"contract_id": contract_id},
        )
    try:
        cached_terminal = validate_tombstone_authority(
            json.loads(
                read_bounded(
                    terminal_path,
                    max_bytes=BINDING_MAX_BYTES,
                    root=store.root.parent,
                    anchor=store._lock_directory_anchor,
                    label="artifact tombstone authority",
                )
            ),
            anchor,
        )
    except FileNotFoundError:
        cached_terminal = None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact tombstone authority is invalid",
            operation="read",
            details={"contract_id": contract_id, "path": str(terminal_path)},
        ) from exc
    terminal = state.terminal
    if terminal is None and cached_terminal is not None:
        raise ArtifactManifestError(
            "Terminal record exists without monotonic lifecycle authority",
            operation="read",
            details={"contract_id": contract_id},
        )
    if terminal is not None:
        if cached_terminal is None:
            store._write_record_locked(
                terminal_path,
                encode_record(terminal),
                stable=True,
                authority_check=authority_check,
            )
        elif cached_terminal != terminal:
            raise ArtifactManifestError(
                "Terminal record does not match monotonic lifecycle authority",
                operation="read",
                details={"contract_id": contract_id},
            )
    recovering = not manifest["events"]
    terminal_recovered = False
    lifecycle_recovered = False
    if recovering:
        if marker.exists():
            raise ArtifactManifestError(
                "Committed contract manifest binding is missing; recovery refused",
                operation="read",
                details={"contract_id": contract_id},
            )
        manifest = validate_manifest(
            manifest_from_authority(anchor),
            contract_id=contract_id,
            path=store._manifest_path(contract_id),
        )
    else:
        try:
            terminal_recovered = validate_manifest_authority(manifest, anchor, terminal)
        except ValueError as exc:
            raise ArtifactManifestError(
                "Contract manifest binding does not match independently anchored initial or terminal authority",
                operation="read",
                details={"contract_id": contract_id},
            ) from exc
    expected_lifecycle_projection = {
        "active": state.active,
        "retain_until": state.retain_until,
        "updated_at": state.updated_at,
    }
    lifecycle_recovered = any(
        manifest.get(field) != value for field, value in expected_lifecycle_projection.items()
    )
    manifest.update(expected_lifecycle_projection)
    try:
        cached = json.loads(
            read_bounded(
                binding,
                max_bytes=BINDING_MAX_BYTES,
                root=store._bindings_root,
                anchor=store._directory_anchor,
                label="artifact binding",
            )
        )
        validate_binding(cached, anchor)
    except FileNotFoundError:
        store._write_record_locked(
            binding,
            encode_record(anchor),
            stable=False,
            authority_check=authority_check,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact binding does not match independently anchored authority",
            operation="read",
            details={"contract_id": contract_id},
        ) from exc
    if recovering or terminal_recovered or lifecycle_recovered:
        store._write_manifest_locked(
            contract_id,
            manifest,
            authority_check=authority_check,
        )
    expected_marker = completion_payload(anchor)
    if marker.exists():
        actual_marker = read_bounded(
            marker,
            max_bytes=len(expected_marker),
            root=store.root.parent,
            anchor=store._lock_directory_anchor,
            label="artifact authority completion",
        )
        if actual_marker != expected_marker:
            raise ArtifactManifestError(
                "Artifact authority completion marker is invalid",
                operation="read",
                details={"contract_id": contract_id},
            )
    else:
        store._write_record_locked(
            marker,
            expected_marker,
            stable=True,
            authority_check=authority_check,
        )
    return manifest


__all__ = [
    "BINDING_MAX_BYTES",
    "LIFECYCLE_MAX_EPOCHS",
    "LifecycleState",
    "append_lifecycle_epoch",
    "authority_path",
    "authority_prefix",
    "binding_path",
    "binding_record",
    "completion_path",
    "completion_payload",
    "contract_path_component",
    "encode_record",
    "lifecycle_epoch_path",
    "lifecycle_epoch_prefix",
    "lifecycle_genesis_record",
    "lifecycle_head_path",
    "lifecycle_head_record",
    "lifecycle_path",
    "manifest_from_authority",
    "read_lifecycle_state",
    "reconcile_contract_authority",
    "tombstone_event",
    "tombstone_path",
    "tombstone_record",
    "validate_authority",
    "validate_binding",
    "validate_manifest_authority",
    "validate_tombstone_authority",
]
