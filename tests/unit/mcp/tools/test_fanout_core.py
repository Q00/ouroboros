"""Generic interview fan-out core + ``ouroboros_submit_fanout_results`` re-entry.

Covers PR-J:
- ``build_fanout_subagents`` generic builder,
- ``stamp_fanout_meta`` 3-mode stamping (byte-identical to the legacy inline
  producers),
- ``FanoutRegistry`` persist/load,
- ``submit_fanout_results`` routing (complete / partial / unknown / mismatch),
- end-to-end producer -> registry -> submit for both revived synthesizer kinds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _attach_question_assist_requests,
)
from ouroboros.mcp.tools.evaluation_handlers import (
    LateralThinkHandler,
    SubmitFanoutResultsHandler,
)
from ouroboros.mcp.tools.subagent import (
    FANOUT_KIND_CODE_INVESTIGATION,
    FANOUT_KIND_LATERAL_PERSONA_PANEL,
    FANOUT_KIND_QUESTION_ADVISORY,
    FanoutRecord,
    FanoutRegistry,
    build_fanout_subagents,
    build_interview_question_advisory_subagents,
    build_subagent_payload,
    register_code_investigation_fanout,
    register_lateral_persona_fanout,
    register_question_advisory_fanout,
    stamp_fanout_meta,
    submit_fanout_results,
)
from ouroboros.orchestrator.capabilities import (
    stable_code_investigation_question_identity,
)
from ouroboros.orchestrator.capabilities.interview_schemas import (
    _interview_question_advisory_fanout_metadata,
)

# --------------------------------------------------------------------------- #
# build_fanout_subagents
# --------------------------------------------------------------------------- #


def test_build_fanout_subagents_builds_one_payload_per_request() -> None:
    requests = [
        {"tool_name": "t", "title": "A", "prompt": "pa", "agent": "researcher"},
        {"tool_name": "t", "title": "B", "prompt": "pb", "context": {"lane_id": "code"}},
    ]
    payloads = build_fanout_subagents(requests, "context.lane_id")
    assert [p.title for p in payloads] == ["A", "B"]
    assert payloads[0].agent == "researcher"
    assert payloads[1].agent == "general"
    assert payloads[1].context == {"lane_id": "code"}


def test_build_fanout_subagents_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError, match="requests must not be empty"):
        build_fanout_subagents([], "context.lane_id")
    with pytest.raises(ValueError, match="correlation_key must not be empty"):
        build_fanout_subagents([{"tool_name": "t", "title": "x", "prompt": "y"}], "")


# --------------------------------------------------------------------------- #
# stamp_fanout_meta (byte-identical 3-mode contract)
# --------------------------------------------------------------------------- #


def _payloads(n: int = 2) -> list[Any]:
    return [build_subagent_payload(tool_name="t", title=f"T{i}", prompt=f"p{i}") for i in range(n)]


def test_stamp_fanout_meta_host_driven_prefixed() -> None:
    meta: dict[str, Any] = {}
    stamp_fanout_meta(
        meta,
        prefix="question_advisory",
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        payloads=_payloads(),
        correlation_key="context.lane_id",
    )
    assert meta == {
        "question_advisory_dispatch_mode": "host_driven",
        "question_advisory_host_action": "spawn_subagents",
        "question_advisory_result_correlation_key": "context.lane_id",
    }


def test_stamp_fanout_meta_sequential_bare() -> None:
    meta: dict[str, Any] = {}
    stamp_fanout_meta(
        meta,
        prefix="",
        dispatch_mode=SubagentDispatchMode.SEQUENTIAL,
        payloads=_payloads(),
        correlation_key="context.persona",
    )
    assert meta == {
        "dispatch_mode": "sequential",
        "host_action": "process_payloads_sequentially",
        "result_correlation_key": "context.persona",
    }


def test_stamp_fanout_meta_plugin_passive_stamps_nothing() -> None:
    meta: dict[str, Any] = {}
    stamp_fanout_meta(
        meta,
        prefix="question_advisory",
        dispatch_mode=SubagentDispatchMode.PLUGIN_PASSIVE,
        payloads=_payloads(),
        correlation_key="context.lane_id",
    )
    assert meta == {}


def test_stamp_fanout_meta_empty_payloads_is_noop() -> None:
    meta: dict[str, Any] = {}
    stamp_fanout_meta(
        meta,
        prefix="",
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        payloads=[],
        correlation_key="context.persona",
    )
    assert meta == {}


# --------------------------------------------------------------------------- #
# Byte-identical proof for the refactored advisory producer
# --------------------------------------------------------------------------- #


def _advisory_meta(dispatch_mode: SubagentDispatchMode, **kwargs: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-bytes",
        question="What constraint remains?",
        phase="answer",
        score=None,
        dispatch_mode=dispatch_mode,
        runtime_backend="codex" if dispatch_mode is SubagentDispatchMode.HOST_DRIVEN else "gemini",
        **kwargs,
    )
    return meta


def test_advisory_producer_byte_identical_without_registry() -> None:
    """No registry -> emitted fan-out meta is the exact pre-registry contract."""
    host = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN)
    assert host["question_advisory_contract_id"] == "interview_question_advisory_fanout.v1"
    assert host["question_advisory_dispatch_mode"] == "host_driven"
    assert host["question_advisory_host_action"] == "spawn_subagents"
    assert host["question_advisory_result_correlation_key"] == "context.lane_id"
    assert "question_advisory_fanout_id" not in host

    seq = _advisory_meta(SubagentDispatchMode.SEQUENTIAL)
    assert seq["question_advisory_contract_id"] == "interview_question_advisory_fanout.v1"
    assert seq["question_advisory_dispatch_mode"] == "sequential"
    assert seq["question_advisory_host_action"] == "process_payloads_sequentially"
    assert seq["question_advisory_result_correlation_key"] == "context.lane_id"
    assert "question_advisory_fanout_id" not in seq


def test_advisory_registry_delta_is_exactly_fanout_id(tmp_path: Any) -> None:
    """Adding a registry adds exactly one key: question_advisory_fanout_id."""
    without = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN)
    registry = FanoutRegistry(tmp_path)
    with_registry = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN, fanout_registry=registry)
    added = set(with_registry) - set(without)
    assert added == {"question_advisory_fanout_id"}
    # Every shared key is byte-identical.
    for key in without:
        assert with_registry[key] == without[key]


# --------------------------------------------------------------------------- #
# FanoutRegistry
# --------------------------------------------------------------------------- #


def test_registry_register_and_load_round_trip(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id = registry.register(
        kind=FANOUT_KIND_LATERAL_PERSONA_PANEL,
        session_id="s1",
        correlation_key="context.persona",
        expected_keys=["researcher", "contrarian"],
        synthesizer_input={"entries": [{"persona_id": "researcher", "execution_order": 1}]},
    )
    assert fanout_id.startswith("fanout_")
    loaded = registry.load(fanout_id)
    assert isinstance(loaded, FanoutRecord)
    assert loaded.kind == FANOUT_KIND_LATERAL_PERSONA_PANEL
    assert loaded.expected_keys == ("researcher", "contrarian")


def test_registry_load_unknown_returns_none(tmp_path: Any) -> None:
    assert FanoutRegistry(tmp_path).load("nope") is None


# --------------------------------------------------------------------------- #
# submit_fanout_results routing
# --------------------------------------------------------------------------- #


def test_submit_unknown_fanout_id_is_clean_error(tmp_path: Any) -> None:
    out = submit_fanout_results(
        FanoutRegistry(tmp_path),
        session_id="s",
        correlation_key="context.persona",
        results=[],
        fanout_id="ghost",
    )
    assert out["status"] == "unknown_fanout_id"
    assert "ghost" in out["error"]


def test_submit_partial_lists_missing_keys(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    payloads = [
        build_subagent_payload(
            tool_name="ouroboros_lateral_think",
            title=f"L ({p})",
            prompt="x",
            agent=p,
            context={"persona": p},
        )
        for p in ("researcher", "contrarian", "simplifier")
    ]
    fanout_id = register_lateral_persona_fanout(registry, session_id="s1", payloads=payloads)
    out = submit_fanout_results(
        registry,
        session_id="s1",
        correlation_key="context.persona",
        results=[{"key": "researcher", "content": "found facts"}],
        fanout_id=fanout_id,
    )
    assert out["status"] == "partial"
    assert out["missing_keys"] == ["contrarian", "simplifier"]
    assert out["received_keys"] == ["researcher"]


def test_submit_correlation_mismatch(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    payloads = [
        build_subagent_payload(
            tool_name="ouroboros_lateral_think",
            title="L (researcher)",
            prompt="x",
            agent="researcher",
            context={"persona": "researcher"},
        )
    ]
    fanout_id = register_lateral_persona_fanout(registry, session_id="s1", payloads=payloads)
    out = submit_fanout_results(
        registry,
        session_id="s1",
        correlation_key="context.lane_id",  # wrong key
        results=[{"key": "researcher", "content": "x"}],
        fanout_id=fanout_id,
    )
    assert out["status"] == "correlation_mismatch"


def test_submit_complete_lateral_panel_routes_to_synthesizer(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    personas = ("researcher", "contrarian", "simplifier")
    payloads = [
        build_subagent_payload(
            tool_name="ouroboros_lateral_think",
            title=f"L ({p})",
            prompt="x",
            agent=p,
            context={"persona": p},
        )
        for p in personas
    ]
    fanout_id = register_lateral_persona_fanout(registry, session_id="s1", payloads=payloads)
    out = submit_fanout_results(
        registry,
        session_id="s1",
        correlation_key="context.persona",
        results=[{"key": p, "content": f"{p}-output"} for p in personas],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert out["kind"] == FANOUT_KIND_LATERAL_PERSONA_PANEL
    result = out["result"]
    # continue_interview_after_lateral_persona_synthesis was exercised.
    assert result["ready_for_synthesis"] is True
    assert result["continued_interview"] is True
    assert result["interview_continuation"]["ready_to_continue"] is True
    agg = result["synthesis"]["aggregated_outputs"]
    assert [item["persona_id"] for item in agg] == list(personas)


def _code_fact_output(session_id: str, question: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "question_identity": stable_code_investigation_question_identity(question),
        "answer_prefix": "[from-code][auto-confirmed]",
        "answer_text": "pyproject.toml declares the package metadata.",
        "confidence": "high_exact_match",
        "evidence": [
            {
                "source": "pyproject.toml",
                "locator": "project.name",
                "claim": "The package name is declared in pyproject.toml.",
            }
        ],
        "requires_user_confirmation": False,
    }


def test_submit_complete_code_investigation_routes_to_synthesizer(tmp_path: Any) -> None:
    # The advisory producer no longer registers a code-investigation record
    # (#1578 follow-up: it registered `code_facts` while stamping
    # `context.lane_id`, so contract-following hosts were rejected). The
    # code-investigation kind is now registered directly from its request.
    registry = FanoutRegistry(tmp_path)
    question = "Which manifest declares the package?"
    session_id = "sess-code"
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id=session_id,
        question=question,
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
    )
    fanout_id = register_code_investigation_fanout(
        registry,
        session_id=session_id,
        request=meta["code_investigation_request"],
    )
    out = submit_fanout_results(
        registry,
        session_id=session_id,
        correlation_key="code_facts",
        results=[{"key": "code_facts", "content": _code_fact_output(session_id, question)}],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert out["kind"] == FANOUT_KIND_CODE_INVESTIGATION
    result = out["result"]
    assert result["ready_for_synthesis"] is True
    assert result["ready_for_forward"] is True
    assert result["contract_violations"] == []


# --------------------------------------------------------------------------- #
# Advisory re-entry regression (#1578 follow-up): the STAMPED contract works
# --------------------------------------------------------------------------- #


def _resolve_correlated_key(payload: Mapping[str, Any], dotted_key: str) -> str:
    """Resolve a payload's correlation value by walking the stamped dotted path."""
    node: Any = payload
    for part in dotted_key.split("."):
        assert isinstance(node, Mapping), f"cannot traverse {dotted_key!r} at {part!r}"
        node = node[part]
    return str(node)


