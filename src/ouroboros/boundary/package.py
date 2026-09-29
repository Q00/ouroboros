"""Separately hashed check package linked to an immutable Seed.

A check package holds the executable bindings generated for a Seed's
acceptance criteria. It is stored apart from the Seed, so a check can be
audited or regenerated without changing requirement identity. Once sealed
(``seal_package``) it has an opaque id, and receipts and journal events cite
that id and the Seed digest. The Seed is referenced only by its digest and
criterion keys; the package never mutates or embeds the Seed.

The package records, for every check: command argv, working directory, the
reproduction or preservation role, assertion-to-criterion links, and the
failure signature a reproduction check must print when it reaches its intended
failing assertion. Generated fixture and test files are carried with their
content and SHA-256. Public base files that a check relies on are pinned by
SHA-256. Criteria without a check are listed as uncovered obligations, so every
criterion key is either linked or explicitly uncovered.

Only criterion descriptions are meant for the worker. ``worker_criteria``
builds that view, and ``find_workspace_leaks`` / ``find_text_leaks`` detect
generated check code in a worker workspace or bundle.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from ouroboros.boundary.binding import BINDING_GRAMMAR
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    ORACLE_HARNESS_PATH,
    ORACLE_HARNESS_SOURCE,
    OracleSpec,
    case_id_for,
    oracle_check_id,
    oracle_data_text,
)
from ouroboros.core.filesystem_capability import (
    HeldPathChanged,
    NoFollowDirectoryChain,
    open_nofollow_directory_chain,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed, derive_semantic_ac_key

CHECK_PACKAGE_SCHEMA = "ouroboros.check_package.v1"
PACKAGE_RECORD_SCHEMA = "ouroboros.check_package_record.v4"
PACKAGE_ID_BYTES = 32
ORACLE_PACKAGE_SCHEMA = "ouroboros.check_package.v2"
# Fields added with the oracle split. They are left out of the canonical
# bytes while empty, so a package without oracles keeps its v1 bytes and
# digest (stored v1 packages still load and verify).
_OPTIONAL_PACKAGE_FIELDS = ("oracles", "binding_grammar")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIN_LEAK_TEXT_CHARS = 16


class CheckPackageError(ValueError):
    """A package is malformed or does not belong to the given Seed."""


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize ``value`` deterministically for hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("expected a lowercase hex SHA-256 digest")
    return value


def normalize_relative_path(value: str) -> str:
    """Validate a workspace-relative POSIX path and return its normal form.

    Absolute paths, drive letters, backslashes, empty components, and ``..``
    are refused so a package can never address bytes outside the checkout.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"path must be relative POSIX: {value!r}")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", "..", "."} for part in parts):
        if value == ".":
            return "."
        raise ValueError(f"path must not contain '.' or '..' components: {value!r}")
    return PurePosixPath(*parts).as_posix()


_SCRIPT_CHECK_PREFIX = "script_"


def script_check_id(criterion_number: int, ordinal: int) -> str:
    """The product's id of the ``ordinal``-th script check first linking criterion ``criterion_number``."""
    if criterion_number < 1 or ordinal < 1:
        raise ValueError("criterion numbers and ordinals start at 1")
    return f"{_SCRIPT_CHECK_PREFIX}{criterion_number}_{ordinal}"


def script_assertion_id(check_id: str, position: int) -> str:
    """The product's id of the assertion at 1-based ``position`` in a script check."""
    if position < 1:
        raise ValueError("assertion positions start at 1")
    return f"{check_id}.a{position}"


def mint_check_ids(
    oracle_criteria: Sequence[int], script_criteria: Sequence[int]
) -> tuple[list[str], list[str]]:
    """The product's check ids, from the criterion number of each oracle and script check in order.

    The one place check ids are minted: the ``k``-th oracle of criterion
    ``n`` is ``oracle_check_id(n, k)`` and the ``k``-th script check first
    linking ``n`` is ``script_check_id(n, k)``, counting 1, 2, ... in the
    given order. The ids are a function of structure alone, so none can
    carry a value, and a package holds exactly this sequence.
    """
    counts: dict[tuple[str, int], int] = {}

    def ordinal(kind: str, number: int) -> int:
        counts[kind, number] = counts.get((kind, number), 0) + 1
        return counts[kind, number]

    return (
        [oracle_check_id(number, ordinal("oracle", number)) for number in oracle_criteria],
        [script_check_id(number, ordinal("script", number)) for number in script_criteria],
    )


