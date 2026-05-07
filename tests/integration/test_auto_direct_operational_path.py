"""End-to-end integration tests for the direct operational path (#689 PR-F).

These cover the full slice that PR-A through PR-E land:

1. PR URL goal → classifier authorizes direct path → pipeline records
   the routing decision and blocks with PR-F-pending guidance (until a
   destructive executor is wired we do not actually merge in tests).
2. Ambiguous goal → pipeline falls back to interview-first.
3. Gate-blocked merge → the provider returns a clean
   ``PullRequestStatus`` for the simulated PR but the gate still
   refuses because CI is pending; the audit record lands on the ledger.

The pipeline test uses a fake interview driver so the real backend
(which would talk to a model runtime) is never invoked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

from ouroboros.auto.gh_pr_provider import (
    CommandResult,
    GhPrProvider,
)
from ouroboros.auto.goal_classifier import classify_goal
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.merge_policy import (
    CIState,
    MergePolicyDecision,
    PullRequestStatus,
    evaluate_merge,
    record_decision_on_ledger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.core.seed import Seed


@dataclass(slots=True)
class _RecordingDriver:
    called: bool = False

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        self.called = True
        return AutoInterviewResult(
            status="blocked",
            session_id=None,
            ledger=ledger,
            rounds=0,
            blocker="interview must not be reached for direct-path goals",
        )


def _build_pipeline(*, env_enabled: bool, store: AutoStore) -> tuple[AutoPipeline, _RecordingDriver]:
    driver = _RecordingDriver()

    async def _seed_generator(_session_id: str) -> Seed:  # pragma: no cover
        msg = "seed generator must not be called in direct-path tests"
        raise AssertionError(msg)

    pipeline = AutoPipeline(
        interview_driver=driver,  # type: ignore[arg-type]
        seed_generator=_seed_generator,
        store=store,
        operational_env_override=env_enabled,
    )
    return pipeline, driver


def test_pr_url_goal_routes_through_direct_path(tmp_path) -> None:
    store = AutoStore(tmp_path)
    pipeline, driver = _build_pipeline(env_enabled=True, store=store)
    state = AutoPipelineState(
        goal="merge https://github.com/Q00/ouroboros/pull/689 once CI is green",
        cwd=str(tmp_path),
    )

    asyncio.run(pipeline.run(state))

    assert driver.called is False, "direct path must not invoke the interview driver"
    assert state.phase is AutoPhase.BLOCKED
    assert "PR-D/E" in (state.last_error or "")
    assert state.last_tool_name == "goal_classifier"

    persisted = store.load(state.auto_session_id)
    assert persisted.classification is not None
    assert persisted.classification["direct_run_allowed"] is True
    assert persisted.direct_path_reason == persisted.classification["reason"]
    ledger = SeedDraftLedger.from_dict(persisted.ledger)
    assert ledger.direct_path_reason == persisted.direct_path_reason


def test_ambiguous_goal_falls_back_to_interview(tmp_path) -> None:
    store = AutoStore(tmp_path)
    pipeline, driver = _build_pipeline(env_enabled=True, store=store)
    state = AutoPipelineState(
        goal=(
            "plan how we should fix https://github.com/Q00/ouroboros/issues/692 "
            "before any merge"
        ),
        cwd=str(tmp_path),
    )

    asyncio.run(pipeline.run(state))

    assert driver.called is True, "ambiguous goal must reach interview-first flow"
    persisted = store.load(state.auto_session_id)
    assert persisted.classification is not None
    assert persisted.classification["direct_run_allowed"] is False


def test_idea_goal_keeps_interview_when_env_enabled(tmp_path) -> None:
    """Env opt-in must not regress idea goals into a blocked state."""
    store = AutoStore(tmp_path)
    pipeline, driver = _build_pipeline(env_enabled=True, store=store)
    state = AutoPipelineState(
        goal="Build a CLI tool that tracks daily habits",
        cwd=str(tmp_path),
    )

    asyncio.run(pipeline.run(state))

    assert driver.called is True
    persisted = store.load(state.auto_session_id)
    assert persisted.classification is not None
    assert persisted.classification["direct_run_allowed"] is False


def test_gate_blocks_merge_when_ci_is_pending_and_records_audit() -> None:
    """End-to-end: provider → status → gate → ledger audit."""
    fake_responses = {
        (
            "gh",
            "pr",
            "view",
            "689",
            "--repo",
            "Q00/ouroboros",
            "--json",
            "mergeable,mergeStateStatus,headRefOid,baseRefName,"
            "isDraft,reviewDecision,latestReviews,statusCheckRollup",
        ): CommandResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "headRefOid": "deadbeef",
                    "baseRefName": "main",
                    "isDraft": False,
                    "reviewDecision": "APPROVED",
                    "latestReviews": [{"state": "APPROVED"}],
                    "statusCheckRollup": [{"status": "IN_PROGRESS"}],
                }
            ),
            stderr="",
        ),
        (
            "gh",
            "api",
            "repos/Q00/ouroboros",
            "--jq",
            ".permissions",
        ): CommandResult(
            returncode=0,
            stdout=json.dumps({"push": True}),
            stderr="",
        ),
    }

    def _runner(args: Sequence[str]) -> CommandResult:
        return fake_responses[tuple(args)]

    provider = GhPrProvider(runner=_runner)
    status: PullRequestStatus = provider.fetch_status("Q00/ouroboros", 689)
    assert status.ci_state is CIState.PENDING

    classification = classify_goal(
        "merge https://github.com/Q00/ouroboros/pull/689 once CI is green"
    )
    decision: MergePolicyDecision = evaluate_merge(
        classification=classification, status=status
    )
    assert decision.allowed is False
    assert any("CI state is pending" in r for r in decision.blocking_reasons)

    ledger = SeedDraftLedger.from_goal(
        "merge https://github.com/Q00/ouroboros/pull/689 once CI is green"
    )
    record = record_decision_on_ledger(ledger, decision, status)
    assert ledger.merge_policy_decisions == [record]

    # Resume safety: the audit record survives serialization.
    restored = SeedDraftLedger.from_dict(ledger.to_dict())
    assert restored.merge_policy_decisions == ledger.merge_policy_decisions
    assert (
        restored.merge_policy_decisions[-1]["decision"]["allowed"] is False
    )
