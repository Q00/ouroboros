"""Model-tier routing policy: the pure decision the live executor lays itself on."""

from __future__ import annotations

import pytest

from ouroboros.config._model_defaults import (
    _SHIPPED_TIER_HISTORY,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    LEGACY_TIER_MODEL_PROVIDERS,
    LEGACY_TIER_MODEL_TIERS,
    LEGACY_TIER_MODELS,
    SHIPPED_TIER_LADDER,
    is_historical_tier_model,
    normalize_tier_model,
)
from ouroboros.config.model_selection import CLAUDE_TIER_ALIASES
from ouroboros.config.models import (
    EconomicsConfig,
    ModelConfig,
    TierConfig,
    fresh_config_data,
    get_default_config,
)
from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.model_routing import (
    MODEL_TIER_LADDER,
    ModelDecision,
    ModelRouter,
    build_model_router,
    decide_model,
    deserialize_model_router,
    lower_one_notch,
    raise_one_notch,
    resolve_execute_model,
    serialize_model_router,
    tier_from_model_tier_arg,
    tier_from_profile_hint,
)
from ouroboros.orchestrator.resolved_models import resolve_route_economics


def _economics(**overrides: object) -> EconomicsConfig:
    """A minimal anthropic-populated economics config for router tests."""
    defaults: dict[str, object] = {
        "default_tier": "frugal",
        "escalation_threshold": 2,
        "tiers": {
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="haiku-x")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="sonnet-x")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="opus-x")],
            ),
        },
    }
    defaults.update(overrides)
    return EconomicsConfig(**defaults)  # type: ignore[arg-type]


class TestLowerOneNotch:
    def test_drops_one_rung(self) -> None:
        assert lower_one_notch("frontier") == "standard"
        assert lower_one_notch("standard") == "frugal"

    def test_never_below_floor(self) -> None:
        assert lower_one_notch("frugal") == "frugal"  # ladder floor

    def test_custom_floor(self) -> None:
        assert lower_one_notch("frontier", floor="standard") == "standard"
        assert lower_one_notch("standard", floor="standard") == "standard"

    def test_unknown_tier_passthrough(self) -> None:
        assert lower_one_notch("bananas") == "bananas"

    def test_ladder_is_ordered_weak_to_strong(self) -> None:
        assert MODEL_TIER_LADDER.index("frugal") < MODEL_TIER_LADDER.index("frontier")


class TestRaiseOneNotch:
    def test_lifts_one_rung(self) -> None:
        assert raise_one_notch("frugal") == "standard"
        assert raise_one_notch("standard") == "frontier"

    def test_never_above_ceiling(self) -> None:
        assert raise_one_notch("frontier") == "frontier"  # ladder top
        assert raise_one_notch("frugal", ceiling="standard") == "standard"

    def test_unknown_tier_passthrough(self) -> None:
        assert raise_one_notch("bananas") == "bananas"


class TestTierVocabularyMapping:
    def test_profile_hint_maps_low_medium_high(self) -> None:
        assert tier_from_profile_hint("low") == "frugal"
        assert tier_from_profile_hint("medium") == "standard"
        assert tier_from_profile_hint("high") == "frontier"

    def test_profile_hint_none_and_unknown(self) -> None:
        assert tier_from_profile_hint(None) is None
        assert tier_from_profile_hint("bananas") is None

    def test_model_tier_arg_maps_small_medium_large(self) -> None:
        assert tier_from_model_tier_arg("small") == "frugal"
        assert tier_from_model_tier_arg("medium") == "standard"
        assert tier_from_model_tier_arg("large") == "frontier"

    def test_model_tier_arg_none_and_unknown(self) -> None:
        assert tier_from_model_tier_arg(None) is None
        assert tier_from_model_tier_arg("giant") is None


