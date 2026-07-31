"""Tests for the Git-backed Project Map V1 identity resolver."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import pytest

from ouroboros.core import project_identity
from ouroboros.core.project_identity import (
    ManagedProjectOwnershipError,
    ManagedProjectScopeError,
    ProjectIdentity,
    ProjectIdentityError,
    ProjectIdentityGitVersionError,
    ProjectIdentityUnavailableError,
    _git_environment,
    _git_path,
    _git_project_root,
    _require_supported_git,
    _resolve_canonical_project_identity,
    _run_git,
    project_id_for_root,
    resolve_managed_project_identity,
    resolve_project_identity,
)


def _git(
    *args: str, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git("init", "-q", str(root))
    _git(
        "-c",
        "user.name=Project Identity Test",
        "-c",
        "user.email=project-identity@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
        cwd=root,
    )


def _add_linked(primary: Path, linked: Path, *, branch: str = "linked") -> None:
    _git("worktree", "add", "-q", "-b", branch, str(linked), "HEAD", cwd=primary)


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
    _init_repo(root)
    workspace = root / "packages" / "api"
    workspace.mkdir(parents=True)

    identity = resolve_project_identity(workspace)

    assert identity.project_root == str(root.resolve())
    assert identity.project_id == project_id_for_root(root)
    assert identity.workspace_path == "packages/api"


def test_symlinked_workspace_canonicalizes_before_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    workspace = root / "packages" / "api"
    workspace.mkdir(parents=True)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(root, target_is_directory=True)

    assert resolve_project_identity(alias / "packages" / "api") == resolve_project_identity(
        workspace
    )


@pytest.mark.parametrize("target_exists", [False, True], ids=["broken", "live"])
@pytest.mark.parametrize("parent_kind", ["ordinary", "bare"])
def test_malformed_child_git_entry_stops_parent_discovery(
    tmp_path: Path,
    target_exists: bool,
    parent_kind: str,
) -> None:
    parent = tmp_path / "parent"
    if parent_kind == "bare":
        _git("init", "-q", "--bare", str(parent))
    else:
        _init_repo(parent)
    child = parent / "nested" / "child"
    workspace = child / "packages" / "app"
    workspace.mkdir(parents=True)
    target = tmp_path / "not-a-git-marker"
    if target_exists:
        target.write_text("invalid\n", encoding="utf-8")
    (child / ".git").symlink_to(target)

    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(workspace)


def test_nested_ordinary_checkout_beats_enclosing_bare_repository(tmp_path: Path) -> None:
    parent = tmp_path / "parent.git"
    child = parent / "nested" / "child"
    workspace = child / "packages" / "app"
    _git("init", "-q", "--bare", str(parent))
    _init_repo(child)
    workspace.mkdir(parents=True)

    identity = resolve_project_identity(workspace)

    assert identity.project_root == str(child.resolve())
    assert identity.workspace_path == "packages/app"


def test_explicit_git_marker_wins_tie_with_bare_shape(tmp_path: Path) -> None:
    repository = tmp_path / "repository.git"
    _git("init", "-q", "--bare", str(repository))
    (repository / ".git").write_text("invalid\n", encoding="utf-8")

    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(repository)


def test_standard_linked_and_managed_worktrees_share_primary_identity(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    generated = tmp_path / "generated"
    _init_repo(primary)
    _add_linked(primary, linked)
    _add_linked(primary, generated, branch="generated")
    primary_workspace = primary / "packages" / "app"
    linked_workspace = linked / "packages" / "app"
    generated_workspace = generated / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    linked_workspace.mkdir(parents=True)
    generated_workspace.mkdir(parents=True)

    direct = resolve_project_identity(primary_workspace)
    linked_direct = resolve_project_identity(linked_workspace)
    managed = resolve_managed_project_identity(
        generated_workspace,
        source_root=linked,
        source_workspace=linked_workspace,
        worktree_root=generated,
    )

    assert direct == linked_direct == managed
    assert direct.project_root == str(primary.resolve())
    assert direct.workspace_path == "packages/app"


def test_newline_bearing_git_paths_preserve_direct_linked_and_managed_identity(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary\nrepo"
    linked = tmp_path / "linked\nrepo"
    generated = tmp_path / "generated\nrepo"
    _init_repo(primary)
    _add_linked(primary, linked)
    _add_linked(primary, generated, branch="generated")
    primary_workspace = primary / "packages" / "app"
    linked_workspace = linked / "packages" / "app"
    generated_workspace = generated / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    linked_workspace.mkdir(parents=True)
    generated_workspace.mkdir(parents=True)

    direct = resolve_project_identity(primary_workspace)
    linked_direct = resolve_project_identity(linked_workspace)
    managed = resolve_managed_project_identity(
        generated_workspace,
        source_root=linked,
        source_workspace=linked_workspace,
        worktree_root=generated,
    )

    assert direct == linked_direct == managed
    assert direct.project_root == str(primary.resolve())
    assert direct.workspace_path == "packages/app"


def test_unowned_separate_git_directory_fails_closed(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    common_git = tmp_path / "metadata.git"
    linked = tmp_path / "linked"
    primary.mkdir()
    _git("init", "-q", f"--separate-git-dir={common_git}", str(primary))
    _git(
        "-c",
        "user.name=Project Identity Test",
        "-c",
        "user.email=project-identity@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
        cwd=primary,
    )
    _add_linked(primary, linked)

    primary_identity = resolve_project_identity(primary)
    linked_identity = resolve_project_identity(linked)

    assert primary_identity.project_root == str(primary.resolve())
    assert linked_identity.project_root == str(common_git.resolve())
    assert primary_identity != linked_identity


def test_explicit_core_worktree_owner_is_resolved_by_git(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    owner = tmp_path / "owner"
    linked = tmp_path / "linked"
    _init_repo(holder)
    _add_linked(holder, linked)
    owner.mkdir()
    (owner / ".git").write_text(f"gitdir: {holder / '.git'}\n", encoding="utf-8")
    _git("config", "core.worktree", str(owner), cwd=holder)
    for checkout in (holder, owner, linked):
        (checkout / "packages" / "app").mkdir(parents=True)

    identities = [
        resolve_project_identity(checkout / "packages" / "app")
        for checkout in (holder, owner, linked)
    ]

    assert identities[0] == identities[1] == identities[2]
    assert identities[0].project_root == str(owner.resolve())
    assert identities[0].workspace_path == "packages/app"


@pytest.mark.parametrize("bare_owner", [False, True], ids=["standard", "bare"])
@pytest.mark.parametrize("marker_kind", ["gitfile", "symlink"])
@pytest.mark.parametrize("nested_under_owner", [False, True], ids=["external", "nested"])
def test_unowned_git_marker_cannot_adopt_another_repository(
    tmp_path: Path,
    marker_kind: str,
    bare_owner: bool,
    nested_under_owner: bool,
) -> None:
    owner = tmp_path / "owner"
    if bare_owner:
        _git("init", "-q", "--bare", str(owner))
        owner_git_dir = owner
    else:
        _init_repo(owner)
        owner_git_dir = owner / ".git"
    unowned = (owner if nested_under_owner else tmp_path) / "unowned"
    unowned_workspace = unowned / "packages" / "app"
    generated = tmp_path / "generated"
    generated_workspace = generated / "packages" / "app"
    unowned_workspace.mkdir(parents=True)
    generated_workspace.mkdir(parents=True)
    marker = unowned / ".git"
    if marker_kind == "gitfile":
        marker.write_text(f"gitdir: {owner_git_dir}\n", encoding="utf-8")
    else:
        marker.symlink_to(owner_git_dir, target_is_directory=True)

    direct = resolve_project_identity(unowned_workspace)
    with pytest.raises(ManagedProjectOwnershipError):
        resolve_managed_project_identity(
            generated_workspace,
            source_root=unowned,
            source_workspace=unowned_workspace,
            worktree_root=generated,
        )

    assert direct.project_root == str(unowned.resolve())
    assert direct.workspace_path == "packages/app"
    assert direct.project_id != project_id_for_root(owner)


def test_markerless_bare_candidate_cannot_redirect_common_directory(tmp_path: Path) -> None:
    owner = tmp_path / "owner.git"
    redirected = owner / "redirected.git"
    generated = tmp_path / "generated"
    _git("init", "-q", "--bare", str(owner))
    _git("init", "-q", "--bare", str(redirected))
    (redirected / "commondir").write_text(f"{owner}\n", encoding="utf-8")
    generated.mkdir()

    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(redirected)
    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_managed_project_identity(
            generated,
            source_root=redirected,
            source_workspace=redirected,
            worktree_root=generated,
        )


def test_git_owns_bom_tab_and_value_continuation_grammar(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    owner = tmp_path / "primary owner"
    linked = tmp_path / "linked"
    _init_repo(holder)
    _add_linked(holder, linked)
    owner.mkdir()
    (owner / ".git").write_text(f"gitdir: {holder / '.git'}\n", encoding="utf-8")
    config = holder / ".git" / "config"
    raw_config = config.read_text(encoding="utf-8")
    raw_config += (
        "[core]\n"
        "\tbare\t=\tfalse\n"
        f'\tworktree = "{tmp_path}/primary \\\nowner" # Git folds this value\n'
    )
    config.write_bytes(b"\xef\xbb\xbf" + raw_config.encode("utf-8"))
    holder_workspace = holder / "packages" / "app"
    owner_workspace = owner / "packages" / "app"
    linked_workspace = linked / "packages" / "app"
    holder_workspace.mkdir(parents=True)
    owner_workspace.mkdir(parents=True)
    linked_workspace.mkdir(parents=True)

    identities = (
        resolve_project_identity(holder_workspace),
        resolve_project_identity(owner_workspace),
        resolve_project_identity(linked_workspace),
        resolve_managed_project_identity(
            holder_workspace,
            source_root=linked,
            source_workspace=linked_workspace,
            worktree_root=holder,
        ),
    )

    assert all(identity == identities[0] for identity in identities)
    assert identities[0].project_root == str(owner.resolve())


def test_installed_git_owns_form_feed_config_decision(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    owner = tmp_path / "owner"
    linked = tmp_path / "linked"
    _init_repo(holder)
    _add_linked(holder, linked)
    owner.mkdir()
    (owner / ".git").write_text(f"gitdir: {holder / '.git'}\n", encoding="utf-8")
    with (holder / ".git" / "config").open("a", encoding="utf-8") as stream:
        stream.write(f"\f[core]\n\tworktree = {owner}\n")

    git_decision = _git(
        "config",
        "--local",
        "--path",
        "--get",
        "core.worktree",
        cwd=holder,
        check=False,
    )
    if git_decision.returncode == 0:
        identities = [resolve_project_identity(checkout) for checkout in (holder, owner, linked)]
        assert identities[0] == identities[1] == identities[2]
        assert identities[0].project_root == str(owner.resolve())
    else:
        for checkout in (holder, owner, linked):
            with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
                resolve_project_identity(checkout)


@pytest.mark.parametrize(
    "invalid_config",
    [
        '[core ]\n\tworktree = "/tmp/claimed"\n',
        "[core]\n\tbare # malformed valueless assignment\n",
    ],
    ids=["section-whitespace", "bare-comment"],
)
def test_git_rejection_of_malformed_config_cannot_join_worktrees(
    tmp_path: Path,
    invalid_config: str,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_repo(primary)
    _add_linked(primary, linked)
    with (primary / ".git" / "config").open("a", encoding="utf-8") as stream:
        stream.write(invalid_config)

    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(linked)


def test_bare_repository_and_its_worktrees_share_common_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    common_git = tmp_path / "common.git"
    direct_a = tmp_path / "direct-a"
    direct_b = tmp_path / "direct-b"
    _init_repo(source)
    _git("clone", "-q", "--bare", str(source), str(common_git))
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "direct-a",
        str(direct_a),
        "HEAD",
    )
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "direct-b",
        str(direct_b),
        "HEAD",
    )

    identities = [
        resolve_project_identity(checkout) for checkout in (common_git, direct_a, direct_b)
    ]

    assert identities[0] == identities[1] == identities[2]
    assert identities[0].project_root == str(common_git.resolve())


def test_nested_markerless_bare_repository_beats_enclosing_checkout(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    source = tmp_path / "source"
    common_git = outer / "vendor" / "source.git"
    linked = tmp_path / "linked"
    _init_repo(outer)
    _init_repo(source)
    common_git.parent.mkdir()
    _git("clone", "-q", "--bare", str(source), str(common_git))
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked),
        "HEAD",
    )

    direct = resolve_project_identity(common_git)
    linked_direct = resolve_project_identity(linked)
    managed = resolve_managed_project_identity(
        linked,
        source_root=common_git,
        source_workspace=common_git,
        worktree_root=linked,
    )

    assert direct == linked_direct == managed
    assert direct.project_root == str(common_git.resolve())
    assert direct.project_id != project_id_for_root(outer)


def test_malformed_bare_head_cannot_join_linked_worktree(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    source = tmp_path / "source"
    common_git = outer / "vendor" / "common.git"
    linked = tmp_path / "linked"
    _init_repo(outer)
    _init_repo(source)
    common_git.parent.mkdir()
    _git("clone", "-q", "--bare", str(source), str(common_git))
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked),
        "HEAD",
    )
    (common_git / "HEAD").write_text("not-a-ref-or-object-id\n", encoding="utf-8")

    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(common_git)
    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_project_identity(linked)
    with pytest.raises(ProjectIdentityUnavailableError, match="topology"):
        resolve_managed_project_identity(
            linked,
            source_root=common_git,
            source_workspace=common_git,
            worktree_root=linked,
        )


def test_bare_repository_named_dot_git_is_not_its_parent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    common_git = tmp_path / "storage" / ".git"
    linked = tmp_path / "linked"
    _init_repo(source)
    _git("clone", "-q", "--bare", str(source), str(common_git))
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked),
        "HEAD",
    )

    assert resolve_project_identity(common_git) == resolve_project_identity(linked)
    assert resolve_project_identity(common_git).project_root == str(common_git.resolve())


def test_real_submodule_remains_a_separate_project(tmp_path: Path) -> None:
    child_source = tmp_path / "child-source"
    parent = tmp_path / "parent"
    _init_repo(child_source)
    _init_repo(parent)
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child_source),
        "vendor/child",
        cwd=parent,
    )
    child = parent / "vendor" / "child"

    child_identity = resolve_project_identity(child)

    assert child_identity.project_root == str(child.resolve())
    assert child_identity.project_id != project_id_for_root(parent)


def test_git_invocation_strips_overrides_and_uses_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/forged")
    main = tmp_path / "main"
    owner = tmp_path / "owner"
    (main / ".git").mkdir(parents=True)
    owner.mkdir()
    outputs = iter(
        (
            f"{main / '.git'}\n".encode(),
            b"false\n",
            f"worktree {main}\0\0".encode(),
            f"{owner}\n".encode(),
        )
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = kwargs["stdout"]
        output.write(next(outputs))  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args[0], 0)

    with patch("ouroboros.core.project_identity.subprocess.run", side_effect=fake_run) as run:
        assert _git_project_root(tmp_path) == owner.resolve()

    assert run.call_count == 4
    for call in run.call_args_list:
        assert call.kwargs["shell"] is False
        assert call.kwargs["timeout"] == 5.0
        assert "GIT_DIR" not in call.kwargs["env"]
        assert call.kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_top_level_query_stays_bound_to_validated_common_directory(tmp_path: Path) -> None:
    ancestor = tmp_path / "unrelated-ancestor"
    common_git = ancestor / "external-git-storage"
    primary = ancestor / "reported-primary-without-marker"
    linked = tmp_path / "linked"
    for directory in (common_git, primary, linked):
        directory.mkdir(parents=True)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command = args[0]
        output = kwargs["stdout"]
        if "--git-common-dir" in command:
            value = f"{common_git}\n".encode()
        elif "--is-bare-repository" in command:
            value = b"false\n"
        elif "worktree" in command:
            value = f"worktree {primary}\0worktree {linked}\0".encode()
        elif f"--git-dir={common_git}" in command:
            value = f"{primary}\n".encode()
        else:
            value = f"{ancestor}\n".encode()
        output.write(value)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(command, 0)

    with patch("ouroboros.core.project_identity.subprocess.run", side_effect=fake_run):
        assert _git_project_root(linked, linked) == primary.resolve()


def test_git_output_is_bounded(tmp_path: Path) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = kwargs["stdout"]
        output.write(b"x" * 1_048_577)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args[0], 0)

    with patch("ouroboros.core.project_identity.subprocess.run", side_effect=fake_run):
        with pytest.raises(ProjectIdentityUnavailableError, match="output"):
            _run_git(tmp_path, "rev-parse", "--show-toplevel")


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired(["git", "--version"], timeout=5),
    ],
    ids=["missing", "timeout"],
)
def test_git_operational_failure_cannot_become_a_fallback_identity(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    with (
        patch("ouroboros.core.project_identity.subprocess.run", side_effect=failure),
        pytest.raises(ProjectIdentityUnavailableError, match="unavailable"),
    ):
        resolve_project_identity(tmp_path)


@pytest.mark.parametrize(
    "output",
    [
        b"git version 2.35.9\n",
        b"git version unknown\n",
        b"git version 9999999999.36.0\n",
    ],
    ids=["too-old", "unrepresentable", "unbounded-component"],
)
def test_unsupported_git_is_a_non_retryable_configuration_error(
    output: bytes,
) -> None:
    with (
        patch("ouroboros.core.project_identity._run_git_command", return_value=output),
        pytest.raises(ProjectIdentityGitVersionError, match="2.36.0") as exc_info,
    ):
        _require_supported_git()

    assert not isinstance(exc_info.value, ProjectIdentityUnavailableError)


@pytest.mark.parametrize(
    "output",
    [
        b"git version 2.36.0\n",
        b"git version 2.36.0.windows.1\n",
        b"git version 2.39.5 (Apple Git-154)\n",
        b"git version 3.0.0\n",
    ],
)
def test_supported_git_version_grammar_is_explicit(output: bytes) -> None:
    with patch("ouroboros.core.project_identity._run_git_command", return_value=output):
        _require_supported_git()


def test_git_version_probe_does_not_depend_on_workdir_capability() -> None:
    with patch(
        "ouroboros.core.project_identity._run_git_command",
        return_value=b"git version 2.36.0\n",
    ) as run_git_command:
        _require_supported_git()

    assert run_git_command.call_count == 1
    assert run_git_command.call_args.args == ("--version",)


def test_repository_query_nonzero_after_git_probe_is_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command = args[0]
        output = kwargs["stdout"]
        if "--version" in command:
            output.write(b"git version 2.50.0\n")  # type: ignore[union-attr]
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 128)

    with (
        patch("ouroboros.core.project_identity.subprocess.run", side_effect=fake_run),
        pytest.raises(ProjectIdentityUnavailableError, match="topology"),
    ):
        resolve_project_identity(repo)


def test_linked_identity_does_not_drift_when_git_disappears_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_repo(primary)
    _add_linked(primary, linked)
    stable = resolve_project_identity(linked)

    with monkeypatch.context() as context:
        context.setenv("PATH", str(tmp_path / "empty-path"))
        with pytest.raises(ProjectIdentityUnavailableError, match="unavailable"):
            resolve_project_identity(linked)

    assert resolve_project_identity(linked) == stable


def test_git_path_removes_only_the_final_terminator(tmp_path: Path) -> None:
    root = tmp_path / "project \nwith-newline"
    root.mkdir()

    assert _git_path(f"{root}\n".encode()) == root.resolve()
    with pytest.raises(ProjectIdentityUnavailableError, match="representable"):
        _git_path(str(root).encode())
    with pytest.raises(ProjectIdentityUnavailableError, match="representable"):
        _git_path(f"{root}\x00\n".encode())
    with pytest.raises(ProjectIdentityUnavailableError, match="representable"):
        _git_path(f"{tmp_path / 'missing'}\n".encode())


def test_git_environment_disables_global_and_system_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/forged")

    environment = _git_environment()

    assert "GIT_WORK_TREE" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_git_queries_are_independent_of_process_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    _init_repo(repo)
    home_a.mkdir()
    home_b.mkdir()
    _git("config", "include.path", "~/.identity-owner", cwd=repo)

    monkeypatch.setenv("HOME", str(home_a))
    first = _run_git(repo, "config", "--path", "--get", "include.path")
    first_identity = resolve_project_identity(repo)
    monkeypatch.setenv("HOME", str(home_b))
    second = _run_git(repo, "config", "--path", "--get", "include.path")
    second_identity = resolve_project_identity(repo)

    assert first == second
    assert str(home_a).encode() not in (first or b"")
    assert str(home_b).encode() not in (second or b"")
    assert first_identity == second_identity


def test_non_git_directory_is_a_local_first_project(tmp_path: Path) -> None:
    project = tmp_path / "greenfield"
    project.mkdir()

    assert resolve_project_identity(project) == ProjectIdentity.from_root(project)


def test_direct_resolution_revalidates_live_evidence_after_topology(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def resolve_then_delete(effective: Path):
        resolved = _resolve_canonical_project_identity(effective)
        workspace.rmdir()
        return resolved

    with (
        patch(
            "ouroboros.core.project_identity._resolve_canonical_project_identity",
            side_effect=resolve_then_delete,
        ),
        pytest.raises(ProjectIdentityError, match="directory"),
    ):
        resolve_project_identity(workspace)


def test_direct_resolution_rejects_same_path_directory_replacement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    displaced = tmp_path / "displaced"
    workspace.mkdir()

    def resolve_then_replace(effective: Path):
        resolved = _resolve_canonical_project_identity(effective)
        workspace.rename(displaced)
        workspace.mkdir()
        return resolved

    with (
        patch(
            "ouroboros.core.project_identity._resolve_canonical_project_identity",
            side_effect=resolve_then_replace,
        ),
        pytest.raises(ProjectIdentityError, match="changed"),
    ):
        resolve_project_identity(workspace)


def test_existing_file_cannot_become_a_project_root(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-project"
    file_path.write_text("data", encoding="utf-8")

    with pytest.raises(ProjectIdentityError, match="directory"):
        resolve_project_identity(file_path)


def test_missing_directory_cannot_become_a_fresh_project_root(tmp_path: Path) -> None:
    with pytest.raises(ProjectIdentityError, match="directory"):
        resolve_project_identity(tmp_path / "missing")


def test_filesystem_oserror_is_retryable_identity_unavailability(tmp_path: Path) -> None:
    with (
        patch("ouroboros.core.project_identity.Path.resolve", side_effect=OSError("mount")),
        pytest.raises(ProjectIdentityUnavailableError, match="temporarily unavailable"),
    ):
        resolve_project_identity(tmp_path)


@pytest.mark.parametrize("raw_path", ["x" * 4097, "bad\x00path"])
def test_unrepresentable_paths_fail_before_git(raw_path: str) -> None:
    with pytest.raises(ProjectIdentityError, match="path"):
        resolve_project_identity(raw_path)


def test_direct_resolver_cannot_accept_caller_supplied_source_attribution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    unrelated_execution = tmp_path / "unrelated"
    source.mkdir()
    unrelated_execution.mkdir()

    with pytest.raises(TypeError, match="source_root"):
        resolve_project_identity(  # type: ignore[call-arg]
            unrelated_execution,
            source_root=source,
            source_workspace=source,
        )

    identity = resolve_project_identity(unrelated_execution)
    assert identity.project_root == str(unrelated_execution.resolve())
    assert identity.project_id != project_id_for_root(source)


def test_managed_workspace_uses_durable_source_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    _init_repo(source)
    _add_linked(source, generated, branch="generated")
    source_workspace = source / "packages" / "app"
    generated_workspace = generated / "packages" / "app"
    source_workspace.mkdir(parents=True)
    generated_workspace.mkdir(parents=True)

    identity = resolve_managed_project_identity(
        generated_workspace,
        source_root=source,
        source_workspace=source_workspace,
        worktree_root=generated,
    )

    assert identity.project_root == str(source.resolve())
    assert identity.workspace_path == "packages/app"


def test_managed_nested_source_root_cannot_collapse_proven_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    _init_repo(source)
    _add_linked(source, generated, branch="generated")
    nested_source = source / "packages" / "app"
    nested_source.mkdir(parents=True)

    with pytest.raises(ManagedProjectScopeError, match="scopes do not match"):
        resolve_managed_project_identity(
            generated,
            source_root=nested_source,
            source_workspace=nested_source,
            worktree_root=generated,
        )


@pytest.mark.parametrize(
    "removed",
    ["source_root", "source_workspace", "worktree_root", "execution_workspace"],
)
def test_managed_resolution_revalidates_complete_live_population(
    tmp_path: Path,
    removed: str,
) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    _init_repo(source)
    _add_linked(source, generated, branch="generated")
    source_workspace = source / "packages" / "app"
    execution_workspace = generated / "packages" / "app"
    source_workspace.mkdir(parents=True)
    execution_workspace.mkdir(parents=True)
    targets = {
        "source_root": source,
        "source_workspace": source_workspace,
        "worktree_root": generated,
        "execution_workspace": execution_workspace,
    }

    def resolve_then_delete(effective: Path):
        resolved = _resolve_canonical_project_identity(effective)
        target = targets[removed]
        if target.exists():
            shutil.rmtree(target)
        return resolved

    with (
        patch(
            "ouroboros.core.project_identity._resolve_canonical_project_identity",
            side_effect=resolve_then_delete,
        ),
        pytest.raises(ProjectIdentityError, match="directory"),
    ):
        resolve_managed_project_identity(
            execution_workspace,
            source_root=source,
            source_workspace=source_workspace,
            worktree_root=generated,
        )


def test_managed_workspace_outside_source_root_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    outside = tmp_path / "outside"
    source.mkdir()
    generated.mkdir()
    outside.mkdir()

    with pytest.raises(ManagedProjectScopeError, match="scopes do not match"):
        resolve_managed_project_identity(
            generated,
            source_root=source,
            source_workspace=outside,
            worktree_root=generated,
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
    ],
)
def test_project_identity_rejects_noncanonical_fields(
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


class TestPublicationEvidence:
    """#1796 L2: publication accepts by metadata closure or escalates."""

    def _repo(self, tmp_path):
        import subprocess as sp

        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        sp.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "sub").mkdir()
        return repo

    def test_happy_path_is_stable_and_runs_no_git(self, tmp_path, monkeypatch) -> None:
        repo = self._repo(tmp_path)
        identity, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
        assert identity == project_identity.resolve_project_identity(repo / "sub")
        assert evidence.escalate is False

        spawns = {"n": 0}
        real_run = project_identity.subprocess.run

        def counting_run(*args, **kwargs):  # noqa: ANN001, ANN202
            spawns["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(project_identity.subprocess, "run", counting_run)
        assert project_identity.publication_evidence_is_stable(evidence, repo / "sub") is True
        assert spawns["n"] == 0, "the fast path must not spawn any git subprocess"

    def test_each_closure_mutation_destabilizes(self, tmp_path) -> None:
        cases = []

        def case(name):
            def register(fn):
                cases.append((name, fn))
                return fn

            return register

        @case("config content edited in place")
        def _(repo):
            config = repo / ".git" / "config"
            config.write_text(config.read_text() + "[user]\n\tname = drift\n")

        @case("commondir appears (worktree constellation)")
        def _(repo):
            (repo / ".git" / "commondir").write_text("../..\n")

        @case("new intermediate boundary marker")
        def _(repo):
            (repo / "sub" / ".git").mkdir()

        @case("HEAD rewritten")
        def _(repo):
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/elsewhere\n")

        @case("worktree registry appears")
        def _(repo):
            (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
            (repo / ".git" / "worktrees" / "wt" / "gitdir").write_text("x\n")

        @case("worktree-scoped config appears")
        def _(repo):
            (repo / ".git" / "config.worktree").write_text("[core]\n\tbare = true\n")

        for name, mutate in cases:
            repo = self._repo(tmp_path / name.replace(" ", "-"))
            _, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
            assert evidence.escalate is False, name
            assert (
                project_identity.publication_evidence_is_stable(evidence, repo / "sub") is True
            ), name
            mutate(repo)
            assert (
                project_identity.publication_evidence_is_stable(evidence, repo / "sub") is False
            ), f"mutation not detected: {name}"

    def test_unenumerable_shapes_escalate_at_capture(self, tmp_path) -> None:
        import subprocess as sp

        include_repo = self._repo(tmp_path / "inc")
        config = include_repo / ".git" / "config"
        config.write_text(config.read_text() + "[include]\n\tpath = other\n")
        _, evidence = project_identity.resolve_project_identity_for_publication(
            include_repo / "sub"
        )
        assert evidence.escalate is True

        linked_base = self._repo(tmp_path / "base")
        sp.run(
            [
                "git",
                "-C",
                str(linked_base),
                "-c",
                "user.name=Project Identity Test",
                "-c",
                "user.email=project-identity@example.com",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "x",
            ],
            check=True,
        )
        linked = tmp_path / "linked"
        sp.run(
            ["git", "-C", str(linked_base), "worktree", "add", "-q", str(linked)],
            check=True,
        )
        _, linked_evidence = project_identity.resolve_project_identity_for_publication(linked)
        assert linked_evidence.escalate is True, (
            "a gitdir-pointer checkout must not take the fast path"
        )

    def test_mutation_between_resolution_and_capture_escalates(self, tmp_path, monkeypatch) -> None:
        """Evidence must be bound to the exact resolution that produced it.

        A closure mutated after the resolver's queries but before the capture
        would otherwise record the new state against the old identity; the
        pre/post capture bracket detects the interleaving and escalates.
        """
        repo = self._repo(tmp_path)
        original = project_identity._resolve_canonical_project_identity

        def resolve_then_mutate(effective):
            resolved = original(effective)
            config = repo / ".git" / "config"
            config.write_text(config.read_text() + "[user]\n\tname = other-owner\n")
            return resolved

        monkeypatch.setattr(
            project_identity, "_resolve_canonical_project_identity", resolve_then_mutate
        )
        _, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
        assert evidence.escalate is True, (
            "a closure change between resolution and capture must not produce usable evidence"
        )

    def test_worktree_scoped_config_is_part_of_the_closure(self, tmp_path) -> None:
        """config.worktree can carry topology-affecting values; pin it."""
        repo = self._repo(tmp_path / "edit")
        worktree_config = repo / ".git" / "config.worktree"
        worktree_config.write_text("[core]\n")
        _, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
        assert evidence.escalate is False
        assert project_identity.publication_evidence_is_stable(evidence, repo / "sub") is True
        worktree_config.write_text("[core]\n\tbare = true\n")
        assert project_identity.publication_evidence_is_stable(evidence, repo / "sub") is False, (
            "an in-place config.worktree edit must destabilize the evidence"
        )

        include_repo = self._repo(tmp_path / "include")
        (include_repo / ".git" / "config.worktree").write_text("[include]\n\tpath = other\n")
        _, include_evidence = project_identity.resolve_project_identity_for_publication(
            include_repo / "sub"
        )
        assert include_evidence.escalate is True

    def test_include_detection_is_position_and_bom_insensitive(self, tmp_path) -> None:
        """Include escalation must over-approximate Git's grammar, never under."""
        bom_repo = self._repo(tmp_path / "bom")
        (bom_repo / ".git" / "config").write_bytes(b"\xef\xbb\xbf[include]\n\tpath = other\n")
        _, bom_evidence = project_identity.resolve_project_identity_for_publication(
            bom_repo / "sub"
        )
        assert bom_evidence.escalate is True, "a BOM must not hide an include section"

        mid_repo = self._repo(tmp_path / "mid")
        config = mid_repo / ".git" / "config"
        config.write_text(config.read_text() + '[includeIf "gitdir:/x"]\n\tpath = other\n')
        _, mid_evidence = project_identity.resolve_project_identity_for_publication(
            mid_repo / "sub"
        )
        assert mid_evidence.escalate is True

    def test_incomplete_or_forged_evidence_is_refused(self, tmp_path) -> None:
        """Acceptance rests on what the evidence proves, not where it came from."""
        import dataclasses

        repo = self._repo(tmp_path)
        identity, real = project_identity.resolve_project_identity_for_publication(repo / "sub")

        forged = project_identity.PublicationEvidence(
            identity=identity,
            effective_directory=real.effective_directory,
            directory_evidence=(),
            file_evidence=(),
            escalate=False,
        )
        assert project_identity.publication_evidence_is_stable(forged, repo / "sub") is False, (
            "an empty closure must never be accepted"
        )

        for index in range(len(real.file_evidence)):
            partial = dataclasses.replace(
                real,
                file_evidence=real.file_evidence[:index] + real.file_evidence[index + 1 :],
            )
            dropped = real.file_evidence[index].path
            assert (
                project_identity.publication_evidence_is_stable(partial, repo / "sub") is False
            ), f"dropping {dropped} from the closure must be refused"

    def test_caller_assembled_closure_is_refused_even_when_structurally_complete(
        self, tmp_path
    ) -> None:
        """Trust comes from issuance, not shape.

        A closure assembled with the capture helpers over an invalid marker
        shape matches the live filesystem entry for entry, but the resolver
        never issued it — acceptance must refuse it and leave the shape to
        the full re-resolution, which fails closed.
        """
        fake_root = (tmp_path / "fake").resolve()
        fake_root.mkdir()
        git_dir = fake_root / ".git"
        git_dir.mkdir()
        identity = project_identity.ProjectIdentity.from_root(
            fake_root, workspace_path=".", require_exists=True
        )
        forged_files = [project_identity._capture_topology_file(git_dir, hash_content=True)]
        for name in project_identity._GIT_DIR_CLOSURE_NAMES:
            forged_files.append(
                project_identity._capture_topology_file(git_dir / name, hash_content=True)
            )
        forged_files.append(
            project_identity._capture_topology_file(git_dir / "worktrees", hash_content=False)
        )
        forged = project_identity.PublicationEvidence(
            identity=identity,
            effective_directory=fake_root,
            directory_evidence=(project_identity._capture_live_directory(fake_root),),
            file_evidence=tuple(forged_files),
            escalate=False,
        )

        assert project_identity.publication_evidence_is_stable(forged, fake_root) is False
        with pytest.raises(ProjectIdentityUnavailableError):
            project_identity.resolve_project_identity(fake_root)

    def test_escalating_evidence_is_never_stable(self, tmp_path) -> None:
        repo = self._repo(tmp_path)
        _, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
        forced = project_identity.PublicationEvidence(
            identity=evidence.identity,
            effective_directory=evidence.effective_directory,
            directory_evidence=evidence.directory_evidence,
            file_evidence=evidence.file_evidence,
            escalate=True,
        )
        assert project_identity.publication_evidence_is_stable(forced, repo / "sub") is False

    def test_evidence_vouches_only_for_its_own_workspace(self, tmp_path) -> None:
        repo = self._repo(tmp_path)
        _, evidence = project_identity.resolve_project_identity_for_publication(repo / "sub")
        assert project_identity.publication_evidence_is_stable(evidence, repo / "sub") is True
        assert project_identity.publication_evidence_is_stable(evidence, repo) is False, (
            "evidence must not transfer to a different effective directory"
        )
