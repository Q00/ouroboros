"""Generic interview fan-out core + ``ouroboros_submit_fanout_results`` re-entry.

Covers PR-J:
- ``build_fanout_subagents`` generic builder,
- ``stamp_fanout_meta`` 3-mode stamping (the cue is host-only; the
  correlation key is written on all three),
- ``FanoutRegistry`` persist/load,
- ``submit_fanout_results`` routing (complete / partial / unknown / mismatch),
- end-to-end producer -> registry -> submit for both revived synthesizer kinds.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.mcp.tools import fanout as fanout_module
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _attach_question_assist_requests,
)
from ouroboros.mcp.tools.evaluation_handlers import (
    FetchArtifactHandler,
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
    build_subagent_payload,
    register_code_investigation_fanout,
    register_lateral_persona_fanout,
    stamp_fanout_meta,
    submit_fanout_results,
)
from ouroboros.orchestrator.capabilities import (
    stable_code_investigation_question_identity,
)
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import ArtifactStore


def _bounded_submit(
    registry: FanoutRegistry,
    project_dir: Any,
) -> tuple[SubmitFanoutResultsHandler, DisposableMemory]:
    disposable = DisposableMemory(artifact_store=ArtifactStore.for_project(project_dir))
    return (
        SubmitFanoutResultsHandler(
            fanout_registry=registry,
            disposable_memory=disposable,
        ),
        disposable,
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
# stamp_fanout_meta (3-mode contract)
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


def test_stamp_fanout_meta_plugin_passive_stamps_only_the_correlation_key() -> None:
    """No host-action cue there, but the submission still has to be keyed.

    This asserted an empty dict, which read as "the bridge transport needs
    nothing from this stamp". It needed one thing: the bridge lifts
    ``result_correlation_key`` by name with no fallback, so omitting it did not
    leave re-entry undegraded — it left every submission answering
    ``correlation_mismatch``. The cue is what is host-only, not the key.
    """
    meta: dict[str, Any] = {}
    stamp_fanout_meta(
        meta,
        prefix="question_advisory",
        dispatch_mode=SubagentDispatchMode.PLUGIN_PASSIVE,
        payloads=_payloads(),
        correlation_key="context.lane_id",
    )
    assert meta == {"question_advisory_result_correlation_key": "context.lane_id"}


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
    """No registry -> the pre-registry contract, on the two modes built here.

    Scoped deliberately. ``PLUGIN_PASSIVE`` is the mode that stopped being
    byte-identical — it gained the correlation key it had always needed — and
    it is the one this test does not construct, which is why an unqualified
    claim here would have outlived its own coverage.
    """
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


def test_an_omitted_envelope_field_does_not_redeem_a_bound_fanout(tmp_path: Any) -> None:
    """An absent value is a mismatch, not a waiver.

    The MCP handler turns a missing `session_id` / `correlation_key` argument
    into `""`, so a caller that left it out used to skip these checks entirely
    and redeem a fan-out registered under someone else's session. That matters
    more than it reads: contracted lane answers carry no session of their own
    *because* this envelope settles it, so the guarantee they lean on has to
    hold for a caller who asserts nothing (Q00/ouroboros#1754).
    """
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
    results = [{"key": "researcher", "content": "x"}]

    def submit(session_id: str, correlation_key: str) -> dict[str, Any]:
        return submit_fanout_results(
            registry,
            session_id=session_id,
            correlation_key=correlation_key,
            results=results,
            fanout_id=fanout_id,
        )

    # The same submission under its own envelope still completes: the checks bind
    # the owner, they do not make the envelope harder to satisfy correctly.
    assert submit("s1", "context.persona")["status"] == "complete"
    assert submit("", "context.persona")["status"] == "correlation_mismatch"
    assert submit("s2", "context.persona")["status"] == "correlation_mismatch"
    assert submit("s1", "")["status"] == "correlation_mismatch"
    assert submit("s1", "code_facts")["status"] == "correlation_mismatch"


def test_a_record_that_bound_nothing_has_nothing_to_demand(tmp_path: Any) -> None:
    """A producer that ran without a session keeps today's behavior.

    The record decides what must be proven. Demanding a session the producer
    never recorded would reject correct submissions to prove a binding that was
    never made — over-blocking in the name of a guarantee.
    """
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
    fanout_id = register_lateral_persona_fanout(registry, session_id="", payloads=payloads)

    out = submit_fanout_results(
        registry,
        session_id="",
        correlation_key="context.persona",
        results=[{"key": "researcher", "content": "x"}],
        fanout_id=fanout_id,
    )

    assert out["status"] == "complete"


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
) -> tuple[str, str, list[str], dict[str, Any]]:
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
    return fanout_id, correlation_key, lane_keys, meta


def _advisory_lane_outputs(meta: Mapping[str, Any], lane_keys: list[str]) -> dict[str, Any]:
    """Return one contract-satisfying output per emitted lane.

    Only ``data_context`` carries an answer contract, and it is satisfied here
    with its no-op answer — the response a child gives when the question's
    honest answer is not a measurement. Every other lane completes on the
    generic advisory shape, so a plain string stands in for its advice.
    """
    identity = ""
    for payload in meta["question_advisory_subagents"]:
        context = payload.get("context") or {}
        if context.get("lane_id") == "data_context":
            identity = str(context.get("question_identity") or "")
    outputs: dict[str, Any] = {key: f"{key}-advice" for key in lane_keys}
    if "data_context" in outputs:
        outputs["data_context"] = {
            "question_identity": identity,
            "lane_id": "data_context",
            "data_needed": False,
            "read_requests": [],
            "no_evidence_reason": "not_a_measurement",
        }
    return outputs


def _required_advisory_lanes() -> list[str]:
    """Return the lane ids whose absence must block advisory completion."""
    from ouroboros.orchestrator.capabilities.interview_schemas import (
        _interview_question_advisory_fanout_metadata,
    )

    return [
        str(lane["lane_id"])
        for lane in _interview_question_advisory_fanout_metadata()["lanes"]
        if lane.get("required")
    ]


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
    fanout_id, correlation_key, lane_keys, meta = _emitted_advisory_contract(registry, session_id)
    outputs = _advisory_lane_outputs(meta, lane_keys)

    submit, disposable = _bounded_submit(registry, tmp_path)
    submit_result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": correlation_key,
            "results": [{"key": key, "content": outputs[key]} for key in lane_keys],
        }
    )
    assert submit_result.is_ok, submit_result
    envelope = submit_result.unwrap().meta
    contract_id = envelope["contract_id"]
    # The colon-carrying fanout id is stored and fetched verbatim below; the
    # store has no filesystem layout left for the id's characters to violate.
    assert contract_id.startswith("fanout:")
    digest_component = contract_id.removeprefix("fanout:")
    assert len(digest_component) == 64
    assert set(digest_component) <= set("0123456789abcdef")
    out = disposable.fetch(envelope["contract_id"]).body
    assert out["status"] == "complete"
    assert out["kind"] == FANOUT_KIND_QUESTION_ADVISORY
    assert out["correlation_key"] == correlation_key
    assert out["contract_violations"] == {}
    aggregated = out["result"]["aggregated_outputs"]
    assert [item["lane_id"] for item in aggregated] == lane_keys
    assert [item["output"] for item in aggregated] == [outputs[key] for key in lane_keys]


@pytest.mark.asyncio
async def test_advisory_reentry_partial_set_lists_missing_required_lane_ids(
    tmp_path: Any,
) -> None:
    """A subset submission reports the REQUIRED lanes still outstanding.

    Optional lanes are not listed as missing here: their absence does not block
    completion, so naming them would tell the host to chase output it was never
    obliged to produce.
    """
    registry = FanoutRegistry(tmp_path)
    session_id = "sess-advisory-partial"
    fanout_id, correlation_key, lane_keys, _meta = _emitted_advisory_contract(registry, session_id)
    assert len(lane_keys) > 1, "partial-set case needs multiple advisory lanes"
    optional_first = next(key for key in lane_keys if key not in _required_advisory_lanes())

    submit = SubmitFanoutResultsHandler(fanout_registry=registry)
    submit_result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": correlation_key,
            "results": [{"key": optional_first, "content": f"{optional_first}-advice"}],
        }
    )
    assert submit_result.is_ok, submit_result
    out = submit_result.unwrap().meta
    assert out["status"] == "partial"
    assert out["missing_required_keys"] == _required_advisory_lanes()
    assert out["missing_keys"] == out["missing_required_keys"]
    assert out["received_keys"] == [optional_first]


@pytest.mark.asyncio
async def test_a_completed_submission_is_the_only_reply_carrying_a_contract_id(
    tmp_path: Any,
) -> None:
    """Regression (#1941): what the skills read to tell success apart.

    This tool answers in two shapes. An incomplete submission answers with a
    ``status`` and the fields it implies; a complete one answers with the
    disposable-memory envelope, which has no ``status`` of its own -- its
    ``result.status`` is that subsystem's word for its own run, and the envelope
    is ``extra="forbid"``. So the tool cannot add a discriminator without making
    the reply stop being that model.

    ``contract_id`` is the discriminator it already has: only the completed
    reply carries one. The PM and interview skills both read it, and the PM
    skill previously read a top-level ``status`` instead -- which meant it
    resubmitted every successful fan-out and then discarded the evidence it had
    just been told was valid. Asserted across the branches together rather than
    one by one, because what broke was not a branch but their agreement.
    """
    registry = FanoutRegistry(tmp_path)
    session_id = "sess-discriminator"
    fanout_id, correlation_key, lane_keys, meta = _emitted_advisory_contract(registry, session_id)
    outputs = _advisory_lane_outputs(meta, lane_keys)
    submit, _disposable = _bounded_submit(registry, tmp_path)

    async def reply(results: list[dict[str, Any]]) -> dict[str, Any]:
        result = await submit.handle(
            {
                "session_id": session_id,
                "fanout_id": fanout_id,
                "correlation_key": correlation_key,
                "results": results,
            }
        )
        assert result.is_ok, result
        return result.unwrap().meta

    optional_first = next(key for key in lane_keys if key not in _required_advisory_lanes())
    partial = await reply([{"key": optional_first, "content": f"{optional_first}-advice"}])
    invalid = await reply([{"key": lane_keys[0]}])
    complete = await reply([{"key": key, "content": outputs[key]} for key in lane_keys])

    # What the skills key on: present on the completed reply, absent everywhere
    # else. Both directions, so neither drifts without this failing.
    assert complete["contract_id"].startswith("fanout:")
    for name, out in (("partial", partial), ("invalid", invalid)):
        assert "contract_id" not in out, f"{name} reply carries a contract_id: {sorted(out)}"

    # And the incomplete replies keep saying why, which is the other half of
    # what the skills act on.
    assert partial["status"] == "partial"
    assert partial["missing_required_keys"]
    assert invalid["status"] == "invalid_result_entry"
    assert "status" not in complete

    # The skills are the consumers, so the same key is asserted against what
    # they tell a host to do. Both mirrors, because a rule that holds in one of
    # them is not the rule -- it is a copy of it.
    #
    # What is asserted here is an instruction, not a claim about the runtime.
    # The previous version of this test pinned the sentence "a contract_id means
    # every required lane passed its contract", which is false -- a required lane
    # submitted as ``undispatched`` is excused from the completeness test and the
    # submission still completes. Pinning a claim keeps the claim, true or not;
    # only the runtime can say what a reply means, and it says it below.
    repo_root = Path(__file__).resolve().parents[4]
    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        skill = (root / "pm" / "SKILL.md").read_text(encoding="utf-8")
        assert "With a `contract_id`, synthesize" in skill, root
        assert "leave out a lane you submitted as\n`undispatched`" in skill, root
        assert "dispatch_subagents_if_supported" in skill, root
        assert "process_payloads_sequentially" in skill, root
        assert "host action selects the execution strategy" in skill, root


@pytest.mark.asyncio
async def test_a_completed_reply_does_not_mean_every_required_lane_ran(tmp_path: Any) -> None:
    """Regression (#1941): what a `contract_id` does not promise.

    A required lane declared ``undispatched`` is excused from the completeness
    test (``prepare_fanout_results``), so the submission completes and an
    artifact is published with that lane never having run. That is deliberate --
    #1754 put it there because pinning the fan-out at ``partial`` for good leaves
    inventing the missing output as the cheapest way for a host to finish, and in
    an evidence lane an invented output is fabricated grounds in front of a user.

    It is pinned here because a skill once read the completed reply as proof that
    every required lane had passed. The reply cannot carry that meaning, and the
    host is the only party that knows which lanes it excused -- so the skills
    subtract their own ``undispatched`` set rather than reading a promise off the
    reply. If this ever stops completing, the instruction those skills carry is
    stricter than it needs to be and should be revisited with it.
    """
    registry = FanoutRegistry(tmp_path)
    session_id = "sess-undispatched-required"
    fanout_id, correlation_key, lane_keys, meta = _emitted_advisory_contract(registry, session_id)
    outputs = _advisory_lane_outputs(meta, lane_keys)
    excused = _required_advisory_lanes()[0]
    submit, disposable = _bounded_submit(registry, tmp_path)

    result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": correlation_key,
            "results": [
                {"key": key, "content": outputs[key]} for key in lane_keys if key != excused
            ]
            + [{"key": excused, "undispatched": True}],
        }
    )

    assert result.is_ok, result
    reply = result.unwrap().meta
    assert reply["contract_id"].startswith("fanout:")

    # The reply that means "accepted" is the same shape either way; only the
    # body records which lane was excused, and the host never reads the body.
    body = disposable.fetch(reply["contract_id"]).body
    assert body["undispatched_keys"] == [excused]
    assert excused not in {item["lane_id"] for item in body["result"]["aggregated_outputs"]}


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


def test_registry_never_moves_out_from_under_an_issued_fanout_id(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An issued fan-out id must stay redeemable.

    Reachable ordering: a lateral panel registers before the first interview
    turn, and the interview then resolves a custom ``state_dir`` and re-roots
    the SHARED registry. The record stays at the old path while lookups move to
    the new one, so a valid submission comes back ``unknown_fanout_id`` — the
    id was a promise the move quietly broke.
    """
    # The default location is the whole subject here, so it is redirected
    # rather than used: a test must never write into the developer's own
    # ``~/.ouroboros``.
    monkeypatch.setattr(fanout_module, "_DEFAULT_FANOUT_DIR", tmp_path / "home-default")
    registry = FanoutRegistry()
    fanout_id = registry.register(
        kind=FANOUT_KIND_LATERAL_PERSONA_PANEL,
        session_id="sess-lateral-first",
        correlation_key="context.persona",
        expected_keys=["researcher"],
        synthesizer_input={"entries": []},
    )
    issued_dir = registry.directory
    assert registry.load(fanout_id) is not None

    registry.rebase_default(tmp_path / "fanout")

    assert registry.directory == issued_dir
    assert registry.load(fanout_id) is not None


def test_interview_handler_threads_state_dir_into_registry(tmp_path: Any) -> None:
    handler = InterviewHandler(data_dir=tmp_path, fanout_registry=FanoutRegistry())
    registry = handler._resolved_fanout_registry()
    assert registry is not None
    assert registry.directory == tmp_path / "fanout"


# --------------------------------------------------------------------------- #
# Handler-level: lateral producer registers + submit tool re-entry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lateral_and_submit_emit_privacy_safe_dispatch_telemetry(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_subagent_dispatch",
        lambda properties: captured.append(properties),
    )
    registry = FanoutRegistry(tmp_path)
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        fanout_registry=registry,
    )
    produced = await handler.handle(
        {
            "problem_context": "stuck",
            "current_approach": "same",
            "personas": ["researcher", "contrarian"],
        }
    )
    fanout_id = produced.unwrap().meta["fanout_id"]
    submit, _ = _bounded_submit(registry, tmp_path)
    submitted = await submit.handle(
        {
            "correlation_key": "context.persona",
            "fanout_id": fanout_id,
            "results": [
                {"key": "researcher", "content": "r"},
                {"key": "contrarian", "undispatched": True},
            ],
        }
    )

    assert submitted.is_ok
    assert captured[0] == {
        "phase": "emitted",
        "fanout_kind": "lateral_persona_panel",
        "payload_count": 2,
        "invocation_surface": "internal_runtime",
        "dispatch_authority": "internal_runtime",
        "host_family": "unknown",
        "host_identity_status": "unknown",
        "host_capability": "undeclared",
        "capability_source": "none",
        "delivery_mode": "inline_runtime",
        "execution_preference": "sequential",
        "fallback_strategy": "sequential",
        "configured_worker_backend": "gemini",
        "host_worker_mismatch": False,
        "decision_reason": "configured_internal_runtime",
        "contract_version": "v2",
        "fanout_reentry_available": True,
    }
    assert captured[1] == {
        "phase": "submitted",
        "fanout_kind": "lateral_persona_panel",
        "submission_status": "complete",
        "expected_count": 2,
        "received_count": 1,
        "undispatched_count": 1,
        "contract_version": "v2",
    }


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

    submit, disposable = _bounded_submit(registry, tmp_path)
    submit_result = await submit.handle(
        {
            "correlation_key": "context.persona",
            "fanout_id": fanout_id,
            "results": [{"key": p, "content": f"{p}-out"} for p in personas],
        }
    )
    assert submit_result.is_ok, submit_result
    envelope = submit_result.unwrap().meta
    out = disposable.fetch(envelope["contract_id"]).body
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
async def test_submit_tool_omitting_the_envelope_does_not_redeem_a_bound_fanout(
    tmp_path: Any,
) -> None:
    """The public path is where omission is cheapest, so it is pinned there too.

    `SubmitFanoutResultsHandler` declares both envelope arguments optional and
    converts an omission to `""`. A host that sends only `fanout_id` and results
    must not redeem a record that bound a session — the core check above is the
    same one, but only this test covers the arguments a real host actually omits.
    """
    registry = FanoutRegistry(tmp_path)
    handler = LateralThinkHandler(agent_runtime_backend="gemini", fanout_registry=registry)
    personas = ["researcher", "contrarian"]
    produced = await handler.handle(
        {
            "problem_context": "stuck on a milestone question",
            "current_approach": "keep asking the same thing",
            "personas": personas,
            "session_id": "sess-envelope",
        }
    )
    assert produced.is_ok, produced
    fanout_id = produced.unwrap().meta["fanout_id"]
    results = [{"key": persona, "content": f"{persona}-out"} for persona in personas]

    submit, disposable = _bounded_submit(registry, tmp_path)
    omitted = await submit.handle({"fanout_id": fanout_id, "results": results})

    assert omitted.is_ok, omitted
    assert omitted.unwrap().meta["status"] == "correlation_mismatch"

    honored = await submit.handle(
        {
            "session_id": "sess-envelope",
            "correlation_key": "context.persona",
            "fanout_id": fanout_id,
            "results": results,
        }
    )

    assert honored.is_ok, honored
    envelope = honored.unwrap().meta
    assert disposable.fetch(envelope["contract_id"]).body["status"] == "complete"


def test_an_id_that_is_not_a_registry_filename_redeems_nothing(tmp_path: Any) -> None:
    """The id is a filename, so a path is what it must not be able to spell.

    `Path(directory) / "/tmp/forged.json"` is `/tmp/forged.json` — the join
    silently discards the directory — so an absolute id turned a caller-chosen
    file into an authoritative persisted record. The alphabet is what closes it:
    a separator, a parent segment and a drive letter are unspellable, so this is
    not detection over an open space (Q00/ouroboros#1754).
    """
    outside = tmp_path / "forged.json"
    outside.write_text(
        json.dumps(
            {
                "fanout_id": "forged",
                "kind": FANOUT_KIND_LATERAL_PERSONA_PANEL,
                "session_id": "",
                "correlation_key": "context.persona",
                "expected_keys": ["researcher"],
                "synthesizer_input": {"entries": []},
            }
        ),
        encoding="utf-8",
    )
    registry = FanoutRegistry(tmp_path / "fanout")

    forged_ids = [
        str(tmp_path / "forged"),  # absolute
        "../forged",  # traversal
        "..",
        "sub/forged",  # separator
        "",
    ]

    for forged in forged_ids:
        assert registry.load(forged) is None, forged
        out = submit_fanout_results(
            registry,
            session_id="",
            correlation_key="context.persona",
            results=[{"key": "researcher", "content": "x"}],
            fanout_id=forged,
        )
        assert out["status"] == "unknown_fanout_id", forged


def test_a_producer_cannot_issue_an_id_it_could_never_redeem(tmp_path: Any) -> None:
    """Refused where it was written, not where it fails to load."""
    registry = FanoutRegistry(tmp_path)

    with pytest.raises(ValueError, match="fanout_id"):
        register_lateral_persona_fanout(
            registry,
            session_id="s1",
            payloads=[
                build_subagent_payload(
                    tool_name="ouroboros_lateral_think",
                    title="L (researcher)",
                    prompt="x",
                    agent="researcher",
                    context={"persona": "researcher"},
                )
            ],
            fanout_id="../escape",
        )


@pytest.mark.asyncio
async def test_submit_tool_rejects_forged_ids_and_still_redeems_issued_ones(
    tmp_path: Any,
) -> None:
    """Through the public tool, which is what made the id caller-controlled.

    Registering the re-entry tool on the shipped server is what turned
    `fanout_id` into hostile input; before that it never crossed a transport.
    So the boundary is pinned where a real caller reaches it.
    """
    state_dir = tmp_path / "state"
    registry = FanoutRegistry(state_dir / "fanout")
    handler = LateralThinkHandler(agent_runtime_backend="gemini", fanout_registry=registry)
    personas = ["researcher", "contrarian"]
    produced = await handler.handle(
        {
            "problem_context": "stuck",
            "current_approach": "same",
            "personas": personas,
            "session_id": "sess-forge",
        }
    )
    assert produced.is_ok, produced
    issued_id = produced.unwrap().meta["fanout_id"]

    outside = tmp_path / "forged.json"
    outside.write_text((state_dir / "fanout" / f"{issued_id}.json").read_text(), encoding="utf-8")

    submit, disposable = _bounded_submit(registry, tmp_path)
    results = [{"key": persona, "content": f"{persona}-out"} for persona in personas]

    for forged in (str(tmp_path / "forged"), "../forged", "sub/forged"):
        refused = await submit.handle(
            {
                "session_id": "sess-forge",
                "correlation_key": "context.persona",
                "fanout_id": forged,
                "results": results,
            }
        )
        assert refused.is_err, forged

    honored = await submit.handle(
        {
            "session_id": "sess-forge",
            "correlation_key": "context.persona",
            "fanout_id": issued_id,
            "results": results,
        }
    )

    assert honored.is_ok, honored
    envelope = honored.unwrap().meta
    assert disposable.fetch(envelope["contract_id"]).body["status"] == "complete"


@pytest.mark.asyncio
async def test_submit_tool_partial_retry_is_judged_on_the_whole_set(tmp_path: Any) -> None:
    """A retry carries every lane, and the docs say so in the same words.

    `provided` is built from the current call alone; nothing an earlier call
    carried is kept, because keeping it would put child-authored output in the
    record — the durable result state RFC #1754 defers to a later slice with its
    sanitization duties. The failure this pins is not the statelessness but the
    instruction that contradicted it: a partial reply that reads as "send the
    rest" traps a sequential host in a loop that never completes.
    """
    registry = FanoutRegistry(tmp_path)
    handler = LateralThinkHandler(agent_runtime_backend="gemini", fanout_registry=registry)
    personas = ["researcher", "contrarian"]
    produced = await handler.handle(
        {
            "problem_context": "stuck",
            "current_approach": "same",
            "personas": personas,
            "session_id": "sess-partial",
        }
    )
    assert produced.is_ok, produced
    fanout_id = produced.unwrap().meta["fanout_id"]
    submit, disposable = _bounded_submit(registry, tmp_path)

    async def send(*keys: str) -> dict[str, Any]:
        result = await submit.handle(
            {
                "session_id": "sess-partial",
                "correlation_key": "context.persona",
                "fanout_id": fanout_id,
                "results": [{"key": key, "content": f"{key}-out"} for key in keys],
            }
        )
        assert result.is_ok, result
        outcome = dict(result.unwrap().meta)
        if "contract_id" in outcome:
            return dict(disposable.fetch(outcome["contract_id"]).body)
        return outcome

    first = await send("researcher")
    assert first["status"] == "partial"
    assert first["missing_keys"] == ["contrarian"]
    # The reply is the one place a host is certainly reading, so it carries the
    # contract rather than leaving a list of missing keys to be read as a list
    # of what to send.
    assert "not only the missing ones" in first["retry_contract"]

    # The remaining-lanes-only retry the old wording invited: still partial, and
    # now the lane the first call carried is the one reported missing.
    remaining_only = await send("contrarian")
    assert remaining_only["status"] == "partial"
    assert remaining_only["missing_keys"] == ["researcher"]

    whole = await send(*personas)
    assert whole["status"] == "complete"
    assert whole["result"]["ready_for_synthesis"] is True
    # Both lanes reach synthesis from this one call — the retry is what carried
    # them, not anything the registry remembered.
    rendered = repr(whole["result"])
    assert all(f"{persona}-out" in rendered for persona in personas)


@pytest.mark.asyncio
async def test_submit_tool_requires_fanout_id() -> None:
    submit = SubmitFanoutResultsHandler()
    result = await submit.handle({"results": []})
    assert result.is_err


@pytest.mark.asyncio
async def test_fetch_tool_without_project_artifact_service_fails_closed() -> None:
    result = await FetchArtifactHandler().handle({"contract_id": "fanout:missing"})

    assert result.is_err
    assert "requires a configured project artifact service" in str(result.error)


@pytest.mark.asyncio
async def test_terminal_submit_without_disposable_service_fails_closed(
    tmp_path: Any,
) -> None:
    registry = FanoutRegistry(tmp_path)
    fanout_id = _advisory_fanout(registry)
    marker = "must-not-return-inline-" * 512

    result = await SubmitFanoutResultsHandler(fanout_registry=registry).handle(
        {
            "session_id": "s1",
            "correlation_key": "context.lane_id",
            "fanout_id": fanout_id,
            "results": [
                {"key": "data_context", "content": marker},
                {"key": "code_context", "content": marker},
            ],
        }
    )

    assert result.is_err
    assert "requires a configured disposable artifact service" in str(result.error)
    assert marker not in repr(result)
    assert len(repr(result).encode()) < 4 * 1024


def _advisory_fanout(registry: FanoutRegistry) -> str:
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="s1",
        correlation_key="context.lane_id",
        expected_keys=["data_context", "code_context"],
        question_identity="",
        synthesizer_input={"lane_ids": ["data_context", "code_context"]},
        required_keys=[],
    )
    assert fanout_id is not None
    return fanout_id


