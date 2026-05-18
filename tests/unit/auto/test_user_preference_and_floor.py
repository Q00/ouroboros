"""Phase 1 tests — USER_PREFERENCE source + deterministic_floor.

Covers gaps 4 and 5 of RFC #809:

- ``LedgerSource.USER_PREFERENCE`` round-trips and is treated as evidence-backed
- ``count_active_conflicting_entries`` helper returns correct counts
- ``deterministic_floor`` formula and clamping behaviour
- Grading gate blocks Seeds when the floor pushes ambiguity_score above the gate
- ``AutoAnswerer`` upgrades a CONSERVATIVE_DEFAULT to USER_PREFERENCE when a
  caller-supplied preference matches the question's intent section
- Stronger sources (REPO_FACT) beat caller-supplied preferences
- ``[from-auto][user_preference]`` log format is correct
"""

from __future__ import annotations

from ouroboros.auto.answerer import (
    AutoAnswer,
    AutoAnswerContext,
    AutoAnswerer,
    AutoAnswerSource,
)
from ouroboros.auto.grading import GradeGate, SeedGrade, deterministic_floor
from ouroboros.auto.ledger import (
    SOURCE_PRIORITY,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
    resolve_conflict,
)
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)

# ---------------------------------------------------------------------------
# Test fixtures (kept local to avoid coupling with neighbouring test files)
# ---------------------------------------------------------------------------


def _fill_minimal_ready_ledger(ledger: SeedDraftLedger) -> None:
    entries = {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }
    for section, value in entries.items():
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


def _seed(*, ambiguity_score: float = 0.05) -> Seed:
    return Seed(
        goal="Build a habit tracker",
        constraints=("Use existing project patterns",),
        acceptance_criteria=(
            "`habit add` writes a stable artifact to the local store and exits with code 0.",
        ),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=ambiguity_score),
    )


# ---------------------------------------------------------------------------
# LedgerSource.USER_PREFERENCE plumbing
# ---------------------------------------------------------------------------


def test_user_preference_source_round_trips_through_to_dict_and_from_dict() -> None:
    entry = LedgerEntry(
        key="runtime_context.user_preference",
        value="Python 3.14 with uv",
        source=LedgerSource.USER_PREFERENCE,
        confidence=0.83,
        status=LedgerStatus.CONFIRMED,
    )
    raw = entry.to_dict()
    assert raw["source"] == "user_preference"

    restored = LedgerEntry.from_dict(raw)
    assert restored.source == LedgerSource.USER_PREFERENCE
    assert restored.value == "Python 3.14 with uv"


def test_user_preference_section_lands_in_evidence_backed_summary() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime_context.user_preference",
            value="Python 3.14 with uv",
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.83,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    summary = ledger.summary()
    assert "runtime_context" in summary["evidence_backed_sections"]
    assert "runtime_context" not in summary["assumption_only_sections"]


# ---------------------------------------------------------------------------
# count_active_conflicting_entries helper
# ---------------------------------------------------------------------------


def test_count_active_conflicting_entries_returns_count_across_sections() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.a",
            value="Stay local",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.CONFLICTING,
        ),
    )
    ledger.add_entry(
        "inputs",
        LedgerEntry(
            key="inputs.a",
            value="CLI args",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.CONFLICTING,
        ),
    )
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.a",
            value="stdout",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    assert ledger.count_active_conflicting_entries() == 2


# ---------------------------------------------------------------------------
# deterministic conflict policy
# ---------------------------------------------------------------------------


def test_source_priority_places_user_preference_between_convention_and_default() -> None:
    assert SOURCE_PRIORITY.index(LedgerSource.EXISTING_CONVENTION) < SOURCE_PRIORITY.index(
        LedgerSource.USER_PREFERENCE
    )
    assert SOURCE_PRIORITY.index(LedgerSource.USER_PREFERENCE) < SOURCE_PRIORITY.index(
        LedgerSource.CONSERVATIVE_DEFAULT
    )


def test_higher_priority_incoming_entry_supersedes_lower_priority_existing() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.storage",
            value="Use a local JSON file",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.85,
            status=LedgerStatus.DEFAULTED,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.storage",
            value="Use SQLite because the repo already depends on it",
            source=LedgerSource.REPO_FACT,
            confidence=0.70,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    entries = ledger.sections["constraints"].entries
    assert entries[0].status is LedgerStatus.WEAK
    assert entries[1].status is LedgerStatus.CONFIRMED
    assert ledger.sections["constraints"].status() is LedgerStatus.CONFIRMED


