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

    def test_none_seed_returns_empty_resolution(self, tmp_path: Path) -> None:
        result = resolve_seed_project_path(None, stable_base=tmp_path)
        assert result.path is None
        assert result.rejected is False

    def test_resolves_first_candidate(self, tmp_path: Path) -> None:
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="myproject", working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result.path == (tmp_path / "myproject").resolve()
        assert result.rejected is False

    def test_absolute_path_inside_base_in_seed(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "absolute_project"
        abs_dir.mkdir()
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=str(abs_dir), working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result.path == abs_dir.resolve()
        assert result.rejected is False

    def test_absolute_path_escaping_base_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "escaped_project"
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=str(outside), working_directory=None),
            brownfield_context=None,
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result.path is None
        assert result.rejected is True

    def test_traversal_brownfield_reference_rejected(self, tmp_path: Path) -> None:
        ref = SimpleNamespace(path="../../etc/passwd", role="primary")
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=[ref]),
        )
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result.path is None
        assert result.rejected is True

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
        assert result.path == safe.resolve()
        assert result.rejected is False

    def test_empty_seed_returns_empty_resolution(self, tmp_path: Path) -> None:
        seed = SimpleNamespace(metadata=None, brownfield_context=None)
        result = resolve_seed_project_path(seed, stable_base=tmp_path)
        assert result.path is None
        assert result.rejected is False

    def test_distinguishes_no_candidates_from_all_rejected(self, tmp_path: Path) -> None:
        """The two ``path is None`` cases must be distinguishable by callers."""
        empty = SimpleNamespace(metadata=None, brownfield_context=None)
        rejected = SimpleNamespace(
            metadata=SimpleNamespace(project_dir="/etc/passwd", working_directory=None),
            brownfield_context=None,
        )

        empty_result = resolve_seed_project_path(empty, stable_base=tmp_path)
        rejected_result = resolve_seed_project_path(rejected, stable_base=tmp_path)

        assert empty_result.path is None and not empty_result.rejected
        assert rejected_result.path is None and rejected_result.rejected

    def test_reference_nonexistent_path_skipped_falls_through(
        self,
        tmp_path: Path,
    ) -> None:
        """A primary reference resolving to a non-existent path is skipped.

        Reproduces the central-seed cwd bug: seed-generator emits primary
        ``context_references`` as file pointers (e.g. ``src/foo/bar.py``).
        When the seed lives somewhere whose stable_base does not actually
        contain those files (the common case for central seeds invoked
        from a fresh worktree), joining the reference path against
        stable_base produces a non-existent location. That synthetic join
        must not be returned as the runtime cwd — the resolver falls
        through to the next candidate (or back to the caller's stable_base).
        """
        safe_dir = tmp_path / "safe-dir"
        safe_dir.mkdir()

        refs = [
            # Resolves to <stable_base>/src/events.py which does not exist.
            SimpleNamespace(path="src/events.py", role="primary"),
            SimpleNamespace(path="safe-dir", role="secondary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        result = resolve_seed_project_path(seed, stable_base=tmp_path)

        assert result.path == safe_dir.resolve()
        assert result.rejected is False

    def test_reference_only_all_nonexistent_returns_unrejected_empty(
        self,
        tmp_path: Path,
    ) -> None:
        """Seeds whose only references all miss yield path=None, rejected=False.

        Heuristic misses (reference candidates resolved inside stable_base
        but pointing at non-existent paths) are not security events: the
        caller falls back to ``stable_base`` rather than aborting. Only
        containment violations flip ``rejected=True``.
        """
        refs = [
            SimpleNamespace(path="does/not/exist.py", role="primary"),
            SimpleNamespace(path="also/missing.py", role="secondary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        result = resolve_seed_project_path(seed, stable_base=tmp_path)

        assert result.path is None
        assert result.rejected is False

    def test_reference_existing_file_returned_for_caller_to_normalize(
        self,
        tmp_path: Path,
    ) -> None:
        """An existing-file reference is returned so the caller can collapse it.

        ``_resolve_cli_project_dir`` follows this with
        ``_directory_for_runtime`` to use the file's parent directory as
        the runtime cwd. Filtering files at the resolver level would
        regress that established behavior.
        """
        target_file = tmp_path / "src" / "events.py"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("# fixture file")

        refs = [SimpleNamespace(path="src/events.py", role="primary")]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        result = resolve_seed_project_path(seed, stable_base=tmp_path)

        assert result.path == target_file.resolve()
        assert result.rejected is False

    def test_reference_skip_emits_info_log(self, tmp_path: Path) -> None:
        """Skipped non-existent reference candidates are observable in logs."""
        refs = [SimpleNamespace(path="src/events.py", role="primary")]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        with capture_logs() as cap_logs:
            resolve_seed_project_path(seed, stable_base=tmp_path)

        skipped = [
            entry
            for entry in cap_logs
            if entry.get("event") == "project_paths.reference_skipped_nonexistent"
        ]
        assert len(skipped) == 1
        assert skipped[0]["raw_path"] == "src/events.py"

    def test_containment_violation_still_flips_rejected(self, tmp_path: Path) -> None:
        """An escaping candidate is rejected; subsequent misses do not unset it."""
        refs = [
            SimpleNamespace(path="../escape", role="primary"),
            SimpleNamespace(path="missing.py", role="secondary"),
        ]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        result = resolve_seed_project_path(seed, stable_base=tmp_path)

        assert result.path is None
        assert result.rejected is True