def _emitted_advisory_contract(
    registry: FanoutRegistry, session_id: str
) -> tuple[str, str, list[str]]:
    """Emit an advisory response and read the re-entry contract FROM its meta.

    Returns ``(fanout_id, correlation_key, lane_keys)`` exactly as a
    contract-following host would obtain them: the stamped fan-out id, the
    stamped correlation key, and the per-lane keys resolved by walking that
    dotted key against each emitted advisory payload.
    """
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id=session_id,
        question="Which rollout strategy should we pick?",
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
        fanout_registry=registry,
    )
    fanout_id = meta["question_advisory_fanout_id"]
    correlation_key = meta["question_advisory_result_correlation_key"]
    lane_keys = [
        _resolve_correlated_key(payload, correlation_key)
        for payload in meta["question_advisory_subagents"]
    ]
    assert lane_keys, "advisory fan-out emitted no lanes"
    return fanout_id, correlation_key, lane_keys


@pytest.mark.asyncio
async def test_advisory_reentry_follows_stamped_meta_contract(tmp_path: Any) -> None:
    """Regression (#1578): a host following the STAMPED contract must succeed.

    The producer stamped ``question_advisory_result_correlation_key=
    "context.lane_id"`` but registered a ``code_facts`` code-investigation
    record, so submitting with the stamped key + per-lane keys was rejected
    with ``correlation_mismatch``. Everything submitted here is read from the
    emitted meta/payloads — nothing is hardcoded from server internals.
    """
    registry = FanoutRegistry(tmp_path)
    session_id = "sess-advisory-contract"
    fanout_id, correlation_key, lane_keys = _emitted_advisory_contract(registry, session_id)

    # data_context carries an answer contract, so its submitted output must be
    # contract-conforming JSON (free-text lanes keep plain string outputs).
    def _lane_content(key: str) -> Any:
        if key == "data_context":
            return {
                "lane_id": "data_context",
                "data_needed": False,
                "finding": "No data evidence is needed for this question.",
                "confidence": "no_evidence",
                "evidence": [],
                "proposed_queries": [],
                "requires_user_confirmation": True,
            }
        return f"{key}-advice"

    submit = SubmitFanoutResultsHandler(fanout_registry=registry)
    submit_result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": correlation_key,
            "results": [{"key": key, "content": _lane_content(key)} for key in lane_keys],
        }
    )
    assert submit_result.is_ok, submit_result
    out = submit_result.unwrap().meta
    assert out["status"] == "complete"
    assert out["kind"] == FANOUT_KIND_QUESTION_ADVISORY
    assert out["correlation_key"] == correlation_key
    assert out["contract_violations"] == []
    aggregated = out["result"]["aggregated_outputs"]
    assert [item["lane_id"] for item in aggregated] == lane_keys
    assert [item["output"] for item in aggregated] == [_lane_content(key) for key in lane_keys]


