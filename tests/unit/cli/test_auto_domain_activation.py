"""Tests for 3-step DomainProfile activation in ooo auto CLI (PR-3, #809 P3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY, DomainProfile
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import SeedOrigin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_profile(name: str, detector_score: float = 0.0) -> DomainProfile:
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
        detector=lambda _cwd: detector_score,
    )


_FAKE_RESULT = AutoPipelineResult(
    status="complete",
    auto_session_id="auto_test123",
    phase="complete",
    grade="A",
    seed_path=None,
    seed_origin=SeedOrigin.NONE.value,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_profile():
    """Register a fake 'fake-domain' profile in DEFAULT_REGISTRY for the test duration."""
    profile = _make_fake_profile("fake-domain", detector_score=0.0)
    DEFAULT_REGISTRY.register(profile)
    yield profile
    # Cleanup: remove the registered profile so other tests are not affected.
    DEFAULT_REGISTRY._profiles[:] = [
        p for p in DEFAULT_REGISTRY._profiles if p.name != "fake-domain"
    ]


@pytest.fixture()
def detectable_profile(tmp_path):
    """Register a fake 'detectable' profile that returns high confidence for any dir."""
    profile = _make_fake_profile("detectable", detector_score=0.9)
    DEFAULT_REGISTRY.register(profile)
    yield profile, tmp_path
    DEFAULT_REGISTRY._profiles[:] = [
        p for p in DEFAULT_REGISTRY._profiles if p.name != "detectable"
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
