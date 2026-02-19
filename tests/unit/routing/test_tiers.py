"""Unit tests for tier configuration and model selection.

Tests cover:
- Tier enum and cost multipliers
- Model selection from tiers
- Configuration validation
- Error handling for invalid configurations
"""

from ouroboros.config.models import (
    EconomicsConfig,
    ModelConfig,
    OuroborosConfig,
    TierConfig,
)
from ouroboros.core.errors import ConfigError
from ouroboros.routing.tiers import (
    Tier,
    get_model_for_tier,
    get_tier_config,
    validate_tier_configuration,
)


class TestTierEnum:
    """Test the Tier enumeration."""

    def test_tier_values(self) -> None:
        """Test that tier enum has correct string values."""
        assert Tier.FRUGAL.value == "frugal"
        assert Tier.STANDARD.value == "standard"
        assert Tier.FRONTIER.value == "frontier"

    def test_tier_cost_multipliers(self) -> None:
        """Test that tiers have correct cost multipliers."""
        assert Tier.FRUGAL.cost_multiplier == 1
        assert Tier.STANDARD.cost_multiplier == 10
        assert Tier.FRONTIER.cost_multiplier == 30

    def test_tier_iteration(self) -> None:
        """Test that all three tiers are present."""
        tiers = list(Tier)
        assert len(tiers) == 3
        assert Tier.FRUGAL in tiers
        assert Tier.STANDARD in tiers
        assert Tier.FRONTIER in tiers


