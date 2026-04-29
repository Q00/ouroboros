"""Unit tests for brownfield registry — DB-backed business logic."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.brownfield import (
    _SKIP_DIRS,
    BrownfieldEntry,
    _has_github_origin,
    _has_origin_remote,
    _read_readme_content,
    generate_desc,
    register_repo,
    scan_and_register,
    scan_home_for_repos,
    set_default_repo,
)
from ouroboros.core.errors import ProviderError
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore


def _git(args: list[str], cwd: Path | None = None) -> None:
    """Run a git command for tests and fail with stderr on errors."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", str(repo)])
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=repo,
    )


def _init_repo_with_origin(
    repo: Path,
    origin: str = "https://github.com/user/repo.git",
) -> None:
    _init_repo(repo)
    _git(["remote", "add", "origin", origin], cwd=repo)


# ── BrownfieldEntry (re-exported BrownfieldRepo) ──────────────────


class TestBrownfieldEntry:
    """Tests for the BrownfieldEntry alias (backward compat)."""

    def test_alias_is_brownfield_repo(self) -> None:
        assert BrownfieldEntry is BrownfieldRepo

    def test_create_with_defaults(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="my-repo")
        assert entry.path == "/repo"
        assert entry.name == "my-repo"
        assert entry.desc is None

    def test_to_dict(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="proj", desc="desc")
        d = entry.to_dict()
        assert d == {"path": "/repo", "name": "proj", "desc": "desc", "is_default": False}

    def test_from_dict_valid(self) -> None:
        data = {"path": "/repo", "name": "proj", "desc": "hello"}
        entry = BrownfieldEntry.from_dict(data)
        assert entry.path == "/repo"
        assert entry.name == "proj"

    def test_from_dict_missing_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path"):
            BrownfieldEntry.from_dict({"name": "proj"})

    def test_from_dict_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            BrownfieldEntry.from_dict({"path": "/repo"})

    def test_from_dict_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path.*empty"):
            BrownfieldEntry.from_dict({"path": "", "name": "proj"})

    def test_from_dict_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name.*empty"):
            BrownfieldEntry.from_dict({"path": "/repo", "name": "  "})


# ── _has_github_origin ─────────────────────────────────────────────


