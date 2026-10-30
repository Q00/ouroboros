"""Probe runs in these tests use the execution sandbox's off switch by default.

Whether a host has a sandbox backend varies (``sandbox-exec`` inside another
sandbox, Landlock and user namespaces on a CI runner); where it has none,
every probe run is indeterminate. The same autouse switch is used by
``tests/unit/boundary/conftest.py``. A test of the real confinement requests
``real_check_isolation``.
"""

from __future__ import annotations

import pytest

from ouroboros.config.exec_sandbox import EXEC_SANDBOX_ENV_VAR


@pytest.fixture(autouse=True)
def _unconfined_check_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXEC_SANDBOX_ENV_VAR, "off")


@pytest.fixture
def real_check_isolation(_unconfined_check_host: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use this host's real execution sandbox."""
    monkeypatch.setenv(EXEC_SANDBOX_ENV_VAR, "on")
