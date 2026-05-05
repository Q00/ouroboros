"""Unit tests for scripts/ralph.py — parse_evolve_text()."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

# Load ralph.py as a module without requiring its dependencies at import time.
_RALPH_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ralph.py"
_spec = importlib.util.spec_from_file_location("ralph", _RALPH_PATH)
assert _spec and _spec.loader
_ralph = importlib.util.module_from_spec(_spec)
# We need ralph in sys.modules so relative references work
sys.modules["ralph"] = _ralph
_spec.loader.exec_module(_ralph)

parse_evolve_text = _ralph.parse_evolve_text


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Fixtures — realistic evolve_step text outputs
# ---------------------------------------------------------------------------

CONTINUE_TEXT = """\
## Generation 2

**Action**: continue
**Phase**: reflect
**Convergence similarity**: 85.00%
**Reason**: Ontology still diverging
**Lineage**: lin_task_mgr (2 generations)
**Next generation**: 3

### Execution output
TaskManager created with 5 fields …

### Evaluation
- **Approved**: True
- **Score**: 0.88
- **Drift**: 0.12

### Wonder questions
- What about subtasks?
- Are permissions needed?

### Ontology delta (similarity: 85.00%)
- **Added**: projects (array)
- **Added**: tags (array)
"""

CONVERGED_TEXT = """\
## Generation 5

**Action**: converged
**Phase**: evaluate
**Convergence similarity**: 97.50%
**Reason**: Similarity above threshold
**Lineage**: lin_task_mgr (5 generations)
**Next generation**: 6
"""

STAGNATED_TEXT = """\
## Generation 4

**Action**: stagnated
**Phase**: reflect
**Convergence similarity**: 62.30%
**Reason**: No improvement for 3 generations
**Lineage**: lin_stuck (4 generations)
**Next generation**: 5
"""

MINIMAL_TEXT = """\
## Generation 1

**Action**: continue
**Convergence similarity**: 0.00%
**Next generation**: 2
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseEvolveText:
    """Test the regex parser for evolve_step text output."""

    def test_continue_action(self) -> None:
        result = parse_evolve_text(CONTINUE_TEXT)
        assert result["action"] == "continue"
        assert result["generation"] == 2
        assert result["similarity"] == pytest.approx(0.85, abs=1e-4)
        assert result["next_generation"] == 3
        assert result["lineage_id"] == "lin_task_mgr"

    def test_converged_action(self) -> None:
        result = parse_evolve_text(CONVERGED_TEXT)
        assert result["action"] == "converged"
        assert result["generation"] == 5
        assert result["similarity"] == pytest.approx(0.975, abs=1e-4)
        assert result["next_generation"] == 6
        assert result["lineage_id"] == "lin_task_mgr"

    def test_stagnated_action(self) -> None:
        result = parse_evolve_text(STAGNATED_TEXT)
        assert result["action"] == "stagnated"
        assert result["generation"] == 4
        assert result["similarity"] == pytest.approx(0.623, abs=1e-4)
        assert result["lineage_id"] == "lin_stuck"

    def test_minimal_text(self) -> None:
        result = parse_evolve_text(MINIMAL_TEXT)
        assert result["action"] == "continue"
        assert result["generation"] == 1
        assert result["similarity"] == pytest.approx(0.0, abs=1e-4)
        assert result["next_generation"] == 2
        # No lineage line → None
        assert result["lineage_id"] is None

    def test_empty_string(self) -> None:
        result = parse_evolve_text("")
        assert result["action"] is None
        assert result["generation"] is None
        assert result["similarity"] is None
        assert result["next_generation"] is None
        assert result["lineage_id"] is None

    def test_exhausted_action(self) -> None:
        text = """\
## Generation 20

**Action**: exhausted
**Phase**: reflect
**Convergence similarity**: 70.00%
**Reason**: Max generations reached
**Lineage**: lin_big (20 generations)
**Next generation**: 21
"""
        result = parse_evolve_text(text)
        assert result["action"] == "exhausted"
        assert result["generation"] == 20
        assert result["similarity"] == pytest.approx(0.70, abs=1e-4)

    def test_failed_action(self) -> None:
        text = """\
## Generation 3

**Action**: failed
**Phase**: execute
**Convergence similarity**: 50.00%
**Reason**: Execution error
**Lineage**: lin_fail (3 generations)
**Next generation**: 4
"""
        result = parse_evolve_text(text)
        assert result["action"] == "failed"
        assert result["generation"] == 3

    def test_high_precision_similarity(self) -> None:
        text = """\
## Generation 7

**Action**: continue
**Convergence similarity**: 99.99%
**Next generation**: 8
"""
        result = parse_evolve_text(text)
        assert result["similarity"] == pytest.approx(0.9999, abs=1e-4)

    def test_zero_similarity(self) -> None:
        text = """\
## Generation 1

**Action**: continue
**Convergence similarity**: 0.00%
**Next generation**: 2
"""
        result = parse_evolve_text(text)
        assert result["similarity"] == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# Project directory forwarding
