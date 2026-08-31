"""Harness-observed workspace changes back ``files_touched`` claims.

Reproduces the false negative behind the largest run-failure family in the
July event store: a leaf that really wrote a file through a shell command (no
``Edit``/``Write`` tool event, no authenticated Bash lease) was rejected as
``FABRICATION_SUSPECTED``. The dispatcher's before/after workspace snapshot is
the harness's own evidence that the file changed during the leaf's window.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.claims import _runtime_messages_support_file_claim
from ouroboros.orchestrator.evidence.harness_observation import (
    HARNESS_OBSERVATION_DATA_KEY,
    HARNESS_OBSERVATION_MESSAGE_TYPE,
    WorkspaceObservation,
    WorkspaceSnapshot,
    build_observation_message,
    diff_workspace_snapshots,
    insert_observation_message,
    is_harness_observation_message,
    observation_from_message,
    observations_confirm_unmutated_workspace,
    snapshot_workspace,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.profile_loader import load_profile


def _bump_mtime(path: Path) -> None:
    """Force a visible fingerprint change even on coarse filesystem clocks."""
    stat_result = path.stat()
    os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000))


def _bash_pair(command: str) -> tuple[AgentMessage, AgentMessage]:
    return (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}, "tool_call_id": "call-1"},
        ),
        AgentMessage(
            type="tool_result",
            content="",
            data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "call-1"},
        ),
    )


class TestSnapshotDiff:
    def test_new_and_modified_files_are_observed(self, tmp_path: Path) -> None:
        existing = tmp_path / "keep.py"
        existing.write_text("v1\n", encoding="utf-8")
        untouched = tmp_path / "untouched.py"
        untouched.write_text("same\n", encoding="utf-8")
        before = snapshot_workspace(tmp_path)

        existing.write_text("v2 longer\n", encoding="utf-8")
        _bump_mtime(existing)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "new.py").write_text("new\n", encoding="utf-8")

        observation = diff_workspace_snapshots(before, snapshot_workspace(tmp_path))

        assert observation is not None
        assert observation.changed_paths == frozenset({"keep.py", "sub/new.py"})
        assert observation.supports_file_claim("keep.py")
        assert observation.supports_file_claim("./sub/new.py")
        assert observation.supports_file_claim("SUB\\NEW.PY")
        assert not observation.supports_file_claim("untouched.py")
        assert not observation.supports_file_claim("../keep.py")
        assert not observation.supports_file_claim(str(tmp_path / "keep.py"))
        assert observation.truncated is False

    def test_ignored_directories_and_symlinks_are_not_fingerprinted(self, tmp_path: Path) -> None:
        # The repo conftest seeds tmp_path with fake CLI shims; use a clean root.
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / ".git").mkdir()
        (workspace / ".git" / "index").write_text("x", encoding="utf-8")
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
        (workspace / "real.txt").write_text("x", encoding="utf-8")
        os.symlink(workspace / "real.txt", workspace / "link.txt")

        snapshot = snapshot_workspace(workspace)

        assert snapshot is not None
        assert set(snapshot.fingerprints) == {"real.txt"}

    def test_entry_budget_marks_snapshot_truncated(self, tmp_path: Path) -> None:
        for index in range(5):
            (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")

        snapshot = snapshot_workspace(tmp_path, max_entries=3)

        assert snapshot is not None
        assert snapshot.truncated is True
        assert len(snapshot.fingerprints) == 3

    def test_truncated_pre_snapshot_withholds_paths_it_never_saw(self, tmp_path: Path) -> None:
        """Budget shift must not turn an unseen stale file into positive evidence.

        With ``max_entries=1`` the pre-snapshot holds one of the two files.
        Deleting one file lets the other enter the post-snapshot budget; its
        absence from the truncated pre-snapshot is uncertainty, not proof that
        this leaf created it.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.py").write_text("a\n", encoding="utf-8")
        (workspace / "b.py").write_text("b\n", encoding="utf-8")
        before = snapshot_workspace(workspace, max_entries=1)
        assert before is not None and before.truncated is True

        (workspace / "a.py").unlink()
        after = snapshot_workspace(workspace, max_entries=1)
        observation = diff_workspace_snapshots(before, after)

        assert observation is not None
        assert not observation.supports_file_claim("a.py")
        assert not observation.supports_file_claim("b.py")

    def test_truncated_snapshots_still_observe_a_path_seen_in_both(self) -> None:
        """A fingerprint change for a path present in both snapshots is genuine."""
        before = WorkspaceSnapshot(root="/ws", fingerprints={"seen.py": (2, 100)}, truncated=True)
        after = WorkspaceSnapshot(
            root="/ws",
            fingerprints={"seen.py": (9, 200), "unseen.py": (3, 300)},
            truncated=True,
        )

        observation = diff_workspace_snapshots(before, after)

        assert observation is not None
        assert observation.truncated is True
        assert observation.changed_paths == frozenset({"seen.py"})
        assert observation.supports_file_claim("seen.py")
        assert not observation.supports_file_claim("unseen.py")

    def test_deletion_is_observed_and_withdraws_the_zero_mutation_waiver(
        self, tmp_path: Path
    ) -> None:
        """Removing a file is a mutation: the diff must see it symmetrically."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "kept.py").write_text("x\n", encoding="utf-8")
        doomed = workspace / "doomed.py"
        doomed.write_text("y\n", encoding="utf-8")
        before = snapshot_workspace(workspace)

        doomed.unlink()
        observation = diff_workspace_snapshots(before, snapshot_workspace(workspace))

        assert observation is not None
        assert observation.changed_paths == frozenset()
        assert observation.deleted_paths == frozenset({"doomed.py"})
        assert not observations_confirm_unmutated_workspace(
            (build_observation_message(observation),)
        )

    def test_truncated_post_snapshot_claims_no_deletions(self) -> None:
        """Absence from a truncated post-snapshot is budget uncertainty."""
        before = WorkspaceSnapshot(root="/ws", fingerprints={"a.py": (1, 1), "b.py": (2, 2)})
        after = WorkspaceSnapshot(root="/ws", fingerprints={"a.py": (1, 1)}, truncated=True)

        observation = diff_workspace_snapshots(before, after)

        assert observation is not None
        assert observation.deleted_paths == frozenset()
        assert observation.truncated is True

    def test_missing_or_mismatched_roots_yield_no_observation(self, tmp_path: Path) -> None:
        assert snapshot_workspace(None) is None
        assert snapshot_workspace(tmp_path / "absent") is None
        other = tmp_path / "other"
        other.mkdir()
        assert (
            diff_workspace_snapshots(snapshot_workspace(tmp_path), snapshot_workspace(other))
            is None
        )
        assert diff_workspace_snapshots(None, snapshot_workspace(tmp_path)) is None


class TestObservationMessage:
    def test_round_trip_and_forgery_boundary(self) -> None:
        observation = WorkspaceObservation(changed_paths=frozenset({"a.py"}))
        message = build_observation_message(observation)

        assert observation_from_message(message) is observation
        assert is_harness_observation_message(message)

        # A runtime can only ever deliver JSON-shaped data: a dict with the same
        # key, or the right type name, is not an observation.
        forged_dict = AgentMessage(
            type=HARNESS_OBSERVATION_MESSAGE_TYPE,
            content="",
            data={HARNESS_OBSERVATION_DATA_KEY: {"changed_paths": ["a.py"]}},
        )
        forged_type = AgentMessage(
            type="tool",
            content="",
            tool_name="Bash",
            data={HARNESS_OBSERVATION_DATA_KEY: observation},
        )
        assert observation_from_message(forged_dict) is None
        assert observation_from_message(forged_type) is None

    def test_insert_appends_without_moving_existing_messages(self) -> None:
        """Appending only: repositioning would shift mid-stream index bookkeeping."""
        final = AgentMessage(type="result", content="done", data={"subtype": "success"})
        messages = [AgentMessage(type="assistant", content="working"), final]

        insert_observation_message(messages, WorkspaceObservation(changed_paths=frozenset()))

        assert [message.type for message in messages] == [
            "assistant",
            "result",
            HARNESS_OBSERVATION_MESSAGE_TYPE,
        ]
        assert messages[1] is final

        open_stream = [AgentMessage(type="assistant", content="working")]
        insert_observation_message(open_stream, WorkspaceObservation(changed_paths=frozenset()))
        assert open_stream[-1].type == HARNESS_OBSERVATION_MESSAGE_TYPE


class TestFileClaimSupport:
    def test_observed_change_backs_a_shell_written_file(self, tmp_path: Path) -> None:
        """The July false negative: a heredoc write has no Edit event and no lease."""
        command = "cat > hello.py <<'EOF'\nprint('hi')\nEOF"
        before = snapshot_workspace(tmp_path)
        (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        observation = diff_workspace_snapshots(before, snapshot_workspace(tmp_path))
        assert observation is not None

        call, completion = _bash_pair(command)
        without_observation = (call, completion)
        with_observation = (call, completion, build_observation_message(observation))

        assert not _runtime_messages_support_file_claim(
            "hello.py", without_observation, task_cwd=str(tmp_path)
        )
        assert _runtime_messages_support_file_claim(
            "hello.py", with_observation, task_cwd=str(tmp_path)
        )

    def test_unchanged_stale_file_stays_unsupported(self, tmp_path: Path) -> None:
        (tmp_path / "stale.py").write_text("old\n", encoding="utf-8")
        time.sleep(0.01)
        before = snapshot_workspace(tmp_path)
        observation = diff_workspace_snapshots(before, snapshot_workspace(tmp_path))
        assert observation is not None
        messages = (*_bash_pair("ls"), build_observation_message(observation))

        assert not _runtime_messages_support_file_claim(
            "stale.py", messages, task_cwd=str(tmp_path)
        )

    def test_duplicate_basename_cannot_borrow_observation_support(self, tmp_path: Path) -> None:
        """A changed ``foo.py`` must not vouch for an unchanged ``nested/foo.py``.

        The basename fallback exists for transcript-shaped evidence whose tool
        output reported ``generated.py`` instead of ``src/generated.py``; the
        observation answers only for the exact workspace-relative path.
        """
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "foo.py").write_text("stale\n", encoding="utf-8")
        observation = WorkspaceObservation(changed_paths=frozenset({"foo.py"}))
        messages = (*_bash_pair("ls"), build_observation_message(observation))

        assert not _runtime_messages_support_file_claim(
            "nested/foo.py", messages, task_cwd=str(tmp_path)
        )

    def test_out_of_workspace_claim_is_never_backed(self, tmp_path: Path) -> None:
        observation = WorkspaceObservation(changed_paths=frozenset({"hello.py"}))
        messages = (*_bash_pair("ls"), build_observation_message(observation))

        assert not _runtime_messages_support_file_claim(
            "../hello.py", messages, task_cwd=str(tmp_path)
        )
        assert not _runtime_messages_support_file_claim(
            "/etc/hello.py", messages, task_cwd=str(tmp_path)
        )


class TestSessionSignalBoundary:
    def test_follow_up_slice_and_restore_use_the_list_boundary(self) -> None:
        """The synthetic observation must not leak into follow-up bookkeeping.

        ``message_count`` counts only runtime messages, so after the observation
        is appended the transcript list is one entry longer. Slicing a queued
        signal's reply window (or restoring an aborted follow-up) from the
        runtime counter would start inside the previous turn — a successful
        first signal could make an error-only second signal look acknowledged.
        """
        from ouroboros.orchestrator.adapter import RuntimeHandle
        from ouroboros.orchestrator.leaf_dispatcher import LeafDispatchState
        from ouroboros.orchestrator.session_signal_followup import CompletedProviderTurn

        runtime_messages = [
            AgentMessage(type="assistant", content="working"),
            AgentMessage(type="result", content="done", data={"subtype": "success"}),
        ]
        state = LeafDispatchState(
            messages=list(runtime_messages),
            runtime_handle=RuntimeHandle(backend="claude"),
            message_count=len(runtime_messages),
            final_message="done",
            success=True,
        )
        insert_observation_message(
            state.messages, WorkspaceObservation(changed_paths=frozenset({"a.py"}))
        )

        assert state.runtime_handle is not None
        turn = CompletedProviderTurn.capture("dispatch-1", state.runtime_handle, state)
        assert turn.message_count == 2
        assert turn.message_list_length == 3

        # A follow-up's reply window starts after the observation, so the
        # previous turn's tail can never acknowledge the next signal.
        follow_up = AgentMessage(type="assistant", content="signal reply")
        state.messages.append(follow_up)
        assert state.messages[turn.message_list_length :] == [follow_up]

        # Restoring an aborted follow-up keeps the primary's observation.
        turn.restore(state)
        assert [message.type for message in state.messages] == [
            "assistant",
            "result",
            HARNESS_OBSERVATION_MESSAGE_TYPE,
        ]


class TestVerifierIntegration:
    def _verify(self, messages: tuple[AgentMessage, ...], tmp_path: Path) -> object:
        return _verify_atomic_evidence_against_runtime_messages(
            messages=messages,
            typed_evidence=EvidenceRecord(
                data={
                    "files_touched": ["hello.py"],
                    "commands_run": ["python3 -m pytest -q hello.py"],
                    "tests_passed": ["python3 -m pytest -q hello.py"],
                }
            ),
            ac_content="Implement greet() in hello.py with a passing doctest",
            execution_profile=load_profile("code"),
            task_cwd=str(tmp_path),
            adapter_working_directory=str(tmp_path),
        )

    def test_observation_turns_files_touched_rejection_into_pass(self, tmp_path: Path) -> None:
        test_command = "python3 -m pytest -q hello.py"
        before = snapshot_workspace(tmp_path)
        (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
        observation = diff_workspace_snapshots(before, snapshot_workspace(tmp_path))
        assert observation is not None

        write_call, write_done = _bash_pair("cat > hello.py <<'EOF'\n...\nEOF")
        test_call = AgentMessage(
            type="tool",
            content=f"Bash: {test_command}",
            tool_name="Bash",
            data={"tool_input": {"command": test_command}, "tool_call_id": "call-2"},
        )
        test_done = AgentMessage(
            type="tool_result",
            content="1 passed in 0.01s",
            data={
                "subtype": "tool_result",
                "exit_code": 0,
                "tool_call_id": "call-2",
                "output": "1 passed in 0.01s",
            },
        )
        final = AgentMessage(type="result", content="{}", data={"subtype": "success"})

        rejected = self._verify((write_call, write_done, test_call, test_done, final), tmp_path)
        assert rejected.passed is False
        assert rejected.failure_class == FailureClass.FABRICATION_SUSPECTED.value
        assert "files_touched: hello.py" in rejected.reasons[0]

        messages = [write_call, write_done, test_call, test_done, final]
        insert_observation_message(messages, observation)
        accepted = self._verify(tuple(messages), tmp_path)
        assert accepted.passed is True, accepted.reasons

    def test_observation_alone_does_not_count_as_a_transcript(self, tmp_path: Path) -> None:
        observation = WorkspaceObservation(changed_paths=frozenset({"hello.py"}))
        messages = [AgentMessage(type="result", content="{}", data={"subtype": "success"})]
        insert_observation_message(messages, observation)

        verdict = self._verify(tuple(messages), tmp_path)

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value
