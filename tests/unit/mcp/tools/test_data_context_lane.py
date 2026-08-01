"""The ``data_context`` advisory lane and the completion rules it needs (#1754).

The lane proposes measurements and runs nothing. These tests pin the properties
that make that true by construction rather than by good behaviour: what the
child can express, what completion does when a lane is absent, and what happens
to an output that breaks its contract.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
import pytest

from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.bigbang.answer_provenance import classify_answer_provenance
from ouroboros.mcp.tools.authoring_handlers import (
    _attach_question_assist_requests,
    _build_question_advisory_request,
)
from ouroboros.mcp.tools.fanout import (
    FANOUT_KIND_QUESTION_ADVISORY,
    FanoutRegistry,
    submit_fanout_results,
)
from ouroboros.mcp.tools.subagent import build_interview_question_advisory_subagents
from ouroboros.orchestrator.capabilities.interview_schemas import (
    _interview_question_advisory_fanout_metadata,
    _interview_question_advisory_request_schema,
    interview_data_evidence_answer_contract,
)

QUESTION = "How many enterprise accounts asked for SSO last quarter?"


def _advisory_payloads() -> list[dict[str, Any]]:
    request = _build_question_advisory_request(
        session_id="sess-data",
        question=QUESTION,
        phase="answer",
        score=None,
    )
    return [payload.to_dict() for payload in build_interview_question_advisory_subagents(request)]


def _data_payload() -> dict[str, Any]:
    return next(p for p in _advisory_payloads() if p["context"]["lane_id"] == "data_context")


def _valid_no_op(identity: str) -> dict[str, Any]:
    return {
        "question_identity": identity,
        "lane_id": "data_context",
        "data_needed": False,
        "read_requests": [],
        "no_evidence_reason": "the question asks for a decision, not a measurement",
    }


# --------------------------------------------------------------------------- #
# Lane definition
# --------------------------------------------------------------------------- #


def test_lane_is_dispatched_required_with_its_contract() -> None:
    payload = _data_payload()

    assert payload["context"]["capability"] == "read_data"
    assert payload["context"]["required"] is True
    # Not the investigating persona: the other research lanes exist to go and
    # find things out, and this one exists to name what it would measure and
    # then stop. Asking for `researcher` would ask for the opposite.
    assert payload["agent"] != "researcher"
    # The contract must arrive whole: a child validated field-for-field at
    # re-entry cannot satisfy a schema it was shown half of.
    assert "data_evidence_answer.v1" in payload["prompt"]
    assert "[truncated]" not in payload["prompt"]


def test_emitted_request_validates_against_its_own_schema() -> None:
    """A lane whose capability is not in the enum fails the schema it ships under."""
    request = _build_question_advisory_request(
        session_id="sess-data",
        question=QUESTION,
        phase="answer",
        score=None,
    )
    errors = list(
        Draft202012Validator(_interview_question_advisory_request_schema()).iter_errors(request)
    )

    assert errors == []


def test_data_lane_is_the_only_required_optional_split_completion_reads() -> None:
    lanes = {
        str(lane["lane_id"]): bool(lane["required"])
        for lane in _interview_question_advisory_fanout_metadata()["lanes"]
    }

    assert lanes["data_context"] is True
    assert lanes["code_context"] is False
    assert lanes["web_context"] is False


# --------------------------------------------------------------------------- #
# What the child can express
# --------------------------------------------------------------------------- #


def test_a_fetched_value_has_nowhere_to_go() -> None:
    """The child cannot report a measurement it ran, because no field holds one.

    This is the structural half of "the child executes nothing": a child that
    ignored the instruction and called a tool still has no way to hand the
    result back, so the property does not depend on detecting misbehaviour.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer["read_requests"] = [
        {
            "operation": "read",
            "tool_name": "warehouse_query",
            "metric": "accounts requesting SSO",
            "aggregation": "count",
            "source_class": "metered",
            "informs_decision": "whether SSO belongs in the paid tier",
        }
    ]
    answer.pop("no_evidence_reason")
    assert list(Draft202012Validator(schema).iter_errors(answer)) == []

    answer["observed_value"] = 42
    errors = [error.validator for error in Draft202012Validator(schema).iter_errors(answer)]
    assert "additionalProperties" in errors


