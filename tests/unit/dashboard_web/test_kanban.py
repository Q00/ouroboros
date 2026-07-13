"""reduce_board — provider-tagged Kanban projection (deterministic, no DB)."""

from __future__ import annotations

from ouroboros.dashboard_web.kanban import COLUMNS, reduce_board


def _ev(event_type: str, **payload: object) -> dict[str, object]:
    return {"event_type": event_type, "payload": payload}


class TestColumnsAndStatus:
    def test_created_pending_then_completed_moves_columns(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC 1", status="pending"),
                _ev("execution.node.updated", node_id="n1", status="executing"),
                _ev("execution.node.updated", node_id="n1", status="completed"),
            ]
        )
        assert [c["id"] for c in board["columns"]["completed"]] == ["n1"]
        assert board["columns"]["pending"] == []
        assert board["columns"]["executing"] == []

    def test_status_aliases_normalized(self) -> None:
        board = reduce_board(
            [_ev("execution.node.created", node_id="n1", content="x", status="running")]
        )
        assert [c["id"] for c in board["columns"]["executing"]] == ["n1"]

    def test_ac_completed_failure_lands_in_failed(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="x", status="executing"),
                _ev("execution.ac.completed", node_id="n1", success=False),
            ]
        )
        assert [c["id"] for c in board["columns"]["failed"]] == ["n1"]

    def test_all_columns_present_even_when_empty(self) -> None:
        board = reduce_board([])
        assert set(board["columns"].keys()) == set(COLUMNS)


class TestProviderTagging:
    def test_session_started_tags_provider_and_session(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC 1", status="pending"),
                _ev(
                    "execution.session.started",
                    node_id="n1",
                    runtime_backend="codex_cli",
                    session_id="sess-codex",
                ),
            ]
        )
        # session.started moves a pending node into executing and tags it.
        card = board["columns"]["executing"][0]
        assert card["provider"] == "codex_cli"
        assert card["session_id"] == "sess-codex"

    def test_mixed_providers_render_distinctly(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC 1", status="executing"),
                _ev("execution.session.started", node_id="n1", runtime_backend="codex_cli"),
                _ev("execution.node.created", node_id="n2", content="AC 2", status="executing"),
                _ev("execution.session.started", node_id="n2", runtime_backend="claude"),
                _ev("execution.node.created", node_id="n3", content="AC 3", status="executing"),
                _ev("execution.session.started", node_id="n3", runtime_backend="claude_mcp"),
            ]
        )
        assert board["providers"] == ["claude", "claude_mcp", "codex_cli"]
        by_id = {c["id"]: c for c in board["columns"]["executing"]}
        assert by_id["n1"]["provider"] == "codex_cli"
        assert by_id["n2"]["provider"] == "claude"
        assert by_id["n3"]["provider"] == "claude_mcp"


class TestAcceptanceCriteriaCardSource:
    """Simple (non-decomposed) runs emit no execution.node.* events — the AC cards
    come from the workflow.progress.updated acceptance_criteria snapshot. Surfaced
    by the live smoke test."""

    def test_cards_built_from_ac_snapshot_when_no_node_events(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "workflow.progress.updated",
                    completed_count=1,
                    total_count=1,
                    current_phase="Deliver",
                    acceptance_criteria=[
                        {
                            "node_id": "node_ABC",
                            "content": "Create smoke.txt with SMOKE_OK",
                            "status": "completed",
                            "root_ac_number": 1,
                            "depth": 0,
                        }
                    ],
                )
            ]
        )
        done = board["columns"]["completed"]
        assert [c["id"] for c in done] == ["node_ABC"]
        assert done[0]["title"] == "Create smoke.txt with SMOKE_OK"
        assert done[0]["ac_index"] == 1

    def test_string_ac_items_are_supported(self) -> None:
        board = reduce_board(
            [_ev("workflow.progress.updated", acceptance_criteria=["Do the thing"])]
        )
        cards = board["columns"]["pending"]
        assert cards[0]["title"] == "Do the thing"
        assert cards[0]["ac_index"] == 1

    def test_node_events_merge_with_ac_snapshot_by_node_id(self) -> None:
        # The AC snapshot and a per-worker session.started reference the SAME
        # node_id → one card carrying both status and provider.
        board = reduce_board(
            [
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[
                        {"node_id": "node_X", "content": "AC", "status": "executing"}
                    ],
                ),
                _ev("execution.session.started", node_id="node_X", runtime_backend="codex_cli"),
            ]
        )
        card = board["columns"]["executing"][0]
        assert card["id"] == "node_X"
        assert card["provider"] == "codex_cli"


