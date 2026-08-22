"""PM question-advisory lanes: what the RFC decided, checked by running it.

Each test names the decision from RFC Q00/ouroboros#1937 it holds, because the
value of most of these is not that the code works but that a particular thing
*cannot be expressed* — and an unexpressible thing has no failing behaviour to
point at later if the guard is removed.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result as CoreResult
from ouroboros.mcp.tools.fanout import (
    FANOUT_KIND_QUESTION_ADVISORY,
    FanoutRegistry,
    submit_fanout_results,
)
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
from ouroboros.mcp.tools.question_advisory import (
    attach_question_advisory,
    build_question_advisory_subagents,
)
from ouroboros.orchestrator.capabilities.pm_schemas import (
    _pm_question_advisory_fanout_metadata,
    pm_code_context_answer_contract,
    pm_repo_id,
    pm_repository_roster,
    stable_pm_question_identity,
)

QUESTION = "What happens today when a subscription lapses mid-period?"


def _found_policy(roster: list[dict[str, str]], **overrides: Any) -> dict[str, Any]:
    """Return a well-formed ``PolicyCarried`` answer for the code lane.

    Built in one place: a test that spells the carried state out by hand
    records today's contract in a dozen places, and the last round's lesson is
    that a payload with more than one spelling is a payload whose rules stop
    agreeing.
    """
    payload: dict[str, Any] = {
        "question_identity": stable_pm_question_identity(QUESTION),
        "lane_id": "code_context",
        "examined": [
            {
                "repo_id": roster[0]["repo_id"] if roster else "api-12345678",
                "policy_claims": [
                    {
                        "path": "src/billing.py",
                        "policy_claim": "grace period applies",
                        "plain_statement": "a grace period applies after lapse",
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _state(schema: dict[str, Any], title: str) -> dict[str, Any]:
    """Return one named answer state from the code lane's ``oneOf``."""
    return next(state for state in schema["oneOf"] if state["title"] == title)


def _stub_engine(state: InterviewState, questions: tuple[str, ...] = ()) -> Any:
    """A PM engine that loads, records and asks, and does nothing else.

    One stub for the whole file rather than a copy per test. The copies were
    where the two runtimes drifted apart the first time this was built, and a
    test suite that reproduces the drift cannot notice it.
    """
    upcoming = iter(questions)

    class _Engine:
        codebase_context = ""
        _selected_brownfield_repos: list[dict[str, str]] = []
        deferred_items: list[str] = []
        decide_later_items: list[str] = []
        classifications: list[Any] = []

        async def load_state(self, _sid: str) -> Any:
            return CoreResult.ok(state)

        async def save_state(self, s: InterviewState) -> Any:
            return CoreResult.ok(s)

        async def record_response(self, s: InterviewState, answer: str, question: str) -> Any:
            s.record_answer(question, answer)
            return CoreResult.ok(s)

        async def ask_next_question(self, _s: InterviewState) -> Any:
            return CoreResult.ok(next(upcoming))

        async def check_completion(self, _s: InterviewState) -> None:
            return None

        def get_pending_reframe(self) -> None:
            return None

        def get_last_classification(self) -> None:
            return None

        def restore_meta(self, _m: Any) -> None:
            return None

        def compute_deferred_diff(self, _a: int, _b: int) -> dict[str, Any]:
            return {
                "new_deferred": [],
                "new_decide_later": [],
                "deferred_count": 0,
                "decide_later_count": 0,
            }

    return _Engine()


@pytest.fixture
def roster() -> list[dict[str, str]]:
    return pm_repository_roster(
        [
            {"path": "/repo/api", "name": "api"},
            {"path": "/repo/web", "name": "web"},
        ]
    )


@pytest.fixture
def registry(tmp_path: Path) -> FanoutRegistry:
    return FanoutRegistry(tmp_path / "fanout")


def _attach(
    registry: FanoutRegistry,
    roster: list[dict[str, str]],
    *,
    session_id: str = "pm-1",
    question: str = QUESTION,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_pm_interview",
        session_id=session_id,
        question=question,
        repository_roster=roster,
        fanout_registry=registry,
    )
    return meta


def _submit(
    registry: FanoutRegistry,
    meta: dict[str, Any],
    code_output: dict[str, Any],
    *,
    session_id: str = "pm-1",
    identity: str | None = None,
) -> dict[str, Any]:
    identity = identity or stable_pm_question_identity(QUESTION)
    return submit_fanout_results(
        registry,
        session_id=session_id,
        correlation_key="context.lane_id",
        results=[
            {"key": "code_context", "content": code_output},
            {
                "key": "data_context",
                "content": {
                    "question_identity": identity,
                    "lane_id": "data_context",
                    "data_needed": False,
                    "no_evidence_reason": "not_a_measurement",
                },
            },
        ],
        fanout_id=meta["question_advisory_fanout_id"],
    )


# ── Decision 1: only the two evidence lanes ──────────────────────────────


def test_pm_runs_only_the_two_evidence_lanes() -> None:
    lanes = _pm_question_advisory_fanout_metadata()["lanes"]
    assert [lane["lane_id"] for lane in lanes] == ["code_context", "data_context"]


