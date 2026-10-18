"""Every previously shipped Anthropic pin retains its migration provenance."""

import os
from unittest.mock import patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.config._model_defaults import recognized_shipped_defaults
from ouroboros.config.loader import get_consensus_models
from ouroboros.config.model_selection import resolve_role_model
from ouroboros.config.models import EconomicsConfig, ModelConfig, OuroborosConfig, TierConfig
from ouroboros.orchestrator.model_routing import configured_tier_models

HISTORICAL = [
    ("frugal", "claude-3-5-haiku", "claude-haiku-5-5"),
    ("frugal", "claude-haiku-4-5", "claude-haiku-5-5"),
    ("standard", "claude-sonnet-4-20250514", "claude-sonnet-5-5"),
    ("standard", "claude-sonnet-4-6", "claude-sonnet-5-5"),
    ("frontier", "claude-opus-4-5-20251101", "claude-opus-5-5"),
    ("frontier", "claude-opus-4-6", "claude-opus-5-5"),
    ("frontier", "claude-opus-4-8", "claude-opus-5-5"),
    ("frontier", "claude-opus-5", "claude-opus-5-5"),
]


@pytest.mark.parametrize(("tier", "old", "current"), HISTORICAL)
@pytest.mark.parametrize("provider", ["anthropic", "claude", "openai"])
def test_historical_economics_refresh_only_under_original_provider(
    tier: str, old: str, current: str, provider: str
) -> None:
    economics = EconomicsConfig(
        tiers={tier: TierConfig(cost_factor=3, models=[ModelConfig(provider=provider, model=old)])}
    )
    backend = "codex_cli" if provider == "openai" else "claude"
    assert configured_tier_models(economics, runtime_backend=backend) == {
        tier: current if provider == "anthropic" else old
    }


@pytest.mark.parametrize(("old", "current"), [(old, current) for _, old, current in HISTORICAL])
def test_prior_role_default_chain_remains_recognized(old: str, current: str) -> None:
    # Economics shipped Haiku 3.5 and the dated Opus 4.5, but these two were
    # never role defaults. Recognition must not swallow explicit role pins.
    expected = old not in {"claude-3-5-haiku", "claude-opus-4-5-20251101"}
    assert (old in recognized_shipped_defaults(current)) is expected


@pytest.mark.parametrize("old", ["claude-sonnet-4-20250514", "claude-sonnet-4-6"])
def test_persisted_role_default_refreshes_but_invocation_pin_stays_literal(old: str) -> None:
    config = OuroborosConfig(llm={"qa_model": old})
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("ouroboros.config.loader.load_config", return_value=config),
    ):
        assert resolve_role_model("qa", backend="litellm").model == "claude-sonnet-5-5"
        assert resolve_role_model("qa", backend="litellm", invocation_model=old).model == old


def test_new_economics_and_custom_values_keep_existing_shape_and_policy() -> None:
    config = get_default_config()
    assert configured_tier_models(config.economics, runtime_backend="claude") == {
        "frugal": "claude-haiku-5-5",
        "standard": "claude-sonnet-5-5",
        "frontier": "claude-opus-5-5",
    }
    economics = EconomicsConfig(
        tiers={
            "standard": TierConfig(
                cost_factor=7,
                models=[ModelConfig(provider="anthropic", model="claude-custom-proxy")],
            )
        }
    )
    assert configured_tier_models(economics, runtime_backend="claude") == {
        "standard": "claude-custom-proxy"
    }
    assert economics.tiers["standard"].cost_factor == 7


@pytest.mark.parametrize(
    "old",
    [
        "openrouter/anthropic/claude-opus-5",
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/anthropic/claude-opus-4-6",
    ],
)
def test_prior_consensus_rosters_refresh_only_the_anthropic_slot(old: str) -> None:
    config = OuroborosConfig(
        consensus={"models": ["openrouter/openai/gpt-4o", old, "openrouter/google/gemini-2.5-pro"]}
    )
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("ouroboros.config.loader.load_config", return_value=config),
    ):
        assert get_consensus_models(backend="litellm") == (
            "openrouter/openai/gpt-4o",
            "openrouter/anthropic/claude-opus-5.5",
            "openrouter/google/gemini-2.5-pro",
        )