def test_a_write_cannot_be_proposed() -> None:
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer.pop("no_evidence_reason")
    answer["read_requests"] = [
        {
            "operation": "delete",
            "tool_name": "warehouse_query",
            "metric": "accounts",
            "aggregation": "count",
            "source_class": "local",
            "informs_decision": "n/a",
        }
    ]

    assert list(Draft202012Validator(schema).iter_errors(answer)) != []


def test_no_op_and_evidence_cannot_both_be_claimed() -> None:
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["read_requests"] = [
        {
            "operation": "read",
            "tool_name": "warehouse_query",
            "metric": "accounts",
            "aggregation": "count",
            "source_class": "local",
            "informs_decision": "pricing tier",
        }
    ]

    assert list(Draft202012Validator(schema).iter_errors(answer)) != []


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


def _registered_advisory(registry: FanoutRegistry) -> tuple[str, list[str], str]:
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-data",
        question=QUESTION,
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
        fanout_registry=registry,
    )
    payloads = meta["question_advisory_subagents"]
    lane_ids = [p["context"]["lane_id"] for p in payloads]
    identity = next(
        str(p["context"]["question_identity"])
        for p in payloads
        if p["context"]["lane_id"] == "data_context"
    )
    return meta["question_advisory_fanout_id"], lane_ids, identity


