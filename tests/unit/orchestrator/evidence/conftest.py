"""Shared fixtures for the evidence verifier tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ouroboros.orchestrator.evidence import command_replay

_REAL_NETWORK_ISOLATION_PREFIX: Callable[[], tuple[str, ...] | None] = (
    command_replay.network_isolation_prefix
)


@pytest.fixture(autouse=True)
def _offline_replay_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay as on a host that is already offline (an empty isolation prefix).

    Replay refuses to run without network isolation, and whether this host can
    isolate (``sandbox-exec`` inside another sandbox, unprivileged user
    namespaces on a CI runner) varies. Tests of linkage, selection and
    protection must not depend on it. The empty prefix also takes the
    fingerprint path for linked live trees on every platform. Tests of the real
    mechanism request ``real_replay_isolation``.
    """
    monkeypatch.setattr(command_replay, "network_isolation_prefix", lambda: ())


@pytest.fixture
def real_replay_isolation(_offline_replay_host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use this host's real network isolation probe."""
    monkeypatch.setattr(command_replay, "network_isolation_prefix", _REAL_NETWORK_ISOLATION_PREFIX)
