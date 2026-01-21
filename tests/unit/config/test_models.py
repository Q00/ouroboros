"""Unit tests for ouroboros.config.models module."""

from pydantic import ValidationError
import pytest

from ouroboros.config.models import (
    ClarificationConfig,
    ConsensusConfig,
    CredentialsConfig,
    DriftConfig,
    EconomicsConfig,
    EvaluationConfig,
    ExecutionConfig,
    LoggingConfig,
    ModelConfig,
    OuroborosConfig,
    PersistenceConfig,
    ProviderCredentials,
    ResilienceConfig,
    TierConfig,
    get_config_dir,
    get_default_config,
    get_default_credentials,
)


class TestModelConfig:
    """Test ModelConfig for LLM model configuration."""

    def test_model_config_creation(self) -> None:
        """ModelConfig stores provider and model."""
        config = ModelConfig(provider="openai", model="gpt-4o")
        assert config.provider == "openai"
        assert config.model == "gpt-4o"

    def test_model_config_is_frozen(self) -> None:
        """ModelConfig is immutable."""
        config = ModelConfig(provider="openai", model="gpt-4o")
        with pytest.raises(ValidationError):
            config.provider = "anthropic"  # type: ignore[misc]


class TestTierConfig:
    """Test TierConfig for cost tier configuration."""

    def test_tier_config_creation(self) -> None:
        """TierConfig stores all tier settings."""
        config = TierConfig(
            cost_factor=10,
            intelligence_range=(14, 16),
            models=[ModelConfig(provider="openai", model="gpt-4o")],
            use_cases=["logic_design"],
        )
        assert config.cost_factor == 10
        assert config.intelligence_range == (14, 16)
        assert len(config.models) == 1
        assert config.use_cases == ["logic_design"]

    def test_tier_config_defaults(self) -> None:
        """TierConfig has sensible defaults."""
        config = TierConfig(cost_factor=1)
        assert config.intelligence_range == (1, 20)
        assert config.models == []
        assert config.use_cases == []

    def test_tier_config_cost_factor_minimum(self) -> None:
        """TierConfig cost_factor must be >= 1."""
        with pytest.raises(ValidationError):
            TierConfig(cost_factor=0)

    def test_tier_config_intelligence_range_validation(self) -> None:
        """TierConfig validates intelligence range min <= max."""
        with pytest.raises(ValidationError) as exc_info:
            TierConfig(cost_factor=1, intelligence_range=(20, 10))
        assert "min" in str(exc_info.value).lower()


class TestProviderCredentials:
    """Test ProviderCredentials for API credentials."""

    def test_provider_credentials_creation(self) -> None:
        """ProviderCredentials stores API key and base URL."""
        creds = ProviderCredentials(
            api_key="sk-test123",
            base_url="https://api.openai.com/v1",
        )
        assert creds.api_key == "sk-test123"
        assert creds.base_url == "https://api.openai.com/v1"

    def test_provider_credentials_optional_base_url(self) -> None:
        """ProviderCredentials base_url is optional."""
        creds = ProviderCredentials(api_key="sk-test123")
        assert creds.api_key == "sk-test123"
        assert creds.base_url is None

    def test_provider_credentials_requires_api_key(self) -> None:
        """ProviderCredentials requires non-empty api_key."""
        with pytest.raises(ValidationError):
            ProviderCredentials(api_key="")


class TestCredentialsConfig:
    """Test CredentialsConfig for all provider credentials."""

    def test_credentials_config_creation(self) -> None:
        """CredentialsConfig stores provider credentials."""
        config = CredentialsConfig(
            providers={
                "openai": ProviderCredentials(api_key="sk-openai"),
                "anthropic": ProviderCredentials(api_key="sk-anthropic"),
            }
        )
        assert "openai" in config.providers
        assert config.providers["openai"].api_key == "sk-openai"

    def test_credentials_config_default_empty(self) -> None:
        """CredentialsConfig defaults to empty providers."""
        config = CredentialsConfig()
        assert config.providers == {}


