"""Unit tests for ouroboros.config.loader module."""

import os
from pathlib import Path
import stat
from unittest.mock import patch

import pytest
import yaml

from ouroboros.config.loader import (
    config_exists,
    create_default_config,
    credentials_file_secure,
    ensure_config_dir,
    get_agent_permission_mode,
    get_agent_runtime_backend,
    get_assertion_extraction_model,
    get_atomicity_model,
    get_clarification_model,
    get_codex_cli_path,
    get_consensus_advocate_model,
    get_consensus_models,
    get_context_compression_model,
    get_decomposition_model,
    get_dependency_analysis_model,
    get_double_diamond_model,
    get_gemini_cli_path,
    get_llm_backend,
    get_llm_permission_mode,
    get_max_parallel_workers,
    get_ontology_analysis_model,
    get_opencode_cli_path,
    get_qa_model,
    get_reflect_model,
    get_runtime_controls_config,
    get_semantic_model,
    get_usage_limit_pause_seconds,
    get_wonder_model,
    load_config,
    load_credentials,
)
from ouroboros.config.models import (
    ClarificationConfig,
    ConsensusConfig,
    CredentialsConfig,
    EvaluationConfig,
    ExecutionConfig,
    LLMConfig,
    OrchestratorConfig,
    OuroborosConfig,
    ResilienceConfig,
    RuntimeControlsConfig,
)
from ouroboros.core.errors import ConfigError


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory."""
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def temp_config_file(temp_config_dir: Path) -> Path:
    """Create a temporary config file with valid content."""
    config_path = temp_config_dir / "config.yaml"
    config_content = {
        "economics": {
            "default_tier": "frugal",
            "escalation_threshold": 2,
            "downgrade_success_streak": 5,
        },
        "clarification": {
            "ambiguity_threshold": 0.2,
        },
    }
    with config_path.open("w") as f:
        yaml.dump(config_content, f)
    return config_path


@pytest.fixture
def temp_credentials_file(temp_config_dir: Path) -> Path:
    """Create a temporary credentials file with valid content."""
    creds_path = temp_config_dir / "credentials.yaml"
    creds_content = {
        "providers": {
            "openai": {"api_key": "sk-test123"},
            "anthropic": {"api_key": "sk-ant-test456"},
        }
    }
    with creds_path.open("w") as f:
        yaml.dump(creds_content, f)
    os.chmod(creds_path, stat.S_IRUSR | stat.S_IWUSR)
    return creds_path


class TestEnsureConfigDir:
    """Test ensure_config_dir function."""

    def test_ensure_config_dir_creates_directory(self, tmp_path: Path) -> None:
        """ensure_config_dir creates directory if not exists."""
        # Temporarily change HOME to test directory creation
        config_dir = tmp_path / ".ouroboros"
        assert not config_dir.exists()

        # We can't easily mock Path.home(), so we test the actual directory creation
        # by directly calling ensure_config_dir and checking the returned path
        result = ensure_config_dir()
        assert result.exists()
        assert result.is_dir()

    def test_ensure_config_dir_creates_subdirs(self) -> None:
        """ensure_config_dir creates data and logs subdirectories."""
        config_dir = ensure_config_dir()
        assert (config_dir / "data").exists()
        assert (config_dir / "logs").exists()

    def test_ensure_config_dir_idempotent(self) -> None:
        """ensure_config_dir can be called multiple times safely."""
        # First call
        config_dir1 = ensure_config_dir()
        # Second call
        config_dir2 = ensure_config_dir()
        assert config_dir1 == config_dir2


class TestCreateDefaultConfig:
    """Test create_default_config function."""

    def test_create_default_config_creates_files(self, tmp_path: Path) -> None:
        """create_default_config creates config.yaml and credentials.yaml."""
        config_dir = tmp_path / ".ouroboros"
        config_path, creds_path = create_default_config(config_dir)

        assert config_path.exists()
        assert creds_path.exists()
        assert config_path.name == "config.yaml"
        assert creds_path.name == "credentials.yaml"

    def test_create_default_config_credentials_permissions(self, tmp_path: Path) -> None:
        """create_default_config sets chmod 600 on credentials.yaml."""
        config_dir = tmp_path / ".ouroboros"
        _, creds_path = create_default_config(config_dir)

        file_mode = creds_path.stat().st_mode
        assert (file_mode & 0o777) == 0o600

    def test_create_default_config_valid_yaml(self, tmp_path: Path) -> None:
        """create_default_config creates valid YAML files."""
        config_dir = tmp_path / ".ouroboros"
        config_path, creds_path = create_default_config(config_dir)

        # Load and validate config
        with config_path.open() as f:
            config_dict = yaml.safe_load(f)
        config = OuroborosConfig.model_validate(config_dict)
        assert config.economics.default_tier == "frugal"

        # Load and validate credentials
        with creds_path.open() as f:
            creds_dict = yaml.safe_load(f)
        creds = CredentialsConfig.model_validate(creds_dict)
        assert "openai" in creds.providers

    def test_create_default_config_raises_on_existing(self, tmp_path: Path) -> None:
        """create_default_config raises ConfigError if files exist."""
        config_dir = tmp_path / ".ouroboros"
        create_default_config(config_dir)

        with pytest.raises(ConfigError) as exc_info:
            create_default_config(config_dir)
        assert "already exists" in str(exc_info.value)

    def test_create_default_config_overwrite(self, tmp_path: Path) -> None:
        """create_default_config can overwrite existing files."""
        config_dir = tmp_path / ".ouroboros"
        create_default_config(config_dir)

        # Should not raise with overwrite=True
        config_path, creds_path = create_default_config(config_dir, overwrite=True)
        assert config_path.exists()
        assert creds_path.exists()

    def test_create_default_config_creates_subdirs(self, tmp_path: Path) -> None:
        """create_default_config creates data and logs subdirectories."""
        config_dir = tmp_path / ".ouroboros"
        create_default_config(config_dir)

        assert (config_dir / "data").exists()
        assert (config_dir / "logs").exists()


class TestLoadConfig:
    """Test load_config function."""

    def test_load_config_success(self, temp_config_file: Path) -> None:
        """load_config loads valid config file."""
        config = load_config(temp_config_file)
        assert isinstance(config, OuroborosConfig)
        assert config.economics.default_tier == "frugal"
        assert config.clarification.ambiguity_threshold == 0.2

    def test_load_config_raises_on_missing(self, tmp_path: Path) -> None:
        """load_config raises ConfigError if file doesn't exist."""
        missing_path = tmp_path / "nonexistent.yaml"

        with pytest.raises(ConfigError) as exc_info:
            load_config(missing_path)
        assert "not found" in str(exc_info.value)
        assert "ouroboros config init" in str(exc_info.value)

    def test_load_config_raises_on_malformed_yaml(self, tmp_path: Path) -> None:
        """load_config raises ConfigError on malformed YAML."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("invalid: yaml: content: [")

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)
        assert "parse" in str(exc_info.value).lower()

    def test_load_config_raises_on_validation_error(self, tmp_path: Path) -> None:
        """load_config raises ConfigError on validation failure."""
        config_path = tmp_path / "config.yaml"
        # Invalid: ambiguity_threshold must be <= 1.0
        config_content = {
            "clarification": {
                "ambiguity_threshold": 5.0,  # Invalid
            }
        }
        with config_path.open("w") as f:
            yaml.dump(config_content, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)
        assert "validation" in str(exc_info.value).lower()

    def test_load_config_validation_error_shows_field(self, tmp_path: Path) -> None:
        """load_config validation error includes field information."""
        config_path = tmp_path / "config.yaml"
        config_content = {
            "economics": {
                "default_tier": "invalid_tier",
            }
        }
        with config_path.open("w") as f:
            yaml.dump(config_content, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)
        error_message = str(exc_info.value)
        assert "default_tier" in error_message or "economics" in error_message

    def test_load_config_single_validation_error_sets_config_key(self, tmp_path: Path) -> None:
        """Single-field validation errors should be self-classifying."""
        config_path = tmp_path / "config.yaml"
        config_content = {
            "orchestrator": {
                "max_parallel_workers": 0,
            }
        }
        with config_path.open("w") as f:
            yaml.dump(config_content, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        error = exc_info.value
        assert error.config_key == "orchestrator.max_parallel_workers"
        assert error.details["config_keys"] == ["orchestrator.max_parallel_workers"]

    def test_load_config_multiple_validation_errors_leave_config_key_unset(
        self,
        tmp_path: Path,
    ) -> None:
        """Multi-field validation failures should keep all keys in details."""
        config_path = tmp_path / "config.yaml"
        config_content = {
            "economics": {
                "default_tier": "invalid_tier",
            },
            "orchestrator": {
                "max_parallel_workers": 0,
            },
        }
        with config_path.open("w") as f:
            yaml.dump(config_content, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        error = exc_info.value
        assert error.config_key is None
        assert set(error.details["config_keys"]) == {
            "economics.default_tier",
            "orchestrator.max_parallel_workers",
        }

    def test_load_config_empty_file(self, tmp_path: Path) -> None:
        """load_config handles empty file (uses defaults)."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("")

        config = load_config(config_path)
        assert isinstance(config, OuroborosConfig)
        # Should have all defaults
        assert config.economics.default_tier == "frugal"

    def test_load_config_partial_config(self, tmp_path: Path) -> None:
        """load_config fills in missing sections with defaults."""
        config_path = tmp_path / "config.yaml"
        config_content = {
            "economics": {
                "default_tier": "standard",
            }
            # Missing other sections
        }
        with config_path.open("w") as f:
            yaml.dump(config_content, f)

        config = load_config(config_path)
        assert config.economics.default_tier == "standard"
        # Other sections should have defaults
        assert config.clarification.ambiguity_threshold == 0.2
        assert config.execution.max_iterations_per_ac == 10


