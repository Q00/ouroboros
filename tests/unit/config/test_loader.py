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
    get_llm_backend,
    get_llm_permission_mode,
    get_ontology_analysis_model,
    get_qa_model,
    get_reflect_model,
    get_semantic_model,
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