def test_higher_priority_existing_entry_supersedes_lower_priority_incoming() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.storage",
            value="Use SQLite because the repo already depends on it",
            source=LedgerSource.REPO_FACT,
            confidence=0.70,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.storage",
            value="Use a local JSON file",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.95,
            status=LedgerStatus.DEFAULTED,
        ),
    )

    entries = ledger.sections["constraints"].entries
    assert entries[0].status is LedgerStatus.CONFIRMED
    assert entries[1].status is LedgerStatus.WEAK
    assert ledger.sections["constraints"].status() is LedgerStatus.CONFIRMED


def test_later_grounded_answer_clears_prior_same_key_blocker() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python version unknown; cannot proceed",
            source=LedgerSource.BLOCKER,
            confidence=1.0,
            status=LedgerStatus.BLOCKED,
        ),
    )
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python 3.13 from pyproject.toml",
            source=LedgerSource.REPO_FACT,
            confidence=0.90,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    entries = ledger.sections["runtime_context"].entries
    assert entries[0].status is LedgerStatus.WEAK
    assert entries[1].status is LedgerStatus.CONFIRMED
    assert ledger.sections["runtime_context"].status() is LedgerStatus.CONFIRMED


def test_same_priority_higher_confidence_wins() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python 3.12",
            source=LedgerSource.REPO_FACT,
            confidence=0.60,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python 3.13",
            source=LedgerSource.REPO_FACT,
            confidence=0.90,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    entries = ledger.sections["runtime_context"].entries
    assert entries[0].status is LedgerStatus.WEAK
    assert entries[1].status is LedgerStatus.CONFIRMED


def test_same_priority_same_confidence_stays_conflicting_for_human_resolution() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python 3.12",
            source=LedgerSource.REPO_FACT,
            confidence=0.80,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    ledger.add_entry(
        "runtime_context",
        LedgerEntry(
            key="runtime.python",
            value="Python 3.13",
            source=LedgerSource.REPO_FACT,
            confidence=0.80,
            status=LedgerStatus.CONFIRMED,
        ),
    )

    entries = ledger.sections["runtime_context"].entries
    assert entries[0].status is LedgerStatus.CONFLICTING
    assert entries[1].status is LedgerStatus.CONFLICTING
    assert ledger.sections["runtime_context"].status() is LedgerStatus.CONFLICTING
    assert "runtime_context" in ledger.open_gaps()
    assert ledger.count_active_conflicting_entries() == 2


def test_resolve_conflict_reports_same_value_without_marking_conflict() -> None:
    existing = LedgerEntry(
        key="outputs.format",
        value="Stable stdout",
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.80,
        status=LedgerStatus.DEFAULTED,
    )
    incoming = LedgerEntry(
        key="outputs.format",
        value=" stable  stdout ",
        source=LedgerSource.ASSUMPTION,
        confidence=0.50,
        status=LedgerStatus.INFERRED,
    )

    assert resolve_conflict(existing, incoming).value == "same_value"


# ---------------------------------------------------------------------------
# deterministic_floor formula
# ---------------------------------------------------------------------------


def test_deterministic_floor_zero_for_minimal_ready_ledger() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    _fill_minimal_ready_ledger(ledger)
    # No open gaps, no conflicts; assumption_only depends on default sources
    floor = deterministic_floor(ledger)
    assert 0.0 <= floor <= 0.10


def test_deterministic_floor_grows_with_open_gaps() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    # ``from_goal`` populates the goal section. The remaining 9 required
    # sections are MISSING → floor = 0.05 * 9 = 0.45.
    floor = deterministic_floor(ledger)
    assert floor == pytest.approx(0.45, abs=0.001)


def test_deterministic_floor_adds_for_conflicting_entries() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    _fill_minimal_ready_ledger(ledger)
    base = deterministic_floor(ledger)
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.alt",
            value="Avoid local execution",
            source=LedgerSource.ASSUMPTION,
            confidence=0.7,
            status=LedgerStatus.CONFLICTING,
        ),
    )
    after = deterministic_floor(ledger)
    # Adding a conflicting entry should push the floor up by ~0.10 (within
    # rounding of the ratio term).
    assert after - base >= 0.09