class TestGetTierConfig:
    """Test get_tier_config function."""

    def test_get_valid_tier_config(self) -> None:
        """Test getting configuration for a valid tier."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                },
            ),
        )

        result = get_tier_config(Tier.FRUGAL, config)

        assert result.is_ok
        tier_config = result.value
        assert tier_config.cost_factor == 1
        assert len(tier_config.models) == 1
        assert tier_config.models[0].provider == "openai"
        assert tier_config.models[0].model == "gpt-4o-mini"

    def test_get_missing_tier(self) -> None:
        """Test error when tier is not in configuration."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={},
            ),
        )

        result = get_tier_config(Tier.FRUGAL, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "frugal" in error.message.lower()
        assert "not found" in error.message.lower()
        assert error.config_key == "economics.tiers.frugal"
        assert "available_tiers" in error.details
        assert "requested_tier" in error.details

    def test_get_tier_with_no_models(self) -> None:
        """Test error when tier has no models configured."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[],
                    ),
                },
            ),
        )

        result = get_tier_config(Tier.FRUGAL, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "no models" in error.message.lower()
        assert error.config_key == "economics.tiers.frugal.models"
        assert error.details["tier"] == "frugal"
        assert error.details["cost_factor"] == 1

    def test_get_tier_with_invalid_cost_factor(self) -> None:
        """Test error when tier has incorrect cost factor."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=5,  # Should be 1
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                },
            ),
        )

        result = get_tier_config(Tier.FRUGAL, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "invalid cost factor" in error.message.lower()
        assert error.config_key == "economics.tiers.frugal.cost_factor"
        assert error.details["expected_cost_factor"] == 1
        assert error.details["actual_cost_factor"] == 5

    def test_get_all_tiers(self) -> None:
        """Test getting configuration for all three tiers."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        # Test each tier
        for tier in Tier:
            result = get_tier_config(tier, config)
            assert result.is_ok
            tier_config = result.value
            assert tier_config.cost_factor == tier.cost_multiplier


class TestGetModelForTier:
    """Test get_model_for_tier function."""

    def test_get_model_from_single_model_tier(self) -> None:
        """Test selecting model when tier has one model."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                },
            ),
        )

        result = get_model_for_tier(Tier.FRUGAL, config)

        assert result.is_ok
        model = result.value
        assert model.provider == "openai"
        assert model.model == "gpt-4o-mini"

    def test_get_model_from_multi_model_tier(self) -> None:
        """Test selecting model when tier has multiple models."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                            ModelConfig(provider="anthropic", model="claude-sonnet-4"),
                            ModelConfig(provider="google", model="gemini-2.5-pro"),
                        ],
                    ),
                },
            ),
        )

        # Get model multiple times to test randomness
        models_selected = []
        for _ in range(10):
            result = get_model_for_tier(Tier.STANDARD, config)
            assert result.is_ok
            model = result.value
            models_selected.append(f"{model.provider}/{model.model}")

        # Verify all selected models are from the tier
        valid_models = {
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.5-pro",
        }
        for model_str in models_selected:
            assert model_str in valid_models

    def test_get_model_from_missing_tier(self) -> None:
        """Test error when tier is missing from configuration."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={},
            ),
        )

        result = get_model_for_tier(Tier.FRUGAL, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "not found" in error.message.lower()

    def test_get_model_from_tier_with_no_models(self) -> None:
        """Test error when tier has no models."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[],
                    ),
                },
            ),
        )

        result = get_model_for_tier(Tier.FRUGAL, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "no models" in error.message.lower()

    def test_get_model_from_tier_with_invalid_cost_factor(self) -> None:
        """Test error when tier has invalid cost factor."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "standard": TierConfig(
                        cost_factor=15,  # Should be 10
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                },
            ),
        )

        result = get_model_for_tier(Tier.STANDARD, config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "invalid cost factor" in error.message.lower()


class TestValidateTierConfiguration:
    """Test validate_tier_configuration function."""

    def test_validate_complete_configuration(self) -> None:
        """Test validation passes with all tiers configured."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        result = validate_tier_configuration(config)

        assert result.is_ok

    def test_validate_missing_tier(self) -> None:
        """Test validation fails when a tier is missing."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    # Missing standard and frontier
                },
            ),
        )

        result = validate_tier_configuration(config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "validation failed" in error.message.lower()
        assert "errors" in error.details
        assert len(error.details["errors"]) == 2  # Missing standard and frontier

    def test_validate_tier_with_no_models(self) -> None:
        """Test validation fails when a tier has no models."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[],  # No models
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        result = validate_tier_configuration(config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert "validation failed" in error.message.lower()
        assert len(error.details["errors"]) == 1

    def test_validate_tier_with_invalid_cost_factors(self) -> None:
        """Test validation fails when cost factors are incorrect."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=2,  # Should be 1
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=25,  # Should be 30
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        result = validate_tier_configuration(config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert len(error.details["errors"]) == 2  # Two invalid cost factors

    def test_validate_empty_configuration(self) -> None:
        """Test validation fails with empty tier configuration."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={},
            ),
        )

        result = validate_tier_configuration(config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ConfigError)
        assert len(error.details["errors"]) == 3  # All three tiers missing


class TestIntegrationScenarios:
    """Test integration scenarios combining multiple functions."""

    def test_default_tier_workflow(self) -> None:
        """Test using default tier from configuration."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                default_tier="frugal",
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        # Use default tier from config
        default_tier = Tier(config.economics.default_tier)
        assert default_tier == Tier.FRUGAL

        # Get model for default tier
        result = get_model_for_tier(default_tier, config)
        assert result.is_ok
        model = result.value
        assert model.model == "gpt-4o-mini"

    def test_tier_escalation_workflow(self) -> None:
        """Test escalating from frugal to standard to frontier."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        # Start with frugal
        tier = Tier.FRUGAL
        result = get_model_for_tier(tier, config)
        assert result.is_ok
        assert result.value.model == "gpt-4o-mini"

        # Escalate to standard
        tier = Tier.STANDARD
        result = get_model_for_tier(tier, config)
        assert result.is_ok
        assert result.value.model == "gpt-4o"

        # Escalate to frontier
        tier = Tier.FRONTIER
        result = get_model_for_tier(tier, config)
        assert result.is_ok
        assert result.value.model == "o3"

    def test_cost_tracking_workflow(self) -> None:
        """Test tracking cost multipliers for different tiers."""
        config = OuroborosConfig(
            economics=EconomicsConfig(
                tiers={
                    "frugal": TierConfig(
                        cost_factor=1,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o-mini"),
                        ],
                    ),
                    "standard": TierConfig(
                        cost_factor=10,
                        models=[
                            ModelConfig(provider="openai", model="gpt-4o"),
                        ],
                    ),
                    "frontier": TierConfig(
                        cost_factor=30,
                        models=[
                            ModelConfig(provider="openai", model="o3"),
                        ],
                    ),
                },
            ),
        )

        # Track costs for each tier
        costs = {}
        for tier in Tier:
            tier_result = get_tier_config(tier, config)
            assert tier_result.is_ok
            costs[tier.value] = tier_result.value.cost_factor

        assert costs["frugal"] == 1
        assert costs["standard"] == 10
        assert costs["frontier"] == 30