def _submit(
    registry: FanoutRegistry, fanout_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    return submit_fanout_results(
        registry,
        session_id="sess-data",
        correlation_key="context.lane_id",
        results=results,
        fanout_id=fanout_id,
    )


def _required_results(lane_ids: list[str], identity: str) -> list[dict[str, Any]]:
    required = {
        str(lane["lane_id"])
        for lane in _interview_question_advisory_fanout_metadata()["lanes"]
        if lane.get("required")
    }
    return [
        {
            "key": lane_id,
            "content": _valid_no_op(identity) if lane_id == "data_context" else f"{lane_id}-advice",
        }
        for lane_id in lane_ids
        if lane_id in required
    ]


def test_optional_lane_may_be_absent_and_its_absence_is_reported(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)

    outcome = _submit(registry, fanout_id, _required_results(lane_ids, identity))

    assert outcome["status"] == "complete"
    assert outcome["kind"] == FANOUT_KIND_QUESTION_ADVISORY
    # Reported, not silently dropped: a lane that was asked for and did not
    # answer is something the host should be able to see.
    assert "code_context" in outcome["missing_optional_keys"]
    assert "web_context" in outcome["missing_optional_keys"]


def test_required_lane_that_never_ran_can_be_declared(tmp_path: Any) -> None:
    """A consultation that did not happen completes, and says so.

    Without this, a required lane the host could not spawn pins the fan-out at
    ``partial`` for good, and the cheapest way out is to invent its output —
    which in this lane means fabricated evidence in front of the user.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    results.append({"key": "data_context", "undispatched": True})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "complete"
    assert outcome["undispatched_keys"] == ["data_context"]
    # Distinct from a no-op finding: nothing was consulted, so nothing is
    # aggregated for that lane.
    aggregated = {item["lane_id"] for item in outcome["result"]["aggregated_outputs"]}
    assert "data_context" not in aggregated


def test_absent_required_lane_still_returns_partial(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    assert outcome["missing_required_keys"] == ["data_context"]


def test_a_record_written_before_requiredness_keeps_the_all_keys_gate(tmp_path: Any) -> None:
    """A fan-out already in flight does not change its completion rule mid-air."""
    registry = FanoutRegistry(tmp_path)
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="sess-data",
        correlation_key="context.lane_id",
        expected_keys=["code_context", "answer_simplifier"],
        synthesizer_input={"lane_ids": ["code_context", "answer_simplifier"]},
    )
    record = registry.load(fanout_id)
    assert record is not None
    assert record.required_keys is None

    outcome = _submit(registry, fanout_id, [{"key": "answer_simplifier", "content": "draft"}])

    assert outcome["status"] == "partial"
    assert outcome["missing_required_keys"] == ["code_context"]


# --------------------------------------------------------------------------- #
# Contract enforcement at re-entry
# --------------------------------------------------------------------------- #


def test_violating_output_is_excluded_and_reported_without_echoing_it(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    leaked = "alice@example.com"
    results.append(
        {
            "key": "data_context",
            "content": {
                "question_identity": identity,
                "lane_id": "data_context",
                "data_needed": True,
                "read_requests": [],
                "observed_rows": [leaked],
            },
        }
    )

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    assert "data_context" in outcome["contract_violations"]
    # The violation report names the path and the rule, never the value: an
    # error channel that quotes its input is a second copy of it.
    assert leaked not in repr(outcome)


def test_a_lane_without_a_contract_completes_on_the_generic_shape(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = _required_results(lane_ids, identity)
    results.append({"key": "code_context", "content": "a plain string of advice"})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "complete"
    assert outcome["contract_violations"] == {}


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "answer",
    [
        "[from-data] 41 enterprise accounts asked for SSO",
        "[from-data][auto-confirmed] 41 accounts",
    ],
)
def test_from_data_answers_classify_as_observations(answer: str) -> None:
    """Defense in depth: there is no [from-data] answer path, but if one arrives
    out of contract it must be withheld like every other adopted fact."""
    assert classify_answer_provenance(answer) == "observation"


def test_a_user_decision_about_the_same_numbers_is_untouched() -> None:
    assert classify_answer_provenance("Support SSO in the paid tier from Q3") == "user"


# --------------------------------------------------------------------------- #
# Durable state
# --------------------------------------------------------------------------- #


def test_the_record_never_holds_what_a_child_said(tmp_path: Any) -> None:
    """Results are submitted, judged, and returned — never accumulated on disk.

    The fan-out record is request-side only: which lanes were asked, which of
    them gate completion, how to correlate them. That is what keeps it off the
    two egress paths a Seed travels, and it is why the record needs no
    sanitizer — there is nothing child-authored in it to sanitize.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    secret = "41 enterprise accounts, contact bob@example.com"
    results = _required_results(lane_ids, identity)
    for entry in results:
        if entry["key"] == "data_context":
            entry["content"]["no_evidence_reason"] = secret

    outcome = _submit(registry, fanout_id, results)
    assert outcome["status"] == "complete"

    on_disk = (tmp_path / f"{fanout_id}.json").read_text(encoding="utf-8")
    assert secret not in on_disk
    assert "advice" not in on_disk
    record = registry.load(fanout_id)
    assert record is not None
    assert set(record.synthesizer_input) == {"lane_ids"}


# --------------------------------------------------------------------------- #
# Provenance of a proposal
# --------------------------------------------------------------------------- #


def test_a_proposal_for_another_question_is_refused(tmp_path: Any) -> None:
    """Shape is not provenance.

    `interview-question:ffffffffffffffff` satisfies the schema pattern and
    belongs to nothing. Advisory children run asynchronously and a host may hold
    several questions open, so an unbound answer is one whose numbers can be
    rendered beside a question they did not measure.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    foreign = _valid_no_op("interview-question:ffffffffffffffff")
    results.append({"key": "data_context", "content": foreign})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    violations = outcome["contract_violations"]["data_context"]
    assert "question_identity: does not belong to this fan-out" in violations
    # Named, never quoted — the error channel must not become a second copy of
    # the submission.
    assert "ffffffffffffffff" not in repr(outcome)


def test_a_session_the_child_asserts_is_not_a_field_at_all(tmp_path: Any) -> None:
    """The contract holds no session, so a child cannot half-assert one.

    A session field the child fills is checked only when the child chose to fill
    it, which reads as a binding and holds as an option. The session is bound by
    the submission envelope instead — `_submit` above carries it, and a
    submission under another session is refused before any content is read. So
    the field is absent rather than lenient, and the closed schema is what makes
    absent enforceable (Q00/ouroboros#1754).
    """
    contract = interview_data_evidence_answer_contract()
    schema = contract["response_model_schema"]
    assert "session_id" not in schema["properties"]
    assert "session_id" not in schema["required"]

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    asserted = _valid_no_op(identity)
    asserted["session_id"] = "sess-data"
    results.append({"key": "data_context", "content": asserted})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    assert "data_context" in outcome["contract_violations"]


def test_a_second_issuance_of_the_same_question_is_not_a_mismatch(tmp_path: Any) -> None:
    """Two fan-outs for the same question text share one identity, by design.

    A question re-emitted on resume, or asked twice in one session, produces the
    same `question_identity` because that identity is a digest of the question
    text — and a proposal drafted for the first issuance names the same
    measurement as one drafted for the second, because it was drafted from the
    same question. Nothing false can be rendered: the child returns a proposal
    and never a measurement, and the read runs after confirmation, at the time
    it is confirmed. Binding per issuance would demand a token echoed by the
    child, buying detection of a case that carries no harm (Q00/ouroboros#1754).
    """
    registry = FanoutRegistry(tmp_path)
    first_id, lane_ids, identity = _registered_advisory(registry)
    second_id, _second_lanes, second_identity = _registered_advisory(registry)

    assert first_id != second_id
    assert second_identity == identity

    drafted_for_the_first = _required_results(lane_ids, identity)
    outcome = _submit(registry, second_id, drafted_for_the_first)

    assert outcome["status"] == "complete"
    assert outcome["contract_violations"] == {}


def test_a_proposal_for_this_question_is_accepted(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)

    outcome = _submit(registry, fanout_id, _required_results(lane_ids, identity))

    assert outcome["status"] == "complete"
    assert outcome["contract_violations"] == {}


def test_the_record_binds_the_question_it_was_registered_for(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, _lane_ids, identity = _registered_advisory(registry)

    record = registry.load(fanout_id)

    assert record is not None
    assert record.question_identity == identity
    assert record.session_id == "sess-data"


# --------------------------------------------------------------------------- #
# The prompt asks for the contract it is judged by
# --------------------------------------------------------------------------- #


def _output_section(prompt: str) -> str:
    return prompt.split("## Output")[-1]


def test_the_prompt_asks_for_the_contract_it_will_be_judged_by() -> None:
    """The last thing the child is told must not contradict its contract.

    The generic advisory Output section asks for `finding` / `evidence` /
    `suggested_options` — fields this closed contract forbids — and omits the
    ones it requires. Emitted after the contract, it told the child two
    incompatible things and let the wrong one win: a required lane obeying it is
    rejected at re-entry and pins the fan-out at `partial` forever.
    """
    section = _output_section(_data_payload()["prompt"])

    assert "data_evidence_answer.v1" in section
    for generic_field in ("- finding:", "- evidence:", "- suggested_options:"):
        assert generic_field not in section


def test_a_lane_without_a_contract_keeps_the_generic_output_shape() -> None:
    """The branch is on the contract's presence, not on a lane-id list."""
    code_lane = next(p for p in _advisory_payloads() if p["context"]["lane_id"] == "code_context")
    section = _output_section(code_lane["prompt"])

    assert "- suggested_options:" in section
    assert "data_evidence_answer.v1" not in section


# --------------------------------------------------------------------------- #
# What the user is asked to approve
# --------------------------------------------------------------------------- #


def _fully_populated_proposal(identity: str) -> dict[str, Any]:
    """A read request carrying every field the schema allows."""
    return {
        "question_identity": identity,
        "lane_id": "data_context",
        "data_needed": True,
        "read_requests": [
            {
                "operation": "read",
                "tool_name": "warehouse_query",
                "metric": "enterprise accounts",
                "aggregation": "count",
                "group_by": ["plan_tier"],
                "filters": [{"field": "region", "comparator": "eq", "value": "emea"}],
                "time_window": "last 90 days",
                "source_class": "metered",
                "informs_decision": "whether SSO ships in the first milestone",
            }
        ],
    }


def test_the_host_receives_every_read_request_field_verbatim(tmp_path: Any) -> None:
    """ "Render the request whole" has to be satisfiable, not just instructed.

    The host is told to show the user every field of the request it is asking
    them to authorize. That duty is only keepable if the request reaches the
    host as issued — a field dropped in aggregation is one the user can never be
    shown, and two proposals differing only in a filter would then arrive
    identical. So the passthrough is pinned here rather than the prose being
    guarded: what makes an omission impossible is that there is no summarizing
    step between the child and the confirmation surface (Q00/ouroboros#1754).
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    proposal = _fully_populated_proposal(identity)
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    request_schema = schema["properties"]["read_requests"]["items"]
    # The fixture must exercise the whole schema, or the passthrough below is
    # only proven for the fields someone remembered to put in it.
    assert set(proposal["read_requests"][0]) == set(request_schema["properties"])
    assert list(Draft202012Validator(schema).iter_errors(proposal)) == []

    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    results.append({"key": "data_context", "content": proposal})
    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "complete"
    aggregated = outcome["result"]["aggregated_outputs"]
    delivered = next(item for item in aggregated if item["lane_id"] == "data_context")
    assert delivered["output"] == proposal


def test_the_confirmation_instruction_asks_for_the_whole_request() -> None:
    """Both copies of the host contract, because only one of them ships to each.

    `skills/` is the canonical source and the wheel's payload;
    `.claude-plugin/skills/` is what a marketplace install reads. An instruction
    present in one and absent from the other is absent for half the hosts.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    for skill in (
        root / "skills" / "interview" / "SKILL.md",
        root / ".claude-plugin" / "skills" / "interview" / "SKILL.md",
    ):
        content = skill.read_text(encoding="utf-8")
        assert "every field the object carries" in content, skill
        # The partial list this replaced: naming a subset is what let two
        # requests differing by a filter render identically.
        assert "(metric, aggregation, grouping, time window, source class)" not in content, skill


# --------------------------------------------------------------------------- #
# Durable replay
# --------------------------------------------------------------------------- #


def test_re_entry_judges_by_the_contract_that_was_issued(tmp_path: Any, monkeypatch: Any) -> None:
    """A record outlives the process that wrote it.

    A host can submit after a restart or an upgrade. Judging that answer against
    whatever the current build advertises rejects work the child did exactly as
    asked — advertised-iff-enforced, applied across time.
    """
    from ouroboros.orchestrator.capabilities import interview_schemas as schemas

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = _required_results(lane_ids, identity)
    assert _submit(registry, fanout_id, results)["status"] == "complete"

    def upgraded() -> dict[str, Any]:
        metadata = schemas._interview_question_advisory_fanout_metadata()
        for lane in metadata["lanes"]:
            if lane["lane_id"] == "data_context":
                lane["answer_contract"]["response_model_schema"]["required"].append("caveats")
        return metadata

    monkeypatch.setattr(schemas, "_interview_question_advisory_fanout_metadata", upgraded)

    assert _submit(registry, fanout_id, results)["status"] == "complete"


def test_the_record_pins_the_contract_it_issued(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id, _lane_ids, _identity = _registered_advisory(registry)

    record = registry.load(fanout_id)

    assert record is not None
    assert record.answer_contracts is not None
    assert record.answer_contracts["data_context"]["contract_id"] == "data_evidence_answer.v1"


def test_a_record_issued_before_contracts_were_pinned_still_enforces(tmp_path: Any) -> None:
    """The legacy fallback is the canonical contract, not silence.

    Its issuing build's contracts are unrecoverable, so the current ones are the
    closest available approximation. Dropping enforcement instead would let an
    old record accept anything.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="sess-data",
        correlation_key="context.lane_id",
        expected_keys=["data_context"],
        required_keys=["data_context"],
        synthesizer_input={"lane_ids": ["data_context"]},
    )
    record = registry.load(fanout_id)
    assert record is not None
    assert record.answer_contracts is None

    outcome = _submit(registry, fanout_id, [{"key": "data_context", "content": {"shape": "wrong"}}])

    assert outcome["status"] == "partial"
    assert "data_context" in outcome["contract_violations"]


def test_the_contract_carries_only_what_is_enforced_or_instructed() -> None:
    """No field for a guarantee nothing makes true.

    Three findings on this PR were the same shape: a guarantee stated in a
    description, a policy block, or a design document, with nothing on the code
    path making it so. A contract addressed to the child holds what it must
    satisfy and what it must do; a claim about the system reads as authoritative
    as either, is enforced by nothing, and is addressed to a reader who cannot
    act on it. Removing the field is what stops the fourth instance.
    """
    contract = interview_data_evidence_answer_contract()

    assert set(contract) == {
        "contract_id",
        "scope",
        "response_model_schema",
        "runtime_instruction",
    }


def test_the_undecidable_rules_are_instruction_not_policy() -> None:
    """Categorical grouping and "a row list is not evidence" cannot be checked.

    Both quantify over an open value space, which is the class that cost #1703
    ten rounds. Stated as policy they would be a guarantee; stated to the child
    they are what they actually are.
    """
    instruction = interview_data_evidence_answer_contract()["runtime_instruction"]

    assert "never by an identifier" in instruction
    assert "row list" in instruction
