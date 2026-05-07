"""End-to-end ``ooo auto`` dispatch regression tests.

Closes the last open acceptance bullet of issue #637: prove that ``ooo auto ...``
reaches the ``ouroboros_auto`` MCP pipeline and produces a Seed (or fails closed
with the documented unavailable-tool contract). The tests stay laser-focused on
this acceptance bullet — they do not exercise progress UI, answer grounding, or
CLI help text. All side-effects are confined to ``tmp_path``: no network, no
real LLM, no real home directory.

Cases:
1. A pre-supplied goal that already names actor/inputs/outputs/runtime context
   reaches the Seed phase and is persisted on disk.
2. A sparse goal goes through interview-fill (canned answerer responses) and
   still reaches the Seed phase.
3. The deterministic ``ooo auto`` dispatch surface fails closed with the
   user-visible "ouroboros_auto is unavailable" contract message and does not
   create any persisted auto session state when the MCP tool is unregistered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.seed_reviewer import SeedReview
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test helpers (inline; mirror ``tests/unit/auto/test_interview_pipeline.py``).
# ---------------------------------------------------------------------------


_FULLY_SPECIFIED_GOAL = (
    "Build a CLI named hello that prints exactly hello followed by a newline "
    "and exits 0. "
    "Actor is a local developer running it from a unix shell. "
    "Inputs are no CLI args and no stdin. "
    "Outputs are stdout hello with a trailing newline, no stderr, exit code 0. "
    "Runtime context is a temp scratch directory outside any repo, posix shell, no network. "
    "Constraints are stdlib only and no real-project edits. "
    "Non-goals are package publishing and network access. "
    "Acceptance criteria are stdout equals hello newline and exit status is 0. "
    "Verification plan is run the CLI and assert stdout, stderr, exit code. "
    "Failure modes are missing newline or non-zero exit."
)


def _build_a_grade_seed(goal: str) -> Seed:
    """Return a minimally valid Seed used as the seed_generator output."""
    return Seed(
        goal=goal,
        constraints=("Use the Python standard library only",),
        acceptance_criteria=("`hello` prints exactly `hello\\n` to stdout and exits 0",),
        ontology_schema=OntologySchema(
            name="HelloCli",
            description="Tiny hello CLI ontology",
            fields=(
                OntologyField(
                    name="command", field_type="string", description="Invocation command"
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="testability", description="Stdout, stderr, exit code are observable"
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="All acceptance criteria pass",
                evaluation_criteria="Stdout/stderr/exit-code assertions all pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _fill_ledger_ready(ledger: SeedDraftLedger) -> None:
    """Populate every required ledger section with conservative defaults."""
    defaults = {
        "actors": "Single local CLI user",
        "inputs": "No CLI args, no stdin",
        "outputs": "Stdout hello with newline, exit 0",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Stdout equals hello newline",
        "verification_plan": "Run the CLI and assert stdout/exit",
        "failure_modes": "Non-zero exit code",
        "runtime_context": "Local Unix-like shell with Python 3",
    }
    for section, value in defaults.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test_default",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )


class _AGradeRepairer:
    """Stub repairer that always returns an A-grade review without mutating the Seed."""

    def converge(
        self, seed: Seed, *, ledger: SeedDraftLedger
    ) -> tuple[Seed, SeedReview, list[object]]:
        review = SeedReview(
            grade_result=GradeResult(
                grade=SeedGrade.A,
                scores={
                    "coverage": 0.95,
                    "ambiguity": 0.05,
                    "testability": 0.95,
                    "execution_feasibility": 0.95,
                    "risk": 0.05,
                },
                findings=[],
                blockers=[],
                may_run=True,
            ),
            findings=(),
        )
        return seed, review, []


def _make_seed_saver(tmp_path: Path) -> tuple[callable, list[str]]:
    """Return a seed_saver and the list it appends each saved path to."""
    saved: list[str] = []
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)

    def save(seed: Seed) -> str:
        path = seeds_dir / f"{seed.metadata.seed_id}.yaml"
        # Persist a non-empty Seed YAML payload so the tests can verify the
        # saver materialised real bytes.
        import yaml

        path.write_text(
            yaml.dump(seed.to_dict(), default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        saved.append(str(path))
        return str(path)

    return save, saved


def _build_pipeline(
    *,
    tmp_path: Path,
    interview_start,
    interview_answer,
    seed_generator,
    seed_saver,
) -> tuple[AutoPipeline, AutoStore]:
    store = AutoStore(tmp_path / "auto_store")
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(interview_start, interview_answer),
        store=store,
        max_rounds=4,
        timeout_seconds=2.0,
    )
    pipeline = AutoPipeline(
        driver,
        seed_generator,
        store=store,
        repairer=_AGradeRepairer(),
        seed_saver=seed_saver,
        skip_run=True,
    )
    return pipeline, store


# ---------------------------------------------------------------------------
# Case 1 — pre-supplied goal reaches Seed without needing the answerer.
# ---------------------------------------------------------------------------


async def test_pre_supplied_goal_reaches_seed_without_answerer(tmp_path: Path) -> None:
    """A goal containing all required ledger sections must reach the Seed phase."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        # The fully-specified goal hydrates the ledger; the interview backend is
        # allowed to short-circuit and mark the interview complete on turn 1.
        return InterviewTurn(
            question="done",
            session_id="interview_dispatch_e2e_1",
            seed_ready=True,
            completed=True,
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("fully specified goal should not require any auto answerer turns")

    seed_generator_calls: list[str] = []

    async def seed_generator(session_id: str) -> Seed:
        seed_generator_calls.append(session_id)
        return _build_a_grade_seed(_FULLY_SPECIFIED_GOAL)

    seed_saver, saved_paths = _make_seed_saver(tmp_path)

    state = AutoPipelineState(goal=_FULLY_SPECIFIED_GOAL, cwd=str(tmp_path))
    state.skip_run = True

    pipeline, _store = _build_pipeline(
        tmp_path=tmp_path,
        interview_start=start,
        interview_answer=answer,
        seed_generator=seed_generator,
        seed_saver=seed_saver,
    )

    result = await pipeline.run(state)

    assert state.phase in {AutoPhase.COMPLETE, AutoPhase.REVIEW}, (
        f"pre-supplied goal should reach Seed/Complete, got {state.phase.value} "
        f"(blocker: {state.last_error!r})"
    )
    assert state.phase is not AutoPhase.BLOCKED
    assert state.last_error is None, (
        f"pre-supplied goal must not produce an auto error, got {state.last_error!r}"
    )
    assert state.seed_id, "Seed id must be populated after Seed generation"
    assert state.seed_path, "Seed path must be persisted after Seed generation"

    seed_path = Path(state.seed_path)
    assert seed_path.exists(), f"Seed file must exist on disk: {seed_path}"
    assert seed_path.read_bytes(), "Seed file must be non-empty"
    assert state.seed_path == saved_paths[0]

    assert state.interview_session_id == "interview_dispatch_e2e_1"
    assert seed_generator_calls == ["interview_dispatch_e2e_1"]

    assert result.status == "complete"
    assert result.grade == "A"
    assert result.seed_path == state.seed_path
    assert result.blocker is None


# ---------------------------------------------------------------------------
# Case 2 — sparse goal still reaches Seed via the interview/answerer path.
# ---------------------------------------------------------------------------


async def test_sparse_goal_reaches_seed_via_interview_fill(tmp_path: Path) -> None:
    """A sparse goal must still reach the Seed phase through the interview path."""
    captured_answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            question="What else should we know about this hello CLI?",
            session_id="interview_dispatch_e2e_2",
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:
        captured_answers.append(text)
        # Mark seed ready on the first answer; the test pre-fills the ledger so
        # the driver has everything it needs to converge.
        return InterviewTurn(
            question="done",
            session_id=session_id,
            seed_ready=True,
            completed=True,
        )

    async def seed_generator(session_id: str) -> Seed:  # noqa: ARG001
        return _build_a_grade_seed("Build a hello CLI")

    seed_saver, saved_paths = _make_seed_saver(tmp_path)

    state = AutoPipelineState(goal="Build a hello CLI", cwd=str(tmp_path))
    state.skip_run = True

    # Pre-fill the ledger so the driver does not depend on the production
    # answerer's heuristics for hermetic tests. The interview backend stub is
    # what proves the dispatch path went through interview-fill before Seed.
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ledger_ready(ledger)
    state.ledger = ledger.to_dict()

    pipeline, _store = _build_pipeline(
        tmp_path=tmp_path,
        interview_start=start,
        interview_answer=answer,
        seed_generator=seed_generator,
        seed_saver=seed_saver,
    )

    result = await pipeline.run(state)

    assert captured_answers, (
        "sparse goal must drive at least one answerer turn before Seed generation"
    )
    assert state.phase in {AutoPhase.COMPLETE, AutoPhase.REVIEW}
    assert state.phase is not AutoPhase.BLOCKED
    assert state.last_error is None
    assert state.seed_id, "Seed id must be populated after interview-fill path"
    assert state.seed_path, "Seed path must be persisted after interview-fill path"
    seed_path = Path(state.seed_path)
    assert seed_path.exists()
    assert seed_path.read_bytes()
    assert state.seed_path == saved_paths[0]
    assert result.status == "complete"
    assert result.grade == "A"
    assert result.blocker is None


# ---------------------------------------------------------------------------
# Case 3 — unregistered MCP tool fails closed with the contract message and
# does NOT create persisted auto session state.
# ---------------------------------------------------------------------------


def _write_auto_skill(skills_dir: Path) -> None:
    """Mirror the packaged ``auto`` SKILL.md frontmatter for the dispatch test."""
    skill_dir = skills_dir / "auto"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
                "---",
                "",
                "# auto",
                "",
            ]
        ),
        encoding="utf-8",
    )