class TestBuildModelRouter:
    def test_pin_keeps_routing_dormant(self) -> None:
        router = build_model_router(
            _economics(), runtime_backend="claude", pinned_model="claude-opus-4-8"
        )
        assert router is None

    def test_unmapped_or_missing_backend_is_dormant(self) -> None:
        # An unmapped backend string and None both keep routing dormant. ("codex"
        # bare is not a runtime_backend property value — the runtimes report
        # "codex_cli"/"codex_mcp" — so it is unmapped.)
        assert build_model_router(_economics(), runtime_backend="codex") is None
        assert build_model_router(_economics(), runtime_backend="opencode") is None
        assert build_model_router(_economics(), runtime_backend=None) is None

    def test_claude_code_backend_routes(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude_code")
        assert router is not None
        assert router.runtime_backend == "claude_code"  # recorded for the redispatch guard

    def test_claude_worker_backend_routes(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude_mcp")
        assert router is not None
        assert router.runtime_backend == "claude_mcp"
        assert router.tier_models == {
            "frugal": "haiku-x",
            "standard": "sonnet-x",
            "frontier": "opus-x",
        }

    @pytest.mark.parametrize("backend", ["codex_cli", "codex_mcp"])
    def test_codex_shipped_ladder_inherits_the_codex_default(self, backend: str) -> None:
        # A codex runtime reports "codex_cli"/"codex_mcp" -> openai provider. The
        # shipped ladder names no model: Codex keeps its own configured model.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend=backend)
        assert router is not None
        assert router.runtime_backend == backend
        assert router.tier_models == dict.fromkeys(MODEL_TIER_LADDER, "default")
        # A starting run enforces nothing from that ladder, so it stays dormant.
        assert (
            build_model_router(economics, runtime_backend=backend, require_enforceable=True) is None
        )

    @pytest.mark.parametrize("backend", ["codex_cli", "codex_mcp"])
    @pytest.mark.parametrize("provider", ["openai", "codex"])
    def test_codex_explicit_custom_tier_is_enforced(self, backend: str, provider: str) -> None:
        economics = get_default_config().economics
        frontier = economics.tiers["frontier"]
        tiers = dict(economics.tiers)
        tiers["frontier"] = frontier.model_copy(
            update={
                "models": [ModelConfig(provider=provider, model="proxy/large"), *frontier.models]
            }
        )
        economics = economics.model_copy(update={"tiers": tiers})

        router = build_model_router(economics, runtime_backend=backend, require_enforceable=True)

        assert router is not None
        assert router.tier_models == {
            "frugal": "default",
            "standard": "default",
            "frontier": "proxy/large",
        }
        base = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=False)
        assert (base.tier, base.model, base.mode) == ("standard", "default", "advised")
        escalated = decide_model(
            ParamSupport.NATIVE, router=router, is_decomposed_child=False, retry_attempt=3
        )
        assert (escalated.tier, escalated.model, escalated.mode) == (
            "frontier",
            "proxy/large",
            "enforced",
        )

    def test_gemini_backend_builds_google_router(self) -> None:
        # Gemini has no frontier google model in the default config, so only the
        # tiers with a google model are present.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend="gemini_cli")
        assert router is not None
        assert router.runtime_backend == "gemini_cli"
        assert router.tier_models == {
            "frugal": "flash",
            "standard": "pro",
        }

    def test_opencode_backend_is_dormant(self) -> None:
        # opencode is intentionally unmapped (composite provider/model ids).
        assert (
            build_model_router(get_default_config().economics, runtime_backend="opencode") is None
        )

    def test_provider_filter_picks_anthropic_model_per_tier(self) -> None:
        economics = _economics(
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    models=[
                        ModelConfig(provider="openai", model="gpt-mini"),
                        ModelConfig(provider="anthropic", model="haiku-x"),
                    ],
                ),
                "standard": TierConfig(
                    cost_factor=10,
                    models=[ModelConfig(provider="anthropic", model="sonnet-x")],
                ),
            }
        )
        router = build_model_router(economics, runtime_backend="claude")
        assert router is not None
        assert router.tier_models == {"frugal": "haiku-x", "standard": "sonnet-x"}

    def test_tier_without_matching_provider_is_skipped(self) -> None:
        economics = _economics(
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    models=[ModelConfig(provider="anthropic", model="haiku-x")],
                ),
                "standard": TierConfig(
                    cost_factor=10,
                    models=[ModelConfig(provider="openai", model="gpt-4o")],
                ),
            }
        )
        router = build_model_router(economics, runtime_backend="claude")
        assert router is not None
        assert router.tier_models == {"frugal": "haiku-x"}

    def test_empty_map_is_dormant(self) -> None:
        economics = _economics(
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    models=[ModelConfig(provider="openai", model="gpt-mini")],
                ),
            }
        )
        assert build_model_router(economics, runtime_backend="claude") is None

    def test_base_tier_is_one_notch_above_child(self) -> None:
        router = build_model_router(_economics(default_tier="frugal"), runtime_backend="claude")
        assert router is not None
        assert router.child_tier == "frugal"
        assert router.base_tier == "standard"

    def test_base_tier_override_wins(self) -> None:
        router = build_model_router(
            _economics(), runtime_backend="claude", base_tier_override="frontier"
        )
        assert router is not None
        assert router.base_tier == "frontier"

    def test_escalation_threshold_carried_from_config(self) -> None:
        router = build_model_router(_economics(escalation_threshold=3), runtime_backend="claude")
        assert router is not None
        assert router.escalation_retry_threshold == 3

    @pytest.mark.parametrize("backend", ["claude", "claude_code", "claude_mcp"])
    def test_default_config_end_to_end(self, backend: str) -> None:
        # The shipped default: children run haiku, top-level ACs run sonnet, as
        # Claude Code aliases it resolves for its own provider.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend=backend, require_enforceable=True)
        assert router is not None
        assert router.child_tier == "frugal"
        assert router.base_tier == "standard"
        assert router.tier_models == {"frugal": "haiku", "standard": "sonnet", "frontier": "opus"}
        for retry_attempt, tier in ((0, "sonnet"), (2, "opus")):
            decision = decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=False,
                retry_attempt=retry_attempt,
            )
            assert (decision.model, decision.mode) == (tier, "enforced")


