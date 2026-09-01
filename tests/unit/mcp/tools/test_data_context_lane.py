"""The ``data_context`` advisory lane and the completion rules it needs (#1754).

The lane takes the measurements it names (#1825). These tests pin what remains
true by construction rather than by good behaviour: what the child can express,
what it cannot claim, what completion does when a lane is absent, and what
happens to an output that breaks its contract.

The barrier moved when the lane was allowed to execute. It was "no field holds a
value"; it is now "no value here becomes an answer" -- so the tests that used to
prove a measurement unspellable now prove that everything *around* the
measurement still holds: aggregates only, no claim about the host, and a
required lane that can always answer.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.bigbang.answer_provenance import classify_answer_provenance
from ouroboros.mcp.tools.advisory_dispatch import append_question_advisory_dispatch
from ouroboros.mcp.tools.authoring_handlers import (
    _attach_question_assist_requests,
    _build_question_advisory_request,
)
from ouroboros.mcp.tools.fanout import (
    FANOUT_KIND_QUESTION_ADVISORY,
    FanoutRegistry,
    _validate_against_contract,
    submit_fanout_results,
)
import ouroboros.mcp.tools.question_advisory as question_advisory_module
from ouroboros.mcp.tools.subagent import build_interview_question_advisory_subagents
import ouroboros.orchestrator.capabilities as capabilities
from ouroboros.orchestrator.capabilities import interview_schemas as schemas
from ouroboros.orchestrator.capabilities.interview_schemas import (
    interview_data_evidence_answer_contract,
)

QUESTION = "How many enterprise accounts asked for SSO last quarter?"

_PRODUCTION_ADVISORY_METADATA = schemas._interview_question_advisory_fanout_metadata
_PRODUCTION_TOOL_METADATA = capabilities.ouroboros_tool_capability_metadata


def _standalone_tool_metadata(tool_name: str) -> dict[str, Any]:
    metadata = deepcopy(_PRODUCTION_TOOL_METADATA(tool_name))
    if tool_name == "ouroboros_interview":
        metadata["orchestration"]["question_advisory_fanout"] = (
            schemas._interview_question_advisory_fanout_metadata()
        )
    return metadata


_PRODUCTION_REQUEST_SCHEMA = schemas._interview_question_advisory_request_schema


def _interview_question_advisory_request_schema() -> dict[str, Any]:
    """Test-only request schema for the standalone data-lane contract."""
    schema = deepcopy(_PRODUCTION_REQUEST_SCHEMA())
    properties = schema["properties"]
    properties["allowed_capabilities"]["items"]["enum"].append("read_data")
    lane_properties = properties["lanes"]["items"]["properties"]
    lane_properties["lane_id"]["enum"].append("data_context")
    lane_properties["capability"]["enum"].append("read_data")
    return schema


def _interview_question_advisory_fanout_metadata() -> dict[str, Any]:
    """Test-only catalog preserving data contracts outside ordinary interviews."""
    metadata = deepcopy(_PRODUCTION_ADVISORY_METADATA())
    metadata["allowed_capabilities"] = ["inspect_code", "web_research", "read_data"]
    metadata["request_model_schema"] = _interview_question_advisory_request_schema()
    metadata["lanes"] = [
        {
            "lane_id": "code_context",
            "purpose": "Find repo-local facts.",
            "capability": "inspect_code",
            "required": False,
        },
        {
            "lane_id": "web_context",
            "purpose": "Find current web facts.",
            "capability": "web_research",
            "required": False,
        },
        {
            "lane_id": "data_context",
            "purpose": "Take the measurement that informs the question.",
            "capability": "read_data",
            "required": True,
            "answer_contract": interview_data_evidence_answer_contract(),
        },
    ]
    return metadata


@pytest.fixture(autouse=True)
def _standalone_data_lane_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep data-lane tests strong without restoring blanket interview fan-out."""
    for module in (schemas, capabilities):
        monkeypatch.setattr(
            module,
            "_interview_question_advisory_fanout_metadata",
            _interview_question_advisory_fanout_metadata,
        )
        monkeypatch.setattr(
            module,
            "_interview_question_advisory_request_schema",
            _interview_question_advisory_request_schema,
        )
    monkeypatch.setattr(
        capabilities,
        "ouroboros_tool_capability_metadata",
        _standalone_tool_metadata,
    )
    monkeypatch.setattr(
        question_advisory_module,
        "ouroboros_tool_capability_metadata",
        _standalone_tool_metadata,
    )


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


