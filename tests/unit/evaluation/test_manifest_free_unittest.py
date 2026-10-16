"""Manifest-free unittest discovery must feed real Stage 1 evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouroboros.evaluation.detector import ensure_mechanical_toml, has_mechanical_toml
from ouroboros.evaluation.languages import build_mechanical_config
from ouroboros.evaluation.mechanical import MechanicalVerifier
from ouroboros.evaluation.models import CheckType, MechanicalDisposition


def _run(coro):
    return asyncio.run(coro)


def _write_unittest(path: Path, *, fail: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assertion = "self.assertEqual(1, 2)" if fail else "self.assertEqual(1, 1)"
    path.write_text(
        "import unittest\n\n"
        "class TestSample(unittest.TestCase):\n"
        "    def test_value(self):\n"
        f"        {assertion}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("fail", "expected_disposition"),
    [
        (False, MechanicalDisposition.EXECUTED_PASS),
        (True, MechanicalDisposition.EXECUTED_FAIL),
    ],
)
def test_manifest_free_unittest_runs_as_stage1_evidence(
    tmp_path: Path,
    fail: bool,
    expected_disposition: MechanicalDisposition,
) -> None:
    """The TOML consumer runs the discovered command and uses its exit status."""
    _write_unittest(tmp_path / "tests" / "test_sample.py", fail=fail)

    assert _run(ensure_mechanical_toml(tmp_path, None)) is True

    config = build_mechanical_config(tmp_path)
    assert config.test_command == ("python3", "-m", "unittest", "discover", "-s", "tests")
    result = _run(MechanicalVerifier(config).verify("manifest-free", checks=[CheckType.TEST]))

    assert result.is_ok
    mechanical, _ = result.value
    assert mechanical.disposition is expected_disposition
    check = mechanical.checks[0]
    assert check.executed is True
    assert check.passed is (not fail)


def test_project_without_discoverable_tests_stays_unverified(tmp_path: Path) -> None:
    """A test-like filename alone must not produce a zero-test passing check."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_helpers.py").write_text("def helper():\n    return True\n", encoding="utf-8")

    assert _run(ensure_mechanical_toml(tmp_path, None)) is False
    assert has_mechanical_toml(tmp_path) is False

    config = build_mechanical_config(tmp_path)
    result = _run(MechanicalVerifier(config).verify("no-tests", checks=[CheckType.TEST]))

    assert result.is_ok
    mechanical, _ = result.value
    assert mechanical.disposition is MechanicalDisposition.NO_EVIDENCE
    assert mechanical.checks[0].executed is False


def test_existing_manifest_keeps_llm_detection_path_and_is_unchanged(tmp_path: Path) -> None:
    """The unittest fallback is limited to manifest-free projects."""
    manifest = tmp_path / "pyproject.toml"
    original = '[project]\nname = "manifest-backed"\n'
    manifest.write_text(original, encoding="utf-8")
    _write_unittest(tmp_path / "tests" / "test_sample.py")

    class EmptyAdapter:
        calls = 0

        async def complete(self, messages, config):
            from ouroboros.core.types import Result
            from ouroboros.providers.base import CompletionResponse, UsageInfo

            self.calls += 1
            return Result.ok(
                CompletionResponse(
                    content="{}",
                    model=config.model,
                    usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            )

    adapter = EmptyAdapter()
    assert _run(ensure_mechanical_toml(tmp_path, adapter)) is False
    assert adapter.calls == 1
    assert manifest.read_text(encoding="utf-8") == original
    assert has_mechanical_toml(tmp_path) is False