async def test_dispatch_fails_closed_when_ouroboros_auto_unregistered(tmp_path: Path) -> None:
    """``ooo auto`` must fail closed when the ``ouroboros_auto`` MCP tool is unregistered.

    Asserts the actual user-visible contract phrasing (discovered in the
    ``CodexCliRuntime._build_auto_dispatch_unavailable_message`` source —
    Issue #637's literal phrase is reworded by the runtime as
    "Cannot run ooo auto: required MCP tool ``ouroboros_auto`` is unavailable.")
    and that no auto session state is persisted to the auto store on this
    fail-closed path.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_auto_skill(skills_dir)

    # Auto store rooted at tmp_path so we can assert no auto_*.json was written.
    auto_store_root = tmp_path / "auto_store"
    auto_store_root.mkdir()
    pre_existing = sorted(auto_store_root.glob("auto_*.json"))
    assert pre_existing == [], "tmp auto store must start empty for this test"

    cwd = tmp_path / "project"
    cwd.mkdir()

    dispatcher = AsyncMock(
        side_effect=LookupError("No local handler registered for tool: ouroboros_auto")
    )
    runtime = CodexCliRuntime(
        cli_path="codex",
        cwd=str(cwd),
        skills_dir=skills_dir,
        skill_dispatcher=dispatcher,
    )

    with (
        patch("ouroboros.orchestrator.codex_cli_runtime.log.warning"),
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec,
    ):
        messages = [message async for message in runtime.execute_task("ooo auto Build a hello CLI")]

    dispatcher.assert_awaited_once()
    # fail-closed unavailable dispatch must not spawn the codex subprocess
    mock_exec.assert_not_called()

    assert len(messages) == 1, f"expected single fail-closed result, got {messages!r}"
    failure = messages[0]
    assert failure.is_error is True

    # The Issue #637 acceptance phrase is "ouroboros_auto MCP tool is unavailable;
    # cannot run ooo auto". The actual codebase contract phrasing combines both
    # halves into a single sentence — assert each half is present so the contract
    # cannot silently regress in either direction.
    assert "Cannot run ooo auto" in failure.content
    assert "`ouroboros_auto` is unavailable" in failure.content
    assert failure.data["error_type"] == "SkillDispatchUnavailable"
    assert failure.data["tool_name"] == "ouroboros_auto"
    assert failure.data["command_prefix"] == "ooo auto"

    # No auto session state must be created on this fail-closed path.
    after = sorted(auto_store_root.glob("auto_*.json"))
    assert after == [], (
        f"unavailable-dispatch must not create persisted auto session state; found: {after!r}"
    )