def test_no_lane_produces_a_recommended_draft() -> None:
    """The interview synthesizes answer options; PM synthesizes evidence.

    Asserted on the synthesis contract rather than on the lane list because it
    is the shape that would quietly re-admit a draft: keeping the interview's
    ``answer_advisory`` output would ask the parent for a recommended answer
    even with no lane producing one.
    """
    contract = _pm_question_advisory_fanout_metadata()["synthesis_contract"]
    assert contract["output_shape"] == "evidence_beside_question"
    assert contract["include_recommended_draft"] is False
    assert contract["preserve_user_agency"] is True


# ── Decision 10: the rule the child reads is the rule the catalog declares ─


def _code_lane_prompt(roster: list[dict[str, str]]) -> str:
    """Return the prompt actually emitted to the PM ``code_context`` child."""
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_pm_interview",
        session_id="pm-prompt",
        question=QUESTION,
        repository_roster=roster,
    )
    payloads = build_question_advisory_subagents(meta["question_advisory_request"])
    return next(
        payload.to_dict()["prompt"]
        for payload in payloads
        if payload.context["lane_id"] == "code_context"
    )


def test_the_emitted_prompt_renders_the_catalog_answer_rule(
    roster: list[dict[str, str]],
) -> None:
    """The lane brief renders ``child_answer_rule``; it does not restate it.

    Read from the catalog rather than compared against a literal, so this fails
    the moment the two spellings diverge again. They did diverge: the rule was
    changed in the catalog while a hand-written copy in the brief was left
    behind, and ``child_answer_rule`` had no reader at all to notice.
    """
    rule = _pm_question_advisory_fanout_metadata()["child_answer_rule"]
    assert rule and rule in _code_lane_prompt(roster)


def test_the_emitted_prompt_speaks_evidence_only(
    roster: list[dict[str, str]],
) -> None:
    """One rule with one spelling: the finding is evidence beside the question.

    The prompt once told the child its finding would be shown for confirmation
    and recorded; that flow is retired (RFC #2222), and a prompt still saying
    so would instruct the child toward a recording that no longer exists.
    """
    prompt = _code_lane_prompt(roster)
    assert "shown to the user for confirmation" not in prompt
    assert "adopted fact" not in prompt
    assert "evidence beside the question" in prompt


def test_the_emitted_prompt_carries_no_forwarding_instruction(
    roster: list[dict[str, str]],
) -> None:
    """Findings are evidence, never answers (RFC #2222).

    The v1 forwarding fields gated a recorded adopted fact; with nothing
    recorded, a prompt still naming them would instruct the child toward a
    shape the contract now rejects.
    """
    prompt = _code_lane_prompt(roster)
    for field in ("answer_prefix", "requires_user_confirmation", "user_confirmation_prompt"):
        assert field not in prompt


def _prompt_json_block(prompt: str, heading: str) -> Any:
    """Return the parsed JSON fenced under one ``##`` heading of the prompt."""
    match = re.search(rf"^{re.escape(heading)}.*?\n```json\n(.*?)\n```", prompt, re.S | re.M)
    assert match is not None, f"no {heading!r} block in the emitted prompt"
    return json.loads(match.group(1))


def test_the_child_is_given_the_contract_it_is_judged_by(
    roster: list[dict[str, str]],
) -> None:
    """Named fields are not the contract, and the prompt said they were.

    The Output section tells the child its contract is "rendered in full above".
    Nothing rendered it: the brief named three forwarding fields in prose and
    carried none of the closed part, so the ordinary empty-roster answer required
    inventing ``no_repository_in_roster`` -- a literal the prompt never showed --
    and a reasonable answer in any other wording was rejected. The lane is
    required, so that was not a degraded consultation but no consultation.

    Pinned as equality rather than as a list of substrings. A substring list is
    the thing that goes stale when the contract grows a field, and it would have
    passed against the broken prompt for every field the prose happened to name.
    """
    contract = pm_code_context_answer_contract()
    rendered = _prompt_json_block(_code_lane_prompt(roster), "## Answer Contract")
    assert rendered == contract

    # The enum the reviewer's probe had to guess is reachable by value now, not
    # by the child knowing the codebase.
    prompt = _code_lane_prompt(roster)
    schema = contract["response_model_schema"]
    reasons = _state(schema, "NothingExamined")["properties"]["nothing_examined_reason"]["enum"]
    assert reasons
    for reason in reasons:
        assert reason in prompt


def test_the_whole_roster_reaches_the_child_at_the_declared_ceiling() -> None:
    """A roster cut mid-array is cited as a complete one.

    ``examined`` accepts up to 64 entries, and the roster used to be rendered
    through the generic 2,400-character advisory bound -- crossed by 15
    ordinarily-named repositories. ``_bounded_json`` answers that by cutting,
    which does not produce a rejected answer: the child reports honestly on
    everything it was shown and says it examined the roster, while the
    repositories past the cut were never in front of it.

    So the ceiling this checks is the contract's own, and the assertion is on
    every identifier rather than on a count.
    """
    roster = pm_repository_roster(
        [
            {
                "name": f"payments-ledger-service-{index:02d}",
                "path": f"/Users/dev/workspace/acme-platform/payments-ledger-service-{index:02d}",
            }
            for index in range(64)
        ]
    )
    assert len(roster) == 64

    prompt = _code_lane_prompt(roster)
    rendered = _prompt_json_block(prompt, "## Repository Roster")
    assert rendered == roster
    assert "... [truncated]" not in prompt


