"""Three-tier model configuration for Ouroboros.

This module implements the three-tier Progressive Adaptive LLM (PAL) routing system:
- Frugal (1x cost): Fast, cheap models for routine tasks
- Standard (10x cost): Balanced models for most work
- Frontier (30x cost): Most capable models for complex tasks

The tier system enables cost optimization by routing tasks to the appropriate
model based on complexity and context.

Usage:
    from ouroboros.routing.tiers import Tier, get_model_for_tier, get_tier_config

    # Get model for a tier
    result = get_model_for_tier(Tier.FRUGAL, config)
    if result.is_ok:
        model = result.value
        print(f"Using model: {model.provider}/{model.model}")

    # Get tier configuration
    result = get_tier_config(Tier.STANDARD, config)
    if result.is_ok:
        tier_config = result.value
        print(f"Cost factor: {tier_config.cost_factor}x")
"""

from enum import Enum
import random

from ouroboros.config.models import ModelConfig, OuroborosConfig, TierConfig
from ouroboros.core.errors import ConfigError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


class Tier(str, Enum):
    """Model tier enumeration.

    Three tiers with different cost/capability tradeoffs:
    - FRUGAL: 1x cost, fastest and cheapest
    - STANDARD: 10x cost, balanced performance
    - FRONTIER: 30x cost, highest capability
    """

    FRUGAL = "frugal"
    STANDARD = "standard"
    FRONTIER = "frontier"

    @property
    def cost_multiplier(self) -> int:
        """Get the cost multiplier for this tier.

        Returns:
            Cost multiplier (1, 10, or 30).
        """
        multipliers = {
            Tier.FRUGAL: 1,
            Tier.STANDARD: 10,
            Tier.FRONTIER: 30,
        }
        return multipliers[self]


def get_tier_config(
    tier: Tier,
    config: OuroborosConfig,
) -> Result[TierConfig, ConfigError]:
    """Get configuration for a specific tier.

    Args:
        tier: The tier to get configuration for.
        config: The Ouroboros configuration.

    Returns:
        Result containing TierConfig on success or ConfigError on failure.

    Example:
        result = get_tier_config(Tier.FRUGAL, config)
        if result.is_ok:
            tier_config = result.value
            print(f"Cost factor: {tier_config.cost_factor}x")
        else:
            print(f"Error: {result.error}")
    """
    tier_name = tier.value

    # Check if tier exists in configuration
    if tier_name not in config.economics.tiers:
        error = ConfigError(
            f"Tier '{tier_name}' not found in configuration",
            config_key=f"economics.tiers.{tier_name}",
            details={
                "available_tiers": list(config.economics.tiers.keys()),
                "requested_tier": tier_name,
            },
        )
        log.error(
            "tier.config.missing",
            tier=tier_name,
            available_tiers=list(config.economics.tiers.keys()),
        )
        return Result.err(error)

    tier_config = config.economics.tiers[tier_name]

    # Validate tier has models configured
    if not tier_config.models:
        error = ConfigError(
            f"Tier '{tier_name}' has no models configured",
            config_key=f"economics.tiers.{tier_name}.models",
            details={
                "tier": tier_name,
                "cost_factor": tier_config.cost_factor,
            },
        )
        log.error(
            "tier.config.no_models",
            tier=tier_name,
            cost_factor=tier_config.cost_factor,
        )
        return Result.err(error)

    # Validate cost factor matches expected multiplier
    expected_multiplier = tier.cost_multiplier
    if tier_config.cost_factor != expected_multiplier:
        error = ConfigError(
            f"Tier '{tier_name}' has invalid cost factor: "
            f"expected {expected_multiplier}, got {tier_config.cost_factor}",
            config_key=f"economics.tiers.{tier_name}.cost_factor",
            details={
                "tier": tier_name,
                "expected_cost_factor": expected_multiplier,
                "actual_cost_factor": tier_config.cost_factor,
            },
        )
        log.error(
            "tier.config.invalid_cost_factor",
            tier=tier_name,
            expected=expected_multiplier,
            actual=tier_config.cost_factor,
        )
        return Result.err(error)

    log.debug(
        "tier.config.retrieved",
        tier=tier_name,
        cost_factor=tier_config.cost_factor,
        model_count=len(tier_config.models),
    )

    return Result.ok(tier_config)


def get_model_for_tier(
    tier: Tier,
    config: OuroborosConfig,
) -> Result[ModelConfig, ConfigError]:
    """Get a model for the specified tier.

    Selects a random model from the tier's configured models to enable
    load balancing and provider diversity.

    Args:
        tier: The tier to get a model for.
        config: The Ouroboros configuration.

    Returns:
        Result containing ModelConfig on success or ConfigError on failure.

    Example:
        result = get_model_for_tier(Tier.STANDARD, config)
        if result.is_ok:
            model = result.value
            print(f"Selected: {model.provider}/{model.model}")
        else:
            print(f"Error: {result.error}")
    """
    # Get tier configuration
    tier_result = get_tier_config(tier, config)
    if tier_result.is_err:
        return Result.err(tier_result.error)

    tier_config = tier_result.value

    # Select random model from tier (for load balancing)
    model = random.choice(tier_config.models)

    log.info(
        "tier.model.selected",
        tier=tier.value,
        provider=model.provider,
        model=model.model,
        cost_factor=tier_config.cost_factor,
    )

    return Result.ok(model)


def validate_tier_configuration(
    config: OuroborosConfig,
) -> Result[None, ConfigError]:
    """Validate that all three tiers are properly configured.

    Checks:
    - All three tiers (frugal, standard, frontier) exist
    - Each tier has at least one model
    - Cost factors match expected values (1x, 10x, 30x)

    Args:
        config: The Ouroboros configuration to validate.

    Returns:
        Result containing None on success or ConfigError on failure.

    Example:
        result = validate_tier_configuration(config)
        if result.is_err:
            print(f"Configuration error: {result.error}")
    """
    errors = []

    # Validate all three tiers exist and are properly configured
    for tier in Tier:
        result = get_tier_config(tier, config)
        if result.is_err:
            errors.append(str(result.error))

    if errors:
        error = ConfigError(
            "Tier configuration validation failed",
            config_key="economics.tiers",
            details={
                "errors": errors,
                "required_tiers": [t.value for t in Tier],
            },
        )
        log.error(
            "tier.validation.failed",
            error_count=len(errors),
            errors=errors,
        )
        return Result.err(error)

    log.info("tier.validation.passed", tier_count=len(Tier))
    return Result.ok(None)