@pytest.mark.asyncio
async def test_advisory_reentry_partial_set_lists_missing_lane_ids(tmp_path: Any) -> None:
    """Submitting a subset of the emitted lanes reports the missing lane ids."""
    registry = FanoutRegistry(tmp_path)
    session_id = "sess-advisory-partial"
    fanout_id, correlation_key, lane_keys = _emitted_advisory_contract(registry, session_id)
    assert len(lane_keys) > 1, "partial-set case needs multiple advisory lanes"

    submit = SubmitFanoutResultsHandler(fanout_registry=registry)
    submit_result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": correlation_key,
            "results": [{"key": lane_keys[0], "content": f"{lane_keys[0]}-advice"}],
        }
    )
    assert submit_result.is_ok, submit_result
    out = submit_result.unwrap().meta
    assert out["status"] == "partial"
    assert out["missing_keys"] == lane_keys[1:]
    assert out["received_keys"] == [lane_keys[0]]


# --------------------------------------------------------------------------- #
# Optional-lane completion semantics (Q00/ouroboros#1671)
# --------------------------------------------------------------------------- #


def _mixed_advisory_payloads() -> list[Any]:
    """Advisory payloads with one optional data lane and two required lanes."""
    request = {
        "session_id": "sess-optional-lanes",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": [
            {
                "lane_id": "data_context",
                "capability": "call_mcp",
                "purpose": "Fetch data evidence.",
                "required": False,
                "data_policy": {"read_only": True, "aggregate_only": True},
            },
            {
                "lane_id": "ambiguity_contrarian",
                "capability": "run_lateral_review",
                "persona": "contrarian",
                "purpose": "Find hidden assumptions.",
                "required": True,
            },
            {
                "lane_id": "answer_simplifier",
                "capability": "run_lateral_review",
                "persona": "simplifier",
                "purpose": "Make it easy to answer.",
                "required": True,
            },
        ],
    }
    return build_interview_question_advisory_subagents(request)


def test_register_question_advisory_fanout_records_required_subset(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-optional-lanes",
        payloads=_mixed_advisory_payloads(),
    )
    record = registry.load(fanout_id)
    assert record is not None
    assert record.expected_keys == ("data_context", "ambiguity_contrarian", "answer_simplifier")
    assert record.required_keys == ("ambiguity_contrarian", "answer_simplifier")


def test_advisory_completes_when_only_optional_lanes_missing(tmp_path: Any) -> None:
    """A host that cannot run an optional lane must not pin the fan-out.

    Before #1671 every emitted lane was an expected completion key, so a
    runtime without MCP access that skipped ``data_context`` was stuck at
    ``status="partial"`` forever.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-optional-lanes",
        payloads=_mixed_advisory_payloads(),
    )
    out = submit_fanout_results(
        registry,
        session_id="sess-optional-lanes",
        correlation_key="context.lane_id",
        results=[
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert out["missing_optional_keys"] == ["data_context"]
    aggregated = out["result"]["aggregated_outputs"]
    assert [item["lane_id"] for item in aggregated] == [
        "ambiguity_contrarian",
        "answer_simplifier",
    ]


def test_advisory_submitted_optional_lane_still_aggregates(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-optional-lanes",
        payloads=_mixed_advisory_payloads(),
    )
    out = submit_fanout_results(
        registry,
        session_id="sess-optional-lanes",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": "data-evidence"},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert out["missing_optional_keys"] == []
    aggregated = out["result"]["aggregated_outputs"]
    assert [item["lane_id"] for item in aggregated] == [
        "data_context",
        "ambiguity_contrarian",
        "answer_simplifier",
    ]


def test_advisory_missing_required_lane_is_still_partial(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-optional-lanes",
        payloads=_mixed_advisory_payloads(),
    )
    out = submit_fanout_results(
        registry,
        session_id="sess-optional-lanes",
        correlation_key="context.lane_id",
        results=[{"key": "ambiguity_contrarian", "content": "contrarian-advice"}],
        fanout_id=fanout_id,
    )
    assert out["status"] == "partial"
    assert out["missing_required_keys"] == ["answer_simplifier"]
    # missing_keys keeps listing every missing lane (backward-compatible).
    assert out["missing_keys"] == ["data_context", "answer_simplifier"]


def test_partial_submissions_accumulate_across_calls(tmp_path: Any) -> None:
    """Submit required lane A, then only the remaining lane B -> complete.

    Each call used to rebuild the provided set from that request alone, so the
    documented "resubmit the remaining lanes" retry contract could never
    complete. Received results now persist on the record between calls.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-optional-lanes",
        payloads=_mixed_advisory_payloads(),
    )
    first = submit_fanout_results(
        registry,
        session_id="sess-optional-lanes",
        correlation_key="context.lane_id",
        results=[{"key": "ambiguity_contrarian", "content": "contrarian-advice"}],
        fanout_id=fanout_id,
    )
    assert first["status"] == "partial"
    assert first["missing_required_keys"] == ["answer_simplifier"]

    second = submit_fanout_results(
        registry,
        session_id="sess-optional-lanes",
        correlation_key="context.lane_id",
        results=[{"key": "answer_simplifier", "content": "simplifier-advice"}],
        fanout_id=fanout_id,
    )
    assert second["status"] == "complete"
    aggregated = second["result"]["aggregated_outputs"]
    assert [item["lane_id"] for item in aggregated] == [
        "ambiguity_contrarian",
        "answer_simplifier",
    ]
    assert aggregated[0]["output"] == "contrarian-advice"


def test_data_lane_output_is_validated_against_answer_contract(tmp_path: Any) -> None:
    """A contract-violating data_context output must not flow to synthesis.

    Bot-review probe (PR #1703): raw PII-shaped evidence with
    ``requires_user_confirmation=false`` previously aggregated as-is under
    ``status="complete"``. The lane's answer contract is persisted at
    registration and enforced at re-entry: violations surface under
    ``contract_violations`` and the violating lane is excluded from the
    aggregation.
    """
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-contract",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-contract", payloads=payloads
    )

    violating_data_output = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "user rows follow",
        "confidence": "reported_by_tool",
        "evidence": [],  # reported_by_tool without executed evidence
        "proposed_queries": [],
        "requires_user_confirmation": False,  # contract forbids skipping the user
        "raw_rows": ["alice@example.com", "bob@example.com"],
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-contract",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": violating_data_output},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )

    assert out["status"] == "complete"
    violations = out["contract_violations"]
    assert [item["lane_id"] for item in violations] == ["data_context"]
    assert violations[0]["contract_id"] == "data_evidence_answer.v1"
    assert violations[0]["errors"]
    aggregated_lanes = [item["lane_id"] for item in out["result"]["aggregated_outputs"]]
    assert "data_context" not in aggregated_lanes


def test_contract_conforming_data_lane_output_aggregates(tmp_path: Any) -> None:
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-contract-ok",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-contract-ok", payloads=payloads
    )

    conforming = {
        "lane_id": "data_context",
        "data_needed": False,
        "finding": "No data evidence is needed for this question.",
        "confidence": "no_evidence",
        "evidence": [],
        "proposed_queries": [],
        "requires_user_confirmation": True,
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-contract-ok",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": conforming},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )

    assert out["status"] == "complete"
    assert out["contract_violations"] == []
    aggregated_lanes = [item["lane_id"] for item in out["result"]["aggregated_outputs"]]
    assert "data_context" in aggregated_lanes


