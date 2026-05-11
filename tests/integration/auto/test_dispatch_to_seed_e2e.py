"""End-to-end ``ooo auto`` dispatch regression tests.

Closes the last open acceptance bullet of issue #637: prove that ``ooo auto ...``
reaches the ``ouroboros_auto`` MCP pipeline and produces a Seed (or fails closed
with the documented unavailable-tool contract). The tests stay laser-focused on
this acceptance bullet — they do not exercise progress UI, answer grounding, or
CLI help text. All side-effects are confined to ``tmp_path``: no network, no
real LLM, no real home directory.

Cases:
1. ``ooo auto "<fully specified goal>"`` enters through ``CodexCliRuntime.execute_task``,
   traverses ``resolve_skill_dispatch`` (packaged ``auto`` SKILL.md frontmatter +
   ``$goal``/``$CWD`` template normalization), reaches the real ``AutoHandler.handle``
   /``AutoHandler._run``, and yields a Seed-bearing result. Real LLM/MCP/subprocess
   side effects are stubbed *inside* the boundary (handlers + ``AutoPipeline``),
   not around it. A regression in the packaged frontmatter, dispatch arg shape,
   or ``AutoHandler`` wiring would make this test fail.
2. A sparse goal still reaches Seed via the interview-fill ``AutoPipeline``. This
   case stays a post-dispatch unit-style test (it does NOT enter via the runtime
   boundary) — it covers the ledger-hydration + seed-generator wiring landed in
   PR #652. Case 1 already covers the dispatch boundary itself.
3. The deterministic ``ooo auto`` dispatch surface fails closed with the
   user-visible "ouroboros_auto is unavailable" contract message and does not
   create any persisted auto session state when the MCP tool is unregistered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerSource, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
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
from ouroboros.mcp.tools import auto_handler as auto_handler_module
from ouroboros.mcp.tools.auto_handler import AutoHandler
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.router import Resolved

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test helpers (inline; mirror ``tests/unit/auto/test_interview_pipeline.py``).
# ---------------------------------------------------------------------------


# The dispatch tests resolve the *real* packaged ``skills/`` directory shipped
# from the repository root so a regression in ``skills/auto/SKILL.md`` is
# caught here, rather than against a synthetic in-test copy.
_PACKAGED_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"
_PACKAGED_AUTO_SKILL = _PACKAGED_SKILLS_DIR / "auto" / "SKILL.md"


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


def _make_seed_saver(tmp_path: Path) -> tuple[Any, list[str]]:
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
    interview_start: Any,
    interview_answer: Any,
    seed_generator: Any,
    seed_saver: Any,
    max_rounds: int = 4,
) -> tuple[AutoPipeline, AutoStore]:
    store = AutoStore(tmp_path / "auto_store")
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(interview_start, interview_answer),
        store=store,
        max_rounds=max_rounds,
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
# Case 1 — ``ooo auto`` enters via CodexCliRuntime, traverses the real
# router/AutoHandler dispatch boundary, and reaches the Seed phase.
# ---------------------------------------------------------------------------


class _StubAuthoringHandler:
    """Drop-in stub for ``InterviewHandler``/``GenerateSeedHandler``.

    AutoHandler builds these inside ``_run`` to drive the real authoring chain.
    The test never lets execution reach ``AutoPipeline.run``, so the handlers
    only need to satisfy attribute lookups and the matches-runtime check.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.agent_runtime_backend = kwargs.get("agent_runtime_backend")
        self.opencode_mode = kwargs.get("opencode_mode")
        # Mirror the real handler attributes touched by the AutoHandler
        # ``_handler_matches_runtime``/``_authoring_*_handler`` paths.
        self.interview_engine = None
        self.event_store = None
        self.llm_adapter = None
        self.llm_backend = kwargs.get("llm_backend")
        self.data_dir = None
        self.seed_generator = None


class _StubExecuteSeedHandler(_StubAuthoringHandler):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mcp_manager = kwargs.get("mcp_manager")
        self.mcp_tool_prefix = kwargs.get("mcp_tool_prefix", "")