class TestDeserializeModelRouter:
    @staticmethod
    def _payload(**router_overrides: object) -> dict[str, object]:
        router: dict[str, object] = {
            "tier_models": {"frugal": "haiku-x", "standard": "sonnet-x"},
            "runtime_backend": "claude",
            "child_tier": "frugal",
            "base_tier": "standard",
            "escalation_retry_threshold": 2,
        }
        router.update(router_overrides)
        return {"version": 1, "enabled": True, "router": router}

    @pytest.mark.parametrize("threshold", [0, -1, True, 1.0])
    def test_rejects_threshold_outside_economics_contract(self, threshold: object) -> None:
        recognized, router = deserialize_model_router(
            self._payload(escalation_retry_threshold=threshold)
        )

        assert recognized is False
        assert router is None

    def test_rejects_whitespace_normalized_duplicate_tiers(self) -> None:
        recognized, router = deserialize_model_router(
            self._payload(tier_models={"frugal": "haiku-x", " frugal ": "other-haiku"})
        )

        assert recognized is False
        assert router is None

    @pytest.mark.parametrize("invalid", [None, 0, 1, "false", []])
    def test_rejects_non_boolean_child_lowering_policy(self, invalid: object) -> None:
        recognized, router = deserialize_model_router(
            self._payload(child_model_lowering_enabled=invalid)
        )
        assert recognized is False
        assert router is None

    def test_missing_child_lowering_bit_keeps_legacy_semantics(self) -> None:
        recognized, router = deserialize_model_router(self._payload())
        assert recognized is True
        assert router is not None
        assert router.child_model_lowering_enabled is True
        assert serialize_model_router(router)["router"]["child_model_lowering_enabled"] is True


def _legacy_persisted_economics() -> EconomicsConfig:
    """Economics as an OLD release would have persisted it to ~/.ouroboros/config.yaml.

    Carries the FULL old shipped tier defaults verbatim across all three providers,
    exactly as they appear in the git history of ``ouroboros.config.models``.
    """
    return EconomicsConfig(
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[
                    ModelConfig(provider="openai", model="gpt-4o-mini"),
                    ModelConfig(provider="google", model="gemini-2.0-flash"),
                    ModelConfig(provider="anthropic", model="claude-3-5-haiku"),
                ],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[
                    ModelConfig(provider="openai", model="gpt-4o"),
                    ModelConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
                    ModelConfig(provider="google", model="gemini-2.5-pro"),
                ],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[
                    ModelConfig(provider="openai", model="o3"),
                    ModelConfig(provider="anthropic", model="claude-opus-4-5-20251101"),
                ],
            ),
        },
    )