def _answer_states() -> dict[str, dict[str, Any]]:
    """Return the contract's two answer shapes, keyed by title.

    The schema is a `oneOf` over two closed objects, so there is no top-level
    `properties` to index: a field belongs to a state, which is the point.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    return {branch["title"]: branch for branch in schema["oneOf"]}


def _measured_state() -> dict[str, Any]:
    return _answer_states()["MeasurementTaken"]


def _read_request_shapes() -> dict[str, dict[str, Any]]:
    """Return the two closed measurement shapes by title.

    The read is a `oneOf` since #1825's grouping fix: a grouped value carries its
    label or the read is not grouped, which one object with an optional `group`
    could not say.
    """
    items = _measured_state()["properties"]["read_requests"]["items"]
    return {shape["title"]: shape for shape in items["oneOf"]}


def _read_request_schema() -> dict[str, Any]:
    """Return the grouped shape — it carries every field both shapes share."""
    return _read_request_shapes()["GroupedMeasurement"]


# --------------------------------------------------------------------------- #
# Lane definition
# --------------------------------------------------------------------------- #


def test_lane_is_dispatched_required_with_its_contract() -> None:
    payload = _data_payload()

    assert payload["context"]["capability"] == "read_data"
    assert payload["context"]["required"] is True
    # Not the `researcher` persona, though the lane investigates now (#1825).
    # `agents/researcher.md` prescribes its own free-form OUTPUT section, which
    # this lane's closed contract rejects -- handing it that persona would
    # reintroduce, from the persona side, the contradiction
    # `_advisory_output_section` exists to prevent.
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

    The result has its own field now (`values`); what is closed here is the
    prose, which was still sentence-shaped and a result fits in a sentence. Rather
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
        "values": [{"value": 41}],
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
    """What the user is shown has to be the whole operation, not most of it.

    The user reads every field beside a measurement the lane has already taken,
    so a request that still needs a decision is one the lane finished by
    guessing. Closed by making the incomplete forms
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
            "values": [{"value": 41}],
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
            "values": [{"value": 41}],
            "filters": [
                {"field": "day", "comparator": "gte", "value": "2026-01-01"},
                {"field": "day", "comparator": "lte", "value": "2026-03-31"},
            ],
        }
    ]

    assert list(Draft202012Validator(schema).iter_errors(answer)) == []


def test_the_two_answer_states_are_exclusive() -> None:
    """An answer is a measurement or a no-op, and cannot be both.

    `data_needed: true` with a concrete read *and* `no_evidence_reason` says the
    question is measurable and is not a measurement. Nothing downstream can
    resolve that — the host renders a measurement the same answer disowned
    (Q00/ouroboros#1754).
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    validator = Draft202012Validator(schema)
    proposal = {
        "question_identity": "interview-question:0123456789abcdef",
        "lane_id": "data_context",
        "data_needed": True,
        "read_requests": [
            {
                "operation": "read",
                "tool_name": "warehouse_query",
                "metric": "enterprise accounts",
                "aggregation": "count",
                "informs_decision": "whether SSO ships first",
                "values": [{"value": 41}],
            }
        ],
    }

    assert list(validator.iter_errors(proposal)) == []
    assert (
        list(validator.iter_errors({**proposal, "no_evidence_reason": "not_a_measurement"})) != []
    )


def test_neither_state_can_borrow_the_other_state_s_fields() -> None:
    """The class, not the instance: four rounds found this schema admitting a
    shape that is not a coherent answer, each time through a different field.

    The root is that it is written as one object with conditions rather than as
    two complete states, so a field belonging to one state stays spellable in
    the other until someone forbids it by name. Rather than wait for the next
    field to be added and the forbidding to be forgotten, every property is
    checked here: each must be legal in the no-op state, in the measured state,
    or in both deliberately — never legal in one only by omission.
    """
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    validator = Draft202012Validator(schema)
    identity = "interview-question:0123456789abcdef"
    read = {
        "operation": "read",
        "tool_name": "warehouse_query",
        "metric": "enterprise accounts",
        "aggregation": "count",
        "informs_decision": "whether SSO ships first",
        "values": [{"value": 41}],
    }
    states = {
        "no_op": {
            "question_identity": identity,
            "lane_id": "data_context",
            "data_needed": False,
            "read_requests": [],
            "no_evidence_reason": "not_a_measurement",
        },
        "proposal": {
            "question_identity": identity,
            "lane_id": "data_context",
            "data_needed": True,
            "read_requests": [read],
        },
    }
    for name, answer in states.items():
        assert list(validator.iter_errors(answer)) == [], name

    # Structure does the forbidding now: each state is a closed object, so a
    # field it does not name is rejected without anyone having named it as
    # forbidden. This walks the states rather than a hand-written list, so a
    # field added to one of them is covered the day it is added.
    branches = _answer_states()
    owned = {
        title: set(state["properties"]) - set(branches["NoMeasurementNeeded"]["properties"])
        | set(state["properties"]) - set(branches["MeasurementTaken"]["properties"])
        for title, state in branches.items()
    }
    assert owned["NoMeasurementNeeded"], "the states must differ, or oneOf decides nothing"

    for title, fields in owned.items():
        borrower = "proposal" if title == "NoMeasurementNeeded" else "no_op"
        owner = "no_op" if title == "NoMeasurementNeeded" else "proposal"
        for field in fields:
            if field not in states[owner]:
                continue
            answer = {**states[borrower], field: states[owner][field]}
            assert list(validator.iter_errors(answer)) != [], f"{borrower} borrowed {field}"


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
    request_schema = _read_request_schema()
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
            "values": [{"value": 41}],
            "source_class": "local",
        }
    ]

    reported = _validate_against_contract(answer, interview_data_evidence_answer_contract())
    assert any(violation.endswith(": additionalProperties") for violation in reported), reported


def test_the_shape_admits_the_measurement_and_nothing_beside_it() -> None:
    """A value fits where the contract put one, and nowhere else.

    This test used to prove the opposite -- that a child which ran a lookup had
    nowhere to hand the result back. That was the barrier until #1825, and it
    was aimed at the wrong thing: what #1754 set out to stop was a guess
    becoming the Seed's evidence, and withholding the number made the guess
    likelier. What survives is the closure. `values` is the only place a result
    may land; a field the child invents for one is refused, so there is still
    exactly one shape a consumer has to read.
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
            "values": [{"value": 41}],
        }
    ]
    answer.pop("no_evidence_reason")
    assert list(Draft202012Validator(schema).iter_errors(answer)) == []

    answer["observed_value"] = 42
    reported = _validate_against_contract(answer, interview_data_evidence_answer_contract())
    assert any(violation.endswith(": additionalProperties") for violation in reported), reported


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
            "values": [{"value": 41}],
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
            "values": [{"value": 41}],
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
@pytest.mark.asyncio
async def test_the_public_submit_tool_refuses_a_self_contradicting_answer(tmp_path: Any) -> None:
    """Through re-entry, because that is where the contradiction would land.

    Schema validation alone would prove the shape is rejected; this proves the
    lane is excluded from the aggregation rather than reaching the host's
    rendering as a measurement the same answer disowned.
    """
    from ouroboros.mcp.tools.evaluation_handlers import SubmitFanoutResultsHandler

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    contradictory = _fully_populated_proposal(identity)
    contradictory["no_evidence_reason"] = "not_a_measurement"
    results = [
        entry for entry in _required_results(lane_ids, identity) if entry["key"] != "data_context"
    ]
    results.append({"key": "data_context", "content": contradictory})

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


