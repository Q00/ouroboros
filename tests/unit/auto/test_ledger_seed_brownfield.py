"""Brownfield pass-through tests for auto ledger Seed synthesis (PR-C)."""

from __future__ import annotations

from pathlib import Path

from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import (
    brownfield_context_from_cwd,
    synthesize_seed_from_ledger,
)
from ouroboros.core.seed import BrownfieldContext


def _complete_ledger(goal: str = "Build a CLI tool that prints hello.") -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(goal)
    fillers = {
        "actors": "End user invoking the CLI.",
        "inputs": "A single positional argument.",
        "outputs": "stdout greeting.",
        "constraints": "Pure Python.",
        "non_goals": "Daemon mode.",
        "acceptance_criteria": "CLI exits 0 and prints the greeting.",
        "verification_plan": "Run the CLI and assert stdout/exit code.",
        "failure_modes": "Missing argument raises a typed error.",
        "runtime_context": "Local POSIX shell.",
    }
    for section, value in fillers.items():
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.t",
                value=value,
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )
    return ledger


class TestBrownfieldContextFromCwd:
    def test_greenfield_when_no_config_files(self, tmp_path: Path) -> None:
        ctx = brownfield_context_from_cwd(tmp_path)
        assert ctx.project_type == "greenfield"
        assert ctx.context_references == ()

    def test_brownfield_when_manifest_present(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        ctx = brownfield_context_from_cwd(tmp_path)
        assert ctx.project_type == "brownfield"
        assert ctx.context_references[0].path == str(tmp_path)
        assert ctx.context_references[0].role == "primary"

    def test_none_cwd_is_greenfield(self) -> None:
        assert brownfield_context_from_cwd(None).project_type == "greenfield"


class TestSynthesizePassThrough:
    def test_default_seed_stays_greenfield(self) -> None:
        seed = synthesize_seed_from_ledger(_complete_ledger())
        assert seed.brownfield_context.project_type == "greenfield"

    def test_brownfield_context_carried_into_seed(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")
        ctx = brownfield_context_from_cwd(tmp_path)
        seed = synthesize_seed_from_ledger(_complete_ledger(), brownfield_context=ctx)
        assert seed.brownfield_context.project_type == "brownfield"
        assert seed.brownfield_context.context_references[0].path == str(tmp_path)

    def test_explicit_none_keeps_greenfield_default(self) -> None:
        seed = synthesize_seed_from_ledger(
            _complete_ledger(), brownfield_context=BrownfieldContext()
        )
        assert seed.brownfield_context.project_type == "greenfield"