class CheckRole(StrEnum):
    """What a check must do on the pinned public base state."""

    REPRODUCTION = "reproduction"
    """Must reach its intended failing assertion on the base."""

    PRESERVATION = "preservation"
    """Must pass on the base."""


class PackageFile(BaseModel, frozen=True):
    """A generated fixture or test file carried inside the package."""

    path: str
    sha256: str
    content: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        normalized = normalize_relative_path(value)
        if normalized == ".":
            raise ValueError("package file path must name a file")
        return normalized

    @model_validator(mode="after")
    def _content_matches_digest(self) -> PackageFile:
        _require_sha256(self.sha256)
        if sha256_bytes(self.content.encode("utf-8")) != self.sha256:
            raise ValueError(f"content digest mismatch for {self.path}")
        return self

    @classmethod
    def from_content(cls, path: str, content: str) -> PackageFile:
        """Build a file entry, computing its digest from ``content``."""
        return cls(path=path, sha256=sha256_bytes(content.encode("utf-8")), content=content)


class BaseFileRef(BaseModel, frozen=True):
    """A public base-checkout file a check relies on, pinned by digest."""

    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_sha256(value)


class AssertionLink(BaseModel, frozen=True):
    """One assertion inside a check and the criterion it encodes."""

    assertion_id: str = Field(..., min_length=1)
    criterion_key: str = Field(..., min_length=1)
    file: str | None = None
    locator: str | None = None

    @field_validator("file")
    @classmethod
    def _file(cls, value: str | None) -> str | None:
        return None if value is None else normalize_relative_path(value)


class CheckSpec(BaseModel, frozen=True):
    """One executable check: argv, role, and assertion links.

    ``failure_signature`` is a literal string that a reproduction check prints
    only when it reaches its intended failing assertion. A non-zero exit without
    this string (for example a setup or import failure) is indeterminate, not an
    admitted reproduction.
    """

    check_id: str = Field(..., min_length=1)
    role: CheckRole
    argv: tuple[str, ...] = Field(..., min_length=1)
    cwd: str = "."
    assertions: tuple[AssertionLink, ...] = Field(..., min_length=1)
    failure_signature: str | None = None

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def _role_contract(self) -> CheckSpec:
        if any(not isinstance(arg, str) or "\x00" in arg for arg in self.argv):
            raise ValueError(f"{self.check_id}: argv entries must be strings without NUL")
        if self.role is CheckRole.REPRODUCTION and not self.failure_signature:
            raise ValueError(f"{self.check_id}: reproduction checks require failure_signature")
        return self