@pytest.mark.asyncio
async def test_the_public_submit_tool_also_refuses_a_malformed_declaration(tmp_path: Any) -> None:
    """Through the tool that accepts the unconstrained result objects."""
    from ouroboros.mcp.tools.evaluation_handlers import SubmitFanoutResultsHandler
    from ouroboros.orchestrator.disposable_memory import DisposableMemory
    from ouroboros.persistence.artifact_store import ArtifactStore

    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    required = [entry["key"] for entry in _required_results(lane_ids, identity)]
    disposable = DisposableMemory(artifact_store=ArtifactStore.for_project(tmp_path))
    submit = SubmitFanoutResultsHandler(
        fanout_registry=registry,
        disposable_memory=disposable,
    )

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
    envelope = honest.unwrap().meta
    meta = disposable.fetch(envelope["contract_id"]).body
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
    # exist to be read by the user beside the measurement that already ran, so
    # they cannot be closed without taking that reading away. What protects the
    # user is where this can travel, and the assertions below are that boundary.
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
    assert set(record.synthesizer_input) == {"lane_ids", "phase"}


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
    for state in _answer_states().values():
        assert "session_id" not in state["properties"], state["title"]
        assert "session_id" not in state["required"], state["title"]

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


def test_a_measurement_may_cross_issuances_of_the_same_question(tmp_path: Any) -> None:
    """Two fan-outs for the same question text share one identity, by design.

    A question re-emitted on resume, or asked twice in one session, produces the
    same `question_identity` because that identity is a digest of the question
    text, so a measurement taken for one issuance satisfies the other.

    The original reason for allowing this was that the child returned a proposal
    and never a measurement, so nothing time-sensitive could cross. That stopped
    being true in #1825, and the behaviour is kept anyway on a different
    reasoning, recorded here so the next reader is not working from the retired
    one.

    A measurement that crosses issuances measured *the same question*, and a
    measurement of the same question is that measurement whenever it was taken —
    ageing is accepted unconditionally by RFC #1754's second revision, which is
    also why `observed_at` no longer exists to caption it. The decision it could
    corrupt is guarded by the one rule that does the work: a measurement
    arriving after the user has answered is dropped rather than shown. What is
    genuinely lost is the record that this round's lane did not run.

    Closing that needs a token the child echoes back, because the server cannot
    otherwise tell when a payload was produced. A required field is a new way
    for a required lane to stall on every turn — the defect class this contract
    spent eight fixes closing — bought against a narrow and already-disclosed
    harm. The trade is negative, so it is an accepted risk rather than a gap
    (Q00/ouroboros#1825).
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
# What the user is shown
# --------------------------------------------------------------------------- #


def _fully_populated_proposal(identity: str) -> dict[str, Any]:
    """A measurement carrying every field the grouped shape allows.

    Grouped, because that shape is the superset: it is the only one with
    `group_by`, and its values carry the label the ungrouped shape forbids.
    """
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
                "values": [{"group": "enterprise", "value": 41}],
            }
        ],
    }


def test_the_host_receives_every_read_request_field_verbatim(tmp_path: Any) -> None:
    """ "Render the request whole" has to be satisfiable, not just instructed.

    The host renders the measurement beside the question, so what the user can
    be shown is bounded by what reaches the host. That is only worth pinning if
    the measurement arrives as the child returned it — a field dropped in aggregation is one
    the user can never be shown, and two measurements differing only in a filter
    would then arrive identical. So the passthrough is pinned here rather than
    the prose being guarded: what makes an omission impossible is that there is
    no summarizing step between the child and the host's rendering.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id, lane_ids, identity = _registered_advisory(registry)
    proposal = _fully_populated_proposal(identity)
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    request_schema = _read_request_schema()
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


