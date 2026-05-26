"""Behaviour tests for the safe-default unsafe-context lateral escalation.

Issue #1248 — the safe-default closure path is the second-to-last rung in
SSOT #1157's L5 ladder and must not die in place when the unsafe-context
matcher fires on lexical false positives. Instead the driver escalates
through a bounded lateral persona chain (CONTRARIAN, ARCHITECT) before
falling to a typed ``unstuck_exhausted`` BLOCKED.

PR-B review (ouroboros-agent) gate: ledger demotion is now contingent on
the lateral response carrying the canonical
``CLEARANCE: lexical_false_positive`` marker (see
``_LATERAL_CLEARANCE_MARKER_RE``). A bare prompt-only response — which is
what the production inline ``ouroboros_lateral_think`` path returns today
— must NOT clear an unsafe matcher fire. These tests pin the observable
paths:

1. Lateral resolves on first attempt (with clearance) → seed_ready, no
   stop_reason_code.
2. Lateral handler errors out → existing BLOCKED path preserved.
3. Lateral chain exhausts (pre-tried) → BLOCKED with ``unstuck_exhausted``.
4. ``lateral_thinker=None`` → pre-issue behaviour preserved (regression).
5. Selector chain order (CONTRARIAN → ARCHITECT).
6. Transient ``LateralResult(error=...)`` is recorded, no retry.
7. Lateral returns prompt-only text (no clearance) → ledger untouched,
   chain exhausts → ``unstuck_exhausted`` (safety regression guard).
8. Genuinely unsafe conservative_default entry survives a prompt-only
   lateral response (safety regression guard).
9. After a clearance-marked demotion, ``state.ledger`` reloads from the
   ``AutoStore`` checkpoint with the demoted source classifications
   (resume contract — keeps persona audit and ledger mutation in sync).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ouroboros.auto.adapters import LateralResult
from ouroboros.auto.interview_driver import (
    UNSTUCK_EXHAUSTED_STOP_REASON_CODE,
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.resilience.lateral import ThinkingPersona

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ledger_with_matcher_trigger(goal: str = "Build a habit-tracker CLI") -> SeedDraftLedger:
    """Pre-seed a ledger whose CONSERVATIVE_DEFAULT entries trigger the matcher.

    Mirrors the R2 cli-todo evidence shape: an auto-answerer-authored
    ``CONSERVATIVE_DEFAULT`` entry containing the word ``contract`` (in the
    benign SE sense "acceptance contract") fires the ``legal/medical
    judgment`` regex.  Two open gaps remain (``non_goals`` and
    ``runtime_context``) so the safe-default policy has something to
    default in the matcher's blast radius.
    """
    ledger = SeedDraftLedger.from_goal(goal)
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command-line arguments",
        "outputs": "Stable stdout and a JSON file",
        "constraints": "Use existing project patterns; no new dependencies",
        "acceptance_criteria": (
            "Should we define the acceptance contract as: success prints "
            "one fixed stdout line; failure prints one fixed stderr line "
            "and exits 1?"
        ),
        "verification_plan": "Run command-level tests against the binary",
        "failure_modes": "Invalid input exits non-zero",
    }.items():
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=LedgerSource.CONSERVATIVE_DEFAULT,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )
    return ledger


def _make_scripted_backend(answer_seed_ready: bool = True) -> FunctionInterviewBackend:
    """Backend that always returns ``seed_ready=answer_seed_ready`` on synthesis."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else should we know?", "interview_test")

    async def answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        if "[safe-default-synthesis]" in text:
            return InterviewTurn(
                "done",
                session_id,
                seed_ready=answer_seed_ready,
                completed=answer_seed_ready,
            )
        return InterviewTurn("What else should we know?", session_id, seed_ready=False)

    return FunctionInterviewBackend(start, answer)


_DEFAULT_CLEARANCE_TEXT = (
    "the matched 'contract' refers to acceptance contract, not legal contract.\n"
    "CLEARANCE: lexical_false_positive\n"
)


def _resolving_lateral_thinker(
    *,
    return_persona: str | None = None,
    summary: str = "false positive — SE vocabulary, demote to assumption",
    text: str = _DEFAULT_CLEARANCE_TEXT,
) -> tuple[object, list[ThinkingPersona]]:
    """Lateral handle that succeeds (with clearance marker) and captures invocation personas.

    The default ``text`` includes the canonical
    ``CLEARANCE: lexical_false_positive`` line, which is what the
    interview driver requires to authorize demoting active
    ``CONSERVATIVE_DEFAULT`` ledger entries to ``ASSUMPTION``. Tests that
    want to exercise the no-clearance (prompt-only) path override
    ``text`` explicitly.
    """
    invocations: list[ThinkingPersona] = []

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        invocations.append(persona)
        return LateralResult(
            persona=return_persona or persona.value,
            approach_summary=summary,
            text=text,
        )

    return _thinker, invocations


