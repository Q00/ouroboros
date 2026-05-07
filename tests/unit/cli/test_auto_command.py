"""Tests for the direct `ouroboros auto` CLI surface."""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.cli.main import app

runner = CliRunner()


def _plain(text: str) -> str:
    """Strip ANSI sequences from rich-rendered Typer output."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_auto_help_uses_direct_goal_command_shape() -> None:
    result = runner.invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Usage: ouroboros auto [OPTIONS] [GOAL]" in output
    assert "COMMAND [ARGS]" not in output
    assert "Goal/task for ooo auto" in output


def test_auto_goal_skip_run_does_not_require_subcommand() -> None:
    result_value = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_test",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        interview_session_id="interview_test",
    )

    def consume(coro):
        coro.close()
        return result_value

    with patch("ouroboros.cli.commands.auto.asyncio.run", side_effect=consume) as run_auto:
        result = runner.invoke(app, ["auto", "safe test goal", "--skip-run"])

    assert result.exit_code == 0
    assert run_auto.called
    assert "Auto session:" in result.output
    assert "auto_test" in result.output


def test_auto_prompts_for_driver_when_interactive_and_unconfigured() -> None:
    """Interactive plain `ooo auto` should offer selected-driver mode."""
    from ouroboros.cli.commands.auto import _prompt_driver_if_missing

    with (
        patch("ouroboros.cli.commands.auto.get_auto_interview_driver_backend", return_value=None),
        patch("ouroboros.cli.commands.auto.sys.stdin.isatty", return_value=True),
        patch("ouroboros.cli.commands.auto.typer.confirm", return_value=True) as confirm,
        patch("ouroboros.cli.commands.auto.typer.prompt", return_value="hermes") as prompt,
    ):
        driver = _prompt_driver_if_missing(driver=None, resume=None)

    assert driver == "hermes"
    confirm.assert_called_once()
    prompt.assert_called_once()


def test_auto_prompt_decline_keeps_deterministic_driver() -> None:
    """Declining the interactive driver prompt preserves existing deterministic mode."""
    from ouroboros.cli.commands.auto import _prompt_driver_if_missing

    with (
        patch("ouroboros.cli.commands.auto.get_auto_interview_driver_backend", return_value=None),
        patch("ouroboros.cli.commands.auto.sys.stdin.isatty", return_value=True),
        patch("ouroboros.cli.commands.auto.typer.confirm", return_value=False),
        patch("ouroboros.cli.commands.auto.typer.prompt") as prompt,
    ):
        driver = _prompt_driver_if_missing(driver=None, resume=None)

    assert driver is None
    prompt.assert_not_called()


def test_auto_prompt_skips_when_configured_driver_exists() -> None:
    """Configured defaults should avoid an extra prompt."""
    from ouroboros.cli.commands.auto import _prompt_driver_if_missing

    with (
        patch(
            "ouroboros.cli.commands.auto.get_auto_interview_driver_backend",
            return_value="hermes",
        ),
        patch("ouroboros.cli.commands.auto.sys.stdin.isatty", return_value=True),
        patch("ouroboros.cli.commands.auto.typer.confirm") as confirm,
    ):
        driver = _prompt_driver_if_missing(driver=None, resume=None)

    assert driver is None
    confirm.assert_not_called()


def _persisted_state_with_bounds(tmp_path, *, max_interview_rounds: int, max_repair_rounds: int):
    """Persist a blocked auto session with a known loop budget for resume tests."""
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.interview_driver_backend = "codex"
    state.max_interview_rounds = max_interview_rounds
    state.max_repair_rounds = max_repair_rounds
    state.skip_run = True
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "auto interview reached max rounds with unresolved gaps: actors",
        tool_name="interview_driver",
    )
    store = AutoStore(tmp_path)
    store.save(state)
    return state, store, state.auto_session_id


def test_resume_uses_persisted_bounds_when_cli_unspecified(tmp_path) -> None:
    """No explicit CLI bound on resume must keep the persisted budget intact."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )

    captured: dict[str, int] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["max_interview_rounds"] = self.interview_driver.max_rounds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store

        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured["max_interview_rounds"] == 2


