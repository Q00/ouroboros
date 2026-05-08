from __future__ import annotations

import asyncio

import pytest

from ouroboros.auto.adapters import PartialInterviewStartError
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.seed_reviewer import ReviewFinding, SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _fill_ready(ledger: SeedDraftLedger) -> None:
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )


def _seed(
    ac: tuple[str, ...] = ("`habit list` prints stable stdout containing created habits",),
) -> Seed:
    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=ac,
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )


def _fully_specified_hello_goal() -> str:
    return (
        "Produce only an A-grade Seed for a future tiny CLI. "
        "Actor is a local developer or automated agent. "
        "Inputs are no CLI arguments and no stdin. "
        "Outputs are exactly hello followed by one trailing newline on stdout, no stderr, exit code 0. "
        "Runtime context is a local Unix-like shell in a temporary scratch directory outside real projects. "
        "Constraints are Seed artifact only, Python 3 standard library only, and no real-project edits. "
        "Non-goals are implementation in this run, package publishing, external dependencies, network, auth, persistence, and real-project edits. "
        "Acceptance criteria are Seed artifact only, scratch repo isolation, exact stdout newline behavior, empty stderr, and exit status 0. "
        "Verification plan is future checks for stdout, stderr, and exit code without executing in this Seed-only run. "
        "Failure modes are real-project edits, execution during skip-run, missing exact output checks, or out-of-scope dependencies."
    )


def test_seed_draft_ledger_hydrates_explicit_goal_facts() -> None:
    ledger = SeedDraftLedger.from_goal(_fully_specified_hello_goal())

    assert ledger.is_seed_ready()
    statuses = ledger.section_statuses()
    for section in ("actors", "inputs", "outputs", "runtime_context"):
        assert statuses[section] == LedgerStatus.CONFIRMED
    assert "local developer" in ledger.sections["actors"].entries[-1].value
    assert "no CLI arguments" in ledger.sections["inputs"].entries[-1].value
    assert "hello" in ledger.sections["outputs"].entries[-1].value
    assert "temporary scratch directory" in ledger.sections["runtime_context"].entries[-1].value


def test_seed_draft_ledger_preserves_punctuation_inside_explicit_goal_facts() -> None:
    ledger = SeedDraftLedger.from_goal(
        "Actor is a local developer. "
        "Inputs are config path ./fixtures/hello.txt; use Python 3.11. "
        "Outputs are write ./out/hello.txt and print hello; goodbye. "
        "Runtime context is Python 3.11 on linux; cwd is /tmp/demo.v1. "
        "Constraints are stdlib only. "
        "Non-goals are network calls. "
        "Acceptance criteria are hello.txt exists and stdout is hello. "
        "Verification plan is run python3.11 ./hello.py. "
        "Failure modes are missing ./out/hello.txt."
    )

    assert ledger.is_seed_ready()
    inputs = ledger.sections["inputs"].entries[-1].value
    outputs = ledger.sections["outputs"].entries[-1].value
    runtime_context = ledger.sections["runtime_context"].entries[-1].value
    assert "./fixtures/hello.txt; use Python 3.11" in inputs
    assert "write ./out/hello.txt and print hello; goodbye" in outputs
    assert "Python 3.11 on linux; cwd is /tmp/demo.v1" in runtime_context
    assert "Constraints are" not in runtime_context


def test_seed_draft_ledger_ignores_inline_section_label_phrases() -> None:
    ledger = SeedDraftLedger.from_goal(
        "Actor is a local developer. "
        "Inputs are no CLI arguments. "
        "Outputs are stable stdout. "
        "Runtime context is local Python 3.11. "
        "Constraints are the Seed must mention acceptance criteria are important to reviewers. "
        "Non-goals are network calls. "
        "Acceptance criteria are stdout includes hello. "
        "Verification plan is run pytest. "
        "Failure modes are missing stdout assertion."
    )

    assert ledger.is_seed_ready()
    constraints = ledger.sections["constraints"].entries[-1].value
    acceptance_criteria = ledger.sections["acceptance_criteria"].entries[-1].value
    assert "acceptance criteria are important" in constraints
    assert acceptance_criteria == "stdout includes hello"


def test_seed_draft_ledger_hydrates_markdown_bulleted_goal() -> None:
    ledger = SeedDraftLedger.from_goal(
        "- Actor is a local developer\n"
        "- Inputs are no CLI arguments\n"
        "- Outputs are stable stdout\n"
        "- Runtime context is local Python 3.11\n"
        "- Constraints are stdlib only\n"
        "- Non-goals are network calls\n"
        "- Acceptance criteria are stdout includes hello\n"
        "- Verification plan is run pytest\n"
        "- Failure modes are missing stdout assertion"
    )

    assert ledger.is_seed_ready()
    assert "actors" not in ledger.open_gaps()
    assert ledger.sections["actors"].entries[-1].value == "a local developer"
    assert ledger.sections["inputs"].entries[-1].value == "no CLI arguments"


def test_seed_draft_ledger_uses_later_repeated_goal_label_as_correction() -> None:
    ledger = SeedDraftLedger.from_goal(
        "Actor is a local developer. "
        "Inputs are no CLI arguments. "
        "Outputs are json. "
        "Outputs are yaml. "
        "Runtime context is local Python 3.11. "
        "Constraints are stdlib only. "
        "Non-goals are network calls. "
        "Acceptance criteria are stdout includes hello. "
        "Verification plan is run pytest. "
        "Failure modes are missing stdout assertion."
    )

    assert ledger.is_seed_ready()
    outputs = ledger.sections["outputs"].entries
    assert [(entry.value, entry.status) for entry in outputs] == [
        ("json", LedgerStatus.WEAK),
        ("yaml", LedgerStatus.CONFIRMED),
    ]


def test_seed_draft_ledger_uses_later_repeated_non_goal_as_correction() -> None:
    ledger = SeedDraftLedger.from_goal(
        "Actor is a local developer. "
        "Inputs are no CLI arguments. "
        "Outputs are stable stdout. "
        "Runtime context is local Python 3.11. "
        "Constraints are stdlib only. "
        "Non-goals are network calls. "
        "Non-goals are network calls and package publishing. "
        "Acceptance criteria are stdout includes hello. "
        "Verification plan is run pytest. "
        "Failure modes are missing stdout assertion."
    )

    assert ledger.is_seed_ready()
    non_goals = ledger.sections["non_goals"].entries
    assert [(entry.value, entry.status) for entry in non_goals] == [
        ("network calls", LedgerStatus.WEAK),
        ("network calls and package publishing", LedgerStatus.CONFIRMED),
    ]


