"""Pre-anchor legacy identity path: observable activations, unchanged behavior.

The transitional pre-anchor representation now lives in
``orchestrator.legacy_identity`` and every activation emits the structured
event ``project_map.legacy_identity_path`` (see docs/rfc/project-map-v1.md,
"Legacy pre-anchor sessions"). These tests pin the event contract at each
entry point and the byte-identical legacy representation behind it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from ouroboros.core.worktree import TaskWorkspace
from ouroboros.orchestrator.legacy_identity import (
    legacy_task_workspace_identity,
    note_legacy_identity_path,
)
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorRunner,
)
from ouroboros.orchestrator.session import SESSION_START_IDENTITY_PROGRESS_KEY

_EVENT = "project_map.legacy_identity_path"


def _adapter(cwd: str) -> MagicMock:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.llm_backend = "anthropic"
    adapter.working_directory = cwd
    adapter.permission_mode = "acceptEdits"
    adapter._model = "constructor-sonnet"
    return adapter


def _runner(cwd: str = "/tmp/project", **kwargs) -> OrchestratorRunner:
    return OrchestratorRunner(_adapter(cwd), AsyncMock(), MagicMock(), **kwargs)


def _workspace(*, repo_root: str, original_cwd: str) -> TaskWorkspace:
    return TaskWorkspace(
        durable_id="task-1",
        repo_root=repo_root,
        repo_name="repo",
        original_cwd=original_cwd,
        effective_cwd=original_cwd,
        worktree_path=repo_root,
        branch="main",
        lock_path="/tmp/task-1.lock",
    )


def _activations(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == _EVENT]


def _contract_and_resumed_runner(tmp_path: Path) -> tuple[dict, OrchestratorRunner]:
    """Build a contract from an owned live project, as production does."""
    project = tmp_path / "project"
    project.mkdir()
    original = _runner(cwd=str(project))
    project_identity = original._project_identity()
    return (
        original._build_execution_contract(project_identity=project_identity),
        _runner(cwd=str(project)),
    )


class TestLegacyTaskWorkspaceIdentity:
    """The extracted pure representation reproduces pre-anchor output exactly."""

    def test_nested_cwd_maps_to_relative_posix_path(self) -> None:
        workspace = _workspace(repo_root="/repo/root", original_cwd="/repo/root/pkg/sub")

        identity = legacy_task_workspace_identity(workspace, lambda path: path)

        assert identity == {"project_root": "/repo/root", "workspace_path": "pkg/sub"}

    def test_cwd_at_repo_root_maps_to_dot(self) -> None:
        workspace = _workspace(repo_root="/repo/root", original_cwd="/repo/root")

        identity = legacy_task_workspace_identity(workspace, lambda path: path)

        assert identity == {"project_root": "/repo/root", "workspace_path": "."}

    def test_paths_are_canonicalized_before_comparison(self) -> None:
        # A symlinked original_cwd must land inside the canonical root, exactly
        # as the pre-extraction classmethod resolved both sides first.
        workspace = _workspace(repo_root="/repo/root", original_cwd="/link")
        canonical = {"/link": "/repo/root/pkg"}

        identity = legacy_task_workspace_identity(workspace, lambda path: canonical.get(path, path))

        assert identity == {"project_root": "/repo/root", "workspace_path": "pkg"}

    def test_cwd_outside_root_raises_the_historical_error(self) -> None:
        workspace = _workspace(repo_root="/repo/root", original_cwd="/elsewhere")

        with pytest.raises(OrchestratorError) as exc_info:
            legacy_task_workspace_identity(workspace, lambda path: path)

        assert "invalid historical task workspace" in str(exc_info.value)
        assert exc_info.value.details == {"invalid": "legacy_task_workspace"}


class TestRunnerDelegation:
    """The runner's proof-identity path reuses the extracted representation."""

    def test_task_workspace_proof_identity_matches_module_output(self) -> None:
        workspace = _workspace(repo_root="/tmp/project", original_cwd="/tmp/project")
        runner = _runner(task_workspace=workspace)

        assert runner._legacy_proof_workspace_identity() == legacy_task_workspace_identity(
            workspace, runner._canonical_path
        )

    def test_direct_cwd_proof_identity_is_unchanged(self) -> None:
        runner = _runner(cwd="/tmp/project")

        assert runner._legacy_proof_workspace_identity() == {
            "project_root": runner._canonical_path("/tmp/project"),
            "workspace_path": ".",
        }


class TestActivationEvents:
    """Only genuine durable pre-anchor resumes count as legacy activations."""

    def test_note_emits_the_structured_event(self) -> None:
        with capture_logs() as logs:
            note_legacy_identity_path("some_entry", session_id="sess-1")

        activations = _activations(logs)
        assert len(activations) == 1
        assert activations[0]["entry_point"] == "some_entry"
        assert activations[0]["session_id"] == "sess-1"
        assert activations[0]["log_level"] == "info"

    def test_durable_preanchor_resume_emits_workspace_comparison(self, tmp_path: Path) -> None:
        # A durable start-identity snapshot without the additive project anchor
        # is exactly the historical v9 shape: restoring it must take — and
        # record — the legacy workspace-comparison branch, then still restore
        # cleanly.
        persisted, resumed = _contract_and_resumed_runner(tmp_path)
        progress = {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {"seed_id": "seed-legacy"},
        }

        with capture_logs() as logs:
            changed = resumed._restore_execution_contract(progress)

        assert changed is False
        activations = _activations(logs)
        assert [entry["entry_point"] for entry in activations] == ["resume_workspace_comparison"]
        assert activations[0]["prepared_live_execution"] is False

    def test_prepared_live_restore_is_not_a_legacy_activation(self, tmp_path: Path) -> None:
        # The bot-probed false-positive shape: current prepared executions
        # restore an intentionally anchorless contract-only mapping with
        # prepared_live_execution=True. That is not a historical resume and
        # must not pollute the removal metric.
        persisted, resumed = _contract_and_resumed_runner(tmp_path)

        with capture_logs() as logs:
            resumed._restore_execution_contract(
                {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
                prepared_live_execution=True,
            )

        assert _activations(logs) == []

    def test_contract_only_restore_without_snapshot_does_not_count(self, tmp_path: Path) -> None:
        # Without a durable start-identity snapshot there is no evidence the
        # session is historical, so no activation may be recorded even on the
        # non-prepared path.
        persisted, resumed = _contract_and_resumed_runner(tmp_path)

        with capture_logs() as logs:
            changed = resumed._restore_execution_contract(
                {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}
            )

        assert changed is False
        assert _activations(logs) == []

    def test_identity_window_does_not_bypass_execution_contract_gate(self, tmp_path: Path) -> None:
        """The support window preserves identity only, not every resume contract."""
        persisted, resumed = _contract_and_resumed_runner(tmp_path)
        persisted["version"] = 8
        progress = {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {"seed_id": "seed-legacy"},
        }

        with capture_logs() as logs, pytest.raises(OrchestratorError) as exc_info:
            resumed._restore_execution_contract(progress)

        assert exc_info.value.details["resume_blocked"] == "execution_inputs_unavailable"
        assert _activations(logs) == []
