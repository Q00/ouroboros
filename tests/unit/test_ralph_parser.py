"""Unit tests for scripts/ralph.py — parse_evolve_text()."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
