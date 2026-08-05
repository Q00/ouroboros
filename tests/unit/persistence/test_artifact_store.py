"""Disposable Memory CAS, replay, retention, and tombstone contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.disposable_memory import MAX_DISPOSABLE_ARTIFACT_BYTES
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
    )


def _blob_path(store: ContentAddressedArtifactStore, artifact_ref: str) -> Path:
    digest = artifact_ref.removeprefix("sha256:")
    return store.root / digest[:2] / f"{digest}.json"


def _age(path: Path, *, days: int) -> None:
    timestamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _manifest(store: ContentAddressedArtifactStore, contract_id: str) -> dict:
    path = store.root / "contracts" / contract_id / "events.json"
    return json.loads(path.read_text(encoding="utf-8"))


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

    def track_write(digest: str, payload: bytes) -> None:
        sequence.append("write")
        original_write(digest, payload)

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
    envelope = _put(store, "CONTRACT1", {"orphan": True})
    path = _blob_path(store, envelope.artifact_ref)
    manifest_path = store.root / "contracts" / "CONTRACT1" / "events.json"
    manifest_path.unlink()
    _age(path, days=100)

    report = store.prune(ttl=timedelta(days=90), apply=True, now=NOW)
    assert report.candidates[0].contract_ids == ()
    assert not path.exists()


def test_malformed_manifest_aborts_prune_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"safe": True})
    path = _blob_path(store, envelope.artifact_ref)
    _age(path, days=100)
    manifest_path = store.root / "contracts" / "CONTRACT1" / "events.json"
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
    manifest_path = store.root / "contracts" / "CONTRACT1" / "events.json"
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
    contract_dir = store.root / "contracts" / "CONTRACT1"
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
    manifest_path = store.root / "contracts" / "CONTRACT1" / "events.json"
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
    manifest_path = store.root / "contracts" / "CONTRACT1" / "events.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["events"][0]["envelope"]["contract_id"] = "OTHER"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="does not match"):
        store.fetch("CONTRACT1")


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

    def fail_second_manifest(contract_id: str, manifest: dict) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated second tombstone failure")
        original_write(contract_id, manifest)

    monkeypatch.setattr(store, "_write_manifest_locked", fail_second_manifest)
    with pytest.raises(OSError, match="second tombstone"):
        store.prune(ttl=timedelta(days=90), apply=True, now=NOW)

    assert path.exists()
    assert _manifest(store, "CONTRACT1")["events"][-1]["type"] == "artifact.tombstoned"
    assert _manifest(store, "CONTRACT2")["events"][-1]["type"] == "artifact.referenced"


@pytest.mark.parametrize("junction_component", ["digest_prefix", "body"])
def test_prune_revalidates_modeled_windows_junction_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junction_component: str,
) -> None:
    """A post-plan junction replacement can never delete an external body."""
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"external": "must survive"})
    path = _blob_path(store, envelope.artifact_ref)
    prefix = path.parent
    (store.root / "contracts" / "CONTRACT1" / "events.json").unlink()
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
    envelope = _put(store, "CONTRACT1", {"external": "must survive final unlink"})
    path = _blob_path(store, envelope.artifact_ref)
    prefix = path.parent
    (store.root / "contracts" / "CONTRACT1" / "events.json").unlink()
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
    envelope = _put(store, "CONTRACT1", {"planned": "identity"})
    path = _blob_path(store, envelope.artifact_ref)
    (store.root / "contracts" / "CONTRACT1" / "events.json").unlink()
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
        creation_component = "CONTRACT2"
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
        rejected_path = displaced / "artifacts" / "contracts" / "CONTRACT2" / "events.json"
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
        publication_parent = store.root / "contracts" / "CONTRACT2"
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
        target_parent = store.root / "contracts" / "CONTRACT2"
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

    with pytest.raises(ArtifactIntegrityError, match="link|junction|changed"):
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
    manifest_parent = store.root / "contracts" / "CONTRACT1"
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