class TestEconomicsConfig:
    """Test EconomicsConfig for economic model settings."""

    def test_economics_config_creation(self) -> None:
        """EconomicsConfig stores economic model settings."""
        config = EconomicsConfig(
            default_tier="standard",
            escalation_threshold=3,
            downgrade_success_streak=10,
        )
        assert config.default_tier == "standard"
        assert config.escalation_threshold == 3
        assert config.downgrade_success_streak == 10

    def test_economics_config_defaults(self) -> None:
        """EconomicsConfig has sensible defaults."""
        config = EconomicsConfig()
        assert config.default_tier == "frugal"
        assert config.escalation_threshold == 2
        assert config.downgrade_success_streak == 5

    def test_economics_config_tier_validation(self) -> None:
        """EconomicsConfig default_tier must be valid tier."""
        with pytest.raises(ValidationError):
            EconomicsConfig(default_tier="invalid")  # type: ignore[arg-type]


class TestClarificationConfig:
    """Test ClarificationConfig for Phase 0 settings."""

    def test_clarification_config_creation(self) -> None:
        """ClarificationConfig stores clarification settings."""
        config = ClarificationConfig(
            ambiguity_threshold=0.15,
            max_interview_rounds=15,
            model_tier="frontier",
        )
        assert config.ambiguity_threshold == 0.15
        assert config.max_interview_rounds == 15
        assert config.model_tier == "frontier"

    def test_clarification_config_defaults(self) -> None:
        """ClarificationConfig has sensible defaults."""
        config = ClarificationConfig()
        assert config.ambiguity_threshold == 0.2
        assert config.max_interview_rounds == 10
        assert config.model_tier == "standard"

    def test_clarification_ambiguity_threshold_bounds(self) -> None:
        """ClarificationConfig ambiguity_threshold must be in [0, 1]."""
        with pytest.raises(ValidationError):
            ClarificationConfig(ambiguity_threshold=-0.1)
        with pytest.raises(ValidationError):
            ClarificationConfig(ambiguity_threshold=1.5)


class TestExecutionConfig:
    """Test ExecutionConfig for Phase 2 settings."""

    def test_execution_config_creation(self) -> None:
        """ExecutionConfig stores execution settings."""
        config = ExecutionConfig(
            max_iterations_per_ac=20,
            retrospective_interval=5,
        )
        assert config.max_iterations_per_ac == 20
        assert config.retrospective_interval == 5

    def test_execution_config_defaults(self) -> None:
        """ExecutionConfig has sensible defaults."""
        config = ExecutionConfig()
        assert config.max_iterations_per_ac == 10
        assert config.retrospective_interval == 3


class TestResilienceConfig:
    """Test ResilienceConfig for Phase 3 settings."""

    def test_resilience_config_creation(self) -> None:
        """ResilienceConfig stores resilience settings."""
        config = ResilienceConfig(
            stagnation_enabled=False,
            lateral_thinking_enabled=True,
            lateral_model_tier="standard",
            lateral_temperature=0.9,
        )
        assert config.stagnation_enabled is False
        assert config.lateral_thinking_enabled is True
        assert config.lateral_model_tier == "standard"
        assert config.lateral_temperature == 0.9

    def test_resilience_config_defaults(self) -> None:
        """ResilienceConfig has sensible defaults."""
        config = ResilienceConfig()
        assert config.stagnation_enabled is True
        assert config.lateral_thinking_enabled is True
        assert config.lateral_model_tier == "frontier"
        assert config.lateral_temperature == 0.8

    def test_resilience_temperature_bounds(self) -> None:
        """ResilienceConfig lateral_temperature must be in [0, 2]."""
        with pytest.raises(ValidationError):
            ResilienceConfig(lateral_temperature=-0.1)
        with pytest.raises(ValidationError):
            ResilienceConfig(lateral_temperature=2.5)


