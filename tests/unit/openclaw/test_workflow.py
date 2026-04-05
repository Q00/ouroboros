from pathlib import Path

from ouroboros.openclaw.workflow import (
    ChannelRef,
    ChannelRepoRegistry,
    ChannelWorkflowManager,
    ChannelWorkflowRequest,
    WorkflowEntryPoint,
    WorkflowStage,
    detect_entry_point,
)


def test_detect_entry_point_defaults_to_interview_for_natural_language() -> None:
    detection = detect_entry_point("work on issue #123 and ask clarifying questions")
    assert detection.entry_point == WorkflowEntryPoint.INTERVIEW


def test_detect_entry_point_uses_execution_for_seed_like_yaml() -> None:
    detection = detect_entry_point(
        "goal: test\nacceptance_criteria:\n- thing\nconstraints:\n- other"
    )
    assert detection.entry_point == WorkflowEntryPoint.EXECUTION


def test_channel_repo_registry_persists_default_repo(tmp_path: Path) -> None:
    registry = ChannelRepoRegistry(tmp_path / "repos.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")
    registry.set(channel, "/repos/ouroboros")

    reloaded = ChannelRepoRegistry(tmp_path / "repos.json")
    assert reloaded.get(channel) == "/repos/ouroboros"


def test_same_channel_second_request_is_queued(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")

    first = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    second = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u2",
            message="work on feature B",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )

    assert first.stage == WorkflowStage.INTERVIEWING
    assert second.stage == WorkflowStage.QUEUED
    assert manager.active_for_channel(channel).workflow_id == first.workflow_id
    assert [record.workflow_id for record in manager.queued_for_channel(channel)] == [
        second.workflow_id
    ]


def test_cross_channel_requests_are_independent(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")

    first = manager.enqueue(
        ChannelWorkflowRequest(
            channel=ChannelRef(channel_id="chan-a", guild_id="guild-1"),
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    second = manager.enqueue(
        ChannelWorkflowRequest(
            channel=ChannelRef(channel_id="chan-b", guild_id="guild-1"),
            user_id="u2",
            message="goal: run\nacceptance_criteria:\n- thing\nconstraints:\n- other",
            repo="/repo/b",
            entry_point=WorkflowEntryPoint.EXECUTION,
        )
    )

    assert first.stage == WorkflowStage.INTERVIEWING
    assert second.stage == WorkflowStage.EXECUTING


def test_queue_advances_after_completion(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")

    first = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    second = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u2",
            message="goal: run\nacceptance_criteria:\n- thing\nconstraints:\n- other",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.EXECUTION,
        )
    )

    manager.mark_completed(first.workflow_id, pr_url="https://example.com/pr/1")

    active = manager.active_for_channel(channel)
    assert active is not None
    assert active.workflow_id == second.workflow_id
    assert active.stage == WorkflowStage.EXECUTING


def test_state_persists_across_manager_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    manager = ChannelWorkflowManager(state_path)
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")
    first = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u2",
            message="work on feature B",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )

    reloaded = ChannelWorkflowManager(state_path)
    active = reloaded.active_for_channel(channel)
    queued = reloaded.queued_for_channel(channel)
    assert active is not None
    assert active.workflow_id == first.workflow_id
    assert len(queued) == 1


def test_duplicate_detection_ignores_terminal_workflow(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")
    record = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    manager.mark_completed(record.workflow_id, pr_url="https://example.com/pr/1")

    duplicate = manager.find_inflight_duplicate(
        channel,
        user_id="u1",
        message="work on feature A",
        repo="/repo/a",
        entry_point=WorkflowEntryPoint.INTERVIEW,
    )
    assert duplicate is None


def test_duplicate_detection_uses_transport_message_id(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")
    manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
            message_id="msg-1",
        )
    )

    duplicate = manager.find_inflight_duplicate(
        channel,
        user_id="u1",
        message="work on feature A",
        repo="/repo/a",
        entry_point=WorkflowEntryPoint.INTERVIEW,
        message_id="msg-1",
    )
    assert duplicate is not None


def test_duplicate_detection_uses_transport_event_id(tmp_path: Path) -> None:
    manager = ChannelWorkflowManager(tmp_path / "state.json")
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")
    manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo="/repo/a",
            entry_point=WorkflowEntryPoint.INTERVIEW,
            event_id="evt-1",
        )
    )

    duplicate = manager.find_inflight_duplicate(
        channel,
        user_id="u1",
        message="different text is okay for event dedupe",
        repo="/repo/a",
        entry_point=WorkflowEntryPoint.INTERVIEW,
        event_id="evt-1",
    )
    assert duplicate is not None


def test_changing_default_repo_does_not_rewrite_existing_queued_workflow(tmp_path: Path) -> None:
    registry = ChannelRepoRegistry(tmp_path / "repos.json")
    state_path = tmp_path / "state.json"
    manager = ChannelWorkflowManager(state_path)
    channel = ChannelRef(channel_id="chan-1", guild_id="guild-1")

    registry.set(channel, "/repo/old")
    manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u1",
            message="work on feature A",
            repo=registry.get(channel) or "",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )
    queued = manager.enqueue(
        ChannelWorkflowRequest(
            channel=channel,
            user_id="u2",
            message="work on feature B",
            repo=registry.get(channel) or "",
            entry_point=WorkflowEntryPoint.INTERVIEW,
        )
    )

    registry.set(channel, "/repo/new")
    reloaded = ChannelWorkflowManager(state_path)
    queued_record = reloaded.get(queued.workflow_id)
    assert queued_record is not None
    assert queued_record.repo == "/repo/old"
