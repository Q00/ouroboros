"""Shared errors for disposable artifact persistence."""

from ouroboros.core.errors import PersistenceError


class ArtifactStoreError(PersistenceError):
    """Base error for deterministic artifact-store failures."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when a contract or its published body does not exist."""


class ArtifactTombstonedError(ArtifactStoreError):
    """Raised when deterministic replay points at an intentionally pruned body."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when a stored body is not the JSON it has to be to be returned."""


class ArtifactContractConflictError(ArtifactStoreError):
    """Raised when one contract id is reused for a different artifact."""


class ArtifactTooLargeError(ArtifactStoreError):
    """Raised when the encoded artifact exceeds the disposable output cap."""


__all__ = [
    "ArtifactContractConflictError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactStoreError",
    "ArtifactTombstonedError",
    "ArtifactTooLargeError",
]