def test_long_registration_notes_shorten_themselves_and_never_the_roster() -> None:
    """The free-text field yields; the list does not.

    ``name`` and ``desc`` are typed by a person at ``ooo brownfield``
    registration and stored in unbounded ``Text`` columns, so they are the roster
    fields that can grow without limit -- and the ones nothing is keyed on, since
    the child cites by ``repo_id`` and reads at ``path``. Both are covered here:
    bounding one free-text field and leaving its twin is the shape of the
    original mistake.

    A size ceiling on the whole roster stood here first and had the sign
    backwards: it bounded the list, whose loss is fatal, and left ``desc``
    unbounded, whose loss is free. Sixty-four repositories carrying long notes
    then attached *no* lanes at all, and the resulting turn was indistinguishable
    from an ordinary greenfield question -- the same ungrounded PM decision the
    truncation bug produced, reached by refusing instead of by cutting. So the
    assertion is that every repository still arrives, however long the notes.
    """
    roster = pm_repository_roster(
        [
            {
                "name": f"payments-ledger-service-{index:02d}-" + "n" * 300,
                "path": f"/Users/dev/workspace/acme-platform/payments-ledger-service-{index:02d}",
                "desc": "why this repository was registered, at length. " * 20,
            }
            for index in range(64)
        ]
    )
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_pm_interview",
        session_id="pm-long-notes",
        question=QUESTION,
        repository_roster=roster,
    )
    assert len(meta["question_advisory_subagents"]) == 2

    rendered = _prompt_json_block(_code_lane_prompt(roster), "## Repository Roster")
    # What the answer is keyed on arrives exactly: the identifier it cites by and
    # the location it reads at.
    assert [entry["repo_id"] for entry in rendered] == [entry["repo_id"] for entry in roster]
    assert [entry["path"] for entry in rendered] == [entry["path"] for entry in roster]
    # The notes are what gave, and they gave visibly rather than silently.
    for field_name in ("name", "desc"):
        assert all(
            len(entry[field_name]) < len(original[field_name])
            for entry, original in zip(rendered, roster, strict=True)
        )
        assert all(entry[field_name].endswith("…") for entry in rendered)


# ── Decision 2: one door in, and no prefix that skips the person ─────────


def test_no_tool_parameter_holds_a_finding_beside_the_answer() -> None:
    """A finding enters where every answer enters, and there is no second door.

    This is an absence, so it has no failing behaviour to point at later: put the
    parameter back and nothing breaks until a host uses it, which is exactly how
    the two-door shape survived six review rounds. Pinned here instead.

    Asserted against the sibling tool as well, because "PM has a parameter the
    interview does not" was the whole diagnosis.
    """
    from ouroboros.mcp.tools.definitions import InterviewHandler

    pm_params = {
        param.name for param in PMInterviewHandler(data_dir=Path("/x")).definition.parameters
    }
    interview_params = {param.name for param in InterviewHandler().definition.parameters}

    assert "evidence" not in pm_params
    assert "evidence" not in interview_params
    assert {"answer", "last_question"} <= pm_params


def test_no_state_carries_a_forwarding_field() -> None:
    """Findings are evidence, never answers (RFC #2222).

    The v1 confirmation machinery — ``answer_prefix``,
    ``requires_user_confirmation``, ``user_confirmation_prompt`` — gated a
    recorded adopted fact. With nothing recorded there is nothing to gate, and
    the closed shapes make an answer that still carries those fields rejected
    rather than quietly honoured.
    """
    schema = pm_code_context_answer_contract()["response_model_schema"]

    for state in schema["oneOf"]:
        assert state["additionalProperties"] is False
        for field in (
            "answer_prefix",
            "requires_user_confirmation",
            "user_confirmation_prompt",
            "confidence",
            "answer_text",
        ):
            assert field not in state["properties"], (state["title"], field)


def test_a_carried_policy_is_accepted_and_a_forwarding_field_is_not(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Both directions: the plain carried state admits; a v1 relic refuses."""
    accepted = _submit(registry, _attach(registry, roster), _found_policy(roster))
    assert accepted["status"] == "complete"

    refused = _submit(
        registry,
        _attach(registry, roster, session_id="pm-2"),
        _found_policy(roster, answer_prefix="[from-code]"),
        session_id="pm-2",
    )
    assert refused["status"] == "partial"
    assert "code_context" in refused["contract_violations"]


def test_a_finding_carrying_the_retired_confirmation_flag_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """The field is gone, so any value of it — True or False — is unspellable."""
    result = _submit(
        registry,
        _attach(registry, roster),
        _found_policy(roster, requires_user_confirmation=False),
    )
    assert result["status"] == "partial"
    assert "code_context" in result["contract_violations"]


# ── Decision 3: the lane speaks about itself ─────────────────────────────


def test_a_free_text_reason_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """A sentence is where a claim about the PM's system would be written."""
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "examined": [],
            "nothing_examined_reason": "there is no such policy anywhere in your system",
        },
    )
    assert result["status"] == "partial"
    assert "nothing_examined_reason" in str(result["contract_violations"]["code_context"])


