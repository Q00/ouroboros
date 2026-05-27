"""Pytest fixtures for the canonical acceptance harness.

L0-a slice of #1170 — provides scenario discovery + per-scenario
fixture loading. The live ``ouroboros_auto`` invocation is available
through the explicit ``OUROBOROS_RUN_CANONICAL=1`` opt-in path; the
default CI path remains hermetic and validates fixture shape.

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

# Pytest stash key for the runtime-binary preflight result. Populated by
# ``assert_runtime_is_repo_source`` so live-run helpers can record the
# enforced runtime path/version alongside the MCP envelope (raw-passthrough
# evidence contract — see PR-γ docs and #1170 acceptance criteria).
_RUNTIME_PREFLIGHT_KEY = pytest.StashKey[dict[str, object]]()


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

    The default run is still the no-cost shape check, but the live
    ``ouroboros_auto`` path is available behind ``OUROBOROS_RUN_CANONICAL=1``.
    """
    probe_text = ",".join(scenario.runtime_probe_kinds) or "none"
    return (
        f"CANONICAL {scenario.slug}: shape_valid "
        f"domain={scenario.domain_class} "
        f"completion={scenario.completion_mode} "
        f"probes={probe_text} "
        f"budget={scenario.wall_clock_budget_seconds}s "
        f"live=available_opt_in"
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


def _runtime_is_inside_repo(runtime_file: Path, repo_root: Path) -> bool:
    """Return True iff ``runtime_file`` is contained under ``repo_root``.

    Uses real path-component containment (``is_relative_to``) rather than
    string-prefix matching. A sibling install at a path that happens to
    share the repo-root prefix (e.g. ``/Users/me/proj-old/...`` while the
    repo is ``/Users/me/proj/...``) MUST be rejected — that is the
    exact false-positive class this preflight is supposed to catch
    (#1170 R2 / R2-1709). Both paths are assumed resolved by the caller.
    """
    try:
        return runtime_file.is_relative_to(repo_root)
    except (ValueError, TypeError):
        return False


@pytest.fixture(scope="session", autouse=True)
def assert_runtime_is_repo_source(pytestconfig: pytest.Config) -> None:
    """L0 harness runtime-binary preflight (PR-γ / #1170).

    The canonical acceptance gate must exercise the repo's ouroboros source,
    not an arbitrary installed copy. #1170 R2 (20260526-1636) and R2-1709
    both produced false-positive BLOCKED evidence because the MCP server
    was importing uvx-installed 0.39.1 from
    ``/Users/.../uv/tools/ouroboros-ai/lib/...`` while the worktree carried
    0.39.2.devNN containing the substrate fixes under test. The harness
    must fail fast in that situation rather than emit acceptance evidence
    against the wrong binary.

    The check is opt-out via ``OUROBOROS_CANONICAL_SKIP_RUNTIME_CHECK=1``
    for the narrow case where a maintainer is deliberately validating
    against a published release (e.g. confirming a release-cut PR before
    tagging). In that mode the runtime path is still recorded — just not
    enforced.
    """
    import importlib
    import sys as _sys

    repo_root = Path(__file__).resolve().parents[2]
    skip = os.environ.get(
        "OUROBOROS_CANONICAL_SKIP_RUNTIME_CHECK", ""
    ).strip().lower() in {"1", "true", "yes"}
    try:
        module = importlib.import_module("ouroboros")
    except Exception as exc:  # pragma: no cover - imports always succeed in CI
        pytest.fail(
            "Canonical harness could not import ``ouroboros``. "
            f"Install the repo as editable first: `pip install -e {repo_root}`. "
            f"Underlying error: {exc!r}"
        )
    runtime_file = Path(module.__file__ or "").resolve()
    runtime_version = getattr(module, "__version__", "<unknown>")
    pytestconfig.stash.setdefault(
        _RUNTIME_PREFLIGHT_KEY,
        {
            "runtime_path": str(runtime_file),
            "runtime_version": runtime_version,
            "repo_root": str(repo_root),
            "enforced": not skip,
            "python_executable": _sys.executable,
        },
    )
    if skip:
        return
    if not _runtime_is_inside_repo(runtime_file, repo_root):
        pytest.fail(
            "L0 canonical harness must exercise repo source, not an installed copy.\n"
            f"  repo_root:    {repo_root}\n"
            f"  runtime_path: {runtime_file}\n"
            f"  runtime_ver:  {runtime_version}\n"
            f"  python_exec:  {_sys.executable}\n"
            "Fix one of:\n"
            f"  (a) `{_sys.executable} -m pip install -e {repo_root} --break-system-packages`\n"
            "      (re-installs ouroboros from this worktree onto the python\n"
            "       the MCP server / pytest uses), then restart this process.\n"
            "  (b) Opt out for a release-cut validation only:\n"
            "      `OUROBOROS_CANONICAL_SKIP_RUNTIME_CHECK=1 pytest ...`\n"
            "See #1170 acceptance criteria + PR-γ rationale."
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
