"""Unit tests for canonical acceptance harness fixture loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from . import conftest as canonical_conftest
from .conftest import (
    _load_scenario,
    _runtime_is_inside_repo,
    format_canonical_summary_line,
)


def _scenario_dir(
    tmp_path: Path, *, goal: str = "Build a tiny CLI tool.", expected: str | None = None
) -> Path:
    scenario_dir = tmp_path / "broken-scenario"
    scenario_dir.mkdir()
    (scenario_dir / "goal.txt").write_text(goal, encoding="utf-8")
    if expected is not None:
        (scenario_dir / "expected.yaml").write_text(expected, encoding="utf-8")
    return scenario_dir


def _assert_load_fails(scenario_dir: Path, expected_message: str) -> None:
    with pytest.raises(pytest.fail.Exception, match=expected_message):
        _load_scenario(scenario_dir)


class _RecordingTerminalReporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def write_sep(self, sep: str, title: str) -> None:
        self.events.append(("sep", sep, title))

    def write_line(self, line: str) -> None:
        self.events.append(("line", line))


def test_load_scenario_rejects_missing_expected_yaml(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(tmp_path)

    _assert_load_fails(scenario_dir, "missing expected.yaml")


def test_load_scenario_rejects_empty_goal(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(
        tmp_path,
        goal="   \n",
        expected="domain_class: cli\ncompletion_mode: product_complete\n",
    )

    _assert_load_fails(scenario_dir, "empty goal.txt")


def test_load_scenario_rejects_unparseable_yaml(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(tmp_path, expected="domain_class: [unterminated\n")

    _assert_load_fails(scenario_dir, "expected.yaml does not parse")


def test_load_scenario_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(tmp_path, expected="- cli\n- product_complete\n")

    _assert_load_fails(scenario_dir, "top-level must be a mapping")


def test_load_scenario_rejects_missing_required_keys(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(tmp_path, expected="domain_class: cli\n")

    _assert_load_fails(scenario_dir, "missing required keys")


def test_load_scenario_rejects_invalid_completion_mode(tmp_path: Path) -> None:
    scenario_dir = _scenario_dir(
        tmp_path,
        expected="domain_class: cli\ncompletion_mode: almost_done\n",
    )

    _assert_load_fails(scenario_dir, "invalid completion_mode")


def test_format_canonical_summary_line_is_copyable() -> None:
    scenario = _load_scenario(Path(__file__).resolve().parent / "cli-todo")

    assert format_canonical_summary_line(scenario) == (
        "CANONICAL cli-todo: shape_valid "
        "domain=cli "
        "completion=product_complete "
        "probes=headless_run,stdout_golden "
        "budget=1800s "
        "live=available_opt_in"
    )


class TestRuntimeIsInsideRepo:
    """Regression coverage for the L0 runtime-preflight path-containment check.

    The original ``str.startswith`` implementation false-passed siblings
    that happened to share a textual prefix with the repo root (#1170 R2
    failure mode). Tests pin the boundary that
    ``_runtime_is_inside_repo`` MUST treat path components, not strings.
    """

    def test_runtime_inside_repo_root_accepts(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "ouroboros"
        repo_root.mkdir()
        runtime_file = repo_root / "src" / "ouroboros" / "__init__.py"

        assert _runtime_is_inside_repo(runtime_file, repo_root) is True

    def test_sibling_with_matching_string_prefix_rejected(self, tmp_path: Path) -> None:
        """Sibling ``ouroboros-old`` shares the string prefix ``ouroboros``
        but is NOT inside the repo. The original ``startswith`` check
        would have wrongly accepted it, masking a stale-binary run.
        """
        repo_root = tmp_path / "ouroboros"
        repo_root.mkdir()
        sibling_install = tmp_path / "ouroboros-old" / "src" / "ouroboros" / "__init__.py"

        assert _runtime_is_inside_repo(sibling_install, repo_root) is False

    def test_uvx_install_outside_repo_rejected(self, tmp_path: Path) -> None:
        """The exact #1170 R2 case: uvx-installed package outside repo."""
        repo_root = tmp_path / "Users" / "me" / "ouroboros"
        repo_root.mkdir(parents=True)
        uvx_install = (
            tmp_path
            / "Users"
            / "me"
            / ".local"
            / "share"
            / "uv"
            / "tools"
            / "ouroboros-ai"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "ouroboros"
            / "__init__.py"
        )

        assert _runtime_is_inside_repo(uvx_install, repo_root) is False


def test_pytest_terminal_summary_emits_copyable_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_dir = _scenario_dir(
        tmp_path,
        expected=(
            "domain_class: cli\n"
            "completion_mode: product_complete\n"
            "runtime_probe_kinds:\n"
            "  - headless_run\n"
            "wall_clock_budget_seconds: 42\n"
        ),
    )
    monkeypatch.setattr(
        canonical_conftest,
        "_iter_scenario_dirs",
        lambda: iter((scenario_dir,)),
    )
    terminalreporter = _RecordingTerminalReporter()

    canonical_conftest.pytest_terminal_summary(terminalreporter)  # type: ignore[arg-type]

    assert terminalreporter.events == [
        ("sep", "-", "canonical scenario summary"),
        (
            "line",
            "CANONICAL broken-scenario: shape_valid "
            "domain=cli "
            "completion=product_complete "
            "probes=headless_run "
            "budget=42s "
            "live=available_opt_in",
        ),
    ]
