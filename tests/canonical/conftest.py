"""Pytest fixtures for the canonical acceptance harness.

L0-a slice of #1170 — provides scenario discovery + per-scenario
fixture loading. The actual ``ouroboros_auto`` invocation lands
in a follow-up sub-PR; this PR ships the discovery contract,
fixture-shape validation, and the runner skeleton.

Per-scenario fixture is parametrized via ``pytest_generate_tests`` so
adding a new ``tests/canonical/<slug>/`` directory automatically
extends test coverage with no code change.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path

import pytest
import yaml

_CANONICAL_ROOT = Path(__file__).resolve().parent
_REQUIRED_KEYS: frozenset[str] = frozenset({"domain_class", "completion_mode"})
_VALID_COMPLETION_MODES: frozenset[str] = frozenset({"code_complete", "product_complete"})
_DEFAULT_WALL_CLOCK_BUDGET_SECONDS = 7200
_LIVE_RUN_ENV_VAR = "OUROBOROS_RUN_CANONICAL"


@dataclass(frozen=True, slots=True)
class CanonicalScenario:
    """Frozen view of one ``tests/canonical/<slug>/`` directory.

    The runner consumes this; ``expected.yaml`` becomes ``metadata``
    after validation, so downstream test functions need not re-parse
    YAML.
    """

    slug: str
    directory: Path
    goal: str
    metadata: dict[str, object]

    @property
    def domain_class(self) -> str:
        value = self.metadata["domain_class"]
        assert isinstance(value, str)
        return value

    @property
    def completion_mode(self) -> str:
        value = self.metadata["completion_mode"]
        assert isinstance(value, str)
        return value

    @property
    def runtime_probe_kinds(self) -> tuple[str, ...]:
        value = self.metadata.get("runtime_probe_kinds", ())
        if not value:
            return ()
        assert isinstance(value, (list, tuple))
        out: list[str] = []
        for item in value:
            assert isinstance(item, str)
            out.append(item)
        return tuple(out)

    @property
    def wall_clock_budget_seconds(self) -> int:
        value = self.metadata.get("wall_clock_budget_seconds", _DEFAULT_WALL_CLOCK_BUDGET_SECONDS)
        assert isinstance(value, int)
        return value

    @property
    def env_dir(self) -> Path | None:
        candidate = self.directory / "env"
        if candidate.is_dir():
            return candidate
        return None


def format_canonical_summary_line(scenario: CanonicalScenario) -> str:
    """Return the copyable one-line status for a canonical scenario.

    The default canonical run remains a hermetic shape/catalog check, so
    the summary deliberately reports ``shape_valid`` instead of pretending
    PRODUCT_COMPLETE was exercised. ``live=opt_in`` records that the live
    ``ouroboros_auto`` path is wired but only runs when the maintainer sets
    ``OUROBOROS_RUN_CANONICAL=1``.
    """
    probe_text = ",".join(scenario.runtime_probe_kinds) or "none"
    return (
        f"CANONICAL {scenario.slug}: shape_valid "
        f"domain={scenario.domain_class} "
        f"completion={scenario.completion_mode} "
        f"probes={probe_text} "
        f"budget={scenario.wall_clock_budget_seconds}s "
        f"live=opt_in"
    )


def _iter_scenario_dirs() -> Iterator[Path]:
    """Yield each ``tests/canonical/<slug>/`` directory in stable order."""
    for entry in sorted(_CANONICAL_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("_", ".")):
            continue
        if entry.name == "__pycache__":
            continue
        # A scenario directory must contain at least a goal.txt to count.
        if not (entry / "goal.txt").is_file():
            continue
        yield entry


def _load_scenario(directory: Path) -> CanonicalScenario:
    """Read goal.txt + expected.yaml from *directory* and validate shape.

    Validation errors are raised as ``pytest.fail`` so the harness
    surfaces fixture rot as a test failure, not an import-time crash.
    """
    slug = directory.name
    goal_path = directory / "goal.txt"
    expected_path = directory / "expected.yaml"

    if not expected_path.is_file():
        pytest.fail(
            f"canonical scenario {slug!r} is missing expected.yaml at {expected_path}",
            pytrace=False,
        )

    goal = goal_path.read_text(encoding="utf-8").strip()
    if not goal:
        pytest.fail(
            f"canonical scenario {slug!r} has empty goal.txt at {goal_path}",
            pytrace=False,
        )

    try:
        raw_metadata = yaml.safe_load(expected_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        pytest.fail(
            f"canonical scenario {slug!r} expected.yaml does not parse: {exc}",
            pytrace=False,
        )

    if not isinstance(raw_metadata, dict):
        pytest.fail(
            f"canonical scenario {slug!r} expected.yaml top-level must be a mapping; "
            f"got {type(raw_metadata).__name__}",
            pytrace=False,
        )

    missing_keys = _REQUIRED_KEYS - raw_metadata.keys()
    if missing_keys:
        pytest.fail(
            f"canonical scenario {slug!r} expected.yaml is missing required keys: "
            f"{sorted(missing_keys)}",
            pytrace=False,
        )

    completion_mode = raw_metadata.get("completion_mode")
    if completion_mode not in _VALID_COMPLETION_MODES:
        pytest.fail(
            f"canonical scenario {slug!r} expected.yaml has invalid completion_mode "
            f"{completion_mode!r}; must be one of {sorted(_VALID_COMPLETION_MODES)}",
            pytrace=False,
        )

    return CanonicalScenario(
        slug=slug,
        directory=directory,
        goal=goal,
        metadata=dict(raw_metadata),
    )


@pytest.fixture(scope="session")
def canonical_scenarios() -> tuple[CanonicalScenario, ...]:
    """Return every discovered scenario as a frozen tuple, in stable order."""
    return tuple(_load_scenario(d) for d in _iter_scenario_dirs())


@pytest.fixture
def live_run_enabled() -> bool:
    """True iff the operator opted into the live-run path.

    The harness's two cost regimes:

    - ``OUROBOROS_RUN_CANONICAL`` unset → hermetic shape-check only.
      Validates fixture shape; never invokes ``ouroboros_auto``.
    - ``OUROBOROS_RUN_CANONICAL=1`` → live invocation. Costs real LLM
      tokens; the maintainer is expected to opt in explicitly.
    """
    return os.environ.get(_LIVE_RUN_ENV_VAR, "").strip() in {"1", "true", "yes"}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:  # type: ignore[name-defined]
    """Parametrize any test taking a ``scenario`` fixture over the discovered
    canonical scenarios.

    This lets ``test_canonical.py`` declare one test body per assertion
    and have pytest fan it out automatically across every
    ``tests/canonical/<slug>/`` directory.
    """
    if "scenario" not in metafunc.fixturenames:
        return
    scenarios = tuple(_load_scenario(d) for d in _iter_scenario_dirs())
    metafunc.parametrize(
        "scenario",
        scenarios,
        ids=[s.slug for s in scenarios],
    )


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:  # type: ignore[name-defined]
    """Emit one copyable status line per canonical scenario.

    This is the #1170 L0-a manual-reporting contract: after a maintainer
    runs ``pytest tests/canonical/ -v``, the terminal output contains a
    stable line that can be pasted into an SSOT or PR progress comment.
    """
    scenarios = tuple(_load_scenario(d) for d in _iter_scenario_dirs())
    if not scenarios:
        return
    terminalreporter.write_sep("-", "canonical scenario summary")
    for scenario in scenarios:
        terminalreporter.write_line(format_canonical_summary_line(scenario))
