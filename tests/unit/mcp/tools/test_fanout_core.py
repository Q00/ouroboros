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
