"""Tests for ouroboros.core.project_paths — path resolution helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ouroboros.core.project_paths import (
    project_path_candidates_from_seed,
    resolve_path_against_base,
    resolve_seed_project_path,
)


class TestResolvePathAgainstBase:
    """Tests for resolve_path_against_base."""

    def test_none_returns_none(self, tmp_path: Path) -> None:
        assert resolve_path_against_base(None, stable_base=tmp_path) is None

    def test_absolute_path_returned_as_is(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "project"
        abs_path.mkdir()
        result = resolve_path_against_base(str(abs_path), stable_base=Path("/other"))
        assert result == abs_path.resolve()

    def test_relative_path_resolved_against_base(self, tmp_path: Path) -> None:
        result = resolve_path_against_base("subdir/project", stable_base=tmp_path)
        assert result == (tmp_path / "subdir" / "project").resolve()

    def test_path_object_accepted(self, tmp_path: Path) -> None:
        result = resolve_path_against_base(Path("myproject"), stable_base=tmp_path)
        assert result == (tmp_path / "myproject").resolve()

    def test_tilde_expanded(self, tmp_path: Path) -> None:
        result = resolve_path_against_base("~/some/path", stable_base=tmp_path)
        assert result is not None
        assert "~" not in str(result)


class TestProjectPathCandidatesFromSeed:
    """Tests for project_path_candidates_from_seed."""

    def test_none_seed_returns_empty(self) -> None:
        assert project_path_candidates_from_seed(None) == ()

    def test_seed_with_project_dir(self) -> None:
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="/home/user/project", working_directory=None),
            brownfield_context=None,
        )
        result = project_path_candidates_from_seed(seed)
        assert "/home/user/project" in result

    def test_seed_with_working_directory_fallback(self) -> None:
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=None, working_directory="/work/dir"),
            brownfield_context=None,
        )
        result = project_path_candidates_from_seed(seed)
        assert "/work/dir" in result

    def test_seed_with_brownfield_primary_reference(self) -> None:
        ref = SimpleNamespace(path="/repo/main", role="primary")
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=[ref]),
        )
        result = project_path_candidates_from_seed(seed)
        assert "/repo/main" in result

    def test_seed_with_multiple_references(self) -> None:
        refs = [
            SimpleNamespace(path="/repo/primary", role="primary"),
            SimpleNamespace(path="/repo/secondary", role="secondary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )
        result = project_path_candidates_from_seed(seed)
        assert result[0] == "/repo/primary"
        assert "/repo/secondary" in result

    def test_seed_with_no_metadata_or_brownfield(self) -> None:
        seed = SimpleNamespace(metadata=None, brownfield_context=None)
        result = project_path_candidates_from_seed(seed)
        assert result == ()

    def test_empty_string_path_ignored(self) -> None:
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="", working_directory=""),
            brownfield_context=None,
        )
        result = project_path_candidates_from_seed(seed)
        assert result == ()

    def test_primary_reference_comes_first(self) -> None:
        refs = [
            SimpleNamespace(path="/secondary", role="secondary"),
            SimpleNamespace(path="/primary", role="primary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )
        result = project_path_candidates_from_seed(seed)
        assert result[0] == "/primary"

    def test_no_duplicate_candidates(self) -> None:
        ref = SimpleNamespace(path="/same/path", role="primary")
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="/same/path", working_directory=None),
            brownfield_context=SimpleNamespace(context_references=[ref]),
        )
        result = project_path_candidates_from_seed(seed)
        assert result.count("/same/path") == 1


class TestResolveSeedProjectPath:
    """Tests for resolve_seed_project_path."""

    def test_none_seed_returns_none(self, tmp_path: Path) -> None:
        assert resolve_seed_project_path(None, stable_base=tmp_path) is None

    def test_resolves_first_candidate(self, tmp_path: Path) -> None:
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="myproject", working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result == (tmp_path / "myproject").resolve()

    def test_absolute_path_in_seed(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "absolute_project"
        abs_dir.mkdir()
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=str(abs_dir), working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=Path("/other"))
        assert result == abs_dir.resolve()

    def test_empty_seed_returns_none(self, tmp_path: Path) -> None:
        seed = SimpleNamespace(metadata=None, brownfield_context=None)
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result is None
