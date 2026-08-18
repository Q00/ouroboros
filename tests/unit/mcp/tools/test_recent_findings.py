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


def test_a_lane_answering_under_a_closed_contract_is_offered_none(
    roster: list[dict[str, str]], store: ArtifactStore
) -> None:
    """It has nowhere to put a finding, and nowhere to say it could not fetch one.

    Its answer shape rejects any field it does not name, so "I could not reach
    the tool" discards the whole answer -- and those lanes are required, so the
    fan-out then cannot complete. Staying silent instead reports having found
    nothing, which is the confusion this is built to prevent. The PM lanes are
    the case that made this visible: both of theirs are closed.
    """
    _publish(store)

    interview_meta = _attach(roster, store, tool_name="ouroboros_interview")
    pm_meta = _attach(roster, store, tool_name="ouroboros_pm_interview")
    interview = _lane_prompts(interview_meta)
    pm = _lane_prompts(pm_meta)

    assert "## Recently Found Here" in interview["code_context"]
    assert "## Recently Found Here" not in interview["data_context"]
    for prompt in pm.values():
        assert "## Recently Found Here" not in prompt

    # And the request carries no key for a lane that will never render one: a
    # key nothing reads is a promise the schema makes and the prompt never keeps.
    assert list(interview_meta["question_advisory_request"]["recent_findings"]) == ["code_context"]
    assert "recent_findings" not in pm_meta["question_advisory_request"]


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

    assert "published 2 findings" in prompt
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
