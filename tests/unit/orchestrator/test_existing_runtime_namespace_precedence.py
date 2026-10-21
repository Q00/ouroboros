"""Shared family namespaces apply to both CLI and MCP routing backends."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.config.model_selection import CLAUDE_TIER_ALIASES, resolve_role_model
from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.orchestrator.model_routing import (
    MODEL_TIER_LADDER,
    build_model_router,
    configured_tier_models,
    tier_model_providers,
)
from ouroboros.orchestrator.route_compat import _configured_models


@pytest.mark.parametrize(
    ("runtime", "family", "vendor"),
    [
        ("claude", "claude", "anthropic"),
        ("claude_code", "claude", "anthropic"),
        ("claude_mcp", "claude", "anthropic"),
        ("codex_cli", "codex", "openai"),
        ("codex_mcp", "codex", "openai"),
        ("gemini_cli", "gemini", "google"),
    ],
)
def test_existing_runtime_namespace_order(runtime: str, family: str, vendor: str) -> None:
    expected = (runtime, vendor) if runtime == family else (runtime, family, vendor)
    assert tier_model_providers(runtime) == expected


@pytest.mark.parametrize(
    ("runtime", "family", "vendor"),
    [("codex_mcp", "codex", "openai"), ("claude_mcp", "claude", "anthropic")],
)
@pytest.mark.parametrize("catalog", ["runtime_exact", "family_only", "family_and_vendor"])
def test_mcp_family_ladders_agree_across_routing_paths(
    runtime: str, family: str, vendor: str, catalog: str
) -> None:
    # List the least preferred namespace first, so model-list order cannot
    # accidentally satisfy runtime > family > vendor precedence.
    providers = {
        "runtime_exact": [vendor, family, runtime],
        "family_only": [family],
        "family_and_vendor": [vendor, family],
    }[catalog]
    selected = runtime if catalog == "runtime_exact" else family
    expected = {tier: f"proxy/{selected}/{tier}" for tier in MODEL_TIER_LADDER}
    economics = EconomicsConfig(
        tiers={
            tier: TierConfig(
                cost_factor=1,
                models=[
                    ModelConfig(provider=provider, model=f"proxy/{provider}/{tier}")
                    for provider in providers
                ],
            )
            for tier in MODEL_TIER_LADDER
        }
    )
    config = get_default_config().model_copy(update={"economics": economics})

    assert configured_tier_models(economics, runtime_backend=runtime) == expected
    router = build_model_router(economics, runtime_backend=runtime)
    assert router is not None
    assert dict(router.tier_models) == expected
    assert _configured_models(economics, runtime_backend=runtime) == expected

    with patch("ouroboros.config.model_selection._load_config", return_value=config):
        for tier in MODEL_TIER_LADDER:
            resolved = resolve_role_model("execute", backend=runtime, invocation_model=tier)
            # Claude role requests retain their existing evergreen alias
            # contract; the economics router still uses its configured ladder.
            role_model = CLAUDE_TIER_ALIASES[tier] if family == "claude" else expected[tier]
            assert resolved.model == role_model
            assert resolved.source == "invocation"


@pytest.mark.parametrize("runtime", ["opencode", "goose", "hermes"])
def test_family_namespace_fix_does_not_enable_additional_runtimes(runtime: str) -> None:
    assert tier_model_providers(runtime) == ()
    assert build_model_router(get_default_config().economics, runtime_backend=runtime) is None