def test_the_surviving_boundary_is_stated_in_both_host_contracts() -> None:
    """`skills/` is the single shipped contract: the wheel's payload and what a
    marketplace install reads (plugin.json points at it; the old
    `.claude-plugin/skills/` copy was never loaded and is gone).

    What the contract must carry changed with the lane. There is no
    confirmation surface to describe any more, and correspondingly less
    standing between a measurement and the Seed: the host putting the number
    beside the question rather than into the answer is now the whole of it, so
    that is the sentence that has to be present.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    skill = root / "skills" / "interview" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")
    assert "there is no `[from-data]` answer to" in content, skill
    assert "material for the user's" in content, skill
    # The drop-after-answer duty is what a measurement crossing issuances is
    # weighed against (see the cross-issuance test above): the server cannot
    # tell when a payload was produced, so this rule — not a token the child
    # echoes — is what keeps a late number from re-opening a settled
    # decision. It is a host duty, so the duty being *stated* is the whole of
    # what this side can pin, and losing the sentence would silently retire
    # the guard the accepted risk rests on.
    assert "already answered the question by the time the measurement" in content, skill
    assert "drop it" in content, skill
    assert "evidence informs a\n     decision, it does not revisit one" in content, skill
    assert "data_context" not in content, skill
    assert "[from-data]" not in content, skill
    # The retired gate: leaving it would have hosts waiting to confirm a
    # read that already ran.
    assert "Run a read only after the user confirms" not in content, skill


# --------------------------------------------------------------------------- #
# Durable replay
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", ["directory", "write"])
def test_no_id_is_stamped_when_the_record_could_not_be_written(
    tmp_path: Any, monkeypatch: Any, failure: str
) -> None:
    """An id is a promise that a later submission can redeem it.

    Registration used to swallow a persistence failure and return the id
    anyway, so a full disk produced a fan-out id over no record — and the host
    found out at submission, after every child had run, when re-entry answered
    `unknown_fanout_id`. Failing to register costs the turn its re-entry;
    issuing an unredeemable id costs the turn its results (Q00/ouroboros#1754,
    which lists "no stamped id when registration failed to persist").
    """
    from ouroboros.mcp.tools import fanout as fanout_module

    if failure == "directory":
        monkeypatch.setattr(
            fanout_module,
            "secure_directory",
            lambda _directory: (_ for _ in ()).throw(OSError("disk full")),
        )
    else:
        monkeypatch.setattr(fanout_module, "write_owner_only", lambda *_args, **_kwargs: False)

    registry = FanoutRegistry(tmp_path)
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

    # The turn still happens — the lanes are dispatched and the question is
    # still answered. What it loses is re-entry, and it loses it visibly.
    assert meta["question_advisory_subagents"]
    assert "question_advisory_fanout_id" not in meta


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
                for branch in lane["answer_contract"]["response_model_schema"]["oneOf"]:
                    branch["required"].append("caveats")
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

    Four findings on this PR were the same shape: a guarantee stated in a
    description, a policy block, a design document, or a reason constant, with
    nothing on the code path making it so. A contract addressed to the child holds what it must
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


# --------------------------------------------------------------------------- #
# Measurability is judged against the environment, not against the sentence
# --------------------------------------------------------------------------- #


def test_the_prompt_does_not_forbid_the_discovery_it_permits() -> None:
    """One prompt must not both allow discovery and ban touching any tool.

    The lane brief said "you may discover which data tools exist"; the Lane Task
    rendered above it said "decide from the question text ALONE, before touching
    any tool". Facing the conflict a child obeys the earlier, more absolute
    instruction, so the self-selection the lane is built around never ran --
    observed as a `data_needed=false` verdict reached with zero tool uses.

    The calling prohibition is gone too (#1825): the lane runs the read. What
    this still pins is the ordering the conflict destroyed — the lane looks at
    what is described before it decides whether the answer is a measurement.
    """
    prompt = _data_payload()["prompt"]

    for absolute_ban in (
        "before touching any tool",
        "from the question text ALONE",
        "from the question text alone",
    ):
        assert absolute_ban not in prompt


def test_discovery_is_instructed_before_the_verdict() -> None:
    """Order is load-bearing: the verdict depends on what discovery finds."""
    prompt = _data_payload()["prompt"]

    discovery_at = prompt.lower().index("read what data stores are described")
    verdict_at = prompt.index("is a measurement")

    assert discovery_at < verdict_at


def test_no_reason_lets_the_lane_speak_about_the_host() -> None:
    """A subagent sees what reached it, not what is connected.

    `no_data_tool_available` was removed for this. It was reported for a
    question about EKS cost while a Mimir store holding exactly the container
    CPU/memory series sat described in the lane's own prompt — the lane had no
    loadable tool schemas and read that as "no data path exists". The parent
    relayed it to the user as a fact about their infrastructure. It was false,
    and it suppressed the only evidence that could bound the answer.
    """
    from ouroboros.orchestrator.capabilities.interview_schemas import (
        DATA_NO_EVIDENCE_REASONS,
    )

    assert "no_data_tool_available" not in DATA_NO_EVIDENCE_REASONS
    # The two claims it collapsed, split by what the lane can decide alone.
    assert "no_data_store_described" in DATA_NO_EVIDENCE_REASONS
    assert "store_described_but_not_callable" in DATA_NO_EVIDENCE_REASONS

    prompt = _data_payload()["prompt"]
    for reason in DATA_NO_EVIDENCE_REASONS:
        assert reason in prompt, reason


def test_unreachability_requires_an_attempted_call() -> None:
    """A search result is not evidence of absence.

    Descriptions say what stores exist; tool schemas say what is invocable. A
    store can be described and its tools unloadable at the instant the lane
    looks — which is what happened, because the subagent's tool snapshot froze
    before those servers finished connecting.
    """
    prompt = _data_payload()["prompt"]

    assert "attempted the call" in prompt
    assert "not evidence of absence" in prompt


def test_the_contract_instruction_agrees_with_the_lane_task() -> None:
    """The contract is rendered inside the same prompt; it cannot disagree."""
    contract = interview_data_evidence_answer_contract()
    instruction = contract["runtime_instruction"]

    assert "before you judge" in instruction
    assert "no_data_store_described" in instruction
    assert "store_described_but_not_callable" in instruction
    assert "not evidence of absence" in instruction
    assert "from the question text alone" not in instruction


def test_the_rendered_contract_fits_inside_its_own_bound() -> None:
    """A truncated contract is not a smaller contract — it is an unsatisfiable one.

    The child is validated field-for-field at re-entry, and this lane is
    `required: true`, so a contract cut mid-render cannot be answered and the
    fan-out cannot complete at all. The bound exists to stop a runaway contract
    from crowding out the question; it is a backstop, and a legitimate contract
    must never reach it.

    Pinned with headroom rather than at the exact size, because a bound met
    exactly is one the next field breaks.
    """
    import json

    from ouroboros.mcp.tools.advisory_prompts import _INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS

    rendered = json.dumps(
        interview_data_evidence_answer_contract(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )

    assert len(rendered) < _INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS
    # And the prompt the child actually receives shows it whole.
    assert "[truncated]" not in _data_payload()["prompt"]


# --------------------------------------------------------------------------- #
# A required lane must always be able to answer
# --------------------------------------------------------------------------- #
#
# This lane is `required: true` and a contract violation is popped from the
# aggregation, so every shape a well-behaved child can honestly produce must
# validate. A rejection here is not strictness — it is the fan-out stalling and
# the user seeing no measurement at all.


def _measured(**patch: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "operation": "read",
        "tool_name": "clickhouse",
        "metric": "completed trial lessons",
        "aggregation": "count",
        "informs_decision": "which completion definition to use",
        "values": [{"value": 41}],
    }
    request.update(patch)
    return {
        "question_identity": "interview-question:0123456789abcdef",
        "lane_id": "data_context",
        "data_needed": True,
        "read_requests": [request],
    }


def _accepts(answer: dict[str, Any]) -> bool:
    schema = interview_data_evidence_answer_contract()["response_model_schema"]
    return list(Draft202012Validator(schema).iter_errors(answer)) == []


def test_a_read_that_came_back_empty_is_still_a_measurement() -> None:
    """Zero rows is a result, and often the informative one.

    The observation that moved this lane to executing was "zero cost-series
    metrics exist". A `minItems: 1` on `values` would make that finding
    unspellable — and leave the child nowhere to go, because no
    `no_evidence_reason` means "I measured and it was empty", so it would have
    to pick one that is false.
    """
    assert _accepts(_measured(values=[]))
    # An empty `values` is the read attesting it ran and found nothing, which
    # an absent answer cannot say.
    assert _measured(values=[])["read_requests"][0]["values"] == []


def test_every_bound_the_child_can_hit_is_stated_in_its_description() -> None:
    """A limit the child cannot see is a stall, not a limit.

    `values` is capped, and a child that exceeds the cap is rejected and popped.
    It can only avoid that if the cap is written where it reads.
    """
    values_schema = _read_request_schema()["properties"]["values"]
    cap = values_schema["maxItems"]

    assert str(cap) in values_schema["description"]
    assert "Empty means" in values_schema["description"]
    # And the cap is real, so the description is not decorative.
    assert not _accepts(_measured(values=[{"group": str(i), "value": i} for i in range(cap + 1)]))


def test_a_grouped_value_carries_its_label_or_the_read_is_not_grouped() -> None:
    """The schema could not say which category a number belonged to.

    One object with an optional `group` accepted `group_by=["region","plan"]`
    beside `values=[{"value": 41}]` — every category lost, two different
    aggregates rendered identically — and equally accepted a label on a read
    that declared no grouping. A field optional where it is required is a field
    that is not required, so the read is two closed shapes, like the answer.
    """
    assert _accepts(_measured(values=[{"value": 41}]))
    assert not _accepts(_measured(values=[{"group": "seoul", "value": 41}]))
    assert _accepts(_measured(group_by=["region"], values=[{"group": "seoul", "value": 41}]))
    assert not _accepts(_measured(group_by=["region"], values=[{"value": 41}]))
    # An empty result stays spellable in either shape.
    assert _accepts(_measured(group_by=["region"], values=[]))


def test_grouping_takes_one_key_because_one_label_can_only_name_one() -> None:
    """Four keys were never representable; the capacity only bought ambiguity."""
    assert not _accepts(
        _measured(group_by=["region", "plan"], values=[{"group": "seoul", "value": 1}])
    )
    shapes = _read_request_shapes()
    assert shapes["GroupedMeasurement"]["properties"]["group_by"]["maxItems"] == 1
    assert "group_by" not in shapes["UngroupedMeasurement"]["properties"]


def test_a_nested_alternative_still_reports_the_rule_that_failed() -> None:
    """`oneOf` must not be the report; it is what the host already knows.

    The answer is one of two states and a measured read is in turn one of two
    shapes, so flattening one level turned the outer `oneOf` into an inner one.
    A violation names the path and the rule so the host can fix it.
    """
    answer = _measured(group_by=["region"], values=[{"value": 41}])
    answer["read_requests"][0]["source_class"] = "warehouse"

    reported = _validate_against_contract(answer, interview_data_evidence_answer_contract())

    assert reported
    assert not any(v.endswith(": oneOf") for v in reported), reported


# --------------------------------------------------------------------------- #
# The class, not the last instance
# --------------------------------------------------------------------------- #
#
# Eight defects were found in this contract across one PR, and every one was the
# same mismatch: a field constraint chosen from what the author meant rather than
# from what a producer can emit. They split two ways and only two — the contract
# refuses something honest (which stalls a `required: true` lane), or it admits
# something that is not a measurement (which puts non-evidence beside a
# question). Fixing them one at a time is how a ninth arrives, so both questions
# are asked of every field here instead.


_HONEST = {
    "a fractional aggregate": {"aggregation": "average", "values": [{"value": 41.7}]},
    "a signed sum": {"aggregation": "sum", "values": [{"value": -1200}]},
    # Negative and fractional are honest for what computes over things, and are
    # refused for what tallies them — the matrix asked only about the value and
    # missed that the constraint lives between two fields (review round 21).
    "a negative value under a computing aggregation": {
        "aggregation": "min",
        "values": [{"value": -3}],
    },
    "zero — often the informative result": {"values": [{"value": 0}]},
    "a large count": {"values": [{"value": 1_500_000_000}]},
    "a non-ASCII metric": {"metric": "완료 레슨 수"},
    "an MCP tool name": {"tool_name": "mcp__lemonboard-mimir__query"},
    "a non-ASCII group label": {
        "group_by": ["region"],
        "values": [{"group": "서울 강남", "value": 1}],
    },
    "a time window written for a human": {"time_window": "2026-05-01 ~ 2026-08-01"},
    "a range as two scalar filters": {
        "filters": [
            {"field": "d", "comparator": "gte", "value": "2026-05-01"},
            {"field": "d", "comparator": "lte", "value": "2026-08-01"},
        ]
    },
}

_NOT_A_MEASUREMENT = {
    "a value that is a string": {"values": [{"value": "41"}]},
    "a value that is null": {"values": [{"value": None}]},
    "an empty metric": {"metric": ""},
    "an aggregation nobody defined": {"aggregation": "top_n"},
    "a write wearing a read's label": {"operation": "write"},
    "a query spelled in the tool name": {"tool_name": "SELECT count(*) FROM t"},
    "a query spelled in a grouping key": {
        "group_by": ["SELECT 1"],
        "values": [{"group": "x", "value": 1}],
    },
    "a row list wearing an aggregate's name": {
        "group_by": ["r"],
        "values": [{"group": str(i), "value": i} for i in range(21)],
    },
    "a field the child invented": {"source_class": "warehouse"},
    "a negative tally": {"aggregation": "count", "values": [{"value": -1}]},
    "a fractional tally": {"aggregation": "count", "values": [{"value": 1.5}]},
    "a fractional distinct tally": {"aggregation": "distinct_count", "values": [{"value": 2.5}]},
}


@pytest.mark.parametrize("case", sorted(_HONEST))
def test_the_contract_admits_every_honest_measurement(case: str) -> None:
    """A shape a producer can honestly emit and the contract refuses is a stall."""
    assert _accepts(_measured(**_HONEST[case]))


@pytest.mark.parametrize("case", sorted(_NOT_A_MEASUREMENT))
def test_the_contract_admits_nothing_that_is_not_a_measurement(case: str) -> None:
    """Anything this accepts is rendered to a user as evidence."""
    assert not _accepts(_measured(**_NOT_A_MEASUREMENT[case]))


def test_every_declared_constant_is_usable_by_the_child() -> None:
    """A constant the schema names and the schema rejects is a trap."""
    from ouroboros.orchestrator.capabilities.interview_schemas import (
        DATA_AGGREGATIONS,
        DATA_NO_EVIDENCE_REASONS,
    )

    for aggregation in DATA_AGGREGATIONS:
        assert _accepts(_measured(aggregation=aggregation)), aggregation
    for reason in DATA_NO_EVIDENCE_REASONS:
        assert _accepts(
            {
                "question_identity": "interview-question:0123456789abcdef",
                "lane_id": "data_context",
                "data_needed": False,
                "no_evidence_reason": reason,
            }
        ), reason


# --------------------------------------------------------------------------- #
# Constraints that live between two fields
# --------------------------------------------------------------------------- #
#
# The completeness matrix above asked two questions of every field and missed
# these, because each is a relation: a value is honest or not depending on the
# aggregation beside it, a timestamp depending on the clock, a marker depending
# on what follows it. The class was "a constraint chosen from what the author
# meant"; the part left open was that a constraint can span fields.


def _advisory_meta_for_strip() -> dict[str, Any]:
    """Meta carrying a real host directive, so the strip sees its own output."""
    from ouroboros.backends.capabilities import SubagentDispatchMode
    from ouroboros.mcp.tools.authoring_handlers import _attach_question_assist_requests

    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-strip",
        question="Real question?",
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="claude",
    )
    return meta


def test_a_tally_cannot_be_negative_or_fractional() -> None:
    """`count` and `distinct_count` count things; nothing else is their result."""
    for aggregation in ("count", "distinct_count"):
        assert not _accepts(_measured(aggregation=aggregation, values=[{"value": -1}]))
        assert not _accepts(_measured(aggregation=aggregation, values=[{"value": 1.5}]))
        assert _accepts(_measured(aggregation=aggregation, values=[{"value": 0}]))
        assert _accepts(_measured(aggregation=aggregation, values=[{"value": 41}]))


def test_what_computes_over_things_keeps_the_whole_number_line() -> None:
    """The tally rule must not spread to aggregations it is false for.

    A sum over signed quantities is signed; an average over anything is
    fractional. `sum` is deliberately outside the counting set.
    """
    for aggregation, value in (
        ("sum", -1200),
        ("average", 41.7),
        ("min", -40),
        ("rate", -0.2),
        ("p95", 12.5),
    ):
        assert _accepts(_measured(aggregation=aggregation, values=[{"value": value}])), aggregation


def test_parsing_our_own_response_keeps_a_question_that_mentions_the_marker() -> None:
    """Shape is all this side has when it reads a response it just produced.

    `auto` extracts the question it must answer from the response body, so there
    is no second copy to compare against and the directive can only be
    recognised by its form. That is enough here and not enough everywhere: a
    question quoting the marker *and* the directive's opening is cut short by
    this function too when it is called — and it is called only when
    `directive_was_appended` says the server wrote one, which is what keeps the
    quote in front of the cut. Pinned at the handler in
    `test_interview_lateral_review_advisory.py`.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )
    from ouroboros.mcp.tools.advisory_dispatch import (
        split_appended_dispatch as split,
    )

    quoted = f"What does this token mean: {marker} and should it be preserved?"
    assert split(quoted) == quoted
    assert split(f"Explain {marker}") == f"Explain {marker}"

    meta = _advisory_meta_for_strip()
    assert split(append_question_advisory_dispatch("Real question?", meta)) == "Real question?"
    both = append_question_advisory_dispatch(f"What is {marker} for?", meta)
    assert split(both) == f"What is {marker} for?"


