"""Stable cross-process lock authority for disposable artifact storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING

from ouroboros.core.file_lock import file_lock
from ouroboros.persistence.artifact_schema import validate_contract_id

if TYPE_CHECKING:
    from ouroboros.persistence.artifact_store import ContentAddressedArtifactStore


def nearest_existing_directory(path: Path) -> Path:
    """Choose the nearest lexical ancestor that can anchor safe creation."""
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def validate_pinned_directory(
    directory_fd: int,
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> None:
    """Prove one held directory is still the live descendant of its anchor."""
    from ouroboros.persistence.artifact_store import ArtifactIntegrityError

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


@dataclass(frozen=True, slots=True)
class ArtifactLockAuthority:
    """Pinned generation of the artifact directory protected by one lock."""

    directory_fd: int | None
    path: Path
    anchor: Path
    label: str

    def validate(self) -> None:
        """Reject a renamed or replaced protected directory generation."""
        if self.directory_fd is None:
            return
        validate_pinned_directory(
            self.directory_fd,
            self.path,
            anchor=self.anchor,
            label=self.label,
        )


def _authority_target(
    store: ContentAddressedArtifactStore,
    *,
    contract_id: str | None,
) -> Path:
    """Address one lock directly inside the trusted parent boundary."""
    identity = str(store.root) if contract_id is None else f"{store.root}\0{contract_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    scope = "store" if contract_id is None else "contract"
    return store.root.parent / f".ouroboros-artifact-{scope}-{digest}"


@contextmanager
def store_lock(
    store: ContentAddressedArtifactStore,
    *,
    exclusive: bool,
    blocking: bool,
) -> Iterator[ArtifactLockAuthority]:
    """Lock the stable store authority, then pin the current artifact generation."""
    from ouroboros.persistence.artifact_store import _pinned_directory_tree

    authority_parent = store.root.parent
    with _pinned_directory_tree(
        authority_parent,
        anchor=store._lock_directory_anchor,
        root=authority_parent,
        label="trusted artifact lock anchor",
    ) as authority_fd:
        with file_lock(
            _authority_target(store, contract_id=None),
            exclusive=exclusive,
            blocking=blocking,
            parent_fd=authority_fd,
            stable_parent_authority=True,
        ):
            with _pinned_directory_tree(
                store.root,
                anchor=store._directory_anchor,
                root=store.root,
                label="artifact store lock",
            ) as directory_fd:
                with file_lock(
                    store._lock_target,
                    exclusive=exclusive,
                    blocking=blocking,
                    parent_fd=directory_fd,
                ):
                    held = ArtifactLockAuthority(
                        directory_fd=directory_fd,
                        path=store.root,
                        anchor=store._directory_anchor,
                        label="artifact store lock",
                    )
                    held.validate()
                    yield held
                    held.validate()


@contextmanager
def contract_execution_lock(
    store: ContentAddressedArtifactStore,
    contract_id: str,
    *,
    blocking: bool,
) -> Iterator[ArtifactLockAuthority]:
    """Lock one stable contract authority before its replaceable manifest tree."""
    from ouroboros.persistence.artifact_store import _pinned_directory_tree

    contract_id = validate_contract_id(contract_id)
    store.initialize()
    authority_parent = store.root.parent
    with _pinned_directory_tree(
        authority_parent,
        anchor=store._lock_directory_anchor,
        root=authority_parent,
        label="trusted artifact lock anchor",
    ) as authority_fd:
        with file_lock(
            _authority_target(store, contract_id=contract_id),
            exclusive=True,
            blocking=blocking,
            parent_fd=authority_fd,
            stable_parent_authority=True,
        ):
            lock_target = store._contract_execution_lock_target(contract_id)
            with _pinned_directory_tree(
                lock_target.parent,
                anchor=store._directory_anchor,
                root=store._contracts_root,
                label="contract execution lock",
            ) as directory_fd:
                with file_lock(
                    lock_target,
                    exclusive=True,
                    blocking=blocking,
                    parent_fd=directory_fd,
                ):
                    held = ArtifactLockAuthority(
                        directory_fd=directory_fd,
                        path=lock_target.parent,
                        anchor=store._directory_anchor,
                        label="contract execution lock",
                    )
                    held.validate()
                    yield held
                    held.validate()


__all__ = [
    "ArtifactLockAuthority",
    "contract_execution_lock",
    "nearest_existing_directory",
    "store_lock",
    "validate_pinned_directory",
]
