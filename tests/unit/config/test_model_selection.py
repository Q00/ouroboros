"""Tests for automatic model resolution (``ouroboros.config.model_selection``)."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
)
from ouroboros.config.loader import (
    get_clarification_model,
    get_consensus_models,
    get_llm_model_for_role,
    get_qa_model,
    resolve_execution_model,
)
from ouroboros.config.model_selection import (
    CLAUDE_TIER_ALIASES,
    MODEL_ENV_VARS,
    ROLE_TIERS,
    SHIPPED_CONSENSUS_ROSTER,
    resolve_consensus_roster,
    resolve_role_model,
)
from ouroboros.config.models import (
    ClarificationConfig,
    ConsensusConfig,
    EvaluationConfig,
    ExecutionConfig,
    LLMConfig,
    ModelsConfig,
    OuroborosConfig,
)
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
from ouroboros.core.errors import ConfigError
from ouroboros.orchestrator_stage import LLM_ROLE_STAGE_MAP, UnknownLLMRoleError

ALIAS_BACKENDS = ("claude", "claude_code", "claude_mcp")
SENTINEL_BACKENDS = (
    "codex",
    "codex_mcp",
    "opencode",
    "gemini",
    "goose",
    "host",
    "ourocode",
    "dsh",
    "kiro",
    "hermes",
    "pi",
    "omp",
    "gjc",
    "antigravity",
    "grok",
    "zcode",
)

# A persisted choice in every source a role reads: never a shipped default.
_CHOSEN_CONFIG = OuroborosConfig(
    clarification=ClarificationConfig(default_model="claude-opus-4-1-20250805"),
    llm=LLMConfig(qa_model="gpt-5-nano"),
    evaluation=EvaluationConfig(semantic_model="gpt-5"),
    execution=ExecutionConfig(default_model="gpt-5-codex"),
)


def _resolve(
    role: str,
    backend: str | None,
    *,
    config: OuroborosConfig | None = None,
    env: dict[str, str] | None = None,
    invocation_model: str | None = None,
    pinned: bool | None = None,
):
    load = (
        patch("ouroboros.config.loader.load_config", return_value=config)
        if config is not None
        else patch("ouroboros.config.loader.load_config", side_effect=ConfigError("no config"))
    )
    with patch.dict(os.environ, env or {}, clear=True), load:
        return resolve_role_model(
            role, backend=backend, invocation_model=invocation_model, pinned=pinned
        )


class TestRoleTiers:
    def test_every_stage_role_has_a_tier(self) -> None:
        assert set(LLM_ROLE_STAGE_MAP) <= set(ROLE_TIERS)

    @pytest.mark.parametrize(
        ("role", "tier"),
        [
            ("interview", "frontier"),
            ("seed_generation", "frontier"),
            ("pm_interview", "frontier"),
            ("brownfield", "frontier"),
            ("brownfield_explore", "frontier"),
            ("semantic_evaluation", "frontier"),
            ("wonder", "frontier"),
            ("reflect", "frontier"),
            ("consensus_advocate", "frontier"),
            ("consensus_devil", "frontier"),
            ("consensus_judge", "frontier"),
            ("execute", "standard"),
            ("decomposition", "standard"),
            ("qa", "standard"),
            ("assertion_extraction", "standard"),
            ("mechanical_detection", "standard"),
            ("dependency_analysis", "standard"),
            ("ontology_analysis", "standard"),
            ("context_compression", "standard"),
            ("validation", "standard"),
            ("brownfield_scan", "frugal"),
        ],
    )
    def test_role_tier_table(self, role: str, tier: str) -> None:
        assert ROLE_TIERS[role] == tier

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(UnknownLLMRoleError):
            _resolve("not_a_role", "claude")

    def test_get_llm_model_for_role_degrades_unknown_role_to_evaluation(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.load_config", side_effect=ConfigError("x")),
        ):
            assert get_llm_model_for_role("not_a_role", backend="claude") == "opus"


class TestAutoSelection:
    @pytest.mark.parametrize("backend", ALIAS_BACKENDS)
    @pytest.mark.parametrize("role", sorted(ROLE_TIERS))
    def test_alias_backends_receive_the_tier_alias(self, role: str, backend: str) -> None:
        resolved = _resolve(role, backend, config=OuroborosConfig())
        assert resolved.model == CLAUDE_TIER_ALIASES[ROLE_TIERS[role]]
        assert resolved.source == "auto"
        assert resolved.tier == ROLE_TIERS[role]

    @pytest.mark.parametrize("backend", SENTINEL_BACKENDS)
    @pytest.mark.parametrize("role", sorted(ROLE_TIERS))
    def test_sentinel_backends_receive_the_sentinel(self, role: str, backend: str) -> None:
        resolved = _resolve(role, backend, config=OuroborosConfig())
        assert resolved.model == "default"
        assert resolved.source == "auto"

    @pytest.mark.parametrize("backend", ["gemini", "goose", "ourocode", "host", "dsh"])
    def test_backends_that_used_to_receive_claude_ids_get_the_sentinel(self, backend: str) -> None:
        """gemini/goose previously ran ``--model claude-opus-5``; ourocode rejects aliases."""
        for role in ("interview", "qa", "semantic_evaluation", "reflect"):
            assert _resolve(role, backend, config=_CHOSEN_CONFIG).model == "default"

    def test_unknown_backend_receives_the_sentinel(self) -> None:
        assert _resolve("qa", "not-a-backend").model == "default"

    @pytest.mark.parametrize("backend", ALIAS_BACKENDS + SENTINEL_BACKENDS)
    def test_persisted_values_are_ignored_without_pin(self, backend: str) -> None:
        env = {"OUROBOROS_CLARIFICATION_MODEL": "env-model"}
        for pinned, env_value in ((None, env), (False, env), (None, {})):
            resolved = _resolve(
                "interview", backend, config=_CHOSEN_CONFIG, env=env_value, pinned=pinned
            )
            assert resolved.source == "auto"

    def test_config_missing_uses_auto(self) -> None:
        assert _resolve("qa", "claude").model == "sonnet"
        assert _resolve("qa", "codex").model == "default"


class TestPinnedSelection:
    @pytest.mark.parametrize("backend", ALIAS_BACKENDS + SENTINEL_BACKENDS)
    def test_env_then_config_verbatim(self, backend: str) -> None:
        env = {"OUROBOROS_CLARIFICATION_MODEL": "env-model"}
        resolved = _resolve("interview", backend, config=_CHOSEN_CONFIG, env=env, pinned=True)
        assert (resolved.model, resolved.source) == ("env-model", "pin")
        resolved = _resolve("interview", backend, config=_CHOSEN_CONFIG, pinned=True)
        assert (resolved.model, resolved.source) == ("claude-opus-4-1-20250805", "pin")

    def test_pin_from_config_and_env_flag(self) -> None:
        pinned_config = _CHOSEN_CONFIG.model_copy(update={"models": ModelsConfig(pin=True)})
        assert _resolve("qa", "claude", config=pinned_config).model == "gpt-5-nano"
        env = {"OUROBOROS_PIN_MODELS": "1"}
        assert _resolve("qa", "claude", config=_CHOSEN_CONFIG, env=env).model == "gpt-5-nano"
        # An explicit env "off" beats the config switch.
        env = {"OUROBOROS_PIN_MODELS": "0"}
        assert _resolve("qa", "claude", config=pinned_config, env=env).source == "auto"

    def test_role_inherits_its_stage_setting(self) -> None:
        config = OuroborosConfig(evaluation=EvaluationConfig(semantic_model="gpt-5"))
        assert _resolve("qa", "claude", config=config, pinned=True).model == "gpt-5"

    def test_legacy_shipped_default_snaps_to_current_pin_on_claude(self) -> None:
        config = OuroborosConfig(llm=LLMConfig(qa_model="claude-sonnet-4-20250514"))
        resolved = _resolve("qa", "claude", config=config, pinned=True)
        assert (resolved.model, resolved.source) == (DEFAULT_SONNET_MODEL, "pin")

    def test_legacy_shipped_default_is_the_sentinel_on_sentinel_backends(self) -> None:
        config = OuroborosConfig(clarification=ClarificationConfig(default_model="claude-opus-4-6"))
        assert _resolve("interview", "codex", config=config, pinned=True).model == "default"

    def test_unset_pinned_values_fall_through_to_auto(self) -> None:
        resolved = _resolve("interview", "claude", config=OuroborosConfig(), pinned=True)
        assert (resolved.model, resolved.source) == ("opus", "auto")

    def test_env_automatic_value_overrides_a_pinned_config_value(self) -> None:
        env = {"OUROBOROS_EXECUTION_MODEL": "default"}
        resolved = _resolve("execute", "claude", config=_CHOSEN_CONFIG, env=env, pinned=True)
        assert (resolved.model, resolved.source) == ("sonnet", "auto")

    def test_blank_execution_env_clears_the_pinned_config_value(self) -> None:
        env = {"OUROBOROS_EXECUTION_MODEL": ""}
        resolved = _resolve("execute", "claude", config=_CHOSEN_CONFIG, env=env, pinned=True)
        assert resolved.source == "auto"


class TestExplicitBackends:
    def test_litellm_keeps_configured_values_without_pin(self) -> None:
        resolved = _resolve("qa", "litellm", config=_CHOSEN_CONFIG, pinned=False)
        assert (resolved.model, resolved.source) == ("gpt-5-nano", "configured")
        env = {"OUROBOROS_QA_MODEL": "openrouter/x/y"}
        assert _resolve("qa", "litellm", config=_CHOSEN_CONFIG, env=env).model == "openrouter/x/y"

    @pytest.mark.parametrize("alias", ["openai", "openrouter"])
    def test_litellm_aliases_are_explicit(self, alias: str) -> None:
        assert _resolve("qa", alias, config=_CHOSEN_CONFIG).model == "gpt-5-nano"

    def test_litellm_empty_config_falls_back_to_shipped_pins(self) -> None:
        for role, model in (
            ("interview", DEFAULT_OPUS_MODEL),
            ("qa", DEFAULT_OPUS_MODEL),
            ("execute", DEFAULT_OPUS_MODEL),
            ("brownfield_scan", DEFAULT_HAIKU_MODEL),
            ("consensus_advocate", DEFAULT_CONSENSUS_OPUS_MODEL),
        ):
            resolved = _resolve(role, "litellm", config=OuroborosConfig())
            assert (resolved.model, resolved.source) == (model, "shipped_fallback")
        assert _resolve("qa", "litellm").model == DEFAULT_OPUS_MODEL

    def test_litellm_legacy_shipped_default_normalizes_to_current_pin(self) -> None:
        """#2069: a retired shipped id must not reach the provider."""
        config = OuroborosConfig(llm=LLMConfig(qa_model="claude-sonnet-4-20250514"))
        assert _resolve("qa", "litellm", config=config).model == DEFAULT_SONNET_MODEL
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.load_config", return_value=config),
        ):
            assert get_qa_model(backend="litellm") == DEFAULT_SONNET_MODEL

    def test_litellm_execute_inherits_the_evaluation_model(self) -> None:
        config = OuroborosConfig(evaluation=EvaluationConfig(semantic_model="openrouter/a/b"))
        assert _resolve("decomposition", "litellm", config=config).model == "openrouter/a/b"

    def test_litellm_never_receives_auto_or_an_alias(self) -> None:
        config = OuroborosConfig(llm=LLMConfig(qa_model="auto"))
        for invocation in (None, "auto"):
            resolved = _resolve("qa", "litellm", config=config, invocation_model=invocation)
            assert resolved.model not in {"auto", *CLAUDE_TIER_ALIASES.values()}
        resolved = _resolve("qa", "litellm", invocation_model="frontier")
        assert resolved.model == DEFAULT_OPUS_MODEL

    def test_copilot_keeps_configured_values_and_defaults_to_sentinel(self) -> None:
        assert _resolve("qa", "copilot", config=_CHOSEN_CONFIG).model == "gpt-5-nano"
        resolved = _resolve("qa", "copilot", config=OuroborosConfig())
        assert (resolved.model, resolved.source) == ("default", "shipped_fallback")
        assert _resolve("execute", "copilot", config=_CHOSEN_CONFIG).model == "gpt-5-codex"
        # Copilot's Execute role never inherited the evaluation model.
        config = OuroborosConfig(evaluation=EvaluationConfig(semantic_model="gpt-5"))
        assert _resolve("execute", "copilot", config=config).model == "default"