class _StubStartExecuteSeedHandler:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.execute_handler = kwargs.get("execute_handler")
        self.event_store = kwargs.get("event_store")
        self.job_manager = kwargs.get("job_manager")
        self.agent_runtime_backend = kwargs.get("agent_runtime_backend")
        self.opencode_mode = kwargs.get("opencode_mode")


class _StubSeedRepairer:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _install_auto_handler_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    seed_path: str,
    captured: dict[str, Any],
) -> None:
    """Replace the in-process side-effecting deps inside ``AutoHandler._run``.

    The real ``AutoHandler.handle`` and ``AutoHandler._run`` still execute. Only
    the leaves that would otherwise spawn real LLM/MCP/subprocess work get
    swapped out: authoring handlers, ``SeedRepairer``, and ``AutoPipeline``.
    The stub ``AutoPipeline.run`` produces a complete A-grade ``AutoPipelineResult``
    so the runtime sees a Seed-bearing dispatch result.
    """

    monkeypatch.setattr(auto_handler_module, "InterviewHandler", _StubAuthoringHandler)
    monkeypatch.setattr(auto_handler_module, "GenerateSeedHandler", _StubAuthoringHandler)
    monkeypatch.setattr(auto_handler_module, "ExecuteSeedHandler", _StubExecuteSeedHandler)
    monkeypatch.setattr(
        auto_handler_module, "StartExecuteSeedHandler", _StubStartExecuteSeedHandler
    )
    monkeypatch.setattr(auto_handler_module, "SeedRepairer", _StubSeedRepairer)

    # Also replace the default AutoStore so no auto_*.json files leak into the
    # real $HOME/.ouroboros/data path.
    monkeypatch.setattr(
        auto_handler_module, "AutoStore", lambda: AutoStore(tmp_path / "auto_store_default")
    )

    class _StubAutoPipeline:
        def __init__(
            self,
            interview_driver: Any,
            seed_generator: Any,
            *,
            run_starter: Any = None,
            store: Any = None,
            repairer: Any = None,
            seed_saver: Any = None,
            seed_loader: Any = None,
            skip_run: bool = False,
            **_: Any,
        ) -> None:
            captured["interview_driver"] = interview_driver
            captured["seed_generator"] = seed_generator
            captured["run_starter"] = run_starter
            captured["store"] = store
            captured["repairer"] = repairer
            captured["seed_saver"] = seed_saver
            captured["seed_loader"] = seed_loader
            captured["skip_run"] = skip_run
            captured["complete_product"] = _.get("complete_product")

        async def run(self, state: AutoPipelineState) -> AutoPipelineResult:
            captured["state_goal"] = state.goal
            captured["state_cwd"] = state.cwd
            captured["state_skip_run"] = state.skip_run
            captured["state_complete_product"] = state.complete_product
            captured["state_pipeline_timeout_seconds"] = state.pipeline_timeout_seconds
            state.transition(AutoPhase.INTERVIEW, "stubbed interview start")
            state.interview_session_id = "interview_dispatch_e2e_runtime"
            state.transition(AutoPhase.SEED_GENERATION, "stubbed seed generation")
            state.seed_id = "seed_dispatch_e2e_runtime"
            state.seed_path = seed_path
            state.transition(AutoPhase.REVIEW, "stubbed seed review")
            state.last_grade = "A"
            state.transition(AutoPhase.COMPLETE, "stubbed auto pipeline complete")
            return AutoPipelineResult(
                status="complete",
                auto_session_id=state.auto_session_id,
                phase="complete",
                grade="A",
                seed_path=seed_path,
                interview_session_id=state.interview_session_id,
                last_grade="A",
            )

    monkeypatch.setattr(auto_handler_module, "AutoPipeline", _StubAutoPipeline)


