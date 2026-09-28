"""The package identity is an opaque id; nothing persisted confirms a held-out guess.

A probe that rebuilds the package around guessed held-out expected values and
compares its unkeyed SHA-256 (or the oracle data file's) with anything
persisted recovers the held-out values whenever such a digest is persisted.
So the journal, the stored record and every receipt cite the sealed package
by an opaque random id and its Seed digest, and the stored record reduces a
held-out case to its id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest

from ouroboros.boundary.events import BindingsPayload, package_frozen_event, superseded_event
from ouroboros.boundary.ledger import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    verify_boundary_order,
)
from ouroboros.boundary.oracle import ORACLE_DATA_PATH, OracleSpec, oracle_data_text
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    CheckRole,
    PackageFile,
    package_record,
    package_record_bytes,
    seal_package,
    sha256_bytes,
    write_package_record,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.persistence.event_store import EventStore

from .journal_fixtures import admission_receipt

HELD = {"c2": -2, "c3": 7}  # the held-out cases, by the product's ids
DOMAIN = range(-10, 11)  # 21 x 21 = 441 joint guesses


def _seed() -> Seed:
    return Seed(
        goal="clamp helper",
        acceptance_criteria=("clamp(15, 0, 10) returns 10",),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(
            seed_id="seed_identity",
            ambiguity_score=0.1,
            created_at=datetime(2026, 9, 25, tzinfo=UTC),
        ),
    )


def held_out_package(seed: Seed | None = None, base: Path | None = None) -> CheckPackage:
    """One reproduction oracle: a case stated in the Seed and two held-out cases."""
    seed = seed or _seed()
    spec = build_oracle_spec(
        seed,
        criterion_index=0,
        check_id="oracle_1",
        call_kind="function",
        params=("value", "low", "high"),
        default_binding={"symbol": "mathutils.clamp"},
        cases=(
            {
                "case_id": "c1",
                "held_out": False,
                "args": {"value": 15, "low": 0, "high": 10},
                "expect": {"kind": "returns", "value": 10},
            },
            {
                "case_id": "c2",
                "held_out": True,
                "args": {"value": -3, "low": -2, "high": 4},
                "expect": {"kind": "returns", "value": HELD["c2"]},
            },
            {
                "case_id": "c3",
                "held_out": True,
                "args": {"value": 9, "low": 1, "high": 7},
                "expect": {"kind": "returns", "value": HELD["c3"]},
            },
        ),
    )
    return assemble_package(
        seed,
        input_digest="1" * 64,
        generator="test",
        oracles=((spec, CheckRole.REPRODUCTION),),
    )


def _guess(package: CheckPackage, values: dict[str, int]) -> CheckPackage:
    """The package an attacker rebuilds around guessed held-out values."""
    oracles: list[OracleSpec] = []
    for spec in package.oracles:
        cases = tuple(
            case.model_copy(
                update={"expect": case.expect.model_copy(update={"value": values[case.case_id]})}
            )
            if case.case_id in values
            else case
            for case in spec.cases
        )
        oracles.append(spec.model_copy(update={"cases": cases}))
    files = tuple(
        PackageFile.from_content(item.path, oracle_data_text(oracles))
        if item.path == ORACLE_DATA_PATH
        else item
        for item in package.files
    )
    return package.model_copy(update={"oracles": tuple(oracles), "files": files})


def guess_probe(package: CheckPackage, haystack: bytes) -> list[tuple[int, int]]:
    """Every joint guess whose unkeyed package or oracle-data digest appears in ``haystack``.

    A 16-hex-character prefix counts too: 64 bits confirm a guess as well.
    """
    tokens = set(re.findall(rb"[0-9a-f]{16,}", haystack))
    prefixes = {token[:16] for token in tokens}
    hits = []
    for first, second in product(DOMAIN, DOMAIN):
        guessed = _guess(package, {"c2": first, "c3": second})
        data_file = next(item for item in guessed.files if item.path == ORACLE_DATA_PATH)
        for digest in (guessed.sha256, data_file.sha256):
            encoded = digest.encode()
            if encoded in tokens or encoded[:16] in prefixes:
                hits.append((first, second))
    return hits


def test_sealing_gives_a_fresh_opaque_id() -> None:
    package = held_out_package()
    assert not package.sealed
    with pytest.raises(CheckPackageError):
        _ = package.package_id
    first, second = seal_package(package), seal_package(package)
    assert re.fullmatch(r"[0-9a-f]{64}", first.package_id)
    assert first.package_id != second.package_id
    assert first.package_id != package.sha256
    # The id is not part of the package's content.
    assert first.sha256 == second.sha256 == package.sha256


def test_the_probe_recovers_held_out_values_from_an_unkeyed_digest() -> None:
    # Negative control: the probe works against a persisted unkeyed digest.
    package = held_out_package()
    assert _guess(package, HELD).sha256 == package.sha256
    leaked = json.dumps({"package_sha256": package.sha256}).encode()
    assert guess_probe(package, leaked) == [(HELD["c2"], HELD["c3"])]


def test_the_frozen_event_and_the_record_confirm_no_guess(tmp_path: Path) -> None:
    package = seal_package(held_out_package())
    event = package_frozen_event("b", package).data
    assert event["package_id"] == package.package_id
    assert event["seed_digest"] == package.seed_digest
    path = write_package_record(package, tmp_path / "packages")
    # The journal's record digest is the digest of the stored, redacted bytes;
    # no caller can put the full package's digest in its place.
    assert event["record_sha256"] == sha256_bytes(path.read_bytes())
    assert event["record_sha256"] != package.sha256
    assert path.name == f"{package.package_id}.json"
    record = json.loads(path.read_bytes())
    assert record == package_record(package)
    assert (record["package_id"], record["seed_digest"]) == (
        package.package_id,
        package.seed_digest,
    )
    haystack = json.dumps(event).encode() + path.read_bytes()
    assert guess_probe(package, haystack) == []
    assert package.sha256.encode()[:16] not in haystack
    # The record keeps only product-computed counts; no case, visible or held
    # out, is stored.
    oracle = record["package"]["oracles"][0]
    assert "cases" not in oracle
    assert (oracle["case_count"], oracle["held_out_count"]) == (3, 2)


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def test_the_ledger_freezes_only_a_sealed_package(store: EventStore) -> None:
    ledger = BoundaryLedger(store)
    seed = _seed()
    package = held_out_package(seed)
    with pytest.raises(CheckPackageError):
        await ledger.record_package_frozen("b1", package, seed=seed)
    sealed = seal_package(package)
    await ledger.record_package_frozen("b1", sealed, seed=seed)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_bindings(
            "b1", package_id=package.sha256, payload=BindingsPayload(phase="final", checks=())
        )


def test_any_unkeyed_package_digest_in_an_event_is_a_violation() -> None:
    package = seal_package(held_out_package())
    frozen = package_frozen_event("b1", package)

    def superseded(successor: str | None) -> Any:
        return superseded_event(
            "b1",
            superseded_by="b2",
            package_id=package.package_id,
            successor_package_id=successor,
            reason="package_rejected",
        )

    flag = "a boundary event records an unkeyed package digest"
    assert flag not in verify_boundary_order([frozen, superseded(None)])
    assert flag not in verify_boundary_order([frozen, superseded("c" * 64)])
    leaked = superseded(None)
    leaked.data["successor_package_sha256"] = package.sha256
    assert flag in verify_boundary_order([frozen, leaked])


def test_a_changed_copy_of_a_sealed_package_cannot_be_cited(tmp_path: Path) -> None:
    # The round-2 probe: a model_copy with one changed field kept the id,
    # while the full-package digest and the record bytes changed.
    sealed = seal_package(held_out_package())
    changed = sealed.model_copy(update={"input_digest": "2" * 64})
    assert changed.sha256 != sealed.sha256
    for cite in (
        lambda: changed.package_id,
        lambda: package_frozen_event("b", changed),
        lambda: package_record_bytes(changed),
        lambda: write_package_record(changed, tmp_path / "packages"),
        changed.manifest_summary,
    ):
        with pytest.raises(CheckPackageError, match="changed after it was sealed"):
            cite()
    assert not (tmp_path / "packages").exists() or not any((tmp_path / "packages").iterdir())
    # An unchanged copy is the same package and keeps its id.
    assert sealed.model_copy().package_id == sealed.package_id


def test_an_in_place_edit_of_a_sealed_package_is_refused() -> None:
    sealed = seal_package(held_out_package())
    sealed.oracles[0].cases[1].args["value"] = 100  # nested dicts are mutable
    with pytest.raises(CheckPackageError, match="changed after it was sealed"):
        _ = sealed.package_id


def test_sealing_does_not_share_state_with_the_unsealed_package() -> None:
    package = held_out_package()
    sealed = seal_package(package)
    package.oracles[0].cases[1].args["value"] = 100
    assert sealed.package_id  # the sealed copy is unaffected


async def test_a_renamed_copy_of_the_oracle_data_blocks_the_worker_start(
    store: EventStore, tmp_path: Path
) -> None:
    # The round-2 probe: the journal manifest lists the oracle data file by
    # path only, so a renamed exact copy of it passed the pre-start scan.
    seed = _seed()
    package = seal_package(held_out_package(seed))
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("b1", package, seed=seed)
    await ledger.record_admission("b1", admission_receipt(package, tmp_path))
    worker = tmp_path / "worker"
    worker.mkdir()
    data = next(item for item in package.files if item.path == ORACLE_DATA_PATH)
    (worker / "renamed.json").write_text(data.content, encoding="utf-8")
    with pytest.raises(BoundaryLeakError) as caught:
        await ledger.record_actor_started("exec", ["b1"], workspace=worker, packages=[package])
    assert caught.value.details["paths"] == ["renamed.json"]
    # Without the live sealed package the scan cannot run, so the start is refused.
    (worker / "renamed.json").unlink()
    with pytest.raises(BoundaryOrderError, match="sealed package"):
        await ledger.record_actor_started("exec", ["b1"], workspace=worker)
    # Another package cannot stand in for the sealed one.
    other = seal_package(held_out_package(seed))
    with pytest.raises(BoundaryOrderError, match="sealed package"):
        await ledger.record_actor_started("exec", ["b1"], workspace=worker, packages=[other])
    await ledger.record_actor_started("exec", ["b1"], workspace=worker, packages=[package])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
async def test_a_link_to_the_oracle_data_blocks_the_worker_start(
    store: EventStore, tmp_path: Path
) -> None:
    # The round-3 probe: the scan skipped links, so a renamed link to a copy
    # of the oracle data file (readable through the workspace) passed.
    seed = _seed()
    package = seal_package(held_out_package(seed))
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("b1", package, seed=seed)
    await ledger.record_admission("b1", admission_receipt(package, tmp_path))
    worker = tmp_path / "worker"
    worker.mkdir()
    outside = tmp_path / "elsewhere.json"
    data = next(item for item in package.files if item.path == ORACLE_DATA_PATH)
    outside.write_text(data.content, encoding="utf-8")
    (worker / "notes.txt").symlink_to(outside)
    with pytest.raises(BoundaryLeakError) as caught:
        await ledger.record_actor_started("exec", ["b1"], workspace=worker, packages=[package])
    assert caught.value.details["paths"] == ["notes.txt"]