def test_a_tied_branch_reports_the_shape_the_answer_was_trying_to_be() -> None:
    """Wrong-branch advice is what flattening exists to avoid.

    An answer declaring `group_by` and omitting a label was told "group_by is
    not an allowed field" — the ungrouped branch's reason, reached because both
    branches objected once and the tie fell to declaration order.
    """
    answer = _measured(group_by=["region"], values=[{"value": 41}])

    reported = _validate_against_contract(answer, interview_data_evidence_answer_contract())

    assert reported == ["read_requests/0/values/0: required"], reported


def test_an_ungrouped_read_returns_one_aggregate() -> None:
    """Several unlabelled numbers are a row list with nothing to tell them apart.

    Found by enumerating the contract's cross-field relations rather than by a
    review round: categorical grouping exists to stop a row list, and this is
    the same shape arriving where there is no grouping at all.
    """
    assert _accepts(_measured(values=[{"value": 41}]))
    assert _accepts(_measured(values=[]))
    assert not _accepts(_measured(values=[{"value": 1}, {"value": 2}]))


def test_a_group_appears_once_or_it_is_not_an_aggregate() -> None:
    """Two numbers under one label is a row list wearing an aggregate's name.

    Checked at re-entry rather than in the schema: Draft 2020-12 has
    `uniqueItems` for whole items and nothing for uniqueness by property, so
    claiming it in the contract would be a rule that reads as enforced and is
    not.
    """
    from ouroboros.mcp.tools.fanout import _aggregate_violations

    repeated = _measured(
        group_by=["region"],
        values=[{"group": "seoul", "value": 1}, {"group": "seoul", "value": 2}],
    )
    distinct = _measured(
        group_by=["region"],
        values=[{"group": "seoul", "value": 1}, {"group": "busan", "value": 2}],
    )

    assert _aggregate_violations(repeated)
    assert not _aggregate_violations(distinct)


