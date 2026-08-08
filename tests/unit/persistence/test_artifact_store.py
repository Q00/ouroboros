"""Disposable Memory CAS, replay, retention, and tombstone contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
from typing import Any

import pytest

from ouroboros.core.disposable_memory import MAX_DISPOSABLE_ARTIFACT_BYTES
from ouroboros.persistence.artifact_binding import (
    LifecycleState,
    encode_record,
    lifecycle_epoch_path,
    lifecycle_epoch_prefix,
    lifecycle_epoch_record,
)
from ouroboros.persistence.artifact_schema import MANIFEST_MAX_BYTES
import ouroboros.persistence.artifact_store as artifact_store_module
from ouroboros.persistence.artifact_store import (
    ArtifactContractConflictError,
    ArtifactIntegrityError,
    ArtifactManifestError,
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactTombstonedError,
    ArtifactTooLargeError,
    ContentAddressedArtifactStore,
    canonical_artifact_bytes,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _store(tmp_path: Path) -> ContentAddressedArtifactStore:
    return ContentAddressedArtifactStore(tmp_path / "artifacts")


def _put(
    store: ContentAddressedArtifactStore,
    contract_id: str,
    body: object,
    *,
    active: bool = False,
    retain_until: datetime | None = None,
    commit_check: Any = None,
):
    return store.put_for_contract(
        contract_id=contract_id,
        body=body,
        runtime_id="test-runtime",
        duration_ms=12,
        events_emitted_count=3,
        active=active,
        retain_until=retain_until or NOW + timedelta(days=90),
        now=NOW,
        commit_check=commit_check,
    )


def _blob_path(store: ContentAddressedArtifactStore, artifact_ref: str) -> Path:
    digest = artifact_ref.removeprefix("sha256:")
    return store.root / digest[:2] / f"{digest}.json"


def _age(path: Path, *, days: int) -> None:
    timestamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _manifest(store: ContentAddressedArtifactStore, contract_id: str) -> dict:
    path = store._manifest_path(contract_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _contract_root(store: ContentAddressedArtifactStore, contract_id: str) -> Path:
    return store._manifest_path(contract_id).parent


def _lifecycle_epochs(
    store: ContentAddressedArtifactStore,
    contract_id: str,
) -> list[Path]:
    anchor = store._anchor_path(contract_id)
    return sorted(anchor.parent.glob(f"{lifecycle_epoch_prefix(anchor)}.*.epoch"))


def _substitute_contract_artifact(
    store: ContentAddressedArtifactStore,
    *,
    victim: str = "CONTRACTA",
    source: str = "CONTRACTB",
    replace_binding: bool = False,
) -> tuple[Path, Path, Path]:
    """Replace a victim's reference with another valid contract's artifact."""
    victim_envelope = _put(store, victim, {"owner": "a"})
    source_envelope = _put(store, source, {"owner": "b", "payload": "different"})
    victim_path = store._manifest_path(victim)
    victim_manifest = _manifest(store, victim)
    source_event = _manifest(store, source)["events"][0]
    victim_event = victim_manifest["events"][0]
    victim_event["artifact_ref"] = source_event["artifact_ref"]
    victim_event["size_bytes"] = source_event["size_bytes"]
    victim_event["envelope"]["artifact_ref"] = source_event["artifact_ref"]
    victim_path.write_text(json.dumps(victim_manifest), encoding="utf-8")
    if replace_binding:
        victim_binding = store._binding_path(victim)
        binding = json.loads(victim_binding.read_text(encoding="utf-8"))
        binding["artifact_ref"] = source_event["artifact_ref"]
        binding["size_bytes"] = source_event["size_bytes"]
        binding["envelope"]["artifact_ref"] = source_event["artifact_ref"]
        victim_binding.write_text(json.dumps(binding), encoding="utf-8")
    return (
        victim_path,
        _blob_path(store, victim_envelope.artifact_ref),
        _blob_path(store, source_envelope.artifact_ref),
    )


def _try_replaced_artifact_lock(
    artifact_root: str,
    contract_id: str | None,
    result: Any,
) -> None:
    """Attempt a nonblocking replacement-inode acquisition in a child process."""
    store = ContentAddressedArtifactStore(Path(artifact_root))
    try:
        if contract_id is None:
            lock = store._store_lock(exclusive=True, blocking=False)
        else:
            lock = store.contract_execution_lock(contract_id, blocking=False)
        with lock:
            result.put("acquired")
    except BlockingIOError:
        result.put("blocked")
    except BaseException as exc:  # pragma: no cover - diagnostic transport
        result.put(f"error:{type(exc).__name__}:{exc}")


def _publish_after_store_replacement(
    artifact_root: str,
    attempting: Any,
    result: Any,
) -> None:
    """Publish through a second store generation in a spawned process."""
    store = ContentAddressedArtifactStore(Path(artifact_root))
    attempting.put("attempting")
    try:
        envelope = _put(store, "CONTRACTRACE", {"writer": "second"})
        result.put(("published", envelope.artifact_ref))
    except BaseException as exc:  # pragma: no cover - diagnostic transport
        result.put(("error", f"{type(exc).__name__}:{exc}"))


def _assert_replaced_lock_authority_stays_held(
    store: ContentAddressedArtifactStore,
    lock: Any,
    lock_path: Path,
    contract_id: str | None,
) -> None:
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(
        target=_try_replaced_artifact_lock,
        args=(str(store.root), contract_id, result),
    )

    try:
        with pytest.raises(OSError, match="lockfile changed while locked"):
            with lock:
                lock_path.unlink()
                process.start()
                process.join(20)
                assert process.exitcode == 0
                assert result.get(timeout=5) == "blocked"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink semantics only")
def test_store_lock_replacement_cannot_split_serialization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()

    _assert_replaced_lock_authority_stays_held(
        store,
        store._store_lock(exclusive=True),
        store.root / ".artifact-store.lock",
        None,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink semantics only")
def test_contract_lock_replacement_cannot_duplicate_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    contract_id = "CONTRACTLOCK"
    store.initialize()
    lock_target = store._contract_execution_lock_target(contract_id)

    _assert_replaced_lock_authority_stays_held(
        store,
        store.contract_execution_lock(contract_id),
        lock_target.with_suffix(lock_target.suffix + ".lock"),
        contract_id,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics only")
def test_stable_store_authority_replacement_cannot_split_serialization(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(
        target=_try_replaced_artifact_lock,
        args=(str(store.root), None, result),
    )
    authority_path: Path | None = None
    displaced_path: Path | None = None

    try:
        with pytest.raises(OSError, match="lockfile changed while locked"):
            with store._store_lock(exclusive=True):
                authority_paths = list(store.root.parent.glob(".ouroboros-artifact-store-*.lock"))
                assert len(authority_paths) == 1
                authority_path = authority_paths[0]
                displaced_path = authority_path.with_name(f"{authority_path.name}.displaced")
                authority_path.rename(displaced_path)
                authority_path.touch(mode=0o600)

                process.start()
                process.join(20)
                assert process.exitcode == 0
                assert result.get(timeout=5) == "blocked"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)

    assert authority_path is not None
    assert displaced_path is not None
    assert authority_path.parent == store.root.parent
    assert displaced_path.parent == store.root.parent
    assert authority_path.is_file()
    assert displaced_path.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_contract_authority_reuses_parent_for_nested_store_publication(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    contract_id = "CONTRACTNESTED"

    with store.contract_execution_lock(contract_id):
        with store._store_lock(exclusive=True, blocking=False):
            pass
        envelope = _put(store, contract_id, {"nested": "publication"})

    assert store.fetch(contract_id).envelope == envelope


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics only")
def test_contract_parent_replacement_cannot_create_competing_authority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    contract_id = "CONTRACTPARENT"
    store.initialize()
    contract_root = _contract_root(store, contract_id)
    displaced = tmp_path / "displaced-contract"
    lock = store.contract_execution_lock(contract_id)

    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(
        target=_try_replaced_artifact_lock,
        args=(str(store.root), contract_id, result),
    )
    try:
        with pytest.raises((OSError, ArtifactIntegrityError), match="changed"):
            with lock:
                contract_root.rename(displaced)
                process.start()
                process.join(20)
                assert process.exitcode == 0
                assert result.get(timeout=5) == "blocked"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics only")
def test_store_root_replacement_cannot_overwrite_contract_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    displaced = tmp_path / "displaced-store"
    context = multiprocessing.get_context("spawn")
    attempting = context.Queue()
    result = context.Queue()
    process = context.Process(
        target=_publish_after_store_replacement,
        args=(str(store.root), attempting, result),
    )

    def replace_root_at_commit() -> None:
        store.root.rename(displaced)
        process.start()
        assert attempting.get(timeout=20) == "attempting"
        process.join(1)

    try:
        with pytest.raises((OSError, ArtifactIntegrityError), match="changed"):
            _put(
                store,
                "CONTRACTRACE",
                {"writer": "first"},
                commit_check=replace_root_at_commit,
            )
        process.join(20)
        assert process.exitcode == 0
        assert result.get(timeout=5)[0] == "published"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)

    assert store.fetch("CONTRACTRACE").body == {"writer": "second"}


def test_put_uses_content_addressed_layout_and_deduplicates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"answer": 42})
    second = _put(store, "CONTRACT2", {"answer": 42})

    assert first.artifact_ref == second.artifact_ref
    path = _blob_path(store, first.artifact_ref)
    assert path == store.root / first.artifact_ref[7:9] / f"{first.artifact_ref[7:]}.json"
    assert path.read_bytes() == canonical_artifact_bytes({"answer": 42})
    assert list(store.root.glob("[0-9a-f][0-9a-f]/*.json")) == [path]


def test_commit_gate_immediately_precedes_first_publication_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    sequence: list[str] = []
    original_load = store._load_manifest_locked
    original_write = store._write_blob_locked

    def track_load(contract_id: str, *, missing_ok: bool) -> dict[str, Any]:
        sequence.append("load")
        return original_load(contract_id, missing_ok=missing_ok)

    def track_write(
        digest: str,
        payload: bytes,
        *,
        authority_check: Any = None,
    ) -> None:
        sequence.append("write")
        original_write(digest, payload, authority_check=authority_check)

    monkeypatch.setattr(store, "_load_manifest_locked", track_load)
    monkeypatch.setattr(store, "_write_blob_locked", track_write)

    store.put_for_contract(
        contract_id="CONTRACT-GATE",
        body={"durable": True},
        runtime_id="test-runtime",
        duration_ms=12,
        events_emitted_count=3,
        now=NOW,
        precommit_check=lambda: sequence.append("precommit"),
        commit_check=lambda: sequence.append("commit"),
    )

    assert sequence == ["precommit", "load", "commit", "write"]


def test_fetch_is_explicit_and_verifies_content_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"large": "payload"})

    fetched = store.fetch("CONTRACT1")
    assert fetched.envelope == envelope
    assert fetched.body == {"large": "payload"}

    _blob_path(store, envelope.artifact_ref).write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="hash does not match"):
        store.fetch("CONTRACT1")


def test_missing_contract_reads_do_not_create_store_state(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore.for_project(tmp_path)

    assert store.envelope_if_exists("MISSING1") is None
    assert store.fetch_if_exists("MISSING1") is None
    with pytest.raises(ArtifactNotFoundError):
        store.fetch("MISSING1")

    assert not (tmp_path / ".ouroboros").exists()


def test_fetch_and_replay_reject_oversized_stored_body_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"bounded": True})
    path = _blob_path(store, envelope.artifact_ref)
    path.write_bytes(b"x" * (MAX_DISPOSABLE_ARTIFACT_BYTES + 1))
    read_sizes: list[int] = []
    original_read = os.read

    def tracked_read(file_descriptor: int, byte_count: int) -> bytes:
        if os.fstat(file_descriptor).st_size > MAX_DISPOSABLE_ARTIFACT_BYTES:
            read_sizes.append(byte_count)
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(artifact_store_module.os, "read", tracked_read)

    for operation in (store.fetch, store.replay):
        with pytest.raises(ArtifactIntegrityError, match="exceeds the configured"):
            operation("CONTRACT1")
    assert read_sizes == []


def test_dedup_rejects_oversized_stored_body_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"bounded": True})
    path = _blob_path(store, envelope.artifact_ref)
    path.write_bytes(b"x" * (MAX_DISPOSABLE_ARTIFACT_BYTES + 1))
    read_sizes: list[int] = []
    original_read = os.read

    def tracked_read(file_descriptor: int, byte_count: int) -> bytes:
        read_sizes.append(byte_count)
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(artifact_store_module.os, "read", tracked_read)

    with pytest.raises(ArtifactIntegrityError, match="different bytes"):
        _put(store, "CONTRACT2", {"bounded": True})
    assert read_sizes == []


def test_fetch_bounds_read_when_body_grows_after_size_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"bounded": True})
    path = _blob_path(store, envelope.artifact_ref)
    body_inode = path.stat().st_ino
    read_sizes: list[int] = []
    original_read = os.read

    def growing_read(file_descriptor: int, byte_count: int) -> bytes:
        if os.fstat(file_descriptor).st_ino != body_inode:
            return original_read(file_descriptor, byte_count)
        if not read_sizes:
            path.write_bytes(b"x" * (MAX_DISPOSABLE_ARTIFACT_BYTES + 2))
        read_sizes.append(byte_count)
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(artifact_store_module.os, "read", growing_read)

    with pytest.raises(ArtifactIntegrityError, match="exceeds the configured"):
        store.fetch("CONTRACT1")
    assert sum(read_sizes) == MAX_DISPOSABLE_ARTIFACT_BYTES + 1
    assert max(read_sizes) <= 64 * 1024


def test_json_native_values_round_trip_without_normalization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = {
        "null": None,
        "boolean": True,
        "integer": 42,
        "number": 1.25,
        "string": "value",
        "nested": [False, {"items": [1, 2, 3]}],
    }

    _put(store, "CONTRACT1", body)

    assert store.fetch("CONTRACT1").body == body


@pytest.mark.parametrize(
    "body",
    [
        {"tuple": (1, 2)},
        {1: "integer-key"},
        {True: "boolean-key"},
    ],
    ids=["tuple", "integer-key", "boolean-key"],
)
def test_lossy_non_json_native_values_are_rejected(tmp_path: Path, body: object) -> None:
    store = _store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="JSON-native|JSON strings"):
        _put(store, "CONTRACT1", body)


def test_contract_id_cannot_be_reused_for_different_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"version": 1})
    assert _put(store, "CONTRACT1", {"version": 1}) == first

    with pytest.raises(ArtifactContractConflictError, match="different artifact"):
        _put(store, "CONTRACT1", {"version": 2})


def test_exact_one_mib_encoded_body_is_allowed_and_one_byte_more_is_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    empty_size = len(canonical_artifact_bytes({"output": ""}))
    exact = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size)}
    oversized = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size + 1)}
    assert len(canonical_artifact_bytes(exact)) == MAX_DISPOSABLE_ARTIFACT_BYTES

    envelope = _put(store, "CONTRACT1", exact)
    assert _blob_path(store, envelope.artifact_ref).stat().st_size == MAX_DISPOSABLE_ARTIFACT_BYTES
    with pytest.raises(ArtifactTooLargeError, match="exceeds"):
        _put(store, "CONTRACT2", oversized)

    with pytest.raises(ValueError, match="1 MiB hard cap"):
        ContentAddressedArtifactStore(
            tmp_path / "too-large-cap",
            max_artifact_bytes=MAX_DISPOSABLE_ARTIFACT_BYTES + 1,
        )


def test_prune_is_dry_run_by_default_and_fans_out_tombstones_before_delete(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _put(
        store,
        "CONTRACT1",
        {"shared": True},
        retain_until=NOW - timedelta(days=1),
    )
    second = _put(
        store,
        "CONTRACT2",
        {"shared": True},
        retain_until=NOW - timedelta(days=1),
    )
    assert first.artifact_ref == second.artifact_ref
    path = _blob_path(store, first.artifact_ref)
    _age(path, days=100)

    plan = store.prune(ttl=timedelta(days=90), now=NOW)
    assert plan.applied is False
    assert plan.candidates[0].contract_ids == ("CONTRACT1", "CONTRACT2")
    assert path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.referenced"

    applied = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert applied.removed_refs == (first.artifact_ref,)
    assert not path.exists()
    for contract_id in ("CONTRACT1", "CONTRACT2"):
        assert _manifest(store, contract_id)["events"][-1]["type"] == "artifact.tombstoned"
        with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
            store.replay(contract_id)


def test_active_and_retained_contracts_are_protected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    active = _put(
        store,
        "ACTIVE1",
        {"kind": "active"},
        active=True,
        retain_until=NOW - timedelta(days=1),
    )
    retained = _put(
        store,
        "RETAINED1",
        {"kind": "retained"},
        retain_until=NOW + timedelta(days=1),
    )
    _age(_blob_path(store, active.artifact_ref), days=100)
    _age(_blob_path(store, retained.artifact_ref), days=100)

    default_plan = store.prune(ttl=timedelta(days=90), now=NOW)
    assert default_plan.candidates == ()

    opted_in = store.prune(
        ttl=timedelta(days=90),
        allow_replay_tombstone=True,
        now=NOW,
    )
    assert [item.artifact_ref for item in opted_in.candidates] == [retained.artifact_ref]


def test_unreferenced_old_blob_is_collectable_without_tombstone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    payload = canonical_artifact_bytes({"orphan": True})
    digest = hashlib.sha256(payload).hexdigest()
    path = store.root / digest[:2] / f"{digest}.json"
    path.parent.mkdir()
    path.write_bytes(payload)
    _age(path, days=100)

    report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert report.candidates[0].contract_ids == ()
    assert not path.exists()


def test_malformed_manifest_aborts_prune_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"safe": True})
    path = _blob_path(store, envelope.artifact_ref)
    _age(path, days=100)
    manifest_path = store._manifest_path("CONTRACT1")
    manifest_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="fail-closed"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert path.exists()


@pytest.mark.parametrize("operation", ["fetch", "prune"])
def test_oversized_manifest_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"safe": True})
    manifest_path = store._manifest_path("CONTRACT1")
    manifest_path.write_bytes(b"{" + b" " * MANIFEST_MAX_BYTES)
    manifest_inode = manifest_path.stat().st_ino
    manifest_reads: list[int] = []
    original_read = os.read

    def tracked_read(file_descriptor: int, byte_count: int) -> bytes:
        if os.fstat(file_descriptor).st_ino == manifest_inode:
            manifest_reads.append(byte_count)
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(artifact_store_module.os, "read", tracked_read)

    with pytest.raises(ArtifactIntegrityError, match="exceeds the configured"):
        if operation == "fetch":
            store.fetch("CONTRACT1")
        else:
            store.prune(now=NOW)
    assert manifest_reads == []


@pytest.mark.parametrize("operation", ["retry", "prune"])
def test_manifest_read_rejects_contract_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"real": True})
    contract_dir = _contract_root(store, "CONTRACT1")
    manifest_path = contract_dir / "events.json"
    manifest_inode = manifest_path.stat().st_ino
    displaced = tmp_path / f"manifest-displaced-{operation}"
    external = tmp_path / f"manifest-external-{operation}"
    external.mkdir()
    attack_manifest = _manifest(store, "CONTRACT1")
    attack_manifest["events"][0]["envelope"]["runtime_id"] = "attacker-runtime"
    (external / "events.json").write_text(json.dumps(attack_manifest), encoding="utf-8")
    original_read = os.read
    swapped = False

    def swap_during_read(file_descriptor: int, byte_count: int) -> bytes:
        nonlocal swapped
        if not swapped and os.fstat(file_descriptor).st_ino == manifest_inode:
            contract_dir.rename(displaced)
            try:
                contract_dir.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(artifact_store_module.os, "read", swap_during_read)

    with pytest.raises(ArtifactIntegrityError, match="link|changed"):
        if operation == "retry":
            store.envelope_if_exists("CONTRACT1")
        else:
            store.prune(now=NOW)
    assert swapped


@pytest.mark.parametrize("operation", ["retry", "retention", "prune"])
@pytest.mark.parametrize("forbidden_field", ["body", "transcript"])
def test_manifest_rejects_and_preserves_forbidden_fields(
    tmp_path: Path,
    operation: str,
    forbidden_field: str,
) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"safe": True})
    manifest_path = store._manifest_path("CONTRACT1")
    manifest = _manifest(store, "CONTRACT1")
    if forbidden_field == "body":
        manifest["body"] = {"must": "not persist"}
    else:
        manifest["events"][0]["transcript"] = "must not persist"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = manifest_path.read_bytes()

    with pytest.raises(ArtifactManifestError, match="exact versioned schema"):
        if operation == "retry":
            store.envelope_if_exists("CONTRACT1")
        elif operation == "retention":
            store.set_contract_retention(
                "CONTRACT1",
                active=False,
                retain_until=NOW + timedelta(days=1),
                now=NOW,
            )
        else:
            store.prune(now=NOW)
    assert manifest_path.read_bytes() == tampered


def test_manifest_envelope_must_match_contract_and_artifact_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACT1", {"safe": True})
    manifest_path = store._manifest_path("CONTRACT1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["events"][0]["envelope"]["contract_id"] = "OTHER"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="does not match"):
        store.fetch("CONTRACT1")


@pytest.mark.parametrize("operation", ["fetch", "replay", "envelope", "retention", "prune"])
def test_schema_valid_cross_contract_substitution_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    manifest_path, victim_blob, source_blob = _substitute_contract_artifact(store)
    tampered = manifest_path.read_bytes()

    with pytest.raises(ArtifactManifestError, match="binding"):
        if operation == "fetch":
            store.fetch("CONTRACTA")
        elif operation == "replay":
            store.replay("CONTRACTA")
        elif operation == "envelope":
            store.envelope_if_exists("CONTRACTA")
        elif operation == "retention":
            store.set_contract_retention(
                "CONTRACTA",
                active=False,
                retain_until=NOW + timedelta(days=1),
                now=NOW,
            )
        else:
            _age(victim_blob, days=100)
            _age(source_blob, days=100)
            store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert manifest_path.read_bytes() == tampered
    assert victim_blob.exists()
    assert source_blob.exists()


def test_missing_binding_cache_is_recovered_from_independent_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"safe": True})
    binding_path = store._binding_path("CONTRACT1")
    binding_path.unlink()

    assert store.fetch("CONTRACT1").envelope == envelope
    assert binding_path.is_file()
    assert store.prune(ttl=timedelta(0), apply=False, now=NOW).candidates == ()
    assert _blob_path(store, envelope.artifact_ref).exists()


def test_durable_binding_without_manifest_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"safe": True})
    manifest_path = store._manifest_path("CONTRACT1")
    manifest_path.unlink()

    with pytest.raises(ArtifactManifestError, match="binding"):
        store.envelope_if_exists("CONTRACT1")
    with pytest.raises(ArtifactManifestError, match="binding|authority"):
        store.prune(ttl=timedelta(0), apply=True, now=NOW)
    assert _blob_path(store, envelope.artifact_ref).exists()


def test_schema_valid_replaced_binding_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, "CONTRACTA", {"owner": "a"})
    _put(store, "CONTRACTB", {"owner": "b"})
    victim_binding = store._binding_path("CONTRACTA")
    source_binding = store._binding_path("CONTRACTB")
    replacement = json.loads(source_binding.read_text(encoding="utf-8"))
    replacement["contract_id"] = "CONTRACTA"
    victim_binding.write_text(json.dumps(replacement), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="binding"):
        store.fetch("CONTRACTA")


@pytest.mark.parametrize("operation", ["fetch", "replay", "envelope", "retention", "prune"])
def test_coordinated_manifest_and_binding_substitution_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    manifest_path, victim_blob, source_blob = _substitute_contract_artifact(
        store,
        replace_binding=True,
    )
    tampered = manifest_path.read_bytes()

    with pytest.raises(ArtifactManifestError, match="authority"):
        if operation == "fetch":
            store.fetch("CONTRACTA")
        elif operation == "replay":
            store.replay("CONTRACTA")
        elif operation == "envelope":
            store.envelope_if_exists("CONTRACTA")
        elif operation == "retention":
            store.set_contract_retention(
                "CONTRACTA",
                active=False,
                retain_until=NOW + timedelta(days=1),
                now=NOW,
            )
        else:
            _age(victim_blob, days=100)
            _age(source_blob, days=100)
            store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert manifest_path.read_bytes() == tampered
    assert victim_blob.exists()
    assert source_blob.exists()


def test_binding_then_manifest_failure_recovers_on_identical_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    original_write = store._write_manifest_locked
    failures = 0

    def fail_first_manifest(*args: Any, **kwargs: Any) -> None:
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("simulated manifest publication failure")
        original_write(*args, **kwargs)

    monkeypatch.setattr(store, "_write_manifest_locked", fail_first_manifest)
    with pytest.raises(OSError, match="manifest publication failure"):
        _put(store, "CONTRACT1", {"recoverable": True})

    assert store._anchor_path("CONTRACT1").is_file()
    assert store._binding_path("CONTRACT1").is_file()
    assert not store._manifest_path("CONTRACT1").exists()

    recovered = _put(store, "CONTRACT1", {"recoverable": True})
    assert store.fetch("CONTRACT1").envelope == recovered


def test_blob_survives_until_every_shared_contract_tombstone_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _put(
        store,
        "CONTRACT1",
        {"shared": "failure-safe"},
        retain_until=NOW - timedelta(days=1),
    )
    _put(
        store,
        "CONTRACT2",
        {"shared": "failure-safe"},
        retain_until=NOW - timedelta(days=1),
    )
    path = _blob_path(store, first.artifact_ref)
    _age(path, days=100)
    original_write = store._write_manifest_locked
    writes = 0

    def fail_second_manifest(
        contract_id: str,
        manifest: dict,
        *,
        authority_check: Any = None,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated second tombstone failure")
        original_write(
            contract_id,
            manifest,
            authority_check=authority_check,
        )

    monkeypatch.setattr(store, "_write_manifest_locked", fail_second_manifest)
    with pytest.raises(OSError, match="second tombstone"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert _manifest(store, "CONTRACT2")["events"][-1]["type"] == "artifact.referenced"
    for contract_id in ("CONTRACT1", "CONTRACT2"):
        assert store._anchor_path(contract_id).with_suffix(".tombstoned").is_file()
    monkeypatch.undo()
    for contract_id in ("CONTRACT1", "CONTRACT2"):
        with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
            store.envelope_if_exists(contract_id)
        assert _manifest(store, contract_id)["events"][-1]["type"] == "artifact.tombstoned"
    assert path.exists()


def test_shared_blob_recovers_partial_terminal_epoch_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _put(
        store,
        "CONTRACT1",
        {"shared": "epoch fanout"},
        retain_until=NOW - timedelta(days=1),
    )
    second = _put(
        store,
        "CONTRACT2",
        {"shared": "epoch fanout"},
        retain_until=NOW - timedelta(days=1),
    )
    assert first.artifact_ref == second.artifact_ref
    blob_path = _blob_path(store, first.artifact_ref)
    _age(blob_path, days=100)
    original_write = store._write_record_locked
    epoch_writes = 0

    def fail_second_epoch(
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Any,
        replace_existing: bool = False,
    ) -> None:
        nonlocal epoch_writes
        if path.suffix == ".epoch":
            epoch_writes += 1
            if epoch_writes == 2:
                raise OSError("simulated second epoch failure")
        original_write(
            path,
            payload,
            stable=stable,
            authority_check=authority_check,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(store, "_write_record_locked", fail_second_epoch)
    with pytest.raises(OSError, match="second epoch failure"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert blob_path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert _manifest(store, "CONTRACT2")["events"][-1]["type"] == "artifact.referenced"
    assert len(_lifecycle_epochs(store, "CONTRACT1")) == 1
    assert _lifecycle_epochs(store, "CONTRACT2") == []
    monkeypatch.undo()

    report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert report.removed_refs == (first.artifact_ref,)
    assert not blob_path.exists()
    for contract_id in ("CONTRACT1", "CONTRACT2"):
        with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
            store.fetch(contract_id)


@pytest.mark.parametrize("operation", ["fetch", "replay", "envelope", "retention", "prune"])
def test_committed_tombstone_survives_unlink_failure_and_manifest_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "must remain durable"},
        retain_until=NOW - timedelta(days=1),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    referenced_manifest = manifest_path.read_bytes()
    genesis_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle")
    referenced_genesis = genesis_path.read_bytes()
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)

    def fail_unlink(_candidate: object) -> None:
        raise OSError("simulated blob unlink failure")

    monkeypatch.setattr(store, "_unlink_blob_candidate_locked", fail_unlink)
    with pytest.raises(OSError, match="unlink failure"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert blob_path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    terminal_path = store._anchor_path("CONTRACT1").with_suffix(".tombstoned")
    terminal_bytes = terminal_path.read_bytes()
    terminal_path.unlink()
    genesis_path.write_bytes(referenced_genesis)
    manifest_path.write_bytes(referenced_manifest)
    monkeypatch.undo()

    if operation == "prune":
        report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
        assert report.removed_refs == (envelope.artifact_ref,)
        assert not blob_path.exists()
    else:
        with pytest.raises(ArtifactTombstonedError, match="pruned|retention"):
            if operation == "fetch":
                store.fetch("CONTRACT1")
            elif operation == "replay":
                store.replay("CONTRACT1")
            elif operation == "envelope":
                store.envelope_if_exists("CONTRACT1")
            else:
                store.set_contract_retention(
                    "CONTRACT1",
                    active=True,
                    retain_until=NOW + timedelta(days=1),
                    now=NOW,
                )
        assert blob_path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert terminal_path.read_bytes() == terminal_bytes


def test_tombstone_authority_recovers_manifest_write_failure_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "crash recoverable"},
        retain_until=NOW - timedelta(days=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    manifest_path = store._manifest_path("CONTRACT1")
    referenced_manifest = manifest_path.read_bytes()
    _age(blob_path, days=100)
    original_write = store._write_manifest_locked

    def fail_tombstone_manifest(
        contract_id: str,
        manifest: dict,
        *,
        authority_check: Any = None,
    ) -> None:
        if manifest["events"][-1]["type"] == "artifact.tombstoned":
            raise OSError("simulated tombstone manifest failure")
        original_write(contract_id, manifest, authority_check=authority_check)

    monkeypatch.setattr(store, "_write_manifest_locked", fail_tombstone_manifest)
    with pytest.raises(OSError, match="tombstone manifest failure"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    terminal_path = store._anchor_path("CONTRACT1").with_suffix(".tombstoned")
    terminal_bytes = terminal_path.read_bytes()
    assert blob_path.exists()
    assert manifest_path.read_bytes() == referenced_manifest
    monkeypatch.undo()

    with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
        store.envelope_if_exists("CONTRACT1")
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert terminal_path.read_bytes() == terminal_bytes
    assert blob_path.exists()


def test_lifecycle_head_recovers_terminal_record_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "head committed first"},
        retain_until=NOW - timedelta(days=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    manifest_path = store._manifest_path("CONTRACT1")
    referenced_manifest = manifest_path.read_bytes()
    _age(blob_path, days=100)
    original_write = store._write_record_locked

    def fail_terminal_record(
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Any,
        replace_existing: bool = False,
    ) -> None:
        if path.suffix == ".tombstoned":
            raise OSError("simulated terminal record failure")
        original_write(
            path,
            payload,
            stable=stable,
            authority_check=authority_check,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(store, "_write_record_locked", fail_terminal_record)
    with pytest.raises(OSError, match="terminal record failure"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    epochs = _lifecycle_epochs(store, "CONTRACT1")
    assert len(epochs) == 1
    assert json.loads(epochs[0].read_text(encoding="utf-8"))["kind"] == "terminal"
    assert not store._anchor_path("CONTRACT1").with_suffix(".tombstoned").exists()
    assert manifest_path.read_bytes() == referenced_manifest
    assert blob_path.exists()
    monkeypatch.undo()

    with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
        store.envelope_if_exists("CONTRACT1")
    assert store._anchor_path("CONTRACT1").with_suffix(".tombstoned").is_file()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert blob_path.exists()


def test_committed_contract_requires_non_optional_lifecycle_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"lifecycle": "required"})
    lifecycle_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle")
    lifecycle_path.unlink()

    with pytest.raises(ArtifactManifestError, match="lifecycle authority is missing"):
        store.fetch("CONTRACT1")
    assert _blob_path(store, envelope.artifact_ref).exists()


def test_committed_contract_requires_expected_lifecycle_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"expected_head": "required"})
    store._anchor_path("CONTRACT1").with_suffix(".lifecycle.head").unlink()

    with pytest.raises(ArtifactManifestError, match="lifecycle authority is missing"):
        store.fetch("CONTRACT1")
    assert _blob_path(store, envelope.artifact_ref).exists()


def test_lifecycle_head_substitution_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _put(store, "CONTRACT1", {"head": "first"})
    _put(store, "CONTRACT2", {"head": "second"})
    first_head = store._anchor_path("CONTRACT1").with_suffix(".lifecycle.head")
    second_head = store._anchor_path("CONTRACT2").with_suffix(".lifecycle.head")
    first_head.write_bytes(second_head.read_bytes())

    with pytest.raises(ArtifactManifestError, match="lifecycle authority is invalid"):
        store.fetch("CONTRACT1")
    assert _blob_path(store, first.artifact_ref).exists()


def test_lifecycle_retention_authority_repairs_schema_valid_manifest_rollback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"retention": "must remain protected"},
        active=True,
        retain_until=NOW + timedelta(days=365),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    manifest_path = store._manifest_path("CONTRACT1")
    manifest = _manifest(store, "CONTRACT1")
    manifest["active"] = False
    manifest["retain_until"] = (NOW - timedelta(days=1)).isoformat()
    manifest["updated_at"] = (NOW + timedelta(hours=2)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert report.candidates == ()
    assert blob_path.exists()
    repaired = _manifest(store, "CONTRACT1")
    assert repaired["active"] is True
    assert repaired["retain_until"] == (NOW + timedelta(days=365)).isoformat()
    assert repaired["updated_at"] == NOW.isoformat()


def test_expected_head_rejects_retention_epoch_tail_erasure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"retention": "tail must not disappear"},
        retain_until=NOW - timedelta(days=1),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    expired_manifest = manifest_path.read_bytes()
    store.set_contract_retention(
        "CONTRACT1",
        active=True,
        retain_until=NOW + timedelta(days=365),
        now=NOW + timedelta(hours=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    _lifecycle_epochs(store, "CONTRACT1")[-1].unlink()
    manifest_path.write_bytes(expired_manifest)

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert blob_path.exists()


def test_expected_head_rejects_terminal_epoch_tail_erasure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "tail must remain monotonic"},
        retain_until=NOW - timedelta(days=1),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    referenced_manifest = manifest_path.read_bytes()
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    monkeypatch.setattr(
        store,
        "_unlink_blob_candidate_locked",
        lambda _candidate: (_ for _ in ()).throw(OSError("keep body")),
    )
    with pytest.raises(OSError, match="keep body"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    monkeypatch.undo()

    _lifecycle_epochs(store, "CONTRACT1")[-1].unlink()
    store._anchor_path("CONTRACT1").with_suffix(".tombstoned").unlink()
    manifest_path.write_bytes(referenced_manifest)

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        store.replay("CONTRACT1")
    assert blob_path.exists()


def test_retention_epoch_before_head_crash_recovers_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"retention": "head crash"},
        retain_until=NOW - timedelta(days=1),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    expired_manifest = manifest_path.read_bytes()
    head_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle.head")
    original_write = store._write_record_locked

    def fail_head_update(
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Any,
        replace_existing: bool = False,
    ) -> None:
        if path == head_path and replace_existing:
            raise OSError("simulated lifecycle head failure")
        original_write(
            path,
            payload,
            stable=stable,
            authority_check=authority_check,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(store, "_write_record_locked", fail_head_update)
    with pytest.raises(OSError, match="lifecycle head failure"):
        store.set_contract_retention(
            "CONTRACT1",
            active=True,
            retain_until=NOW + timedelta(days=365),
            now=NOW + timedelta(hours=1),
        )
    assert len(_lifecycle_epochs(store, "CONTRACT1")) == 1
    assert json.loads(head_path.read_text(encoding="utf-8"))["sequence"] == 0
    assert manifest_path.read_bytes() == expired_manifest
    monkeypatch.undo()

    assert store.fetch("CONTRACT1").envelope == envelope
    assert json.loads(head_path.read_text(encoding="utf-8"))["sequence"] == 1
    repaired = _manifest(store, "CONTRACT1")
    assert repaired["active"] is True
    assert repaired["retain_until"] == (NOW + timedelta(days=365)).isoformat()


def test_terminal_epoch_before_head_crash_recovers_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "head crash"},
        retain_until=NOW - timedelta(days=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    head_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle.head")
    terminal_path = store._anchor_path("CONTRACT1").with_suffix(".tombstoned")
    original_write = store._write_record_locked

    def fail_head_update(
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Any,
        replace_existing: bool = False,
    ) -> None:
        if path == head_path and replace_existing:
            raise OSError("simulated lifecycle head failure")
        original_write(
            path,
            payload,
            stable=stable,
            authority_check=authority_check,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(store, "_write_record_locked", fail_head_update)
    with pytest.raises(OSError, match="lifecycle head failure"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert len(_lifecycle_epochs(store, "CONTRACT1")) == 1
    assert json.loads(head_path.read_text(encoding="utf-8"))["sequence"] == 0
    assert not terminal_path.exists()
    assert blob_path.exists()
    monkeypatch.undo()

    with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
        store.envelope_if_exists("CONTRACT1")
    assert json.loads(head_path.read_text(encoding="utf-8"))["sequence"] == 1
    assert terminal_path.is_file()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert blob_path.exists()


def test_multiple_uncommitted_lifecycle_successors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"retention": "committed protection"},
        active=True,
        retain_until=NOW + timedelta(days=365),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    committed_manifest = manifest_path.read_bytes()
    head_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle.head")
    committed_head = head_path.read_bytes()
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    original_write = store._write_record_locked

    def fail_head_update(
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Any,
        replace_existing: bool = False,
    ) -> None:
        if path == head_path and replace_existing:
            raise OSError("leave one uncommitted successor")
        original_write(
            path,
            payload,
            stable=stable,
            authority_check=authority_check,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(store, "_write_record_locked", fail_head_update)
    with pytest.raises(OSError, match="uncommitted successor"):
        store.set_contract_retention(
            "CONTRACT1",
            active=True,
            retain_until=NOW + timedelta(days=365),
            now=NOW + timedelta(hours=1),
        )
    monkeypatch.undo()

    authority = store._read_authority_locked("CONTRACT1")
    assert authority is not None
    first_epoch = json.loads(_lifecycle_epochs(store, "CONTRACT1")[0].read_text())
    first_digest = hashlib.sha256(encode_record(first_epoch)).hexdigest()
    first_state = LifecycleState(
        active=first_epoch["active"],
        retain_until=first_epoch["retain_until"],
        updated_at=first_epoch["timestamp"],
        terminal=None,
        sequence=1,
        head_sha256=first_digest,
    )
    second_epoch = lifecycle_epoch_record(
        authority,
        first_state,
        timestamp=(NOW + timedelta(hours=2)).isoformat(),
        active=False,
        retain_until=(NOW - timedelta(days=1)).isoformat(),
    )
    second_payload = encode_record(second_epoch)
    second_digest = hashlib.sha256(second_payload).hexdigest()
    lifecycle_epoch_path(
        store._anchor_path("CONTRACT1"),
        sequence=2,
        digest=second_digest,
    ).write_bytes(second_payload)

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert manifest_path.read_bytes() == committed_manifest
    assert head_path.read_bytes() == committed_head
    assert blob_path.exists()


def test_retention_epoch_recovers_manifest_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"retention": "epoch first"},
        retain_until=NOW - timedelta(days=1),
    )
    manifest_path = store._manifest_path("CONTRACT1")
    previous_manifest = manifest_path.read_bytes()
    original_write = store._write_manifest_locked

    def fail_retention_manifest(
        contract_id: str,
        manifest: dict,
        *,
        authority_check: Any = None,
    ) -> None:
        if manifest["active"]:
            raise OSError("simulated retention manifest failure")
        original_write(contract_id, manifest, authority_check=authority_check)

    monkeypatch.setattr(store, "_write_manifest_locked", fail_retention_manifest)
    with pytest.raises(OSError, match="retention manifest failure"):
        store.set_contract_retention(
            "CONTRACT1",
            active=True,
            retain_until=NOW + timedelta(days=365),
            now=NOW + timedelta(hours=1),
        )

    epochs = _lifecycle_epochs(store, "CONTRACT1")
    assert len(epochs) == 1
    assert json.loads(epochs[0].read_text(encoding="utf-8"))["kind"] == "retention"
    assert manifest_path.read_bytes() == previous_manifest
    monkeypatch.undo()

    assert store.fetch("CONTRACT1").envelope == envelope
    repaired = _manifest(store, "CONTRACT1")
    assert repaired["active"] is True
    assert repaired["retain_until"] == (NOW + timedelta(days=365)).isoformat()


@pytest.mark.parametrize("operation", ["fetch", "replay", "envelope", "retention", "prune"])
def test_tombstone_event_substitution_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "substitution protected"},
        retain_until=NOW - timedelta(days=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    monkeypatch.setattr(
        store,
        "_unlink_blob_candidate_locked",
        lambda _candidate: (_ for _ in ()).throw(OSError("keep body")),
    )
    with pytest.raises(OSError, match="keep body"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    monkeypatch.undo()

    manifest_path = store._manifest_path("CONTRACT1")
    manifest = _manifest(store, "CONTRACT1")
    terminal_path = store._anchor_path("CONTRACT1").with_suffix(".tombstoned")
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal["timestamp"] = (NOW + timedelta(hours=1)).isoformat()
    terminal["reason"] = "attacker substituted terminal metadata"
    terminal_path.write_text(json.dumps(terminal, sort_keys=True) + "\n", encoding="utf-8")
    manifest["events"][-1]["timestamp"] = terminal["timestamp"]
    manifest["events"][-1]["reason"] = terminal["reason"]
    manifest["updated_at"] = terminal["timestamp"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = manifest_path.read_bytes()
    terminal_bytes = terminal_path.read_bytes()
    lifecycle_path = store._anchor_path("CONTRACT1").with_suffix(".lifecycle")
    lifecycle_bytes = lifecycle_path.read_bytes()

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        if operation == "fetch":
            store.fetch("CONTRACT1")
        elif operation == "replay":
            store.replay("CONTRACT1")
        elif operation == "envelope":
            store.envelope_if_exists("CONTRACT1")
        elif operation == "retention":
            store.set_contract_retention(
                "CONTRACT1",
                active=True,
                retain_until=NOW + timedelta(days=1),
                now=NOW,
            )
        else:
            store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert manifest_path.read_bytes() == tampered
    assert terminal_path.read_bytes() == terminal_bytes
    assert lifecycle_path.read_bytes() == lifecycle_bytes
    assert blob_path.exists()


def test_coordinated_terminal_epoch_projection_substitution_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"terminal": "content addressed"},
        retain_until=NOW - timedelta(days=1),
    )
    blob_path = _blob_path(store, envelope.artifact_ref)
    _age(blob_path, days=100)
    monkeypatch.setattr(
        store,
        "_unlink_blob_candidate_locked",
        lambda _candidate: (_ for _ in ()).throw(OSError("keep body")),
    )
    with pytest.raises(OSError, match="keep body"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    monkeypatch.undo()

    manifest_path = store._manifest_path("CONTRACT1")
    terminal_path = store._anchor_path("CONTRACT1").with_suffix(".tombstoned")
    epoch_path = _lifecycle_epochs(store, "CONTRACT1")[0]
    epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
    substituted_at = (NOW + timedelta(hours=1)).isoformat()
    epoch["timestamp"] = substituted_at
    epoch["terminal"]["timestamp"] = substituted_at
    epoch["terminal"]["reason"] = "coordinated substitution"
    epoch_path.write_text(json.dumps(epoch, sort_keys=True) + "\n", encoding="utf-8")
    terminal_path.write_text(
        json.dumps(epoch["terminal"], sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = _manifest(store, "CONTRACT1")
    manifest["events"][-1]["timestamp"] = substituted_at
    manifest["events"][-1]["reason"] = "coordinated substitution"
    manifest["updated_at"] = substituted_at
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        store.fetch("CONTRACT1")
    assert blob_path.exists()


def test_lifecycle_epoch_fork_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"lifecycle": "no forks"})
    store.set_contract_retention(
        "CONTRACT1",
        active=True,
        retain_until=NOW + timedelta(days=365),
        now=NOW + timedelta(hours=1),
    )
    epoch_path = _lifecycle_epochs(store, "CONTRACT1")[0]
    epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
    epoch["timestamp"] = (NOW + timedelta(hours=2)).isoformat()
    payload = (json.dumps(epoch, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    fork_path = epoch_path.with_name(
        f"{lifecycle_epoch_prefix(store._anchor_path('CONTRACT1'))}."
        f"{epoch['sequence']:020d}.{digest}.epoch"
    )
    fork_path.write_bytes(payload)

    with pytest.raises(ArtifactManifestError, match="lifecycle authority"):
        store.fetch("CONTRACT1")
    assert _blob_path(store, envelope.artifact_ref).exists()


@pytest.mark.parametrize("junction_component", ["digest_prefix", "body"])
def test_prune_revalidates_modeled_windows_junction_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junction_component: str,
) -> None:
    """A post-plan junction replacement can never delete an external body."""
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"external": "must survive"},
        retain_until=NOW - timedelta(days=1),
    )
    path = _blob_path(store, envelope.artifact_ref)
    prefix = path.parent
    _age(path, days=100)

    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_body = external_dir / path.name
    external_body.write_bytes(path.read_bytes())
    _age(external_body, days=100)

    original_plan = store._plan_prune_locked
    junction_path = prefix if junction_component == "digest_prefix" else path
    junction_active = False

    def replace_candidate_with_junction(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal junction_active
        candidates = original_plan(*args, **kwargs)
        path.unlink()
        if junction_component == "digest_prefix":
            prefix.rmdir()
            prefix.symlink_to(external_dir, target_is_directory=True)
        else:
            path.symlink_to(external_body)
        junction_active = True
        return candidates

    real_is_symlink = Path.is_symlink
    real_is_junction = getattr(Path, "is_junction", lambda _path: False)

    def modeled_is_symlink(candidate: Path) -> bool:
        if junction_active and candidate == junction_path:
            return False
        return real_is_symlink(candidate)

    def modeled_is_junction(candidate: Path) -> bool:
        if junction_active and candidate == junction_path:
            return True
        return real_is_junction(candidate)

    monkeypatch.setattr(store, "_plan_prune_locked", replace_candidate_with_junction)
    monkeypatch.setattr(Path, "is_symlink", modeled_is_symlink)
    monkeypatch.setattr(Path, "is_junction", modeled_is_junction, raising=False)

    with pytest.raises(ArtifactIntegrityError, match="junction"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert external_body.exists()
    assert external_body.read_bytes() == canonical_artifact_bytes({"external": "must survive"})


def test_prune_final_unlink_never_follows_digest_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation inside final unlink cannot delete a same-named external body."""
    if not artifact_store_module._supports_directory_fd_unlink():
        pytest.skip("directory-relative unlink is unavailable on this platform")

    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"external": "must survive final unlink"},
        retain_until=NOW - timedelta(days=1),
    )
    path = _blob_path(store, envelope.artifact_ref)
    prefix = path.parent
    _age(path, days=100)

    external = tmp_path / "unlink-external"
    displaced = tmp_path / "unlink-displaced"
    external.mkdir()
    external_body = external / path.name
    external_body.write_bytes(path.read_bytes())
    original_unlink = os.unlink
    original_rename = os.rename
    swapped = False

    def swap_parent_during_unlink(
        target: object,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if dir_fd is not None and target == path.name and not swapped:
            original_rename(prefix, displaced)
            try:
                prefix.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        original_unlink(target, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", swap_parent_during_unlink)

    report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert report.removed_refs == (envelope.artifact_ref,)
    assert swapped
    assert external_body.read_bytes() == canonical_artifact_bytes(
        {"external": "must survive final unlink"}
    )
    assert list(displaced.iterdir()) == []


def test_prune_rejects_same_size_body_replacement_after_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"planned": "identity"},
        retain_until=NOW - timedelta(days=1),
    )
    path = _blob_path(store, envelope.artifact_ref)
    _age(path, days=100)
    original_plan = store._plan_prune_locked
    replacement = b"x" * path.stat().st_size

    def replace_body_after_plan(*args, **kwargs):  # noqa: ANN002, ANN003
        candidates = original_plan(*args, **kwargs)
        with path.open("rb") as original:
            path.unlink()
            path.write_bytes(replacement)
            assert path.stat().st_ino != os.fstat(original.fileno()).st_ino
        return candidates

    monkeypatch.setattr(store, "_plan_prune_locked", replace_body_after_plan)

    with pytest.raises(ArtifactIntegrityError, match="changed after prune planning"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert path.read_bytes() == replacement


@pytest.mark.parametrize("publication", ["body", "manifest"])
def test_directory_creation_never_follows_project_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: str,
) -> None:
    """Mutation inside mkdirat cannot publish a body or manifest externally."""
    if not artifact_store_module._supports_directory_fd_publication():
        pytest.skip("directory-relative creation is unavailable on this platform")

    project = tmp_path / "project"
    project.mkdir()
    store = ContentAddressedArtifactStore.for_project(project)
    store.initialize()
    body = {"ancestor": "must remain project-owned"}
    digest = hashlib.sha256(canonical_artifact_bytes(body)).hexdigest()
    if publication == "manifest":
        _put(store, "CONTRACT1", body)
        creation_component = hashlib.sha256(b"CONTRACT2").hexdigest()
    else:
        creation_component = digest[:2]

    ouroboros_dir = project / ".ouroboros"
    displaced = tmp_path / f"ancestor-displaced-{publication}"
    external = tmp_path / f"ancestor-external-{publication}"
    external.mkdir()
    original_mkdir = os.mkdir
    original_rename = os.rename
    swapped = False

    def swap_ancestor_during_mkdir(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if dir_fd is not None and path == creation_component and not swapped:
            original_rename(ouroboros_dir, displaced)
            try:
                ouroboros_dir.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_ancestor_during_mkdir)

    with pytest.raises(ArtifactIntegrityError, match="ancestor changed"):
        _put(store, "CONTRACT2" if publication == "manifest" else "CONTRACT1", body)

    assert swapped
    assert list(external.iterdir()) == []
    if publication == "body":
        rejected_path = displaced / "artifacts" / digest[:2] / f"{digest}.json"
    else:
        rejected_path = displaced / "artifacts" / "contracts" / creation_component / "events.json"
    assert not rejected_path.exists()