class TestLegacyTierModelNormalization:
    """A config persisted by an older release carries the shipped tier ids of its time.

    Model-tier routing must resolve those to each runtime's own default for the
    tier (the persisted-config trap that made every AC fail with "unknown provider
    for model gpt-4o"), while preserving a genuinely explicit user id verbatim —
    the same precedent role models use in LEGACY_DEFAULT_MODELS
    (Q00/ouroboros#1324).
    """

    @pytest.mark.parametrize("backend", ["claude", "claude_code", "claude_mcp"])
    def test_claude_backends_route_legacy_ids_to_enforced_aliases(self, backend: str) -> None:
        router = build_model_router(_legacy_persisted_economics(), runtime_backend=backend)
        assert router is not None
        assert router.tier_models == {"frugal": "haiku", "standard": "sonnet", "frontier": "opus"}
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE, runtime_backend=backend),
            router=router,
            is_decomposed_child=False,
        )
        assert decision.mode == "enforced"
        assert kwargs == {"model": "sonnet"}

    @pytest.mark.parametrize(
        ("tier", "model", "alias"),
        [
            *(("frugal", m, "haiku") for m in ("claude-3-5-haiku", "claude-haiku-4-5")),
            ("frugal", DEFAULT_HAIKU_MODEL, "haiku"),
            *(("standard", m, "sonnet") for m in ("claude-sonnet-4-20250514", "claude-sonnet-4-6")),
            ("standard", DEFAULT_SONNET_MODEL, "sonnet"),
            *(
                ("frontier", m, "opus")
                for m in (
                    "claude-opus-4-5-20251101",
                    "claude-opus-4-6",
                    "claude-opus-4-8",
                    "claude-opus-5",
                )
            ),
            ("frontier", DEFAULT_OPUS_MODEL, "opus"),
        ],
    )
    def test_every_shipped_anthropic_id_routes_to_its_tier_alias(
        self, tier: str, model: str, alias: str
    ) -> None:
        economics = _economics(
            tiers={
                tier: TierConfig(
                    cost_factor=1, models=[ModelConfig(provider="anthropic", model=model)]
                )
            }
        )
        router = build_model_router(economics, runtime_backend="claude_code")
        assert router is not None
        assert router.tier_models == {tier: alias}

    @pytest.mark.parametrize("backend", ["claude", "claude_code", "claude_mcp"])
    @pytest.mark.parametrize("model", ["claude-opus-4-1", "us.anthropic.claude-opus-5-5-v1:0"])
    def test_explicit_custom_claude_id_is_enforced_verbatim(self, backend: str, model: str) -> None:
        economics = _economics(
            tiers={
                "standard": TierConfig(
                    cost_factor=10, models=[ModelConfig(provider="anthropic", model=model)]
                )
            }
        )
        router = build_model_router(economics, runtime_backend=backend)
        assert router is not None
        assert router.tier_models == {"standard": model}
        _decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE, runtime_backend=backend),
            router=router,
            is_decomposed_child=False,
        )
        assert kwargs == {"model": model}

    def test_runtime_namespace_keeps_a_shipped_looking_id_literal(self) -> None:
        # A ``claude`` entry is an explicit runtime choice, never a vendor default.
        economics = _economics(
            tiers={
                "standard": TierConfig(
                    cost_factor=10,
                    models=[
                        ModelConfig(provider="claude", model=DEFAULT_SONNET_MODEL),
                        ModelConfig(provider="anthropic", model=DEFAULT_SONNET_MODEL),
                    ],
                )
            }
        )
        router = build_model_router(economics, runtime_backend="claude_code")
        assert router is not None
        assert router.tier_models == {"standard": DEFAULT_SONNET_MODEL}

    @pytest.mark.parametrize("backend", ["codex_cli", "codex_mcp"])
    @pytest.mark.parametrize(
        "ladder",
        [
            ("gpt-4o-mini", "gpt-4o", "o3"),
            ("gpt-5.1-codex-mini", "gpt-5-codex", "gpt-5.2"),
            ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"),
        ],
    )
    def test_codex_backends_inherit_for_every_shipped_openai_ladder(
        self, backend: str, ladder: tuple[str, str, str]
    ) -> None:
        economics = _economics(
            tiers={
                tier: TierConfig(cost_factor=1, models=[ModelConfig(provider="openai", model=m)])
                for tier, m in zip(MODEL_TIER_LADDER, ladder, strict=True)
            }
        )
        router = build_model_router(economics, runtime_backend=backend)
        assert router is not None
        assert router.tier_models == dict.fromkeys(MODEL_TIER_LADDER, "default")
        for retry_attempt in (0, 5):
            decision, kwargs = resolve_execute_model(
                _Adapter(ParamSupport.NATIVE, runtime_backend=backend),
                router=router,
                is_decomposed_child=False,
                retry_attempt=retry_attempt,
            )
            assert decision.mode == "advised"
            assert kwargs == {}
        assert (
            build_model_router(economics, runtime_backend=backend, require_enforceable=True) is None
        )

    def test_gemini_legacy_api_ids_route_to_cli_aliases(self) -> None:
        router = build_model_router(_legacy_persisted_economics(), runtime_backend="gemini_cli")
        assert router is not None
        assert router.tier_models == {"frugal": "flash", "standard": "pro"}

    def test_explicit_user_id_preserved_verbatim(self) -> None:
        # A never-shipped, proxy-specific id is a deliberate user choice — routing
        # must not rewrite it to any shipped default.
        economics = _economics(
            tiers={
                "standard": TierConfig(
                    cost_factor=10,
                    models=[ModelConfig(provider="openai", model="gpt-5.6-sol")],
                ),
            }
        )
        router = build_model_router(economics, runtime_backend="codex_cli")
        assert router is not None
        assert router.tier_models == {"standard": "gpt-5.6-sol"}

    def test_legacy_looking_id_under_other_provider_is_preserved(self) -> None:
        # This value was historically shipped only for OpenAI. Under Anthropic it
        # cannot be an untouched old default; it is an explicit proxy/model alias.
        economics = _economics(
            tiers={
                "standard": TierConfig(
                    cost_factor=10,
                    models=[ModelConfig(provider="anthropic", model="gpt-4o")],
                ),
            }
        )

        router = build_model_router(economics, runtime_backend="claude")

        assert router is not None
        assert router.tier_models == {"standard": "gpt-4o"}