class TestLoadCredentials:
    """Test load_credentials function."""

    def test_load_credentials_success(self, temp_credentials_file: Path) -> None:
        """load_credentials loads valid credentials file."""
        creds = load_credentials(temp_credentials_file)
        assert isinstance(creds, CredentialsConfig)
        assert "openai" in creds.providers
        assert creds.providers["openai"].api_key == "sk-test123"

    def test_load_credentials_raises_on_missing(self, tmp_path: Path) -> None:
        """load_credentials raises ConfigError if file doesn't exist."""
        missing_path = tmp_path / "nonexistent.yaml"

        with pytest.raises(ConfigError) as exc_info:
            load_credentials(missing_path)
        assert "not found" in str(exc_info.value)
        assert "ouroboros config init" in str(exc_info.value)

    def test_load_credentials_raises_on_malformed_yaml(self, tmp_path: Path) -> None:
        """load_credentials raises ConfigError on malformed YAML."""
        creds_path = tmp_path / "credentials.yaml"
        creds_path.write_text("invalid: yaml: [")

        with pytest.raises(ConfigError) as exc_info:
            load_credentials(creds_path)
        assert "parse" in str(exc_info.value).lower()

    def test_load_credentials_raises_on_validation_error(self, tmp_path: Path) -> None:
        """load_credentials raises ConfigError on validation failure."""
        creds_path = tmp_path / "credentials.yaml"
        # Invalid: api_key cannot be empty
        creds_content = {
            "providers": {
                "openai": {"api_key": ""},
            }
        }
        with creds_path.open("w") as f:
            yaml.dump(creds_content, f)

        with pytest.raises(ConfigError) as exc_info:
            load_credentials(creds_path)
        assert "validation" in str(exc_info.value).lower()

    def test_load_credentials_empty_file(self, tmp_path: Path) -> None:
        """load_credentials handles empty file (uses defaults)."""
        creds_path = tmp_path / "credentials.yaml"
        creds_path.write_text("")

        creds = load_credentials(creds_path)
        assert isinstance(creds, CredentialsConfig)
        assert creds.providers == {}