# --------------------------------------------------------------------------- #
# An impossible time is unspellable, because there is no field to spell it in
# --------------------------------------------------------------------------- #
#
# Review round 21 asked re-entry to reject clearly-future `observed_at` values,
# after round 20 asked for component ranges and round 19 for anything at all.
# Three rounds of validators on one field is the signal the field is wrong, not
# the validator: an LLM child has no clock, so requiring it to testify about
# time was asking for a fact it could only guess.
#
# RFC #1754's second revision removes the field instead. Ageing is accepted
# unconditionally — a measurement of the same question is that measurement
# whenever it was taken — and the interview session is the time envelope, so a
# field restating it carried nothing any consumer read. These tests pin the
# closure as structural, which is the difference between "we check for it" and
# "it cannot be said".


def test_a_measurement_has_nowhere_to_put_a_time() -> None:
    """No validator can be wrong about a field that does not exist.

    The three rejected timestamps below are the exact values earlier rounds
    added validators for. Each is now refused by the same rule that refuses any
    invented field, so there is no clock, no skew allowance, and no zone policy
    left to get wrong.
    """
    for impossible in ("9999-12-31T23:59:59Z", "2026-13-40T29:99:99Z", "not a time"):
        assert not _accepts(_measured(observed_at=impossible)), impossible
    # ...and a well-formed one is refused too, which is the point: the field is
    # gone, not merely validated.
    assert not _accepts(_measured(observed_at="2026-08-02T06:00:00Z"))

    for shape in _read_request_shapes().values():
        assert "observed_at" not in shape["properties"], shape["title"]
        assert "observed_at" not in shape["required"], shape["title"]


def test_no_surface_still_asks_the_child_for_a_time() -> None:
    """A prompt outliving its contract is this module's recurring defect.

    The lane task, the brief, and the contract's own instruction are rendered
    into one prompt; an instruction to stamp a field the schema no longer has
    would be a required lane told to produce a rejected answer.
    """
    prompt = _data_payload()["prompt"]
    instruction = interview_data_evidence_answer_contract()["runtime_instruction"]

    assert "observed_at" not in prompt
    assert "observed_at" not in instruction


def test_ordinary_interview_has_no_timestamped_measurement_surface() -> None:
    """Ordinary interview skills no longer own an automatic measurement lane."""
    from pathlib import Path

    skill = Path("skills/interview/SKILL.md")
    content = skill.read_text(encoding="utf-8")
    assert "observed_at" not in content, skill
    assert "Say when each was measured" not in content, skill
    assert "already answered the question by the time the measurement" in content, skill