class TestEvaluationConfig:
    """Test EvaluationConfig for Phase 4 settings."""

    def test_evaluation_config_creation(self) -> None:
        """EvaluationConfig stores evaluation settings."""
        config = EvaluationConfig(
            stage1_enabled=True,
            stage2_enabled=False,
            stage3_enabled=True,
            satisfaction_threshold=0.9,
            uncertainty_threshold=0.2,
        )
        assert config.stage1_enabled is True
        assert config.stage2_enabled is False
        assert config.stage3_enabled is True
        assert config.satisfaction_threshold == 0.9
        assert config.uncertainty_threshold == 0.2

    def test_evaluation_config_defaults(self) -> None:
        """EvaluationConfig has sensible defaults."""
        config = EvaluationConfig()
        assert config.stage1_enabled is True
        assert config.stage2_enabled is True
        assert config.stage3_enabled is True
        assert config.satisfaction_threshold == 0.8
        assert config.uncertainty_threshold == 0.3


class TestConsensusConfig:
    """Test ConsensusConfig for Phase 5 settings."""

    def test_consensus_config_creation(self) -> None:
        """ConsensusConfig stores consensus settings."""
        config = ConsensusConfig(
            min_models=5,
            threshold=0.8,
            diversity_required=False,
        )
        assert config.min_models == 5
        assert config.threshold == 0.8
        assert config.diversity_required is False

    def test_consensus_config_defaults(self) -> None:
        """ConsensusConfig has sensible defaults."""
        config = ConsensusConfig()
        assert config.min_models == 3
        assert config.threshold == 0.67
        assert config.diversity_required is True

    def test_consensus_min_models_minimum(self) -> None:
        """ConsensusConfig min_models must be >= 2."""
        with pytest.raises(ValidationError):
            ConsensusConfig(min_models=1)


class TestPersistenceConfig:
    """Test PersistenceConfig for storage settings."""

    def test_persistence_config_creation(self) -> None:
        """PersistenceConfig stores persistence settings."""
        config = PersistenceConfig(
            enabled=True,
            database_path="custom/path.db",
        )
        assert config.enabled is True
        assert config.database_path == "custom/path.db"

    def test_persistence_config_defaults(self) -> None:
        """PersistenceConfig has sensible defaults."""
        config = PersistenceConfig()
        assert config.enabled is True
        assert config.database_path == "data/ouroboros.db"


class TestDriftConfig:
    """Test DriftConfig for drift monitoring settings."""

    def test_drift_config_creation(self) -> None:
        """DriftConfig stores drift monitoring settings."""
        config = DriftConfig(
            warning_threshold=0.25,
            critical_threshold=0.6,
        )
        assert config.warning_threshold == 0.25
        assert config.critical_threshold == 0.6

    def test_drift_config_defaults(self) -> None:
        """DriftConfig has sensible defaults."""
        config = DriftConfig()
        assert config.warning_threshold == 0.3
        assert config.critical_threshold == 0.5

    def test_drift_critical_must_exceed_warning(self) -> None:
        """DriftConfig critical_threshold must be >= warning_threshold."""
        with pytest.raises(ValidationError):
            DriftConfig(warning_threshold=0.5, critical_threshold=0.3)


class TestLoggingConfig:
    """Test LoggingConfig for logging settings."""

    def test_logging_config_creation(self) -> None:
        """LoggingConfig stores logging settings."""
        config = LoggingConfig(
            level="debug",
            log_path="custom/logs/app.log",
            include_reasoning=False,
        )
        assert config.level == "debug"
        assert config.log_path == "custom/logs/app.log"
        assert config.include_reasoning is False

    def test_logging_config_defaults(self) -> None:
        """LoggingConfig has sensible defaults."""
        config = LoggingConfig()
        assert config.level == "info"
        assert config.log_path == "logs/ouroboros.log"
        assert config.include_reasoning is True

    def test_logging_level_validation(self) -> None:
        """LoggingConfig level must be valid log level."""
        with pytest.raises(ValidationError):
            LoggingConfig(level="invalid")  # type: ignore[arg-type]