def test_deterministic_floor_clamped_at_one() -> None:
    ledger = SeedDraftLedger.from_goal("Build a tool")
    # Stuff the ledger with many conflicting entries to drive floor past 1.0
    for index in range(20):
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key=f"constraints.alt_{index}",
                value=f"Conflict {index}",
                source=LedgerSource.ASSUMPTION,
                confidence=0.5,
                status=LedgerStatus.CONFLICTING,
            ),
        )
    floor = deterministic_floor(ledger)
    assert floor == 1.0


# ---------------------------------------------------------------------------
# Grade gate consumes ambiguity_score; the floor application happens in the
# pipeline (covered separately). Here we verify the gate still rejects high
# ambiguity_score on its own — proving the threshold path works for whatever
# value the pipeline writes.
# ---------------------------------------------------------------------------


def test_grade_gate_blocks_seed_when_ambiguity_score_above_threshold() -> None:
    ledger = SeedDraftLedger.from_goal("Build a habit tracker")
    _fill_minimal_ready_ledger(ledger)
    # Simulate the floor having been applied to bump score above the 0.20 gate
    seed = _seed(ambiguity_score=0.35)
    result = GradeGate().grade_seed(seed, ledger=ledger)
    assert result.grade == SeedGrade.C
    assert result.may_run is False
    assert any(b.code == "high_ambiguity_score" for b in result.blockers)


# ---------------------------------------------------------------------------
# AutoAnswerer USER_PREFERENCE upgrade
# ---------------------------------------------------------------------------


def test_answerer_upgrades_default_runtime_answer_to_user_preference() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"runtime_context": "Python 3.14 with uv"},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert answer.confidence == 0.83
    assert answer.text == "Python 3.14 with uv"
    runtime_entries = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert len(runtime_entries) == 1
    assert runtime_entries[0].source == LedgerSource.USER_PREFERENCE
    assert runtime_entries[0].status == LedgerStatus.CONFIRMED


def test_answerer_repo_fact_beats_user_preference_for_runtime() -> None:
    """REPO_FACT is grounded; caller preference must not override it."""
    ledger = SeedDraftLedger.from_goal("Update the CLI")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python 3.12 project managed with uv and Typer CLI."},
        evidence={"runtime_context": ("pyproject.toml",)},
        user_preferences={"runtime_context": "Rust with Cargo"},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source == AutoAnswerSource.REPO_FACT
    assert "Python 3.12" in answer.text
    assert "Rust" not in answer.text


def test_answerer_user_preference_log_format() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"runtime_context": "Python 3.14 with uv"},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.prefixed_text == "[from-auto][user_preference] Python 3.14 with uv"


def test_answerer_no_upgrade_without_matching_preference() -> None:
    """If user supplies a preference for a different section, the answer is unchanged."""
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"non_goals": "no analytics"},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    # Falls back to EXISTING_CONVENTION default, NOT user_preference
    assert answer.source != AutoAnswerSource.USER_PREFERENCE


def test_answerer_skips_empty_user_preference_values() -> None:
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"runtime_context": "   "},  # whitespace-only ignored
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source != AutoAnswerSource.USER_PREFERENCE


def test_answerer_upgrade_preserves_acceptance_criteria_pair() -> None:
    """When user preference upgrades verification_plan, the acceptance_criteria
    update from the original verification_answer is preserved (other-section
    updates survive the upgrade)."""
    ledger = SeedDraftLedger.from_goal("Build a tool")
    context = AutoAnswerContext(
        user_preferences={"verification_plan": "Run pytest with --strict markers"},
    )

    answer = AutoAnswerer().answer(
        "Which command output verifies the acceptance criteria?", ledger, context
    )

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    sections = {section for section, _ in answer.ledger_updates}
    assert "verification_plan" in sections
    # The original _verification_answer also seeded acceptance_criteria —
    # the upgrade must not have erased that.
    assert "acceptance_criteria" in sections


def test_answerer_assumption_status_for_blocker_skips_upgrade() -> None:
    """Blocker answers carry ``source=BLOCKER`` and must never be upgraded
    even if the caller has supplied a matching preference."""
    blocker_answer = AutoAnswer(
        text="Cannot safely decide automatically: high-risk topic",
        source=AutoAnswerSource.BLOCKER,
        confidence=1.0,
    )
    # Direct unit check on the helper (BLOCKER source is not in the upgradable set)
    from ouroboros.auto.answerer import _maybe_apply_user_preference

    upgraded = _maybe_apply_user_preference(
        blocker_answer,
        ("runtime_context",),
        AutoAnswerContext(user_preferences={"runtime_context": "Python 3.14"}),
    )
    assert upgraded is blocker_answer