# ---------------------------------------------------------------------------


class _TextContent:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolResult:
    isError = False

    def __init__(self, text: str) -> None:
        self.content = [_TextContent(text)]


class _FakeSession:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object]) -> _ToolResult:
        self.calls.append((name, arguments))
        if name == "ouroboros_lateral_think":
            return _ToolResult("lateral thinking applied")
        return _ToolResult(self._responses.pop(0))


def test_resolve_project_dir_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="does not exist"):
        _ralph._resolve_project_dir(str(missing))


def test_resolve_project_dir_rejects_non_git_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "not-a-repo"
    project_dir.mkdir()

    with pytest.raises(ValueError, match="git worktree"):
        _ralph._resolve_project_dir(str(project_dir))


def test_resolve_project_dir_normalizes_nested_path_to_repo_root(tmp_path: Path) -> None:
    project_dir = tmp_path / "target repo"
    nested_dir = project_dir / "nested" / "pkg"
    _init_git_repo(project_dir)
    nested_dir.mkdir(parents=True)

    assert _ralph._resolve_project_dir(str(nested_dir)) == str(project_dir.resolve())


@pytest.mark.asyncio
async def test_call_evolve_forwards_project_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "target repo"
    _init_git_repo(project_dir)
    session = _FakeSession([CONTINUE_TEXT])
    args = _ralph.build_parser().parse_args(
        ["--lineage-id", "lin_task_mgr", "--project-dir", str(project_dir)]
    )

    result = await _ralph._call_evolve(session, args)

    assert result["action"] == "continue"
    assert session.calls == [
        (
            "ouroboros_evolve_step",
            {"lineage_id": "lin_task_mgr", "project_dir": str(project_dir.resolve())},
        )
    ]


@pytest.mark.asyncio
async def test_stagnation_retry_preserves_project_dir_and_execution_flags(tmp_path: Path) -> None:
    project_dir = tmp_path / "target"
    _init_git_repo(project_dir)
    session = _FakeSession([STAGNATED_TEXT, CONTINUE_TEXT])
    args = _ralph.build_parser().parse_args(
        [
            "--lineage-id",
            "lin_stuck",
            "--project-dir",
            str(project_dir),
            "--no-execute",
            "--no-parallel",
            "--no-qa",
            "--max-retries",
            "1",
        ]
    )

    result = await _ralph._call_evolve(session, args)

    assert result["action"] == "continue"
    assert session.calls[0] == (
        "ouroboros_evolve_step",
        {
            "lineage_id": "lin_stuck",
            "project_dir": str(project_dir.resolve()),
            "execute": False,
            "parallel": False,
            "skip_qa": True,
        },
    )
    assert session.calls[1][0] == "ouroboros_lateral_think"
    assert session.calls[2] == (
        "ouroboros_evolve_step",
        {
            "lineage_id": "lin_stuck",
            "project_dir": str(project_dir.resolve()),
            "execute": False,
            "parallel": False,
            "skip_qa": True,
        },
    )
