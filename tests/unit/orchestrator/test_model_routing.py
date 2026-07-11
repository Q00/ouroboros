"""Model-tier routing policy: the pure decision the live executor lays itself on."""

from __future__ import annotations

import pytest

from ouroboros.config._model_defaults import (
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
)
from ouroboros.config.models import (
    EconomicsConfig,
    ModelConfig,
    TierConfig,
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
    tier_from_model_tier_arg,
    tier_from_profile_hint,
)


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

    def test_codex_cli_backend_resolves_openai_tier_models(self) -> None:
        # A codex runtime reports runtime_backend "codex_cli" -> openai provider.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend="codex_cli")
        assert router is not None
        assert router.runtime_backend == "codex_cli"
        assert router.tier_models == {
            "frugal": "gpt-5.1-codex-mini",
            "standard": "gpt-5-codex",
            "frontier": "gpt-5.2",
        }

    def test_codex_mcp_backend_resolves_openai_tier_models(self) -> None:
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend="codex_mcp")
        assert router is not None
        assert router.tier_models["standard"] == "gpt-5-codex"

    def test_gemini_backend_builds_google_router(self) -> None:
        # Gemini has no frontier google model in the default config, so only the
        # tiers with a google model are present.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend="gemini_cli")
        assert router is not None
        assert router.runtime_backend == "gemini_cli"
        assert router.tier_models == {
            "frugal": "gemini-2.0-flash",
            "standard": "gemini-2.5-pro",
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

    def test_default_config_end_to_end(self) -> None:
        # The shipped default: children run haiku, top-level ACs run sonnet.
        economics = get_default_config().economics
        router = build_model_router(economics, runtime_backend="claude")
        assert router is not None
        assert router.child_tier == "frugal"
        assert router.base_tier == "standard"
        assert router.tier_models["frugal"] == DEFAULT_HAIKU_MODEL
        assert router.tier_models["standard"] == DEFAULT_SONNET_MODEL


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
    """A config persisted by an older release carries retired shipped tier ids.

    Model-tier routing must normalize those to the CURRENT shipped ids (the
    persisted-config trap that made every AC fail with "unknown provider for model
    gpt-4o"), while preserving a genuinely explicit user id verbatim — the same
    precedent role models use in LEGACY_DEFAULT_MODELS (Q00/ouroboros#1324).
    """

    def test_claude_backend_resolves_current_ids(self) -> None:
        router = build_model_router(_legacy_persisted_economics(), runtime_backend="claude")
        assert router is not None
        assert router.tier_models == {
            "frugal": DEFAULT_HAIKU_MODEL,
            "standard": DEFAULT_SONNET_MODEL,
            "frontier": DEFAULT_OPUS_MODEL,
        }

    def test_codex_cli_backend_resolves_current_ids(self) -> None:
        router = build_model_router(_legacy_persisted_economics(), runtime_backend="codex_cli")
        assert router is not None
        assert router.tier_models == {
            "frugal": "gpt-5.1-codex-mini",
            "standard": "gpt-5-codex",
            "frontier": "gpt-5.2",
        }

    def test_legacy_opus_4_6_normalizes_to_current(self) -> None:
        # The later frontier default claude-opus-4-6 is also a legacy shipped id.
        economics = _economics(
            tiers={
                "frontier": TierConfig(
                    cost_factor=30,
                    models=[ModelConfig(provider="anthropic", model="claude-opus-4-6")],
                ),
            }
        )
        router = build_model_router(economics, runtime_backend="claude")
        assert router is not None
        assert router.tier_models == {"frontier": DEFAULT_OPUS_MODEL}

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

    def test_decomposed_child_drops_one_tier(self) -> None:
        router = build_model_router(_economics(), runtime_backend="claude")
        assert router is not None
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=True)
        assert d.tier == "frugal"  # standard base dropped one notch
        assert d.model == "haiku-x"

    def test_retry_escalation_beats_child_drop(self) -> None:
        # A failing child earns a stronger model: escalation is applied AFTER the
        # child drop, so at the threshold it climbs back to (at least) the base.
        router = build_model_router(_economics(escalation_threshold=2), runtime_backend="claude")
        assert router is not None
        initial = decide_model(
            ParamSupport.NATIVE, router=router, is_decomposed_child=True, retry_attempt=0
        )
        first = decide_model(
            ParamSupport.NATIVE, router=router, is_decomposed_child=True, retry_attempt=1
        )
        second = decide_model(
            ParamSupport.NATIVE, router=router, is_decomposed_child=True, retry_attempt=2
        )
        assert initial.tier == "frugal"  # child drop
        assert first.tier == "frugal"  # below threshold, no raise yet
        assert second.tier == "standard"  # drop then raise = back to base

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
        assert d.model == "gemini-2.5-pro"
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
        d = decide_model(ParamSupport.NATIVE, router=router, is_decomposed_child=True)
        assert d.tier == "standard"
        assert d.model == "sonnet-x"  # walked up, not down

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

    def test_ignored_runtime_is_advised_with_empty_kwargs(self, router: ModelRouter) -> None:
        decision, kwargs = resolve_execute_model(
            _Adapter(ParamSupport.IGNORED), router=router, is_decomposed_child=False
        )
        assert decision.mode == "advised"
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