class TestShippedTierLadder:
    """The shipped ladder holds each runtime's own default, never a concrete id."""

    def test_default_config_ships_runtime_defaults_per_vendor(self) -> None:
        tiers = get_default_config().economics.tiers
        shipped = {
            provider: {
                tier: entry.model
                for tier, config in tiers.items()
                for entry in config.models
                if entry.provider == provider
            }
            for provider in ("openai", "anthropic", "google")
        }
        assert shipped == {
            "openai": dict.fromkeys(MODEL_TIER_LADDER, "default"),
            "anthropic": {"frugal": "haiku", "standard": "sonnet", "frontier": "opus"},
            "google": {"frugal": "flash", "standard": "pro"},
        }
        assert shipped == SHIPPED_TIER_LADDER
        assert dict(CLAUDE_TIER_ALIASES) == SHIPPED_TIER_LADDER["anthropic"]

    def test_fresh_config_writes_no_concrete_tier_id(self) -> None:
        economics = fresh_config_data()["economics"]["tiers"]
        written = {entry["model"] for tier in economics.values() for entry in tier["models"]}
        assert written == {"default", "haiku", "sonnet", "opus", "flash", "pro"}
        assert not written & set(LEGACY_TIER_MODELS)

    def test_each_historical_id_belongs_to_one_provider_and_tier(self) -> None:
        listed = [
            model
            for tiers in _SHIPPED_TIER_HISTORY.values()
            for models in tiers.values()
            for model in models
        ]
        assert len(listed) == len(set(listed)) == len(LEGACY_TIER_MODELS)
        for model in listed:
            provider = LEGACY_TIER_MODEL_PROVIDERS[model]
            tier = LEGACY_TIER_MODEL_TIERS[model]
            assert LEGACY_TIER_MODELS[model] == SHIPPED_TIER_LADDER[provider][tier]
            assert is_historical_tier_model(model, provider=provider, tier=tier)
            assert normalize_tier_model(model, provider=provider) == LEGACY_TIER_MODELS[model]
            other = next(name for name in MODEL_TIER_LADDER if name != tier)
            assert not is_historical_tier_model(model, provider=provider, tier=other)