class TestHasOriginRemote:
    """Tests for origin-remote helpers."""

    def test_returns_true_for_github_origin(self, tmp_path: Path) -> None:
        # Create a real git repo with a GitHub origin
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "remote",
                "add",
                "origin",
                "https://github.com/user/repo.git",
            ],
            capture_output=True,
        )
        assert _has_origin_remote(tmp_path) is True
        assert _has_github_origin(tmp_path) is True

    def test_returns_true_only_for_origin_remote_on_non_github_origin(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "remote",
                "add",
                "origin",
                "https://gitlab.com/user/repo.git",
            ],
            capture_output=True,
        )
        assert _has_origin_remote(tmp_path) is True
        assert _has_github_origin(tmp_path) is False

    def test_github_helper_returns_true_for_ssh_origin(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "remote",
                "add",
                "origin",
                "git@github.com:user/repo.git",
            ],
            capture_output=True,
        )
        assert _has_origin_remote(tmp_path) is True
        assert _has_github_origin(tmp_path) is True

    def test_github_helper_rejects_lookalike_hosts(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "remote",
                "add",
                "origin",
                "https://github.com.example.com/user/repo.git",
            ],
            capture_output=True,
        )
        assert _has_origin_remote(tmp_path) is True
        assert _has_github_origin(tmp_path) is False

    def test_returns_false_for_no_origin(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        assert _has_origin_remote(tmp_path) is False
        assert _has_github_origin(tmp_path) is False

    def test_returns_false_for_non_git_dir(self, tmp_path: Path) -> None:
        assert _has_origin_remote(tmp_path) is False
        assert _has_github_origin(tmp_path) is False


# ── scan_home_for_repos ────────────────────────────────────────────


class TestScanHomeForRepos:
    """Tests for scan_home_for_repos."""

    def test_finds_repos_with_non_origin_remote(self, tmp_path: Path) -> None:
        # Create a repo with a remote whose name is not "origin".
        repo = tmp_path / "my-project"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "upstream",
                "https://dev.azure.com/org/project/_git/my-project",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 1
        assert result[0]["path"] == str(repo.resolve())
        assert result[0]["name"] == "my-project"

    def test_finds_worktree_seed_with_git_file_without_expanding_family(
        self,
        tmp_path: Path,
    ) -> None:
        main = tmp_path / "main"
        linked = tmp_path / "linked-worktrees" / "task"
        sibling = tmp_path / "other-worktrees" / "sibling"
        linked.parent.mkdir(parents=True)
        sibling.parent.mkdir(parents=True)
        _init_repo(main)
        _git(["worktree", "add", "-b", "task", str(linked)], cwd=main)
        _git(["worktree", "add", "-b", "sibling", str(sibling)], cwd=main)

        result = scan_home_for_repos(linked.parent)

        paths = {r["path"] for r in result}
        assert paths == {str(linked.resolve())}

    def test_includes_git_reported_worktrees_without_crawling_hidden_dirs(
        self,
        tmp_path: Path,
    ) -> None:
        main = tmp_path / "projects" / "main"
        managed_worktree = tmp_path / ".ouroboros" / "worktrees" / "main" / "task"
        unlinked_hidden_repo = tmp_path / ".ouroboros" / "worktrees" / "unlinked"
        managed_worktree.parent.mkdir(parents=True)
        _init_repo(main)
        _git(["worktree", "add", "-b", "task", str(managed_worktree)], cwd=main)
        _init_repo(unlinked_hidden_repo)

        result = scan_home_for_repos(tmp_path)

        paths = {r["path"] for r in result}
        assert paths == {str(main.resolve()), str(managed_worktree.resolve())}

    def test_finds_local_repos_without_remotes(self, tmp_path: Path) -> None:
        repo = tmp_path / "local-proj"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 1
        assert result[0]["path"] == str(repo.resolve())
        assert result[0]["name"] == "local-proj"

    def test_prunes_subdirectories_after_git_found(self, tmp_path: Path) -> None:
        # Parent repo
        parent = tmp_path / "parent"
        parent.mkdir()
        subprocess.run(["git", "init", str(parent)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(parent),
                "remote",
                "add",
                "origin",
                "https://github.com/user/parent.git",
            ],
            capture_output=True,
        )

        # Nested repo inside parent (should NOT be found)
        nested = parent / "sub" / "nested"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", str(nested)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(nested),
                "remote",
                "add",
                "origin",
                "https://github.com/user/nested.git",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 1
        assert result[0]["path"] == str(parent.resolve())
        assert result[0]["name"] == "parent"

    def test_skips_excluded_directories(self, tmp_path: Path) -> None:
        # Create a repo inside node_modules (should be skipped)
        nm = tmp_path / "node_modules" / "some-pkg"
        nm.mkdir(parents=True)
        subprocess.run(["git", "init", str(nm)], capture_output=True)
        subprocess.run(
            ["git", "-C", str(nm), "remote", "add", "origin", "https://github.com/user/pkg.git"],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 0

    def test_skips_dot_directories(self, tmp_path: Path) -> None:
        # Create a repo inside a hidden directory
        hidden = tmp_path / ".hidden-dir" / "repo"
        hidden.mkdir(parents=True)
        subprocess.run(["git", "init", str(hidden)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(hidden),
                "remote",
                "add",
                "origin",
                "https://github.com/user/repo.git",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 0

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = scan_home_for_repos(tmp_path)
        assert result == []

    def test_results_are_sorted(self, tmp_path: Path) -> None:
        for name in ["zeta", "alpha", "mid"]:
            repo = tmp_path / name
            repo.mkdir()
            subprocess.run(["git", "init", str(repo)], capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/user/{name}.git",
                ],
                capture_output=True,
            )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 3
        names = [r["name"] for r in result]
        assert names == sorted(names)

    def test_returns_path_and_name_keys(self, tmp_path: Path) -> None:
        repo = tmp_path / "my-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/user/my-repo.git",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {"path", "name"}
        assert isinstance(entry["path"], str)
        assert isinstance(entry["name"], str)


# ── _read_readme_content ───────────────────────────────────────────


class TestReadReadmeContent:
    """Tests for README content reading."""

    def test_reads_readme_md(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Hello\nWorld")
        content = _read_readme_content(tmp_path)
        assert content == "# Hello\nWorld"

    def test_prefers_claude_md(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("Claude doc")
        (tmp_path / "README.md").write_text("Readme doc")
        content = _read_readme_content(tmp_path)
        assert content == "Claude doc"

    def test_returns_none_when_no_readme(self, tmp_path: Path) -> None:
        assert _read_readme_content(tmp_path) is None

    def test_truncates_long_content(self, tmp_path: Path) -> None:
        long_text = "x" * 5000
        (tmp_path / "README.md").write_text(long_text)
        content = _read_readme_content(tmp_path, max_chars=100)
        assert len(content) == 100


# ── generate_desc ──────────────────────────────────────────────────


class TestGenerateDesc:
    """Tests for LLM-based description generation."""

    @pytest.mark.asyncio
    async def test_generates_desc_from_readme(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# My Project\nA cool tool")

        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "A cool development tool"
        from ouroboros.core.types import Result

        mock_adapter.complete.return_value = Result.ok(mock_response)

        desc = await generate_desc(tmp_path, mock_adapter)
        assert desc == "A cool development tool"
        mock_adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_readme(self, tmp_path: Path) -> None:
        mock_adapter = AsyncMock()
        desc = await generate_desc(tmp_path, mock_adapter)
        assert desc == ""
        mock_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_on_llm_failure(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Project")

        mock_adapter = AsyncMock()
        mock_adapter.complete.side_effect = ProviderError("LLM down")

        desc = await generate_desc(tmp_path, mock_adapter)
        assert desc == ""

    @pytest.mark.asyncio
    async def test_truncates_long_desc(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Project")

        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "x" * 200
        from ouroboros.core.types import Result

        mock_adapter.complete.return_value = Result.ok(mock_response)

        desc = await generate_desc(tmp_path, mock_adapter)
        assert len(desc) <= 120


# ── scan_and_register ──────────────────────────────────────────────


class TestScanAndRegister:
    """Tests for the high-level scan_and_register orchestration."""

    @pytest.mark.asyncio
    async def test_upsert_registers_found_repos(self, tmp_path: Path) -> None:
        # Create a GitHub repo
        repo = tmp_path / "my-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/user/my-repo.git",
            ],
            capture_output=True,
        )

        resolved = str(repo.resolve())

        # Set up store mock — register returns the repo, list returns it
        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=resolved, name="my-repo")
        store.list.return_value = [BrownfieldRepo(path=resolved, name="my-repo")]

        result = await scan_and_register(store, root=tmp_path)

        assert len(result) == 1
        # Should use individual register (upsert), not bulk_register
        store.register.assert_called_once_with(path=resolved, name="my-repo")
        # clear_all should NOT be called (upsert preserves manual entries)
        store.clear_all.assert_not_called()
        # Should NOT auto-set default — user picks defaults via setup prompt
        store.update_is_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_llm_during_scan(self, tmp_path: Path) -> None:
        """scan_and_register does NOT call LLM — desc generation is deferred."""
        repo = tmp_path / "proj"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/user/proj.git"],
            capture_output=True,
        )
        (repo / "README.md").write_text("# Great Project\nDoes great things")

        resolved = str(repo.resolve())
        mock_adapter = AsyncMock()

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=resolved, name="proj", is_default=True)
        store.list.return_value = [BrownfieldRepo(path=resolved, name="proj", is_default=True)]

        await scan_and_register(store, llm_adapter=mock_adapter, root=tmp_path)

        # LLM should NOT be called during scan — desc generation is deferred
        mock_adapter.complete.assert_not_called()
        # Should use individual register (upsert)
        store.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_preserves_existing_defaults_after_rescan(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/user/repo.git"],
            capture_output=True,
        )

        resolved = str(repo.resolve())
        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=resolved, name="repo")
        # Existing default repo is preserved (not deleted by upsert approach)
        existing_default = BrownfieldRepo(path="/some/other", name="other", is_default=True)
        store.list.return_value = [
            existing_default,
            BrownfieldRepo(path=resolved, name="repo"),
        ]

        await scan_and_register(store, root=tmp_path)

        # clear_all should NOT be called — manual entries preserved
        store.clear_all.assert_not_called()
        # Existing default is preserved, so update_is_default should NOT be called
        # (there's already a default)
        store.update_is_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_repos_found(self, tmp_path: Path) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        store.list.return_value = []

        result = await scan_and_register(store, root=tmp_path)

        assert result == []
        store.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_registers_multiple_repos(self, tmp_path: Path) -> None:
        """scan_and_register upserts all scanned repos individually."""
        for name in ["alpha", "beta"]:
            repo = tmp_path / name
            repo.mkdir()
            subprocess.run(["git", "init", str(repo)], capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/user/{name}.git",
                ],
                capture_output=True,
            )

        alpha_path = str((tmp_path / "alpha").resolve())
        beta_path = str((tmp_path / "beta").resolve())

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=alpha_path, name="alpha")
        store.list.return_value = [
            BrownfieldRepo(path=alpha_path, name="alpha"),
            BrownfieldRepo(path=beta_path, name="beta"),
        ]

        result = await scan_and_register(store, root=tmp_path)

        assert len(result) == 2
        # Should call register individually for each repo (upsert)
        assert store.register.call_count == 2
        # clear_all should NOT be called
        store.clear_all.assert_not_called()


