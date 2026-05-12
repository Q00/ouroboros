"""Tests for 3-step DomainProfile activation in ooo auto CLI (PR-3, #809 P3)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY, DomainProfile
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPipelineState, AutoStore, SeedOrigin
from ouroboros.cli.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_profile(
    name: str,
    detector_score: float = 0.0,
    detector: Callable[[Path], float] | None = None,
) -> DomainProfile:
    """Build a minimal DomainProfile suitable for unit tests."""

    class _FakeRepoContextExtractor:
        def extract(self, cwd: Path) -> dict[str, Any]:
            return {}

    class _FakeVerifiablePredicate:
        code = "fake_predicate"

        def matches(self, criterion: str) -> bool:
            return False

        def repair_template(self, criterion: str) -> str:
            return criterion

    class _FakeIntentClassifier:
        def classify(self, question: str) -> str | None:
            return None

        def supported_intents(self) -> frozenset[str]:
            return frozenset()

    return DomainProfile(
        name=name,
        repo_context_extractor=_FakeRepoContextExtractor(),
        verifiable_predicates=(_FakeVerifiablePredicate(),),
        intent_classifier=_FakeIntentClassifier(),
        vague_terms=frozenset(),
        safe_defaults={},
        detector=detector or (lambda _cwd: detector_score),
    )


_FAKE_RESULT = AutoPipelineResult(
    status="complete",
    auto_session_id="auto_test123",
    phase="complete",
    grade="A",
    seed_path=None,
    seed_origin=SeedOrigin.NONE.value,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_default_registry(monkeypatch):
    """Give each test an isolated DEFAULT_REGISTRY profile list."""
    monkeypatch.setattr(DEFAULT_REGISTRY, "_profiles", [])


@pytest.fixture()
def fake_profile(isolated_default_registry):
    """Register a fake 'fake-domain' profile in DEFAULT_REGISTRY for the test duration."""
    profile = _make_fake_profile("fake-domain", detector_score=0.0)
    DEFAULT_REGISTRY.register(profile)
    return profile


@pytest.fixture()
def detectable_profile(isolated_default_registry, tmp_path):
    """Register a fake 'detectable' profile that returns high confidence for any dir."""
    profile = _make_fake_profile("detectable", detector_score=0.9)
    DEFAULT_REGISTRY.register(profile)
    return profile, tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_import_bootstraps_builtin_research_profile() -> None:
    """Production CLI imports register built-in profiles before domain lookup."""
    from ouroboros.cli.commands import auto as auto_command

    assert auto_command.DEFAULT_REGISTRY.get("research") is not None


def test_auto_help_hides_domain_option(isolated_default_registry) -> None:
    """The dormant domain activation hook is accepted but not publicly advertised."""
    assert DEFAULT_REGISTRY.all() == ()

    result = runner.invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    assert "--domain" not in result.output


def test_domain_flag_overrides_detection(fake_profile, tmp_path) -> None:
    """--domain <name> wins even when cwd has no matching signals."""
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(state):
        captured["profile_name"] = state.active_domain_profile_name
        return _FAKE_RESULT

    from ouroboros.cli.commands.auto import _run_auto

    with patch(
        "ouroboros.auto.pipeline.AutoPipeline.run",
        side_effect=_fake_pipeline_run,
    ):
        with patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path):
            result = asyncio.run(
                _run_auto(
                    goal="build something",
                    resume=None,
                    runtime=None,
                    max_interview_rounds=None,
                    max_repair_rounds=None,
                    skip_run=True,
                    domain="fake-domain",
                )
            )

    assert result.status == "complete"
    assert captured["profile_name"] == "fake-domain"


def test_detection_falls_back_to_best_profile(detectable_profile, tmp_path) -> None:
    """Without --domain, detect_best() is called and its result is stored on state."""
    profile, cwd = detectable_profile
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(state):
        captured["profile_name"] = state.active_domain_profile_name
        return _FAKE_RESULT

    from ouroboros.cli.commands.auto import _run_auto

    with patch(
        "ouroboros.auto.pipeline.AutoPipeline.run",
        side_effect=_fake_pipeline_run,
    ):
        with patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=cwd):
            result = asyncio.run(
                _run_auto(
                    goal="build something",
                    resume=None,
                    runtime=None,
                    max_interview_rounds=None,
                    max_repair_rounds=None,
                    skip_run=True,
                    domain=None,
                )
            )

    assert result.status == "complete"
    assert captured["profile_name"] == "detectable"


def test_no_match_leaves_profile_none(tmp_path) -> None:
    """An empty registry with no --domain leaves active_domain_profile_name as None."""
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(state):
        captured["profile_name"] = state.active_domain_profile_name
        return _FAKE_RESULT

    from ouroboros.cli.commands.auto import _run_auto

    # Patch DEFAULT_REGISTRY.detect_best to return None regardless of registry state.
    with patch.object(DEFAULT_REGISTRY, "detect_best", return_value=None):
        with patch(
            "ouroboros.auto.pipeline.AutoPipeline.run",
            side_effect=_fake_pipeline_run,
        ):
            with patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path):
                result = asyncio.run(
                    _run_auto(
                        goal="build something",
                        resume=None,
                        runtime=None,
                        max_interview_rounds=None,
                        max_repair_rounds=None,
                        skip_run=True,
                        domain=None,
                    )
                )

    assert result.status == "complete"
    assert captured["profile_name"] is None


def test_detector_exception_leaves_profile_none(tmp_path) -> None:
    """Detector failures during new-session startup are best-effort no-matches."""
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(state):
        captured["profile_name"] = state.active_domain_profile_name
        return _FAKE_RESULT

    def _raise_detector(_cwd: Path) -> float:
        raise OSError("unreadable")

    from ouroboros.cli.commands.auto import _run_auto

    with patch.object(DEFAULT_REGISTRY, "_profiles", []):
        profile = _make_fake_profile("broken-detector", detector=_raise_detector)
        DEFAULT_REGISTRY.register(profile)
        with (
            patch(
                "ouroboros.auto.pipeline.AutoPipeline.run",
                side_effect=_fake_pipeline_run,
            ),
            patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path),
        ):
            result = asyncio.run(
                _run_auto(
                    goal="build something",
                    resume=None,
                    runtime=None,
                    max_interview_rounds=None,
                    max_repair_rounds=None,
                    skip_run=True,
                    domain=None,
                )
            )

    assert result.status == "complete"
    assert captured["profile_name"] is None


def test_unknown_domain_value_errors(tmp_path) -> None:
    """--domain <unknown> exits nonzero without starting the pipeline."""
    import typer

    from ouroboros.cli.commands.auto import _run_auto

    with patch.object(DEFAULT_REGISTRY, "get", return_value=None):
        with patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path):
            with pytest.raises(typer.Exit) as exc_info:
                asyncio.run(
                    _run_auto(
                        goal="build something",
                        resume=None,
                        runtime=None,
                        max_interview_rounds=None,
                        max_repair_rounds=None,
                        skip_run=True,
                        domain="banana",
                    )
                )

    assert exc_info.value.exit_code == 1


def test_resume_without_domain_preserves_persisted_profile(tmp_path) -> None:
    """Resuming without --domain must not re-run detection or overwrite state."""
    from ouroboros.cli.commands.auto import _run_auto

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="build something", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.active_domain_profile_name = "persisted-domain"
    store.save(state)

    detectable = _make_fake_profile("detectable-on-resume", detector_score=0.9)
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(run_state):
        captured["profile_name"] = run_state.active_domain_profile_name
        store.save(run_state)
        return _FAKE_RESULT

    with (
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=store),
        patch.object(DEFAULT_REGISTRY, "detect_best", return_value=detectable) as detect_best,
        patch(
            "ouroboros.auto.pipeline.AutoPipeline.run",
            side_effect=_fake_pipeline_run,
        ),
    ):
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=state.auto_session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=True,
                domain=None,
            )
        )

    assert result.status == "complete"
    assert captured["profile_name"] == "persisted-domain"
    assert store.load(state.auto_session_id).active_domain_profile_name == "persisted-domain"
    detect_best.assert_not_called()


def test_resume_with_explicit_domain_overrides_persisted_profile(tmp_path) -> None:
    """Explicit --domain on resume intentionally updates active_domain_profile_name."""
    from ouroboros.cli.commands.auto import _run_auto

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="build something", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.active_domain_profile_name = "persisted-domain"
    store.save(state)

    override = _make_fake_profile("override-domain", detector_score=0.0)
    captured: dict[str, Any] = {}

    async def _fake_pipeline_run(run_state):
        captured["profile_name"] = run_state.active_domain_profile_name
        store.save(run_state)
        return _FAKE_RESULT

    with (
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=store),
        patch.object(DEFAULT_REGISTRY, "get", return_value=override) as get_profile,
        patch.object(DEFAULT_REGISTRY, "detect_best") as detect_best,
        patch(
            "ouroboros.auto.pipeline.AutoPipeline.run",
            side_effect=_fake_pipeline_run,
        ),
    ):
        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=state.auto_session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=True,
                domain="override-domain",
            )
        )

    assert result.status == "complete"
    assert captured["profile_name"] == "override-domain"
    assert store.load(state.auto_session_id).active_domain_profile_name == "override-domain"
    get_profile.assert_called_once_with("override-domain")
    detect_best.assert_not_called()


def test_unknown_domain_cli_error_is_not_wrapped(tmp_path) -> None:
    """typer.Exit from unknown --domain should keep the clean domain error."""
    with (
        patch.object(DEFAULT_REGISTRY, "get", return_value=None),
        patch("ouroboros.cli.commands.auto._safe_default_cwd", return_value=tmp_path),
    ):
        result = runner.invoke(app, ["auto", "build something", "--domain", "banana"])

    assert result.exit_code == 1
    assert "Unknown domain profile: 'banana'" in result.output
    assert "Auto pipeline failed" not in result.output
