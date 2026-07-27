"""Tests for the Project Map V1 identity resolver."""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from ouroboros.core.project_identity import (
    ProjectIdentity,
    ProjectIdentityError,
    project_id_for_root,
    resolve_project_identity,
)


def test_project_id_is_full_uuid5_of_canonical_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    identity = resolve_project_identity(root)

    canonical = str(root.resolve())
    assert identity.project_id == f"project_{uuid5(NAMESPACE_URL, canonical).hex}"
    assert len(identity.project_id) == len("project_") + 32
    assert identity.project_root == canonical
    assert identity.workspace_path == "."


def test_nested_checkout_uses_repo_root_and_relative_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    workspace = root / "packages" / "api"
    workspace.mkdir(parents=True)
    (root / ".git").mkdir()

    identity = resolve_project_identity(workspace)

    assert identity.project_root == str(root.resolve())
    assert identity.project_id == project_id_for_root(root)
    assert identity.workspace_path == "packages/api"


def test_symlinked_workspace_canonicalizes_before_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    workspace = root / "packages" / "api"
    workspace.mkdir(parents=True)
    (root / ".git").mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(root, target_is_directory=True)

    direct = resolve_project_identity(workspace)
    through_alias = resolve_project_identity(alias / "packages" / "api")

    assert through_alias == direct


def test_linked_worktree_reuses_primary_source_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    linked = tmp_path / "linked"
    workspace = linked / "packages" / "web"
    workspace.mkdir(parents=True)
    linked_git_dir = source_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")

    source_identity = resolve_project_identity(source)
    linked_identity = resolve_project_identity(workspace)

    assert linked_identity.project_id == source_identity.project_id
    assert linked_identity.project_root == str(source.resolve())
    assert linked_identity.workspace_path == "packages/web"


def test_external_git_directory_joins_primary_and_linked_checkouts(tmp_path: Path) -> None:
    external_git = tmp_path / "storage.git"
    (external_git / "objects").mkdir(parents=True)
    (external_git / "refs").mkdir()
    (external_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (external_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "web"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {external_git}\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "web"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = external_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")

    primary_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)

    assert primary_identity == linked_identity
    assert primary_identity.project_root == str(external_git.resolve())
    assert primary_identity.workspace_path == "packages/web"


def test_unproven_worktree_pointer_cannot_join_another_project(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    forged = tmp_path / "forged"
    forged.mkdir()
    unrelated_git_dir = tmp_path / "unrelated-gitdir"
    unrelated_git_dir.mkdir()
    (forged / ".git").write_text(
        f"gitdir: {unrelated_git_dir}\n",
        encoding="utf-8",
    )
    (unrelated_git_dir / "commondir").write_text(
        f"{source_git}\n",
        encoding="utf-8",
    )

    identity = resolve_project_identity(forged)

    assert identity.project_root == str(forged.resolve())
    assert identity.project_id == project_id_for_root(forged)


def test_git_file_without_commondir_remains_its_own_project(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent_git = parent / ".git"
    parent_git.mkdir(parents=True)
    submodule = parent / "vendor" / "child"
    submodule.mkdir(parents=True)
    module_git_dir = parent_git / "modules" / "vendor" / "child"
    module_git_dir.mkdir(parents=True)
    (submodule / ".git").write_text(
        f"gitdir: {module_git_dir}\n",
        encoding="utf-8",
    )

    identity = resolve_project_identity(submodule)

    assert identity.project_root == str(submodule.resolve())
    assert identity.project_id == project_id_for_root(submodule)
    assert identity.workspace_path == "."


def test_submodule_core_worktree_resolves_back_to_its_checkout(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent_git = parent / ".git"
    parent_git.mkdir(parents=True)
    submodule = parent / "vendor" / "child"
    submodule.mkdir(parents=True)
    module_git_dir = parent_git / "modules" / "vendor" / "child"
    (module_git_dir / "objects").mkdir(parents=True)
    (module_git_dir / "refs").mkdir()
    (module_git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (module_git_dir / "config").write_text(
        "[core]\n\tbare = false\n\tworktree = ../../../../vendor/child\n",
        encoding="utf-8",
    )
    (submodule / ".git").write_text(
        "gitdir: ../../.git/modules/vendor/child\n",
        encoding="utf-8",
    )

    identity = resolve_project_identity(submodule)

    assert identity.project_root == str(submodule.resolve())
    assert identity.project_id == project_id_for_root(submodule)
    assert identity.workspace_path == "."


def test_non_git_directory_is_a_local_first_project(tmp_path: Path) -> None:
    project = tmp_path / "greenfield"
    project.mkdir()

    assert resolve_project_identity(project) == ProjectIdentity.from_root(project)


def test_existing_file_cannot_become_a_project_root(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-project"
    file_path.write_text("data", encoding="utf-8")

    with pytest.raises(ProjectIdentityError, match="directory"):
        resolve_project_identity(file_path)


@pytest.mark.parametrize("raw_path", ["x" * 4097, "bad\x00path"])
def test_unrepresentable_paths_fail_before_filesystem_resolution(raw_path: str) -> None:
    with pytest.raises(ProjectIdentityError, match="path"):
        resolve_project_identity(raw_path)


def test_managed_workspace_uses_durable_source_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_workspace = source / "packages" / "app"
    source_workspace.mkdir(parents=True)
    generated = tmp_path / "managed-worktree" / "packages" / "app"
    generated.mkdir(parents=True)

    identity = resolve_project_identity(
        generated,
        source_root=source,
        source_workspace=source_workspace,
    )

    assert identity.project_id == project_id_for_root(source)
    assert identity.project_root == str(source.resolve())
    assert identity.workspace_path == "packages/app"


def test_managed_workspace_outside_source_root_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    with pytest.raises(ProjectIdentityError, match="outside"):
        resolve_project_identity(
            generated,
            source_root=source,
            source_workspace=outside,
        )


@pytest.mark.parametrize(
    ("project_id", "project_root", "workspace_path", "message"),
    [
        ("project_not-a-uuid", str(Path("/tmp/project").resolve()), ".", "project_id"),
        (
            project_id_for_root("/tmp/project"),
            "/tmp/project/../project",
            ".",
            "project_root",
        ),
        (
            project_id_for_root("/tmp/project"),
            str(Path("/tmp/project").resolve()),
            "../escape",
            "workspace_path",
        ),
        (
            project_id_for_root("/tmp/project"),
            str(Path("/tmp/project").resolve()),
            "a//b",
            "workspace_path",
        ),
    ],
)
def test_identity_rejects_noncanonical_manual_construction(
    project_id: str,
    project_root: str,
    workspace_path: str,
    message: str,
) -> None:
    with pytest.raises(ProjectIdentityError, match=message):
        ProjectIdentity(
            project_id=project_id,
            project_root=project_root,
            workspace_path=workspace_path,
        )