# ── register_repo ─────────────────────────────────────────────────


class TestRegisterRepo:
    """Tests for the register_repo business-level handler."""

    @pytest.mark.asyncio
    async def test_registers_with_explicit_name_and_desc(self) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(
            path="/home/user/my-repo", name="my-repo", desc="A cool project"
        )

        repo = await register_repo(
            store=store,
            path="/home/user/my-repo",
            name="my-repo",
            desc="A cool project",
        )

        assert repo.path == "/home/user/my-repo"
        assert repo.name == "my-repo"
        assert repo.desc == "A cool project"
        store.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_defaults_name_to_basename(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "my-project"
        repo_dir.mkdir()

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(
            path=str(repo_dir.resolve()), name="my-project"
        )

        repo = await register_repo(store=store, path=str(repo_dir))

        assert repo.name == "my-project"
        call_kwargs = store.register.call_args.kwargs
        assert call_kwargs["name"] == "my-project"

    @pytest.mark.asyncio
    async def test_auto_generates_desc_with_llm(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "proj"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Great Project\nDoes things")

        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "A great project"
        from ouroboros.core.types import Result

        mock_adapter.complete.return_value = Result.ok(mock_response)

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(
            path=str(repo_dir.resolve()), name="proj", desc="A great project"
        )

        await register_repo(
            store=store,
            path=str(repo_dir),
            llm_adapter=mock_adapter,
        )

        mock_adapter.complete.assert_called_once()
        call_kwargs = store.register.call_args.kwargs
        assert call_kwargs["desc"] == "A great project"

    @pytest.mark.asyncio
    async def test_skips_llm_when_desc_provided(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "proj"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Project")

        mock_adapter = AsyncMock()

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(
            path=str(repo_dir.resolve()), name="proj", desc="Manual desc"
        )

        await register_repo(
            store=store,
            path=str(repo_dir),
            desc="Manual desc",
            llm_adapter=mock_adapter,
        )

        # LLM should NOT be called since desc was provided
        mock_adapter.complete.assert_not_called()
        call_kwargs = store.register.call_args.kwargs
        assert call_kwargs["desc"] == "Manual desc"

    @pytest.mark.asyncio
    async def test_handles_llm_failure_gracefully(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "proj"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Project")

        mock_adapter = AsyncMock()
        mock_adapter.complete.side_effect = ProviderError("LLM down")

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=str(repo_dir.resolve()), name="proj")

        # Should not raise
        await register_repo(
            store=store,
            path=str(repo_dir),
            llm_adapter=mock_adapter,
        )

        # Should still register (with None desc)
        store.register.assert_called_once()
        call_kwargs = store.register.call_args.kwargs
        assert call_kwargs["desc"] is None

    @pytest.mark.asyncio
    async def test_resolves_existing_path(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "proj"
        repo_dir.mkdir()

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path=str(repo_dir.resolve()), name="proj")

        await register_repo(store=store, path=str(repo_dir))

        call_kwargs = store.register.call_args.kwargs
        # Path should be resolved (absolute) for existing directories
        assert Path(call_kwargs["path"]).is_absolute()
        assert call_kwargs["path"] == str(repo_dir.resolve())

    @pytest.mark.asyncio
    async def test_preserves_nonexistent_path(self) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path="/nonexistent/repo", name="repo")

        await register_repo(store=store, path="/nonexistent/repo")

        call_kwargs = store.register.call_args.kwargs
        # Non-existent path should be preserved as-is
        assert call_kwargs["path"] == "/nonexistent/repo"


# ── set_default_repo ──────────────────────────────────────────────


class TestSetDefaultRepo:
    """Tests for the set_default_repo business-level handler."""

    @pytest.mark.asyncio
    async def test_sets_default_successfully(self) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = BrownfieldRepo(
            path="/home/user/repo-a", name="repo-a", is_default=True
        )

        repo = await set_default_repo(store=store, path="/home/user/repo-a")

        assert repo is not None
        assert repo.path == "/home/user/repo-a"
        assert repo.is_default is True
        store.update_is_default.assert_called_once_with("/home/user/repo-a", is_default=True)

    @pytest.mark.asyncio
    async def test_returns_none_for_unregistered_path(self) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = None

        repo = await set_default_repo(store=store, path="/nonexistent")

        assert repo is None
        store.update_is_default.assert_called_once_with("/nonexistent", is_default=True)

    @pytest.mark.asyncio
    async def test_delegates_to_store(self) -> None:
        store = AsyncMock(spec=BrownfieldStore)
        expected = BrownfieldRepo(path="/repo", name="repo", desc="Desc", is_default=True)
        store.update_is_default.return_value = expected

        result = await set_default_repo(store=store, path="/repo")

        assert result is expected
        store.update_is_default.assert_called_once_with("/repo", is_default=True)

    @pytest.mark.asyncio
    async def test_generates_desc_when_empty(self, tmp_path: Path) -> None:
        """set_default generates desc via Frugal model when desc is empty."""
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# My Repo\nA great tool for devs")

        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = BrownfieldRepo(
            path=str(repo_dir), name="my-repo", desc=None, is_default=True
        )
        updated_repo = BrownfieldRepo(
            path=str(repo_dir), name="my-repo", desc="A great tool for developers", is_default=True
        )
        store.update_desc.return_value = updated_repo

        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "A great tool for developers"
        from ouroboros.core.types import Result

        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await set_default_repo(
            store=store,
            path=str(repo_dir),
            llm_adapter=mock_adapter,
        )

        assert result is not None
        assert result.desc == "A great tool for developers"
        mock_adapter.complete.assert_called_once()
        store.update_desc.assert_called_once_with(str(repo_dir), "A great tool for developers")

    @pytest.mark.asyncio
    async def test_skips_desc_generation_when_desc_exists(self) -> None:
        """set_default does NOT call LLM if desc already exists."""
        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = BrownfieldRepo(
            path="/repo", name="repo", desc="Already has desc", is_default=True
        )

        mock_adapter = AsyncMock()

        result = await set_default_repo(
            store=store,
            path="/repo",
            llm_adapter=mock_adapter,
        )

        assert result is not None
        assert result.desc == "Already has desc"
        mock_adapter.complete.assert_not_called()
        store.update_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_desc_generation_when_no_llm(self) -> None:
        """set_default does NOT generate desc without LLM adapter."""
        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = BrownfieldRepo(
            path="/repo", name="repo", desc=None, is_default=True
        )

        result = await set_default_repo(store=store, path="/repo")

        assert result is not None
        store.update_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_unset_default_preserves_existing_desc(self) -> None:
        """When switching default, the previously-default repo keeps its desc."""
        store = AsyncMock(spec=BrownfieldStore)
        # update_is_default returns the NEW default repo — its desc should be intact
        store.update_is_default.return_value = BrownfieldRepo(
            path="/repo-b", name="repo-b", desc="B description", is_default=True
        )

        result = await set_default_repo(store=store, path="/repo-b")

        assert result is not None
        assert result.desc == "B description"
        assert result.is_default is True
        # update_is_default only changes is_default column, not desc
        store.update_is_default.assert_called_once_with("/repo-b", is_default=True)
        # update_desc should NOT be called since desc is already present
        store.update_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_desc_generation_failure_gracefully(self, tmp_path: Path) -> None:
        """set_default returns repo even if desc generation fails."""
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Project")

        original_repo = BrownfieldRepo(
            path=str(repo_dir), name="my-repo", desc=None, is_default=True
        )
        store = AsyncMock(spec=BrownfieldStore)
        store.update_is_default.return_value = original_repo

        mock_adapter = AsyncMock()
        mock_adapter.complete.side_effect = ProviderError("LLM down")

        result = await set_default_repo(
            store=store,
            path=str(repo_dir),
            llm_adapter=mock_adapter,
        )

        # Should still return the repo (without desc)
        assert result is not None
        assert result is original_repo
        store.update_desc.assert_not_called()


# ── Skip directory verification ───────────────────────────────────


class TestSkipDirsVerification:
    """Verify that all hardcoded _SKIP_DIRS are correctly skipped during scan."""

    EXPECTED_SKIP_DIRS = frozenset(
        {
            "node_modules",
            ".venv",
            "__pycache__",
            ".cache",
            "Library",
            ".Trash",
            "vendor",
            ".gradle",
            "build",
            "dist",
            "target",
            ".tox",
            ".mypy_cache",
            ".pytest_cache",
            ".cargo",
            "Pods",
            ".npm",
            ".nvm",
            ".local",
            ".docker",
            ".rustup",
            "go",
        }
    )

    def test_skip_dirs_constant_matches_expected(self) -> None:
        """Ensure _SKIP_DIRS hasn't drifted from the expected set."""
        assert _SKIP_DIRS == self.EXPECTED_SKIP_DIRS

    def test_skip_dirs_is_frozenset(self) -> None:
        """_SKIP_DIRS must be immutable."""
        assert isinstance(_SKIP_DIRS, frozenset)

    @pytest.mark.parametrize("skip_dir", sorted(EXPECTED_SKIP_DIRS))
    def test_each_skip_dir_is_pruned(self, tmp_path: Path, skip_dir: str) -> None:
        """Each entry in _SKIP_DIRS prevents scan from descending into it."""
        # Create a repo inside the skip directory
        nested = tmp_path / skip_dir / "some-project"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", str(nested)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(nested),
                "remote",
                "add",
                "origin",
                "https://github.com/user/project.git",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 0, f"Repo inside '{skip_dir}' should be skipped but was found"

    def test_dot_prefixed_dirs_are_skipped(self, tmp_path: Path) -> None:
        """Any directory starting with '.' is pruned (not just those in _SKIP_DIRS)."""
        for hidden in [".hidden", ".config", ".ssh", ".aws"]:
            repo = tmp_path / hidden / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", str(repo)], capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/user/repo.git",
                ],
                capture_output=True,
            )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 0

    def test_non_skip_dir_is_scanned(self, tmp_path: Path) -> None:
        """A directory NOT in _SKIP_DIRS and NOT dot-prefixed IS scanned."""
        repo = tmp_path / "projects" / "my-app"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/user/my-app.git",
            ],
            capture_output=True,
        )

        result = scan_home_for_repos(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "my-app"


# ── scan_and_register mocking tests ──────────────────────────────


class TestScanAndRegisterMocked:
    """Tests for scan_and_register with fully mocked scan_home_for_repos.

    These tests mock the filesystem scan to verify the orchestration logic
    (bulk registration, default setting) without creating real git repos.
    """

    @pytest.mark.asyncio
    async def test_mocked_scan_calls_register_per_repo(self) -> None:
        """scan_and_register upserts each scanned repo via store.register."""
        fake_repos = [
            {"path": "/home/user/alpha", "name": "alpha"},
            {"path": "/home/user/beta", "name": "beta"},
        ]

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path="/home/user/alpha", name="alpha")
        store.list.return_value = [
            BrownfieldRepo(path="/home/user/alpha", name="alpha"),
            BrownfieldRepo(path="/home/user/beta", name="beta"),
        ]

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=fake_repos,
        ):
            result = await scan_and_register(store, root=Path("/fake"))

        assert len(result) == 2
        assert store.register.call_count == 2
        store.clear_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_mocked_scan_does_not_auto_set_default(self) -> None:
        """Scan should NOT auto-set default — user picks via setup prompt."""
        fake_repos = [{"path": "/a", "name": "a"}]

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path="/a", name="a")
        store.list.return_value = [
            BrownfieldRepo(path="/a", name="a"),
        ]

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=fake_repos,
        ):
            await scan_and_register(store, root=Path("/fake"))

        store.update_is_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_mocked_scan_preserves_existing_default(self) -> None:
        """When default already exists, scan_and_register does not override it."""
        fake_repos = [{"path": "/new", "name": "new"}]
        existing_default = BrownfieldRepo(
            path="/old",
            name="old",
            is_default=True,
        )

        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path="/new", name="new")
        # list returns the existing default + newly scanned repo
        store.list.return_value = [existing_default, BrownfieldRepo(path="/new", name="new")]

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=fake_repos,
        ):
            await scan_and_register(store, root=Path("/fake"))

        # Existing default is preserved via upsert — no update_is_default needed
        store.update_is_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_mocked_scan_empty_returns_empty_list(self) -> None:
        """An empty scan must not leak previously-registered repos into the result.

        scan_and_register is boundary-sensitive: callers (CLI, MCP) display its
        return value as "what this scan discovered." Returning the full DB on an
        empty scan would falsely report unrelated repos as if they were just
        found. Callers that need the full registry must call store.list() directly.
        """
        store = AsyncMock(spec=BrownfieldStore)
        pre_registered = [BrownfieldRepo(path="/existing", name="existing")]
        store.list.return_value = pre_registered

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=[],
        ):
            result = await scan_and_register(store, root=Path("/fake"))

        assert result == []
        store.register.assert_not_called()
        store.bulk_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_mocked_scan_returns_only_scanned_repos(self) -> None:
        """Result must contain only repos discovered by THIS scan, not the whole DB."""
        fake_repos = [{"path": "/scanned", "name": "scanned"}]
        store = AsyncMock(spec=BrownfieldStore)
        store.register.return_value = BrownfieldRepo(path="/scanned", name="scanned")
        # store.list() includes both the scanned repo AND an unrelated, manually
        # registered repo outside the scan root.
        store.list.return_value = [
            BrownfieldRepo(path="/scanned", name="scanned"),
            BrownfieldRepo(path="/manual/outside", name="outside"),
        ]

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=fake_repos,
        ):
            result = await scan_and_register(store, root=Path("/fake"))

        assert {r.path for r in result} == {"/scanned"}

    @pytest.mark.asyncio
    async def test_mocked_scan_llm_adapter_not_called(self) -> None:
        """LLM adapter is accepted but never invoked during scan phase."""
        fake_repos = [{"path": "/repo", "name": "repo"}]
        mock_adapter = AsyncMock()

        store = AsyncMock(spec=BrownfieldStore)
        store.get_default.return_value = BrownfieldRepo(
            path="/repo",
            name="repo",
            is_default=True,
        )
        store.bulk_register.return_value = 1
        store.list.return_value = [
            BrownfieldRepo(path="/repo", name="repo"),
        ]

        with patch(
            "ouroboros.bigbang.brownfield.scan_home_for_repos",
            return_value=fake_repos,
        ):
            await scan_and_register(
                store,
                llm_adapter=mock_adapter,
                root=Path("/fake"),
            )

        mock_adapter.complete.assert_not_called()


