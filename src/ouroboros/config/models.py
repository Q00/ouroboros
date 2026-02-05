"""Pydantic models for Ouroboros configuration.

This module defines the configuration schema using Pydantic v2.
All configuration validation happens through these models.

Classes:
    ModelConfig: Single LLM model configuration
    TierConfig: Tier configuration with cost factor and models
    ProviderCredentials: API credentials for a single provider
    CredentialsConfig: All provider credentials
    EconomicsConfig: Economic model with tier definitions
    ClarificationConfig: Phase 0 configuration
    ExecutionConfig: Phase 2 configuration
    ResilienceConfig: Phase 3 configuration
    EvaluationConfig: Phase 4 configuration
    ConsensusConfig: Phase 5 configuration
    PersistenceConfig: Storage configuration
    LoggingConfig: Logging configuration
    OuroborosConfig: Top-level configuration combining all sections
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ModelConfig(BaseModel, frozen=True):
    """Configuration for a single LLM model.

    Attributes:
        provider: Provider name (openai, anthropic, google, openrouter)
        model: Model identifier string
    """

    provider: str
    model: str


class TierConfig(BaseModel, frozen=True):
    """Configuration for a cost tier.

    Attributes:
        cost_factor: Relative cost multiplier (1 for frugal, 10 for standard, etc.)
        intelligence_range: Tuple of min/max intelligence score
        models: List of models available in this tier
        use_cases: List of use cases this tier is suited for
    """

    cost_factor: int = Field(ge=1)
    intelligence_range: tuple[int, int] = Field(default=(1, 20))
    models: list[ModelConfig] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)

    @field_validator("intelligence_range")
    @classmethod
    def validate_intelligence_range(cls, v: tuple[int, int]) -> tuple[int, int]:
        """Validate that min <= max in intelligence range."""
        if v[0] > v[1]:
            msg = f"Intelligence range min ({v[0]}) must be <= max ({v[1]})"
            raise ValueError(msg)
        return v


class ProviderCredentials(BaseModel, frozen=True):
    """API credentials for a single provider.

    Attributes:
        api_key: The API key for the provider
        base_url: Optional custom base URL for the provider
    """

    api_key: str = Field(min_length=1)
    base_url: str | None = None


class CredentialsConfig(BaseModel, frozen=True):
    """Configuration for all provider credentials.

    Attributes:
        providers: Dict mapping provider name to credentials
    """

    providers: dict[str, ProviderCredentials] = Field(default_factory=dict)


class EconomicsConfig(BaseModel, frozen=True):
    """Economic model configuration.

    Attributes:
        default_tier: Default tier to use for tasks
        tiers: Dict mapping tier name to tier configuration
        escalation_threshold: Number of failures before upgrading tier
        downgrade_success_streak: Successes needed to downgrade tier
    """

    default_tier: Literal["frugal", "standard", "frontier"] = "frugal"
    tiers: dict[str, TierConfig] = Field(default_factory=dict)
    escalation_threshold: int = Field(default=2, ge=1)
    downgrade_success_streak: int = Field(default=5, ge=1)


class ClarificationConfig(BaseModel, frozen=True):
    """Phase 0 (Big Bang) configuration.

    Attributes:
        ambiguity_threshold: Maximum ambiguity score to proceed
        max_interview_rounds: Maximum number of clarification rounds
        model_tier: Tier to use for clarification
        default_model: Default LLM model for interview and seed generation
    """

    ambiguity_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    max_interview_rounds: int = Field(default=10, ge=1)
    model_tier: Literal["frugal", "standard", "frontier"] = "standard"
    default_model: str = "openrouter/google/gemini-2.0-flash-001"


class ExecutionConfig(BaseModel, frozen=True):
    """Phase 2 (Execution) configuration.

    Attributes:
        max_iterations_per_ac: Maximum iterations per acceptance criteria
        retrospective_interval: Iterations between retrospectives
    """

    max_iterations_per_ac: int = Field(default=10, ge=1)
    retrospective_interval: int = Field(default=3, ge=1)


class ResilienceConfig(BaseModel, frozen=True):
    """Phase 3 (Resilience) configuration.

    Attributes:
        stagnation_enabled: Whether stagnation detection is enabled
        lateral_thinking_enabled: Whether lateral thinking is enabled
        lateral_model_tier: Tier for lateral thinking
        lateral_temperature: Temperature for lateral thinking LLM calls
    """

    stagnation_enabled: bool = True
    lateral_thinking_enabled: bool = True
    lateral_model_tier: Literal["frugal", "standard", "frontier"] = "frontier"
    lateral_temperature: float = Field(default=0.8, ge=0.0, le=2.0)


class EvaluationConfig(BaseModel, frozen=True):
    """Phase 4 (Evaluation) configuration.

    Attributes:
        stage1_enabled: Whether mechanical checks are enabled
        stage2_enabled: Whether semantic evaluation is enabled
        stage3_enabled: Whether consensus evaluation is enabled
        satisfaction_threshold: Minimum satisfaction score
        uncertainty_threshold: Threshold above which to trigger consensus
    """

    stage1_enabled: bool = True
    stage2_enabled: bool = True
    stage3_enabled: bool = True
    satisfaction_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    uncertainty_threshold: float = Field(default=0.3, ge=0.0, le=1.0)


class ConsensusConfig(BaseModel, frozen=True):
    """Phase 5 (Consensus) configuration.

    Attributes:
        min_models: Minimum number of models for consensus
        threshold: Agreement threshold for consensus
        diversity_required: Whether different providers are required
    """

    min_models: int = Field(default=3, ge=2)
    threshold: float = Field(default=0.67, ge=0.0, le=1.0)
    diversity_required: bool = True


class PersistenceConfig(BaseModel, frozen=True):
    """Persistence configuration.

    Attributes:
        enabled: Whether persistence is enabled
        database_path: Path to SQLite database (relative to config dir)
    """

    enabled: bool = True
    database_path: str = "data/ouroboros.db"


class DriftConfig(BaseModel, frozen=True):
    """Drift monitoring configuration.

    Attributes:
        warning_threshold: Drift score threshold for warnings
        critical_threshold: Drift score threshold for intervention
    """

    warning_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    critical_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("critical_threshold")
    @classmethod
    def validate_critical_threshold(cls, v: float, info: object) -> float:
        """Validate that critical threshold >= warning threshold."""
        data = getattr(info, "data", {})
        warning = data.get("warning_threshold", 0.3)
        if v < warning:
            msg = f"critical_threshold ({v}) must be >= warning_threshold ({warning})"
            raise ValueError(msg)
        return v


class LoggingConfig(BaseModel, frozen=True):
    """Logging configuration.

    Attributes:
        level: Log level (debug, info, warning, error)
        log_path: Path to log file (relative to config dir)
        include_reasoning: Whether to log LLM reasoning
    """

    level: Literal["debug", "info", "warning", "error"] = "info"
    log_path: str = "logs/ouroboros.log"
    include_reasoning: bool = True


class OrchestratorConfig(BaseModel, frozen=True):
    """Orchestrator configuration for Claude Agent SDK.

    Attributes:
        cli_path: Path to Claude CLI binary. Supports:
            - Absolute path: /path/to/my-claude-wrapper
            - ~ expansion: ~/.my-claude-wrapper/bin/my-claude-wrapper
            - None: Use SDK bundled CLI
        default_max_turns: Default max turns for agent execution
    """

    cli_path: str | None = None
    default_max_turns: int = Field(default=10, ge=1)

    @field_validator("cli_path")
    @classmethod
    def expand_cli_path(cls, v: str | None) -> str | None:
        """Expand ~ in cli_path."""
        if v is None:
            return None
        return str(Path(v).expanduser())


class OuroborosConfig(BaseModel, frozen=True):
    """Top-level Ouroboros configuration.

    This is the main configuration model that combines all section configs.
    It validates against config.yaml in ~/.ouroboros/.

    Attributes:
        economics: Economic model and tier configuration
        clarification: Phase 0 (Big Bang) configuration
        execution: Phase 2 configuration
        resilience: Phase 3 configuration
        evaluation: Phase 4 configuration
        consensus: Phase 5 configuration
        persistence: Storage configuration
        drift: Drift monitoring configuration
        logging: Logging configuration
    """

    economics: EconomicsConfig = Field(default_factory=EconomicsConfig)
    clarification: ClarificationConfig = Field(default_factory=ClarificationConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)


def get_default_config() -> OuroborosConfig:
    """Get the default Ouroboros configuration.

    Returns:
        OuroborosConfig with all default values populated.
    """
    return OuroborosConfig(
        economics=EconomicsConfig(
            default_tier="frugal",
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    intelligence_range=(9, 11),
                    models=[
                        ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ModelConfig(provider="google", model="gemini-2.0-flash"),
                        ModelConfig(provider="anthropic", model="claude-3-5-haiku"),
                    ],
                    use_cases=["routine_coding", "log_analysis", "stage1_fix"],
                ),
                "standard": TierConfig(
                    cost_factor=10,
                    intelligence_range=(14, 16),
                    models=[
                        ModelConfig(provider="openai", model="gpt-4o"),
                        ModelConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
                        ModelConfig(provider="google", model="gemini-2.5-pro"),
                    ],
                    use_cases=["logic_design", "stage2_evaluation", "refactoring"],
                ),
                "frontier": TierConfig(
                    cost_factor=30,
                    intelligence_range=(18, 20),
                    models=[
                        ModelConfig(provider="openai", model="o3"),
                        ModelConfig(provider="anthropic", model="claude-opus-4-5-20251101"),
                    ],
                    use_cases=["consensus", "lateral_thinking", "big_bang"],
                ),
            },
            escalation_threshold=2,
            downgrade_success_streak=5,
        ),
    )


def get_default_credentials() -> CredentialsConfig:
    """Get the default credentials configuration template.

    Returns:
        CredentialsConfig with placeholder providers.

    Note:
        The returned credentials have empty API keys and should be
        filled in by the user.
    """
    return CredentialsConfig(
        providers={
            "openrouter": ProviderCredentials(
                api_key="YOUR_OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
            ),
            "openai": ProviderCredentials(api_key="YOUR_OPENAI_API_KEY"),
            "anthropic": ProviderCredentials(api_key="YOUR_ANTHROPIC_API_KEY"),
            "google": ProviderCredentials(api_key="YOUR_GOOGLE_API_KEY"),
        }
    )


def get_config_dir() -> Path:
    """Get the Ouroboros configuration directory path.

    Returns:
        Path to ~/.ouroboros/
    """
    return Path.home() / ".ouroboros"
