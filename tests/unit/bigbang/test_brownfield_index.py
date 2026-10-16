"""Tests for the brownfield search index: documents, fingerprints, ranking, scan reuse."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.bigbang import brownfield as brownfield_module
from ouroboros.bigbang.brownfield import register_repo, scan_and_register
from ouroboros.bigbang.brownfield_index import (
    build_repo_document,
    index_repo,
    rank_repos,
    repo_fingerprint,
)
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "init", str(repo)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
async def store(tmp_path):
    s = BrownfieldStore(f"sqlite+aiosqlite:///{tmp_path / 'bf.db'}")
    await s.initialize()
    yield s
    await s.close()


class TestBuildRepoDocument:
    def test_reads_manifest_readme_head_and_top_level_names(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "ledger-sync"\ndescription = "Reconcile bank feeds"\n'
            'keywords = ["accounting"]\n'
        )
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "ledger-ui", "description": "Web console"})
        )
        (tmp_path / "go.mod").write_text("module example.com/ledger/api\n\ngo 1.22\n")
        (tmp_path / "README.md").write_text("# Ledger\n" + "intro " * 10 + "x" * 5000 + "TAIL")
        (tmp_path / "migrations").mkdir()
        (tmp_path / ".hidden").mkdir()

        doc = build_repo_document(tmp_path)

        assert "ledger-sync" in doc and "Reconcile bank feeds" in doc and "accounting" in doc
        assert "ledger-ui" in doc and "Web console" in doc
        assert "example.com/ledger/api" in doc
        assert "# Ledger" in doc
        assert "TAIL" not in doc  # README is read as a bounded head only
        assert "migrations" in doc
        assert ".hidden" not in doc

    def test_tolerates_broken_manifests(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("not = [valid")
        (tmp_path / "package.json").write_text("{nope")
        (tmp_path / "README").write_text("still indexed")

        assert "still indexed" in build_repo_document(tmp_path)

    def test_missing_directory_yields_empty_document(self, tmp_path: Path) -> None:
        assert build_repo_document(tmp_path / "gone") == ""


class TestFingerprint:
    def test_stable_until_an_input_changes(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("one")
        first = repo_fingerprint(tmp_path)
        assert first and repo_fingerprint(tmp_path) == first

        (tmp_path / "unrelated.txt").write_text("x")
        _bump_mtime(tmp_path)
        after_new_entry = repo_fingerprint(tmp_path)
        assert after_new_entry != first

        readme.write_text("two, longer")
        assert repo_fingerprint(tmp_path) != after_new_entry

    def test_index_repo_skips_unchanged(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("hello")
        built = index_repo(tmp_path, None)
        assert built is not None
        _doc, fingerprint = built
        assert index_repo(tmp_path, fingerprint) is None
        assert index_repo(tmp_path, "stale") is not None


class TestRankRepos:
    def test_uses_stored_documents_and_registry_fields(self) -> None:
        repos = [
            BrownfieldRepo(path="/w/alpha", name="alpha", id=1),
            BrownfieldRepo(path="/w/bravo", name="bravo", desc="billing gateway", id=2),
            BrownfieldRepo(path="/w/charlie", name="charlie", id=3),
        ]
        docs = {"/w/charlie": "stripe billing webhooks invoices"}

        ranked = rank_repos("stripe invoices", repos, docs)
        assert [r.name for r, _ in ranked] == ["charlie"]

        ranked = rank_repos("billing", repos, docs)
        assert [r.name for r, _ in ranked] == ["bravo", "charlie"]

    def test_unindexed_repo_ranks_on_name_and_path(self) -> None:
        repos = [BrownfieldRepo(path="/work/payments/core-api", name="core-api")]
        assert rank_repos("payments", repos, {})[0][0].name == "core-api"

    def test_ties_break_by_name(self) -> None:
        repos = [
            BrownfieldRepo(path="/w/zeta", name="zeta"),
            BrownfieldRepo(path="/w/alpha", name="alpha"),
        ]
        docs = {"/w/zeta": "shared", "/w/alpha": "shared"}
        assert [r.name for r, _ in rank_repos("shared", repos, docs)] == ["alpha", "zeta"]


class TestScanStoresDocuments:
    async def test_scan_stores_document_text(self, tmp_path: Path, store) -> None:
        root = tmp_path / "root"
        repo = root / "invoice-service"
        _init_repo(repo)
        (repo / "README.md").write_text("Generates PDF invoices for tenants")

        await scan_and_register(store, root=root)

        entries = await store.get_index_entries()
        resolved = str(repo.resolve())
        assert "PDF invoices" in entries[resolved][0]
        assert entries[resolved][1]

    async def test_rescan_skips_git_check_and_unchanged_documents(
        self, tmp_path: Path, store
    ) -> None:
        root = tmp_path / "root"
        _init_repo(root / "one")
        _init_repo(root / "two")
        (root / "one" / "README.md").write_text("first")

        await scan_and_register(store, root=root)

        real_check = brownfield_module._is_git_worktree
        with (
            patch.object(
                brownfield_module, "_is_git_worktree", side_effect=real_check
            ) as git_check,
            patch.object(
                brownfield_module, "index_repo", side_effect=brownfield_module.index_repo
            ) as indexer,
            patch.object(store, "upsert_index_entries", wraps=store.upsert_index_entries) as up,
        ):
            again = await scan_and_register(store, root=root)

        assert {r.name for r in again} == {"one", "two"}
        git_check.assert_not_called()
        assert indexer.call_count == 2
        assert up.await_args.args[0] == {}  # nothing changed, nothing rewritten

    async def test_rescan_rebuilds_changed_repo_only(self, tmp_path: Path, store) -> None:
        root = tmp_path / "root"
        _init_repo(root / "one")
        _init_repo(root / "two")
        readme = root / "one" / "README.md"
        readme.write_text("first")
        await scan_and_register(store, root=root)

        readme.write_text("rewritten about kafka consumers")
        with patch.object(store, "upsert_index_entries", wraps=store.upsert_index_entries) as up:
            await scan_and_register(store, root=root)

        written = up.await_args.args[0]
        assert list(written) == [str((root / "one").resolve())]
        assert "kafka" in written[str((root / "one").resolve())][0]

    async def test_new_repo_is_still_git_validated(self, tmp_path: Path, store) -> None:
        root = tmp_path / "root"
        _init_repo(root / "one")
        await scan_and_register(store, root=root)

        fake = root / "fake"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: /does/not/exist\n")

        again = await scan_and_register(store, root=root)
        assert {r.name for r in again} == {"one"}

    async def test_index_failure_does_not_fail_scan(self, tmp_path: Path, store) -> None:
        root = tmp_path / "root"
        _init_repo(root / "one")
        with patch.object(brownfield_module, "index_repo", side_effect=RuntimeError("boom")):
            repos = await scan_and_register(store, root=root)
        assert [r.name for r in repos] == ["one"]

    async def test_register_indexes_existing_directory(self, tmp_path: Path, store) -> None:
        repo = tmp_path / "manual"
        repo.mkdir()
        (repo / "README.md").write_text("terraform modules for vpc")

        await register_repo(store, str(repo))
        await register_repo(store, "/not/on/this/machine", name="remote")

        entries = await store.get_index_entries()
        assert "terraform" in entries[str(repo.resolve())][0]
        assert "/not/on/this/machine" not in entries
