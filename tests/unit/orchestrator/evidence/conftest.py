"""Shared fixtures for the evidence verifier tests."""

from __future__ import annotations

import pytest

from ouroboros.config.exec_sandbox import EXEC_SANDBOX_ENV_VAR


@pytest.fixture(autouse=True)
def _unconfined_replay_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay with the execution sandbox switched off.

    Replay refuses to run where the sandbox is unavailable, and whether this
    host has a backend (``sandbox-exec`` inside another sandbox, Landlock and
    unprivileged user namespaces on a CI runner) varies. Tests of linkage,
    selection and protection must not depend on it. Switched off, replay also
    takes the fingerprint path for linked live trees on every platform. Tests
    of the real sandbox request ``real_replay_isolation``.
    """
    monkeypatch.setenv(EXEC_SANDBOX_ENV_VAR, "off")


@pytest.fixture
def real_replay_isolation(_unconfined_replay_host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use this host's real execution sandbox."""
    monkeypatch.setenv(EXEC_SANDBOX_ENV_VAR, "on")
