"""The ACP transport uses normal configuration precedence and factory dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
import pytest

from ouroboros.config import get_copilot_acp_fallback, get_copilot_transport
from ouroboros.config.models import OrchestratorConfig, OuroborosConfig
from ouroboros.config.untrusted_env import UNTRUSTED_ENV_DENYLIST
from ouroboros.core.errors import ConfigError
from ouroboros.orchestrator.copilot_acp_runtime import CopilotAcpRuntime
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime

_FAKE = Path(__file__).parents[2] / "fixtures" / "fake_copilot_acp.py"


def test_legacy_transport_remains_the_default() -> None:
    assert OrchestratorConfig().copilot_transport == "cli"
    assert get_copilot_transport() == "cli"
    assert get_copilot_acp_fallback() is True


def test_yaml_transport_and_fallback_are_consumed() -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(copilot_transport="acp", copilot_acp_fallback=False)
    )
    with patch("ouroboros.config.copilot.load_config", return_value=config):
        assert get_copilot_transport() == "acp"
        assert get_copilot_acp_fallback() is False


def test_trusted_environment_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_COPILOT_TRANSPORT", "acp")
    monkeypatch.setenv("OUROBOROS_COPILOT_ACP_FALLBACK", "off")
    with patch("ouroboros.config.copilot.load_config") as load:
        assert get_copilot_transport() == "acp"
        assert get_copilot_acp_fallback() is False
        load.assert_not_called()


def test_invalid_transport_does_not_silently_select_another_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        OrchestratorConfig(copilot_transport="tcp")
    monkeypatch.setenv("OUROBOROS_COPILOT_TRANSPORT", "tcp")
    with pytest.raises(ValueError, match="cli or acp"):
        get_copilot_transport()
    monkeypatch.setenv("OUROBOROS_COPILOT_ACP_FALLBACK", "maybe")
    with pytest.raises(ValueError, match="boolean"):
        get_copilot_acp_fallback()


def test_project_dotenv_cannot_switch_transport_or_authorize_fallback() -> None:
    assert "OUROBOROS_COPILOT_TRANSPORT" in UNTRUSTED_ENV_DENYLIST
    assert "OUROBOROS_COPILOT_ACP_FALLBACK" in UNTRUSTED_ENV_DENYLIST


def test_invalid_yaml_cannot_silently_downgrade_to_cli_or_enable_fallback() -> None:
    with patch("ouroboros.config.copilot.load_config", side_effect=ConfigError("Invalid YAML")):
        with pytest.raises(ConfigError):
            get_copilot_transport()
        with pytest.raises(ConfigError):
            get_copilot_acp_fallback()


@pytest.mark.parametrize(
    "transport,expected", [("cli", CopilotCliRuntime), ("acp", CopilotAcpRuntime)]
)
def test_existing_copilot_factory_selects_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str, expected: type
) -> None:
    monkeypatch.setenv("OUROBOROS_COPILOT_TRANSPORT", transport)
    monkeypatch.setenv("OUROBOROS_COPILOT_ACP_FALLBACK", "false")
    adapter = create_agent_runtime(
        backend="copilot", cli_path=_FAKE, cwd=tmp_path, llm_backend="copilot"
    )
    assert type(adapter) is expected
    assert adapter.runtime_backend == "copilot_cli"
    assert adapter.llm_backend == "copilot"
    if isinstance(adapter, CopilotAcpRuntime):
        assert adapter._fallback_to_cli is False


def test_acp_factory_keeps_timeouts_profiles_and_model_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUROBOROS_COPILOT_TRANSPORT", "acp")
    with patch("ouroboros.orchestrator.runtime_factory.get_runtime_profile", return_value="worker"):
        adapter = create_agent_runtime(
            backend="copilot",
            cli_path=_FAKE,
            cwd=tmp_path,
            model="claude-haiku-4.5",
            startup_output_timeout_seconds=9,
            stdout_idle_timeout_seconds=30,
        )
    assert isinstance(adapter, CopilotAcpRuntime)
    assert adapter._startup_output_timeout_seconds == 9
    assert adapter._stdout_idle_timeout_seconds == 30
    assert adapter._runtime_profile == "worker"
    assert adapter._model == "claude-haiku-4.5"