def test_resume_raises_persisted_bound_when_cli_overrides_higher(tmp_path) -> None:
    """Explicit CLI value larger than persisted must raise the bound for resume."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )

    captured: dict[str, int] = {}

    async def fake_pipeline_run(self, state):
        captured["driver_max_rounds"] = self.interview_driver.max_rounds
        captured["state_max_interview_rounds"] = state.max_interview_rounds
        captured["state_max_repair_rounds"] = state.max_repair_rounds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store

        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                max_interview_rounds=6,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured["driver_max_rounds"] == 6
    assert captured["state_max_interview_rounds"] == 6
    assert captured["state_max_repair_rounds"] == 1


def test_resume_rejects_lower_bound_override(tmp_path) -> None:
    """Tightening a bound on resume must be refused — never trap a session further."""
    import asyncio

    import pytest

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=4, max_repair_rounds=2
    )

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store

        with pytest.raises(ValueError, match="refuse to tighten"):
            asyncio.run(
                _run_auto(
                    goal=None,
                    resume=session_id,
                    runtime=None,
                    max_interview_rounds=2,
                    max_repair_rounds=None,
                    skip_run=False,
                )
            )


def test_resume_preserves_persisted_driver_and_brake_when_unspecified(tmp_path) -> None:
    """Resume without driver/brake flags keeps the original interview semantics."""
    import asyncio

    from ouroboros.auto.state import AutoBrakeMode
    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    state.brake = AutoBrakeMode.OFF
    store.save(state)
    captured: dict[str, str] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["brake"] = state.brake.value
        captured["answerer_backend"] = self.interview_driver.answerer.backend
        captured["answerer_brake"] = self.interview_driver.answerer.brake.value
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured == {
        "driver": "codex",
        "brake": "off",
        "answerer_backend": "codex",
        "answerer_brake": "off",
    }


def test_resume_applies_requested_driver_for_legacy_session(tmp_path) -> None:
    """Legacy sessions without a persisted driver still honor an explicit resume driver."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    state.interview_driver_backend = None
    store.save(state)
    captured: dict[str, str | None] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["answerer_backend"] = self.interview_driver.answerer.backend
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                driver="hermes",
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured == {"driver": "hermes", "answerer_backend": "hermes"}


def test_resume_rejects_brake_mismatch(tmp_path) -> None:
    """Changing brake mode on resume must be explicit session mismatch, not silent mutation."""
    import asyncio

    import pytest

    from ouroboros.auto.state import AutoBrakeMode
    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    state.brake = AutoBrakeMode.OFF
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        with pytest.raises(ValueError, match="resume brake mismatch"):
            asyncio.run(
                _run_auto(
                    goal=None,
                    resume=session_id,
                    runtime=None,
                    brake=AutoBrakeMode.ON.value,
                    skip_run=False,
                )
            )


def test_auto_without_driver_keeps_deterministic_answerer(tmp_path) -> None:
    """Plain `ooo auto` must not opt into an LLM-backed answer driver."""
    import asyncio

    from ouroboros.auto.answerer import AutoAnswerer
    from ouroboros.auto.state import AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    store = AutoStore(tmp_path)
    captured: dict[str, object] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["answerer_type"] = type(self.interview_driver.answerer)
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
        patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal="Build a CLI",
                resume=None,
                runtime=None,
                skip_run=True,
            )
        )

    assert result.status == "complete"
    assert captured == {"driver": None, "answerer_type": AutoAnswerer}


def test_auto_uses_configured_driver_when_flag_is_omitted(tmp_path) -> None:
    """A configured auto driver opts plain `ooo auto` into selected-driver mode."""
    import asyncio

    from ouroboros.auto.state import AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    store = AutoStore(tmp_path)
    captured: dict[str, str] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["answerer_backend"] = self.interview_driver.answerer.backend
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
        patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path),
        patch(
            "ouroboros.cli.commands.auto.get_auto_interview_driver_backend",
            return_value="hermes",
        ),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal="Build a CLI",
                resume=None,
                runtime=None,
                skip_run=True,
            )
        )

    assert result.status == "complete"
    assert captured == {"driver": "hermes", "answerer_backend": "hermes"}


def test_resume_ignores_configured_driver_for_legacy_session(tmp_path) -> None:
    """Resume must preserve a deterministic legacy session unless --driver is explicit."""
    import asyncio

    from ouroboros.auto.answerer import AutoAnswerer
    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    state.interview_driver_backend = None
    store.save(state)
    captured: dict[str, object] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["answerer_type"] = type(self.interview_driver.answerer)
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
        patch(
            "ouroboros.cli.commands.auto.get_auto_interview_driver_backend",
            return_value="hermes",
        ),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured == {"driver": None, "answerer_type": AutoAnswerer}


def test_resume_accepts_claude_driver_alias_for_legacy_persisted_claude(tmp_path) -> None:
    """Resume compares selected drivers using the persisted LLM backend identity."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    state.interview_driver_backend = "claude"
    store.save(state)
    captured: dict[str, str | None] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                driver="claude",
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured == {"driver": "claude_code"}


def test_auto_persists_selected_driver_and_brake_off(tmp_path) -> None:
    """`ooo auto --driver X --brake off` stores the selected interview respondent."""
    import asyncio

    from ouroboros.auto.state import AutoBrakeMode, AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    store = AutoStore(tmp_path)
    captured: dict[str, str] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["driver"] = state.interview_driver_backend
        captured["brake"] = state.brake.value
        captured["answerer_backend"] = self.interview_driver.answerer.backend
        captured["answerer_brake"] = self.interview_driver.answerer.brake.value
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
        patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path),
    ):
        store_cls.return_value = store
        result = asyncio.run(
            _run_auto(
                goal="Build a CLI",
                resume=None,
                runtime=None,
                driver="hermes",
                brake=AutoBrakeMode.OFF.value,
                skip_run=True,
            )
        )

    assert result.status == "complete"
    assert captured == {
        "driver": "hermes",
        "brake": "off",
        "answerer_backend": "hermes",
        "answerer_brake": "off",
    }
