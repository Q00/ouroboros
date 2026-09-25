"""Separately hashed check package linked to an immutable Seed.

A check package holds the executable bindings generated for a Seed's
acceptance criteria. It is stored and hashed apart from the Seed, so a check
can be audited or regenerated without changing requirement identity, and
receipts can cite both digests. The Seed is referenced only by its digest and
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

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed, derive_semantic_ac_key

CHECK_PACKAGE_SCHEMA = "ouroboros.check_package.v1"

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

    schema_version: Literal["ouroboros.check_package.v1"] = CHECK_PACKAGE_SCHEMA
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
        return self

    def canonical_dict(self) -> dict[str, Any]:
        """Return the JSON-mode dict whose canonical bytes define ``sha256``."""
        return self.model_dump(mode="json")

    def to_json_bytes(self) -> bytes:
        """Return the canonical serialized package."""
        return canonical_json_bytes(self.canonical_dict())

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical serialized package."""
        return sha256_bytes(self.to_json_bytes())

    def manifest_summary(self) -> dict[str, Any]:
        """Return an event-safe summary: digests, roles, links, no check code.

        File contents and argv are excluded so the summary can be persisted in
        the shared event journal without exposing generated check code.
        """
        return {
            "schema_version": self.schema_version,
            "package_sha256": self.sha256,
            "seed_digest": self.seed_digest,
            "input_digest": self.input_digest,
            "generated_at": self.generated_at.isoformat(),
            "generator": self.generator,
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
            "files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": len(item.content.encode("utf-8")),
                }
                for item in self.files
            ],
            "base_files": [item.model_dump(mode="json") for item in self.base_files],
            "scratch_paths": list(self.scratch_paths),
            "uncovered": [item.model_dump(mode="json") for item in self.uncovered],
        }


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
    if set(package.criterion_keys) != set(seed_criterion_keys(seed)):
        raise CheckPackageError("check package criterion keys do not match the Seed")


# --------------------------------------------------------------------------
# Persistence of the package artifact


def write_check_package(package: CheckPackage, directory: Path) -> Path:
    """Write the canonical package bytes to ``<directory>/<sha256>.json``.

    Writing is create-only: an existing file is accepted only if its bytes are
    identical, so a stored package can never be silently replaced.
    """
    directory.mkdir(parents=True, exist_ok=True)
    data = package.to_json_bytes()
    target = directory / f"{sha256_bytes(data)}.json"
    if target.exists():
        if target.read_bytes() != data:
            raise CheckPackageError(f"stored package differs from its digest name: {target}")
        return target
    with open(target, "xb") as handle:
        handle.write(data)
    return target


def load_check_package(path: Path, *, expected_sha256: str | None = None) -> CheckPackage:
    """Load a stored package and verify its digest."""
    data = path.read_bytes()
    package = CheckPackage.model_validate_json(data)
    if package.sha256 != sha256_bytes(data):
        raise CheckPackageError(f"package bytes are not canonical: {path}")
    if expected_sha256 is not None and package.sha256 != expected_sha256:
        raise CheckPackageError("package digest does not match the expected digest")
    return package


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


def _file_manifest(package_or_manifest: CheckPackage | Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(package_or_manifest, CheckPackage):
        return package_or_manifest.manifest_summary()["files"]
    return list(package_or_manifest.get("files", []))


def find_workspace_leaks(
    workspace: Path,
    packages: Iterable[CheckPackage | Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return workspace-relative paths that carry generated check files.

    A file leaks when its relative path equals a package file path, or when its
    bytes hash to a package file digest (a renamed copy). Accepts packages or
    their ``manifest_summary()`` dicts, so a caller holding only journal events
    can run the check.
    """
    manifests = [entry for package in packages for entry in _file_manifest(package)]
    if not manifests:
        return ()
    paths = {entry["path"] for entry in manifests}
    digests_by_size: dict[int, set[str]] = {}
    for entry in manifests:
        digests_by_size.setdefault(int(entry["size"]), set()).add(entry["sha256"])
    root = workspace.resolve()
    leaks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for filename in filenames:
            full = Path(dirpath) / filename
            relative = full.relative_to(root).as_posix()
            if relative in paths:
                leaks.append(relative)
                continue
            if full.is_symlink() or not full.is_file():
                continue
            candidates = digests_by_size.get(full.stat().st_size)
            if candidates and sha256_bytes(full.read_bytes()) in candidates:
                leaks.append(relative)
    return tuple(sorted(leaks))


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
