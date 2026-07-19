"""Task 3: the HUD surfaces the Task 1/2 trust verdict and escalation state.

``execution.ac.decomposition_attested`` and ``execution.ac.parked_for_operator``
are folded onto the AC tree nodes so the node label carries a compact badge
and the footer/summary carries an aggregate count — across all three
verbosity modes (``compact``, ``summary``, ``tree``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    ACTreeHUDHandler,
    _merge_trust_escalation_events_into_snapshot,
    render_ac_tree_hud_markdown,
)
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def _progress_data() -> dict[str, object]:
    return {
        "completed_count": 0,
        "total_count": 2,
        "acceptance_criteria": [
            {"node_id": "ac_1", "index": 1, "content": "First criterion", "status": "executing"},
            {"node_id": "ac_2", "index": 2, "content": "Second criterion", "status": "executing"},
        ],
    }


def test_delayed_older_attestation_cannot_hide_newer_hud_verdict() -> None:
    snapshot = {
        "root_id": "root",
        "nodes": {
            "root": {"id": "root", "children_ids": ["ac_1"]},
            "ac_1": {"id": "ac_1", "children_ids": []},
        },
    }
    merged = _merge_trust_escalation_events_into_snapshot(
        snapshot,
        [
            BaseEvent(
                type="execution.ac.decomposition_attested",
                aggregate_type="execution",
                aggregate_id="exec_1",
                data={
                    "node_id": "ac_1",
                    "retry_attempt": 2,
                    "verdict": "untrustworthy",
                    "trustworthy": False,
                },
            ),
            BaseEvent(
                type="execution.ac.decomposition_attested",
                aggregate_type="execution",
                aggregate_id="exec_1",
                data={
                    "node_id": "ac_1",
                    "retry_attempt": 1,
                    "verdict": "trustworthy",
                    "trustworthy": True,
                },
            ),
        ],
    )

    assert merged["nodes"]["ac_1"]["trust_verdict"] == "untrustworthy"
    assert merged["nodes"]["ac_1"]["trustworthy"] is False


class TestRenderMarkdownDirectly:
    def test_tree_view_badges_untrustworthy_node_and_footer(self) -> None:
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_trust",
            execution_id="exec_trust",
            session_status="running",
            progress_data={
                **_progress_data(),
                "ac_tree": {
                    "root_id": "root",
                    "nodes": {
                        "root": {"id": "root", "content": "root", "children_ids": ["ac_1", "ac_2"]},
                        "ac_1": {
                            "id": "ac_1",
                            "content": "First criterion",
                            "status": "executing",
                            "index": 1,
                            "depth": 1,
                            "children_ids": [],
                            "trust_verdict": "untrustworthy",
                            "trustworthy": False,
                        },
                        "ac_2": {
                            "id": "ac_2",
                            "content": "Second criterion",
                            "status": "executing",
                            "index": 2,
                            "depth": 1,
                            "children_ids": [],
                        },
                    },
                },
            },
            view="tree",
        )

        assert "[untrusted:untrustworthy]" in markdown
        assert "Trust/Escalation: 1 untrustworthy split" in markdown

    def test_tree_view_badges_parked_node(self) -> None:
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_parked",
            execution_id="exec_parked",
            session_status="running",
            progress_data={
                **_progress_data(),
                "ac_tree": {
                    "root_id": "root",
                    "nodes": {
                        "root": {"id": "root", "content": "root", "children_ids": ["ac_1"]},
                        "ac_1": {
                            "id": "ac_1",
                            "content": "Stubborn criterion",
                            "status": "executing",
                            "index": 1,
                            "depth": 1,
                            "children_ids": [],
                            "escalation_state": "parked",
                        },
                    },
                },
            },
            view="tree",
        )

        assert "[parked]" in markdown
        assert "Trust/Escalation: 1 parked for operator" in markdown

    def test_tree_view_badges_escalating_node_with_current_persona(self) -> None:
        """Round-4 follow-up: an AC actively cycling personas (not yet
        parked) shows WHICH persona is currently being tried."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_escalating",
            execution_id="exec_escalating",
            session_status="running",
            progress_data={
                **_progress_data(),
                "ac_tree": {
                    "root_id": "root",
                    "nodes": {
                        "root": {"id": "root", "content": "root", "children_ids": ["ac_1"]},
                        "ac_1": {
                            "id": "ac_1",
                            "content": "Stubborn criterion",
                            "status": "executing",
                            "index": 1,
                            "depth": 1,
                            "children_ids": [],
                            "escalation_state": "escalating",
                            "escalation_persona": "hacker",
                        },
                    },
                },
            },
            view="tree",
        )

        assert "[persona:hacker]" in markdown
        assert "Trust/Escalation: 1 in persona escalation" in markdown

    def test_no_badge_when_absent(self) -> None:
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_clean",
            execution_id="exec_clean",
            session_status="running",
            progress_data=_progress_data(),
            view="tree",
        )

        assert "Trust/Escalation" not in markdown
        assert "[parked]" not in markdown
        assert "[untrusted" not in markdown