class TestInvocationAndGlobalDefault:
    @pytest.mark.parametrize("backend", ALIAS_BACKENDS + SENTINEL_BACKENDS + ("litellm",))
    def test_concrete_invocation_model_is_used_as_given(self, backend: str) -> None:
        resolved = _resolve("qa", backend, config=_CHOSEN_CONFIG, invocation_model="my-model")
        assert (resolved.model, resolved.source) == ("my-model", "invocation")

    def test_invocation_tier_name_maps_through_the_tier_table(self) -> None:
        assert _resolve("qa", "claude", invocation_model="frontier").model == "opus"
        assert _resolve("qa", "codex", invocation_model="frugal").model == "default"

    def test_invocation_auto_ignores_pinned_values(self) -> None:
        resolved = _resolve(
            "qa", "claude", config=_CHOSEN_CONFIG, invocation_model="auto", pinned=True
        )
        assert (resolved.model, resolved.source) == ("sonnet", "auto")

    def test_global_default_applies_to_every_role(self) -> None:
        config = OuroborosConfig(models=ModelsConfig(default="frugal"))
        assert _resolve("interview", "claude", config=config).model == "haiku"
        env = {"OUROBOROS_MODEL": "my-global"}
        resolved = _resolve("interview", "claude", config=config, env=env)
        assert (resolved.model, resolved.source) == ("my-global", "configured")

    def test_explicit_model_argument_wins_in_role_lookup(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.load_config", return_value=_CHOSEN_CONFIG),
        ):
            assert get_llm_model_for_role("qa", backend="claude", explicit_model="x") == "x"
            assert get_clarification_model("claude") == "opus"


