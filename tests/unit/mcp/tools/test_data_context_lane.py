"""The ``data_context`` advisory lane and the completion rules it needs (#1754).

The lane proposes measurements and runs nothing. These tests pin the properties
that make that true by construction rather than by good behaviour: what the
child can express, what completion does when a lane is absent, and what happens
to an output that breaks its contract.
"""

from __future__ import annotations

import json
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
        "no_evidence_reason": "not_a_measurement",
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


@pytest.mark.parametrize(
    ("field", "smuggled", "closed_by"),
    [
        (
            "no_evidence_reason",
            "I ran the query: observed 41 accounts; bob@example.com",
            "the reason is a choice from a closed set, not a sentence",
        ),
        (
            "caveats",
            ["observed 41 accounts"],
            "the field was removed; a closed schema rejects what it does not name",
        ),
    ],
)
def test_a_field_that_could_be_closed_was_closed(field: str, smuggled: Any, closed_by: str) -> None:
    """Every field that could stop being free text has stopped being one.

    A child that ran a lookup has no field designated for the result, but prose
    fields were still sentence-shaped and a result fits in a sentence. Rather
    than look for results in them — the search #1754 records as not converging
    over ten rounds — the fields that did not need to be prose were closed.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer[field] = smuggled

    assert list(Draft202012Validator(schema).iter_errors(answer)) != [], closed_by


@pytest.mark.parametrize(
    "field",
    ["tool_name", "group_by", "filters"],
)
def test_a_name_in_the_source_cannot_be_spelled_as_a_query(field: str) -> None:
    """Identifiers are shaped like identifiers, so a query is unspellable.

    `tool_name`, a grouping key and a filter's field name all name something in
    the data source. Constraining them to an identifier alphabet is cheaper than
    looking for SQL in a field that has no shape, and it does not depend on
    recognising which dialect the SQL is in.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    injected = "SELECT count(*) FROM accounts -- observed 41"
    request: dict[str, Any] = {
        "operation": "read",
        "tool_name": "warehouse_query",
        "metric": "enterprise accounts",
        "aggregation": "count",
        "informs_decision": "whether SSO ships first",
    }
    if field == "tool_name":
        request["tool_name"] = injected
    elif field == "group_by":
        request["group_by"] = [injected]
    else:
        request["filters"] = [{"field": injected, "comparator": "eq", "value": "x"}]

    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer.pop("no_evidence_reason")
    answer["read_requests"] = [request]

    assert list(Draft202012Validator(schema).iter_errors(answer)) != []


