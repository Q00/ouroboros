"""Configuration module for Ouroboros.

This module provides configuration loading, validation, and management
for the Ouroboros system. Configuration is stored in ~/.ouroboros/.

Main exports:
    OuroborosConfig: Main configuration model
    CredentialsConfig: Provider credentials model
    TierConfig: Tier configuration model
    load_config: Load config from YAML file
    load_credentials: Load credentials from YAML file
    create_default_config: Create default config files
    config_exists: Check if config files exist

Usage:
    from ouroboros.config import load_config, load_credentials

    config = load_config()
    credentials = load_credentials()

    # Access configuration
    default_tier = config.economics.default_tier
    api_key = credentials.providers["openai"].api_key
"""

from ouroboros.config.loader import (
    config_exists,
    create_default_config,
    credentials_file_secure,
    ensure_config_dir,
    load_config,
    load_credentials,
)
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

__all__ = [
    # Models
    "OuroborosConfig",
    "CredentialsConfig",
    "TierConfig",
    "ModelConfig",
    "ProviderCredentials",
    "EconomicsConfig",
    "ClarificationConfig",
    "ExecutionConfig",
    "ResilienceConfig",
    "EvaluationConfig",
    "ConsensusConfig",
    "PersistenceConfig",
    "DriftConfig",
    "LoggingConfig",
    # Loader functions
    "load_config",
    "load_credentials",
    "create_default_config",
    "ensure_config_dir",
    "config_exists",
    "credentials_file_secure",
    # Model helpers
    "get_config_dir",
    "get_default_config",
    "get_default_credentials",
]