# ---------------------------------------------------------------------------
# Pipeline integration — floor application at seed generation
# ---------------------------------------------------------------------------


import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore


def _ready_ledger(goal: str, *, evidence_backed: bool) -> SeedDraftLedger:
    """Build a seed-ready ledger.

    When ``evidence_backed=True`` all non-goal sections use REPO_FACT (so they
    land in the ``evidence_backed_sections`` summary surface) — minimal floor.
    When ``False`` they use CONSERVATIVE_DEFAULT (assumption-only) — higher
    floor via the assumption-ratio term.
    """
    ledger = SeedDraftLedger.from_goal(goal)
    section_values = {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }
    for section, value in section_values.items():
        if section == "non_goals":
            source = LedgerSource.NON_GOAL  # always evidence-backed
        elif evidence_backed:
            source = LedgerSource.REPO_FACT
        else:
            source = LedgerSource.CONSERVATIVE_DEFAULT
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=(LedgerStatus.CONFIRMED if evidence_backed else LedgerStatus.DEFAULTED),
            ),
        )
    return ledger


@pytest.mark.asyncio
async def test_pipeline_applies_floor_when_llm_underscores(tmp_path) -> None:
    """LLM returns a Seed with low ambiguity_score, but the ledger's
    assumption-only sections push the floor higher. The pipeline must persist
    the floored score, not the LLM's optimistic value."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ambiguity_score=0.05)

    state = AutoPipelineState(goal="Build a habit tracker", cwd=str(tmp_path))
    # Assumption-heavy ledger: all sections except goal/non_goals are
    # CONSERVATIVE_DEFAULT (assumption-only). 8 sections of 10 are
    # assumption-only → ratio 0.8 → floor term 0.04.
    ledger = _ready_ledger(state.goal, evidence_backed=False)
    # Add a CONFLICTING entry to push the floor above 0.05.
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.alt",
            value="Avoid local execution",
            source=LedgerSource.ASSUMPTION,
            confidence=0.6,
            status=LedgerStatus.CONFLICTING,
        ),
    )
    state.ledger = ledger.to_dict()
    state.phase = AutoPhase.SEED_GENERATION
    state.interview_session_id = "interview_1"
    state.interview_completed = True

    pre_floor = deterministic_floor(ledger)
    assert pre_floor > 0.05  # sanity: the LLM's 0.05 must be below the floor

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
    )

    await pipeline.run(state)

    persisted_score = state.seed_artifact["metadata"]["ambiguity_score"]
    assert persisted_score == pytest.approx(pre_floor, abs=0.001)
    assert persisted_score > 0.05


@pytest.mark.asyncio
async def test_pipeline_preserves_score_when_floor_lower_than_llm_score(tmp_path) -> None:
    """When the LLM-reported ambiguity_score already exceeds the floor, the
    persisted Seed must keep the LLM value verbatim (max(llm, floor) == llm)."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", "interview_1", seed_ready=True, completed=True)

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(session_id: str) -> Seed:  # noqa: ARG001
        return _seed(ambiguity_score=0.18)

    state = AutoPipelineState(goal="Build a habit tracker", cwd=str(tmp_path))
    # Evidence-backed ledger (REPO_FACT/NON_GOAL/USER_GOAL only): assumption
    # ratio = 0, no open gaps, no conflicts → floor ≈ 0.0. LLM score 0.18 wins.
    ledger = _ready_ledger(state.goal, evidence_backed=True)
    state.ledger = ledger.to_dict()
    state.phase = AutoPhase.SEED_GENERATION
    state.interview_session_id = "interview_1"
    state.interview_completed = True

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
    )

    await pipeline.run(state)

    persisted_score = state.seed_artifact["metadata"]["ambiguity_score"]
    assert persisted_score == pytest.approx(0.18)


# ---------------------------------------------------------------------------
# MCP handler argument plumbing
# ---------------------------------------------------------------------------