class TestParkedResolvedFolding:
    """Fix 8 (BLOCKING, PR #1648 review): a parked AC that later succeeds
    must have its parked badge cleared in the HUD too, not just show
    ``completed`` while ``[parked]`` still lingers on the node label."""

    @pytest.mark.asyncio
    async def test_parked_then_resolved_clears_the_hud_badge(
        self, memory_event_store: EventStore
    ) -> None:
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_resolved_e2e",
                data={
                    "execution_id": "exec_resolved_e2e",
                    "seed_id": "seed_resolved_e2e",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_resolved_e2e",
                data={
                    "execution_id": "exec_resolved_e2e",
                    "completed_count": 0,
                    "total_count": 1,
                    "acceptance_criteria": [
                        {
                            "node_id": "ac_1",
                            "index": 1,
                            "content": "Stubborn criterion",
                            "status": "executing",
                        },
                    ],
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id="exec_resolved_e2e",
                data={
                    "execution_id": "exec_resolved_e2e",
                    "session_id": "sess_resolved_e2e",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                    "personas_tried": ["hacker"],
                    "consecutive_terminal_failures": 3,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_resolved",
                aggregate_type="execution",
                aggregate_id="exec_resolved_e2e",
                data={
                    "execution_id": "exec_resolved_e2e",
                    "session_id": "sess_resolved_e2e",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)

        tree_result = await handler.handle(
            {"session_id": "sess_resolved_e2e", "cursor": 0, "view": "tree"}
        )
        assert tree_result.is_ok
        assert "[parked]" not in tree_result.value.text_content
        assert "Trust/Escalation" not in tree_result.value.text_content


class TestLateralEscalationProgressedFolding:
    """Round-4 follow-up: ``execution.ac.lateral_escalation_progressed`` must
    fold into the HUD tree so an operator sees in-flight persona-escalation
    progress BEFORE the AC is fully parked — previously the event was absent
    from both ``_TREE_CHANGE_EVENT_TYPES`` (cursor polling skipped it) and
    the merge reducer (no badge even when other events forced a render)."""

    @pytest.mark.asyncio
    async def test_progressed_event_surfaces_current_persona_in_tree_view(
        self, memory_event_store: EventStore
    ) -> None:
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_escalating_e2e",
                data={
                    "execution_id": "exec_escalating_e2e",
                    "seed_id": "seed_escalating_e2e",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_escalating_e2e",
                data={
                    "execution_id": "exec_escalating_e2e",
                    "completed_count": 0,
                    "total_count": 1,
                    "acceptance_criteria": [
                        {
                            "node_id": "ac_1",
                            "index": 1,
                            "content": "Stubborn criterion",
                            "status": "executing",
                        },
                    ],
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec_escalating_e2e",
                data={
                    "execution_id": "exec_escalating_e2e",
                    "session_id": "sess_escalating_e2e",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                    "personas_tried": ["hacker"],
                    "consecutive_terminal_failures": 3,
                    "parked": False,
                    "persona": "hacker",
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)

        tree_result = await handler.handle(
            {"session_id": "sess_escalating_e2e", "cursor": 0, "view": "tree"}
        )
        assert tree_result.is_ok
        assert "persona:hacker" in tree_result.value.text_content
        assert "1 in persona escalation" in tree_result.value.text_content

    @pytest.mark.asyncio
    async def test_parked_resolved_clears_progressed_badge_too(
        self, memory_event_store: EventStore
    ) -> None:
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_esc_resolved",
                data={
                    "execution_id": "exec_esc_resolved",
                    "seed_id": "seed_esc_resolved",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_esc_resolved",
                data={
                    "execution_id": "exec_esc_resolved",
                    "completed_count": 0,
                    "total_count": 1,
                    "acceptance_criteria": [
                        {
                            "node_id": "ac_1",
                            "index": 1,
                            "content": "Stubborn criterion",
                            "status": "executing",
                        },
                    ],
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec_esc_resolved",
                data={
                    "execution_id": "exec_esc_resolved",
                    "session_id": "sess_esc_resolved",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                    "personas_tried": ["hacker"],
                    "consecutive_terminal_failures": 3,
                    "parked": False,
                    "persona": "hacker",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_resolved",
                aggregate_type="execution",
                aggregate_id="exec_esc_resolved",
                data={
                    "execution_id": "exec_esc_resolved",
                    "session_id": "sess_esc_resolved",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)

        tree_result = await handler.handle(
            {"session_id": "sess_esc_resolved", "cursor": 0, "view": "tree"}
        )
        assert tree_result.is_ok
        assert "persona:hacker" not in tree_result.value.text_content
        assert "Trust/Escalation" not in tree_result.value.text_content


class TestHandlerEndToEnd:
    @pytest.mark.asyncio
    async def test_parked_event_surfaces_in_all_three_views(
        self, memory_event_store: EventStore
    ) -> None:
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_parked_e2e",
                data={
                    "execution_id": "exec_parked_e2e",
                    "seed_id": "seed_parked_e2e",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_parked_e2e",
                data={
                    "execution_id": "exec_parked_e2e",
                    "completed_count": 0,
                    "total_count": 1,
                    "acceptance_criteria": [
                        {
                            "node_id": "ac_1",
                            "index": 1,
                            "content": "Stubborn criterion",
                            "status": "executing",
                        },
                    ],
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id="exec_parked_e2e",
                data={
                    "execution_id": "exec_parked_e2e",
                    "session_id": "sess_parked_e2e",
                    "node_id": "ac_1",
                    "root_ac_index": 0,
                    "personas_tried": [
                        "hacker",
                        "researcher",
                        "simplifier",
                        "architect",
                        "contrarian",
                    ],
                    "consecutive_terminal_failures": 9,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)

        tree_result = await handler.handle(
            {"session_id": "sess_parked_e2e", "cursor": 0, "view": "tree"}
        )
        assert tree_result.is_ok
        assert "parked" in tree_result.value.text_content.lower()

        summary_result = await handler.handle(
            {"session_id": "sess_parked_e2e", "cursor": 0, "view": "summary"}
        )
        assert summary_result.is_ok
        assert "parked" in summary_result.value.text_content.lower()

        compact_result = await handler.handle(
            {"session_id": "sess_parked_e2e", "cursor": 0, "view": "compact"}
        )
        assert compact_result.is_ok
        assert "parked" in compact_result.value.text_content.lower()