class TestRunLevelProvider:
    """Simple runs carry the provider only at run level (orchestrator.session.started
    runtime_backend) — it must tag cards that lack a per-worker provider."""

    def test_run_backend_tags_cards_and_meta(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "orchestrator.session.started",
                    runtime_backend="claude",
                    seed_goal="Build the thing",
                ),
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[
                        {"node_id": "n1", "content": "AC 1", "status": "completed"}
                    ],
                ),
            ]
        )
        assert board["meta"]["provider"] == "claude"
        assert board["meta"]["goal"] == "Build the thing"
        assert board["providers"] == ["claude"]
        assert board["columns"]["completed"][0]["provider"] == "claude"

    def test_per_worker_provider_overrides_run_backend(self) -> None:
        board = reduce_board(
            [
                _ev("orchestrator.session.started", runtime_backend="claude"),
                _ev("execution.node.created", node_id="n1", content="AC", status="executing"),
                _ev("execution.session.started", node_id="n1", runtime_backend="codex_mcp"),
            ]
        )
        # Per-worker codex_mcp wins for the card; run-level claude still in legend.
        assert board["columns"]["executing"][0]["provider"] == "codex_mcp"
        assert set(board["providers"]) == {"claude", "codex_mcp"}


class TestTerminalStickiness:
    """A node finished by ac.completed must NOT flicker back to executing when a
    lagging workflow.progress.updated snapshot still lists it as executing
    (the observed DONE → IN PROGRESS bounce)."""

    def test_ac_completed_not_reverted_by_stale_wpu_snapshot(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "executing"}],
                ),
                _ev("execution.ac.completed", node_id="n1", success=True),
                # The orchestrator keeps emitting coarse snapshots that still say
                # executing for a while after the AC actually completed.
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "executing"}],
                ),
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "executing"}],
                ),
            ]
        )
        assert [c["id"] for c in board["columns"]["completed"]] == ["n1"]
        assert board["columns"]["executing"] == []

    def test_failed_is_also_sticky(self) -> None:
        board = reduce_board(
            [
                _ev("execution.ac.completed", node_id="n1", success=False),
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "executing"}],
                ),
            ]
        )
        assert [c["id"] for c in board["columns"]["failed"]] == ["n1"]

    def test_authoritative_node_update_can_reopen_a_completed_node(self) -> None:
        # A GENUINE retry (execution.node.updated executing after completed) must
        # still re-open the card — only coarse snapshots are blocked.
        board = reduce_board(
            [
                _ev("execution.ac.completed", node_id="n1", success=True),
                _ev("execution.node.updated", node_id="n1", content="AC", status="executing"),
            ]
        )
        assert [c["id"] for c in board["columns"]["executing"]] == ["n1"]
        assert board["columns"]["completed"] == []

    def test_snapshot_can_still_complete_a_node(self) -> None:
        # For simple runs the snapshot is the ONLY source — it must reach completed.
        board = reduce_board(
            [
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "executing"}],
                ),
                _ev(
                    "workflow.progress.updated",
                    acceptance_criteria=[{"node_id": "n1", "content": "AC", "status": "completed"}],
                ),
            ]
        )
        assert [c["id"] for c in board["columns"]["completed"]] == ["n1"]