def test_scope_is_required_even_when_nothing_was_read(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """An empty roster reports itself, not "found nothing"."""
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "examined": [],
            "nothing_examined_reason": "no_repository_in_roster",
        },
    )
    assert result["status"] == "complete"


def test_an_answer_without_its_scope_is_rejected() -> None:
    schema = pm_code_context_answer_contract()["response_model_schema"]
    for state in schema["oneOf"]:
        assert "examined" in state["required"]


def test_no_reason_restates_what_the_entries_already_say() -> None:
    """ "Found nothing in what I read" is the shape, not a reason to choose.

    Keeping it as a constant would leave a value that can disagree with the
    entries beside it — an empty scope claiming it searched, or a populated one
    claiming the roster was empty. It is derived from the entries instead, so
    only reasons about *not reading* remain, and they exist in the one state
    where there are no entries to speak.
    """
    from ouroboros.orchestrator.capabilities.pm_schemas import PM_NOTHING_EXAMINED_REASONS

    assert "no_policy_found_in_examined_repositories" not in PM_NOTHING_EXAMINED_REASONS
    schema = pm_code_context_answer_contract()["response_model_schema"]
    assert "nothing_examined_reason" in _state(schema, "NothingExamined")["properties"]
    for title in ("NoPolicyInExaminedRepositories", "PolicyCarried"):
        assert "nothing_examined_reason" not in _state(schema, title)["properties"]
    # And no boolean restating whether a claim is carried.
    for state in schema["oneOf"]:
        assert "policy_found" not in state["properties"]


# ── Decision 4: a total answer, so the lane can be required ──────────────


def test_both_lanes_are_required_and_both_have_a_no_op_answer() -> None:
    lanes = _pm_question_advisory_fanout_metadata()["lanes"]
    assert all(lane["required"] for lane in lanes)
    code_states = {
        state["title"]
        for state in pm_code_context_answer_contract()["response_model_schema"]["oneOf"]
    }
    assert code_states == {
        "NothingExamined",
        "NoPolicyInExaminedRepositories",
        "PolicyCarried",
    }


# ── Decision 6: the roster is a boundary, decided from the value ─────────


def test_evidence_from_outside_the_roster_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        _found_policy(
            roster,
            examined=[
                {
                    "repo_id": "elsewhere-deadbeef",
                    "policy_claims": [
                        {
                            "path": "src/x.py",
                            "policy_claim": "something",
                            "plain_statement": "something, plainly",
                        }
                    ],
                }
            ],
        ),
    )
    assert result["status"] == "partial"
    assert "not in this session's roster" in str(result["contract_violations"]["code_context"])


def test_a_scope_claiming_an_unoffered_repository_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "examined": [{"repo_id": "ghost-11111111", "policy_claims": []}],
        },
    )
    assert result["status"] == "partial"
    assert "examined" in str(result["contract_violations"]["code_context"])


def test_one_repository_cannot_hold_two_entries(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """The fold removed a disagreement; a repeated key would rebuild it.

    Two entries for the same repository — one carrying a claim, one saying it
    was read and clean — is the round-8 contradiction one level down. Draft
    2020-12 has ``uniqueItems`` for whole items and nothing for uniqueness by
    property, so this is decided at re-entry, exactly as the data lane decides a
    repeated ``group`` key.
    """
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        _found_policy(
            roster,
            examined=[
                {
                    "repo_id": roster[0]["repo_id"],
                    "policy_claims": [
                        {
                            "path": "src/billing.py",
                            "policy_claim": "grace period applies",
                            "plain_statement": "a grace period applies after lapse",
                        }
                    ],
                },
                {"repo_id": roster[0]["repo_id"], "policy_claims": []},
            ],
        ),
    )
    assert result["status"] == "partial"
    assert "more than one entry" in str(result["contract_violations"]["code_context"])