def test_mcp_handler_validates_user_preferences_keys() -> None:
    from ouroboros.mcp.tools.auto_handler import _parse_user_preferences

    # Empty / None inputs return empty dict
    assert _parse_user_preferences(None) == {}
    assert _parse_user_preferences("") == {}
    assert _parse_user_preferences({}) == {}

    # Valid input is normalised (trimmed)
    cleaned = _parse_user_preferences({"runtime_context": "  Python 3.14  "})
    assert cleaned == {"runtime_context": "Python 3.14"}

    # Unknown section name rejected
    with pytest.raises(ValueError, match="not a valid ledger section"):
        _parse_user_preferences({"banana": "foo"})

    # Empty value rejected
    with pytest.raises(ValueError, match="non-empty string"):
        _parse_user_preferences({"runtime_context": "   "})

    # Non-string value rejected
    with pytest.raises(ValueError, match="non-empty string"):
        _parse_user_preferences({"runtime_context": 42})

    # Non-dict input rejected
    with pytest.raises(ValueError, match="must be an object"):
        _parse_user_preferences("not-a-dict")

    # list[str] value accepted, joined with newlines
    cleaned = _parse_user_preferences({"constraints": ["one", "  two  ", ""]})
    assert cleaned == {"constraints": "one\ntwo"}

    # list with only empties rejected
    with pytest.raises(ValueError, match="non-empty string or list of strings"):
        _parse_user_preferences({"constraints": ["", "  "]})

    # list with bool rejected (bool is int subclass, but disallowed)
    with pytest.raises(ValueError, match="non-empty string or list of strings"):
        _parse_user_preferences({"constraints": [True]})

    # Unknown key suggests closest match
    with pytest.raises(ValueError, match=r"did you mean: 'constraints'"):
        _parse_user_preferences({"constraint": "x"})


def test_mcp_handler_context_provider_injects_user_preferences(tmp_path) -> None:
    from ouroboros.mcp.tools.auto_handler import _build_context_provider

    provider = _build_context_provider({"runtime_context": "Python 3.14 with uv"})
    context = provider(str(tmp_path))

    assert context.user_preferences == {"runtime_context": "Python 3.14 with uv"}
    # Repo facts come from base extractor — empty when no pyproject.toml
    assert isinstance(context.repo_facts, dict)


def test_mcp_handler_definition_advertises_user_preferences_param() -> None:
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    definition = AutoHandler().definition
    names = {param.name for param in definition.parameters}
    assert "user_preferences" in names


# ---------------------------------------------------------------------------
# Safety: USER_PREFERENCE must not bypass the risky-fallback gate for
# regulated topics (credentials, payments, security-sensitive choices, etc.)
# ---------------------------------------------------------------------------


def test_user_preference_does_not_bypass_regulated_data_blocker() -> None:
    """A caller-supplied preference must NOT silently bypass the
    risky-fallback safety gate for regulated personal data (PII/GDPR/HIPAA)
    or destructive bulk operations. The gate must still fire and produce a
    BLOCKER even when ``user_preferences`` would otherwise upgrade the answer.
    """
    ledger = SeedDraftLedger.from_goal("Build a data tool")
    context = AutoAnswerContext(
        user_preferences={"constraints": "Just delete everything without consent"},
    )

    for question in (
        "How should the system store PII records?",
        "Which database should we truncate when users are deleted?",
        "How should we handle GDPR data deletion?",
    ):
        answer = AutoAnswerer().answer(question, ledger, context)
        assert answer.source == AutoAnswerSource.BLOCKER, (
            f"USER_PREFERENCE bypassed safety gate for: {question}"
        )
        assert answer.blocker is not None
        # The user's text must not have made it into the blocker text.
        assert "delete everything" not in answer.text.lower()


def test_user_preference_for_safe_runtime_question_is_unaffected_by_gate() -> None:
    """The gate must only fire for regulated topics. A benign runtime
    preference question must still upgrade to USER_PREFERENCE."""
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"runtime_context": "Python 3.14 with uv"},
    )

    answer = AutoAnswerer().answer("Which runtime and framework should we use?", ledger, context)

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert answer.text == "Python 3.14 with uv"


