"""Closure labels for interview events: surface, answer source, gate snapshot."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AUTO_COMPLETE_STREAK_REQUIRED,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
    get_completion_floor_failures,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.interview_closure import (
    HOST_LABELS,
    AnswerSource,
    ClosureVerdict,
    InterviewSurface,
    answer_source,
    closure_snapshot,
    current_interview_surface,
    surface_fields,
    use_interview_surface,
)
from ouroboros.mcp.host_context import HostFamily, MCPHostContext, from_sdk_context

JUSTIFICATION = "secret rationale text that must never reach an event field"


def _sdk_context(name: str | None) -> SimpleNamespace:
    client_info = SimpleNamespace(name=name) if name is not None else None
    params = SimpleNamespace(client_info=client_info) if client_info is not None else None
    return SimpleNamespace(
        session=SimpleNamespace(
            client_params=params,
            client_capabilities=SimpleNamespace(extensions=None, experimental=None),
        )
    )


def _score(
    overall: float,
    goal: float,
    constraint: float,
    success: float,
    context: float | None = None,
) -> AmbiguityScore:
    def component(name: str, value: float) -> ComponentScore:
        return ComponentScore(
            name=name, clarity_score=value, weight=0.25, justification=JUSTIFICATION
        )

    return AmbiguityScore(
        overall_score=overall,
        breakdown=ScoreBreakdown(
            goal_clarity=component("Goal Clarity", goal),
            constraint_clarity=component("Constraint Clarity", constraint),
            success_criteria_clarity=component("Success Criteria Clarity", success),
            context_clarity=None if context is None else component("Context Clarity", context),
        ),
    )


def _state(*, answered: int = 3, streak: int = 0, brownfield: bool = False) -> InterviewState:
    state = InterviewState(
        interview_id="interview_closure_test",
        initial_context="ctx",
        is_brownfield=brownfield,
        completion_candidate_streak=streak,
    )
    for index in range(1, answered + 1):
        state.rounds.append(
            InterviewRound(round_number=index, question=f"Q{index}?", user_response=f"A{index}")
        )
    state.rounds.append(
        InterviewRound(round_number=answered + 1, question="Pending?", user_response=None)
    )
    return state


# --- surface ---------------------------------------------------------------


def test_host_labels_match_the_mcp_host_family_enum() -> None:
    assert {family.value for family in HostFamily} == HOST_LABELS


def test_surface_defaults_to_unknown_outside_mcp_and_without_a_label() -> None:
    assert surface_fields(None) == {"surface": "unknown", "host": None}
    # The MCP layer's default (internal runtime) context is not an MCP host.
    assert surface_fields(MCPHostContext()) == {"surface": "unknown", "host": None}


@pytest.mark.parametrize(
    ("client_name", "host"),
    [
        ("claude-code", "claude_code"),
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("some-editor", "other_known"),
        (None, "unknown"),
    ],
)
def test_surface_inside_an_mcp_request_reuses_the_normalized_client_identity(
    client_name: str | None, host: str
) -> None:
    context = from_sdk_context(_sdk_context(client_name))

    assert surface_fields(context) == {"surface": "mcp", "host": host}


def test_raw_client_name_is_never_recorded() -> None:
    context = from_sdk_context(_sdk_context("My Editor 9.1 <user@example.com>"))

    fields = surface_fields(context)

    assert fields == {"surface": "mcp", "host": "other_known"}
    assert "example.com" not in json.dumps(fields)


def test_an_unrecognized_host_value_is_recorded_as_unknown() -> None:
    forged = SimpleNamespace(is_mcp_host=True, host_family=SimpleNamespace(value="evil<script>"))

    assert surface_fields(forged) == {"surface": "mcp", "host": "unknown"}


def test_explicit_label_wins_and_keeps_the_host() -> None:
    context = from_sdk_context(_sdk_context("claude-code"))

    with use_interview_surface(InterviewSurface.AUTO):
        assert surface_fields(context) == {"surface": "auto", "host": "claude_code"}
        assert current_interview_surface() is InterviewSurface.AUTO
    with use_interview_surface(InterviewSurface.CONSOLE):
        assert surface_fields(None) == {"surface": "console", "host": None}
    assert current_interview_surface() is None


def test_label_is_reset_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), use_interview_surface(InterviewSurface.CONSOLE):
        raise RuntimeError("boom")
    assert current_interview_surface() is None


def test_label_outside_the_closed_set_is_rejected() -> None:
    with pytest.raises(ValueError), use_interview_surface("browser"):  # type: ignore[arg-type]
        pass
    assert current_interview_surface() is None


# --- answer source ---------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("[from-code][auto-confirmed] Python 3.12 (pyproject.toml)", "auto_confirmed"),
        ("[from-code] FastAPI in app/main.py", "from_code"),
        ("  [FROM-REPO] uses poetry", "from_code"),
        ("[from-research] Stripe allows 100 req/s", "from_research"),
        ("[from-user] Yes", "from_user"),
        ("[from-user][refined][closure] Decision: ship", "from_user"),
        ("[from-auto][requester] keep it small", "from_user"),
        ("[from-auto] conservative default", "from_auto"),
        ("[from-auto][safe-default-synthesis] defaults", "from_auto"),
        ("[from-safe-default] none", "from_auto"),
        ("[from-assumption] guess", "from_auto"),
        ("[from-inference] inferred", "from_auto"),
        ("[from-data] 42 rows", "unknown"),
    ],
)
def test_answer_source_reads_only_the_advertised_prefix(answer: str, expected: str) -> None:
    assert answer_source(answer, surface=InterviewSurface.MCP) == expected
    # The label never contains answer text.
    assert answer_source(answer) in {source.value for source in AnswerSource}


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("requester", "from_user"), ("repo_fact", "from_code"), ("assumption", "from_auto")],
)
def test_declared_provenance_wins_over_the_prefix(declared: str, expected: str) -> None:
    assert answer_source("[from-user] text", {"answer_provenance": declared}) == expected


def test_undeclared_or_unknown_provenance_falls_back_to_the_prefix() -> None:
    assert answer_source("[from-research] x", {"answer_provenance": "bogus"}) == "from_research"
    assert answer_source("[from-research] x", {"answer_provenance": True}) == "from_research"


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        (InterviewSurface.CONSOLE, "from_user"),
        (InterviewSurface.CLI_INIT, "from_user"),
        (InterviewSurface.MCP, "unknown"),
        (InterviewSurface.AUTO, "unknown"),
        (InterviewSurface.UNKNOWN, "unknown"),
    ],
)
def test_unprefixed_answer_is_from_user_only_where_a_person_types_it(
    surface: InterviewSurface, expected: str
) -> None:
    assert answer_source("It creates weekly reports.", surface=surface) == expected
    with use_interview_surface(surface):
        assert answer_source("It creates weekly reports.") == expected


def test_unprefixed_answer_without_any_surface_is_unknown() -> None:
    assert answer_source("plain") == "unknown"


@pytest.mark.parametrize("answer", ["", "   ", None, 7, ["[from-user] x"]])
def test_empty_or_non_text_answer_is_unknown(answer: object) -> None:
    assert answer_source(answer, surface=InterviewSurface.CONSOLE) == "unknown"


# --- closure snapshot ------------------------------------------------------


def test_unscored_turn_reports_state_counts_only() -> None:
    snapshot = closure_snapshot(_state(answered=2, streak=0), None)

    assert snapshot == {
        "answered_rounds": 2,
        "is_brownfield": False,
        "threshold": AMBIGUITY_THRESHOLD,
        "ambiguity_score": None,
        "clarity": None,
        "floors_passed": None,
        "streak": 0,
        "streak_required": AUTO_COMPLETE_STREAK_REQUIRED,
        "verdict": "unscored",
    }


@pytest.mark.parametrize(
    ("score", "streak", "verdict"),
    [
        (_score(0.43, 0.7, 0.5, 0.6), 0, "above_threshold"),
        (_score(0.18, 0.9, 0.6, 0.9), 0, "floors_unmet"),
        (_score(0.12, 0.9, 0.9, 0.9), 1, "qualified"),
        (_score(0.12, 0.9, 0.9, 0.9), 2, "passed"),
    ],
)
def test_scored_verdicts_follow_the_gate(score: AmbiguityScore, streak: int, verdict: str) -> None:
    snapshot = closure_snapshot(_state(streak=streak), score)

    assert snapshot["verdict"] == verdict
    assert snapshot["ambiguity_score"] == score.overall_score
    assert snapshot["streak"] == streak


def test_scored_snapshot_records_each_dimension_and_floor() -> None:
    snapshot = closure_snapshot(_state(), _score(0.18, 0.9, 0.6, 0.7))

    assert snapshot["clarity"] == {
        "goal_clarity": 0.9,
        "constraint_clarity": 0.6,
        "success_criteria_clarity": 0.7,
    }
    assert snapshot["floors_passed"] == {
        "goal_clarity": True,
        "constraint_clarity": False,
        "success_criteria_clarity": True,
    }


def test_brownfield_snapshot_includes_the_context_floor() -> None:
    passing = closure_snapshot(
        _state(brownfield=True, streak=2), _score(0.1, 0.9, 0.9, 0.9, context=0.7)
    )
    missing = closure_snapshot(_state(brownfield=True), _score(0.1, 0.9, 0.9, 0.9))

    assert passing["floors_passed"]["context_clarity"] is True
    assert passing["verdict"] == "passed"
    assert missing["clarity"]["context_clarity"] is None
    assert missing["floors_passed"]["context_clarity"] is False
    assert missing["verdict"] == "floors_unmet"


def test_greenfield_snapshot_omits_the_context_floor() -> None:
    snapshot = closure_snapshot(_state(), _score(0.1, 0.9, 0.9, 0.9, context=0.1))

    assert "context_clarity" not in snapshot["clarity"]


def test_safe_default_close_is_labeled_whether_or_not_it_was_scored() -> None:
    assert closure_snapshot(_state(), None, safe_default=True)["verdict"] == "safe_default"
    scored = closure_snapshot(_state(), _score(0.5, 0.5, 0.5, 0.5), safe_default=True)
    assert scored["verdict"] == "safe_default"
    assert scored["ambiguity_score"] == 0.5


def test_scores_are_rounded() -> None:
    snapshot = closure_snapshot(_state(), _score(0.39751234, 0.712345678, 0.5, 0.6))

    assert snapshot["ambiguity_score"] == 0.3975
    assert snapshot["clarity"]["goal_clarity"] == 0.7123


def test_snapshot_holds_numbers_and_labels_only() -> None:
    snapshot = closure_snapshot(_state(), _score(0.3, 0.8, 0.7, 0.7))
    encoded = json.dumps(snapshot)

    assert JUSTIFICATION not in encoded
    assert "Q1?" not in encoded and "A1" not in encoded
    assert snapshot["verdict"] in {verdict.value for verdict in ClosureVerdict}


@pytest.mark.parametrize("brownfield", [False, True])
@pytest.mark.parametrize("streak", [0, 1, 2])
def test_snapshot_agrees_with_the_gate_predicates(brownfield: bool, streak: int) -> None:
    values = (0.5, 0.6, 0.65, 0.7, 0.75, 0.8)
    for overall in (0.1, 0.2, 0.21, 0.5):
        for goal in values:
            for constraint in values:
                for context in (None, 0.55, 0.6):
                    score = _score(overall, goal, constraint, 0.7, context=context)
                    snapshot = closure_snapshot(_state(streak=streak, brownfield=brownfield), score)
                    qualifies = qualifies_for_seed_completion(score, is_brownfield=brownfield)
                    floors_ok = not get_completion_floor_failures(score, is_brownfield=brownfield)

                    assert all(snapshot["floors_passed"].values()) is floors_ok
                    assert (snapshot["verdict"] in {"qualified", "passed"}) is qualifies
                    assert (snapshot["verdict"] == "passed") is (
                        qualifies and streak >= AUTO_COMPLETE_STREAK_REQUIRED
                    )