def _make_auto_handler_dispatcher(
    handler: AutoHandler,
    *,
    intercepts: list[Resolved],
    arguments_log: list[dict[str, Any]],
) -> Any:
    """Build a ``skill_dispatcher`` that calls the real ``AutoHandler.handle``.

    The runtime hands us the resolved skill metadata (after frontmatter +
    template normalization). We forward the resolved ``mcp_args`` straight into
    ``AutoHandler.handle`` and lift the resulting ``MCPToolResult`` back into a
    final ``AgentMessage`` with the Seed metadata the runtime will yield.
    """

    async def dispatcher(intercept: Resolved, current_handle: Any) -> tuple[AgentMessage, ...]:
        intercepts.append(intercept)
        arguments = dict(intercept.mcp_args)
        arguments_log.append(arguments)
        result = await handler.handle(arguments)
        if result.is_err:
            raise result.error  # pragma: no cover - test should not hit this
        tool_result = result.value
        data: dict[str, Any] = {
            "subtype": "error" if tool_result.is_error else "success",
            "tool_name": intercept.mcp_tool,
            "mcp_meta": dict(tool_result.meta),
        }
        data.update(dict(tool_result.meta))
        return (
            AgentMessage(
                type="result",
                content=tool_result.text_content or f"{intercept.mcp_tool} completed.",
                data=data,
                resume_handle=current_handle,
            ),
        )

    return dispatcher


