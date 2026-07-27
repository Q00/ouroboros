"""Tests for the Project Map V1 identity resolver."""

from __future__ import annotations

from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

import pytest

from ouroboros.core.project_identity import (
    ProjectIdentity,
    ProjectIdentityError,
    _parse_git_boolean,
    project_id_for_root,
    resolve_project_identity,
)


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
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


@pytest.mark.parametrize("target_exists", [False, True], ids=["broken", "live"])
def test_git_symlink_entry_stops_parent_checkout_discovery(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    parent = tmp_path / "parent"
    (parent / ".git").mkdir(parents=True)
    child = parent / "nested" / "child"
    workspace = child / "packages" / "app"
    workspace.mkdir(parents=True)
    symlink_target = tmp_path / "untrusted-git-entry"
    if target_exists:
        symlink_target.write_text("not a Git pointer\n", encoding="utf-8")
    (child / ".git").symlink_to(symlink_target)

    identity = resolve_project_identity(workspace)

    assert identity.project_root == str(child.resolve())
    assert identity.project_id == project_id_for_root(child)
    assert identity.workspace_path == "packages/app"


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


def test_external_git_directory_without_owner_keeps_direct_checkout_separate(
    tmp_path: Path,
) -> None:
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

    assert primary_identity.project_root == str(primary.resolve())
    assert linked_identity.project_root == str(external_git.resolve())
    assert primary_identity.project_id != linked_identity.project_id
    assert primary_identity.workspace_path == "packages/web"
    assert linked_identity.workspace_path == "packages/web"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_case_variant_core_sections_use_later_git_value(
    tmp_path: Path,
    line_ending: str,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    first = tmp_path / "first"
    first.mkdir()
    primary = tmp_path / "primary"
    primary.mkdir()
    (first / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        line_ending.join(
            (
                "[CORE] # stale owner",
                "\tbare = false ; non-bare repository",
                f'\tworktree = "{first}" # stale owner',
                "[core] ; later owner",
                f'\tworktree = "{primary}" ; active owner',
                "",
            )
        ),
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    primary_identity = resolve_project_identity(primary)
    linked_identity = resolve_project_identity(linked)

    assert primary_identity == linked_identity
    assert primary_identity.project_root == str(primary.resolve())


def test_same_line_section_assignment_matches_git_grammar(tmp_path: Path) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core] bare = false\n\tworktree = {primary}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    direct_identity = resolve_project_identity(primary)
    linked_identity = resolve_project_identity(linked)

    assert direct_identity == linked_identity
    assert direct_identity.project_root == str(primary.resolve())


def test_valueless_bare_config_joins_direct_and_linked_identity(tmp_path: Path) -> None:
    common_git = tmp_path / "common.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[CORE] # Git boolean shorthand\n\tbare\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    direct_identity = resolve_project_identity(common_git)
    linked_identity = resolve_project_identity(linked)

    assert direct_identity == linked_identity
    assert direct_identity.project_root == str(common_git.resolve())


def test_bare_repository_named_dot_git_joins_direct_linked_and_managed_identity(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "storage" / ".git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()

    direct_identity = resolve_project_identity(common_git)
    linked_identity = resolve_project_identity(linked)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(common_git.resolve())
    assert direct_identity.workspace_path == "."


@pytest.mark.parametrize(
    "head_shape",
    ["contained", "outside", "oversized"],
)
def test_symlink_head_population_controls_bare_common_identity(
    tmp_path: Path,
    head_shape: str,
) -> None:
    common_git = tmp_path / "common.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs" / "heads").mkdir(parents=True)
    (common_git / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    if head_shape == "outside":
        head_target = tmp_path / "outside-head"
        symlink_target = Path("../outside-head")
    else:
        head_target = common_git / "refs" / "heads" / "main"
        symlink_target = Path("refs/heads/main")
    head_target.write_text(
        "a" * (4097 if head_shape == "oversized" else 40),
        encoding="utf-8",
    )
    (common_git / "HEAD").symlink_to(symlink_target)
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()

    direct_identity = resolve_project_identity(common_git)
    linked_identity = resolve_project_identity(linked)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked,
    )

    assert managed_identity == linked_identity
    if head_shape == "contained":
        assert linked_identity == direct_identity
        assert linked_identity.project_root == str(common_git.resolve())
    else:
        assert linked_identity != direct_identity
        assert linked_identity.project_root == str(linked.resolve())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", False),
        ("true", True),
        ("OFF", False),
        ("0", False),
        ("00", False),
        ("+0", False),
        ("-0", False),
        ("0x0", False),
        ("0k", False),
        ("1", True),
        ("01", True),
        ("2", True),
        ("+2", True),
        ("-2", True),
        ("010", True),
        ("0x1", True),
        ("-0x1", True),
        ("1k", True),
        ("1M", True),
        ("1g", True),
        ("2147483647", True),
        ("-2147483648", True),
        ("0" * 5000 + "2", True),
        ("08", None),
        ("1.0", None),
        ("1e2", None),
        ("0x", None),
        ("1t", None),
        ("2g", None),
        ("2147483648", None),
        ("-2147483649", None),
        ("0xffffffff", None),
        ("9" * 5000, None),
        ("falſe", None),
        ("1K", None),
        ("١", None),
        ("２", None),
    ],
)
def test_git_boolean_numeric_grammar_matches_git(
    value: str | None,
    expected: bool | None,
) -> None:
    assert _parse_git_boolean(value) is expected