@pytest.mark.asyncio
async def test_interview_driver_finalizes_safe_defaults_after_max_rounds(tmp_path) -> None:
    answer_calls: list[tuple[str, str]] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:
        answer_calls.append((session_id, text))
        # Mirror the production interview handler: a synthesis answer that
        # carries the "mark the interview complete" completion signal
        # closes the transcript on the same turn.
        if "mark the interview complete" in text.lower():
            return InterviewTurn("", session_id, seed_ready=True, completed=True)
        return InterviewTurn("What else?", session_id, seed_ready=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.phase == AutoPhase.INTERVIEW
    assert state.interview_completed is True
    assert ledger.is_seed_ready()
    assert ledger.summary()["open_gaps"] == []
    final_actor = ledger.sections["actors"].entries[-1]
    assert final_actor.status == LedgerStatus.DEFAULTED
    assert final_actor.source == LedgerSource.ASSUMPTION
    assert any("safe-default policy" in item for item in final_actor.evidence)
    # Synthesis must be persisted to the interview transcript so the seed
    # generator (which reads the transcript) sees the same assumptions the
    # ledger now records — guards against the ledger/transcript split-brain.
    synthesis_text = next(
        text for _sid, text in answer_calls if "safe-default synthesis" in text.lower()
    )
    assert "mark the interview complete" in synthesis_text.lower()
    assert "actors" in synthesis_text
    assert any("safe-default" in item.get("answer", "").lower() for item in ledger.question_history)


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_safe_default_synthesis_rejected(tmp_path) -> None:
    call_count = 0

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return InterviewTurn("What else?", session_id, seed_ready=False)
        msg = "interview backend refuses post-bound synthesis"
        raise RuntimeError(msg)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    # Synthesis failure rolls back the safe-default entries so the canonical
    # "unresolved gaps" blocker is reported and the ledger reflects the
    # genuinely unresolved sections — preserving the convergence contract.
    assert "unresolved gaps" in (result.blocker or "")
    assert state.interview_completed is False
    assert ledger.open_gaps(), (
        "rolled-back ledger must expose at least one unresolved gap so the "
        "convergence contract still names actionable sections"
    )
    assert not any(
        entry.key.endswith(".safe_default_finalization")
        for section in ledger.sections.values()
        for entry in section.entries
    ), "safe-default policy entries must be reverted on synthesis failure"
    # Backend.answer raised after the first synthesis turn — the driver
    # cannot trust the cached pending_question because the backend may have
    # processed (or partially processed) the call. Force ``--resume`` to
    # query live state via ``backend.resume()`` instead of replaying.
    assert state.pending_question is None


def test_revert_safe_default_entries_preserves_user_keys_with_matching_suffix() -> None:
    """Regression: rollback must NOT remove a non-policy entry whose key
    coincidentally ends with ``.safe_default_finalization``.

    The earlier ``entry.key.endswith(".safe_default_finalization")`` filter
    would delete a user/answerer-authored ledger entry whose key just
    happens to share that suffix (for example, an answerer-synthesized
    constraint key ``constraints.my.safe_default_finalization``).
    Only the canonical key written by ``finalize_safe_defaultable_gaps``
    (``{section}.safe_default_finalization``) should be removed on rollback.
    """
    from ouroboros.auto.interview_driver import _revert_safe_default_entries
    from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus

    ledger = SeedDraftLedger.from_goal("Build a small CLI")

    # 1. The canonical safe-default policy entry — must be removed.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.safe_default_finalization",
            value="defaulted",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.DEFAULTED,
            rationale="policy",
            evidence=("provenance",),
        ),
    )
    # 2. A user-authored entry that ends with the same suffix but is NOT
    #    the canonical policy key. Must SURVIVE the rollback.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.my.safe_default_finalization",
            value="user-specified constraint",
            source=LedgerSource.USER_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
            rationale="user said so",
            evidence=("interview answer",),
        ),
    )
    # 3. An unrelated entry that does not match the suffix at all.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.other",
            value="unrelated",
            source=LedgerSource.USER_GOAL,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            rationale="control",
            evidence=("control",),
        ),
    )

    _revert_safe_default_entries(ledger, ("constraints",))

    remaining_keys = {entry.key for entry in ledger.sections["constraints"].entries}
    assert "constraints.safe_default_finalization" not in remaining_keys, (
        "the canonical safe-default policy entry MUST be removed on rollback"
    )
    assert "constraints.my.safe_default_finalization" in remaining_keys, (
        "a user-authored entry whose key shares the suffix MUST survive rollback"
    )
    assert "constraints.other" in remaining_keys


@pytest.mark.asyncio
async def test_interview_driver_finalizes_when_backend_requires_two_completion_signals(
    tmp_path,
) -> None:
    # The production interview handler closes the transcript only after a
    # streak of two qualifying completion signals. The driver must follow up
    # with additional confirmation turns until the backend confirms.
    completion_streak = 0
    answer_calls: list[tuple[str, str]] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:
        nonlocal completion_streak
        answer_calls.append((session_id, text))
        if "mark the interview complete" in text.lower():
            completion_streak += 1
            if completion_streak >= 2:
                return InterviewTurn("", session_id, seed_ready=True, completed=True)
            # Streak shortfall — backend asks the user to confirm again.
            return InterviewTurn("Type done once more", session_id, seed_ready=False)
        return InterviewTurn("What else?", session_id, seed_ready=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.interview_completed is True
    assert ledger.is_seed_ready()
    # Round-1 user answer + synthesis (turn 1) + confirmation (turn 2) = 3.
    completion_signal_calls = [
        text for _sid, text in answer_calls if "mark the interview complete" in text.lower()
    ]
    assert len(completion_signal_calls) >= 2, (
        "expected the driver to send at least two completion signals to satisfy "
        f"the streak contract; got {completion_signal_calls!r}"
    )


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_backend_ignores_synthesis_completion_signal(
    tmp_path,
) -> None:
    # The synthesis text carries a "mark the interview complete" signal; if
    # a backend ignores it and keeps asking questions, the persisted
    # transcript would diverge from auto state. The driver must block
    # rather than declaring seed_ready against a still-open transcript.
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        # Always return another question with seed_ready=False, regardless
        # of the answer text — simulating a backend that does not honour
        # the completion signal.
        return InterviewTurn("Anything else?", session_id, seed_ready=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    # Backend ignored the completion signal → synthesis failed → ledger is
    # rolled back and the canonical "unresolved gaps" blocker is emitted.
    assert "unresolved gaps" in (result.blocker or "")
    assert state.interview_completed is False
    assert ledger.open_gaps()
    # After synthesis failure the driver must leave pending_question pointing
    # at the backend's latest live prompt (or cleared) — never the stale
    # pre-synthesis question. A later ``--resume`` would otherwise replay an
    # already-answered prompt against an advanced session. The fake backend
    # always returns "Anything else?" after the synthesis turns, so that's
    # what the auto state should now hold.
    assert state.pending_question == "Anything else?"


def test_safe_default_blocks_when_interview_answer_introduces_unsafe_context() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small local CLI")
    ledger.record_qa(
        "How should the CLI authenticate?",
        "It needs to call the production OAuth provider with the customer access token.",
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a small local CLI",
        provenance="unit test",
    )

    assert not result.completed
    assert result.unsafe_gaps  # at least one gap stays unsafe
    assert any("unsafe default context" in gap for gap in result.unsafe_gaps)
    assert not ledger.is_seed_ready()


def test_safe_default_ignores_from_auto_answers_in_question_history() -> None:
    # AutoAnswerer prefixes every policy-emitted answer with "[from-auto]".
    # Those answers routinely mention auth/credentials/production as
    # exclusions; the unsafe gate must skip them so its own outputs do not
    # block subsequent finalization passes.
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.record_qa(
        "What boundary should we keep?",
        "[from-auto][conservative_default] Avoid authentication, credentials, "
        "and production deployment per the conservative default.",
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert result.completed
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


def test_safe_default_finalization_is_idempotent_against_its_own_synthesis() -> None:
    # Pushing the safe-default synthesis back through the interview transcript
    # records it as an answer in question_history. A second finalize call on
    # the same ledger must still succeed — the gate must recognize its own
    # synthesis (tagged with the [from-auto] prefix) as policy output, not
    # as new user-asserted unsafe context.
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)

    first = finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="unit test pass 1")
    assert first.completed
    assert ledger.is_seed_ready()

    # Simulate the interview driver appending the synthesis back into
    # question_history (what _record_safe_default_synthesis does).
    from ouroboros.auto.safe_defaults import build_safe_default_synthesis

    synthesis = build_safe_default_synthesis(first)
    assert synthesis  # sanity
    ledger.record_qa("auto safe-default finalization", synthesis)

    second = finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="unit test pass 2")
    # Nothing left to default on pass 2, and the synthesis must not have
    # poisoned the gate.
    assert second.unsafe_gaps == ()
    assert ledger.is_seed_ready()


def test_safe_default_blocks_when_conservative_default_entry_authorizes_unsafe_scope() -> None:
    # CONSERVATIVE_DEFAULT entries land with status DEFAULTED but still carry
    # user-derived scope — the unsafe gate must not silently treat them as
    # safe just because their status is DEFAULTED.
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime_context.conservative_default",
            value="Deploys to production with customer credentials as the conservative default.",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.6,
            status=LedgerStatus.DEFAULTED,
            rationale="Recorded by an earlier auto round.",
        ),
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert not result.completed
    assert any("unsafe default context" in gap for gap in result.unsafe_gaps)
    assert not ledger.is_seed_ready()


def test_safe_default_blocks_when_non_user_goal_entry_introduces_unsafe_context() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small local CLI")
    ledger.add_entry(
        "inputs",
        LedgerEntry(
            key="inputs.repo_fact",
            value="Reads the production database credentials file from disk.",
            source=LedgerSource.REPO_FACT,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            rationale="Surfaced from repo scan during interview.",
        ),
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a small local CLI",
        provenance="unit test",
    )

    assert not result.completed
    assert any("unsafe default context" in gap for gap in result.unsafe_gaps)
    assert not ledger.is_seed_ready()