@pytest.mark.parametrize("publication", ["body", "manifest"])
def test_publication_rejects_parent_swap_before_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: str,
) -> None:
    """The final writer never follows a post-validation directory symlink."""

    store = _store(tmp_path)
    body = {"external": "must remain empty"}
    digest = hashlib.sha256(canonical_artifact_bytes(body)).hexdigest()
    if publication == "manifest":
        _put(store, "CONTRACT1", body)
        publication_parent = _contract_root(store, "CONTRACT2")
    else:
        publication_parent = store.root / digest[:2]

    external = tmp_path / f"external-{publication}"
    external.mkdir()
    original_write = artifact_store_module._atomic_write_bytes
    swapped = False
    junction_active = False
    real_is_symlink = Path.is_symlink
    real_is_junction = getattr(Path, "is_junction", lambda _path: False)

    def modeled_is_symlink(candidate: Path) -> bool:
        if junction_active and candidate == publication_parent:
            return False
        return real_is_symlink(candidate)

    def modeled_is_junction(candidate: Path) -> bool:
        if junction_active and candidate == publication_parent:
            return True
        return real_is_junction(candidate)

    monkeypatch.setattr(Path, "is_symlink", modeled_is_symlink)
    monkeypatch.setattr(Path, "is_junction", modeled_is_junction, raising=False)

    def swap_parent_then_write(path: Path, payload: bytes, **kwargs: object) -> None:
        nonlocal junction_active, swapped
        target = path.parent == publication_parent
        if target and not swapped:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.rmdir()
            try:
                path.parent.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
            junction_active = True
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(artifact_store_module, "_atomic_write_bytes", swap_parent_then_write)

    with pytest.raises(ArtifactIntegrityError, match="link|symlink|publication"):
        _put(store, "CONTRACT2", body)

    assert swapped
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("publication", ["body", "manifest"])
def test_publication_revalidates_parent_handle_at_replace_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: str,
) -> None:
    """A directory moved after temp fsync cannot retain an external publication."""

    if not artifact_store_module._supports_directory_fd_publication():
        pytest.skip("directory-relative publication is unavailable on this platform")
    store = _store(tmp_path)
    body = {"replace_boundary": "must remain empty"}
    digest = hashlib.sha256(canonical_artifact_bytes(body)).hexdigest()
    if publication == "manifest":
        _put(store, "CONTRACT1", body)
        target_parent = _contract_root(store, "CONTRACT2")
    else:
        target_parent = store.root / digest[:2]

    external = tmp_path / f"replace-external-{publication}"
    displaced = tmp_path / f"replace-displaced-{publication}"
    external.mkdir()
    original_rename = os.rename
    swapped = False

    def swap_parent_then_replace(src: object, dst: object, **kwargs: object) -> None:
        nonlocal swapped
        if kwargs.get("src_dir_fd") is not None and not swapped:
            original_rename(target_parent, displaced)
            try:
                target_parent.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        original_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", swap_parent_then_replace)

    with pytest.raises(ArtifactIntegrityError, match="link|junction|changed|escapes"):
        _put(store, "CONTRACT2", body)

    assert swapped
    assert list(external.iterdir()) == []
    assert list(displaced.iterdir()) == []


