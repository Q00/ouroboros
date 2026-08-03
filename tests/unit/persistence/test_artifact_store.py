"""Disposable Memory CAS, replay, retention, and tombstone contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from ouroboros.core.disposable_memory import MAX_DISPOSABLE_ARTIFACT_BYTES
from ouroboros.persistence.artifact_store import (
    ArtifactContractConflictError,
    ArtifactIntegrityError,
    ArtifactManifestError,
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


def test_fetch_is_explicit_and_verifies_content_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _put(store, "CONTRACT1", {"large": "payload"})

    fetched = store.fetch("CONTRACT1")
    assert fetched.envelope == envelope
    assert fetched.body == {"large": "payload"}

    _blob_path(store, envelope.artifact_ref).write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="hash does not match"):
        store.fetch("CONTRACT1")


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