class TestOuroborosConfig:
    """Test OuroborosConfig top-level configuration."""

    def test_ouroboros_config_creation(self) -> None:
        """OuroborosConfig stores all configuration sections."""
        config = OuroborosConfig(
            economics=EconomicsConfig(default_tier="standard"),
            clarification=ClarificationConfig(ambiguity_threshold=0.15),
        )
        assert config.economics.default_tier == "standard"
        assert config.clarification.ambiguity_threshold == 0.15

    def test_ouroboros_config_defaults(self) -> None:
        """OuroborosConfig has all default sections."""
        config = OuroborosConfig()
        assert config.economics is not None
        assert config.clarification is not None
        assert config.execution is not None
        assert config.resilience is not None
        assert config.evaluation is not None
        assert config.consensus is not None
        assert config.persistence is not None
        assert config.drift is not None
        assert config.logging is not None

    def test_ouroboros_config_is_frozen(self) -> None:
        """OuroborosConfig is immutable."""
        config = OuroborosConfig()
        with pytest.raises(ValidationError):
            config.economics = EconomicsConfig()  # type: ignore[misc]


class TestGetDefaultConfig:
    """Test get_default_config helper function."""

    def test_get_default_config_returns_config(self) -> None:
        """get_default_config returns OuroborosConfig instance."""
        config = get_default_config()
        assert isinstance(config, OuroborosConfig)

    def test_get_default_config_has_tiers(self) -> None:
        """get_default_config includes default tier configurations."""
        config = get_default_config()
        assert "frugal" in config.economics.tiers
        assert "standard" in config.economics.tiers
        assert "frontier" in config.economics.tiers

    def test_get_default_config_frugal_tier(self) -> None:
        """get_default_config frugal tier has cost_factor 1."""
        config = get_default_config()
        frugal = config.economics.tiers["frugal"]
        assert frugal.cost_factor == 1
        assert len(frugal.models) > 0

    def test_get_default_config_standard_tier(self) -> None:
        """get_default_config standard tier has cost_factor 10."""
        config = get_default_config()
        standard = config.economics.tiers["standard"]
        assert standard.cost_factor == 10
        assert len(standard.models) > 0

    def test_get_default_config_frontier_tier(self) -> None:
        """get_default_config frontier tier has cost_factor 30."""
        config = get_default_config()
        frontier = config.economics.tiers["frontier"]
        assert frontier.cost_factor == 30
        assert len(frontier.models) > 0


class TestGetDefaultCredentials:
    """Test get_default_credentials helper function."""

    def test_get_default_credentials_returns_config(self) -> None:
        """get_default_credentials returns CredentialsConfig instance."""
        creds = get_default_credentials()
        assert isinstance(creds, CredentialsConfig)

    def test_get_default_credentials_has_providers(self) -> None:
        """get_default_credentials includes common providers."""
        creds = get_default_credentials()
        assert "openrouter" in creds.providers
        assert "openai" in creds.providers
        assert "anthropic" in creds.providers
        assert "google" in creds.providers

    def test_get_default_credentials_has_placeholders(self) -> None:
        """get_default_credentials has placeholder API keys."""
        creds = get_default_credentials()
        # Placeholder keys should contain "YOUR_" prefix
        assert "YOUR_" in creds.providers["openai"].api_key

    def test_get_default_credentials_openrouter_has_base_url(self) -> None:
        """get_default_credentials openrouter has base_url set."""
        creds = get_default_credentials()
        assert creds.providers["openrouter"].base_url is not None
        assert "openrouter" in creds.providers["openrouter"].base_url


class TestGetConfigDir:
    """Test get_config_dir helper function."""

    def test_get_config_dir_returns_path(self) -> None:
        """get_config_dir returns a Path object."""
        from pathlib import Path

        config_dir = get_config_dir()
        assert isinstance(config_dir, Path)

    def test_get_config_dir_is_in_home(self) -> None:
        """get_config_dir returns path in home directory."""
        from pathlib import Path

        config_dir = get_config_dir()
        assert config_dir.parent == Path.home()
        assert config_dir.name == ".ouroboros"
