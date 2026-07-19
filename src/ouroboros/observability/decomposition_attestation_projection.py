"""Shared ordering contract for decomposition-attestation projections."""

from __future__ import annotations

from collections.abc import Mapping


def decomposition_attestation_retry_rank(data: Mapping[str, object]) -> int:
    """Return the authoritative retry ordering rank for an attestation event.

    Valid retry attempts are non-negative integers. Legacy or malformed events
    rank at ``-1`` so they retain their historical chronological last-write-wins
    behavior among themselves, but can never overwrite an attestation carrying
    a valid attempt identity.
    """
    value = data.get("retry_attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return -1
    return value