class TestDecideModel:
    def test_router_none_is_dormant(self) -> None:
        d = decide_model(ParamSupport.NATIVE, router=None, is_decomposed_child=False)
        assert d == ModelDecision(tier=None, model=None, mode="none")
        assert d.is_enforced is False

    def test_top_level_uses_base_tier(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=False)
        assert d.tier == "standard"
        assert d.model == "sonnet-x"
        assert d.mode == "enforced"

    def test_trusted_decomposed_child_drops_one_tier(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        assert d.tier == "frugal"  # standard base dropped one notch
        assert d.model == "haiku-x"

    def test_quality_first_keeps_trusted_child_starting_tier_and_retry_raise(self) -> None:
        router = ModelRouter(
            tier_models={"frugal": "haiku-x", "standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="standard",
            base_tier="standard",
            escalation_retry_threshold=2,
            child_model_lowering_enabled=False,
        )
        initial = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        retry = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=2,
        )
        suggested = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            suggested_tier="frugal",
        )
        assert (initial.tier, initial.model) == ("standard", "sonnet-x")
        assert (retry.tier, retry.model) == ("frontier", "opus-x")
        assert (suggested.tier, suggested.model) == ("frugal", "haiku-x")

    def test_equal_child_and_base_tiers_do_not_disable_adaptive_lowering(self) -> None:
        router = build_model_router(
            _economics(default_tier="standard"),
            runtime_backend="claude",
            base_tier_override="standard",
        )
        assert router is not None
        assert router.child_tier == router.base_tier == "standard"
        decision = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        assert decision.tier == "frugal"

    def test_explicit_model_pin_still_disables_routing(self) -> None:
        assert (
            build_model_router(_economics(), runtime_backend="claude", pinned_model="user-sonnet")
            is None
        )

    def test_untrusted_decomposed_child_stays_at_base_tier(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=True)
        assert d.tier == "standard"
        assert d.model == "sonnet-x"

    def test_decomposition_trust_must_be_exact_true(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=1,  # type: ignore[arg-type]
        )
        assert d.tier == "standard"
        assert d.model == "sonnet-x"

    def test_untrusted_decomposed_child_stays_at_suggested_tier(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            suggested_tier="frontier",
        )
        assert d.tier == "frontier"
        assert d.model == "opus-x"

    def test_trusted_retry_escalation_beats_child_drop(self) -> None:
        # A failing child earns a stronger model: escalation is applied AFTER the
        # child drop, so at the threshold it climbs back to (at least) the base.
        router = build_model_router(_economics(escalation_threshold=2), runtime_backend="claude")
        assert router is not None
        initial = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=0,
        )
        first = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=1,
        )
        second = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=2,
        )
        assert initial.tier == "frugal"  # child drop
        assert first.tier == "frugal"  # below threshold, no raise yet
        assert second.tier == "standard"  # drop then raise = back to base

    def test_untrusted_retry_escalation_starts_from_base_tier(self) -> None:
        router = build_model_router(_economics(escalation_threshold=2), runtime_backend="claude")
        assert router is not None
        tiers = [
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=True,
                retry_attempt=attempt,
            ).tier
            for attempt in range(4)
        ]
        assert tiers == ["standard", "standard", "frontier", "frontier"]

    def test_progressive_escalation_threshold_one_reaches_frontier(self) -> None:
        # threshold=1: a persistently failing unit climbs one tier per retry.
        # Child ladder (drop then progressive raise) walks frugal->standard->
        # frontier; top-level walks standard->frontier->frontier (ceiling cap).
        router = build_model_router(_economics(escalation_threshold=1), runtime_backend="claude")
        assert router is not None
        child_tiers = [
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=True,
                decomposition_trustworthy=True,
                retry_attempt=attempt,
            ).tier
            for attempt in range(3)
        ]
        top_tiers = [
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=False,
                retry_attempt=attempt,
            ).tier
            for attempt in range(3)
        ]
        assert child_tiers == ["frugal", "standard", "frontier"]
        assert top_tiers == ["standard", "frontier", "frontier"]

    def test_progressive_escalation_threshold_two_matches_legacy_first_step(self) -> None:
        # At the SHIPPED default threshold=2, the first escalation step (attempt 2)
        # is byte-identical to the old single-notch rule: child climbs back to the
        # base tier, top-level climbs one notch. Only attempt 3+ diverges.
        router = build_model_router(_economics(escalation_threshold=2), runtime_backend="claude")
        assert router is not None
        child_tiers = [
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=True,
                decomposition_trustworthy=True,
                retry_attempt=attempt,
            ).tier
            for attempt in range(4)
        ]
        top_tiers = [
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=False,
                retry_attempt=attempt,
            ).tier
            for attempt in range(4)
        ]
        assert child_tiers == ["frugal", "frugal", "standard", "frontier"]
        assert top_tiers == ["standard", "standard", "frontier", "frontier"]

    def test_escalation_capped_at_frontier_ceiling(self) -> None:
        # A large retry_attempt cannot climb past the frontier ceiling.
        router = build_model_router(_economics(escalation_threshold=2), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=10,
        )
        assert d.tier == "frontier"
        assert d.model == "opus-x"

    def test_escalation_composes_after_suggested_tier(self) -> None:
        # Escalation is applied AFTER the child drop even when the starting tier
        # comes from suggested_tier: frugal suggested -> child drop stays frugal
        # (floor) -> one progressive notch at threshold=1 lands on standard.
        router = build_model_router(_economics(escalation_threshold=1), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
            retry_attempt=1,
            suggested_tier="frugal",
        )
        assert d.tier == "standard"
        assert d.model == "sonnet-x"

    def test_suggested_tier_overrides_base_upward(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=False,
            suggested_tier="frontier",
        )
        assert d.tier == "frontier"
        assert d.model == "opus-x"

    def test_suggested_tier_overrides_base_downward(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=False,
            suggested_tier="frugal",
        )
        assert d.tier == "frugal"
        assert d.model == "haiku-x"

    def test_advised_when_runtime_cannot_enforce(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(ParamSupport.IGNORED, router=router, is_decomposed_child=False)
        assert d.model == "sonnet-x"
        assert d.mode == "advised"
        assert d.is_enforced is False

    def test_gemini_router_decision_is_advised_only(self) -> None:
        # Gemini has no per-call model-override knob (IGNORED support): a router
        # built for it is observability-only, never enforced.
        router = build_model_router(get_default_config().economics, runtime_backend="gemini_cli")
        assert router is not None
        d = decide_model(ParamSupport.IGNORED, router=router, is_decomposed_child=False)
        assert d.tier == "standard"
        assert d.model == "pro"
        assert d.mode == "advised"
        assert d.is_enforced is False

    def test_missing_tier_walks_up_to_stronger_model(self) -> None:
        # frugal tier absent -> a child dropped to frugal walks UP to standard,
        # never silently substituting something cheaper than decided. The
        # decision reports the tier that actually supplied the model so proof
        # telemetry cannot claim a frugal execution that really ran standard.
        router = ModelRouter(
            tier_models={"standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        )
        d = decide_model(
            ParamSupport.NATIVE,
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        assert d.tier == "standard"
        assert d.model == "sonnet-x"  # walked up, not down

    def test_untrusted_child_sparse_tier_uses_base_exact_model(self) -> None:
        router = ModelRouter(
            tier_models={"standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        )
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=True)
        assert d.tier == "standard"
        assert d.model == "sonnet-x"

    def test_missing_tier_walks_down_as_last_resort(self) -> None:
        # Nothing stronger than the decided tier exists -> walk down.
        router = ModelRouter(
            tier_models={"frugal": "haiku-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        )
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=False)
        assert d.tier == "frugal"
        assert d.model == "haiku-x"  # only cheaper model left

    def test_empty_map_yields_mode_none(self) -> None:
        router = ModelRouter(
            tier_models={},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        )
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=False)
        assert d.model is None
        assert d.mode == "none"


class _Caps:
    def __init__(self, support: ParamSupport) -> None:
        self.model_override_support = support


class _Adapter:
    def __init__(
        self,
        support: ParamSupport | None,
        *,
        with_caps: bool = True,
        runtime_backend: str = "claude",
    ) -> None:
        self.runtime_backend = runtime_backend
        if with_caps:
            self.capabilities: object = _Caps(support) if support is not None else object()


class _AdapterNoCaps:
    """An adapter that declares no capabilities attribute at all."""

    def __init__(self, runtime_backend: str = "claude") -> None:
        self.runtime_backend = runtime_backend


class TestResolveExecuteModel:
    @pytest.fixture
    def router(self) -> ModelRouter:
        built = build_model_router(_economics(), runtime_backend="claude")
        assert built is not None
        return built

    def test_native_runtime_yields_model_kwarg(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE), router=router, is_decomposed_child=False
        )
        assert decision.mode == "enforced"
        assert kwargs == {"model": "sonnet-x"}

    def test_native_trusted_child_yields_lowered_model_kwarg(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE),
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        assert decision.mode == "enforced"
        assert decision.tier == "frugal"
        assert kwargs == {"model": "haiku-x"}

    def test_native_untrusted_child_yields_base_model_kwarg(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE), router=router, is_decomposed_child=True
        )
        assert decision.mode == "enforced"
        assert decision.tier == "standard"
        assert kwargs == {"model": "sonnet-x"}

    def test_ignored_runtime_is_advised_with_empty_kwargs(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.IGNORED), router=router, is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}

    def test_ignored_trusted_child_is_advised_with_empty_kwargs(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.IGNORED),
            router=router,
            is_decomposed_child=True,
            decomposition_trustworthy=True,
        )
        assert decision.mode == "advised"
        assert decision.tier == "frugal"
        assert decision.model == "haiku-x"
        assert kwargs == {}

    def test_capabilities_without_field_defaults_to_ignored(self, router: ModelRouter) -> None:
        # Capabilities object present but missing model_override_support (the
        # field is added by a parallel change) -> treated as IGNORED, advised.
        decision, kwargs = resolve_execute_model(
            _Adapter(None), router=router, is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}

    def test_adapter_without_capabilities_yields_empty_kwargs(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _AdapterNoCaps(), router=router, is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}

    def test_dormant_router_yields_empty_kwargs(self) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE), router=None, is_decomposed_child=False
        )
        assert decision.mode == "none"
        assert kwargs == {}

    def test_cross_backend_adapter_treats_router_as_absent(self, router: ModelRouter) -> None:
        # Cross-harness redispatch swaps in a codex adapter mid-run; a router built
        # for claude must not hand its anthropic model id to a different backend.
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.NATIVE, runtime_backend="codex_cli"),
            router=router,
            is_decomposed_child=False,
        )
        assert decision.mode == "none"
        assert decision.model is None
        assert kwargs == {}