def test_violating_lane_output_is_rejected_before_persistence(tmp_path: Any) -> None:
    """A contract-violating partial submission must never reach durable state.

    Bot-review round-2 probe (PR #1703): raw rows, an email, and a token were
    serialized into ``received_results`` because validation only ran at
    completion. Validation now happens at the door: the violating output is
    reported and excluded, and the persisted record never contains it.
    """
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-door",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-door", payloads=payloads
    )

    pii_output = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "user rows follow",
        "confidence": "reported_by_tool",
        "evidence": [],
        "proposed_queries": [],
        "requires_user_confirmation": False,
        "raw_rows": ["alice@example.com", "token=sk-live-123"],
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-door",
        correlation_key="context.lane_id",
        results=[{"key": "data_context", "content": pii_output}],
        fanout_id=fanout_id,
    )

    assert out["status"] == "partial"
    assert [item["lane_id"] for item in out["contract_violations"]] == ["data_context"]
    assert "data_context" not in out["received_keys"]
    persisted = (tmp_path / f"{fanout_id}.json").read_text()
    assert "alice@example.com" not in persisted
    assert "sk-live-123" not in persisted


def test_completed_fanout_is_terminal(tmp_path: Any) -> None:
    """Replaying a completed fan-out cannot mutate the synthesized outcome."""
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-terminal",
        payloads=_mixed_advisory_payloads(),
    )
    results = [
        {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
        {"key": "answer_simplifier", "content": "simplifier-advice"},
    ]
    first = submit_fanout_results(
        registry,
        session_id="sess-terminal",
        correlation_key="context.lane_id",
        results=results,
        fanout_id=fanout_id,
    )
    assert first["status"] == "complete"

    replay = submit_fanout_results(
        registry,
        session_id="sess-terminal",
        correlation_key="context.lane_id",
        results=[{"key": "ambiguity_contrarian", "content": "MUTATED"}],
        fanout_id=fanout_id,
    )
    assert replay["status"] == "already_complete"
    record = registry.load(fanout_id)
    assert record is not None
    assert record.completed is True
    assert record.received_results["ambiguity_contrarian"] == "contrarian-advice"


def test_partial_reports_failed_accumulation_persistence(tmp_path: Any) -> None:
    """A lost state write must not masquerade as an accepted submission."""
    from unittest.mock import patch

    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-io-fail",
        payloads=_mixed_advisory_payloads(),
    )
    with patch.object(FanoutRegistry, "save", return_value=False):
        out = submit_fanout_results(
            registry,
            session_id="sess-io-fail",
            correlation_key="context.lane_id",
            results=[{"key": "ambiguity_contrarian", "content": "contrarian-advice"}],
            fanout_id=fanout_id,
        )
    assert out["status"] == "partial"
    assert out["accumulation_persisted"] is False


def test_unexpected_key_is_rejected_before_persistence(tmp_path: Any) -> None:
    """A key absent from ``expected_keys`` never enters durable state.

    Bot-review round-3 probe (PR #1703): arbitrary email/token content
    submitted under an unregistered key was accepted and persisted with no
    violation. Unknown keys are now rejected at the door and reported under
    ``unexpected_keys``.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-unexpected",
        payloads=_mixed_advisory_payloads(),
    )
    out = submit_fanout_results(
        registry,
        session_id="sess-unexpected",
        correlation_key="context.lane_id",
        results=[
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
            {"key": "unexpected", "content": "carol@example.com token=sk-live-999"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert out["unexpected_keys"] == ["unexpected"]
    aggregated = [item["lane_id"] for item in out["result"]["aggregated_outputs"]]
    assert "unexpected" not in aggregated
    persisted = (tmp_path / f"{fanout_id}.json").read_text()
    assert "carol@example.com" not in persisted
    assert "sk-live-999" not in persisted


def test_code_investigation_wrong_session_does_not_terminalize(tmp_path: Any) -> None:
    """Synthesis readiness gates terminalization, not key presence.

    Bot-review round-3 probe (PR #1703): a ``code_facts`` output bound to a
    different session returned outer ``status="complete"`` while its result
    said ``ready_for_synthesis=false``, then the record was permanently
    terminal and the corrected retry bounced off ``already_complete``. The
    rejected content is now reported as ``synthesis_rejected_keys``, never
    persisted, and the record stays open for the corrected retry.
    """
    registry = FanoutRegistry(tmp_path)
    question = "Which manifest declares the package?"
    session_id = "sess-code-readiness"
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id=session_id,
        question=question,
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
    )
    fanout_id = register_code_investigation_fanout(
        registry,
        session_id=session_id,
        request=meta["code_investigation_request"],
    )
    wrong = submit_fanout_results(
        registry,
        session_id=session_id,
        correlation_key="code_facts",
        results=[
            {"key": "code_facts", "content": _code_fact_output("some-other-session", question)}
        ],
        fanout_id=fanout_id,
    )
    assert wrong["status"] == "partial"
    assert wrong["synthesis_rejected_keys"] == ["code_facts"]
    assert wrong["missing_required_keys"] == ["code_facts"]
    assert wrong["result"]["ready_for_synthesis"] is False
    record = registry.load(fanout_id)
    assert record is not None
    assert record.completed is False
    assert "code_facts" not in record.received_results

    corrected = submit_fanout_results(
        registry,
        session_id=session_id,
        correlation_key="code_facts",
        results=[{"key": "code_facts", "content": _code_fact_output(session_id, question)}],
        fanout_id=fanout_id,
    )
    assert corrected["status"] == "complete"
    assert corrected["result"]["ready_for_synthesis"] is True


def test_completion_is_not_claimed_when_terminal_write_fails(tmp_path: Any) -> None:
    """A failed terminal write must never masquerade as durable completion.

    Bot-review round-3 probe (PR #1703): with ``save()`` returning ``False``
    the call still reported ``complete``, and a later submission could replace
    the outcome. The response is now ``completion_not_persisted``, the record
    stays open, and a retry completes durably.
    """
    from unittest.mock import patch

    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-terminal-io",
        payloads=_mixed_advisory_payloads(),
    )
    results = [
        {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
        {"key": "answer_simplifier", "content": "simplifier-advice"},
    ]
    with patch.object(FanoutRegistry, "save", return_value=False):
        out = submit_fanout_results(
            registry,
            session_id="sess-terminal-io",
            correlation_key="context.lane_id",
            results=results,
            fanout_id=fanout_id,
        )
    assert out["status"] == "completion_not_persisted"
    assert out["result"]["aggregated_outputs"]
    record = registry.load(fanout_id)
    assert record is not None
    assert record.completed is False

    retry = submit_fanout_results(
        registry,
        session_id="sess-terminal-io",
        correlation_key="context.lane_id",
        results=results,
        fanout_id=fanout_id,
    )
    assert retry["status"] == "complete"
    record = registry.load(fanout_id)
    assert record is not None
    assert record.completed is True


def test_replay_returns_persisted_terminal_outcome(tmp_path: Any) -> None:
    """A caller that lost the completion response can recover the synthesis.

    Bot-review round-3 probe (PR #1703): replaying a completed fan-out
    returned only an ``already_complete`` error, so the terminal outcome was
    unrecoverable. The completion response is persisted on the terminal
    record and replayed.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-replay",
        payloads=_mixed_advisory_payloads(),
    )
    results = [
        {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
        {"key": "answer_simplifier", "content": "simplifier-advice"},
    ]
    first = submit_fanout_results(
        registry,
        session_id="sess-replay",
        correlation_key="context.lane_id",
        results=results,
        fanout_id=fanout_id,
    )
    assert first["status"] == "complete"

    replay = submit_fanout_results(
        registry,
        session_id="sess-replay",
        correlation_key="context.lane_id",
        results=[],
        fanout_id=fanout_id,
    )
    assert replay["status"] == "already_complete"
    assert replay["result"] == first["result"]
    assert replay["missing_optional_keys"] == first["missing_optional_keys"]


def test_data_evidence_pii_shaped_value_is_rejected_at_reentry(tmp_path: Any) -> None:
    """The evidence boundary is enforced at re-entry, without re-leaking.

    Bot-review round-3 probe (PR #1703): a schema-shaped evidence item whose
    value was ``alice@example.com token=sk-live-123`` durably accumulated.
    The boundary scan (aggregates only, PII-scrubbed) now rejects it, and the
    violation report itself never echoes the offending content.
    """
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-boundary",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-boundary", payloads=payloads
    )

    pii_evidence_output = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Aggregate finding text.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "clickhouse_query",
                "query_summary": "count users by plan tier",
                "value": "alice@example.com token=sk-live-123",
                "observed_at": "2026-07-23T09:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time aggregate."],
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-boundary",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": pii_evidence_output},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    violations = out["contract_violations"]
    assert [item["lane_id"] for item in violations] == ["data_context"]
    assert violations[0]["errors"]
    joined = " ".join(violations[0]["errors"])
    assert "alice@example.com" not in joined
    assert "sk-live-123" not in joined
    aggregated = [item["lane_id"] for item in out["result"]["aggregated_outputs"]]
    assert "data_context" not in aggregated
    persisted = (tmp_path / f"{fanout_id}.json").read_text()
    assert "alice@example.com" not in persisted
    assert "sk-live-123" not in persisted


