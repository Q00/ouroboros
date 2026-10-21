"""Project identity resolution and representations owned by the runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.core.project_identity import (
    ManagedProjectOwnershipError,
    ManagedProjectScopeError,
    ProjectIdentity,
    ProjectIdentityError,
    ProjectIdentityUnavailableError,
)
from ouroboros.core.worktree import TaskWorkspace

if TYPE_CHECKING:
    from ouroboros.orchestrator.runner import OrchestratorError
from ouroboros.orchestrator.session import SESSION_START_IDENTITY_PROGRESS_KEY


class ProjectAnchor:
    """Own identity logic with injected cwd, workspace, and resolver dependencies."""

    def __init__(
        self,
        *,
        workspace: Callable[[], TaskWorkspace | None],
        effective_cwd: Callable[[], str | None],
        canonical_path: Callable[[str], str],
        task_identity: Callable[[TaskWorkspace], ProjectIdentity],
        translate_error: Callable[[ProjectIdentityError], OrchestratorError],
        project_identity: Callable[[], ProjectIdentity | None],
        resolved_identity: Callable[[ProjectIdentity | None], dict[str, str] | None],
        task_resume_identity: Callable[[TaskWorkspace], dict[str, str]],
        resolve_identity: Callable[[str], ProjectIdentity],
        resolve_publication_identity: Callable[[str], tuple[ProjectIdentity, Any]],
        publication_sink: Callable[[], Any],
        legacy_identity: Callable[[TaskWorkspace, Callable[[str], str]], dict[str, str]],
    ) -> None:
        self._workspace = workspace
        self._effective_cwd = effective_cwd
        self._canonical_path = canonical_path
        self._task_workspace_project_identity = task_identity
        self._project_identity_error = translate_error
        self._project_identity = project_identity
        self._resolved_proof_workspace_identity = resolved_identity
        self._task_resume_workspace_identity = task_resume_identity
        self._resolve_identity = resolve_identity
        self._resolve_publication_identity = resolve_publication_identity
        self._publication_sink = publication_sink
        self._legacy_identity = legacy_identity

    @property
    def _task_workspace(self) -> TaskWorkspace | None:
        return self._workspace()

    @staticmethod
    def canonical_path(value: str) -> str:
        """Return a symlink-resolved absolute path without requiring existence."""
        return str(Path(value).expanduser().resolve(strict=False))

    @staticmethod
    def task_workspace_project_identity(
        workspace: TaskWorkspace,
        *,
        resolve_managed: Callable[..., ProjectIdentity],
        error_type: type[OrchestratorError],
    ) -> ProjectIdentity:
        """Resolve a managed worktree against its durable source checkout."""
        try:
            return resolve_managed(
                workspace.effective_cwd,
                source_root=workspace.repo_root,
                source_workspace=workspace.original_cwd,
                worktree_root=workspace.worktree_path,
            )
        except ManagedProjectScopeError as exc:
            raise error_type(
                message="Managed source and execution workspace scopes do not match",
                details={
                    "invalid": "runtime_cwd",
                    "source_cwd": exc.source_workspace,
                    "execution_cwd": exc.execution_workspace,
                    "resume_blocked": "runtime_cwd_mismatch",
                },
            ) from exc
        except ManagedProjectOwnershipError as exc:
            raise error_type(
                message="Managed worktree does not belong to its source project",
                details={
                    "invalid": "project_identity",
                    "source_identity": exc.source_identity.to_workspace_data(),
                    "execution_identity": exc.execution_identity.to_workspace_data(),
                    "resume_blocked": "project_identity_mismatch",
                },
            ) from exc

    @staticmethod
    def project_identity_error(
        exc: ProjectIdentityError, *, error_type: type[OrchestratorError]
    ) -> OrchestratorError:
        """Normalize resolver failures at the orchestration lifecycle boundary."""
        details: dict[str, Any] = {"invalid": "project_identity", "cause": str(exc)}
        if isinstance(exc, ProjectIdentityUnavailableError):
            details.update(
                resume_blocked="project_identity_unavailable",
                retryable=True,
            )
        return error_type(message="Cannot resolve project identity", details=details)

    def project_identity(self) -> ProjectIdentity | None:
        """Return the single canonical identity shared by event and contract."""
        try:
            if self._task_workspace is not None:
                self._effective_cwd()
                return self._task_workspace_project_identity(self._task_workspace)
            effective_cwd = self._effective_cwd()
            if not isinstance(effective_cwd, str) or not effective_cwd.strip():
                return None
            if self._publication_sink() is None:
                return self._resolve_identity(effective_cwd)
            identity, _evidence = self._resolve_publication_identity(effective_cwd)
            return identity
        except ProjectIdentityError as exc:
            raise self._project_identity_error(exc) from exc

    def proof_workspace_identity(self) -> dict[str, str] | None:
        """Return the stable project + source-workspace identity for this run.

        Managed task worktrees have a different checkout path for every session,
        so cohort identity is anchored to their persisted source repository and
        source-relative cwd. Direct Git callers resolve the nearest checkout,
        preserve a relative workspace scope, and join provable linked worktrees
        to their primary source root. Non-Git callers retain the conservative
        canonical effective-cwd identity.
        """
        return self._resolved_proof_workspace_identity(self._project_identity())

    @staticmethod
    def resolved_proof_workspace_identity(
        project_identity: ProjectIdentity | None,
    ) -> dict[str, str] | None:
        """Project one already-resolved identity without another resolver call."""
        return project_identity.to_workspace_data() if project_identity is not None else None

    def legacy_proof_workspace_identity(self) -> dict[str, str] | None:
        """Reproduce the pre-Project-Map V1 nested workspace representation."""
        if self._task_workspace is not None:
            return self._legacy_identity(self._task_workspace, self._canonical_path)
        effective_cwd = self._effective_cwd()
        if not isinstance(effective_cwd, str) or not effective_cwd.strip():
            return None
        return {
            "project_root": self._canonical_path(effective_cwd),
            "workspace_path": ".",
        }

    @staticmethod
    def project_start_identity(
        progress: Mapping[str, Any],
        *,
        translate_error: Callable[[ProjectIdentityError], OrchestratorError],
        error_type: type[OrchestratorError],
    ) -> tuple[bool, ProjectIdentity | None]:
        """Parse the all-or-none immutable Project Map session anchor."""
        raw_start_identity = progress.get(SESSION_START_IDENTITY_PROGRESS_KEY)
        if not isinstance(raw_start_identity, Mapping):
            return False, None
        project_keys = frozenset({"project_id", "project_root", "workspace_path"})
        present_keys = {key for key in project_keys if key in raw_start_identity}
        if not present_keys:
            return False, None
        if present_keys != project_keys:
            raise error_type(
                message="Cannot resume with a partial project identity anchor",
                details={"invalid": "project_identity_anchor"},
            )
        try:
            identity = ProjectIdentity(
                project_id=raw_start_identity["project_id"],
                project_root=raw_start_identity["project_root"],
                workspace_path=raw_start_identity["workspace_path"],
            )
        except ProjectIdentityUnavailableError as exc:
            raise translate_error(exc) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise error_type(
                message="Cannot resume with an invalid project identity anchor",
                details={"invalid": "project_identity_anchor"},
            ) from exc
        return True, identity

    @staticmethod
    def task_resume_workspace_identity(
        workspace: TaskWorkspace, *, canonical_path: Callable[[str], str]
    ) -> dict[str, str]:
        """Return the exact managed checkout identity required for safe resume."""
        return {
            "mode": "task_workspace",
            "durable_id": workspace.durable_id,
            "repo_root": canonical_path(workspace.repo_root),
            "worktree_path": canonical_path(workspace.worktree_path),
            "effective_cwd": canonical_path(workspace.effective_cwd),
            "branch": workspace.branch,
        }

    def resume_workspace_identity(self) -> dict[str, str] | None:
        """Return session-specific checkout identity, unlike stable proof cohorting."""
        if self._task_workspace is not None:
            return self._task_resume_workspace_identity(self._task_workspace)
        effective_cwd = self._effective_cwd()
        if not isinstance(effective_cwd, str) or not effective_cwd.strip():
            return None
        return {
            "mode": "direct",
            "effective_cwd": self._canonical_path(effective_cwd),
        }
