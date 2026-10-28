"""Manifest-free unittest discovery must feed real Stage 1 evidence."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ouroboros.evaluation import stdlib_unittest_discovery as unittest_discovery
from ouroboros.evaluation.detector import ensure_mechanical_toml, has_mechanical_toml
from ouroboros.evaluation.languages import build_mechanical_config
from ouroboros.evaluation.mechanical import MechanicalVerifier
from ouroboros.evaluation.models import CheckType, MechanicalDisposition
from ouroboros.evaluation.verification_artifacts import build_verification_artifacts


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


def test_manifest_free_unittest_recurses_into_importable_subpackage(
    tmp_path: Path,
) -> None:
    """unittest discovery also finds tests below a package under tests/."""
    package = tmp_path / "tests" / "unit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write_unittest(package / "test_nested.py")

    assert _run(ensure_mechanical_toml(tmp_path, None)) is True
    config = build_mechanical_config(tmp_path)
    result = _run(MechanicalVerifier(config).verify("nested", checks=[CheckType.TEST]))

    assert result.is_ok
    mechanical, _ = result.value
    assert mechanical.disposition is MechanicalDisposition.EXECUTED_PASS
    assert mechanical.checks[0].passed is True


def test_manifest_free_unittest_does_not_follow_symlink_cycle(tmp_path: Path) -> None:
    """Package symlinks cannot make the bounded discovery loop revisit paths."""
    package = tmp_path / "tests" / "unit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write_unittest(package / "test_nested.py")
    try:
        os.symlink(tmp_path / "tests", package / "cycle", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    assert _run(ensure_mechanical_toml(tmp_path, None)) is True


def test_manifest_free_unittest_budget_exhaustion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test file beyond the scan budget cannot become an unverified guess."""
    _write_unittest(tmp_path / "tests" / "test_sample.py")
    (tmp_path / "tests" / "test_helper.py").write_text(
        "def helper():\n    return True\n", encoding="utf-8"
    )
    monkeypatch.setattr(unittest_discovery, "_MAX_UNITTEST_DISCOVERY_ENTRIES", 1)

    assert _run(ensure_mechanical_toml(tmp_path, None)) is False
    assert has_mechanical_toml(tmp_path) is False


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


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        (
            "test-bad.py",
            "import unittest\n\n"
            "class TestSample(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        pass\n",
        ),
        (
            "test_shadowed_case.py",
            "from unittest import TestCase\n"
            "TestCase = object\n\n"
            "class TestSample(TestCase):\n"
            "    def test_value(self):\n"
            "        pass\n",
        ),
        (
            "test_conditional_shadowed_case.py",
            "from unittest import TestCase\n"
            "if True:\n"
            "    TestCase = object\n\n"
            "class TestSample(TestCase):\n"
            "    def test_value(self):\n"
            "        pass\n",
        ),
        (
            "test_shadowed_module.py",
            "import unittest as ut\n"
            "ut = object\n\n"
            "class TestSample(ut.TestCase):\n"
            "    def test_value(self):\n"
            "        pass\n",
        ),
        (
            "test_unimported_module.py",
            "class TestSample(unittest.TestCase):\n    def test_value(self):\n        pass\n",
        ),
        (
            "test_relative_unittest_import.py",
            "from .unittest import TestCase\n"
            "class TestSample(TestCase):\n    def test_value(self):\n        pass\n",
        ),
        (
            "test_shadowed_test_class.py",
            "import unittest\n\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_value(self):\n        pass\n\n"
            "Tests = object\n",
        ),
        (
            "test_shadowed_test_method.py",
            "import unittest\n\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_value(self):\n        pass\n"
            "    test_value = None\n",
        ),
        (
            "test_module_shadowed_test_method.py",
            "import unittest\n\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_value(self):\n        pass\n\n"
            "Tests.test_value = None\n",
        ),
        (
            "test_module_deleted_test_method.py",
            "import unittest\n\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_value(self):\n        pass\n\n"
            "del Tests.test_value\n",
        ),
    ],
)
def test_manifest_free_unittest_rejects_non_discoverable_evidence(
    tmp_path: Path, filename: str, source: str
) -> None:
    """Invalid module names and rebound unittest symbols cannot imply real tests."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / filename).write_text(source, encoding="utf-8")

    assert unittest_discovery.manifest_free_unittest_proposal(tmp_path) is None
    assert _run(ensure_mechanical_toml(tmp_path, None)) is False
    assert has_mechanical_toml(tmp_path) is False


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


@pytest.mark.parametrize(
    ("child_source", "expected_count", "expect_proposal"),
    [
        ("class Tests(Base):\n    test_value = None\ndel Base\n", 0, False),
        ("class Base(Base):\n    test_value = None\n", 0, False),
        (
            "class Tests(Base):\n"
            "    @property\n    def test_value(self):\n        return None\ndel Base\n",
            0,
            False,
        ),
        (
            "class Tests(Base):\n    if True:\n        test_value = None\ndel Base\n",
            0,
            False,
        ),
        (
            "class Middle(Base):\n    test_value = None\n"
            "class Tests(Middle):\n    pass\ndel Base, Middle\n",
            0,
            False,
        ),
        ("class Tests(Base):\n    pass\ndel Base\n", 1, True),
        (
            "class Tests(Base):\n    test_value = None\n"
            "    def test_other(self):\n        pass\ndel Base\n",
            1,
            True,
        ),
        (
            "class Tests(Base):\n    test_value = None\n"
            "    def test_value(self):\n        pass\ndel Base\n",
            1,
            True,
        ),
        (
            "class Hidden:\n    test_value = None\n"
            "class Tests(Hidden, Base):\n    pass\ndel Base\n",
            0,
            False,
        ),
    ],
)
def test_inherited_test_names_respect_child_shadowing(
    tmp_path: Path, child_source: str, expected_count: int, expect_proposal: bool
) -> None:
    """Proposals follow known callable names, not an inherited has-tests flag."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_inherited.py").write_text(
        "import unittest\n"
        "class Base(unittest.TestCase):\n"
        "    def test_value(self):\n        pass\n" + child_source,
        encoding="utf-8",
    )
    actual = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    # Empty-suite exit codes vary across supported Python versions.
    expected_codes = {0} if expected_count else {0, 5}
    assert actual.returncode in expected_codes, actual.stderr
    assert f"Ran {expected_count} test" in actual.stderr
    proposal = unittest_discovery.manifest_free_unittest_proposal(tmp_path)
    assert (proposal is not None) is expect_proposal
    assert _run(ensure_mechanical_toml(tmp_path, None)) is expect_proposal
    assert has_mechanical_toml(tmp_path) is expect_proposal