def test_numeric_bare_config_joins_linked_and_managed_identity(tmp_path: Path) -> None:
    common_git = tmp_path / "common.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[core]\n\tbare = 2\n",
        encoding="utf-8",
    )
    linked_roots: list[Path] = []
    for name in ("linked-a", "linked-b"):
        linked = tmp_path / name
        linked.mkdir()
        linked_git_dir = common_git / "worktrees" / name
        linked_git_dir.mkdir(parents=True)
        (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
        (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        (linked_git_dir / "gitdir").write_text(
            f"{linked / '.git'}\n",
            encoding="utf-8",
        )
        linked_roots.append(linked)
    generated = tmp_path / "generated"
    generated.mkdir()

    common_identity = resolve_project_identity(common_git)
    linked_identities = [resolve_project_identity(linked) for linked in linked_roots]
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked_roots[0],
        source_workspace=linked_roots[0],
    )

    assert all(identity == common_identity for identity in linked_identities)
    assert managed_identity == common_identity
    assert common_identity.project_root == str(common_git.resolve())


def test_non_ascii_bare_boolean_cannot_claim_linked_identity(tmp_path: Path) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    claimed = tmp_path / "claimed"
    claimed.mkdir()
    (claimed / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core]\n\tbare = falſe\n\tworktree = {claimed}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    linked_identity = resolve_project_identity(linked)

    assert linked_identity.project_root == str(linked.resolve())
    assert linked_identity.project_id != project_id_for_root(claimed)


def test_non_ascii_worktree_config_boolean_cannot_enable_owner_overlay(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    claimed = tmp_path / "claimed"
    claimed.mkdir()
    (claimed / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[core]\n\tbare = false\n[extensions]\n\tworktreeConfig = 1K\n",
        encoding="utf-8",
    )
    (common_git / "config.worktree").write_text(
        f"[core]\n\tworktree = {claimed}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    linked_identity = resolve_project_identity(linked)

    assert linked_identity.project_root == str(linked.resolve())
    assert linked_identity.project_id != project_id_for_root(claimed)


def test_quoted_continued_core_worktree_keeps_direct_linked_parity(tmp_path: Path) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    primary = tmp_path / "primary with space"
    primary.mkdir()
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[core]\n"
        "\tbare = false\n"
        f'\tworktree = "{tmp_path}/primary \\\n'
        'with space" # continued quoted path\n',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    direct_identity = resolve_project_identity(primary)
    linked_identity = resolve_project_identity(linked)

    assert direct_identity == linked_identity
    assert direct_identity.project_root == str(primary.resolve())


def test_same_line_continued_worktree_joins_direct_linked_and_managed_identity(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    primary = tmp_path / "primary owner"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[core] bare = false\n"
        f'[core] worktree = "{tmp_path}/primary \\\n'
        'owner" # continued same-line assignment\n',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    direct_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked_workspace,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(primary.resolve())
    assert direct_identity.workspace_path == "packages/app"


@pytest.mark.parametrize(
    ("section_header", "valid"),
    [
        ('[remote "plain"]', True),
        (r'[remote "a\"b"]', True),
        (r'[remote "a\\b"]', True),
        (r'[remote "a\q"]', True),
        ('[remote "a]b"]', True),
        ('[remote "a"junk"b"]', False),
        ('[remote "a" junk]', False),
    ],
    ids=[
        "plain",
        "escaped-quote",
        "escaped-backslash",
        "dropped-backslash",
        "quoted-closing-bracket",
        "raw-inner-quotes",
        "trailing-junk",
    ],
)
def test_complete_subsection_grammar_controls_identity_claims(
    tmp_path: Path,
    section_header: str,
    valid: bool,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    claimed = tmp_path / "claimed"
    claimed.mkdir()
    (claimed / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"{section_header}\n\turl = ignored\n[core]\n\tbare = false\n\tworktree = {claimed}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()

    direct_identity = resolve_project_identity(claimed)
    linked_identity = resolve_project_identity(linked)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked,
    )

    assert managed_identity == linked_identity
    if valid:
        assert linked_identity == direct_identity
    else:
        assert linked_identity != direct_identity
        assert linked_identity.project_root == str(linked.resolve())


@pytest.mark.parametrize(
    ("bom_location", "bom", "valid"),
    [
        ("common", "\ufeff", True),
        ("worktree", "\ufeff", True),
        ("common", "\ufeff\ufeff", False),
        ("worktree", "\ufeff\ufeff", False),
    ],
    ids=[
        "common-single",
        "worktree-single",
        "common-repeated",
        "worktree-repeated",
    ],
)
def test_config_bom_population_controls_explicit_owner_identity(
    tmp_path: Path,
    bom_location: str,
    bom: str,
    valid: bool,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    if bom_location == "common":
        common_config = f"{bom}[core]\n\tbare = false\n\tworktree = {owner}\n"
    else:
        common_config = "[core]\n\tbare = false\n[extensions]\n\tworktreeConfig = true\n"
        (common_git / "config.worktree").write_text(
            f"{bom}[core]\n\tworktree = {owner}\n",
            encoding="utf-8",
        )
    (common_git / "config").write_text(common_config, encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()

    direct_identity = resolve_project_identity(owner)
    linked_identity = resolve_project_identity(linked)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked,
    )

    assert managed_identity == linked_identity
    if valid:
        assert linked_identity == direct_identity
        assert linked_identity.project_root == str(owner.resolve())
    else:
        assert linked_identity != direct_identity
        assert linked_identity.project_root == str(linked.resolve())


@pytest.mark.parametrize(
    "worktree_config_value",
    ["true", "2"],
    ids=["text-boolean", "numeric-boolean"],
)
def test_worktree_config_owner_joins_direct_linked_and_managed_identity(
    tmp_path: Path,
    worktree_config_value: str,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core]\n\tbare = false\n[extensions]\n\tworktreeConfig = {worktree_config_value}\n",
        encoding="utf-8",
    )
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config.worktree").write_text(
        f'[core]\n\tworktree = "{primary}"\n',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    direct_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked_workspace,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(primary.resolve())
    assert direct_identity.workspace_path == "packages/app"


def test_external_dot_git_explicit_owner_joins_direct_linked_and_managed_identity(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "storage" / ".git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core]\n\tbare = false\n\tworktree = {primary}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    direct_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked_workspace,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(primary.resolve())
    assert direct_identity.workspace_path == "packages/app"


def test_standard_dot_git_explicit_owner_joins_direct_linked_and_managed_identity(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    common_git = primary / ".git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core]\n\tbare = false\n\tworktree = {primary}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    direct_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked_workspace,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(primary.resolve())
    assert direct_identity.workspace_path == "packages/app"


def test_directory_marker_honors_explicit_owner_across_all_execution_modes(
    tmp_path: Path,
) -> None:
    metadata_parent = tmp_path / "metadata-parent"
    metadata_workspace = metadata_parent / "packages" / "app"
    metadata_workspace.mkdir(parents=True)
    common_git = metadata_parent / ".git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    owner = tmp_path / "explicit-owner"
    owner_workspace = owner / "packages" / "app"
    owner_workspace.mkdir(parents=True)
    (owner / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[core]\n\tbare = false\n\tworktree = {owner}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    identities = (
        resolve_project_identity(metadata_workspace),
        resolve_project_identity(owner_workspace),
        resolve_project_identity(linked_workspace),
        resolve_project_identity(
            generated,
            source_root=linked,
            source_workspace=linked_workspace,
        ),
    )

    assert all(identity == identities[0] for identity in identities)
    assert identities[0].project_root == str(owner.resolve())
    assert identities[0].workspace_path == "packages/app"


def test_real_standard_dot_git_explicit_owner_joins_all_execution_modes(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    generated = tmp_path / "generated" / "packages" / "app"

    _git("init", "-q", str(primary))
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
    _git("worktree", "add", "-q", "-b", "linked", str(linked), "HEAD", cwd=primary)
    _git("config", "core.worktree", str(primary), cwd=primary)
    primary_workspace = primary / "packages" / "app"
    linked_workspace = linked / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    linked_workspace.mkdir(parents=True)
    generated.mkdir(parents=True)

    direct_identity = resolve_project_identity(primary_workspace)
    linked_identity = resolve_project_identity(linked_workspace)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked_workspace,
    )

    assert direct_identity == linked_identity == managed_identity
    assert direct_identity.project_root == str(primary.resolve())
    assert direct_identity.workspace_path == "packages/app"


def test_tilde_core_worktree_is_literal_and_independent_of_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common_git / "config").write_text(
        "[core]\n\tbare = false\n\tworktree = ~\n",
        encoding="utf-8",
    )
    literal_owner = common_git / "~"
    owner_workspace = literal_owner / "packages" / "app"
    owner_workspace.mkdir(parents=True)
    (literal_owner / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")

    linked = tmp_path / "linked"
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated" / "packages" / "app"
    generated.mkdir(parents=True)

    home_aliases = (tmp_path / "home-one", tmp_path / "home-two")
    for home_alias in home_aliases:
        home_alias.mkdir()
        (home_alias / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")

    identities_by_home: list[tuple[ProjectIdentity, ProjectIdentity, ProjectIdentity]] = []
    alias_identities: list[ProjectIdentity] = []
    for home_alias in home_aliases:
        monkeypatch.setenv("HOME", str(home_alias))
        identities_by_home.append(
            (
                resolve_project_identity(owner_workspace),
                resolve_project_identity(linked_workspace),
                resolve_project_identity(
                    generated,
                    source_root=linked,
                    source_workspace=linked_workspace,
                ),
            )
        )
        alias_identities.append(resolve_project_identity(home_alias))

    expected = identities_by_home[0][0]
    assert all(identity == expected for group in identities_by_home for identity in group)
    assert expected.project_root == str(literal_owner.resolve())
    assert expected.workspace_path == "packages/app"
    assert [identity.project_root for identity in alias_identities] == [
        str(home_alias.resolve()) for home_alias in home_aliases
    ]
    assert all(identity.project_id != expected.project_id for identity in alias_identities)


def test_continued_section_header_cannot_claim_linked_or_managed_identity(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "storage.git"
    (common_git / "objects").mkdir(parents=True)
    (common_git / "refs").mkdir()
    (common_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    claimed = tmp_path / "claimed"
    claimed.mkdir()
    (claimed / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    (common_git / "config").write_text(
        f"[co\\\nre]\n\tbare = false\n\tworktree = {claimed}\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = common_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    generated = tmp_path / "generated"
    generated.mkdir()

    linked_identity = resolve_project_identity(linked)
    managed_identity = resolve_project_identity(
        generated,
        source_root=linked,
        source_workspace=linked,
    )

    assert linked_identity == managed_identity
    assert linked_identity.project_root == str(linked.resolve())
    assert linked_identity.project_id != project_id_for_root(claimed)


def test_bare_common_repository_joins_direct_and_managed_worktrees(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    common_git = tmp_path / "common.git"
    direct_a = tmp_path / "direct-a"
    direct_b = tmp_path / "direct-b"
    generated = tmp_path / "generated" / "packages" / "app"

    _git("init", "-q", str(source))
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
        cwd=source,
    )
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
    workspace_a = direct_a / "packages" / "app"
    workspace_b = direct_b / "packages" / "app"
    workspace_a.mkdir(parents=True)
    workspace_b.mkdir(parents=True)
    generated.mkdir(parents=True)

    direct_identity_a = resolve_project_identity(workspace_a)
    direct_identity_b = resolve_project_identity(workspace_b)
    managed_identity = resolve_project_identity(
        generated,
        source_root=direct_a,
        source_workspace=workspace_a,
    )

    assert direct_identity_a == direct_identity_b == managed_identity
    assert direct_identity_a.project_root == str(common_git.resolve())
    assert direct_identity_a.workspace_path == "packages/app"


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


def test_direct_gitfile_cannot_claim_an_unowned_dot_git_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / ".git").write_text(f"gitdir: {source_git}\n", encoding="utf-8")

    identity = resolve_project_identity(forged)

    assert identity.project_root == str(forged.resolve())
    assert identity.project_id == project_id_for_root(forged)


def test_direct_alias_cannot_claim_configured_submodule_gitdir(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent_git = parent / ".git"
    parent_git.mkdir(parents=True)
    submodule = parent / "vendor" / "child"
    submodule.mkdir(parents=True)
    module_git_dir = parent_git / "modules" / "vendor" / "child"
    module_git_dir.mkdir(parents=True)
    (module_git_dir / "config").write_text(
        "[core]\n\tbare = false\n\tworktree = ../../../../vendor/child\n",
        encoding="utf-8",
    )
    (submodule / ".git").write_text(
        "gitdir: ../../.git/modules/vendor/child\n",
        encoding="utf-8",
    )
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / ".git").write_text(f"gitdir: {module_git_dir}\n", encoding="utf-8")

    submodule_identity = resolve_project_identity(submodule)
    alias_identity = resolve_project_identity(alias)

    assert submodule_identity.project_root == str(submodule.resolve())
    assert alias_identity.project_root == str(alias.resolve())
    assert alias_identity.project_id != submodule_identity.project_id


@pytest.mark.parametrize("record_name", ["marker", "commondir", "backlink"])
@pytest.mark.parametrize(
    "trailing_data",
    ["unexpected-second-record\n", "x" * 4097],
    ids=["trailing-record", "oversized-content"],
)
def test_incomplete_pointer_record_cannot_join_another_project(
    tmp_path: Path,
    record_name: str,
    trailing_data: str,
) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = source_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    records = {
        "marker": (linked / ".git", f"gitdir: {linked_git_dir}\n"),
        "commondir": (linked_git_dir / "commondir", "../..\n"),
        "backlink": (linked_git_dir / "gitdir", f"{linked / '.git'}\n"),
    }
    for name, (path, valid_value) in records.items():
        suffix = trailing_data if name == record_name else ""
        path.write_text(valid_value + suffix, encoding="utf-8")

    identity = resolve_project_identity(linked)

    assert identity.project_root == str(linked.resolve())
    assert identity.project_id == project_id_for_root(linked)


def test_gitfile_target_leading_whitespace_cannot_join_another_project(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = source_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(
        f"gitdir:  {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )

    identity = resolve_project_identity(linked)

    assert identity.project_root == str(linked.resolve())
    assert identity.project_id == project_id_for_root(linked)


def test_symlinked_gitfile_cannot_reuse_another_checkout_backlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_git = source / ".git"
    source_git.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    linked_git_dir = source_git / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        f"{linked / '.git'}\n",
        encoding="utf-8",
    )
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / ".git").symlink_to(linked / ".git")

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


def test_managed_workspace_empty_source_workspace_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()

    with pytest.raises(ProjectIdentityError, match="path"):
        resolve_project_identity(
            generated,
            source_root=source,
            source_workspace="",
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