@pytest.mark.parametrize(
    "goal",
    [
        "Reproduce a production bug locally with the existing test fixtures",
        "Use the production schema snapshot already in the repo",
        "Replay a captured production trace against the local server",
        "Document the prod logging format in the developer guide",
        "Compile the live preview build for local QA",
    ],
)
def test_safe_default_allows_benign_production_mentions(goal: str) -> None:
    ledger = SeedDraftLedger.from_goal(goal)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert result.completed, (
        f"goal {goal!r} only describes local read-only context; "
        "finalization must not block on bare production/prod/live mentions"
    )
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


@pytest.mark.parametrize(
    "goal",
    [
        # Fullwidth Latin block (U+FF21..U+FF5A) — visually identical to
        # ASCII for end users and routinely produced by IMEs that round-trip
        # through CJK keyboards. The unsafe-context "external side effect"
        # arm matches the action verbs (``deploy``/``release``/``publish``/
        # ``go live``/``push live``/``database migration``/...). Without
        # NFKC normalization those alternations cannot see the fullwidth
        # form, and the gate silently authorizes a production cutover.
        "ｄｅｐｌｏｙ to ｐｒｏｄｕｃｔｉｏｎ this Friday",
        "ｒｅｌｅａｓｅ version 2 to ｐｒｏｄ",
        # Compatibility ligature (U+FB01 ``ﬁ``) paired with a real
        # production-action verb so the regression covers normalization on
        # the verb-token side as well.
        "ﬁnalize ｄｅｐｌｏｙ to production tomorrow",
    ],
)
def test_safe_default_blocks_unicode_compat_production_actions(goal: str) -> None:
    """Fullwidth/ligature Unicode must not bypass the unsafe-context regex bank.

    The relevant arm is the "ambiguous external side effect" pattern,
    which matches the action verbs (``deploy``/``release``/``publish``/
    ``send email``/``webhook``/``database migration``/``go live``/
    ``push live``/...). Bare ``production``/``prod``/``live`` is *not*
    by itself flagged any more — the verb token is what carries the
    block decision. Without ``unicodedata.normalize("NFKC", context)``
    before the ``re.search`` calls in ``_unsafe_context_reason``, those
    verb alternations cannot see fullwidth Latin (``ｄｅｐｌｏｙ``) or
    ligature (``ﬁ``) variants, and the safe-default policy
    auto-defaults a session that actually authorizes a production
    deploy.
    """
    ledger = SeedDraftLedger.from_goal(goal)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert not result.completed, (
        f"NFKC-equivalent goal {goal!r} authorizes a production action; finalization must block"
    )
    assert any("external side effect" in gap.lower() for gap in result.unsafe_gaps)
    assert not ledger.is_seed_ready()


@pytest.mark.parametrize(
    "goal",
    [
        "Deploy the new service to production for the launch event",
        "Release version 2 to prod after the freeze",
        "Push live the cutover migration on Friday",
        "Going live with the rebuilt checkout flow next week",
    ],
)
def test_safe_default_blocks_genuine_production_actions(goal: str) -> None:
    ledger = SeedDraftLedger.from_goal(goal)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert not result.completed, (
        f"goal {goal!r} authorizes a production-class action; finalization must block"
    )
    assert any("external side effect" in gap.lower() for gap in result.unsafe_gaps)
    assert not ledger.is_seed_ready()


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI with no external dependencies",
        "Use existing external API schema files only",
        "Sync against external integration documentation already vendored in the repo",
    ],
)
def test_safe_default_allows_benign_external_mentions(goal: str) -> None:
    ledger = SeedDraftLedger.from_goal(goal)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert result.completed
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


@pytest.mark.parametrize(
    "answer",
    [
        "No production deployment.",
        "No authentication required.",
        "Do not use customer credentials.",
        "Never deploy to production.",
        "We avoid OAuth and skip billing.",
        "No auth, credentials, and production deployment.",
        "Without payment processing or webhook callbacks.",
    ],
)
def test_safe_default_respects_negated_unsafe_terms(answer: str) -> None:
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.record_qa("How should this work?", answer)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert result.completed, f"negated answer {answer!r} should not block finalization"
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