@pytest.mark.parametrize(
    ("label", "repeated"),
    [
        (
            "two measurements for one lane",
            [
                {"key": "data_context", "content": {"value": 41}},
                {"key": "data_context", "content": {"value": 999}},
            ],
        ),
        (
            "answered, then declared undispatched",
            [
                {"key": "data_context", "content": {"value": 41}},
                {"key": "data_context", "undispatched": True},
            ],
        ),
        (
            "declared undispatched, then answered",
            [
                {"key": "data_context", "undispatched": True},
                {"key": "data_context", "content": {"value": 41}},
            ],
        ),
    ],
)
def test_a_repeated_correlation_key_is_refused_rather_than_ordered(
    tmp_path: Any, label: str, repeated: list[Any]
) -> None:
    """One lane, one entry — nothing here may pick between two reports.

    Two measurements for one lane went into a plain dict, so the later entry
    won and the earlier vanished unreported: list position chose the number.
    Answered *and* declared undispatched behaved differently — content won
    whichever order it arrived in — which was deterministic but undeclared, a
    precedence rule no contract states applied to a host that has said two
    opposite things about one lane.

    Both are refused now, because neither is a report this can rank.
    """
    registry = FanoutRegistry(tmp_path)
    out = submit_fanout_results(
        registry,
        fanout_id=_advisory_fanout(registry),
        session_id="s1",
        correlation_key="context.lane_id",
        results=[*repeated, {"key": "code_context", "content": {"value": 7}}],
    )

    assert out["status"] == "invalid_result_entry", label
    assert out["invalid_keys"] == ["data_context"], label


def test_distinct_correlation_keys_still_complete(tmp_path: Any) -> None:
    """The control: refusing repeats must not refuse an ordinary submission."""
    registry = FanoutRegistry(tmp_path)
    out = submit_fanout_results(
        registry,
        fanout_id=_advisory_fanout(registry),
        session_id="s1",
        correlation_key="context.lane_id",
        results=[
            {"key": "data_context", "content": {"value": 41}},
            {"key": "code_context", "content": {"value": 7}},
        ],
    )

    assert out["status"] == "complete"