def test_forbidden_operation_proposal_is_rejected_at_reentry(tmp_path: Any) -> None:
    """User confirmation must not make mutating operations permissible.

    Bot-review round-4 probe (PR #1703): a ``DROP TABLE users`` proposal
    validated with no boundary violation, and the skill then instructs the
    host to execute confirmed proposals. The lane's
    ``forbidden_operation_patterns`` are now consulted at re-entry.
    """
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-readonly",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-readonly", payloads=payloads
    )

    mutating_proposal = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "A cleanup query would clarify the numbers.",
        "confidence": "inferred",
        "evidence": [],
        "proposed_queries": [
            {
                "tool_name": "clickhouse_query",
                "query": "DROP TABLE users",
                "expected_decision": "Whether stale rows skew the metric.",
                "source_class": "external",
            }
        ],
        "requires_user_confirmation": True,
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-readonly",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": mutating_proposal},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    violations = out["contract_violations"]
    assert [item["lane_id"] for item in violations] == ["data_context"]
    joined = " ".join(violations[0]["errors"])
    assert "read-only" in joined
    aggregated = [item["lane_id"] for item in out["result"]["aggregated_outputs"]]
    assert "data_context" not in aggregated


def test_row_shaped_evidence_value_is_rejected_at_reentry(tmp_path: Any) -> None:
    """Aggregate-only means aggregate-shaped, not just email/token-free.

    Bot-review round-4 probe (PR #1703): a JSON-encoded list of customer
    names and phone numbers passed validation and entered the terminal
    record. Row-shaped values and phone-shaped digit groups are now raw
    evidence.
    """
    registry = FanoutRegistry(tmp_path)
    request = {
        "session_id": "sess-rows",
        "question_identity": "interview-question:0123456789abcdef",
        "question": "Which plan tier do most active users hit?",
        "user_question_first": True,
        "lanes": _interview_question_advisory_fanout_metadata()["lanes"],
    }
    payloads = build_interview_question_advisory_subagents(request)
    fanout_id = register_question_advisory_fanout(
        registry, session_id="sess-rows", payloads=payloads
    )

    row_output = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Customer sample follows.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "clickhouse_query",
                "query_summary": "sample customers",
                "value": '[{"name": "Alice Kim", "phone": "010-1234-5678"}]',
                "observed_at": "2026-07-23T09:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time sample."],
    }
    out = submit_fanout_results(
        registry,
        session_id="sess-rows",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": row_output},
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert [item["lane_id"] for item in out["contract_violations"]] == ["data_context"]
    persisted = (tmp_path / f"{fanout_id}.json").read_text()
    assert "Alice Kim" not in persisted
    assert "010-1234-5678" not in persisted


def test_impossible_calendar_date_is_rejected_at_reentry(tmp_path: Any) -> None:
    """A range regex cannot see February 31st; parsing can (round-4 warning)."""
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    impossible = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Aggregate finding.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "clickhouse_query",
                "query_summary": "count users",
                "value": "78% of MAU are on the free tier",
                "observed_at": "2026-02-31T10:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time."],
    }
    errors = _data_evidence_boundary_violations(impossible)
    assert any("observed_at" in error for error in errors)
    valid = {
        **impossible,
        "evidence": [{**impossible["evidence"][0], "observed_at": "2026-02-28T10:00:00Z"}],
    }
    assert _data_evidence_boundary_violations(valid) == []