class UncoveredObligation(BaseModel, frozen=True):
    """A criterion the generator could not bind to an executable check."""

    criterion_key: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class CheckPackage(BaseModel, frozen=True):
    """Immutable, separately hashed executable bindings for one Seed.

    ``sha256`` covers the canonical JSON of every field, including file
    contents, so any change to argv, links, roles, files, or provenance yields
    a new package identity.
    """

    schema_version: Literal["ouroboros.check_package.v1", "ouroboros.check_package.v2"] = (
        CHECK_PACKAGE_SCHEMA
    )
    seed_digest: str
    criterion_keys: tuple[str, ...] = Field(..., min_length=1)
    input_digest: str
    generated_at: datetime
    generator: str | None = None
    checks: tuple[CheckSpec, ...] = ()
    files: tuple[PackageFile, ...] = ()
    base_files: tuple[BaseFileRef, ...] = ()
    scratch_paths: tuple[str, ...] = ()
    uncovered: tuple[UncoveredObligation, ...] = ()
    oracles: tuple[OracleSpec, ...] = ()
    binding_grammar: str | None = None
    # Set by ``seal_package``: an opaque random id, never part of the
    # canonical bytes. Journal events, receipts and the stored record cite
    # the package by this id and its Seed digest only. The id is bound to the
    # SHA-256 the package had when it was sealed (``_sealed_sha256``, in
    # memory only): ``package_id`` refuses a package whose bytes changed
    # since, for example a ``model_copy(update=...)`` of a sealed package.
    _package_id: str | None = PrivateAttr(default=None)
    _sealed_sha256: str | None = PrivateAttr(default=None)

    @field_validator("seed_digest", "input_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("scratch_paths")
    @classmethod
    def _scratch(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_relative_path(item) for item in value)
        if "." in normalized:
            raise ValueError("scratch path must not be the checkout root")
        return normalized

    @model_validator(mode="after")
    def _structure(self) -> CheckPackage:
        keys = self.criterion_keys
        if len(set(keys)) != len(keys):
            raise ValueError("criterion_keys must be unique")
        check_ids = [check.check_id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("check_id values must be unique")
        file_paths = [item.path for item in self.files]
        if len(set(file_paths)) != len(file_paths):
            raise ValueError("package file paths must be unique")
        base_paths = [item.path for item in self.base_files]
        if len(set(base_paths)) != len(base_paths):
            raise ValueError("base file paths must be unique")
        if set(file_paths) & set(base_paths):
            raise ValueError("a path cannot be both a generated file and a base file")
        for scratch in self.scratch_paths:
            for path in (*file_paths, *base_paths):
                if _is_under(path, scratch):
                    raise ValueError(f"file {path} lies inside scratch path {scratch}")
        key_set = set(keys)
        linked: set[str] = set()
        for check in self.checks:
            for link in check.assertions:
                if link.criterion_key not in key_set:
                    raise ValueError(
                        f"{check.check_id}: assertion {link.assertion_id} links unknown "
                        f"criterion {link.criterion_key}"
                    )
                linked.add(link.criterion_key)
        self._validate_ids()
        uncovered = [item.criterion_key for item in self.uncovered]
        if len(set(uncovered)) != len(uncovered):
            raise ValueError("uncovered criterion keys must be unique")
        if not set(uncovered) <= key_set:
            raise ValueError("uncovered obligations must reference known criterion keys")
        if linked & set(uncovered):
            raise ValueError("a criterion cannot be both linked and uncovered")
        missing = key_set - linked - set(uncovered)
        if missing:
            raise ValueError(
                "every criterion must be linked to an assertion or listed as uncovered: "
                + ", ".join(sorted(missing))
            )
        self._validate_oracles()
        return self

    def _validate_ids(self) -> None:
        """Every id is the one the product mints from this package's structure (``mint_check_ids``).

        Ids are persisted, so a package carries exactly the dense sequence:
        check ids by criterion and order, cases ``c1..cN`` in order, script
        assertions ``<id>.a1..a<m>``. No identifier a caller chose, and no
        gap a caller left, can spell a value in a record, a receipt or the
        journal.
        """
        numbers = {key: number for number, key in enumerate(self.criterion_keys, start=1)}
        oracle_ids = {spec.check_id for spec in self.oracles}
        scripts = [check for check in self.checks if check.check_id not in oracle_ids]
        if any(spec.criterion_key not in numbers for spec in self.oracles):
            raise ValueError("an oracle checks a criterion the package does not list")
        oracle_expected, script_expected = mint_check_ids(
            [numbers[spec.criterion_key] for spec in self.oracles],
            [numbers[check.assertions[0].criterion_key] for check in scripts],
        )
        if [spec.check_id for spec in self.oracles] != oracle_expected or [
            check.check_id for check in scripts
        ] != script_expected:
            raise ValueError("check ids are the product's minted sequence (mint_check_ids)")
        for check in scripts:
            if [link.assertion_id for link in check.assertions] != [
                script_assertion_id(check.check_id, position)
                for position in range(1, len(check.assertions) + 1)
            ]:
                raise ValueError(f"{check.check_id}: assertion ids are the product's a1..a<m>")
        for spec in self.oracles:
            if [case.case_id for case in spec.cases] != [
                case_id_for(position) for position in range(1, len(spec.cases) + 1)
            ]:
                raise ValueError(f"{spec.check_id}: case ids are the product's c1..c<n>")

    def _validate_oracles(self) -> None:
        if not self.oracles:
            if self.binding_grammar is not None or self.schema_version != CHECK_PACKAGE_SCHEMA:
                raise ValueError("binding_grammar and schema v2 require oracles")
            return
        if self.schema_version != ORACLE_PACKAGE_SCHEMA or self.binding_grammar != BINDING_GRAMMAR:
            raise ValueError(
                f"a package with oracles is {ORACLE_PACKAGE_SCHEMA} / {BINDING_GRAMMAR}"
            )
        checks = {check.check_id: check for check in self.checks}
        files = {item.path: item.content for item in self.files}
        seen: set[str] = set()
        for spec in self.oracles:
            check = checks.get(spec.check_id)
            if check is None or spec.check_id in seen:
                raise ValueError(f"oracle {spec.check_id} needs exactly one check")
            seen.add(spec.check_id)
            if check != oracle_check(spec, check.role):
                # The harness argv, the frozen failure signature, one assertion
                # per case with the product's ids, and only its criterion.
                raise ValueError(f"oracle check {spec.check_id} must be the product harness check")
        if files.get(ORACLE_HARNESS_PATH) != ORACLE_HARNESS_SOURCE:
            raise ValueError("oracle packages carry the product harness unchanged")
        if files.get(ORACLE_DATA_PATH) != oracle_data_text(self.oracles):
            raise ValueError("oracle data file does not match the package oracles")

    def canonical_dict(self) -> dict[str, Any]:
        """Return the JSON-mode dict whose canonical bytes define ``sha256``."""
        data = self.model_dump(mode="json")
        for key in _OPTIONAL_PACKAGE_FIELDS:
            if not data.get(key):
                data.pop(key, None)
        return data

    def oracle_for(self, check_id: str) -> OracleSpec | None:
        """The oracle a check executes, or ``None`` for a model-written script."""
        return next((spec for spec in self.oracles if spec.check_id == check_id), None)

    def to_json_bytes(self) -> bytes:
        """Return the canonical serialized package."""
        return canonical_json_bytes(self.canonical_dict())

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical serialized package, held-out cases included.

        An in-memory identity check only: it is never journaled or stored,
        because an unkeyed digest of the full package would let anyone who
        reads it confirm guessed held-out values offline.
        """
        return sha256_bytes(self.to_json_bytes())

    @property
    def package_id(self) -> str:
        """The opaque id ``seal_package`` gave this package; what the journal cites.

        The one way anything obtains the id to cite or persist: it fails
        closed (``CheckPackageError``) unless the package still hashes to the
        digest it was sealed with, so no altered copy of a sealed package can
        be cited, recorded or stored under its id.
        """
        if self._package_id is None or self._sealed_sha256 is None:
            raise CheckPackageError("the package is not sealed (seal_package)")
        if self.sha256 != self._sealed_sha256:
            raise CheckPackageError("the package changed after it was sealed")
        return self._package_id

    @property
    def sealed(self) -> bool:
        return self._package_id is not None

    def manifest_summary(self) -> dict[str, Any]:
        """The package's safe projection: what the journal and the stored record carry.

        Built from an allowlist of values whose provenance the model itself
        enforces: the package id (minted by ``seal_package``; ``None`` before
        it is sealed), the Seed digest and criterion keys (bound to the Seed
        by ``validate_package_for_seed``), check and assertion ids (the
        model accepts only the product's minted forms), roles and call kinds
        (closed enums), file kinds and counts. Nothing a caller or the
        constructor supplies as free text or bytes is copied: no file path,
        argv, script, failure signature, binding symbol, parameter name,
        case, locator, scratch path, uncovered reason, generator label, input
        digest or timestamp, and no digest or size of a package file, since
        any of them can carry a held-out value or let anyone confirm a guessed
        one offline. Two packages that differ only in such values have the
        same projection, apart from their random ids.
        """
        return {
            "schema_version": self.schema_version,
            "package_id": self.package_id if self.sealed else None,
            "seed_digest": self.seed_digest,
            "criterion_keys": list(self.criterion_keys),
            "checks": [
                {
                    "check_id": check.check_id,
                    "role": check.role.value,
                    "criterion_keys": sorted({link.criterion_key for link in check.assertions}),
                    "assertion_ids": [link.assertion_id for link in check.assertions],
                }
                for check in self.checks
            ],
            "files": [_file_projection(item) for item in self.files],
            "base_file_count": len(self.base_files),
            "scratch_path_count": len(self.scratch_paths),
            "uncovered": [{"criterion_key": item.criterion_key} for item in self.uncovered],
            **self._oracle_summary(),
        }

    def _oracle_summary(self) -> dict[str, Any]:
        if not self.oracles:
            return {}
        return {
            "binding_grammar": self.binding_grammar,
            "oracles": [
                {
                    "check_id": spec.check_id,
                    "criterion_key": spec.criterion_key,
                    "call_kind": spec.call_kind.value,
                    "param_count": len(spec.params),
                    "target_named_in_criterion": spec.target_named_in_criterion,
                    "case_count": len(spec.cases),
                    "held_out_count": spec.held_out_count,
                }
                for spec in self.oracles
            ],
        }


def _file_projection(item: PackageFile) -> dict[str, Any]:
    """A package file by its product role only.

    Never by its path, and never by a digest or size of its bytes: a
    generated file's bytes are the constructor's, so an unkeyed digest (or a
    length) of them would let anyone confirm a held-out value copied into a
    script by enumerating candidates offline.
    """
    if item.path == ORACLE_DATA_PATH:
        return {"kind": "oracle_data", "held_out_redacted": True}
    return {"kind": "oracle_harness" if item.path == ORACLE_HARNESS_PATH else "generated"}


def oracle_argv(check_id: str) -> tuple[str, ...]:
    """The argv of an oracle check: the product harness, run from the controller dir."""
    return ("python3", ORACLE_HARNESS_PATH, check_id)


def oracle_check(spec: OracleSpec, role: CheckRole) -> CheckSpec:
    """The check that executes ``spec``; one assertion per case."""
    return CheckSpec(
        check_id=spec.check_id,
        role=role,
        argv=oracle_argv(spec.check_id),
        cwd=".",
        assertions=tuple(
            AssertionLink(
                assertion_id=f"{spec.check_id}.{case.case_id}",
                criterion_key=spec.criterion_key,
                locator="held-out case" if case.held_out else "case from the Seed text",
            )
            for case in spec.cases
        ),
        failure_signature=spec.failure_signature if role is CheckRole.REPRODUCTION else None,
    )


def oracle_files(oracles: Iterable[OracleSpec]) -> tuple[PackageFile, ...]:
    """The product harness and the frozen oracle data, as package files."""
    specs = tuple(oracles)
    if not specs:
        return ()
    return (
        PackageFile.from_content(ORACLE_HARNESS_PATH, ORACLE_HARNESS_SOURCE),
        PackageFile.from_content(ORACLE_DATA_PATH, oracle_data_text(specs)),
    )


def mint_package_ids(
    criterion_keys: Sequence[str],
    oracles: Sequence[tuple[OracleSpec, CheckRole]],
    script_checks: Sequence[CheckSpec],
) -> tuple[list[tuple[OracleSpec, CheckRole]], list[CheckSpec]]:
    """``oracles`` and ``script_checks`` under the ids ``mint_check_ids`` gives their structure.

    Whatever ids the parts carried (a rebuilt package after cases or oracles
    were dropped, a merge of two packages' checks), the result carries the
    dense sequence a package must hold: check ids by criterion and order,
    cases ``c1..cN``, script assertions ``a1..a<m>``.
    """
    numbers = {key: number for number, key in enumerate(criterion_keys, start=1)}
    oracle_ids, script_ids = mint_check_ids(
        [numbers[spec.criterion_key] for spec, _role in oracles],
        [numbers[check.assertions[0].criterion_key] for check in script_checks],
    )
    minted_oracles = [
        (
            OracleSpec.model_validate(
                {
                    **spec.model_dump(),
                    "check_id": check_id,
                    "cases": [
                        {**case.model_dump(), "case_id": case_id_for(position)}
                        for position, case in enumerate(spec.cases, start=1)
                    ],
                }
            ),
            role,
        )
        for check_id, (spec, role) in zip(oracle_ids, oracles, strict=True)
    ]
    minted_scripts = [
        check.model_copy(
            update={
                "check_id": check_id,
                "assertions": tuple(
                    link.model_copy(
                        update={"assertion_id": script_assertion_id(check_id, position)}
                    )
                    for position, link in enumerate(check.assertions, start=1)
                ),
            }
        )
        for check_id, check in zip(script_ids, script_checks, strict=True)
    ]
    return minted_oracles, minted_scripts


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


# --------------------------------------------------------------------------
# Seed linkage


def seed_digest(seed: Seed) -> str:
    """Return the SHA-256 of the Seed's canonical serialized form."""
    return sha256_bytes(canonical_json_bytes(seed.to_dict()))


def seed_criterion_keys(seed: Seed) -> tuple[str, ...]:
    """Return each acceptance criterion's stable semantic key, in Seed order."""
    keys: list[str] = []
    for criterion in seed.acceptance_criteria:
        if isinstance(criterion, AcceptanceCriterionSpec) and criterion.semantic_ac_key:
            keys.append(criterion.semantic_ac_key)
        else:
            keys.append(derive_semantic_ac_key(criterion))
    return tuple(keys)


def validate_package_for_seed(package: CheckPackage, seed: Seed) -> None:
    """Raise ``CheckPackageError`` unless ``package`` is bound to ``seed``."""
    if package.seed_digest != seed_digest(seed):
        raise CheckPackageError("check package seed_digest does not match the Seed")
    if tuple(package.criterion_keys) != seed_criterion_keys(seed):
        # Order included: decisions are indexed by the Seed's criterion positions.
        raise CheckPackageError("check package criterion keys do not match the Seed")


# --------------------------------------------------------------------------
# Package identity and the stored record


def seal_package(package: CheckPackage) -> CheckPackage:
    """A copy of ``package`` with a fresh opaque id (``PACKAGE_ID_BYTES`` random bytes, hex).

    The id carries no information about the package's content: receipts,
    journal events and the stored record cite it next to the Seed digest, so
    nothing persisted lets a held-out value be confirmed by enumeration.
    """
    sealed = package.model_copy(deep=True)
    sealed._package_id = secrets.token_hex(PACKAGE_ID_BYTES)
    sealed._sealed_sha256 = sealed.sha256
    return sealed


def package_record(package: CheckPackage) -> dict[str, Any]:
    """The package as the product stores it: its safe projection (``manifest_summary``).

    The record names the package by its id and Seed digest and carries only
    values the product computed, never anything the constructor wrote, so
    no held-out input or expected value can reach the disk through it. It
    cannot be loaded back as a package.
    """
    return {
        "schema_version": PACKAGE_RECORD_SCHEMA,
        "package_id": package.package_id,
        "seed_digest": package.seed_digest,
        "package": package.manifest_summary(),
    }


def package_record_bytes(package: CheckPackage) -> bytes:
    """The canonical bytes of ``package_record``: the only form a package is persisted in.

    The store writes exactly these bytes (``write_package_record``) and the
    frozen journal event records their SHA-256 (``events.package_frozen_event``),
    so the digest the journal holds is always the digest of a record without
    held-out values, never of the full package.
    """
    return canonical_json_bytes(package_record(package))


def publish_exact(target: Path, data: bytes) -> Path:
    """Publish ``data`` at ``target`` exactly once, or confirm it is already there.

    Every directory from ``/`` to the target's parent is opened by name
    without following a link (missing ones are created through their held
    parent), so no link anywhere in the path redirects the publication; one
    raises ``CheckPackageError``. A new target is created exclusively
    (``O_EXCL``, never through a link) through the held parent and written in
    full; if that fails, only the file this call created is removed. When the
    target exists, it is accepted only if it is a regular file (not followed
    through a link) whose bytes equal ``data``; anything else raises
    ``CheckPackageError``, so a file planted at the target never stands in
    for the record or receipt the caller cites.
    """
    absolute = Path(os.path.abspath(target))
    try:
        directory = open_nofollow_directory_chain(absolute.parent, create_missing=True)
    except (OSError, ValueError) as exc:
        raise CheckPackageError(
            f"cannot open the publication directory of {target.name} without following a link"
        ) from exc
    try:
        if not directory.create_exclusive(absolute.name, data):
            _confirm_published(directory, absolute.name, data)
    except HeldPathChanged as exc:
        raise CheckPackageError(f"{target.name} was replaced while it was published") from exc
    finally:
        directory.close()
    return target


def _confirm_published(directory: NoFollowDirectoryChain, name: str, data: bytes) -> None:
    try:
        published = directory.read_regular_file(name)
    except (OSError, ValueError) as exc:
        raise CheckPackageError(f"cannot confirm the published file {name}") from exc
    if published.data != data:
        raise CheckPackageError(f"a different file already exists at {name}")


def write_package_record(package: CheckPackage, directory: Path) -> Path:
    """Publish ``package_record_bytes`` at ``<directory>/<package_id>.json`` (``publish_exact``).

    A package is never persisted in any other form, so held-out inputs and
    expected values never reach the disk.
    """
    return publish_exact(directory / f"{package.package_id}.json", package_record_bytes(package))


# --------------------------------------------------------------------------
# Worker view and leak detection


class WorkerCriterion(BaseModel, frozen=True):
    """What a worker may see about a criterion: its key and description."""

    criterion_key: str
    description: str


def worker_criteria(seed: Seed) -> tuple[WorkerCriterion, ...]:
    """Return criterion descriptions only, never commands or assertions."""
    keys = seed_criterion_keys(seed)
    descriptions = [
        criterion.description
        if isinstance(criterion, AcceptanceCriterionSpec)
        else str(criterion).strip()
        for criterion in seed.acceptance_criteria
    ]
    return tuple(
        WorkerCriterion(criterion_key=key, description=description)
        for key, description in zip(keys, descriptions, strict=True)
    )


def _file_manifest(package: CheckPackage) -> tuple[set[str], list[dict[str, Any]]]:
    """Paths and ``{sha256, size}`` entries of a live package's files, computed in memory.

    Only the live package can be scanned for: its paths, digests and sizes,
    the oracle data file included, are read here and never persisted. A
    journal manifest carries none of them, so it cannot stand in for the
    package; anything else refuses the scan (``CheckPackageError``).
    """
    if not isinstance(package, CheckPackage):
        raise CheckPackageError("the leak scan needs the live, in-memory package")
    return {item.path for item in package.files}, [
        {"sha256": item.sha256, "size": len(item.content.encode("utf-8"))} for item in package.files
    ]


_MAX_SCAN_ENTRIES = 200_000
"""Entries the scan reads through links that lead out of the workspace before it refuses."""


class _Scan:
    """One workspace scan: package paths and digests, visited directories, a budget."""

    def __init__(self, paths: set[str], digests_by_size: dict[int, set[str]]) -> None:
        self.paths = paths
        self.digests_by_size = digests_by_size
        self.visited: set[tuple[int, int]] = set()
        self.budget = _MAX_SCAN_ENTRIES
        self.leaks: list[str] = []

    def file(self, full: str, relative: str, size: int) -> None:
        """A regular file (reached directly or through a link): its bytes against the digests."""
        candidates = self.digests_by_size.get(size)
        if not candidates:
            return
        try:
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError:
            # A file of a package file's size that cannot be read cannot be
            # cleared: the start is refused.
            self.leaks.append(relative)
            return
        if sha256_bytes(data) in candidates:
            self.leaks.append(relative)

    def directory(self, directory: str, prefix: str, root: str) -> None:
        try:
            status = os.stat(directory)
        except OSError:
            self.leaks.append(prefix.rstrip("/") or ".")
            return
        key = (status.st_dev, status.st_ino)
        if key in self.visited:
            return
        self.visited.add(key)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            # An unreadable directory may hide package material: refused.
            self.leaks.append(prefix.rstrip("/") or ".")
            return
        for entry in entries:
            relative = prefix + entry.name
            self.budget -= 1
            if self.budget < 0:
                self.leaks.append(relative)
                return
            if relative in self.paths:
                self.leaks.append(relative)
                continue
            self.entry(entry, relative, root)

    def entry(self, entry: os.DirEntry[str], relative: str, root: str) -> None:
        if entry.is_symlink():
            # Resolved, never trusted: the target is judged by what it is.
            try:
                target = os.stat(entry.path)
            except PermissionError:
                self.leaks.append(relative)
                return
            except OSError:
                return  # dangling or a loop: nothing to read through it
            if stat.S_ISDIR(target.st_mode):
                resolved = os.path.realpath(entry.path)
                if resolved == root or resolved.startswith(root + os.sep):
                    return  # inside the workspace: scanned on its own path
                self.directory(resolved, relative + "/", root)
            elif stat.S_ISREG(target.st_mode):
                self.file(entry.path, relative, target.st_size)
            return
        try:
            if entry.is_dir(follow_symlinks=False):
                self.directory(entry.path, relative + "/", root)
            elif entry.is_file(follow_symlinks=False):
                self.file(entry.path, relative, entry.stat(follow_symlinks=False).st_size)
        except OSError:
            self.leaks.append(relative)


def find_workspace_leaks(
    workspace: Path,
    packages: Iterable[CheckPackage],
) -> tuple[str, ...]:
    """Return workspace-relative paths that carry, or may carry, generated check files.

    A path leaks when it equals a package file path, or when the bytes it
    leads to hash to a package file digest (a renamed copy or a hard link).
    A symbolic link is resolved, never trusted: a link to a regular file is
    judged by that file's bytes; a link to a directory outside the workspace
    is scanned through (every directory at most once, within a budget of
    entries); a dangling link or one to a special file carries nothing. What
    cannot be cleared refuses the start: a file of a package file's size
    that cannot be read, an unreadable directory, or a scan that exceeds its
    budget. The product harness is product code, not generated, and a copy of
    it is not a leak.

    Pass the live packages: a ``manifest_summary()`` dict (all a caller
    holding only journal events has) carries no path, digest or size, and is
    refused (``CheckPackageError``) rather than scanned for nothing.
    """
    paths: set[str] = set()
    manifests: list[dict[str, Any]] = []
    for package in packages:
        package_paths, entries = _file_manifest(package)
        paths |= package_paths
        manifests.extend(entries)
    if not manifests:
        return ()
    harness = sha256_bytes(ORACLE_HARNESS_SOURCE.encode("utf-8"))
    digests_by_size: dict[int, set[str]] = {}
    for entry in manifests:
        if entry["sha256"] != harness:
            digests_by_size.setdefault(int(entry["size"]), set()).add(entry["sha256"])
    root = os.path.realpath(workspace)
    scan = _Scan(paths, digests_by_size)
    scan.directory(root, "", root)
    return tuple(sorted(set(scan.leaks)))


def find_text_leaks(text: str, packages: Iterable[CheckPackage]) -> tuple[str, ...]:
    """Return package file paths whose generated content appears in ``text``.

    Contents shorter than 16 characters after stripping are ignored because
    they are too generic to identify check code.
    """
    leaks: list[str] = []
    for package in packages:
        for item in package.files:
            body = item.content.strip()
            if len(body) >= _MIN_LEAK_TEXT_CHARS and body in text:
                leaks.append(item.path)
    return tuple(sorted(set(leaks)))