class TestExecutionModel:
    def test_claude_execution_resolves_to_the_standard_alias(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.load_config", return_value=_CHOSEN_CONFIG),
        ):
            assert resolve_execution_model("claude") == "sonnet"
            assert resolve_execution_model("codex") is None
            assert resolve_execution_model(None) is None


class TestConsensusRoster:
    def _roster(self, backend: str | None, config: OuroborosConfig, **env: str):
        with (
            patch.dict(os.environ, env, clear=True),
            patch("ouroboros.config.loader.load_config", return_value=config),
        ):
            return resolve_consensus_roster(backend=backend)

    def test_auto_repeats_the_consensus_model(self) -> None:
        assert self._roster("claude", OuroborosConfig()) == ("opus", "opus", "opus")
        assert self._roster("codex", OuroborosConfig()) == ("default",) * 3

    def test_litellm_keeps_or_falls_back_to_the_shipped_roster(self) -> None:
        custom = OuroborosConfig(consensus=ConsensusConfig(models=("a", "b")))
        assert self._roster("litellm", custom) == ("a", "b")
        assert self._roster("litellm", OuroborosConfig()) == SHIPPED_CONSENSUS_ROSTER

    def test_pinned_roster_is_used_and_env_list_is_parsed(self) -> None:
        custom = OuroborosConfig(consensus=ConsensusConfig(models=("a", "b")))
        assert self._roster("claude", custom, OUROBOROS_PIN_MODELS="1") == ("a", "b")
        roster = self._roster(
            "claude", custom, OUROBOROS_PIN_MODELS="1", OUROBOROS_CONSENSUS_MODELS="x, y"
        )
        assert roster == ("x", "y")
        assert self._roster("claude", custom) == ("opus", "opus", "opus")

    def test_loader_wrapper_uses_the_llm_backend(self) -> None:
        with (
            patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": "codex"}, clear=True),
            patch("ouroboros.config.loader.load_config", return_value=OuroborosConfig()),
        ):
            assert get_consensus_models() == ("default",) * 3