def test_boundary_scan_allows_hyphenated_vocabulary() -> None:
    """Ordinary data metrics are not credential leaks (round-4 warning).

    ``token-counts`` / ``secret-santa`` previously matched the credential
    pattern; a credential suffix must carry digits.
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    clean = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Aggregate token-counts by plan; secret-santa participation is up.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "clickhouse_query",
                "query_summary": "sum token-counts grouped by plan",
                "value": "premium plans average 12,400 tokens/day",
                "observed_at": "2026-07-23",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time."],
    }
    assert _data_evidence_boundary_violations(clean) == []


def test_registration_failure_is_not_advertised(tmp_path: Any) -> None:
    """A fan-out id that cannot be redeemed must never be stamped.

    Bot-review round-4 probe (PR #1703): with ``save`` failing, registration
    still returned a public id whose first re-entry was necessarily
    ``unknown_fanout_id``. Registration now surfaces the failure and the
    producer skips stamping.
    """
    from unittest.mock import patch

    registry = FanoutRegistry(tmp_path)
    with patch.object(FanoutRegistry, "save", return_value=False):
        assert (
            register_question_advisory_fanout(
                registry,
                session_id="sess-reg-fail",
                payloads=_mixed_advisory_payloads(),
            )
            is None
        )
        meta: dict[str, Any] = {}
        _attach_question_assist_requests(
            meta,
            session_id="sess-reg-fail",
            question="Which rollout strategy should we pick?",
            phase="answer",
            score=None,
            dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
            runtime_backend="codex",
            fanout_registry=registry,
        )
    assert "question_advisory_fanout_id" not in meta


def test_failed_record_update_preserves_prior_state(tmp_path: Any) -> None:
    """A torn write must not destroy the state needed for recovery.

    Bot-review round-4 probe (PR #1703): a mid-write ``OSError`` left the
    live record file as ``{``, so the documented resubmission returned
    ``unknown_fanout_id``. Saves are now atomic (temp file + rename): a
    failed update preserves the prior replayable record.
    """
    import json
    from unittest.mock import patch

    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-atomic",
        payloads=_mixed_advisory_payloads(),
    )
    assert fanout_id is not None

    with patch(
        "ouroboros.mcp.tools.subagent.os.replace",
        side_effect=OSError("disk full"),
    ):
        out = submit_fanout_results(
            registry,
            session_id="sess-atomic",
            correlation_key="context.lane_id",
            results=[{"key": "ambiguity_contrarian", "content": "contrarian-advice"}],
            fanout_id=fanout_id,
        )
    assert out["status"] == "partial"
    assert out["accumulation_persisted"] is False
    # The prior record is intact JSON and still loadable for the retry.
    json.loads((tmp_path / f"{fanout_id}.json").read_text())
    record = registry.load(fanout_id)
    assert record is not None

    retry = submit_fanout_results(
        registry,
        session_id="sess-atomic",
        correlation_key="context.lane_id",
        results=[
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert retry["status"] == "complete"


def test_terminal_replay_requires_matching_correlation(tmp_path: Any) -> None:
    """Completion recovery must not cross the registered boundary.

    Bot-review round-4 probe (PR #1703): a different session and correlation
    key received ``already_complete`` with the stored synthesis. Correlation
    is now validated before terminal replay.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-replay-boundary",
        payloads=_mixed_advisory_payloads(),
    )
    first = submit_fanout_results(
        registry,
        session_id="sess-replay-boundary",
        correlation_key="context.lane_id",
        results=[
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
        ],
        fanout_id=fanout_id,
    )
    assert first["status"] == "complete"

    cross = submit_fanout_results(
        registry,
        session_id="some-other-session",
        correlation_key="context.persona",
        results=[],
        fanout_id=fanout_id,
    )
    assert cross["status"] == "correlation_mismatch"
    assert "result" not in cross


def test_fanout_id_is_confined_to_registry_root(tmp_path: Any) -> None:
    """A caller-supplied id can never escape the fan-out directory.

    Bot-review round-4 probe (PR #1703): an absolute ``fanout_id`` made
    ``Path`` joining ignore the registry root, loading (and completing) a
    shaped record outside it. Ids are opaque basenames, enforced inside the
    registry independently of outer input validation.
    """
    import json

    root = tmp_path / "root"
    registry = FanoutRegistry(root)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "fanout_id": "outside",
                "kind": FANOUT_KIND_QUESTION_ADVISORY,
                "session_id": "s1",
                "correlation_key": "context.lane_id",
                "expected_keys": ["lane"],
                "synthesizer_input": {"lane_ids": ["lane"]},
            }
        )
    )

    absolute_id = str(tmp_path / "outside")
    traversal_id = "../outside"
    assert registry.load(absolute_id) is None
    assert registry.load(traversal_id) is None
    for evil_id in (absolute_id, traversal_id):
        out = submit_fanout_results(
            registry,
            session_id="s1",
            correlation_key="context.lane_id",
            results=[{"key": "lane", "content": "x"}],
            fanout_id=evil_id,
        )
        assert out["status"] == "unknown_fanout_id"
    # Registration refuses a non-basename id instead of writing outside root.
    assert (
        registry.register(
            kind=FANOUT_KIND_QUESTION_ADVISORY,
            session_id="s1",
            correlation_key="context.lane_id",
            expected_keys=["lane"],
            synthesizer_input={"lane_ids": ["lane"]},
            fanout_id=absolute_id,
        )
        is None
    )


def test_executed_evidence_claiming_mutation_is_rejected(tmp_path: Any) -> None:
    """The read-only boundary binds executed evidence, not only proposals.

    Bot-review round-5 probe (PR #1703): evidence whose provenance claimed
    ``DELETE FROM customers`` completed and aggregated without violations,
    and ``UPSERT``/``REPLACE``/``CALL`` proposals evaded the forbidden list.
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    deleted_evidence = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Customer count after cleanup.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "external metered warehouse",
                "query_summary": "DELETE FROM customers WHERE stale = 1",
                "value": "1,204 rows affected",
                "observed_at": "2026-07-23T09:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time."],
    }
    errors = _data_evidence_boundary_violations(deleted_evidence)
    assert any("delete" in error and "read-only" in error for error in errors)

    for operation in (
        "UPSERT INTO t VALUES (1)",
        "REPLACE INTO t VALUES (1)",
        "CALL cleanup()",
        "UPDATE users SET tier = 'free'",
        "GRANT ALL ON db TO intern",
    ):
        proposal = {
            "lane_id": "data_context",
            "data_needed": True,
            "finding": "Needs a query.",
            "confidence": "inferred",
            "evidence": [],
            "proposed_queries": [
                {
                    "tool_name": "warehouse",
                    "query": operation,
                    "expected_decision": "n/a",
                    "source_class": "external",
                }
            ],
            "requires_user_confirmation": True,
        }
        assert any(
            "read-only" in error for error in _data_evidence_boundary_violations(proposal)
        ), operation


def test_read_only_vocabulary_is_not_a_forbidden_operation() -> None:
    """The scan matches operation SHAPES, not bare English words.

    Wide-lens regression guard: the lane exists to DELIVER aggregates, and a
    bare-word list rejected legitimate read-only evidence whose provenance
    merely contained "call", "merge", "replace", "grant", or "update".
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    for legit_summary in (
        "call volume by day per plan",
        "merge rate of premium upgrades",
        "weekly replace rate of devices",
        "monthly grant program signups",
        "update frequency of the cache per hour",
        "distribution by created_at",
    ):
        output = _minimal_data_output("78% of MAU are on the free tier")
        output["evidence"][0]["query_summary"] = legit_summary
        assert _data_evidence_boundary_violations(output) == [], legit_summary

    legit_proposal = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Needs a query.",
        "confidence": "inferred",
        "evidence": [],
        "proposed_queries": [
            {
                "tool_name": "warehouse",
                "query": "count calls per user last 30d",
                "expected_decision": "Whether call volume justifies the tier.",
                "source_class": "external",
            }
        ],
        "requires_user_confirmation": True,
    }
    assert _data_evidence_boundary_violations(legit_proposal) == []