def test_prune_manifest_update_restores_previous_file_after_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected tombstone update keeps the last durable reachability state."""

    if not artifact_store_module._supports_directory_fd_publication():
        pytest.skip("directory-relative publication is unavailable on this platform")
    if os.link not in os.supports_dir_fd or os.link not in os.supports_follow_symlinks:
        pytest.skip("directory-relative no-follow backups are unavailable on this platform")

    store = _store(tmp_path)
    envelope = _put(
        store,
        "CONTRACT1",
        {"existing_manifest": "must survive"},
        retain_until=NOW - timedelta(days=1),
    )
    body_path = _blob_path(store, envelope.artifact_ref)
    _age(body_path, days=100)
    manifest_parent = _contract_root(store, "CONTRACT1")
    manifest_path = manifest_parent / "events.json"
    previous_manifest = manifest_path.read_bytes()

    external = tmp_path / "manifest-update-external"
    displaced = tmp_path / "manifest-update-displaced"
    external.mkdir()
    original_rename = os.rename
    swapped = False

    def swap_parent_during_manifest_replace(
        src: object,
        dst: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if dst == "events.json" and kwargs.get("src_dir_fd") is not None and not swapped:
            original_rename(manifest_parent, displaced)
            try:
                manifest_parent.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        original_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", swap_parent_during_manifest_replace)

    with pytest.raises(ArtifactIntegrityError, match="link|junction|changed"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert swapped
    assert list(external.iterdir()) == []
    assert body_path.exists()
    restored_manifest = displaced / "events.json"
    assert restored_manifest.read_bytes() == previous_manifest
    assert list(displaced.iterdir()) == [restored_manifest]


def test_path_unsafe_contract_id_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="path-safe"):
        _put(store, "../escape", {"unsafe": True})


@pytest.mark.parametrize(
    "contract_id",
    [f"fanout:{'a' * 64}", "CON", "trailing."],
)
def test_contract_id_uses_portable_hashed_filesystem_component(
    tmp_path: Path,
    contract_id: str,
) -> None:
    store = _store(tmp_path)
    _put(store, contract_id, {"portable": contract_id})

    component = store._manifest_path(contract_id).parent.name
    assert component == hashlib.sha256(contract_id.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", component)
    assert contract_id not in str(store._manifest_path(contract_id))
    assert store._binding_path(contract_id).stem == component
    assert contract_id not in store._anchor_path(contract_id).name
    assert store._contract_execution_lock_target(contract_id).parent.name == component
    with store.contract_execution_lock(contract_id):
        pass
    assert store.fetch(contract_id).body == {"portable": contract_id}
    assert store.replay(contract_id).body == {"portable": contract_id}
    assert store.prune(now=NOW).candidates == ()


def test_case_distinct_contract_ids_do_not_alias_on_case_insensitive_paths(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    upper = _put(store, "CaseSensitive", {"owner": "upper"})
    lower = _put(store, "casesensitive", {"owner": "lower"})

    upper_component = store._manifest_path("CaseSensitive").parent.name
    lower_component = store._manifest_path("casesensitive").parent.name
    assert upper_component.casefold() != lower_component.casefold()
    assert upper.artifact_ref != lower.artifact_ref
    assert store.fetch("CaseSensitive").body == {"owner": "upper"}
    assert store.fetch("casesensitive").body == {"owner": "lower"}
    assert store.replay("CaseSensitive").body == {"owner": "upper"}
    assert store.replay("casesensitive").body == {"owner": "lower"}
    assert store.prune(now=NOW).candidates == ()


@pytest.mark.parametrize("operation", ["fetch", "replay", "prune"])
def test_legacy_raw_contract_directory_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    contract_id = "LEGACYCONTRACT"
    envelope = _put(store, contract_id, {"legacy": "must not alias"})
    portable_root = _contract_root(store, contract_id)
    raw_root = store.root / "contracts" / contract_id
    portable_root.rename(raw_root)

    with pytest.raises(ArtifactManifestError, match="authority|binding|path"):
        if operation == "fetch":
            store.fetch(contract_id)
        elif operation == "replay":
            store.replay(contract_id)
        else:
            store.prune(now=NOW)
    assert _blob_path(store, envelope.artifact_ref).exists()


@pytest.mark.parametrize("linked_component", ["artifact_root", "contracts"])
def test_project_store_rejects_external_directory_symlink(
    tmp_path: Path,
    linked_component: str,
) -> None:
    project = tmp_path / "project"
    artifact_root = project / ".ouroboros" / "artifacts"
    external = tmp_path / f"external-{linked_component}"
    external.mkdir()
    artifact_root.parent.mkdir(parents=True)
    if linked_component == "artifact_root":
        link = artifact_root
    else:
        artifact_root.mkdir()
        link = artifact_root / "contracts"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    with pytest.raises(ArtifactIntegrityError, match="project|symlink"):
        store = ContentAddressedArtifactStore.for_project(project)
        _put(store, "CONTRACT1", {"must_stay_local": True})

    assert list(external.iterdir()) == []