class TestIgnoredWarning:
    def test_one_warning_for_ignored_persisted_ids(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="ouroboros.config.model_selection")
        for role in ("interview", "qa", "execute"):
            _resolve(role, "claude", config=_CHOSEN_CONFIG)
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "models.pin" in messages[0]
        assert "claude-opus-4-1-20250805" in messages[0]

    def test_shipped_defaults_and_auto_do_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="ouroboros.config.model_selection")
        legacy = OuroborosConfig(
            clarification=ClarificationConfig(default_model="claude-opus-4-8"),
            llm=LLMConfig(qa_model=DEFAULT_SONNET_MODEL),
        )
        for config in (OuroborosConfig(), legacy):
            _resolve("interview", "claude", config=config)
            _resolve("qa", "codex", config=config)
        assert caplog.records == []

    def test_pinned_and_explicit_backends_do_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="ouroboros.config.model_selection")
        _resolve("interview", "claude", config=_CHOSEN_CONFIG, pinned=True)
        _resolve("interview", "litellm", config=_CHOSEN_CONFIG)
        assert caplog.records == []


def test_every_model_env_var_is_denied_from_untrusted_project_env() -> None:
    assert {"OUROBOROS_MODEL", "OUROBOROS_PIN_MODELS"} <= MODEL_ENV_VARS
    assert all(is_untrusted_env_denied_key(key) for key in MODEL_ENV_VARS)