def test_single_newline_two_row_value_is_rejected(tmp_path: Any) -> None:
    """An aggregate is a single-line scalar statement.

    Bot-review round-5 probe (PR #1703): two customer rows separated by ONE
    newline passed the multi-newline blacklist and persisted as valid data
    evidence.
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    two_rows = {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Top customers.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "warehouse",
                "query_summary": "top customers by revenue",
                "value": "Kim Minsu, premium tier\nLee Jiwoo, premium tier",
                "observed_at": "2026-07-23T09:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time."],
    }
    errors = _data_evidence_boundary_violations(two_rows)
    assert any("row-shaped" in error for error in errors)


def test_unexpected_key_values_are_redacted_and_not_terminal(tmp_path: Any) -> None:
    """Rejected key VALUES are untrusted content, not identifiers to echo.

    Bot-review round-5 probe (PR #1703): an unexpected key containing an
    email and a token-shaped secret was echoed into ``unexpected_keys`` and
    persisted inside the terminal response. Non-lane-shaped keys are now
    reported as redacted digests, and ``unexpected_keys`` never persists on
    the terminal record.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-redact",
        payloads=_mixed_advisory_payloads(),
    )
    evil_key = "alice@example.com token=sk-live-777"
    out = submit_fanout_results(
        registry,
        session_id="sess-redact",
        correlation_key="context.lane_id",
        results=[
            {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
            {"key": "answer_simplifier", "content": "simplifier-advice"},
            {"key": evil_key, "content": "irrelevant"},
        ],
        fanout_id=fanout_id,
    )
    assert out["status"] == "complete"
    assert len(out["unexpected_keys"]) == 1
    assert out["unexpected_keys"][0].startswith("<redacted-key sha256:")
    persisted = (tmp_path / f"{fanout_id}.json").read_text()
    assert "alice@example.com" not in persisted
    assert "sk-live-777" not in persisted
    assert "unexpected_keys" not in persisted


def test_omitted_correlation_does_not_bypass_the_boundary(tmp_path: Any) -> None:
    """Optional parameters are not an escape hatch (round-5 probe).

    A record registered with a session/correlation identity requires the
    caller to present it — omitting both must not allow completion or
    terminal replay.
    """
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-strict",
        payloads=_mixed_advisory_payloads(),
    )
    results = [
        {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
        {"key": "answer_simplifier", "content": "simplifier-advice"},
    ]
    omitted = submit_fanout_results(
        registry,
        session_id="",
        correlation_key="",
        results=results,
        fanout_id=fanout_id,
    )
    assert omitted["status"] == "correlation_mismatch"

    complete = submit_fanout_results(
        registry,
        session_id="sess-strict",
        correlation_key="context.lane_id",
        results=results,
        fanout_id=fanout_id,
    )
    assert complete["status"] == "complete"

    replay_omitted = submit_fanout_results(
        registry,
        session_id="",
        correlation_key="",
        results=[],
        fanout_id=fanout_id,
    )
    assert replay_omitted["status"] == "correlation_mismatch"
    assert "result" not in replay_omitted


def test_surrogate_content_reports_persistence_failure(tmp_path: Any) -> None:
    """A lone surrogate must degrade honestly, not crash re-entry (round-5)."""
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-surrogate",
        payloads=_mixed_advisory_payloads(),
    )
    out = submit_fanout_results(
        registry,
        session_id="sess-surrogate",
        correlation_key="context.lane_id",
        results=[{"key": "ambiguity_contrarian", "content": "bad \ud800 surrogate"}],
        fanout_id=fanout_id,
    )
    assert out["status"] == "partial"
    assert out["accumulation_persisted"] is False


def test_stale_records_are_swept_on_register(tmp_path: Any) -> None:
    """Completed/orphaned records are retained for a bounded replay window."""
    import os as os_module
    import time

    registry = FanoutRegistry(tmp_path)
    stale_id = register_question_advisory_fanout(
        registry,
        session_id="sess-old",
        payloads=_mixed_advisory_payloads(),
    )
    assert stale_id is not None
    stale_path = tmp_path / f"{stale_id}.json"
    ancient = time.time() - FanoutRegistry._RECORD_RETENTION_SECONDS - 3600
    os_module.utime(stale_path, (ancient, ancient))

    fresh_id = register_question_advisory_fanout(
        registry,
        session_id="sess-new",
        payloads=_mixed_advisory_payloads(),
    )
    assert fresh_id is not None
    assert not stale_path.exists()
    assert (tmp_path / f"{fresh_id}.json").exists()


def _minimal_data_output(value: str) -> dict[str, Any]:
    return {
        "lane_id": "data_context",
        "data_needed": True,
        "finding": "Aggregate finding.",
        "confidence": "reported_by_tool",
        "evidence": [
            {
                "source": "warehouse",
                "query_summary": "count users",
                "value": value,
                "observed_at": "2026-07-23T09:00:00Z",
            }
        ],
        "proposed_queries": [],
        "requires_user_confirmation": True,
        "caveats": ["Point-in-time."],
    }


