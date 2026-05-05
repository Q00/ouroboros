"""Tests for ouroboros.core.project_paths — path resolution helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from structlog.testing import capture_logs

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

    def test_enforce_containment_rejects_escaping_absolute(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside"
        result = resolve_path_against_base(
            str(outside),
            stable_base=tmp_path,
            enforce_containment=True,
        )
        assert result is None

    def test_enforce_containment_rejects_traversal(self, tmp_path: Path) -> None:
        result = resolve_path_against_base(
            "../escape",
            stable_base=tmp_path,
            enforce_containment=True,
        )
        assert result is None

    def test_enforce_containment_allows_inside(self, tmp_path: Path) -> None:
        inside = tmp_path / "subdir" / "project"
        result = resolve_path_against_base(
            str(inside),
            stable_base=tmp_path,
            enforce_containment=True,
        )
        assert result == inside.resolve()

    def test_enforce_containment_allows_base_itself(self, tmp_path: Path) -> None:
        result = resolve_path_against_base(
            str(tmp_path),
            stable_base=tmp_path,
            enforce_containment=True,
        )
        assert result == tmp_path.resolve()

    def test_enforce_containment_rejects_tilde_traversal(self, tmp_path: Path) -> None:
        with capture_logs() as cap_logs:
            result = resolve_path_against_base(
                "~/../../etc/passwd",
                stable_base=tmp_path,
                enforce_containment=True,
            )
        assert result is None
        assert any(
            entry.get("event") == "project_paths.containment_violation" for entry in cap_logs
        )

    def test_enforce_containment_logs_warning_on_rejection(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside"
        with capture_logs() as cap_logs:
            result = resolve_path_against_base(
                str(outside),
                stable_base=tmp_path,
                enforce_containment=True,
            )
        assert result is None
        violations = [
            entry
            for entry in cap_logs
            if entry.get("event") == "project_paths.containment_violation"
        ]
        assert len(violations) == 1
        assert violations[0]["log_level"] == "warning"
        assert str(outside) in violations[0]["raw_path"]


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

    def test_absolute_path_inside_base_in_seed(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "absolute_project"
        abs_dir.mkdir()
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=str(abs_dir), working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result == abs_dir.resolve()

    def test_absolute_path_escaping_base_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "escaped_project"
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=str(outside), working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result is None

    def test_traversal_brownfield_reference_rejected(self, tmp_path: Path) -> None:
        ref = SimpleNamespace(path="../../etc/passwd", role="primary")
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=[ref]),
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result is None

    def test_falls_through_to_safe_candidate(self, tmp_path: Path) -> None:
        safe = tmp_path / "safe"
        safe.mkdir()
        refs = [
            SimpleNamespace(path="/etc", role="primary"),
            SimpleNamespace(path=str(safe), role="secondary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result == safe.resolve()

    def test_empty_seed_returns_none(self, tmp_path: Path) -> None:
        seed = SimpleNamespace(metadata=None, brownfield_context=None)
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result is None