def test_safe_default_still_blocks_when_negation_does_not_cover_unsafe_term() -> None:
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    # Contrastive conjunctions cancel the negation scope — the second half is
    # a positive assertion and must still flag.
    ledger.record_qa(
        "Walk me through the auth model.",
        "No prod deploys, but log credentials in env so the daemon can read them.",
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert not result.completed
    assert any("unsafe default context" in gap for gap in result.unsafe_gaps)


@pytest.mark.parametrize(
    ("answer", "expected_reason_substring"),
    [
        # Comma + imperative verb breaks the negation scope, leaving the
        # second clause visible to the unsafe regex bank.
        (
            "No production deploys, use customer credentials from Vault.",
            "credentials",
        ),
        (
            "Without billing integration, send email notifications to customers.",
            "external side effect",
        ),
        # Comma + noun-led counter-clause (no imperative verb) also breaks
        # the scope when the sentence has no list connector — the second
        # clause is a positive assertion that must still flag.
        (
            "No production deploys, customer credentials from Vault are still required.",
            "credentials",
        ),
        # Semicolon also breaks the scope.
        (
            "No external API; deploy to production for the first launch.",
            "external side effect",
        ),
    ],
)
def test_safe_default_blocks_when_negation_does_not_cover_subsequent_clause(
    answer: str, expected_reason_substring: str
) -> None:
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.record_qa("How should this work?", answer)

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert not result.completed, (
        f"answer {answer!r} contains a positively asserted clause; "
        "finalization must not auto-default"
    )
    joined = "\n".join(result.unsafe_gaps).lower()
    assert expected_reason_substring.lower() in joined, (
        f"expected unsafe reason matching {expected_reason_substring!r} for {answer!r}, "
        f"got {result.unsafe_gaps!r}"
    )
    assert not ledger.is_seed_ready()


def test_safe_default_treats_confirmed_non_goals_as_exclusions_not_unsafe_scope() -> None:
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    ledger.add_entry(
        "non_goals",
        LedgerEntry(
            key="non_goals.user_excludes",
            value="auth, credentials, and production deployment",
            source=LedgerSource.NON_GOAL,
            confidence=0.95,
            status=LedgerStatus.CONFIRMED,
            rationale="User explicitly ruled these out during the interview.",
        ),
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
    )

    assert result.completed
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


def test_safe_default_ignores_unsafe_terms_in_interview_questions() -> None:
    goal = "Build a small local CLI"
    ledger = SeedDraftLedger.from_goal(goal)
    # The backend asked about authentication and production deployment, but
    # the user explicitly answered no. Only answers carry user intent — the
    # question alone must not poison finalization.
    ledger.record_qa(
        "How should this authenticate? Does it deploy to production?",
        "No auth and local-only execution; nothing leaves the machine.",
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=goal,
        provenance="unit test",
        pending_question="Does this require any production credentials?",
    )

    assert result.completed
    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


def test_safe_default_non_goals_do_not_make_later_finalization_unsafe() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small local CLI")
    ledger.add_entry(
        "non_goals",
        LedgerEntry(
            key="non_goals.safe_boundary",
            value="Do not perform credential handling, billing, or production deployment.",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.DEFAULTED,
            rationale="Conservative scope boundary recorded by auto policy.",
        ),
    )

    result = finalize_safe_defaultable_gaps(
        ledger,
        goal="Build a small local CLI",
        provenance="unit test",
    )

    assert result.unsafe_gaps == ()
    assert ledger.is_seed_ready()


@pytest.mark.asyncio
async def test_interview_driver_keeps_unsafe_gaps_blocking_after_max_rounds(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else?", session_id, seed_ready=False)

    state = AutoPipelineState(
        goal="Deploy the service to production and configure the required credentials",
        cwd=str(tmp_path),
    )
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "unresolved gaps" in (result.blocker or "")
    assert "unsafe default context" in (result.blocker or "")
    assert not ledger.is_seed_ready()


@pytest.mark.asyncio
async def test_interview_driver_blocks_on_backend_timeout(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        await asyncio.sleep(0.05)
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        timeout_seconds=0.001,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "timed out" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_timeout_message_records_state_policy_source(tmp_path) -> None:
    """Regression for #686: timeout error must report seconds + state-policy source."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        await asyncio.sleep(0.05)
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        timeout_seconds=0.001,
    )

    result = await driver.run(state, ledger)

    blocker = result.blocker or ""
    assert "interview.start timed out after 0s" in blocker
    assert "policy: state.timeout_seconds_by_phase[interview]" in blocker
    assert state.last_error == blocker


@pytest.mark.asyncio
async def test_interview_driver_supplies_bounded_repo_facts_to_answerer(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-cli"',
                'requires-python = ">=3.12"',
                'dependencies = ["typer>=0.12"]',
                "",
                "[build-system]",
                'build-backend = "hatchling.build"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Which runtime and framework should we use?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    ledger.sections["runtime_context"].entries.clear()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert answers and "[repo_fact]" in answers[0]
    runtime_entries = ledger.sections["runtime_context"].entries
    repo_entry = next(entry for entry in runtime_entries if entry.key == "runtime.repo_fact")
    assert repo_entry.source == LedgerSource.REPO_FACT
    assert repo_entry.status == LedgerStatus.CONFIRMED
    assert repo_entry.evidence == ["pyproject.toml", "src/", "tests/"]
    assert "Python project requiring >=3.12" in repo_entry.value
    assert "Typer CLI" in repo_entry.value


def test_repo_context_keeps_partial_project_hints_out_of_confirmed_runtime(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "tool-only"',
                "",
                "[build-system]",
                'build-backend = "hatchling.build"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["package_manager"] == "hatchling/pyproject"
    assert context.repo_facts["project_structure"] == "src layout with tests directory"


@pytest.mark.asyncio
async def test_interview_driver_keeps_runtime_default_without_repo_facts(tmp_path) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Which runtime and framework should we use?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    ledger.sections["runtime_context"].entries.clear()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert answers and "[existing_convention]" in answers[0]
    runtime_entries = ledger.sections["runtime_context"].entries
    assert runtime_entries[-1].source == LedgerSource.EXISTING_CONVENTION
    assert runtime_entries[-1].status == LedgerStatus.DEFAULTED
    assert runtime_entries[-1].evidence == []


@pytest.mark.asyncio
async def test_pipeline_repairs_b_seed_to_a_and_starts_run(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ac=("The CLI should be easy and user-friendly",))

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": "job_1", "execution_id": "exec_1", "session_id": "session_1"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    repaired_acceptance = state.seed_artifact["acceptance_criteria"][0]
    assert "The CLI" in repaired_acceptance
    assert "stable observable output" in repaired_acceptance
    assert (
        repaired_acceptance
        != "A command/API check returns stable observable output or artifacts proving this requirement."
    )
    assert result.job_id == "job_1"
    assert result.run_session_id == "session_1"
    assert state.execution_id == "exec_1"
    assert state.run_session_id == "session_1"


def test_seed_repairer_rewrites_each_acceptance_criterion_once() -> None:
    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    ledger = SeedDraftLedger.from_goal(seed.goal)
    _fill_ready(ledger)
    review = SeedReviewer().review(seed, ledger=ledger)

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    repaired_acceptance = result.seed.acceptance_criteria[0]
    assert repaired_acceptance.count("original requirement for") == 1
    assert "The CLI" in repaired_acceptance
    assert "original requirement for A command/API check" not in repaired_acceptance


def test_seed_repairer_assigns_new_seed_identity_after_mutation() -> None:
    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    ledger = SeedDraftLedger.from_goal(seed.goal)
    _fill_ready(ledger)
    review = SeedReviewer().review(seed, ledger=ledger)

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    assert result.seed.metadata.seed_id != seed.metadata.seed_id
    assert result.seed.metadata.parent_seed_id == seed.metadata.seed_id


def test_seed_repairer_non_goals_do_not_contradict_goal_scope() -> None:
    seed = _seed()
    ledger = SeedDraftLedger.from_goal("Add authentication and deploy this service to production")
    finding = ReviewFinding.from_parts(
        code="missing_non_goals",
        target="non_goals",
        severity="medium",
        message="Auto-generated Seed has no explicit non-goals",
        repair_instruction="Add MVP non-goals to bound scope.",
    )
    review = SeedReview(
        grade_result=GradeResult(
            grade=SeedGrade.B,
            scores={
                "coverage": 0.8,
                "ambiguity": 0.1,
                "testability": 0.9,
                "execution_feasibility": 0.9,
                "risk": 0.1,
            },
            findings=[],
            blockers=[],
            may_run=False,
        ),
        findings=(finding,),
    )

    result = SeedRepairer().repair_once(seed, review, ledger=ledger)

    assert result.changed
    non_goals = ledger.non_goals()
    assert non_goals
    assert "authentication" not in non_goals[0].lower()
    assert "production deployment" not in non_goals[0].lower()


def test_seed_repairer_converge_returns_latest_repair_when_high_findings_repeat() -> None:
    original_seed_id: str | None = None
    finding = ReviewFinding.from_parts(
        code="vague_acceptance_criteria",
        target="acceptance_criteria[0]",
        severity="high",
        message="Still vague",
        repair_instruction="Make it observable.",
    )

    class RepeatingReviewer:
        def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:  # noqa: ARG002
            coverage = 0.1 if seed.metadata.seed_id == original_seed_id else 0.9
            return SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.B,
                    scores={
                        "coverage": coverage,
                        "ambiguity": 0.2,
                        "testability": 0.5,
                        "execution_feasibility": 0.8,
                        "risk": 0.1,
                    },
                    findings=[],
                    blockers=[],
                    may_run=False,
                ),
                findings=(finding,),
            )

    seed = _seed(ac=("The CLI should be easy and user-friendly",))
    original_seed_id = seed.metadata.seed_id
    repaired, final_review, history = SeedRepairer(
        reviewer=RepeatingReviewer(), max_repair_rounds=3
    ).converge(seed)

    assert history
    assert repaired == history[-1].seed
    assert repaired != seed
    assert final_review.grade_result.scores["coverage"] == 0.9


@pytest.mark.asyncio
async def test_pipeline_skip_run_stops_after_a_grade_seed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert result.job_id is None


@pytest.mark.asyncio
async def test_pipeline_uses_explicit_goal_facts_before_completed_interview(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_hello", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("fully specified completed interview should not need another answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(
            ac=("`python hello.py` prints exactly `hello\\n` to stdout and exits 0",)
        ).model_copy(update={"goal": state.goal})

    saved: list[str] = []

    def save(seed: Seed) -> str:
        path = str(tmp_path / f"{seed.metadata.seed_id}.yaml")
        saved.append(path)
        return path

    state = AutoPipelineState(goal=_fully_specified_hello_goal(), cwd=str(tmp_path))
    state.skip_run = True
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_saver=save,
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert result.seed_path == saved[0]
    assert state.phase == AutoPhase.COMPLETE
    assert state.seed_id is not None
    assert state.seed_path == saved[0]
    assert state.last_grade == "A"
    assert state.job_id is None


@pytest.mark.asyncio
async def test_interview_resume_uses_persisted_pending_question(tmp_path) -> None:
    calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        calls.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "resume interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What should we verify?"
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert calls
    assert "Continue from persisted" not in calls[0]


@pytest.mark.asyncio
async def test_pipeline_non_interview_resume_blocks_without_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("pipeline should not re-enter interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("pipeline should not re-enter interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume without seed artifact should block")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "without persisted Seed artifact" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_resume_backend_error_blocks_and_persists(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer without a question")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "resume interview")
    state.interview_session_id = "interview_1"
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "interview resume failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_seed_generator_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise RuntimeError("generator exploded")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed generation failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_run_starter_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise RuntimeError("runner exploded")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "run start failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_serializes_blocking_review_findings(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ac=("The command uses clean architecture",))

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()

    class BlockingRepairer:
        def converge(
            self, seed: Seed, *, ledger: SeedDraftLedger
        ) -> tuple[Seed, SeedReview, list[object]]:  # noqa: ARG002
            finding = ReviewFinding.from_parts(
                code="still_vague",
                target="acceptance_criteria[0]",
                severity="high",
                message="Still not observable",
                repair_instruction="Make it observable.",
            )
            review = SeedReview(
                grade_result=GradeResult(
                    grade=SeedGrade.B,
                    scores={
                        "coverage": 0.8,
                        "ambiguity": 0.3,
                        "testability": 0.4,
                        "execution_feasibility": 0.8,
                        "risk": 0.2,
                    },
                    findings=[],
                    blockers=[],
                    may_run=False,
                ),
                findings=(finding,),
            )
            return seed, review, []

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), repairer=BlockingRepairer(), skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.findings
    assert "fingerprint" in state.findings[0]


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_backend_never_marks_ready(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Another question", session_id, seed_ready=False, completed=False)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "before backend marked" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_review_from_persisted_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_resumes_completed_interview_without_reanswering(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer again")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_resume_retries_unknown_run_handoff_once(tmp_path) -> None:
    """Resuming a session with an unknown handoff retries the run starter exactly once."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    keys: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        keys.append(idempotency_key)
        return {"job_id": "job_after_retry", "execution_id": "exec_after_retry"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    state.run_handoff_status = "unknown_no_handle"
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_after_retry"
    assert keys == [state.auto_session_id]


@pytest.mark.asyncio
async def test_pipeline_blocks_run_start_without_tracking_handle(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": None, "execution_id": None}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "tracking handle" in (result.blocker or "")
    # Both attempts returned no handle -> the documented retry phrase is on the blocker.
    assert "retried once with idempotency key" in (result.blocker or "")
    assert state.phase == AutoPhase.BLOCKED


@pytest.mark.asyncio
async def test_pipeline_resumes_run_with_persisted_handle_without_restarting(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("persisted run handle should not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.job_id = "job_existing"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_existing"


@pytest.mark.asyncio
async def test_interview_driver_persists_blocker_ledger_entry(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What API key should the workflow use?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("blocker should stop before backend answer")

    state = AutoPipelineState(goal="Deploy a service", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.ledger
    persisted = SeedDraftLedger.from_dict(state.ledger)
    assert any(
        entry.status == LedgerStatus.BLOCKED for entry in persisted.sections["constraints"].entries
    )
    assert persisted.question_history


@pytest.mark.asyncio
async def test_pipeline_blocks_completed_interview_without_session_id(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("missing interview session should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "interview_session_id" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_repair_phase_through_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("repair resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("repair resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("repair resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.REPAIR, "repair")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_interview_driver_blocks_when_backend_completes_before_ledger_ready(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=3
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "completed before auto ledger was ready" in (result.blocker or "")
    assert state.phase == AutoPhase.BLOCKED


@pytest.mark.asyncio
async def test_interview_driver_steers_generic_questions_to_open_gaps(tmp_path) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        completed = len(answers) >= 5
        return InterviewTurn("What else should we know?", session_id, completed=completed)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=6
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert ledger.is_seed_ready()
    assert any("single local user" in item.lower() for item in answers)
    assert any("non-goals" in item.lower() or "non-goal" in item.lower() for item in answers)
    assert any("runtime" in item.lower() for item in answers)


@pytest.mark.asyncio
async def test_interview_driver_uses_gap_answers_when_generic_defaults_repeat(tmp_path) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Anything else?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        completed = len(answers) >= 5
        return InterviewTurn("Anything else?", session_id, completed=completed)

    state = AutoPipelineState(goal="Build a local report generator", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=6
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert ledger.open_gaps() == []
    assert sum("conservative mvp" in item.lower() for item in answers) == 1
    assert any("single local user" in item.lower() for item in answers)
    assert any("non-goals" in item.lower() or "non-goal" in item.lower() for item in answers)
    assert any("runtime" in item.lower() for item in answers)


@pytest.mark.asyncio
async def test_interview_driver_preserves_specific_acceptance_answers_with_open_gaps(
    tmp_path,
) -> None:
    answers: list[str] = []
    questions = iter(
        [
            "What acceptance criteria should the search feature satisfy?",
            "What acceptance criteria should the export feature satisfy?",
        ]
    )

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(next(questions), "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        try:
            next_q = next(questions)
        except StopIteration:
            return InterviewTurn("Anything else?", session_id, completed=True)
        return InterviewTurn(next_q, session_id)

    state = AutoPipelineState(goal="Build a local CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=4
    )

    await driver.run(state, ledger)

    # Both specific feature/acceptance prompts should produce feature-specific
    # answers even though other required ledger sections (e.g. actors,
    # runtime_context) remain open. The driver must not replace them with
    # gap-targeted fallbacks.
    assert len(answers) >= 2, answers
    assert "search feature" in answers[0].lower()
    assert "export feature" in answers[1].lower()


@pytest.mark.asyncio
async def test_interview_driver_preserves_repeated_specific_acceptance_answer(
    tmp_path,
) -> None:
    """A specific feature/acceptance prompt asked twice while other sections are
    still open should still be answered specifically each time, not silently
    replaced by a gap-targeted fallback.
    """
    answers: list[str] = []
    repeated_question = "What acceptance criteria should the search feature satisfy?"
    rounds = {"n": 0}

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(repeated_question, "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        rounds["n"] += 1
        if rounds["n"] >= 2:
            return InterviewTurn("Anything else?", session_id, completed=True)
        return InterviewTurn(repeated_question, session_id)

    state = AutoPipelineState(goal="Build a local CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=4
    )

    await driver.run(state, ledger)

    assert len(answers) >= 2, answers
    assert "search feature" in answers[0].lower()
    assert "search feature" in answers[1].lower()


@pytest.mark.asyncio
async def test_interview_driver_blocks_blank_goal_before_gap_defaults(tmp_path) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("Anything else?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("Anything else?", session_id)

    state = AutoPipelineState(goal="Build a local tool", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal("")
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=3
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "goal is weak" in (result.blocker or "")
    assert answers == []


def test_auto_state_rejects_malformed_resume_optional_fields() -> None:
    base = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project").to_dict()
    base["pending_question"] = []

    with pytest.raises(ValueError, match="pending_question"):
        AutoPipelineState.from_dict(base)

    base = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project").to_dict()
    base["interview_completed"] = "yes"

    with pytest.raises(ValueError, match="interview_completed"):
        AutoPipelineState.from_dict(base)


@pytest.mark.asyncio
async def test_interview_driver_does_not_persist_completion_as_pending_question(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.interview_completed is True
    assert state.pending_question is None


@pytest.mark.asyncio
async def test_pipeline_blocks_completed_interview_with_unresolved_ledger(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("unresolved completed interview should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    state.ledger = SeedDraftLedger.from_goal(state.goal).to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "unresolved ledger gaps" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_marks_malformed_seed_generator_result_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not restart")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("completed interview should not answer")

    async def generate_seed(session_id: str):  # noqa: ANN202, ARG001
        return {"not": "a seed"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "expected Seed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_marks_malformed_run_starter_result_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = ""):  # noqa: ANN202, ARG001
        return ["not", "metadata"]

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "expected dict" in (result.blocker or "")
    assert state.run_start_attempted is False


@pytest.mark.asyncio
async def test_pipeline_resume_completes_after_first_run_timeout(tmp_path) -> None:
    """First call times out; resume retries once with the same idempotency key."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    calls = 0
    keys: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        keys.append(idempotency_key)
        await asyncio.sleep(0.05)
        return {"job_id": "job_after_timeout", "execution_id": "exec_after_timeout"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        run_start_timeout_seconds=0.001,
    )

    first = await pipeline.run(state)
    pipeline.run_start_timeout_seconds = 1
    second = await pipeline.run(state)

    # First pipeline.run() invokes the run starter twice (initial + the
    # bounded retry); both time out under the 1ms budget so it blocks
    # with the documented retry guidance phrase.
    assert first.status == "blocked"
    assert first.run_handoff_status == "unknown_timeout"
    assert "retried once with idempotency key" in (first.blocker or "")
    assert state.run_start_attempted is True
    # Resume after the bounded retry has already been exhausted must
    # NOT call the run starter again — the in-process idempotency map
    # cannot rule out a duplicate enqueue past two attempts.
    assert second.status == "blocked"
    assert "retried once with idempotency key" in (second.blocker or "")
    assert calls == 2
    assert all(key == state.auto_session_id for key in keys)


@pytest.mark.asyncio
async def test_pipeline_retries_after_no_handle_on_first_attempt(tmp_path) -> None:
    """First run-starter call returns no handle; bounded retry succeeds."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    calls = 0
    keys: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        keys.append(idempotency_key)
        if calls == 1:
            return {}
        return {"job_id": "job_after_retry", "execution_id": "exec_after_retry"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    first = await pipeline.run(state)

    # The bounded retry resolved the no-handle outcome inside a single run().
    assert first.status == "complete"
    assert first.execution_id == "exec_after_retry"
    assert state.run_start_attempted is True
    assert calls == 2
    assert keys == [state.auto_session_id, state.auto_session_id]


@pytest.mark.asyncio
async def test_interview_driver_blocks_malformed_backend_turn(tmp_path) -> None:
    async def start(goal: str, cwd: str):  # noqa: ANN202, ARG001
        return {"question": "not a turn"}

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("malformed start should not answer")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "expected InterviewTurn" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_clears_pending_question_before_backend_answer(tmp_path) -> None:
    store = AutoStore(tmp_path)

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        persisted = store.load(state.auto_session_id)
        assert persisted.pending_question is None
        assert persisted.last_tool_name == "auto_answerer"
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=store, max_rounds=1)

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert state.pending_question is None


@pytest.mark.asyncio
async def test_pipeline_returns_structured_failure_for_terminal_malformed_seed_artifact(
    tmp_path,
) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("terminal resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("terminal resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("terminal resume should not generate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = {"goal": "missing required seed fields"}
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "complete")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "persisted Seed artifact is invalid" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_seed_generation_resume_uses_persisted_seed_artifact(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("persisted seed artifact should not regenerate")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.seed_id = "seed_existing"
    state.seed_artifact = _seed().to_dict()
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_resumes_prepared_run_before_first_attempt(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": "job_after_resume", "execution_id": "exec_after_resume"}

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.last_grade = "A"
    state.transition(AutoPhase.RUN, "run prepared")
    state.run_start_attempted = False
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_after_resume"
    assert state.run_start_attempted is True


@pytest.mark.asyncio
async def test_pipeline_persists_seed_path_before_skip_run(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    saved: list[str] = []

    def save(seed: Seed) -> str:
        path = str(tmp_path / f"{seed.metadata.seed_id}.yaml")
        saved.append(path)
        return path

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.seed_path == saved[0]
    assert state.seed_path == saved[0]


@pytest.mark.asyncio
async def test_pipeline_resumes_blocked_seed_generation(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.mark_blocked("seed generation timed out", tool_name="seed_generator")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"


@pytest.mark.asyncio
async def test_pipeline_run_resume_rechecks_persisted_ledger_before_execution(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("unresolved ledger must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "clear the Seed for execution" in (result.blocker or "")
    assert result.grade == "C"


@pytest.mark.asyncio
async def test_pipeline_refuses_run_resume_without_a_grade(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("non-A run resume must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "B"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "persisted grade" in (result.blocker or "")
    assert state.job_id is None


@pytest.mark.asyncio
async def test_pipeline_seed_generation_resume_requires_interview_session_id(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("seed resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("missing interview session should fail before generator")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "interview_session_id" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_retry_after_blocked_run_start_replay_from_seed_path(tmp_path) -> None:
    """Resuming a blocked run-start session retries the run starter once."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("unknown run resume should not generate seed")

    keys: list[str] = []

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        keys.append(idempotency_key)
        return {"job_id": "job_after_replay", "execution_id": "exec_after_replay"}

    seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_path = seed_path
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_handoff_status = "unknown_timeout"
    state.mark_blocked("run start timed out", tool_name="run_starter")
    state.run_start_attempted = True
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.job_id == "job_after_replay"
    assert keys == [state.auto_session_id]


@pytest.mark.asyncio
async def test_pipeline_recovers_auto_answerer_block_to_interview(tmp_path) -> None:
    calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        calls.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "What should we verify?"
    state.mark_blocked("needs human authority", tool_name="auto_answerer")
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert calls


@pytest.mark.asyncio
async def test_pipeline_replays_persisted_run_subagent_after_complete_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, object]:  # noqa: ARG001
        return {
            "session_id": "session_1",
            "_subagent": {"tool_name": "ouroboros_execute_seed", "context": {"seed": "x"}},
        }

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    first = await pipeline.run(state)
    resumed = await pipeline.run(state)

    assert first.status == "complete"
    assert first.run_subagent == {"tool_name": "ouroboros_execute_seed", "context": {"seed": "x"}}
    assert state.run_subagent == first.run_subagent
    assert resumed.run_subagent == first.run_subagent


@pytest.mark.asyncio
async def test_pipeline_seed_save_error_marks_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    def save(seed: Seed) -> str:  # noqa: ARG001
        raise OSError("disk full")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed save failed" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_resumes_seed_saver_failure_from_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    seed = _seed()
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = seed.to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_failed("seed save failed: disk full", tool_name="seed_saver")
    saved: list[str] = []

    def save(recovered: Seed) -> str:
        saved.append(recovered.metadata.seed_id)
        return str(tmp_path / "seed.yaml")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver, generate_seed, store=AutoStore(tmp_path), seed_saver=save, skip_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert saved == [seed.metadata.seed_id]


@pytest.mark.asyncio
async def test_pipeline_grade_gate_resume_prefers_repaired_seed_path(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    stale_seed = _seed(ac=("The CLI should be easy and user-friendly",))
    repaired_seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = stale_seed.to_dict()
    state.seed_path = seed_path
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_blocked("Seed did not reach A-grade", tool_name="grade_gate")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            repaired_seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert state.seed_artifact == repaired_seed.to_dict()


@pytest.mark.asyncio
async def test_pipeline_review_resume_marks_malformed_seed_artifact_failed(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.seed_artifact = {"goal": "missing required fields"}
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, store=AutoStore(tmp_path), skip_run=True)

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "persisted Seed artifact is invalid" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_driver_does_not_send_synthetic_gap_answer_to_specific_prompt(
    tmp_path,
) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What output format should the export command write?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, completed=True)

    state = AutoPipelineState(goal="Build an export command", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert answers
    assert "single local user" not in answers[0].lower()
    assert "non-goals" not in answers[0].lower()


@pytest.mark.asyncio
async def test_interview_driver_accepts_initial_completed_turn_without_answering(tmp_path) -> None:
    answered = False

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("already complete", "interview_done", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        nonlocal answered
        answered = True
        raise AssertionError("completed initial turn should not be answered")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert result.rounds == 0
    assert state.interview_completed is True
    assert state.pending_question is None
    assert not answered


@pytest.mark.asyncio
async def test_interview_driver_does_not_replace_specific_verification_answer_with_gap_prompt(
    tmp_path,
) -> None:
    answers: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_1")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answers.append(text)
        return InterviewTurn("done", session_id, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=1
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert answers
    assert "observable behavior" in answers[0].lower()
    assert "single local user" not in answers[0].lower()


@pytest.mark.asyncio
async def test_pipeline_recovers_seed_loader_failure_from_review(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    stale_seed = _seed(ac=("The CLI should be easy and user-friendly",))
    repaired_seed = _seed()
    seed_path = str(tmp_path / "seed.yaml")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = stale_seed.to_dict()
    state.seed_path = seed_path
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.mark_failed("seed load failed: transient parse error", tool_name="seed_loader")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: (
            repaired_seed if path == seed_path else (_ for _ in ()).throw(AssertionError(path))
        ),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.grade == "A"
    assert state.seed_artifact == repaired_seed.to_dict()


@pytest.mark.asyncio
async def test_pipeline_run_resume_requires_may_run_even_when_required_grade_is_b(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("B-grade Seed with may_run=false must not start execution")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.required_grade = "B"
    state.last_grade = "B"
    state.seed_artifact = _seed(ac=("The CLI should be easy and user-friendly",)).to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "clear the Seed for execution" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_run_resume_rejects_grade_b_when_required_grade_a(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("run resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("run resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("grade B must not run when required grade is A")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.required_grade = "A"
    state.last_grade = "B"
    state.seed_artifact = _seed(ac=("The CLI should be easy and user-friendly",)).to_dict()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run prepared")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(driver, generate_seed, run_starter=run_seed, store=AutoStore(tmp_path))

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "persisted grade" in (result.blocker or "")


@pytest.mark.asyncio
async def test_interview_blocker_does_not_consume_pending_final_round(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should use the persisted pending question")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("blocked auto answer should not reach backend")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_1"
    state.pending_question = "Should we use a billing provider for the live account?"
    state.current_round = 1
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path), max_rounds=2
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert result.rounds == 1
    assert state.current_round == 1
    assert state.pending_question == "Should we use a billing provider for the live account?"
    persisted = AutoStore(tmp_path).load(state.auto_session_id)
    assert persisted.current_round == 1
    assert persisted.pending_question == state.pending_question


@pytest.mark.asyncio
async def test_interview_resume_backend_error_uses_resume_tool_name(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not start a new interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer without a question")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "resume interview")
    state.interview_session_id = "interview_1"
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert "interview resume failed" in (result.blocker or "")
    assert state.last_tool_name == "interview.resume"


@pytest.mark.asyncio
async def test_pipeline_seed_loader_rejects_non_seed_on_review_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("review resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("review resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_path = str(tmp_path / "seed.yaml")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        seed_loader=lambda path: {"path": path},  # type: ignore[return-value]
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert "seed loader returned dict, expected Seed" in (result.blocker or "")
    assert state.last_tool_name == "seed_loader"


def test_recoverable_phase_includes_interview_driver() -> None:
    """Sessions blocked at interview max_rounds set tool_name='interview_driver';
    resume must route them back to the INTERVIEW phase."""
    from ouroboros.auto.pipeline import _recoverable_phase_for_tool

    assert _recoverable_phase_for_tool("interview_driver") == AutoPhase.INTERVIEW


@pytest.mark.asyncio
async def test_resume_after_interview_max_rounds_can_continue_when_bound_raised(
    tmp_path,
) -> None:
    """Reproduce: a session blocked at max_interview_rounds with
    tool_name='interview_driver' must resume cleanly when the bound is raised
    instead of immediately re-emitting the same blocker."""
    answer_calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What is the acceptance signal?", "interview_resume")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        answer_calls.append(text)
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.interview_session_id = "interview_resume"
    state.current_round = 2
    state.max_interview_rounds = 4  # bound raised from the original 2
    state.pending_question = "What is the acceptance signal?"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "auto interview reached max rounds with unresolved gaps: actors",
        tool_name="interview_driver",
    )

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=state.max_interview_rounds,
    )

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed()

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        return {"job_id": "job_resume", "execution_id": "exec_resume", "session_id": "ses_resume"}

    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
    )

    result = await pipeline.run(state)

    assert result.status == "complete", f"resume blocked: {result.blocker!r}"
    assert state.phase == AutoPhase.COMPLETE
    assert answer_calls, "interview backend must be re-engaged on resume"


@pytest.mark.asyncio
async def test_pipeline_attaches_run_handle_after_unknown_handoff_without_restart(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("attach-only resume must not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    state.run_handoff_status = "unknown_no_handle"
    state.mark_blocked("Run starter returned no tracking handle", tool_name="run_starter")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        attach_execution_id="exec_existing",
        attach_source="operator",
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_handoff_status == "attached"
    assert result.execution_id == "exec_existing"
    assert result.attached_run_handle == "exec_existing"
    assert result.attached_run_source == "operator"
    assert result.attached_at is not None
    assert state.run_start_attempted is True
    assert state.execution_id == "exec_existing"


@pytest.mark.asyncio
async def test_pipeline_rejects_attach_without_unknown_handoff(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=None,
        store=AutoStore(tmp_path),
        attach_execution_id="exec_existing",
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "unknown run handoff" in (result.blocker or "")
    assert state.execution_id is None


@pytest.mark.asyncio
async def test_pipeline_records_unsupported_reconciliation_without_restart(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("reconcile-only resume must not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    state.run_handoff_status = "unknown_timeout"
    state.mark_blocked("run start timed out", tool_name="run_starter")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        reconcile_run=True,
        reconcile_source="generic",
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert result.run_handoff_status == "unknown_timeout"
    assert result.run_reconciliation_status == "unsupported"
    assert result.run_reconciliation_source == "generic"
    assert result.run_reconciled_at is not None
    assert "No duplicate run was started" in (result.run_handoff_guidance or "")
    assert state.execution_id is None


@pytest.mark.asyncio
async def test_pipeline_reports_attached_reconciliation_without_restart(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    state.run_handoff_status = "attached"
    state.execution_id = "exec_existing"
    state.attached_run_handle = "exec_existing"
    state.attached_run_source = "operator"
    state.attached_at = "2026-05-07T00:00:00+00:00"
    state.transition(AutoPhase.COMPLETE, "attached existing execution handle")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver, generate_seed, run_starter=None, store=AutoStore(tmp_path), reconcile_run=True
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_reconciliation_status == "attached"
    assert result.run_reconciliation_source == "attached_run"
    assert result.run_reconciled_at is not None
    assert result.execution_id == "exec_existing"


@pytest.mark.asyncio
async def test_pipeline_marks_reconcile_invalid_context_separately(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=None,
        store=AutoStore(tmp_path),
        reconcile_run=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert result.run_reconciliation_status == "invalid_context"
    assert result.run_reconciliation_source == "generic"
    assert result.run_reconciled_at is not None
    assert "unknown run handoff" in (result.blocker or "")


@pytest.mark.asyncio
async def test_complete_session_invalid_reconcile_reports_blocked_result(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "already complete without run handoff")

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=None,
        store=AutoStore(tmp_path),
        reconcile_run=True,
    )

    result = await pipeline.run(state)

    assert state.phase == AutoPhase.COMPLETE
    assert result.phase == "complete"
    assert result.status == "blocked"
    assert result.run_reconciliation_status == "invalid_context"
    assert result.run_reconciliation_source == "generic"
    assert "unknown run handoff" in (result.blocker or "")


@pytest.mark.asyncio
async def test_pipeline_attach_clears_stale_reconciliation_metadata(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    async def run_seed(seed: Seed, *, idempotency_key: str = "") -> dict[str, str | None]:  # noqa: ARG001
        raise AssertionError("attach must not start another run")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.run_start_attempted = True
    state.run_handoff_status = "unknown_no_handle"
    state.mark_blocked("Run starter returned no tracking handle", tool_name="run_starter")
    # Prior --reconcile-run on the same unknown handoff recorded an unsupported
    # reconciliation outcome. A subsequent successful attach must clear those
    # fields so callers do not see contradictory state.
    state.run_reconciliation_status = "unsupported"
    state.run_reconciliation_source = "generic"
    state.run_reconciled_at = "2026-05-07T00:00:00+00:00"

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=run_seed,
        store=AutoStore(tmp_path),
        attach_execution_id="exec_existing",
        attach_source="operator",
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_handoff_status == "attached"
    assert result.attached_run_handle == "exec_existing"
    assert result.run_reconciliation_status is None
    assert result.run_reconciliation_source is None
    assert result.run_reconciled_at is None
    assert state.run_reconciliation_status is None
    assert state.run_reconciliation_source is None
    assert state.run_reconciled_at is None


@pytest.mark.asyncio
async def test_invalid_reconcile_on_complete_does_not_poison_future_resume(tmp_path) -> None:
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not restart interview")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("resume should not answer interview")

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        raise AssertionError("resume should not regenerate seed")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    state.seed_artifact = _seed().to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "already complete without run handoff")
    assert state.last_error is None

    driver = AutoInterviewDriver(FunctionInterviewBackend(start, answer), store=AutoStore(tmp_path))

    invalid_pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=None,
        store=AutoStore(tmp_path),
        reconcile_run=True,
    )
    invalid_result = await invalid_pipeline.run(state)

    assert invalid_result.status == "blocked"
    assert invalid_result.run_reconciliation_status == "invalid_context"
    assert "unknown run handoff" in (invalid_result.blocker or "")
    # Per-invocation misuse must not corrupt the durable terminal-complete state:
    # last_error stays clean so subsequent plain --resume/--status do not
    # report a steady-state blocker.
    assert state.phase == AutoPhase.COMPLETE
    assert state.last_error is None

    plain_pipeline = AutoPipeline(
        driver,
        generate_seed,
        run_starter=None,
        store=AutoStore(tmp_path),
    )
    plain_result = await plain_pipeline.run(state)

    assert plain_result.status == "complete"
    assert plain_result.blocker is None
    assert state.last_error is None


# ---------------------------------------------------------------------------
# Q00/ouroboros#687 — persist interview_session_id even when first-question
# generation fails before the auto driver receives a turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_driver_keeps_session_id_when_probe_confirms_persistence(
    tmp_path,
) -> None:
    """Driver retains the pre-allocated id only when persistence is verifiable.

    Models the issue's primary scenario: the driver's ``asyncio.wait_for``
    cancels the backend mid-flight, but the engine has already persisted
    the interview state.  The driver must consult ``is_session_persisted``
    to confirm before saving the id on auto state.
    """

    received_ids: list[str | None] = []
    persisted_ids: set[str] = set()

    async def start(goal: str, cwd: str, *, interview_id: str | None = None) -> InterviewTurn:
        received_ids.append(interview_id)
        # Simulate engine.start_interview persisting before the cancel.
        if interview_id:
            persisted_ids.add(interview_id)
        await asyncio.sleep(0.5)  # forces TimeoutError below
        return InterviewTurn("never reached", interview_id or "fallback")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer must not run when start times out")

    def is_persisted(session_id: str) -> bool:
        return session_id in persisted_ids

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    store = AutoStore(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer, is_session_persisted=is_persisted),
        store=store,
        timeout_seconds=0.001,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.interview_session_id, "probe-confirmed id must be saved on auto state"
    assert received_ids == [state.interview_session_id], (
        "backend.start must receive the pre-allocated interview_id so the "
        "persisted interview file matches auto state"
    )

    reloaded = store.load(state.auto_session_id)
    assert reloaded is not None
    assert reloaded.interview_session_id == state.interview_session_id


@pytest.mark.asyncio
async def test_interview_driver_clears_session_id_when_backend_rejects_without_persistence(
    tmp_path,
) -> None:
    """A plain rejection (validation/config error) must NOT pollute auto state.

    Without an evidence channel (``PartialInterviewStartError`` carrying a
    confirmed id, or a positive ``is_session_persisted`` probe) the driver
    must leave ``interview_session_id`` unset so ``ooo auto --resume`` does
    not chase a nonexistent interview file.
    """

    async def start(goal: str, cwd: str, *, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG001
        raise RuntimeError("backend rejected start before persisting anything")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer must not run when start fails")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    store = AutoStore(tmp_path)
    # Probe always returns False — no on-disk evidence of persistence.
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer, is_session_persisted=lambda _id: False),
        store=store,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert state.interview_session_id is None, (
        "auto state must NOT retain the pre-allocated id without persistence evidence"
    )
    assert result.session_id is None

    reloaded = store.load(state.auto_session_id)
    assert reloaded is not None
    assert reloaded.interview_session_id is None


@pytest.mark.asyncio
async def test_interview_driver_persists_partial_session_id_on_start_failure(tmp_path) -> None:
    """A handler-level partial-success error keeps the pre-allocated id on state."""

    async def start(goal: str, cwd: str, *, interview_id: str | None = None) -> InterviewTurn:  # noqa: ARG001
        # Real backends honour the supplied id; mirror that here.
        assert interview_id, "driver must pre-allocate an id before backend.start"
        raise PartialInterviewStartError(
            "ouroboros_interview failed: Question generation failed: timed out",
            session_id=interview_id,
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("answer must not be called when start fails")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    store = AutoStore(tmp_path)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=store,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert state.interview_session_id, "auto state must hold the pre-allocated session id"
    assert result.session_id == state.interview_session_id

    reloaded = store.load(state.auto_session_id)
    assert reloaded is not None
    assert reloaded.interview_session_id == state.interview_session_id


@pytest.mark.asyncio
async def test_interview_driver_resumes_existing_session_after_partial_failure(
    tmp_path,
) -> None:
    """After a partial first-question failure resume must reuse the persisted id."""

    persisted_session = "interview_partial_002"
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "starting auto interview")
    state.interview_session_id = persisted_session
    state.pending_question = None

    start_calls: list[tuple[str, str]] = []
    resume_calls: list[str] = []

    async def start(goal: str, cwd: str) -> InterviewTurn:
        start_calls.append((goal, cwd))
        raise AssertionError("start must not be called when a session is already persisted")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def resume(session_id: str) -> InterviewTurn:
        resume_calls.append(session_id)
        return InterviewTurn("Anything else?", session_id)

    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer, resume),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert resume_calls == [persisted_session]
    assert not start_calls
    assert result.session_id == persisted_session
    assert state.interview_session_id == persisted_session


def test_interview_start_timeout_state_routes_to_interview_on_resume_with_retry_capability() -> (
    None
):
    """An ``interview.start`` timeout is a recoverable resume path but classifies as RETRY.

    The #688 invariant: ``_recoverable_phase_for_tool('interview.start')`` returns
    :class:`AutoPhase.INTERVIEW` (so :meth:`AutoPipeline.run` routes the resume
    back into the interview phase), *and* :meth:`AutoPipelineState.resume_capability`
    classifies the post-timeout state as :class:`AutoResumeCapability.RETRY`
    because no ``interview_session_id`` was persisted — there is no prior
    progress to recover.
    """
    from ouroboros.auto.pipeline import _recoverable_phase_for_tool
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("interview.start timed out", tool_name="interview.start")

    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "interview.start"
    assert state.interview_session_id is None
    assert state.pending_question is None
    assert _recoverable_phase_for_tool("interview.start") is AutoPhase.INTERVIEW
    assert state.resume_capability() is AutoResumeCapability.RETRY


@pytest.mark.asyncio
async def test_convergence_contract_broad_benign_goal_resolves_with_safe_assumptions(
    tmp_path,
) -> None:
    """Broad benign goals may proceed only with auditable required sections."""

    rounds = 0

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", "interview_contract")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        nonlocal rounds
        rounds += 1
        return InterviewTurn(
            "What else should we know?",
            session_id,
            seed_ready=rounds >= 5,
            completed=rounds >= 5,
        )

    state = AutoPipelineState(goal="Build a local note taking CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=6,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert ledger.is_seed_ready()
    assert ledger.open_gaps() == []
    for required in (
        "actors",
        "inputs",
        "outputs",
        "non_goals",
        "acceptance_criteria",
        "verification_plan",
        "runtime_context",
    ):
        assert ledger.sections[required].entries, required
    assert {entry.source for entry in ledger.sections["actors"].entries} <= {
        LedgerSource.ASSUMPTION,
        LedgerSource.USER_GOAL,
    }
    assert any(
        entry.status == LedgerStatus.DEFAULTED
        for entry in ledger.sections["acceptance_criteria"].entries
    )


@pytest.mark.asyncio
async def test_convergence_contract_unsafe_authority_question_blocks(tmp_path) -> None:
    """Unsafe authority gaps are blockers, not auto-filled defaults."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn(
            "Which production access token should auto configure?",
            "interview_contract",
        )

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        raise AssertionError("unsafe blocker should stop before backend.answer")

    state = AutoPipelineState(goal="Deploy a service", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=3,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "credential or secret value required" in (result.blocker or "")


@pytest.mark.asyncio
async def test_convergence_contract_stalled_generic_followups_report_actionable_gaps(
    tmp_path,
) -> None:
    """Generic ``What else?`` follow-up loops must blocker on unresolved sections.

    This pins the documented stall pattern from
    ``docs/auto-interview-convergence-contract.md``: when the backend keeps asking
    ``What else?`` / ``Any additional context?``-style questions and required
    sections never resolve, the round-cap blocker has to name the unresolved
    gaps, not just say that ``max_rounds`` was reached.
    """

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", "interview_contract")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", session_id)

    state = AutoPipelineState(
        goal="Build a small note-taking CLI",
        cwd=str(tmp_path),
    )
    ledger = SeedDraftLedger.from_goal(state.goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
    )

    result = await driver.run(state, ledger)

    blocker = result.blocker or ""
    assert result.status == "blocked"
    assert state.phase == AutoPhase.BLOCKED
    assert "unresolved gaps" in blocker
    open_gaps = ledger.open_gaps()
    assert open_gaps, "stalled generic loop must leave at least one open required gap"
    # The blocker must name at least one specific unresolved section, not just
    # report that max_rounds was reached.
    assert any(section in blocker for section in open_gaps)