@pytest.mark.asyncio
async def test_verification_artifacts_run_manifest_free_unittest_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-run Stage 1 consumer executes fallback tests when no provider exists."""
    _write_unittest(tmp_path / "tests" / "test_sample.py")
    monkeypatch.setattr(
        "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
        tmp_path / "artifacts",
    )

    def no_provider(**_kwargs):
        raise RuntimeError("no configured provider")

    monkeypatch.setattr("ouroboros.providers.factory.create_llm_adapter", no_provider)

    artifacts = await build_verification_artifacts("manifest-free", "done", tmp_path)

    manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
    assert len(manifest["runs"]) == 1
    run = manifest["runs"][0]
    assert run["check_type"] == "test"
    assert run["command"] == ["python3", "-m", "unittest", "discover", "-s", "tests"]
    assert run["passed"] is True
    assert "Ran 1 test" in run["final_outcome"]
    assert "OK" in run["final_outcome"]


@pytest.mark.parametrize("package_hook", [False, True])
def test_custom_load_tests_cannot_prove_statically_visible_tests_run(
    tmp_path: Path, package_hook: bool
) -> None:
    """unittest lets a hook replace the discovered tests with an empty suite."""
    tests = tmp_path / "tests"
    directory = tests / "unit" if package_hook else tests
    _write_unittest(directory / "test_hidden.py", fail=True)
    hook = (
        "import unittest\n"
        "def load_tests(loader, tests, pattern):\n"
        "    return unittest.TestSuite()\n"
    )
    if package_hook:
        (directory / "__init__.py").write_text(hook, encoding="utf-8")
    else:
        path = directory / "test_hidden.py"
        path.write_text(path.read_text(encoding="utf-8") + hook, encoding="utf-8")

    # This is a real unittest protocol: the failing method is not run.
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Ran 0 tests" in result.stderr
    assert _run(ensure_mechanical_toml(tmp_path, None)) is False
    assert has_mechanical_toml(tmp_path) is False


def test_separate_test_module_still_proves_a_suite_despite_custom_package_loader(
    tmp_path: Path,
) -> None:
    package = tmp_path / "tests" / "custom"
    _write_unittest(package / "test_hidden.py", fail=True)
    (package / "__init__.py").write_text(
        "import unittest\n"
        "def load_tests(loader, tests, pattern):\n"
        "    return unittest.TestSuite()\n",
        encoding="utf-8",
    )
    _write_unittest(tmp_path / "tests" / "test_visible.py")

    assert _run(ensure_mechanical_toml(tmp_path, None)) is True
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Ran 1 test" in result.stderr