def test_user_preference_for_verification_does_not_bypass_pii_gate() -> None:
    """VERIFICATION early-return path: user_preferences['verification_plan']
    for a regulated-data question must NOT land as a confirmed
    USER_PREFERENCE entry. The helper must short-circuit to a BLOCKER so the
    same risky-fallback policy fires regardless of which call site requested
    the upgrade."""
    ledger = SeedDraftLedger.from_goal("Build a data tool")
    context = AutoAnswerContext(
        user_preferences={"verification_plan": "skip the audit log"},
    )

    answer = AutoAnswerer().answer("How should we verify PII deletion?", ledger, context)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "skip the audit log" not in answer.text.lower()


def test_user_preference_for_acceptance_criteria_does_not_bypass_destructive_gate() -> None:
    """ACCEPTANCE_CRITERIA early-return path: user_preferences for a
    destructive-bulk question must be rejected as a BLOCKER."""
    ledger = SeedDraftLedger.from_goal("Build a data tool")
    context = AutoAnswerContext(
        user_preferences={"acceptance_criteria": "command exits 0 even if rows remain"},
    )

    answer = AutoAnswerer().answer(
        "Which tables should the migration truncate when users are deleted?",
        ledger,
        context,
    )

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "exits 0 even if rows remain" not in answer.text.lower()


def test_user_preference_for_verification_on_safe_question_still_upgrades() -> None:
    """Negative control for the new safety check: a benign verification
    question must still upgrade to USER_PREFERENCE."""
    ledger = SeedDraftLedger.from_goal("Build a small CLI")
    context = AutoAnswerContext(
        user_preferences={"verification_plan": "Run pytest with --strict markers"},
    )

    answer = AutoAnswerer().answer(
        "Which command output verifies the acceptance criteria?", ledger, context
    )

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert answer.text == "Run pytest with --strict markers"


def test_answer_gap_does_not_bypass_safety_via_synthetic_question() -> None:
    """answer_gap() replaces the user's actual question with a generic
    synthetic prompt. The helper must still see the ORIGINAL risky context
    via the converged goal text — otherwise a regulated task could smuggle
    a USER_PREFERENCE past the gate by triggering interview gap-filling."""
    ledger = SeedDraftLedger.from_goal(
        "Build a tool that exports PII records for compliance review"
    )
    context = AutoAnswerContext(
        user_preferences={"verification_plan": "skip the audit log"},
    )

    answer = AutoAnswerer().answer_gap("verification_plan", ledger, context)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "skip the audit log" not in answer.text.lower()


def test_answer_gap_with_safe_goal_still_upgrades_user_preference() -> None:
    """Negative control: when the goal is benign, answer_gap must still
    upgrade to USER_PREFERENCE."""
    ledger = SeedDraftLedger.from_goal("Build a small habit-tracker CLI")
    context = AutoAnswerContext(
        user_preferences={"verification_plan": "Run pytest with --strict markers"},
    )

    answer = AutoAnswerer().answer_gap("verification_plan", ledger, context)

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert answer.text == "Run pytest with --strict markers"


def test_user_preference_preserved_for_safe_regulated_product_question() -> None:
    """Regression guard: when a question is on the SAFE-product allowlist
    (e.g. 'Should the app export PII reports?'), the user's preference must
    NOT be dropped by the routing-block safe-product re-route. Earlier
    iterations of this PR added USER_PREFERENCE to _RISKY_FALLBACK_SOURCES,
    which silently collapsed the caller-supplied value back into the
    generic _product_behavior_answer template. The helper itself now
    enforces the safety policy at upgrade time, so post-upgrade USER_PREFERENCE
    answers must reach the caller verbatim."""
    ledger = SeedDraftLedger.from_goal("Build a privacy dashboard")
    context = AutoAnswerContext(
        user_preferences={
            "constraints": "Use the in-house compliance review queue for all exports"
        },
    )

    answer = AutoAnswerer().answer("Should the app export PII reports?", ledger, context)

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert "in-house compliance review queue" in answer.text


def test_user_preference_safe_account_deletion_question_preserved() -> None:
    """Second negative control on a different safe-product allowlist
    pattern (account/branch deletion permissions). The regulated noun is
    in the question, but the safe-allowlist marks it as a feature question,
    not a compliance-policy decision. USER_PREFERENCE must survive."""
    ledger = SeedDraftLedger.from_goal("Build a user account dashboard")
    context = AutoAnswerContext(
        user_preferences={
            "constraints": "Soft-delete accounts and queue hard-delete for the nightly job"
        },
    )

    answer = AutoAnswerer().answer(
        "Should users be able to delete their own accounts?", ledger, context
    )

    assert answer.source == AutoAnswerSource.USER_PREFERENCE
    assert "Soft-delete" in answer.text