class TestResolveRouteEconomics:
    """Under ``auto`` each tier runs the backend's latest model of that tier."""

    @staticmethod
    def _router_models(economics: EconomicsConfig, backend: str) -> dict[str, str]:
        router = build_model_router(economics, runtime_backend=backend)
        assert router is not None
        return dict(router.tier_models)

    def test_auto_routes_claude_tiers_to_aliases(self) -> None:
        resolved = resolve_route_economics(_economics(), runtime_backend="claude", pinned=False)

        assert self._router_models(resolved, "claude") == {
            "frugal": "haiku",
            "standard": "sonnet",
            "frontier": "opus",
        }
        # The ladder's shape and costs stay configuration.
        assert {name: tier.cost_factor for name, tier in resolved.tiers.items()} == {
            "frugal": 1,
            "standard": 10,
            "frontier": 30,
        }

    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            ("codex_cli", dict.fromkeys(MODEL_TIER_LADDER, "default")),
            ("codex_mcp", dict.fromkeys(MODEL_TIER_LADDER, "default")),
            ("gemini_cli", {"frugal": "flash", "standard": "pro"}),
        ],
    )
    def test_model_router_keeps_native_ladder_on_sentinel_backends(
        self, backend: str, expected: dict[str, str]
    ) -> None:
        shipped = get_default_config().economics
        resolved = resolve_route_economics(shipped, runtime_backend=backend, pinned=False)

        models = self._router_models(resolved, backend)
        assert models == self._router_models(shipped, backend) == expected

    def test_auto_keeps_other_providers_entries(self) -> None:
        shipped = get_default_config().economics
        resolved = resolve_route_economics(shipped, runtime_backend="claude", pinned=False)

        assert self._router_models(resolved, "codex_cli") == self._router_models(
            shipped, "codex_cli"
        )

    def test_pinned_ladder_is_used_verbatim(self) -> None:
        economics = _economics()

        resolved = resolve_route_economics(economics, runtime_backend="claude", pinned=True)

        assert resolved is economics
        assert self._router_models(resolved, "claude") == {
            "frugal": "haiku-x",
            "standard": "sonnet-x",
            "frontier": "opus-x",
        }

    def test_explicit_backend_keeps_configured_ladder(self) -> None:
        economics = _economics()

        assert (
            resolve_route_economics(economics, runtime_backend="litellm", pinned=False) is economics
        )

    def test_unmapped_backend_is_unchanged(self) -> None:
        economics = _economics()

        assert (
            resolve_route_economics(economics, runtime_backend="opencode", pinned=False)
            is economics
        )
        assert resolve_route_economics(economics, runtime_backend=None, pinned=False) is economics


class TestExplicitNativeLadders:
    def test_runtime_specific_codex_ladder_precedes_openai_defaults(self) -> None:
        economics = _economics(
            tiers={
                "standard": TierConfig(
                    cost_factor=10,
                    models=[
                        ModelConfig(provider="openai", model="gpt-4o"),
                        ModelConfig(provider="codex", model="my-custom-proxy"),
                    ],
                )
            }
        )
        router = build_model_router(economics, runtime_backend="codex_cli")
        assert router is not None
        assert dict(router.tier_models) == {"standard": "my-custom-proxy"}

    @pytest.mark.parametrize("model", ["default", "auto", "current", " default ", "AUTO", ""])
    def test_unresolved_sentinel_is_advisory_even_with_native_model_flag(self, model: str) -> None:
        router = ModelRouter({"standard": model}, "codex_cli", "frugal", "standard", 2)
        decision = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=False)
        assert decision.mode == "advised"
        assert not decision.is_enforced
