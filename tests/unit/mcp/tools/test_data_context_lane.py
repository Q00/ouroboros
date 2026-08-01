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
        "session_id": "sess-data",
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
                "session_id": "sess-data",
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