# ---------------------------------------------------------------------------
# Determinism: section hint order must not depend on hash randomization
# ---------------------------------------------------------------------------


def test_section_hints_for_intents_iterates_in_dict_definition_order() -> None:
    """``_section_hints_for_intents`` must iterate ``_INTENT_TO_SECTIONS`` in
    its dict-definition order, not the hash-randomized order of the input
    frozenset. Multiple matching intents must yield a stable ordering across
    Python processes."""
    from ouroboros.auto.answerer import (
        _INTENT_TO_SECTIONS,
        QuestionIntent,
        _section_hints_for_intents,
    )

    multi_intents = frozenset(
        {
            QuestionIntent.RUNTIME_CONTEXT,
            QuestionIntent.NON_GOALS,
            QuestionIntent.PRODUCT_BEHAVIOR,
            QuestionIntent.ACTOR_IO,
        }
    )
    hints = _section_hints_for_intents(multi_intents)

    # Expected order is the order these intents appear in _INTENT_TO_SECTIONS:
    # NON_GOALS, ACTOR_IO, RUNTIME_CONTEXT, PRODUCT_BEHAVIOR.
    expected_order = []
    for intent, sections in _INTENT_TO_SECTIONS.items():
        if intent in multi_intents:
            for section in sections:
                if section not in expected_order:
                    expected_order.append(section)
    assert list(hints) == expected_order


def test_section_hints_for_intents_first_match_is_stable() -> None:
    """Calling ``_section_hints_for_intents`` repeatedly with the same input
    must yield identical output — guards against frozenset iteration order
    creeping back in via subtle refactors."""
    from ouroboros.auto.answerer import (
        QuestionIntent,
        _section_hints_for_intents,
    )

    intents = frozenset(
        {
            QuestionIntent.RUNTIME_CONTEXT,
            QuestionIntent.ACTOR_IO,
            QuestionIntent.NON_GOALS,
        }
    )
    runs = [_section_hints_for_intents(intents) for _ in range(50)]
    assert len(set(runs)) == 1, "section_hints order is not stable across calls"


# ---------------------------------------------------------------------------
# Resume: user_preferences must persist on AutoPipelineState
# ---------------------------------------------------------------------------


def test_auto_pipeline_state_round_trips_user_preferences() -> None:
    from ouroboros.auto.state import AutoPipelineState

    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/x")
    state.user_preferences = {"runtime_context": "Python 3.14 with uv"}

    raw = state.to_dict()
    assert raw["user_preferences"] == {"runtime_context": "Python 3.14 with uv"}

    restored = AutoPipelineState.from_dict(raw)
    assert restored.user_preferences == {"runtime_context": "Python 3.14 with uv"}


def test_auto_pipeline_state_loads_legacy_dump_without_user_preferences() -> None:
    """Old persisted state files predating Phase 1 must still load — the
    field is backfilled to an empty dict."""
    from ouroboros.auto.state import AutoPipelineState

    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/x")
    raw = state.to_dict()
    raw.pop("user_preferences", None)  # simulate legacy dump

    restored = AutoPipelineState.from_dict(raw)
    assert restored.user_preferences == {}


def test_auto_pipeline_state_validates_user_preferences_shape() -> None:
    import pytest as _pytest

    from ouroboros.auto.state import AutoPipelineState

    with _pytest.raises(ValueError, match="user_preferences"):
        AutoPipelineState(
            goal="Build a CLI",
            cwd="/tmp/x",
            user_preferences={"runtime_context": ""},  # empty value
        )._validate_loaded()

    with _pytest.raises(ValueError, match="user_preferences"):
        AutoPipelineState(
            goal="Build a CLI",
            cwd="/tmp/x",
            user_preferences={"": "value"},  # empty key
        )._validate_loaded()


def test_auto_store_persists_user_preferences_across_save_load(tmp_path) -> None:
    """End-to-end round trip: persist user_preferences on a saved state file
    and reload — values must be identical."""
    from ouroboros.auto.state import AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.user_preferences = {"runtime_context": "Python 3.14 with uv"}
    store = AutoStore(tmp_path)
    store.save(state)

    reloaded = store.load(state.auto_session_id)
    assert reloaded.user_preferences == {"runtime_context": "Python 3.14 with uv"}