def _advisory_only_lateral_thinker() -> tuple[object, list[ThinkingPersona]]:
    """Lateral handle that mimics the production inline ``ouroboros_lateral_think`` path.

    The inline path generates a prompt template (``# Lateral Thinking: …``)
    plus a ``## Questions to Consider`` block — it does *not* execute the
    persona's judgement. Such a response carries no clearance marker, so
    the interview driver must NOT demote ledger entries. This fixture
    pins that contract: every call returns a successful ``LateralResult``
    whose text is a prompt-shaped string only.
    """
    invocations: list[ThinkingPersona] = []
    sample_text = (
        "# Lateral Thinking: Re-examine the matcher input\n\n"
        "Given the current approach, consider the following angles before "
        "deciding whether the matcher fire reflects genuine unsafe scope.\n\n"
        "## Questions to Consider\n"
        "- Does the surrounding ledger context actually authorize the "
        "unsafe reading?\n"
        "- Is the matched token used in a benign SE sense?\n"
    )

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        invocations.append(persona)
        return LateralResult(
            persona=persona.value,
            approach_summary="advisory prompt only — no verdict",
            text=sample_text,
        )

    return _thinker, invocations


def _erroring_lateral_thinker() -> tuple[object, list[ThinkingPersona]]:
    """Lateral handle that raises a runtime error."""
    invocations: list[ThinkingPersona] = []

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        invocations.append(persona)
        msg = "synthetic lateral handler failure"
        raise RuntimeError(msg)

    return _thinker, invocations


def _transient_error_lateral_thinker() -> tuple[object, list[ThinkingPersona]]:
    """Lateral handle that returns ``LateralResult(error=...)`` instead of raising."""
    invocations: list[ThinkingPersona] = []

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        invocations.append(persona)
        return LateralResult(
            persona=persona.value,
            approach_summary="",
            text="",
            error="upstream lateral handler unavailable",
        )

    return _thinker, invocations


def _no_op_lateral_thinker() -> tuple[object, list[ThinkingPersona]]:
    """Lateral handle that succeeds but the ledger demotion alone does not clear the matcher.

    Used in the chain-exhausted test: even after demotion, residual user-supplied
    answer text would keep the matcher firing. We simulate that by leaving the
    LateralResult successful (so the loop persists invocation + demotes), then
    pre-loading the question_history with an answer that re-triggers the
    matcher on the second pass too.
    """
    invocations: list[ThinkingPersona] = []

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        invocations.append(persona)
        return LateralResult(
            persona=persona.value,
            approach_summary="reviewed",
            text="lateral text",
        )

    return _thinker, invocations


# ---------------------------------------------------------------------------
# Test 1 — happy path: lateral resolves on first persona (CONTRARIAN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lateral_resolves_matcher_fire_on_first_persona(tmp_path) -> None:
    """CONTRARIAN escalation demotes triggering entries → second finalize clears.

    Mirrors the R2 cli-todo evidence: ledger has CONSERVATIVE_DEFAULT entries
    containing the word "contract"; matcher fires "legal/medical judgment";
    lateral CONTRARIAN persona is invoked once; its successful return demotes
    every active CONSERVATIVE_DEFAULT entry to ASSUMPTION; the next finalize
    pass clears unsafe_gaps; the synthesis path closes the interview.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    thinker, invocations = _resolving_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=True),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert invocations == [ThinkingPersona.CONTRARIAN]
    assert state.last_lateral_persona == ThinkingPersona.CONTRARIAN.value
    assert state.personas_invoked == [ThinkingPersona.CONTRARIAN.value]
    assert state.last_error_code != UNSTUCK_EXHAUSTED_STOP_REASON_CODE
    # Demotion side effect: at least one entry now has source=ASSUMPTION.
    demoted = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.ASSUMPTION
    ]
    assert demoted, "lateral resolution must demote CONSERVATIVE_DEFAULT entries"


# ---------------------------------------------------------------------------
# Test 2 — lateral handler error: fall through to existing BLOCKED path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lateral_handler_error_falls_through_to_blocked(tmp_path) -> None:
    """When the lateral handler raises, the existing BLOCKED path applies.

    The exception is logged, the persona is still recorded on
    ``personas_invoked`` (so a future attempt picks a different one), and
    the resulting BLOCKED carries the original ``interview_unsafe_gaps_remain``
    or ``interview_max_rounds_exhausted`` code — NOT ``unstuck_exhausted``,
    because the chain itself was never proven exhausted.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    thinker, invocations = _erroring_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert invocations == [ThinkingPersona.CONTRARIAN]
    assert state.personas_invoked == [ThinkingPersona.CONTRARIAN.value]
    assert state.last_error_code != UNSTUCK_EXHAUSTED_STOP_REASON_CODE