def test_a_claim_cannot_name_a_repository_the_answer_did_not_examine(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Round 8's probe, in the shape it would have to take now.

    It declared only the API repository examined while citing the web
    repository's code, and re-entry returned ``complete``. There is no longer a
    second list to disagree with the first: a claim's repository *is* the entry
    holding it, so the only way to spell the probe is to smuggle a ``repo_id``
    into the claim, and the claim shape is closed.
    """
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        _found_policy(
            roster,
            examined=[
                {
                    "repo_id": roster[0]["repo_id"],
                    "policy_claims": [
                        {
                            "repo_id": roster[1]["repo_id"],
                            "path": "src/checkout.ts",
                            "policy_claim": "revoked immediately",
                            "plain_statement": "access ends immediately",
                        }
                    ],
                }
            ],
        ),
    )
    assert result["status"] == "partial"
    # And the old two-list spelling has no home either.
    stale = _submit(
        registry,
        _attach(registry, roster, session_id="pm-stale"),
        {
            "question_identity": stable_pm_question_identity(QUESTION),
            "lane_id": "code_context",
            "policy_found": True,
            "examined_repository_ids": [roster[0]["repo_id"]],
            "answer_prefix": "[from-code]",
            "requires_user_confirmation": True,
            "user_confirmation_prompt": "ok?",
            "evidence": [
                {
                    "repo_id": roster[1]["repo_id"],
                    "path": "src/checkout.ts",
                    "policy_claim": "revoked immediately",
                }
            ],
        },
        session_id="pm-stale",
    )
    assert stale["status"] == "partial"


def test_a_repository_read_and_clean_is_not_a_repository_unread(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Two scans with identical claims still say different things.

    This is the loss that ruled out deleting the scope outright: the lane brief
    tells the child to stop once it can answer, so a partial scan is the
    designed default, and "found nothing across two of five" must stay
    distinguishable from "across all five". Absence from ``examined`` means not
    read; an entry with no claims means read and clean.
    """
    wide = pm_repository_roster(
        [
            {"path": "/repo/api", "name": "api"},
            {"path": "/repo/web", "name": "web"},
            {"path": "/repo/mobile", "name": "mobile"},
            {"path": "/repo/admin", "name": "admin"},
            {"path": "/repo/docs", "name": "docs"},
        ]
    )
    carried = [
        {
            "path": "src/billing.py",
            "policy_claim": "grace period applies",
            "plain_statement": "a grace period applies after lapse",
        },
    ]

    def scan(session_id: str, read: list[dict[str, str]]) -> dict[str, Any]:
        meta = _attach(registry, wide, session_id=session_id)
        result = _submit(
            registry,
            meta,
            _found_policy(
                wide,
                examined=[
                    {
                        "repo_id": entry["repo_id"],
                        "policy_claims": carried if index == 0 else [],
                    }
                    for index, entry in enumerate(read)
                ],
            ),
            session_id=session_id,
        )
        assert result["status"] == "complete"
        return next(
            lane["output"]
            for lane in result["result"]["aggregated_outputs"]
            if lane["lane_id"] == "code_context"
        )

    stopped_early = scan("pm-partial", wide[:2])
    read_everything = scan("pm-full", wide)

    assert [entry["repo_id"] for entry in stopped_early["examined"]] == [
        wide[0]["repo_id"],
        wide[1]["repo_id"],
    ]
    assert len(read_everything["examined"]) == 5
    assert stopped_early != read_everything
    # The claims are identical; only the scope differs, which is the whole point.
    assert [claim for entry in stopped_early["examined"] for claim in entry["policy_claims"]] == [
        claim for entry in read_everything["examined"] for claim in entry["policy_claims"]
    ]


def test_a_cited_repository_need_not_be_the_only_one_examined(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Reading more than you cite is normal and stays accepted.

    The reviewer asked for cited repositories to be a subset of the declared
    scope. Folding makes that hold by construction — but the converse must not
    become an equality: a repository read and found clean is exactly the
    honest partial scope the lane brief asks for.
    """
    result = _submit(
        registry,
        _attach(registry, roster),
        _found_policy(
            roster,
            examined=[
                {
                    "repo_id": roster[0]["repo_id"],
                    "policy_claims": [
                        {
                            "path": "src/billing.py",
                            "policy_claim": "grace period applies",
                            "plain_statement": "a grace period applies after lapse",
                        }
                    ],
                },
                {"repo_id": roster[1]["repo_id"], "policy_claims": []},
            ],
        ),
    )
    assert result["status"] == "complete"


def test_cross_repo_disagreement_survives_as_structure(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """Two repositories implementing different policies is the PRD input.

    It is accepted rather than reconciled, and the two claims stay attached to
    their own repositories — which is the whole reason a claim lives inside its
    repository's entry rather than beside a separate scope list.
    """
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        _found_policy(
            roster,
            examined=[
                {
                    "repo_id": roster[0]["repo_id"],
                    "policy_claims": [
                        {
                            "path": "src/billing.py",
                            "policy_claim": "access continues until period end",
                            "plain_statement": "access continues to the end of the paid period",
                        }
                    ],
                },
                {
                    "repo_id": roster[1]["repo_id"],
                    "policy_claims": [
                        {
                            "path": "src/checkout.ts",
                            "policy_claim": "access is revoked immediately",
                            "plain_statement": "access ends immediately",
                        }
                    ],
                },
            ],
        ),
    )
    assert result["status"] == "complete"
    examined = next(
        lane["output"]["examined"]
        for lane in result["result"]["aggregated_outputs"]
        if lane["lane_id"] == "code_context"
    )
    assert [entry["repo_id"] for entry in examined] == [
        roster[0]["repo_id"],
        roster[1]["repo_id"],
    ]
    claims = [claim["policy_claim"] for entry in examined for claim in entry["policy_claims"]]
    assert len(set(claims)) == 2


def test_repo_id_survives_a_rename_and_separates_a_shared_name() -> None:
    assert pm_repo_id(name="api", path="/repo/api") != pm_repo_id(name="api", path="/other/api")
    renamed = pm_repo_id(name="billing-api", path="/repo/api")
    assert renamed.split("-")[-1] == pm_repo_id(name="api", path="/repo/api").split("-")[-1]


def test_the_lane_reads_the_snapshot_and_cites_the_durable_checkout() -> None:
    """Where to read and what to call it are different paths.

    A registered repo is redirected to a worktree pinned to the remote default
    branch so a stale local checkout cannot reach the PRD. Once the engine stops
    reading code, the lane inherits that: it must read the snapshot. The
    identifier stays derived from the durable checkout, so evidence submitted
    before a snapshot is recreated still matches the roster it was bounded by.
    """
    entries = pm_repository_roster(
        [{"path": "/tmp/snapshot-xyz", "source_path": "/repo/api", "name": "api"}]
    )
    assert entries[0]["path"] == "/tmp/snapshot-xyz"
    assert entries[0]["repo_id"] == pm_repo_id(name="api", path="/repo/api")


def test_a_recreated_snapshot_does_not_change_the_identifier() -> None:
    first = pm_repository_roster(
        [{"path": "/tmp/snap-aaa", "source_path": "/repo/api", "name": "api"}]
    )
    second = pm_repository_roster(
        [{"path": "/tmp/snap-bbb", "source_path": "/repo/api", "name": "api"}]
    )
    assert first[0]["repo_id"] == second[0]["repo_id"]
    assert first[0]["path"] != second[0]["path"]


# ── Binding: a PM answer belongs to one PM question ──────────────────────


def test_an_answer_for_a_different_question_is_rejected(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    result = _submit(
        registry,
        meta,
        {
            "question_identity": "pm-question:0000000000000000",
            "lane_id": "code_context",
            "examined": [],
            "nothing_examined_reason": "no_repository_in_roster",
        },
    )
    assert result["status"] == "partial"
    assert "does not belong to this fan-out" in str(result["contract_violations"]["code_context"])


def test_pm_and_interview_identities_are_distinguishable() -> None:
    from ouroboros.orchestrator.capabilities import stable_code_investigation_question_identity

    assert stable_pm_question_identity(QUESTION) != stable_code_investigation_question_identity(
        QUESTION
    )


# ── Wiring: PM lanes are judged by PM contracts ──────────────────────────


def test_pm_registers_its_own_fanout_kind(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    """A shared kind would judge ``code_context`` by the interview's map.

    The interview declares no contract for its code lane, so a PM answer under a
    shared kind would pass unchecked — every guard above would still be written
    down and none of them would run.
    """
    meta = _attach(registry, roster)
    record = registry.load(meta["question_advisory_fanout_id"])
    assert record is not None
    assert record.kind == FANOUT_KIND_QUESTION_ADVISORY
    assert record.synthesizer_input["tool_name"] == "ouroboros_pm_interview"


def test_the_roster_the_child_sees_is_the_roster_re_entry_enforces(
    registry: FanoutRegistry, roster: list[dict[str, str]]
) -> None:
    meta = _attach(registry, roster)
    record = registry.load(meta["question_advisory_fanout_id"])
    assert record is not None
    assert record.synthesizer_input["roster_repo_ids"] == [e["repo_id"] for e in roster]
    prompt = meta["question_advisory_subagents"][0]["prompt"]
    for entry in roster:
        assert entry["repo_id"] in prompt


def test_neither_lane_is_given_a_persona(roster: list[dict[str, str]]) -> None:
    """A persona prescribes a free-form output the closed contracts reject."""
    request = {
        "mcp_tool": "ouroboros_pm_interview",
        "session_id": "pm-1",
        "question_identity": stable_pm_question_identity(QUESTION),
        "question": QUESTION,
        "lanes": _pm_question_advisory_fanout_metadata()["lanes"],
        "synthesis_contract": {},
        "repository_roster": roster,
    }
    payloads = [payload.to_dict() for payload in build_question_advisory_subagents(request)]
    assert {payload["agent"] for payload in payloads} == {"general"}
    assert all(payload["context"]["persona"] is None for payload in payloads)


def test_a_question_with_no_roster_still_gets_its_lanes(registry: FanoutRegistry) -> None:
    """An empty roster bounds the code lane to nothing; it does not skip it.

    Skipping would make "this session registered no repository" indistinguishable
    from "this question needed no code evidence" at the point the PM answers.
    """
    meta = _attach(registry, [])
    assert [payload["context"]["lane_id"] for payload in meta["question_advisory_subagents"]] == [
        "code_context",
        "data_context",
    ]


# ── Decision 3: a confirmed finding takes a round; nothing else records ───
#
# The shape these tests pin is ``ouroboros_interview``'s, arrived at after six
# rounds of the other one. A confirmed ``[from-code]`` finding is sent as the
# answer — one parameter, the one every answer uses — and occupies the round it
# was fetched for. Its provenance, not a branch in the handler, is what keeps it
# out of requirements and out of the completion count, and the decision the
# person makes lands on the question generated next.


@pytest.mark.asyncio
async def test_a_confirmed_finding_takes_its_own_round_and_the_decision_takes_the_next(
    tmp_path: Path,
) -> None:
    """The property the whole shape rests on, end to end through the handler.

    Two calls, two rounds, and the second round is the person's. What is checked
    is that the finding did not consume the decision's place and the decision
    did not land on the finding's question.
    """
    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-shape", initial_context="ctx")
    engine = _stub_engine(state, ("Q2", "Q3"))

    first = await handler._handle_answer(
        engine,
        "pm-shape",
        "[from-code] api: counted by service date",
        str(tmp_path),
        last_question="Q1",
    )
    assert first.is_ok
    # A turn persists nothing when it asks (RFC #2222 r4), so the transcript
    # holds finished rounds only — Q2 is on the wire, not on disk.
    assert [r.question for r in state.rounds] == ["Q1"]
    assert state.rounds[0].user_response == "[from-code] api: counted by service date"
    assert state.rounds[0].provenance == "observation"

    second = await handler._handle_answer(
        engine, "pm-shape", "cancellations free the slot", str(tmp_path), last_question="Q2"
    )
    assert second.is_ok
    assert state.rounds[1].question == "Q2"
    assert state.rounds[1].user_response == "cancellations free the slot"
    assert state.rounds[1].provenance == "user"
    assert [r for r in state.rounds if r.user_response is None] == []


@pytest.mark.asyncio
async def test_an_answer_with_no_question_to_belong_to_is_refused(tmp_path: Path) -> None:
    """Case A, and the reason it is not an observation-only rule.

    Before this, an observation arriving with nothing pending took a side path
    that returned ``evidence_recorded=true`` while persisting nothing — success
    reported for a write that did not happen. The repair is not a filter on that
    side path but the removal of it: every answer needs a question, and when
    none is pending the caller is asked for one. Revert the guard and this test
    goes back to passing with an unchanged transcript.
    """
    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-caseA", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response="A1"))
    engine = _stub_engine(state, ("Q2",))

    for answer in ("[from-code] api: counted by service date", "a plain decision"):
        result = await handler._handle_answer(engine, "pm-caseA", answer, str(tmp_path))
        assert result.is_err, answer
        assert "last_question" in str(result.error)

    # Nothing was written under the question somebody already answered.
    assert [(r.question, r.user_response) for r in state.rounds] == [("Q1", "A1")]


@pytest.mark.asyncio
async def test_the_same_answer_is_accepted_when_it_names_its_question(tmp_path: Path) -> None:
    """The other direction: the guard asks for a question, it does not block.

    Without this, "refuse when nothing is pending" would be satisfied by
    refusing everything, and the reopen path — a question the host asked after
    the interview went quiet — would be closed with no test noticing.
    """
    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-caseA2", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response="A1"))
    engine = _stub_engine(state, ("Q2",))

    result = await handler._handle_answer(
        engine,
        "pm-caseA2",
        "a plain decision",
        str(tmp_path),
        last_question="Q1b, asked by the host",
    )

    assert result.is_ok
    assert [(r.question, r.user_response) for r in state.rounds if r.user_response] == [
        ("Q1", "A1"),
        ("Q1b, asked by the host", "a plain decision"),
    ]


@pytest.mark.asyncio
async def test_no_second_field_can_carry_a_finding_past_the_answer(tmp_path: Path) -> None:
    """Case B, closed by the combination not existing rather than by a rule.

    The ambiguous call was ``answer="[from-code] ..."`` plus a separate
    ``evidence`` argument: the handler read one and dropped the other. The
    schema no longer offers the second field, so the call cannot be built — and
    an argument the tool never declared is ignored rather than silently stored.
    """
    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-caseB", initial_context="ctx")
    engine = _stub_engine(state, ("Q2",))
    handler.pm_engine = engine  # type: ignore[assignment]

    result = await handler.handle(
        {
            "session_id": "pm-caseB",
            "answer": "[from-code] api: counted by service date",
            "last_question": "Q1",
            "evidence": "SECOND-PAYLOAD",
        }
    )

    assert result.is_ok
    assert not any("SECOND-PAYLOAD" in (r.user_response or "") for r in state.rounds)
    assert state.rounds[0].user_response == "[from-code] api: counted by service date"


def test_a_finding_reaches_question_generation_but_not_requirement_extraction() -> None:
    """The asymmetry the shape depends on, now carried by the round itself.

    Writing the next question needs to know what was already established.
    Distilling requirements must not, or what the system does today gets written
    down as what the PM decided — the promotion #1755 closed.
    """
    from ouroboros.mcp.tools.pm_handler import _format_pm_transcript

    state = InterviewState(interview_id="pm-wh", initial_context="ctx")
    state.rounds.append(
        InterviewRound(
            round_number=1,
            question="When does access end?",
            user_response="[from-code] billing-api: period end",
        )
    )
    state.rounds.append(
        InterviewRound(round_number=2, question="What should it be?", user_response="at renewal")
    )

    for_generation = _format_pm_transcript(state, withhold_observations=False)
    for_extraction = _format_pm_transcript(state, withhold_observations=True)

    assert "billing-api: period end" in for_generation
    assert "billing-api: period end" not in for_extraction
    assert "observation withheld" in for_extraction
    # The decision itself is in both: only the adopted fact is withheld.
    assert "at renewal" in for_extraction


def test_a_finding_does_not_count_toward_readiness() -> None:
    """Occupying a round is safe only because completion counts decisions.

    ``check_completion`` filters on ``provenance == "user"``, so a lane
    reporting on three questions cannot push a two-decision interview over the
    minimum. This is the property that made the round-taking shape available at
    all, so it is pinned against the engine rather than against the classifier.
    """
    from ouroboros.bigbang.pm_interview import MIN_ROUNDS_BEFORE_EARLY_EXIT

    state = InterviewState(interview_id="pm-ready", initial_context="ctx")
    for index in range(MIN_ROUNDS_BEFORE_EARLY_EXIT + 2):
        state.rounds.append(
            InterviewRound(
                round_number=index + 1,
                question=f"Q{index + 1}",
                user_response="[from-code] api: a fact",
            )
        )

    counted = sum(1 for r in state.rounds if r.user_response and r.provenance == "user")
    assert counted == 0
    assert all(r.provenance == "observation" for r in state.rounds)


@pytest.mark.asyncio
async def test_the_plugin_runtime_records_a_finding_the_same_way(tmp_path: Path) -> None:
    """R-5: no asymmetry left to keep in step.

    The plugin branch calls ``record_answer`` for every answer and always did.
    What changed is that this is now the whole of the behaviour on both
    runtimes: the branch that used to read a second field there is gone, so the
    two cannot drift by one of them being updated.
    """
    from ouroboros.mcp.tools.pm_handler import _format_pm_transcript

    state = InterviewState(interview_id="pm-plugin", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response=None))
    state.record_answer("Q1", "[from-code] api: counted by service date")

    assert state.rounds[0].provenance == "observation"
    assert len(state.rounds) == 1
    # It rides the transcript to the child that writes the next question...
    assert "counted by service date" in _format_pm_transcript(state)
    # ...and is withheld from the child that produces requirements.
    assert "counted by service date" not in _format_pm_transcript(state, withhold_observations=True)


@pytest.mark.asyncio
async def test_a_bare_reconnect_asks_a_fresh_question(tmp_path: Path) -> None:
    """``answer`` stays optional, and a reconnect plans rather than restores.

    The guard above is about a missing *question*; this is the call with a
    missing *answer*, and blocking it would close a normal path. What it
    returns changed with RFC #2222 revision 4: a turn persists nothing when it
    asks, so there is no in-flight question to re-show — the host that lost its
    turn gets a new one planned from the finished transcript.
    """
    handler = PMInterviewHandler(data_dir=tmp_path, agent_runtime_backend="claude")
    state = InterviewState(interview_id="pm-rc2", initial_context="ctx")
    state.rounds.append(InterviewRound(round_number=1, question="Q1", user_response="A1"))

    result = await handler._handle_answer(
        _stub_engine(state, ("Q2",)), "pm-rc2", None, str(tmp_path)
    )

    assert result.is_ok
    assert result.value.meta["question"] == "Q2"
    # Nothing half-written was left behind by asking.
    assert [r.user_response for r in state.rounds] == ["A1"]


def test_a_citation_path_that_is_not_repository_relative_is_rejected() -> None:
    """The description asked for a relative path; the contract now requires one.

    ``repo_id`` was closed against the roster while ``path`` was left to prose,
    so a citation could name the machine the lane ran on — a CI checkout — or
    escape the repository entirely, and still render to the PM under an
    in-roster identifier. The field exists so the person can go and check the
    evidence, and each of these is a path they cannot check.
    """
    from jsonschema import Draft202012Validator

    from ouroboros.orchestrator.capabilities.pm_schemas import _pm_policy_claim_schema

    schema = _pm_policy_claim_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def accepted(path: str) -> bool:
        item = {
            "path": path,
            "policy_claim": "access ends",
            "plain_statement": "access ends",
        }
        return not list(validator.iter_errors(item))

    # What a lane is supposed to cite, including dotfiles and a literal '..'
    # inside a segment, which is a filename rather than a traversal.
    assert accepted("src/billing/lapse.py")
    assert accepted(".github/workflows/ci.yml")
    assert accepted("src/x..y/z.py")

    # Unix absolute — names the machine the lane ran on, not the PM's clone.
    assert not accepted("/etc/passwd")
    assert not accepted("/Users/someone/.ssh/id_rsa")
    # Windows drive and UNC — same, for a different runner.
    assert not accepted("C:\\src\\x.cs")
    assert not accepted("c:/src/x.cs")
    assert not accepted("\\\\server\\share\\x")
    # Traversal — points outside the repository while filed as its file.
    assert not accepted("../../../../etc/shadow")
    assert not accepted("a/../b")
    assert not accepted("./x")
    # A newline would smuggle a second line into a rendered citation.
    assert not accepted("src/x.py\nrm -rf /")


def test_readiness_counts_decisions_not_records() -> None:
    """Readiness asks how often the person judged, not how much was recorded.

    Evidence no longer occupies a round, so it cannot be counted by accident.
    What remains countable is provenance, and only ``user`` provenance is a
    judgement.
    """
    from ouroboros.bigbang.answer_provenance import classify_answer_provenance

    assert classify_answer_provenance("[from-code] a fact") == "observation"
    assert classify_answer_provenance("[from-data] a number") == "observation"
    assert classify_answer_provenance("counted by service date") == "user"


def test_no_constant_names_a_synthetic_round_any_more() -> None:
    """The shape is gone, so the label that made it buildable is gone too.

    Kept "just for readers", a constant naming a synthetic round leaves that
    round one import away from returning — and the three stored sessions it cost
    are the argument against keeping it.
    """
    import ouroboros.orchestrator.capabilities.pm_schemas as pm_schemas

    assert [name for name in dir(pm_schemas) if "EVIDENCE_ROUND" in name] == []
