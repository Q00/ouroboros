"""Recent findings reach a lane, and the boundary is recency (RFC #2153).

The decision these hold is that a lane may answer from a finding another lane
produced recently, whichever session produced it — so what is asserted here is
mostly the *absence* of a session, and absences have no failing behaviour to
point at later if a guard is quietly restored.

What travels is where a finding is, not what it says: a count, a contract id
and a publication time, which a lane fetches for itself. So these read the
prompt for the id and assert the body is *not* in it -- carried inline, the same
block was copied into every lane of the turn and the response outgrew what a
host takes inline, which cost the turn its fan-out entirely.

A lane is offered only what its own lane produced (RFC #2167), so the lanes that
produce nothing reusable are asserted to receive no block at all.

Every fixture publishes **through the store**, because that is the change these
tests exist to protect. A fixture writing bodies into the directory by hand
would assert against the shape this feature deliberately stopped trusting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.question_advisory import (
    attach_question_advisory,
    build_question_advisory_subagents,
)
from ouroboros.mcp.tools.recent_findings import (
    RECENT_FINDINGS_WINDOW,
    recent_findings_by_lane,
)
from ouroboros.orchestrator.capabilities.pm_schemas import pm_repository_roster
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_errors import ArtifactNotFoundError
from ouroboros.persistence.artifact_store import ArtifactStore

QUESTION = "What happens today when a subscription lapses mid-period?"


@pytest.fixture
def roster() -> list[dict[str, str]]:
    return pm_repository_roster([{"path": "/repo/api", "name": "api"}])


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    built = ArtifactStore.for_project(workspace)
    built.initialize()
    return built


def _publish(
    store: ArtifactStore,
    *,
    kind: str = "question_advisory",
    lanes: tuple[str, ...] = ("code_context", "data_context"),
    claim: str = "access continues to period end",
) -> str:
    """Publish one body the way a completed fan-out does, and return its contract.

    Through ``DisposableMemory`` rather than by writing a file, so what these
    tests read back is a publication the store recorded making — which is the
    thing the lookup asks about.
    """
    body = {
        "kind": kind,
        "result": {
            "aggregated_outputs": [{"lane_id": lane, "output": {"claim": claim}} for lane in lanes]
        },
    }
    canonical = json.dumps(body, sort_keys=True).encode("utf-8")
    return _publish_body(store, body, contract_id=f"fanout:{hashlib.sha256(canonical).hexdigest()}")


def _publish_body(store: ArtifactStore, body: Any, *, contract_id: str) -> str:
    """Publish one body verbatim, for shapes a fan-out would not produce."""
    memory = DisposableMemory(artifact_store=store)

    async def _run() -> Any:
        async def work(_handle: Any) -> Any:
            return body

        return await memory.run(
            intent="publish a finding",
            runtime_id="test:publish",
            work_fn=work,
            contract_id=contract_id,
        )

    asyncio.run(_run())
    return contract_id


def _lane_prompts(meta: dict[str, Any]) -> dict[str, str]:
    return {
        payload.context["lane_id"]: payload.to_dict()["prompt"]
        for payload in build_question_advisory_subagents(meta["question_advisory_request"])
    }


def _attach(
    roster: list[dict[str, str]],
    store: ArtifactStore | None,
    *,
    tool_name: str = "ouroboros_pm_interview",
    **kwargs: Any,
) -> dict[str, Any]:
    pm = tool_name == "ouroboros_pm_interview"
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name=tool_name,
        session_id=kwargs.pop("session_id", "pm-1"),
        question=kwargs.pop("question", QUESTION),
        repository_roster=roster if pm else None,
        code_investigation_request=None
        if pm
        else {"question": QUESTION, "reason": "policy", "repository_path": "/repo/api"},
        findings_store=store,
        **kwargs,
    )
    return meta


def test_a_lane_is_told_where_its_findings_are_and_not_what_they_say(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """The id travels; the body stays in the store.

    Inlining the bodies is what broke this: the same block was copied into every
    lane of the turn, the tool result outgrew what a host accepts inline, and the
    host spent the turn parsing its own output instead of dispatching the lanes.
    """
    claim = "a sentence only a child could have written"
    contract_id = _publish(store, claim=claim)

    prompt = _lane_prompts(_attach(roster, store, tool_name="ouroboros_interview"))["code_context"]

    assert "## Recently Found Here" in prompt
    assert contract_id in prompt
    assert "ouroboros_fetch_artifact" in prompt
    assert claim not in prompt


def test_only_the_lane_that_produced_a_finding_is_offered_it(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """RFC #2167: a lane that produces no fact that keeps consumes none either.

    The reasoning lanes never used a finding for anything, and handing one a code
    fact is a new capability rather than a cache hit — so they are offered none.
    """
    _publish(store, lanes=("code_context",))

    prompts = _lane_prompts(_attach(roster, store, tool_name="ouroboros_interview"))

    assert "## Recently Found Here" in prompts["code_context"]
    for lane_id, prompt in prompts.items():
        if lane_id != "code_context":
            assert "## Recently Found Here" not in prompt, lane_id


def test_a_contracted_lane_is_offered_its_findings_without_the_reporting_duty(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """Eligibility follows what a lane produces, not the shape of its answer (#2223).

    The offer was gated on answer shape once: a contracted lane had no field in
    which to confess a failed fetch, so it was offered nothing -- which withheld
    the head start from exactly the lanes doing the most repeated work (both PM
    lanes are contracted). The confession is what the shape decides, not the
    offer: a contracted answer carries no reuse statement a reader could be
    misled by, a failed fetch degrades to the investigation the lane would have
    run anyway, and the server sees the fetch calls either way. So the prose
    lane keeps the in-band duty, and the contracted lane is told to keep its
    shape instead.
    """
    _publish(store)

    interview_meta = _attach(roster, store, tool_name="ouroboros_interview")
    pm_meta = _attach(roster, store, tool_name="ouroboros_pm_interview")
    interview = _lane_prompts(interview_meta)
    pm = _lane_prompts(pm_meta)

    for prompt in (
        interview["code_context"],
        interview["data_context"],
        pm["code_context"],
        pm["data_context"],
    ):
        assert "## Recently Found Here" in prompt

    # The in-band duty exists only where the answer has a place for it.
    assert "reporting nothing to reuse would be false" in interview["code_context"]
    for prompt in (interview["data_context"], pm["code_context"], pm["data_context"]):
        assert "reporting nothing to reuse would be false" not in prompt
        assert "do not add fields about reuse" in prompt

    assert sorted(interview_meta["question_advisory_request"]["recent_findings"]) == [
        "code_context",
        "data_context",
    ]
    assert sorted(pm_meta["question_advisory_request"]["recent_findings"]) == [
        "code_context",
        "data_context",
    ]


def test_the_eligible_lanes_do_not_read_each_other(store: ArtifactStore) -> None:
    """Being reusable is not being interchangeable (RFC #2167)."""
    _publish(store, lanes=("data_context",))

    by_lane = recent_findings_by_lane(store)

    assert list(by_lane) == ["data_context"]


def test_a_lane_told_its_count_can_tell_a_failed_fetch_from_an_empty_project(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """The count is what keeps silence from standing in for absence.

    Fetching is a call the child may be unable to make, and the objection to
    handing over an id rather than a body was exactly that: a lane that cannot
    reach the tool looks like a project with nothing to reuse. Naming the number
    before naming the tool answers it — the lane knows something is there.
    """
    _publish(store, claim="first")
    _publish(store, claim="second")

    prompt = _lane_prompts(_attach(roster, store, tool_name="ouroboros_interview"))["code_context"]

    assert "You are offered 2 recent findings" in prompt
    assert "cannot reach that tool" in prompt


def test_a_finding_older_than_the_window_is_not_offered(store: ArtifactStore) -> None:
    """Recency is the boundary, so something has to fall outside it."""
    _publish(store)
    later = datetime.now(UTC) + RECENT_FINDINGS_WINDOW + timedelta(minutes=1)

    assert sorted(recent_findings_by_lane(store)) == ["code_context", "data_context"]
    assert recent_findings_by_lane(store, now=later) == {}


def test_a_caller_with_no_store_still_gets_its_lanes(roster: list[dict[str, str]]) -> None:
    """Advisory: losing the shortcut costs a child a place to look, not a turn."""
    prompts = _lane_prompts(_attach(roster, None))

    assert set(prompts) == {"code_context", "data_context"}
    for prompt in prompts.values():
        assert "## Recently Found Here" not in prompt


def test_an_unreadable_store_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    """The turn belongs to the question; a missing shortcut must not take it."""
    absent = ArtifactStore.for_project(tmp_path / "gone")

    assert recent_findings_by_lane(absent) == {}
    assert recent_findings_by_lane(None) == {}


def test_a_record_of_an_unexpected_shape_costs_only_itself(store: ArtifactStore) -> None:
    """Fail-open is per record, so one odd body cannot empty the whole window."""
    _publish_body(store, {"kind": "question_advisory", "result": []}, contract_id="fanout:broken")
    good = _publish(store)

    by_lane = recent_findings_by_lane(store)

    assert [entry["contract_id"] for entry in by_lane["code_context"]] == [good]
    assert [entry["lane_id"] for entry in by_lane["code_context"]] == ["code_context"]


def test_another_fanout_kind_is_not_offered(store: ArtifactStore) -> None:
    """Persona panels publish through the same store and are not this."""
    _publish(store, kind="lateral_persona_panel")

    assert recent_findings_by_lane(store) == {}


def test_a_publication_stamped_ahead_of_now_is_not_offered(store: ArtifactStore) -> None:
    """A record claiming not to have happened yet costs the shortcut, not the window."""
    _publish(store)
    ahead = datetime.now(UTC) - timedelta(days=30)

    assert recent_findings_by_lane(store, now=ahead) == {}


def test_a_request_carrying_findings_satisfies_the_advertised_schema(
    store: ArtifactStore,
) -> None:
    """The request schema is closed, so what the field carries must be in it."""
    from jsonschema import Draft202012Validator

    from ouroboros.orchestrator.capabilities.interview_schemas import (
        _interview_question_advisory_request_schema,
    )

    _publish(store)
    meta = _attach([], store, tool_name="ouroboros_interview", phase="start")
    request = meta["question_advisory_request"]

    assert request["recent_findings"], "the case being validated has to be the loaded one"
    errors = list(
        Draft202012Validator(_interview_question_advisory_request_schema()).iter_errors(request)
    )
    assert errors == [], [error.message for error in errors]


def _publish_distinguishable(store: ArtifactStore) -> str:
    """Publish one fan-out whose lanes are told apart by their own text."""
    body = {
        "kind": "question_advisory",
        "result": {
            "aggregated_outputs": [
                {"lane_id": "code_context", "output": {"finding": "the code says CODE-ONLY"}},
                {"lane_id": "data_context", "output": {"finding": "the rows say DATA-ONLY"}},
                {
                    "lane_id": "ambiguity_contrarian",
                    "output": {"finding": "the question asks CONTRARIAN-ONLY"},
                },
            ]
        },
    }
    canonical = json.dumps(body, sort_keys=True).encode("utf-8")
    return _publish_body(store, body, contract_id=f"fanout:{hashlib.sha256(canonical).hexdigest()}")


def test_fetching_an_offered_finding_returns_only_the_requesting_lane(
    store: ArtifactStore,
) -> None:
    """The lane_id offered beside a contract is what narrows the body to one lane.

    A fan-out publishes one artifact carrying every lane it dispatched, so its
    contract id names the turn: fetching it handed a lane every sibling's output
    while the prompt said the body was its own.
    """
    _publish_distinguishable(store)

    entry = recent_findings_by_lane(store)["code_context"][0]
    fetched = store.fetch_lane(entry["contract_id"], entry["lane_id"]).body

    assert fetched == {"finding": "the code says CODE-ONLY"}
    assert "DATA-ONLY" not in json.dumps(fetched)
    assert "CONTRARIAN-ONLY" not in json.dumps(fetched)


def test_each_lane_reads_its_own_output_of_the_same_fan_out(store: ArtifactStore) -> None:
    """One contract, two lanes, two bodies -- the contract id is shared, the lane is not."""
    _publish_distinguishable(store)

    offered = recent_findings_by_lane(store)
    code, data = offered["code_context"][0], offered["data_context"][0]

    assert code["contract_id"] == data["contract_id"]
    assert (code["lane_id"], data["lane_id"]) == ("code_context", "data_context")
    assert store.fetch_lane(code["contract_id"], code["lane_id"]).body == {
        "finding": "the code says CODE-ONLY"
    }
    assert store.fetch_lane(data["contract_id"], data["lane_id"]).body == {
        "finding": "the rows say DATA-ONLY"
    }


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("advice written as plain prose", id="string"),
        pytest.param(42, id="int"),
        pytest.param(3.5, id="float"),
        pytest.param(True, id="bool-true"),
        pytest.param(False, id="bool-false"),
        pytest.param(None, id="null"),
        pytest.param(["a", "b"], id="array"),
        pytest.param({"finding": "x"}, id="object"),
        pytest.param("", id="empty-string"),
    ],
)
def test_every_json_native_lane_output_survives_a_scoped_fetch(
    store: ArtifactStore, output: Any
) -> None:
    """Fan-out submission accepts any JSON-native content, so a read must return it.

    ``json_extract`` decodes to a SQL value -- prose loses its quotes, ``true``
    arrives as ``1``, ``null`` as absence -- so an extraction that only survived
    objects and arrays lost every other lane output it was handed.
    """
    body = {
        "kind": "question_advisory",
        "result": {"aggregated_outputs": [{"lane_id": "code_context", "output": output}]},
    }
    contract_id = _publish_body(store, body, contract_id="fanout:json-native")

    assert store.fetch_lane(contract_id, "code_context").body == output


def test_an_ordinary_contract_id_containing_a_hash_is_still_reachable(
    store: ArtifactStore,
) -> None:
    """Length is the whole identity rule, so ``ordinary#id`` is a valid stored key.

    Folding a lane into the contract id gave that string a second reading, and
    the artifact stored under it went missing. The lane travels as its own
    value, so there is no address to take apart.
    """
    contract_id = _publish_body(store, {"kind": "other", "note": "kept"}, contract_id="ordinary#id")

    assert store.fetch(contract_id).body == {"kind": "other", "note": "kept"}
    assert store.replay(contract_id).body == {"kind": "other", "note": "kept"}


def test_an_unscoped_fetch_still_returns_the_whole_fan_out(store: ArtifactStore) -> None:
    """Every caller outside the advisory path passes a plain id and must be untouched."""
    contract_id = _publish_distinguishable(store)

    body = store.fetch(contract_id).body

    assert [entry["lane_id"] for entry in body["result"]["aggregated_outputs"]] == [
        "code_context",
        "data_context",
        "ambiguity_contrarian",
    ]


def test_a_lane_absent_from_a_fan_out_is_absence_not_a_sibling(store: ArtifactStore) -> None:
    """Scoping to a lane the body does not carry returns nothing, never someone else's."""
    contract_id = _publish_distinguishable(store)

    assert store.fetch_lane_if_exists(contract_id, "web_context") is None
    with pytest.raises(ArtifactNotFoundError):
        store.fetch_lane(contract_id, "web_context")


def test_the_prompt_hands_over_both_values_the_fetch_needs(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """A lane cannot narrow the artifact without the lane_id, so the block carries it."""
    contract_id = _publish(store)

    prompt = _lane_prompts(_attach(roster, store, tool_name="ouroboros_interview"))["code_context"]

    assert f"`contract_id`: `{contract_id}`" in prompt
    assert "`lane_id`: `code_context`" in prompt
    # The count is what a lane says when the tool is unreachable, and the list is
    # capped -- so it is stated as what was offered rather than as a total.
    assert "You are offered 1 recent finding" in prompt
    assert "you were offered\n1," in prompt


def test_a_tombstoned_artifact_is_tombstoned_through_the_scoped_read_too(
    store: ArtifactStore,
) -> None:
    """Pruning is terminal state, and a read path that hides it re-answers replay.

    The lane match was an inner join, so a pruned row -- whose body is SQL NULL
    and yields no lanes -- vanished before the tombstone check could run, and a
    scoped read reported the contract as never having existed. The join is now
    outer: no row is no contract, a NULL body is the same tombstone ``fetch``
    reports, and only a live body can report a missing lane.
    """
    from datetime import timedelta

    from ouroboros.persistence.artifact_errors import ArtifactTombstonedError

    contract_id = _publish_distinguishable(store)
    store.prune(apply=True, now=datetime.now(UTC) + timedelta(days=91))

    with pytest.raises(ArtifactTombstonedError):
        store.fetch(contract_id)
    with pytest.raises(ArtifactTombstonedError):
        store.fetch_lane(contract_id, "code_context")
    with pytest.raises(ArtifactTombstonedError):
        store.fetch_lane(contract_id, "lane_never_dispatched")


def test_a_missing_contract_is_not_found_whichever_lane_is_asked(store: ArtifactStore) -> None:
    """The scoped read tells a missing contract apart from a missing lane."""
    assert store.fetch_lane_if_exists("fanout:never-published", "code_context") is None
    with pytest.raises(ArtifactNotFoundError):
        store.fetch_lane("fanout:never-published", "code_context")


def _fetch_handler(store: ArtifactStore) -> Any:
    from ouroboros.mcp.tools.fanout_handler import FetchArtifactHandler

    return FetchArtifactHandler(disposable_memory=DisposableMemory(artifact_store=store))


def test_a_supplied_blank_lane_id_fails_closed_at_the_tool_boundary(
    store: ArtifactStore,
) -> None:
    """A malformed scoped request is an error, never the broader read.

    The handler used to normalize the argument and branch on truthiness, so
    ``lane_id: "   "`` -- supplied, but blank -- silently became an unscoped
    fetch and returned every sibling's output. Presence now decides the path:
    anything supplied is looked up verbatim, and no fan-out ever dispatched a
    blank lane, so it is not-found.
    """
    contract_id = _publish_distinguishable(store)
    handler = _fetch_handler(store)

    for supplied in ("", "   ", "\t"):
        result = asyncio.run(handler.handle({"contract_id": contract_id, "lane_id": supplied}))
        assert result.is_err, repr(supplied)
        assert "artifact fetch failed" in str(result.error)


def test_an_unknown_lane_id_fails_closed_at_the_tool_boundary(store: ArtifactStore) -> None:
    """A lane the artifact does not carry is an error, not someone else's output."""
    contract_id = _publish_distinguishable(store)
    handler = _fetch_handler(store)

    result = asyncio.run(handler.handle({"contract_id": contract_id, "lane_id": "web_context"}))

    assert result.is_err


def test_an_omitted_lane_id_is_the_legacy_whole_artifact_read(store: ArtifactStore) -> None:
    """Intentional omission stays the compatibility path -- absent key or JSON null."""
    contract_id = _publish_distinguishable(store)
    handler = _fetch_handler(store)

    for arguments in (
        {"contract_id": contract_id},
        {"contract_id": contract_id, "lane_id": None},
    ):
        result = asyncio.run(handler.handle(dict(arguments)))
        assert result.is_ok, arguments
        lanes = result.value.meta["body"]["result"]["aggregated_outputs"]
        assert [entry["lane_id"] for entry in lanes] == [
            "code_context",
            "data_context",
            "ambiguity_contrarian",
        ]
        assert "lane_id" not in result.value.meta


def test_a_valid_scoped_request_returns_that_lane_through_the_tool(
    store: ArtifactStore,
) -> None:
    """The one good path, pinned beside the failure paths that surround it."""
    contract_id = _publish_distinguishable(store)
    handler = _fetch_handler(store)

    result = asyncio.run(handler.handle({"contract_id": contract_id, "lane_id": "code_context"}))

    assert result.is_ok
    assert result.value.meta == {
        "contract_id": contract_id,
        "lane_id": "code_context",
        "body": {"finding": "the code says CODE-ONLY"},
    }


def test_the_request_schema_makes_a_mismatched_lane_pairing_unrepresentable(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """An entry under one lane key naming a sibling would offer that sibling's output."""
    from jsonschema import Draft202012Validator

    from ouroboros.orchestrator.capabilities.interview_schemas import (
        _interview_question_advisory_request_schema,
    )

    _publish(store)
    meta = _attach([], store, tool_name="ouroboros_interview", phase="start")
    request = json.loads(json.dumps(meta["question_advisory_request"]))
    validator = Draft202012Validator(_interview_question_advisory_request_schema())

    assert list(validator.iter_errors(request)) == []
    request["recent_findings"]["code_context"][0]["lane_id"] = "data_context"
    assert list(validator.iter_errors(request)) != []


def test_a_lane_alone_lists_that_lane_own_recent_findings(store: ArtifactStore) -> None:
    """A lane may ask which of its findings exist instead of being handed the ids.

    Sending the list in every prompt spent a fifth of it on identifiers a child
    has no way to choose between, and a lane wanting none of them paid for it
    anyway. Asking is the same answer, pulled: the window, the eligible kind
    and the cap stay the query's, so a lane cannot ask for a wider read than it
    was ever offered — only for its own, and only from the last day.
    """
    contract_id = _publish_distinguishable(store)
    handler = _fetch_handler(store)

    result = asyncio.run(handler.handle({"lane_id": "code_context"}))

    assert result.is_ok
    listing = result.value.meta
    assert listing["lane_id"] == "code_context"
    assert [entry["contract_id"] for entry in listing["recent"]] == [contract_id]
    assert {"contract_id", "lane_id", "published_at"} == set(listing["recent"][0])
    # Bodies stay in the store: what is listed is what to read, never the read.
    assert "output" not in json.dumps(listing)


def test_neither_a_contract_nor_a_lane_is_refused(store: ArtifactStore) -> None:
    """An empty request names both ways to ask rather than guessing one."""
    result = asyncio.run(_fetch_handler(store).handle({}))

    assert result.is_err
    assert "contract_id" in str(result.error) and "lane_id" in str(result.error)
