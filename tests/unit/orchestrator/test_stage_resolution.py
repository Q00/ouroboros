"""Unit tests for the Stage enum and runtime resolution (slice 1 of #519).

Coverage:
- Stage enum has exactly the four members agreed in the #519
  sub-thread; ``parse_stage`` rejects unknown values.
- ``resolve_runtime_for_stage`` picks ``stages[stage] → default →
  fallback`` in that order.
- ``runtime_profile=None`` (config absence) preserves today's
  behaviour byte-for-byte by always returning the orchestrator's
  ``runtime_backend``.
- A typo in ``runtime_profile.stages`` (e.g. ``"interveiw"``) raises
  :class:`UnknownStageError` at config validation time, not later.
- ``OrchestratorConfig.runtime_profile`` is optional (``None``
  default) so existing configs construct unchanged.
"""

from __future__ import annotations

import pytest

from ouroboros.config import OrchestratorConfig, RuntimeProfileConfig
from ouroboros.orchestrator.stage import (
    VALID_STAGE_KEYS,
    Stage,
    UnknownStageError,
    parse_stage,
    resolve_runtime_for_stage,
)


class TestStageEnum:
    def test_has_four_members(self) -> None:
        assert {s.value for s in Stage} == {
            "interview",
            "execute",
            "evaluate",
            "reflect",
        }

    def test_valid_keys_matches_enum(self) -> None:
        assert {s.value for s in Stage} == VALID_STAGE_KEYS


class TestParseStage:
    def test_valid_string_returns_member(self) -> None:
        assert parse_stage("evaluate") is Stage.EVALUATE

    def test_unknown_string_raises_with_helpful_message(self) -> None:
        with pytest.raises(UnknownStageError) as info:
            parse_stage("interveiw")  # typo
        msg = str(info.value)
        assert "interveiw" in msg
        # The error must surface the valid set so operators can fix
        # without consulting docs.
        for valid in VALID_STAGE_KEYS:
            assert valid in msg


class TestResolveRuntimeForStage:
    def test_explicit_stage_wins(self) -> None:
        runtime = resolve_runtime_for_stage(
            Stage.EVALUATE,
            stages={Stage.EVALUATE: "claude_code"},
            default="opencode",
            fallback="claude",
        )
        assert runtime == "claude_code"

    def test_default_used_when_stage_missing(self) -> None:
        runtime = resolve_runtime_for_stage(
            Stage.EVALUATE,
            stages={Stage.EXECUTE: "opencode"},  # different stage
            default="codex",
            fallback="claude",
        )
        assert runtime == "codex"

    def test_fallback_when_no_runtime_profile_configured(self) -> None:
        runtime = resolve_runtime_for_stage(
            Stage.EVALUATE,
            stages=None,
            default=None,
            fallback="claude",
        )
        assert runtime == "claude"

    def test_empty_stages_dict_uses_default(self) -> None:
        runtime = resolve_runtime_for_stage(
            Stage.EVALUATE,
            stages={},
            default="opencode",
            fallback="claude",
        )
        assert runtime == "opencode"

    def test_empty_default_falls_through_to_fallback(self) -> None:
        runtime = resolve_runtime_for_stage(
            Stage.EVALUATE,
            stages={},
            default="",
            fallback="claude",
        )
        assert runtime == "claude"


class TestRuntimeProfileConfig:
    def test_none_runtime_profile_is_default_on_orchestrator_config(self) -> None:
        config = OrchestratorConfig()
        assert config.runtime_profile is None

    def test_construct_with_explicit_runtime_profile(self) -> None:
        config = OrchestratorConfig(
            runtime_profile=RuntimeProfileConfig(
                default="codex",
                stages={"evaluate": "claude"},
            )
        )
        assert config.runtime_profile is not None
        assert config.runtime_profile.default == "codex"
        assert config.runtime_profile.stages == {"evaluate": "claude"}

    def test_unknown_stage_key_rejected_at_validation(self) -> None:
        # Pydantic wraps validator-raised exceptions in its own
        # ``ValidationError``; the original :class:`UnknownStageError`
        # message survives as the cause so operators see "Unknown
        # runtime_profile.stages key: 'interveiw'" alongside the valid
        # set.
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as info:
            RuntimeProfileConfig(stages={"interveiw": "codex"})
        msg = str(info.value)
        assert "interveiw" in msg
        assert "Unknown runtime_profile.stages key" in msg
        for valid in VALID_STAGE_KEYS:
            assert valid in msg

    def test_legacy_orchestrator_config_unchanged(self) -> None:
        """Configs without ``runtime_profile`` construct exactly as before."""
        # Pin the field set so reviewers see explicit diffs when membership
        # grows.
        legacy = OrchestratorConfig(runtime_backend="codex")
        assert legacy.runtime_backend == "codex"
        assert legacy.runtime_profile is None
        # The orchestrator's runtime_backend remains the byte-for-byte
        # fallback (per #505 commitment).
        runtime = resolve_runtime_for_stage(
            Stage.EXECUTE,
            stages=(
                {parse_stage(k): v for k, v in legacy.runtime_profile.stages.items()}
                if legacy.runtime_profile
                else None
            ),
            default=legacy.runtime_profile.default if legacy.runtime_profile else None,
            fallback=legacy.runtime_backend,
        )
        assert runtime == "codex"

    def test_invalid_runtime_profile_default_backend_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as info:
            RuntimeProfileConfig(default="cluade")
        msg = str(info.value)
        assert "runtime_profile.default" in msg
        assert "claude" in msg
        assert "codex" in msg

    def test_invalid_runtime_profile_stage_backend_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as info:
            RuntimeProfileConfig(stages={"execute": ""})
        msg = str(info.value)
        assert "runtime_profile.stages['execute']" in msg
        assert "claude" in msg
        assert "codex" in msg