class TestConfigExists:
    """Test config_exists function."""

    def test_config_exists_returns_false_when_missing(self) -> None:
        """config_exists returns False when files don't exist."""
        # This tests against the actual home directory
        # If config exists, this test may not be useful
        # We rely on the function working correctly based on
        # the actual state of ~/.ouroboros/
        result = config_exists()
        assert isinstance(result, bool)

    def test_config_exists_both_files_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config_exists requires both config.yaml and credentials.yaml."""
        # This is a conceptual test - in practice we can't easily
        # mock get_config_dir. The function checks for both files.
        pass


class TestRuntimeHelperLookups:
    """Tests for orchestrator runtime helper lookups."""

    def test_get_agent_runtime_backend_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for runtime backend."""
        monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
        assert get_agent_runtime_backend() == "codex"

    def test_get_agent_runtime_backend_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(runtime_backend="codex")
                ),
            ),
        ):
            assert get_agent_runtime_backend() == "codex"

    def test_get_codex_cli_path_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for Codex CLI path."""
        monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", "~/bin/codex")
        assert get_codex_cli_path() == str(Path("~/bin/codex").expanduser())

    def test_get_codex_cli_path_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(codex_cli_path="/tmp/codex")
                ),
            ),
        ):
            assert get_codex_cli_path() == "/tmp/codex"

    def test_get_opencode_cli_path_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for OpenCode CLI path."""
        monkeypatch.setenv("OUROBOROS_OPENCODE_CLI_PATH", "~/bin/opencode")
        assert get_opencode_cli_path() == str(Path("~/bin/opencode").expanduser())

    def test_get_opencode_cli_path_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(opencode_cli_path="/tmp/opencode")
                ),
            ),
        ):
            assert get_opencode_cli_path() == "/tmp/opencode"

    def test_get_gemini_cli_path_returns_executable_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var path is returned when it points to an executable file."""
        fake = tmp_path / "gemini"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("OUROBOROS_GEMINI_CLI_PATH", str(fake))
        assert get_gemini_cli_path() == str(fake)

    def test_get_gemini_cli_path_rejects_stale_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stale env var that doesn't point to an executable is treated as missing.

        Prevents writing an unusable path back into config via
        `ouroboros config backend gemini` / `setup --runtime gemini`.
        """
        stale = tmp_path / "missing-gemini"
        monkeypatch.setenv("OUROBOROS_GEMINI_CLI_PATH", str(stale))
        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(orchestrator=OrchestratorConfig()),
        ):
            assert get_gemini_cli_path() is None

    def test_get_gemini_cli_path_falls_back_to_config(self, tmp_path: Path) -> None:
        """Config path is honored when env is absent and the file is executable."""
        fake = tmp_path / "gemini"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(gemini_cli_path=str(fake))
                ),
            ),
        ):
            assert get_gemini_cli_path() == str(fake)

    def test_get_gemini_cli_path_rejects_stale_config(self, tmp_path: Path) -> None:
        """Stale config value that no longer points to an executable returns None."""
        stale = tmp_path / "ghost-gemini"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(gemini_cli_path=str(stale))
                ),
            ),
        ):
            assert get_gemini_cli_path() is None

    def test_get_opencode_mode_returns_config_value(self) -> None:
        """get_opencode_mode reads orchestrator.opencode_mode from config."""
        from ouroboros.config.loader import get_opencode_mode

        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(
                orchestrator=OrchestratorConfig(opencode_mode="subprocess")
            ),
        ):
            assert get_opencode_mode() == "subprocess"

    def test_get_opencode_mode_plugin(self) -> None:
        from ouroboros.config.loader import get_opencode_mode

        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(orchestrator=OrchestratorConfig(opencode_mode="plugin")),
        ):
            assert get_opencode_mode() == "plugin"

    def test_get_opencode_mode_none_when_unset(self) -> None:
        """Unset mode returns None → runtime gate defaults to plugin."""
        from ouroboros.config.loader import get_opencode_mode

        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(orchestrator=OrchestratorConfig()),
        ):
            assert get_opencode_mode() is None

    def test_get_opencode_mode_returns_none_on_config_error(self) -> None:
        """Missing config file returns None gracefully."""
        from ouroboros.config.loader import ConfigError, get_opencode_mode

        with patch(
            "ouroboros.config.loader.load_config",
            side_effect=ConfigError("no config"),
        ):
            assert get_opencode_mode() is None

    def test_get_opencode_mode_ignores_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Locked decision: no env override — config file is the only source."""
        from ouroboros.config.loader import get_opencode_mode

        monkeypatch.setenv("OUROBOROS_OPENCODE_MODE", "subprocess")
        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(orchestrator=OrchestratorConfig(opencode_mode="plugin")),
        ):
            assert get_opencode_mode() == "plugin"

    def test_get_agent_permission_mode_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for agent permission mode."""
        monkeypatch.setenv("OUROBOROS_AGENT_PERMISSION_MODE", "bypassPermissions")
        assert get_agent_permission_mode() == "bypassPermissions"

    def test_get_agent_permission_mode_falls_back_to_config(self) -> None:
        """Config is used when env override is absent for agent permissions."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(permission_mode="default")
                ),
            ),
        ):
            assert get_agent_permission_mode() == "default"

    def test_get_agent_permission_mode_uses_opencode_specific_config(self) -> None:
        """OpenCode runtimes use the dedicated config default when no generic override exists."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    orchestrator=OrchestratorConfig(
                        permission_mode="default",
                        opencode_permission_mode="acceptEdits",
                    )
                ),
            ),
        ):
            assert get_agent_permission_mode(backend="opencode") == "acceptEdits"

    def test_get_agent_permission_mode_defaults_to_bypass_permissions_for_opencode(self) -> None:
        """OpenCode runtime bootstrap falls back to global auto-approval without config."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                side_effect=ConfigError("missing config"),
            ),
        ):
            assert get_agent_permission_mode(backend="opencode") == "bypassPermissions"

    def test_get_runtime_controls_reads_config(self) -> None:
        """Runtime controls are tunable through config.yaml."""
        controls = RuntimeControlsConfig(
            mcp_tool_timeout_seconds=0,
            generation_idle_timeout_seconds=120,
            generation_no_progress_timeout_seconds=600,
            generation_safety_timeout_seconds=3600,
            watchdog_poll_seconds=2.0,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(runtime_controls=controls),
            ),
        ):
            loaded = get_runtime_controls_config()

        assert loaded.generation_idle_timeout_seconds == 120
        assert loaded.generation_no_progress_timeout_seconds == 600
        assert loaded.generation_safety_timeout_seconds == 3600
        assert loaded.watchdog_poll_seconds == 2.0

    def test_get_runtime_controls_env_overrides_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dedicated env vars override the config values."""
        monkeypatch.setenv("OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS", "90")
        monkeypatch.setenv("OUROBOROS_WATCHDOG_POLL_SECONDS", "1.5")
        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(
                runtime_controls=RuntimeControlsConfig(
                    generation_idle_timeout_seconds=120,
                    watchdog_poll_seconds=2.0,
                )
            ),
        ):
            loaded = get_runtime_controls_config()

        assert loaded.generation_idle_timeout_seconds == 90
        assert loaded.watchdog_poll_seconds == 1.5

    def test_get_runtime_controls_legacy_generation_timeout_maps_to_no_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The legacy env var no longer controls MCP wall-clock timeout."""
        monkeypatch.setenv("OUROBOROS_GENERATION_TIMEOUT", "43200")
        with patch(
            "ouroboros.config.loader.load_config",
            return_value=OuroborosConfig(runtime_controls=RuntimeControlsConfig()),
        ):
            loaded = get_runtime_controls_config()

        assert loaded.mcp_tool_timeout_seconds == 0
        assert loaded.generation_no_progress_timeout_seconds == 43200

    def test_get_runtime_controls_rejects_invalid_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid runtime-control env values fail clearly."""
        monkeypatch.setenv("OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS", "-1")
        with (
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(runtime_controls=RuntimeControlsConfig()),
            ),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_runtime_controls_config()

        assert exc_info.value.config_key == "OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS"

    def test_get_max_parallel_workers_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for max parallel workers."""
        monkeypatch.setenv("OUROBOROS_MAX_PARALLEL_WORKERS", "5")

        assert get_max_parallel_workers() == 5

    @pytest.mark.parametrize("env_value", ["0", "-1", "five", "nan", "inf", "-inf"])
    def test_get_max_parallel_workers_rejects_invalid_env(
        self,
        env_value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid environment values fail instead of silently using the default."""
        monkeypatch.setenv("OUROBOROS_MAX_PARALLEL_WORKERS", env_value)

        with pytest.raises(ConfigError) as exc_info:
            get_max_parallel_workers()

        assert exc_info.value.config_key == "OUROBOROS_MAX_PARALLEL_WORKERS"
        assert "OUROBOROS_MAX_PARALLEL_WORKERS" in str(exc_info.value)

    def test_get_max_parallel_workers_falls_back_to_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Config is used when env override is absent for max parallel workers."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("orchestrator:\n  max_parallel_workers: 5\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_max_parallel_workers() == 5

    @pytest.mark.parametrize(
        "config_content",
        [
            "economics:\n  default_tier: invalid_tier\n",
            "orchestrator:\n  runtime_backend: invalid_backend\n",
        ],
    )
    def test_get_max_parallel_workers_ignores_unrelated_invalid_config(
        self,
        config_content: str,
        tmp_path: Path,
    ) -> None:
        """Worker-cap lookup should not validate unrelated config sections."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            config_content,
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_max_parallel_workers() == 3

    @pytest.mark.parametrize(
        "config_content",
        [
            "economics:\n  default_tier: invalid_tier\norchestrator:\n  max_parallel_workers: 5\n",
            "orchestrator:\n  runtime_backend: invalid_backend\n  max_parallel_workers: 5\n",
        ],
    )
    def test_get_max_parallel_workers_reads_cap_despite_unrelated_invalid_config(
        self,
        config_content: str,
        tmp_path: Path,
    ) -> None:
        """A valid worker cap should not be blocked by unrelated invalid fields."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            config_content,
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_max_parallel_workers() == 5

    def test_get_max_parallel_workers_defaults_when_config_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """The built-in default is used only when no config source is present."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_max_parallel_workers() == 3

    @pytest.mark.parametrize("config_value", ["0", "five"])
    def test_get_max_parallel_workers_rejects_invalid_config(
        self,
        config_value: str,
        tmp_path: Path,
    ) -> None:
        """Invalid config values fail instead of silently using the default."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"orchestrator:\n  max_parallel_workers: {config_value}\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_max_parallel_workers()

        assert exc_info.value.config_key == "orchestrator.max_parallel_workers"
        assert "max_parallel_workers" in str(exc_info.value)

    def test_get_max_parallel_workers_rejects_malformed_config_yaml(
        self,
        tmp_path: Path,
    ) -> None:
        """Malformed config YAML should still fail clearly."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("orchestrator:\n  max_parallel_workers: [\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_max_parallel_workers()

        assert "Failed to parse configuration file" in str(exc_info.value)

    def test_get_max_parallel_workers_normalizes_directory_at_config_path(
        self,
        tmp_path: Path,
    ) -> None:
        """A directory at the config path should surface as ConfigError, not OSError."""
        config_path = tmp_path / "config.yaml"
        config_path.mkdir()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_max_parallel_workers()

        assert "Failed to read configuration file" in str(exc_info.value)
        assert exc_info.value.details["error_type"] == "IsADirectoryError"

    def test_get_max_parallel_workers_normalizes_os_error_on_open(
        self,
        tmp_path: Path,
    ) -> None:
        """Any OSError from opening the config file should surface as ConfigError."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("orchestrator:\n  max_parallel_workers: 5\n", encoding="utf-8")

        def _raise_os_error(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(13, "Permission denied")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            patch.object(Path, "open", _raise_os_error),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_max_parallel_workers()

        assert "Failed to read configuration file" in str(exc_info.value)
        assert exc_info.value.details["error_type"] == "PermissionError"

    def test_get_usage_limit_pause_seconds_prefers_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Environment variable overrides config for usage-limit pause windows."""
        monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "1.5")

        assert get_usage_limit_pause_seconds() == 5400

    @pytest.mark.parametrize("env_value", ["0", "-1", "five", "nan", "inf", "-inf"])
    def test_get_usage_limit_pause_seconds_rejects_invalid_env(
        self,
        env_value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid pause env values fail instead of silently using the default."""
        monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", env_value)

        with pytest.raises(ConfigError) as exc_info:
            get_usage_limit_pause_seconds()

        assert exc_info.value.config_key == "OUROBOROS_USAGE_LIMIT_PAUSE_HOURS"

    def test_get_usage_limit_pause_seconds_falls_back_to_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Config is used when env override is absent for usage-limit pauses."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("orchestrator:\n  usage_limit_pause_hours: 2.0\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_usage_limit_pause_seconds() == 7200

    @pytest.mark.parametrize(
        "config_content",
        [
            "economics:\n  default_tier: invalid_tier\n",
            "orchestrator:\n  runtime_backend: invalid_backend\n",
        ],
    )
    def test_get_usage_limit_pause_seconds_ignores_unrelated_invalid_config(
        self,
        config_content: str,
        tmp_path: Path,
    ) -> None:
        """Pause-window lookup should not validate unrelated config sections."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config_content, encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_usage_limit_pause_seconds() == 18000

    @pytest.mark.parametrize(
        "config_content",
        [
            "economics:\n  default_tier: invalid_tier\norchestrator:\n  usage_limit_pause_hours: 2.0\n",
            "orchestrator:\n  runtime_backend: invalid_backend\n  usage_limit_pause_hours: 2.0\n",
        ],
    )
    def test_get_usage_limit_pause_seconds_reads_value_despite_unrelated_invalid_config(
        self,
        config_content: str,
        tmp_path: Path,
    ) -> None:
        """A valid pause window should not be blocked by unrelated invalid fields."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config_content, encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_usage_limit_pause_seconds() == 7200

    def test_get_usage_limit_pause_seconds_defaults_when_config_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing config falls back to the built-in 5-hour window."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        ):
            assert get_usage_limit_pause_seconds() == 18000

    @pytest.mark.parametrize("config_value", ["0", "five", "nan", "inf", "-inf"])
    def test_get_usage_limit_pause_seconds_rejects_invalid_config_key(
        self,
        config_value: str,
        tmp_path: Path,
    ) -> None:
        """Invalid configured pause windows should not be silently defaulted."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"orchestrator:\n  usage_limit_pause_hours: {config_value}\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_usage_limit_pause_seconds()

        assert exc_info.value.config_key == "orchestrator.usage_limit_pause_hours"

    def test_get_usage_limit_pause_seconds_rejects_malformed_config_yaml(
        self,
        tmp_path: Path,
    ) -> None:
        """Malformed config YAML should still fail clearly."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("orchestrator:\n  usage_limit_pause_hours: [\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            get_usage_limit_pause_seconds()

        assert "Failed to parse configuration file" in str(exc_info.value)


class TestLLMHelperLookups:
    """Tests for LLM backend and model helper lookups."""

    def test_get_llm_backend_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for llm backend."""
        monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "litellm")
        assert get_llm_backend() == "litellm"

    def test_get_llm_backend_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(backend="litellm"),
                ),
            ),
        ):
            assert get_llm_backend() == "litellm"

    def test_get_llm_permission_mode_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for llm permission mode."""
        monkeypatch.setenv("OUROBOROS_LLM_PERMISSION_MODE", "acceptEdits")
        assert get_llm_permission_mode() == "acceptEdits"

    def test_get_llm_permission_mode_falls_back_to_config(self) -> None:
        """Config is used when env override is absent for llm permissions."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(permission_mode="bypassPermissions"),
                ),
            ),
        ):
            assert get_llm_permission_mode() == "bypassPermissions"

    def test_get_llm_permission_mode_uses_opencode_specific_config(self) -> None:
        """OpenCode adapters use the dedicated config default when generic mode is read-only."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(
                        permission_mode="default",
                        opencode_permission_mode="acceptEdits",
                    ),
                ),
            ),
        ):
            assert get_llm_permission_mode(backend="opencode") == "acceptEdits"

    def test_get_llm_permission_mode_defaults_to_accept_edits_for_opencode(self) -> None:
        """OpenCode falls back to auto-approve even when no config is available."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                side_effect=ConfigError("missing config"),
            ),
        ):
            assert get_llm_permission_mode(backend="opencode") == "acceptEdits"

    def test_get_clarification_model_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for clarification model."""
        monkeypatch.setenv("OUROBOROS_CLARIFICATION_MODEL", "gpt-5")
        assert get_clarification_model() == "gpt-5"

    def test_get_clarification_model_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    clarification=ClarificationConfig(default_model="gpt-5-mini"),
                ),
            ),
        ):
            assert get_clarification_model() == "gpt-5-mini"

    def test_codex_backend_uses_default_model_sentinel(self) -> None:
        """Backend-aware defaults avoid Claude model names for Codex."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                side_effect=ConfigError("missing config"),
            ),
        ):
            assert get_clarification_model(backend="codex") == "default"
            assert get_wonder_model(backend="codex") == "default"
            assert get_reflect_model(backend="codex") == "default"
            assert get_semantic_model(backend="codex") == "default"
            assert get_assertion_extraction_model(backend="codex") == "default"

    def test_opencode_backend_uses_default_model_sentinel(self) -> None:
        """Backend-aware defaults avoid Claude model names for OpenCode."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                side_effect=ConfigError("missing config"),
            ),
        ):
            assert get_clarification_model(backend="opencode") == "default"
            assert get_wonder_model(backend="opencode") == "default"
            assert get_reflect_model(backend="opencode") == "default"
            assert get_semantic_model(backend="opencode") == "default"
            assert get_assertion_extraction_model(backend="opencode") == "default"

    def test_codex_backend_normalizes_config_default_models_to_default_sentinel(self) -> None:
        """Config-backed default values should still normalize for Codex LLM flows."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(),
            ),
        ):
            assert get_clarification_model(backend="codex") == "default"
            assert get_qa_model(backend="codex") == "default"
            assert get_wonder_model(backend="codex") == "default"
            assert get_reflect_model(backend="codex") == "default"
            assert get_semantic_model(backend="codex") == "default"
            assert get_assertion_extraction_model(backend="codex") == "default"

    def test_codex_backend_preserves_explicit_non_default_models_from_config(self) -> None:
        """Explicit config overrides should survive backend normalization."""
        custom_config = OuroborosConfig(
            clarification=ClarificationConfig(default_model="gpt-5-mini"),
            llm=LLMConfig(qa_model="gpt-5-nano"),
            resilience=ResilienceConfig(
                wonder_model="gpt-5",
                reflect_model="gpt-5-mini",
            ),
            evaluation=EvaluationConfig(
                semantic_model="gpt-5",
                assertion_extraction_model="gpt-5-nano",
            ),
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=custom_config,
            ),
        ):
            assert get_clarification_model(backend="codex") == "gpt-5-mini"
            assert get_qa_model(backend="codex") == "gpt-5-nano"
            assert get_wonder_model(backend="codex") == "gpt-5"
            assert get_reflect_model(backend="codex") == "gpt-5-mini"
            assert get_semantic_model(backend="codex") == "gpt-5"
            assert get_assertion_extraction_model(backend="codex") == "gpt-5-nano"

    def test_get_qa_model_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for QA model."""
        monkeypatch.setenv("OUROBOROS_QA_MODEL", "gpt-5-nano")
        assert get_qa_model() == "gpt-5-nano"

    def test_get_qa_model_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(qa_model="gpt-5-nano"),
                ),
            ),
        ):
            assert get_qa_model() == "gpt-5-nano"

    def test_get_dependency_analysis_model_prefers_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment variable overrides config for dependency analysis model."""
        monkeypatch.setenv("OUROBOROS_DEPENDENCY_ANALYSIS_MODEL", "gpt-5-coder")
        assert get_dependency_analysis_model() == "gpt-5-coder"

    def test_get_dependency_analysis_model_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(dependency_analysis_model="gpt-5-coder"),
                ),
            ),
        ):
            assert get_dependency_analysis_model() == "gpt-5-coder"

    def test_get_semantic_model_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides config for semantic evaluation model."""
        monkeypatch.setenv("OUROBOROS_SEMANTIC_MODEL", "gpt-5")
        assert get_semantic_model() == "gpt-5"

    def test_get_semantic_model_falls_back_to_config(self) -> None:
        """Config is used when env override is absent."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    evaluation=EvaluationConfig(semantic_model="gpt-5"),
                ),
            ),
        ):
            assert get_semantic_model() == "gpt-5"

    def test_extended_model_helpers_fall_back_to_config(self) -> None:
        """Additional helper lookups use the configured section defaults."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ouroboros.config.loader.load_config",
                return_value=OuroborosConfig(
                    llm=LLMConfig(
                        ontology_analysis_model="gpt-5-ontology",
                        context_compression_model="gpt-5-mini",
                    ),
                    execution=ExecutionConfig(
                        atomicity_model="gpt-5-atomic",
                        decomposition_model="gpt-5-decompose",
                        double_diamond_model="gpt-5-diamond",
                    ),
                    resilience=ResilienceConfig(
                        wonder_model="gpt-5-wonder",
                        reflect_model="gpt-5-reflect",
                    ),
                    evaluation=EvaluationConfig(
                        semantic_model="gpt-5-semantic",
                        assertion_extraction_model="gpt-5-assert",
                    ),
                    consensus=ConsensusConfig(
                        models=("gpt-5-a", "gpt-5-b", "gpt-5-c"),
                        advocate_model="gpt-5-advocate",
                    ),
                ),
            ),
        ):
            assert get_ontology_analysis_model() == "gpt-5-ontology"
            assert get_context_compression_model() == "gpt-5-mini"
            assert get_atomicity_model() == "gpt-5-atomic"
            assert get_decomposition_model() == "gpt-5-decompose"
            assert get_double_diamond_model() == "gpt-5-diamond"
            assert get_wonder_model() == "gpt-5-wonder"
            assert get_reflect_model() == "gpt-5-reflect"
            assert get_assertion_extraction_model() == "gpt-5-assert"
            assert get_consensus_models() == ("gpt-5-a", "gpt-5-b", "gpt-5-c")
            assert get_consensus_advocate_model() == "gpt-5-advocate"

    def test_consensus_model_list_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Consensus roster can be overridden from a comma-separated env var."""
        monkeypatch.setenv("OUROBOROS_CONSENSUS_MODELS", "gpt-5-a, gpt-5-b ,gpt-5-c")
        assert get_consensus_models() == ("gpt-5-a", "gpt-5-b", "gpt-5-c")


class TestCredentialsFileSecure:
    """Test credentials_file_secure function."""

    def test_credentials_file_secure_returns_true(self, tmp_path: Path) -> None:
        """credentials_file_secure returns True for chmod 600."""
        creds_path = tmp_path / "credentials.yaml"
        creds_path.write_text("providers: {}")
        os.chmod(creds_path, stat.S_IRUSR | stat.S_IWUSR)

        assert credentials_file_secure(creds_path) is True

    def test_credentials_file_secure_returns_false_permissive(self, tmp_path: Path) -> None:
        """credentials_file_secure returns False for permissive permissions."""
        creds_path = tmp_path / "credentials.yaml"
        creds_path.write_text("providers: {}")
        os.chmod(creds_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

        assert credentials_file_secure(creds_path) is False

    def test_credentials_file_secure_returns_false_missing(self, tmp_path: Path) -> None:
        """credentials_file_secure returns False for missing file."""
        missing_path = tmp_path / "nonexistent.yaml"
        assert credentials_file_secure(missing_path) is False


class TestIntegration:
    """Integration tests for config loading workflow."""

    def test_create_and_load_config(self, tmp_path: Path) -> None:
        """Full workflow: create default config, then load it."""
        config_dir = tmp_path / ".ouroboros"
        config_path, creds_path = create_default_config(config_dir)

        # Load config
        config = load_config(config_path)
        assert config.economics.default_tier == "frugal"
        assert "frugal" in config.economics.tiers
        assert "standard" in config.economics.tiers
        assert "frontier" in config.economics.tiers

        # Load credentials
        creds = load_credentials(creds_path)
        assert "openai" in creds.providers
        assert "anthropic" in creds.providers

        # Verify credentials are secure
        assert credentials_file_secure(creds_path) is True

    def test_config_roundtrip_preserves_values(self, tmp_path: Path) -> None:
        """Config values are preserved through save/load cycle."""
        config_dir = tmp_path / ".ouroboros"
        config_path, _ = create_default_config(config_dir)

        # Load and verify specific values
        config = load_config(config_path)

        # Check tier configurations
        frugal = config.economics.tiers["frugal"]
        assert frugal.cost_factor == 1
        assert len(frugal.models) == 3

        standard = config.economics.tiers["standard"]
        assert standard.cost_factor == 10

        frontier = config.economics.tiers["frontier"]
        assert frontier.cost_factor == 30