class TestFrugalityTelemetry:
    """execution.ac.model_routed / token_attribution / frugality_proof — three
    events that already exist in the store but were filtered out of the Kanban."""

    def test_model_routed_stamps_tier_and_model(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC", status="executing"),
                _ev(
                    "execution.ac.model_routed",
                    node_id="n1",
                    model_tier="frugal",
                    model="claude-haiku-4-5",
                ),
            ]
        )
        card = board["columns"]["executing"][0]
        assert card["model_tier"] == "frugal"
        assert card["model"] == "claude-haiku-4-5"

    def test_escalated_retry_overwrites_tier(self) -> None:
        # A later routing (escalated retry) wins over the earlier one.
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC", status="executing"),
                _ev("execution.ac.model_routed", node_id="n1", model_tier="frugal"),
                _ev("execution.ac.model_routed", node_id="n1", model_tier="frontier"),
            ]
        )
        assert board["columns"]["executing"][0]["model_tier"] == "frontier"

    def test_token_spend_accumulates_per_node_and_run_total(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC", status="executing"),
                _ev("execution.ac.token_attribution.reported", node_id="n1", token_spend=1200.0),
                _ev("execution.ac.token_attribution.reported", node_id="n1", token_spend=300.0),
                _ev("execution.node.created", node_id="n2", content="AC", status="executing"),
                _ev("execution.ac.token_attribution.reported", node_id="n2", token_spend=500.0),
            ]
        )
        by_id = {c["id"]: c for c in board["columns"]["executing"]}
        assert by_id["n1"]["tokens"] == 1500.0
        assert by_id["n2"]["tokens"] == 500.0
        assert board["meta"]["total_tokens"] == 2000.0

    def test_missing_or_malformed_values_are_omitted_not_none(self) -> None:
        # No telemetry at all → tokens/tier resolve to None (card carries the key
        # like provider does, but the surface omits it); a malformed spend never
        # accumulates into the run total.
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="AC", status="executing"),
                _ev("execution.ac.token_attribution.reported", node_id="n1", token_spend="oops"),
                _ev("execution.ac.model_routed", node_id="n1", model_tier=None),
            ]
        )
        card = board["columns"]["executing"][0]
        assert card["tokens"] is None
        assert card["model_tier"] is None
        assert board["meta"]["total_tokens"] == 0.0

    def test_frugality_proof_summary_reaches_meta(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "execution.frugality_proof.evaluated",
                    status="insufficient_data",
                    counted_rows=2,
                    token_reduction_pct=17.5,
                    reason="need >=3 runs",
                )
            ]
        )
        assert board["meta"]["frugality"] == {
            "status": "insufficient_data",
            "token_reduction_pct": 17.5,
            "reason": "need >=3 runs",
        }

    def test_frugality_proof_omits_malformed_reduction(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "execution.frugality_proof.evaluated",
                    status="proven",
                    token_reduction_pct=None,
                    reason="ok",
                )
            ]
        )
        assert board["meta"]["frugality"] == {"status": "proven", "reason": "ok"}

    def test_frugality_retrospective_summary_reaches_meta(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "execution.frugality_retrospective.reported",
                    retrospective_version="v1",
                    trigger="execution_finalized",
                    terminal_status="failed",
                    evidence_only=True,
                    coverage={
                        "measured_attempts": 3,
                        "unknown_attempts": 1,
                        "invalid_attempts": 0,
                        "total_measured_tokens": 250.0,
                    },
                    evidence_signals=[
                        {
                            "name": "retry_associated_spend",
                            "token_spend": 100.0,
                            "attempt_count": 1,
                        },
                        {
                            "name": "unaccepted_spend",
                            "token_spend": 150.0,
                            "attempt_count": 2,
                        },
                    ],
                )
            ]
        )

        assert board["meta"]["frugality_retrospective"] == {
            "terminal_status": "failed",
            "measured_attempts": 3,
            "unknown_attempts": 1,
            "invalid_attempts": 0,
            "total_measured_tokens": 250.0,
            "retry_associated_tokens": 100.0,
            "retry_associated_attempts": 1,
            "unaccepted_tokens": 150.0,
            "unaccepted_attempts": 2,
        }

    def test_frugality_retrospective_malformed_payload_is_omitted(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "execution.frugality_retrospective.reported",
                    retrospective_version="v1",
                    trigger="execution_finalized",
                    terminal_status="paused",
                    evidence_only=True,
                    coverage={},
                    evidence_signals=[],
                )
            ]
        )

        assert board["meta"]["frugality_retrospective"] is None


class TestToolAndMeta:
    def test_tool_activity_attached_to_node(self) -> None:
        board = reduce_board(
            [
                _ev("execution.node.created", node_id="n1", content="x", status="executing"),
                _ev("execution.tool.started", node_id="n1", tool_name="Bash"),
            ]
        )
        assert board["columns"]["executing"][0]["tool"] == "Bash"

    def test_workflow_progress_feeds_meta(self) -> None:
        board = reduce_board(
            [
                _ev(
                    "workflow.progress.updated",
                    completed_count=3,
                    total_count=7,
                    current_phase="Deliver",
                    activity="Level 5 complete",
                    session_id="orch_x",
                )
            ],
            execution_id="exec_abc",
        )
        assert board["meta"]["completed"] == 3
        assert board["meta"]["total"] == 7
        assert board["meta"]["phase"] == "Deliver"
        assert board["meta"]["session_id"] == "orch_x"
        assert board["meta"]["execution_id"] == "exec_abc"
