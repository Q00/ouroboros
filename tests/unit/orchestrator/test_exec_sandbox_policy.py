"""Where the orchestrator's execution-sandbox policy comes from (``config/exec_sandbox.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.config import exec_sandbox as policy
from ouroboros.config.untrusted_env import UNTRUSTED_ENV_DENYLIST


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import ouroboros.config.loader as loader

    monkeypatch.setattr(loader, "get_config_dir", lambda: tmp_path)
    monkeypatch.delenv(policy.EXEC_SANDBOX_ENV_VAR, raising=False)
    return tmp_path


def test_on_by_default(config_dir: Path) -> None:
    assert policy.exec_sandbox_config_enabled() is True
    assert policy.exec_sandbox_enabled() is True


def test_config_false_switches_it_off(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text("execution:\n  exec_sandbox: false\n")

    assert policy.exec_sandbox_config_enabled() is False
    assert policy.exec_sandbox_enabled() is False


@pytest.mark.parametrize(("value", "enabled"), [("off", False), ("0", False), ("on", True)])
def test_environment_wins_over_config(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str, enabled: bool
) -> None:
    (config_dir / "config.yaml").write_text("execution:\n  exec_sandbox: false\n")
    monkeypatch.setenv(policy.EXEC_SANDBOX_ENV_VAR, value)

    assert policy.exec_sandbox_enabled() is enabled


def test_unreadable_config_keeps_it_on(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text("execution: [not, a, mapping\n")

    assert policy.exec_sandbox_enabled() is True


def test_a_project_env_file_cannot_switch_it_off() -> None:
    assert policy.EXEC_SANDBOX_ENV_VAR in UNTRUSTED_ENV_DENYLIST