# ---------------------------------------------------------------------------
# Test 3 — chain exhausts: BLOCKED with unstuck_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lateral_chain_exhausts_emits_unstuck_exhausted(tmp_path) -> None:
    """When both CONTRARIAN and ARCHITECT have been tried, BLOCKED is typed ``unstuck_exhausted``.

    Simulated by pre-populating ``state.personas_invoked`` with both safe-default
    chain members so the selector immediately returns ``None``.  The driver
    must stamp ``UNSTUCK_EXHAUSTED_STOP_REASON_CODE`` and the downstream
    BLOCKED branch must honor that stamp.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    state.personas_invoked = [
        ThinkingPersona.CONTRARIAN.value,
        ThinkingPersona.ARCHITECT.value,
    ]
    thinker, invocations = _no_op_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert invocations == []  # lateral never invoked — chain pre-exhausted
    assert state.last_error_code == UNSTUCK_EXHAUSTED_STOP_REASON_CODE


# ---------------------------------------------------------------------------
# Test 4 — lateral_thinker=None preserves pre-issue BLOCKED behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_lateral_thinker_preserves_pre_issue_blocked_path(tmp_path) -> None:
    """When ``lateral_thinker`` is ``None`` the matcher fire still BLOCKS the run.

    Regression guard: every existing call site that does not pass
    ``lateral_thinker=`` (unit tests, CLI path, plugin mode) must keep
    seeing the pre-issue ``interview_unsafe_gaps_remain`` /
    ``interview_max_rounds_exhausted`` codes — not ``unstuck_exhausted``,
    and no demotion side effects on the ledger.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        # lateral_thinker omitted → None default
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.last_error_code != UNSTUCK_EXHAUSTED_STOP_REASON_CODE
    # No demotion happened — every CONSERVATIVE_DEFAULT stays put.
    survived = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.CONSERVATIVE_DEFAULT
    ]
    assert survived, "no lateral_thinker → no demotion should have occurred"


# ---------------------------------------------------------------------------
# Test 5 — selector chain is consumed in CONTRARIAN → ARCHITECT order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lateral_chain_order_after_contrarian_already_tried(tmp_path) -> None:
    """Pre-populated ``CONTRARIAN`` in personas_invoked routes the next call to ARCHITECT."""
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    state.personas_invoked = [ThinkingPersona.CONTRARIAN.value]
    thinker, invocations = _resolving_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=True),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    assert invocations == [ThinkingPersona.ARCHITECT]
    assert ThinkingPersona.ARCHITECT.value in state.personas_invoked


# ---------------------------------------------------------------------------
# Test 6 — transient lateral error (LateralResult.error) is recorded but does not loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lateral_transient_error_breaks_loop(tmp_path) -> None:
    """``LateralResult(error=...)`` records the attempt but returns control to caller.

    The persona is still appended to ``personas_invoked`` (so a resume
    picks a different one), but the loop does NOT immediately retry —
    transient errors are treated as a single attempt outcome, mirroring
    the EVALUATE-side semantics.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    thinker, invocations = _transient_error_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert invocations == [ThinkingPersona.CONTRARIAN]
    assert state.personas_invoked == [ThinkingPersona.CONTRARIAN.value]
    assert state.last_error_code != UNSTUCK_EXHAUSTED_STOP_REASON_CODE


# ---------------------------------------------------------------------------
# Test 7 — advisory-only lateral response does not demote (safety regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_only_lateral_response_does_not_demote(tmp_path) -> None:
    """A prompt-only lateral response (no clearance marker) must not mutate the ledger.

    The production inline ``ouroboros_lateral_think`` path generates an
    advisory prompt template (``# Lateral Thinking: …`` + questions) and
    returns it as ``LateralResult.text`` — there is no executed persona
    judgement. The interview driver must treat that as
    *"lateral attempted, no machine-checkable clearance"*: the persona
    invocation is recorded (audit + chain progression), the ledger
    ``CONSERVATIVE_DEFAULT`` entries stay put, and the matcher fire
    survives every persona in the chain until the typed
    ``unstuck_exhausted`` terminal stamps the BLOCKED.

    This is the safety contract requested by ouroboros-agent's PR #1251
    review: advisory prompt generation is not authorization to reclassify
    conservative-default content.
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    thinker, invocations = _advisory_only_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    # Both personas in the safe-default chain were tried with prompt-only
    # responses; neither cleared the matcher fire so the chain exhausted
    # and the typed terminal was stamped.
    assert invocations == [ThinkingPersona.CONTRARIAN, ThinkingPersona.ARCHITECT]
    assert ThinkingPersona.CONTRARIAN.value in state.personas_invoked
    assert ThinkingPersona.ARCHITECT.value in state.personas_invoked
    assert state.last_error_code == UNSTUCK_EXHAUSTED_STOP_REASON_CODE
    # Ledger source classifications were NOT mutated: every entry the
    # fixture wrote stays at ``CONSERVATIVE_DEFAULT``.
    survived = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.CONSERVATIVE_DEFAULT
    ]
    demoted = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.ASSUMPTION
    ]
    assert survived, "advisory-only lateral must leave conservative_default entries intact"
    assert not demoted, (
        "advisory-only lateral must NOT demote any conservative_default entry to assumption"
    )