async def test_ooo_auto_dispatch_reaches_seed_via_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ooo auto <goal>`` reaches the Seed phase through the real runtime/router/AutoHandler chain.

    A regression in any of:
        (a) the packaged ``skills/auto/SKILL.md`` frontmatter (mcp_tool name,
            mcp_args keys, or template placeholders),
        (b) ``resolve_skill_dispatch`` template normalization (``$goal`` /
            ``$CWD``), or
        (c) ``AutoHandler.handle`` / ``AutoHandler._run`` wiring,
    will surface here as either a router NotHandled/InvalidSkill, a missing
    runtime intercept, or a missing Seed in the final dispatch result.

    The runtime is pointed at the *real* packaged ``skills/`` directory shipped
    from the repository root so a rename/drop in any frontmatter key (e.g.
    ``mcp_tool``, ``mcp_args.goal``, ``mcp_args.cwd``) regresses this test.
    """
    assert _PACKAGED_AUTO_SKILL.is_file(), (
        f"packaged auto SKILL.md must exist at {_PACKAGED_AUTO_SKILL}"
    )
    skills_dir = _PACKAGED_SKILLS_DIR

    cwd = tmp_path / "project"
    cwd.mkdir()

    # Pre-write a Seed file path that the stubbed AutoPipeline will return.
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    seed_path = seeds_dir / "seed_dispatch_e2e_runtime.yaml"
    seed_path.write_text("goal: stubbed\n", encoding="utf-8")

    captured: dict[str, Any] = {}
    _install_auto_handler_stubs(
        monkeypatch,
        tmp_path=tmp_path,
        seed_path=str(seed_path),
        captured=captured,
    )

    # Real AutoHandler with a tmp-rooted AutoStore so it never touches $HOME.
    auto_store = AutoStore(tmp_path / "auto_store")
    handler = AutoHandler(store=auto_store)

    intercepts: list[Resolved] = []
    arguments_log: list[dict[str, Any]] = []
    dispatcher = _make_auto_handler_dispatcher(
        handler, intercepts=intercepts, arguments_log=arguments_log
    )

    runtime = CodexCliRuntime(
        cli_path="codex",
        cwd=str(cwd),
        skills_dir=skills_dir,
        skill_dispatcher=dispatcher,
    )

    user_goal = "Build a hello CLI that prints hello and exits 0"
    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
    ) as mock_exec:
        messages = [
            message
            async for message in runtime.execute_task(
                f'ooo auto "{user_goal}" --complete-product --pipeline-timeout-seconds 600.5'
            )
        ]

    # The runtime must NOT spawn the codex subprocess for a successful skill
    # intercept — the dispatch path is the only thing under test.
    mock_exec.assert_not_called()

    # Frontmatter resolution + ``$goal``/``$CWD`` template substitution.
    assert intercepts, "skill_dispatcher must be awaited for ooo auto"
    intercept = intercepts[0]
    assert intercept.skill_name == "auto"
    assert intercept.mcp_tool == "ouroboros_auto"
    assert intercept.command_prefix == "ooo auto"

    # The runtime contract requires documented packaged auto placeholders to be
    # present and normalized before AutoHandler receives them.
    args = arguments_log[0]
    assert {
        "goal",
        "cwd",
        "complete_product",
        "pipeline_timeout_seconds",
        "user_preferences",
    } <= set(args.keys()), (
        "packaged ooo auto frontmatter must declare documented mcp_args; "
        f"got {sorted(args.keys())!r}"
    )
    assert args["goal"] == user_goal, (
        f"resolve_skill_dispatch must inject the user goal via $goal; got {args!r}"
    )
    assert args["cwd"] == str(cwd), (
        f"resolve_skill_dispatch must inject runtime cwd via $CWD; got {args!r}"
    )
    assert args["complete_product"] is True
    assert args["pipeline_timeout_seconds"] == 600.5
    assert isinstance(args["pipeline_timeout_seconds"], float)
    assert args["user_preferences"] == ""

    # AutoHandler._run actually executed: stub AutoPipeline observed the state.
    assert captured.get("state_goal") == user_goal, (
        "AutoHandler._run must construct AutoPipelineState with the dispatched goal"
    )
    assert captured.get("state_cwd") == str(cwd), (
        "AutoHandler._run must thread runtime cwd into AutoPipelineState"
    )
    assert captured.get("complete_product") is True
    assert captured.get("state_complete_product") is True
    assert captured.get("state_pipeline_timeout_seconds") == 600.5

    # The runtime must yield a single final result message carrying the Seed.
    assert len(messages) == 1, f"expected single dispatch result, got {messages!r}"
    final = messages[0]
    assert final.is_final
    assert not final.is_error, f"dispatch should succeed, got {final!r}"
    assert final.data.get("tool_name") == "ouroboros_auto"
    assert final.data.get("seed_path") == str(seed_path)
    assert final.data.get("status") == "complete"
    assert final.data.get("grade") == "A"
    assert final.data.get("phase") == "complete"


# ---------------------------------------------------------------------------
# Case 2 — sparse goal still reaches Seed via the interview/answerer path.
#
# This is intentionally a post-dispatch unit-style test: it constructs
# ``AutoPipeline`` directly to exercise the ledger-hydration + interview-fill
# wiring landed in PR #652. The dispatch boundary itself is covered by Case 1.
#
# Unlike a happy-path stub, this test:
#   * starts from a *bare* ledger (``SeedDraftLedger.from_goal`` only — no
#     pre-filled actors/inputs/outputs/runtime_context); the goal text is
#     deliberately sparse so the explicit-fact parser in ``from_goal`` cannot
#     pre-hydrate any required section,
#   * drives the real production ``AutoInterviewDriver`` + ``AutoAnswerer``
#     through ``FunctionInterviewBackend`` by returning question text that
#     routes to ``_io_actor_answer`` / ``_runtime_answer`` /
#     ``_non_goal_answer`` / ``_verification_answer`` etc.,
#   * only signals ``seed_ready=True`` *after* those sections are populated.
#
# A regression that drops the ledger-hydration step (e.g. the driver stops
# calling ``answerer.apply`` so ``ledger_updates`` never land in the ledger)
# would leave the four required sections empty, ``ledger.is_seed_ready()``
# would return False, and ``_handle_completed_turn`` would mark the state as
# ``BLOCKED`` rather than ``COMPLETE``. The post-run assertions below would
# then fire.
# ---------------------------------------------------------------------------


async def test_sparse_goal_reaches_seed_via_interview_fill(tmp_path: Path) -> None:
    """A sparse goal must still reach Seed via real interview-fill ledger hydration.

    The backend returns a fixed sequence of questions whose text triggers the
    production ``AutoAnswerer`` heuristics that hydrate ``actors``, ``inputs``,
    ``outputs``, ``runtime_context``, ``non_goals``, ``acceptance_criteria``,
    ``verification_plan``, and ``failure_modes`` from a deliberately sparse
    user goal. Only after those questions are exhausted does the backend
    signal ``seed_ready=True``.
    """
    # Sparse goal: no "Actor is...", "Inputs are...", "Outputs are...",
    # "Runtime context is..." labels. ``SeedDraftLedger.from_goal`` therefore
    # leaves all four sections empty -- the only way to reach Seed is for the
    # interview-fill path to populate them.
    sparse_goal = "Build a hello CLI"

    # Sanity check: a bare ``from_goal`` on this sparse text leaves the four
    # required sections empty. This guards the test from a future change to
    # ``from_goal`` silently pre-hydrating sections and turning case 2 back
    # into a false positive.
    bare_ledger = SeedDraftLedger.from_goal(sparse_goal)
    for section_name in ("actors", "inputs", "outputs", "runtime_context"):
        assert bare_ledger.sections[section_name].entries == [], (
            f"sparse goal must leave {section_name!r} empty after from_goal; "
            f"got {bare_ledger.sections[section_name].entries!r}"
        )

    # A fixed, ordered sequence of questions whose text routes through the
    # production ``AutoAnswerer`` to populate every required ledger section.
    # The driver also calls ``answerer.apply``, which records each Q/A in
    # ``ledger.question_history``, so we additionally assert the question
    # text propagated end-to-end.
    interview_questions = [
        "Who are the actors, inputs, and outputs for this task?",
        "Which runtime stack should we use?",
        "What conservative constraints and failure modes should bound this MVP?",
        "What non-goals should explicitly remain out of scope?",
        "Which command output verifies the acceptance criteria?",
    ]

    captured_answers: list[str] = []
    questions_emitted: list[str] = []
    turn_index = {"value": 0}

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        questions_emitted.append(interview_questions[0])
        return InterviewTurn(
            question=interview_questions[0],
            session_id="interview_dispatch_e2e_2",
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:
        captured_answers.append(text)
        turn_index["value"] += 1
        next_idx = turn_index["value"]
        if next_idx < len(interview_questions):
            next_question = interview_questions[next_idx]
            questions_emitted.append(next_question)
            return InterviewTurn(question=next_question, session_id=session_id)
        # All ledger-hydrating questions answered; the backend now signals
        # Seed-ready. The driver verifies ``ledger.is_seed_ready()`` itself
        # before transitioning out of INTERVIEW, so a regression in
        # ledger-hydration would still be caught here as a BLOCKED phase.
        return InterviewTurn(
            question="done",
            session_id=session_id,
            seed_ready=True,
            completed=True,
        )

    async def seed_generator(session_id: str) -> Seed:  # noqa: ARG001
        return _build_a_grade_seed(sparse_goal)

    seed_saver, saved_paths = _make_seed_saver(tmp_path)

    state = AutoPipelineState(goal=sparse_goal, cwd=str(tmp_path))
    state.skip_run = True
    # Critically: do NOT pre-fill ``state.ledger``. The pipeline must build
    # one from ``from_goal`` and rely on interview-fill to converge.
    assert state.ledger == {}, "state.ledger must start empty for the sparse-goal path"

    pipeline, _store = _build_pipeline(
        tmp_path=tmp_path,
        interview_start=start,
        interview_answer=answer,
        seed_generator=seed_generator,
        seed_saver=seed_saver,
        max_rounds=len(interview_questions) + 2,
    )

    result = await pipeline.run(state)

    # The backend produced every scripted question and the driver answered
    # each one before Seed generation began.
    assert questions_emitted == interview_questions, (
        f"interview backend must emit each scripted question before seed_ready; "
        f"got {questions_emitted!r}"
    )
    assert len(captured_answers) == len(interview_questions), (
        f"driver must answer every scripted question; got {captured_answers!r}"
    )
    for prefixed in captured_answers:
        assert prefixed.startswith("[from-auto]["), (
            f"answers must be source-tagged from production answerer; got {prefixed!r}"
        )

    # The pipeline reached Seed: phase moved past INTERVIEW into REVIEW/COMPLETE,
    # a Seed id was populated, and a Seed file was written.
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

    # The persisted ledger must show that the four required sections were
    # hydrated by the *answerer*, not the goal-derived defaults. If a
    # regression dropped ledger hydration, these sections would either be
    # empty (causing the driver to BLOCK before reaching here) or contain
    # only ``LedgerSource.USER_GOAL`` entries from ``from_goal``. We assert
    # the exact answerer-supplied entry keys/sources/values that the
    # production ``AutoAnswerer._io_actor_answer`` and ``_runtime_answer``
    # emit -- a regression that bypasses the answerer would change the
    # ``source`` field or drop these keys entirely.
    final_ledger = SeedDraftLedger.from_dict(state.ledger)
    expected_answerer_entries = {
        "actors": ("actors.single_local_user", LedgerSource.ASSUMPTION, "Single local user"),
        "inputs": (
            "inputs.explicit_arguments",
            LedgerSource.ASSUMPTION,
            "Explicit command/API arguments derived from the task goal",
        ),
        "outputs": (
            "outputs.stable_text_or_artifacts",
            LedgerSource.ASSUMPTION,
            "Stable text output or generated artifacts suitable for verification",
        ),
        "runtime_context": (
            "runtime.existing_project",
            LedgerSource.EXISTING_CONVENTION,
            None,  # value contents asserted via prefix below
        ),
    }
    for section_name, (
        expected_key,
        expected_source,
        expected_value,
    ) in expected_answerer_entries.items():
        section = final_ledger.sections[section_name]
        keys = [entry.key for entry in section.entries]
        assert expected_key in keys, (
            f"{section_name!r} must contain answerer-supplied entry {expected_key!r}; "
            f"got {keys!r} -- a regression in interview-fill ledger hydration would "
            f"drop this entry entirely"
        )
        matching = next(entry for entry in section.entries if entry.key == expected_key)
        assert matching.source == expected_source, (
            f"{section_name}.{expected_key} must come from {expected_source.value!r} "
            f"(answerer-supplied), not goal-derived defaults; got {matching.source.value!r}"
        )
        if expected_value is not None:
            assert matching.value == expected_value, (
                f"{section_name}.{expected_key} value must match the answerer's "
                f"deterministic output; got {matching.value!r}"
            )
    # Runtime-context entry is verbose; assert a stable prefix instead of the
    # full sentence so minor wording tweaks in the answerer do not regress.
    runtime_entry = next(
        entry
        for entry in final_ledger.sections["runtime_context"].entries
        if entry.key == "runtime.existing_project"
    )
    assert runtime_entry.value.startswith("Use the existing repository runtime,"), (
        f"runtime.existing_project value must come from _runtime_answer; got {runtime_entry.value!r}"
    )

    # The ledger must be Seed-ready end-to-end (no open gaps remain). This is
    # the same predicate the driver checks before leaving INTERVIEW; asserting
    # it here lets a regression that leaves any required section empty be
    # diagnosed as a hydration failure, not a downstream pipeline issue.
    assert final_ledger.is_seed_ready(), (
        f"interview-fill must leave the ledger Seed-ready; open_gaps={final_ledger.open_gaps()!r}"
    )

    # Finally: each scripted question must appear in the ledger's
    # ``question_history`` (recorded by ``answerer.apply``). This proves the
    # driver routed every backend question through the answerer instead of
    # silently skipping the hydration step.
    recorded_questions = [item["question"] for item in final_ledger.question_history]
    for question in interview_questions:
        assert question in recorded_questions, (
            f"answerer.apply must record question {question!r} in ledger history; "
            f"got {recorded_questions!r}"
        )


# ---------------------------------------------------------------------------
# Case 3 — unregistered MCP tool fails closed with the contract message and
# never reaches the downstream handler/store construction path.
# ---------------------------------------------------------------------------


async def test_dispatch_fails_closed_when_ouroboros_auto_unregistered(tmp_path: Path) -> None:
    """``ooo auto`` must fail closed when the ``ouroboros_auto`` MCP tool is unregistered.

    The dispatch surface returns a fail-closed ``AgentMessage``; no
    ``AutoHandler`` or ``AutoStore`` is constructed downstream because the
    dispatcher raises before the runtime hands off to any handler. We therefore
    rely on:

    * ``dispatcher.assert_awaited_once()`` — proves the runtime did reach the
      skill dispatch boundary, and
    * ``mock_exec.assert_not_called()`` — proves no codex subprocess was
      spawned and no real handler/store path was exercised,

    plus the user-visible contract phrasing from
    ``CodexCliRuntime._build_auto_dispatch_unavailable_message``.
    """
    assert _PACKAGED_AUTO_SKILL.is_file(), (
        f"packaged auto SKILL.md must exist at {_PACKAGED_AUTO_SKILL}"
    )
    skills_dir = _PACKAGED_SKILLS_DIR

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
    # fail-closed unavailable dispatch must not spawn the codex subprocess —
    # this also proves no downstream handler/AutoStore path was constructed.
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