@pytest.mark.parametrize(
    ("request_patch", "unstated"),
    [
        ({"aggregation": "percentile"}, "which rank — p50 reads differently from p99"),
        (
            {"filters": [{"field": "day", "comparator": "between", "value": "2026-01..2026-03"}]},
            "two operands packed into one string the parent would have to parse",
        ),
        (
            {"filters": [{"field": "region", "comparator": "in", "value": "emea,apac"}]},
            "a set packed into one string",
        ),
    ],
)
def test_a_request_the_parent_would_have_to_finish_cannot_be_proposed(
    request_patch: dict[str, Any], unstated: str
) -> None:
    """What the user approves has to be the whole operation, not most of it.

    The host renders every field and runs the read after confirmation, so a
    request that still needs a decision afterwards moves that decision past the
    approval meant to cover it. Closed by making the incomplete forms
    unspellable rather than by adding a parameter only some aggregations use:
    the ranks are their own members, and a range is two scalar filters.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer.pop("no_evidence_reason")
    answer["read_requests"] = [
        {
            "operation": "read",
            "tool_name": "warehouse_query",
            "metric": "request latency",
            "aggregation": "average",
            "informs_decision": "whether the p95 target is met",
            **request_patch,
        }
    ]

    assert list(Draft202012Validator(schema).iter_errors(answer)) != [], unstated


def test_the_complete_forms_of_those_requests_are_accepted() -> None:
    """The replacements must express what the rejected forms were reaching for."""
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer.pop("no_evidence_reason")
    answer["read_requests"] = [
        {
            "operation": "read",
            "tool_name": "warehouse_query",
            "metric": "request latency",
            "aggregation": "p95",
            "informs_decision": "whether the p95 target is met",
            "filters": [
                {"field": "day", "comparator": "gte", "value": "2026-01-01"},
                {"field": "day", "comparator": "lte", "value": "2026-03-31"},
            ],
        }
    ]

    assert list(Draft202012Validator(schema).iter_errors(answer)) == []


def test_the_child_has_no_field_in_which_to_rate_a_tool() -> None:
    """A classification that disagrees with the tool cannot be submitted at all.

    The asked-for regression is "a child-declared classification that disagrees
    with the registered tool". There is no such submission to test, because the
    field it would travel in is gone: the child names a tool and the host, which
    holds the tool, classifies it. `code_context` has always worked this way —
    the server hands it `Read` / `Glob` / `Grep` with their `mutation_class`
    already filled in — and `web_context` names no tool. This lane was the only
    one asking the party that knows least to rate the risk (Q00/ouroboros#1754).
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    request_schema = schema["properties"]["read_requests"]["items"]
    assert "source_class" not in request_schema["properties"]
    assert "source_class" not in request_schema["required"]

    answer = _valid_no_op("interview-question:0123456789abcdef")
    answer["data_needed"] = True
    answer.pop("no_evidence_reason")
    answer["read_requests"] = [
        {
            "operation": "read",
            "tool_name": "database.delete_all",
            "metric": "accounts",
            "aggregation": "count",
            "informs_decision": "whether SSO ships first",
            "source_class": "local",
        }
    ]

    errors = [error.validator for error in Draft202012Validator(schema).iter_errors(answer)]
    assert "additionalProperties" in errors


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


@pytest.mark.parametrize(
    ("declaration", "why"),
    [
        ({"undispatched": "false"}, "a non-empty string is truthy and says the opposite"),
        ({"undispatched": "true"}, "the string, not the literal"),
        ({"undispatched": 1}, "a number is not the documented boolean"),
        ({"undispatched": {"value": False}}, "an object is truthy whatever it contains"),
        ({"undispatched": None}, "null is not a declaration"),
        ({"undispatched": False}, "the flag says it ran, so the entry owes an output"),
        ({"undispatched": False, "content": "advice"}, "one of the two shapes, or neither"),
        ({"undispatched": True, "content": "advice"}, "never ran, and here is what it said"),
        ({}, "neither what the child said nor that there was nothing to say"),
    ],
)
def test_a_lane_is_not_excused_by_a_value_that_only_looks_true(
    tmp_path: Any, declaration: dict[str, Any], why: str
) -> None:
    """The one key that excuses a required lane cannot be satisfied by accident.

    `undispatched` was read for truthiness, so `"false"` — a non-empty string —
    declared the lane never ran and completed the fan-out without it. That is
    the required evidence lane silently skipped by a value that says the
    opposite of what it did (Q00/ouroboros#1754).

    The empty entry is the same hole one door over, found while auditing this
    fix rather than in the finding: an entry carrying neither `content` nor the
    declaration satisfied a required lane with nothing at all — cheaper than
    inventing the missing output, which is the incentive the declaration exists
    to remove.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    results.append({"key": "data_context", **declaration})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "invalid_result_entry", why
    assert outcome["invalid_keys"] == ["data_context"], why


@pytest.mark.asyncio
async def test_the_public_submit_tool_also_refuses_a_malformed_declaration(tmp_path: Any) -> None:
    """Through the tool that accepts the unconstrained result objects."""
    from ouroboros.mcp.tools.evaluation_handlers import SubmitFanoutResultsHandler

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    required = [entry["key"] for entry in _required_results(lane_ids, identity)]
    submit = SubmitFanoutResultsHandler(fanout_registry=registry)

    malformed = await submit.handle(
        {
            "session_id": "sess-data",
            "correlation_key": "context.lane_id",
            "fanout_id": fanout_id,
            "results": [{"key": key, "undispatched": "false"} for key in required],
        }
    )

    assert malformed.is_ok, malformed
    meta = malformed.unwrap().meta
    assert meta["status"] == "invalid_result_entry"
    # Every offender at once: a host with three bad entries should not need
    # three submissions to learn three facts.
    assert sorted(meta["invalid_keys"]) == sorted(required)

    # An entry that is not an object, or that names no lane, has no key to be
    # reported under — so it is reported by position rather than dropped. It
    # used to be filtered out before reaching the core, which let a submission
    # that mis-serialised one lane come back `complete`.
    unserialisable = await submit.handle(
        {
            "session_id": "sess-data",
            "correlation_key": "context.lane_id",
            "fanout_id": fanout_id,
            "results": [42, {}, *({"key": key, "content": "advice"} for key in required)],
        }
    )

    assert unserialisable.is_ok, unserialisable
    assert unserialisable.unwrap().meta["status"] == "invalid_result_entry"
    assert unserialisable.unwrap().meta["invalid_keys"] == ["<results[0]>", "<results[1]>"]

    # The honest declaration still completes: refusing the malformed shape must
    # not cost the host the escape the shape exists to provide.
    honest = await submit.handle(
        {
            "session_id": "sess-data",
            "correlation_key": "context.lane_id",
            "fanout_id": fanout_id,
            "results": [{"key": key, "undispatched": True} for key in required],
        }
    )

    assert honest.is_ok, honest
    meta = honest.unwrap().meta
    assert meta["status"] == "complete"
    assert sorted(meta["undispatched_keys"]) == sorted(required)


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
    # Placed in `metric`, which is one of the three fields still free: they
    # exist to be read by the user before approving a read, so they cannot be
    # closed without removing the confirmation surface. What protects the user
    # is where this can travel, and the assertions below are that boundary.
    secret = "accounts, observed 41, contact bob@example.com"
    proposal = _fully_populated_proposal(identity)
    proposal["read_requests"][0]["metric"] = secret
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    results.append({"key": "data_context", "content": proposal})

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


def test_the_record_carries_no_contract_to_lose(tmp_path: Any) -> None:
    """Which lanes are contracted is code, so the record has no say in it.

    An earlier revision persisted the contracts with the record. That copy is
    what two rounds of findings were about — first a corrupt schema, then a
    missing entry — and both were the same shape: a fact the code guarantees had
    become a value that could be absent, with absence reading as "nothing to
    check". The field is gone rather than guarded (Q00/ouroboros#1754).
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, _lane_ids, _identity = _registered_advisory(registry)

    persisted = json.loads((tmp_path / f"{fanout_id}.json").read_text(encoding="utf-8"))

    assert "answer_contracts" not in persisted
    assert not hasattr(registry.load(fanout_id), "answer_contracts")


def test_no_record_state_can_switch_a_lane_contract_off(tmp_path: Any) -> None:
    """The probe that used to bypass validation now changes nothing.

    Emptying `answer_contracts` on disk once made re-entry skip `data_context`
    entirely — arbitrary fields accepted, `status="complete"`, and the question
    binding lost with it, because that check rode in the same loop. The key is
    read by nothing now, so the record can carry it, omit it, or lie about it.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    path = tmp_path / f"{fanout_id}.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["answer_contracts"] = {}
    path.write_text(json.dumps(persisted, ensure_ascii=False), encoding="utf-8")

    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    smuggled = _valid_no_op(identity)
    smuggled["arbitrary_child_value"] = 42
    smuggled["question_identity"] = "interview-question:ffffffffffffffff"
    results.append({"key": "data_context", "content": smuggled})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    violations = outcome["contract_violations"]["data_context"]
    assert any("additionalProperties" in violation for violation in violations)
    # The question binding rides in the same loop, so it had to come back too.
    assert "question_identity: does not belong to this fan-out" in violations


def test_a_lane_is_judged_by_the_contract_this_build_declares(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """After an upgrade the current contract judges, and that is the safe half.

    The reverse — replaying against a copy issued before the upgrade — is what
    the removed field bought. Priced out, it was worth less than it cost: a
    fan-out lives from one interview turn to its submission, so skew needs an
    upgrade inside that window, and the outcome here is `partial`. The lane is
    dropped for that turn; the question was shown to the user first and the
    interview continues. Compare that with what the copy made possible when it
    went missing, one test up.
    """
    from ouroboros.orchestrator.capabilities import interview_schemas as schemas

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    results = _required_results(lane_ids, identity)
    assert _submit(registry, fanout_id, results)["status"] == "complete"

    declared = schemas._interview_question_advisory_fanout_metadata

    def upgraded() -> dict[str, Any]:
        metadata = declared()
        for lane in metadata["lanes"]:
            if lane["lane_id"] == "data_context":
                lane["answer_contract"]["response_model_schema"]["required"].append("caveats")
        return metadata

    monkeypatch.setattr(schemas, "_interview_question_advisory_fanout_metadata", upgraded)

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial"
    assert "data_context" in outcome["contract_violations"]


@pytest.mark.parametrize(
    ("corruption", "why"),
    [
        ({"contract_id": "data_evidence_answer.v1"}, "no schema at all"),
        (
            {"contract_id": "data_evidence_answer.v1", "response_model_schema": "not-an-object"},
            "schema is not an object",
        ),
        (
            {
                "contract_id": "data_evidence_answer.v1",
                "response_model_schema": {"type": "not-a-type"},
            },
            "no validator will accept the schema",
        ),
        ("not-a-contract-at-all", "the contract is not even an object"),
    ],
)
def test_a_contract_that_cannot_be_checked_is_not_a_contract_that_passed(
    tmp_path: Any, monkeypatch: Any, corruption: Any, why: str
) -> None:
    """Being unable to check and having checked out must not share an answer.

    An unenforceable contract used to return no violations, and the caller reads
    no violations as *validated* — so a lane whose contract had rotted was
    accepted with whatever fields it carried, `status="complete"`.

    The corruption is injected into the build's lane metadata rather than into a
    record, because that is the only place it can come from now: a lane that
    declares a contract this build cannot use is a defect in the build, and the
    lane must fail closed rather than quietly become uncontracted.
    """
    from ouroboros.orchestrator.capabilities import interview_schemas as schemas

    declared = schemas._interview_question_advisory_fanout_metadata

    def broken() -> dict[str, Any]:
        metadata = declared()
        for lane in metadata["lanes"]:
            if lane["lane_id"] == "data_context":
                lane["answer_contract"] = corruption
        return metadata

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    monkeypatch.setattr(schemas, "_interview_question_advisory_fanout_metadata", broken)

    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    smuggled = _valid_no_op(identity)
    smuggled["arbitrary_child_value"] = 42
    results.append({"key": "data_context", "content": smuggled})

    outcome = _submit(registry, fanout_id, results)

    assert outcome["status"] == "partial", why
    assert "data_context" in outcome["contract_violations"], why
    # The lane is excluded from aggregation, so nothing it smuggled reaches the
    # host — and the report names the condition without quoting the output.
    assert "42" not in repr(outcome["contract_violations"]), why


@pytest.mark.asyncio
async def test_the_public_submit_tool_also_refuses_an_uncheckable_contract(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Through the tool a host actually calls, not only the core function."""
    from ouroboros.mcp.tools.evaluation_handlers import SubmitFanoutResultsHandler
    from ouroboros.orchestrator.capabilities import interview_schemas as schemas

    declared = schemas._interview_question_advisory_fanout_metadata

    def broken() -> dict[str, Any]:
        metadata = declared()
        for lane in metadata["lanes"]:
            if lane["lane_id"] == "data_context":
                lane["answer_contract"] = {
                    "contract_id": "data_evidence_answer.v1",
                    "response_model_schema": {"type": "not-a-type"},
                }
        return metadata

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    monkeypatch.setattr(schemas, "_interview_question_advisory_fanout_metadata", broken)

    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    smuggled = _valid_no_op(identity)
    smuggled["arbitrary_child_value"] = 42
    results.append({"key": "data_context", "content": smuggled})

    submitted = await SubmitFanoutResultsHandler(fanout_registry=registry).handle(
        {
            "session_id": "sess-data",
            "correlation_key": "context.lane_id",
            "fanout_id": fanout_id,
            "results": results,
        }
    )

    assert submitted.is_ok, submitted
    meta = submitted.unwrap().meta
    assert meta["status"] == "partial"
    assert "data_context" in meta["contract_violations"]


def test_every_contract_this_build_advertises_is_enforceable() -> None:
    """Advertised iff enforced, checked at the source rather than at replay.

    Failing closed above is only correct because a contract this build issues is
    never unenforceable; if one were, every fan-out carrying it would fail on a
    server defect rather than a corrupt record. This is where that is caught.
    """
    from jsonschema import Draft202012Validator

    contracted = [
        lane
        for lane in _interview_question_advisory_fanout_metadata()["lanes"]
        if isinstance(lane.get("answer_contract"), dict)
    ]
    assert contracted, "the parametrisation below proves nothing if no lane is contracted"
    for lane in contracted:
        schema = lane["answer_contract"]["response_model_schema"]
        Draft202012Validator.check_schema(schema)


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