# ---------------------------------------------------------------------------
# Test 8 — genuinely unsafe conservative_default entry survives an
# advisory-only lateral response (the explicit bot regression scenario)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsafe_conservative_default_survives_advisory_lateral(tmp_path) -> None:
    """An entry that records genuine unsafe scope must NOT be cleared by an advisory response.

    Mirrors the regression scenario called out by the PR #1251 review:
    a ``conservative_default`` entry whose text describes a production
    deploy with credential handling must not be reclassified to
    ``ASSUMPTION`` just because a lateral persona returned a prompt
    template. The matcher fire must continue to fire on subsequent
    finalize passes, and the BLOCKED must carry the typed
    ``unstuck_exhausted`` reason (chain exhausted with no clearance).
    """
    ledger = _ledger_with_matcher_trigger()
    # Inject a genuinely unsafe conservative_default entry on top of the
    # baseline fixture. The matcher already fires on "contract" — we want
    # to assert the *unsafe* entry is not silently cleared either.
    unsafe_value = (
        "Deploy the binary to the production fleet using the operator's "
        "admin credentials; the legal compliance check is out of scope."
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.unsafe_test",
            value=unsafe_value,
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.85,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    thinker, invocations = _advisory_only_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=False),
        store=AutoStore(tmp_path),
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "blocked"
    assert state.last_error_code == UNSTUCK_EXHAUSTED_STOP_REASON_CODE
    # The genuinely unsafe entry is still there with its original source.
    unsafe_entries = [
        entry
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.key == "constraints.unsafe_test"
    ]
    assert len(unsafe_entries) == 1
    assert unsafe_entries[0].source == LedgerSource.CONSERVATIVE_DEFAULT, (
        "advisory lateral must not reclassify a genuinely unsafe conservative_default entry"
    )
    # Both personas were exercised — the chain is honoured even when the
    # ledger is not mutated.
    assert invocations == [ThinkingPersona.CONTRARIAN, ThinkingPersona.ARCHITECT]


# ---------------------------------------------------------------------------
# Test 9 — durable state after clearance demotion matches the runtime ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clearance_demotion_persists_ledger_for_resume(tmp_path) -> None:
    """After a clearance-marked demotion, the AutoStore checkpoint reflects the demote.

    ouroboros-agent's PR #1251 second-round review (head 85e71b1d) called
    out the resume gap: ``AutoStore.save`` only writes ``state.to_dict()``,
    so the lateral audit fields (``personas_invoked``, ``last_lateral_*``)
    would be persisted while ``state.ledger`` still mirrored the pre-demote
    source classifications. On resume the matcher would re-fire on the
    same input, but the persona is already "spent" — turning a successful
    clearance into a spurious retry/block.

    Contract: every save after the demotion must include the snapshot of
    the mutated ledger. Reload via ``AutoStore.load`` and verify the
    persisted ``state.ledger`` payload no longer contains any active
    ``CONSERVATIVE_DEFAULT`` entries (every one was demoted to
    ``ASSUMPTION`` by the lateral resolution).
    """
    ledger = _ledger_with_matcher_trigger()
    state = AutoPipelineState(goal="Build a habit-tracker CLI", cwd=str(tmp_path))
    store = AutoStore(tmp_path)
    thinker, _invocations = _resolving_lateral_thinker()
    driver = AutoInterviewDriver(
        _make_scripted_backend(answer_seed_ready=True),
        store=store,
        max_rounds=1,
        timeout_seconds=1,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)
    assert result.status == "seed_ready"

    # Reload the same auto_session_id through the store — this simulates
    # the resume path and is the only test surface that actually proves
    # the ledger snapshot landed on disk alongside the persona audit.
    reloaded = store.load(state.auto_session_id)
    assert reloaded.personas_invoked == [ThinkingPersona.CONTRARIAN.value]
    reloaded_ledger = SeedDraftLedger.from_dict(reloaded.ledger)
    survived = [
        entry
        for section in reloaded_ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.CONSERVATIVE_DEFAULT
        and entry.status not in {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    ]
    assert not survived, (
        "persisted state.ledger must reflect demoted sources so resume sees "
        "the same matcher input the runtime path saw"
    )