# ── User selection simulation tests ──────────────────────────────


class TestUserSelectionSimulation:
    """Tests simulating user selection flows in the setup command.

    These tests mock the setup command's _prompt_repo_selection and
    _scan_and_register_repos to verify end-to-end selection behavior.
    """

    def test_select_first_repo(self) -> None:
        """User selects first repo → returns index 0."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [
            {"path": "/a", "name": "alpha"},
            {"path": "/b", "name": "beta"},
        ]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="1"):
            idx = _prompt_repo_selection(repos)
        assert idx == 0

    def test_select_last_repo(self) -> None:
        """User selects last repo → returns correct 0-based index."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [
            {"path": "/a", "name": "a"},
            {"path": "/b", "name": "b"},
            {"path": "/c", "name": "c"},
        ]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="3"):
            idx = _prompt_repo_selection(repos)
        assert idx == 2

    def test_skip_with_s_shorthand(self) -> None:
        """User types 's' → treated as skip → returns None."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="s"):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    def test_skip_with_full_word(self) -> None:
        """User types 'skip' → returns None."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="skip"):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    def test_empty_input_returns_none(self) -> None:
        """Empty input → treated as skip → returns None."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value=""):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    def test_negative_number_returns_none(self) -> None:
        """Negative number → out of range → returns None."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="-1"):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    def test_zero_returns_none(self) -> None:
        """Zero → out of range → returns None."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="0"):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    def test_whitespace_padded_skip(self) -> None:
        """Whitespace around 'skip' is handled correctly."""
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="  skip  "):
            idx = _prompt_repo_selection(repos)
        assert idx is None

    @pytest.mark.asyncio
    async def test_full_setup_scan_then_select(self) -> None:
        """Simulate the full scan → select flow with mocked components."""
        from ouroboros.cli.commands.setup import (
            _scan_and_register_repos,
            _set_default_repo,
        )
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(path="/a", name="alpha", is_default=False),
            BrownfieldRepo(path="/b", name="beta", is_default=False),
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        # Step 1: Scan returns repos
        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            repos = await _scan_and_register_repos()

        assert len(repos) == 2
        assert repos[0]["name"] == "alpha"
        assert repos[1]["name"] == "beta"

        # Step 2: Simulate user selecting repo #2
        from ouroboros.cli.commands.setup import _prompt_repo_selection

        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="2"):
            idx = _prompt_repo_selection(repos)
        assert idx == 1
        selected = repos[idx]
        assert selected["path"] == "/b"

        # Step 3: Set default (toggle: /b is not default, so it gets added)
        mock_result = BrownfieldRepo(path="/b", name="beta", is_default=True)
        mock_store.list = AsyncMock(return_value=mock_repos)
        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            success = await _set_default_repo(selected["path"])

        assert success is True