def test_standard_credential_and_pii_forms_are_rejected() -> None:
    """Bot-review round-6 probe: standard credential/PII forms must not pass.

    ``Authorization: Bearer ...``, password assignments, AWS-style keys, and
    parenthesized US phone numbers previously evaded the denylist.
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    for probe in (
        "Authorization: Bearer abcdef123456",
        "password=abcd1234",
        "AKIAIOSFODNN7EXAMPLE credentials in use",
        "call center at (415) 555-1212",
    ):
        assert _data_evidence_boundary_violations(_minimal_data_output(probe)), probe

    for clean in (
        "authorization required for the premium tier",
        "bearer of the top NPS score is the free plan",
        "password rotation completed for 1,204 accounts",
        "78% of MAU are on the free tier",
    ):
        assert _data_evidence_boundary_violations(_minimal_data_output(clean)) == [], clean


def test_single_line_csv_rows_are_rejected() -> None:
    """Bot-review round-6 probe: single-line raw rows are not aggregates."""
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    for probe in (
        "Alice Kim,premium; Bob Lee,free",
        "Alice Kim, premium; Bob Lee, free",
    ):
        errors = _data_evidence_boundary_violations(_minimal_data_output(probe))
        assert any("row-shaped" in error for error in errors), probe

    # Metric prose with commas and a semicolon keeps its digits and stays
    # valid — the roster rule only fires on digit-free delimited records.
    metric = "revenue up 12%, churn down 3%; retention flat, NPS +4"
    assert _data_evidence_boundary_violations(_minimal_data_output(metric)) == []


def test_error_shaped_tool_output_is_not_evidence() -> None:
    """Bot-review round-6 probe: an error envelope is a failed call.

    The policy's ``error_shaped_tool_output`` rule requires a no-evidence
    finding — ``HTTP 200 body: {"error": ...}`` must never persist as
    ``reported_by_tool`` evidence.
    """
    from ouroboros.mcp.tools.subagent import _data_evidence_boundary_violations

    probe = 'HTTP 200 body: {"error":"upstream timeout"}'
    errors = _data_evidence_boundary_violations(_minimal_data_output(probe))
    assert any("error-shaped" in error for error in errors)

    for probe in ("HTTP 503 from warehouse", "HTTP/502 gateway response"):
        errors = _data_evidence_boundary_violations(_minimal_data_output(probe))
        assert any("error-shaped" in error for error in errors), probe

    clean = "error rate 0.2% across 14,000 jobs"
    assert _data_evidence_boundary_violations(_minimal_data_output(clean)) == []


def test_concurrent_submissions_terminalize_exactly_once(tmp_path: Any) -> None:
    """Terminalization is concurrency-safe (bot-review round-6 probe).

    Two concurrent full submissions previously both returned ``complete``
    with divergent results. The per-fanout exclusive section serializes them:
    exactly one completes, the other replays the terminal outcome.
    """
    from concurrent.futures import ThreadPoolExecutor

    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-concurrent",
        payloads=_mixed_advisory_payloads(),
    )

    def submit(marker: str) -> dict[str, Any]:
        return submit_fanout_results(
            registry,
            session_id="sess-concurrent",
            correlation_key="context.lane_id",
            results=[
                {"key": "ambiguity_contrarian", "content": f"contrarian-{marker}"},
                {"key": "answer_simplifier", "content": f"simplifier-{marker}"},
            ],
            fanout_id=fanout_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(submit, ["a", "b"])

    statuses = sorted([first["status"], second["status"]])
    assert statuses == ["already_complete", "complete"]
    completed = first if first["status"] == "complete" else second
    replayed = second if first["status"] == "complete" else first
    # The replay carries the SAME terminal outcome — never a divergent one.
    assert replayed["result"] == completed["result"]


def test_corrupt_utf8_record_degrades_cleanly(tmp_path: Any) -> None:
    """A torn/corrupt record returns the documented clean outcome (round-6)."""
    registry = FanoutRegistry(tmp_path)
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id="sess-corrupt",
        payloads=_mixed_advisory_payloads(),
    )
    assert fanout_id is not None
    (tmp_path / f"{fanout_id}.json").write_bytes(b'{"fanout_id": "\xff\xfe broken')

    assert registry.load(fanout_id) is None
    out = submit_fanout_results(
        registry,
        session_id="sess-corrupt",
        correlation_key="context.lane_id",
        results=[],
        fanout_id=fanout_id,
    )
    assert out["status"] == "unknown_fanout_id"


def test_known_data_tools_env_is_bounded_and_identifier_validated(monkeypatch: Any) -> None:
    """Env-sourced tool names are identifiers, not prompt text (round-6)."""
    monkeypatch.setenv(
        "OUROBOROS_KNOWN_DATA_TOOLS",
        "clickhouse_query, bad name with spaces, evil\ninjection, " + "x" * 200 + ", metabase",
    )
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-env-bounds",
        question="Which plan tier do most active users hit?",
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
    )
    lanes = {lane["lane_id"]: lane for lane in meta["question_advisory_request"]["lanes"]}
    assert lanes["data_context"]["known_data_tools"] == ["clickhouse_query", "metabase"]


def test_legacy_record_without_required_keys_treats_all_expected_as_required() -> None:
    """Records persisted before the required/optional split keep the old gate."""
    record = FanoutRecord.from_dict(
        {
            "fanout_id": "fanout_legacy",
            "kind": FANOUT_KIND_QUESTION_ADVISORY,
            "session_id": "s1",
            "correlation_key": "context.lane_id",
            "expected_keys": ["code_context", "answer_simplifier"],
            "synthesizer_input": {"lane_ids": ["code_context", "answer_simplifier"]},
        }
    )
    assert record.required_keys == ("code_context", "answer_simplifier")


# --------------------------------------------------------------------------- #
# Registry state-dir threading (#1578 follow-up, MEDIUM)
# --------------------------------------------------------------------------- #


def test_registry_rebase_default_moves_default_location_only(tmp_path: Any) -> None:
    default_registry = FanoutRegistry()
    default_registry.rebase_default(tmp_path / "fanout")
    assert default_registry.directory == tmp_path / "fanout"
    # A second rebase is a no-op: the registry is no longer default-located.
    default_registry.rebase_default(tmp_path / "other")
    assert default_registry.directory == tmp_path / "fanout"

    explicit = FanoutRegistry(tmp_path / "explicit")
    explicit.rebase_default(tmp_path / "fanout")
    assert explicit.directory == tmp_path / "explicit"


def test_interview_handler_threads_state_dir_into_registry(tmp_path: Any) -> None:
    handler = InterviewHandler(data_dir=tmp_path, fanout_registry=FanoutRegistry())
    registry = handler._resolved_fanout_registry()
    assert registry is not None
    assert registry.directory == tmp_path / "fanout"


# --------------------------------------------------------------------------- #
# Handler-level: lateral producer registers + submit tool re-entry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lateral_handler_registers_fanout_and_submit_tool_synthesizes(
    tmp_path: Any,
) -> None:
    registry = FanoutRegistry(tmp_path)
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",  # -> SEQUENTIAL inline path
        fanout_registry=registry,
    )
    personas = ["researcher", "contrarian", "simplifier"]
    result = await handler.handle(
        {
            "problem_context": "stuck on a milestone question",
            "current_approach": "keep asking the same thing",
            "personas": personas,
        }
    )
    assert result.is_ok, result
    meta = result.unwrap().meta
    fanout_id = meta["fanout_id"]
    assert meta["host_action"] == "process_payloads_sequentially"

    submit = SubmitFanoutResultsHandler(fanout_registry=registry)
    submit_result = await submit.handle(
        {
            "correlation_key": "context.persona",
            "fanout_id": fanout_id,
            "results": [{"key": p, "content": f"{p}-out"} for p in personas],
        }
    )
    assert submit_result.is_ok, submit_result
    out = submit_result.unwrap().meta
    assert out["status"] == "complete"
    assert out["result"]["ready_for_synthesis"] is True


@pytest.mark.asyncio
async def test_lateral_handler_without_registry_stamps_no_fanout_id() -> None:
    handler = LateralThinkHandler(agent_runtime_backend="gemini")
    result = await handler.handle(
        {
            "problem_context": "stuck",
            "current_approach": "same",
            "personas": ["researcher", "contrarian"],
        }
    )
    assert result.is_ok, result
    assert "fanout_id" not in result.unwrap().meta


@pytest.mark.asyncio
async def test_submit_tool_requires_fanout_id() -> None:
    submit = SubmitFanoutResultsHandler()
    result = await submit.handle({"results": []})
    assert result.is_err


@pytest.mark.asyncio
async def test_submit_tool_bounds_input_size(tmp_path: Any) -> None:
    """Re-entry input is bounded before validation or persistence.

    Bot-review round-5 probe (PR #1703): two 200 KB results produced an
    804 KB terminal file; repeated submissions could exhaust memory or disk.
    """
    submit = SubmitFanoutResultsHandler(fanout_registry=FanoutRegistry(tmp_path))

    too_many = await submit.handle(
        {
            "fanout_id": "fanout_bounds",
            "results": [{"key": f"k{i}", "content": "x"} for i in range(33)],
        }
    )
    assert too_many.is_err

    too_big = await submit.handle(
        {
            "fanout_id": "fanout_bounds",
            "results": [{"key": "a", "content": "y" * 300_000}],
        }
    )
    assert too_big.is_err

    # Round-6 probe: non-dict items count against the caps too — 33 strings
    # (330 KB) previously bypassed both limits by being filtered out first.
    non_dict_flood = await submit.handle(
        {
            "fanout_id": "fanout_bounds",
            "results": ["y" * 10_000 for _ in range(33)],
        }
    )
    assert non_dict_flood.is_err

    non_dict_big = await submit.handle(
        {
            "fanout_id": "fanout_bounds",
            "results": ["y" * 300_000],
        }
    )
    assert non_dict_big.is_err


def test_known_data_tools_env_reaches_the_data_lane(monkeypatch: Any) -> None:
    """OUROBOROS_KNOWN_DATA_TOOLS is the public source for known_data_tools.

    Round-5 suggestion: previously only manually constructed lane metadata
    could exercise the contract field's prompt/context propagation.
    """
    monkeypatch.setenv("OUROBOROS_KNOWN_DATA_TOOLS", "clickhouse_query, metabase_card")
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-known-tools",
        question="Which plan tier do most active users hit?",
        phase="answer",
        score=None,
        dispatch_mode=SubagentDispatchMode.HOST_DRIVEN,
        runtime_backend="codex",
    )
    lanes = {lane["lane_id"]: lane for lane in meta["question_advisory_request"]["lanes"]}
    assert lanes["data_context"]["known_data_tools"] == ["clickhouse_query", "metabase_card"]
