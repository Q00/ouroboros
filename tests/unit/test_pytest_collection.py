"""Exercise repository marker and wheel scheduling contracts through pytest."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def collection_suite(pytester: pytest.Pytester) -> pytest.Pytester:
    """Load the real collection policy with a cheap wheel fixture stand-in."""
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    pytester.makeconftest(conftest.read_text(encoding="utf-8"))
    integration = pytester.path / "integration" / "nested"
    integration.mkdir(parents=True)
    (integration / "test_integration.py").write_text("def test_integration(): pass\n")
    pytester.makepyfile(test_unit="def test_unit(): pass")
    wheel = pytester.path / "wheel"
    wheel.mkdir()
    (wheel / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(scope='session')\n"
        "def built_wheel(tmp_path_factory):\n"
        "    wheel = tmp_path_factory.mktemp('built-wheel') / 'artifact.whl'\n"
        "    wheel.touch()\n"
        "    return wheel\n"
        "@pytest.fixture\n"
        "def wheel_contents(built_wheel):\n"
        "    return built_wheel\n"
    )
    for name in ("first", "second"):
        (wheel / f"test_{name}.py").write_text(
            f"def test_{name}(wheel_contents):\n    assert wheel_contents.is_file()\n"
        )
    return pytester


def test_integration_marker_selects_nested_directory_only(
    collection_suite: pytest.Pytester,
) -> None:
    # Given integration and unit modules, when selecting integration tests,
    result = collection_suite.runpytest_subprocess("-m", "integration", "-q")
    # Then descendants are selected without marking unrelated unit tests.
    result.assert_outcomes(passed=1, deselected=3)


def test_pr_selection_excludes_indirect_wheel_consumers(
    collection_suite: pytest.Pytester,
) -> None:
    # Given direct and transitive fixture dependencies, when using the PR filter,
    result = collection_suite.runpytest_subprocess("-m", "not performance and not slow", "-q")
    # Then no wheel-producing fixture runs.
    result.assert_outcomes(passed=2, deselected=2)
    assert not list(collection_suite.path.rglob("*.whl"))


def test_loadgroup_builds_one_artifact_across_workers(
    collection_suite: pytest.Pytester,
) -> None:
    # Given wheel consumers in separate modules, when using the CI scheduler,
    basetemp = collection_suite.path / "worker-output"
    result = collection_suite.runpytest_subprocess(
        "-n", "2", "--dist", "loadgroup", "--basetemp", str(basetemp), "-q"
    )
    # Then both assertions run against a single session fixture instance.
    result.assert_outcomes(passed=4)
    assert len(list(basetemp.rglob("artifact.whl"))) == 1
