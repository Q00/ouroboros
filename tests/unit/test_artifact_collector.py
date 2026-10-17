"""Unit tests for ArtifactCollector."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

import ouroboros.evaluation.artifact_collector as artifact_collector_module
from ouroboros.evaluation.artifact_collector import ArtifactCollector


class TestArtifactCollector:
    def _create_project(self, files: dict[str, str]) -> str:
        tmpdir = tempfile.mkdtemp()
        for name, content in files.items():
            path = os.path.join(tmpdir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        return tmpdir

    def test_no_project_dir_returns_text_only(self) -> None:
        collector = ArtifactCollector()
        bundle = collector.collect("some output", project_dir=None)
        assert bundle.text_summary == "some output"
        assert len(bundle.files) == 0

    def test_collects_file_from_write_pattern(self) -> None:
        tmpdir = self._create_project({"main.py": "print('hello')\n"})
        main_path = os.path.join(tmpdir, "main.py")
        output = f"Write: {main_path}\nDone."

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert bundle.files[0].file_path == main_path
        assert "hello" in bundle.files[0].content

    def test_collects_file_from_edit_pattern(self) -> None:
        tmpdir = self._create_project({"config.py": "X = 10\n"})
        config_path = os.path.join(tmpdir, "config.py")
        output = f"Edit: {config_path}\nUpdated config."

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert "X = 10" in bundle.files[0].content

    def test_deduplicates_paths(self) -> None:
        tmpdir = self._create_project({"a.py": "code"})
        path = os.path.join(tmpdir, "a.py")
        output = f"Write: {path}\nEdit: {path}\n"

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1

    def test_nonexistent_file_skipped(self) -> None:
        tmpdir = self._create_project({})
        output = f"Write: {tmpdir}/nonexistent.py\n"

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 0

    def test_no_paths_in_output_falls_back_to_directory_scan(self) -> None:
        tmpdir = self._create_project({"a.py": "code"})
        output = "No file operations here."

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        # Directory scan fallback finds the source file.
        assert len(bundle.files) >= 1
        assert bundle.text_summary == output

    def test_total_chars_tracked(self) -> None:
        tmpdir = self._create_project(
            {
                "a.py": "x" * 100,
                "b.py": "y" * 200,
            }
        )
        output = f"Write: {os.path.join(tmpdir, 'a.py')}\nWrite: {os.path.join(tmpdir, 'b.py')}\n"

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert bundle.total_chars == 300

    def test_task_association_extraction(self) -> None:
        tmpdir = self._create_project({"task.py": "code"})
        path = os.path.join(tmpdir, "task.py")
        output = (
            f"### Task 2: [COMPLETED] Create tasks\nWrite: {path}\n"
            "### Task 3: [FAILED] Delete tasks\nOther stuff\n"
        )

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert 1 in bundle.files[0].ac_indices  # Task 2 → source AC index 1

    def test_legacy_ac_association_extraction_remains_supported(self) -> None:
        tmpdir = self._create_project({"task.py": "code"})
        path = os.path.join(tmpdir, "task.py")
        output = f"### AC 2: [PASS] Create tasks\nWrite: {path}\n### AC 3: [FAIL] Delete tasks\nOther stuff\n"

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert 1 in bundle.files[0].ac_indices  # Legacy AC 2 → source AC index 1

    def test_collects_file_from_subtask_report_format(self) -> None:
        tmpdir = self._create_project({"task_store.py": "TASKS = []\n"})
        path = os.path.join(tmpdir, "task_store.py")
        output = (
            "### Task 1: [COMPLETED] Create tasks\n"
            "Decomposed into 1 Subtasks\n\n"
            "#### Subtask 1.1: [COMPLETED] Create task storage\n"
            "File Changes:\n"
            f"- Write: {path}\n"
            "Result:\n"
            "Implemented storage.\n"
        )

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert bundle.files[0].file_path == path
        assert bundle.files[0].ac_indices == (0,)

    def test_fallback_scan_is_bounded_and_marked_incomplete(self, monkeypatch) -> None:
        tmpdir = self._create_project({"a.py": "a", "b.py": "b", "c.py": "c"})
        monkeypatch.setattr(artifact_collector_module, "MAX_SCAN_ENTRIES", 2)

        bundle = ArtifactCollector().collect("No file operations here.", project_dir=tmpdir)

        assert bundle.scan_complete is False
        assert bundle.scan_error == "entry budget (2) reached"

    def test_fallback_scan_does_not_enumerate_past_entry_budget(self, monkeypatch) -> None:
        tmpdir = self._create_project({f"{index}.py": "code" for index in range(7)})
        monkeypatch.setattr(artifact_collector_module, "MAX_SCAN_ENTRIES", 2)
        real_scandir = os.scandir
        enumerated = 0

        class CountingScandir:
            def __init__(self, path):
                self._iterator = real_scandir(path)

            def __enter__(self):
                self._iterator.__enter__()
                return self

            def __exit__(self, *args):
                return self._iterator.__exit__(*args)

            def __iter__(self):
                nonlocal enumerated
                for entry in self._iterator:
                    enumerated += 1
                    yield entry

        monkeypatch.setattr(artifact_collector_module.os, "scandir", CountingScandir)

        bundle = ArtifactCollector().collect("No file operations here.", project_dir=tmpdir)

        assert bundle.scan_complete is False
        assert enumerated == 2

    def test_fallback_scan_depth_limit_is_marked_incomplete(self, monkeypatch) -> None:
        tmpdir = self._create_project({"nested/deeper/source.py": "code"})
        monkeypatch.setattr(artifact_collector_module, "MAX_SCAN_DEPTH", 1)

        bundle = ArtifactCollector().collect("No file operations here.", project_dir=tmpdir)

        assert bundle.scan_complete is False
        assert bundle.scan_error == "directory depth limit (1) exceeded"

    def test_fallback_scan_prunes_symlinks_and_irrelevant_directories(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "workspace"
            outside = Path(parent) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "src.py").write_text("inside", encoding="utf-8")
            (outside / "external.py").write_text("outside", encoding="utf-8")
            (root / ".cache").mkdir()
            (root / ".cache" / "ignored.py").write_text("ignored", encoding="utf-8")
            (root / "external-link").symlink_to(outside, target_is_directory=True)
            (root / "cycle").symlink_to(root, target_is_directory=True)

            bundle = ArtifactCollector().collect("No file operations here.", project_dir=str(root))

        assert bundle.scan_complete is True
        assert {Path(item.file_path).name for item in bundle.files} == {"src.py"}

    def test_fallback_scan_skips_special_files_without_reading(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO files are unavailable on this platform")
        fifo = tmp_path / "blocked.pipe"
        os.mkfifo(fifo)
        collector = ArtifactCollector()
        monkeypatch.setattr(
            collector,
            "_read_file",
            lambda _path: pytest.fail("fallback scanner must skip special files"),
        )

        bundle = collector.collect("No file operations here.", project_dir=str(tmp_path))

        assert bundle.scan_complete is True
        assert bundle.files == ()


@pytest.mark.parametrize("prefix", ["Write:", "Edit to:", "File:", "file_path:", "Created file"])
@pytest.mark.parametrize("name", ["plain.py", "directory with spaces/a file.py"])
@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_complete_reported_paths_preserve_spaces_and_quotes(tmp_path, prefix, name, quote):
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("reported source")
    paths = ArtifactCollector()._extract_file_paths(
        f"- {prefix} {quote}{source}{quote}\nBash: check", str(tmp_path)
    )
    assert paths == [str(source)]


@pytest.mark.parametrize("suffix", [" trailing explanation", '"', "\tinvalid"])
def test_malformed_reported_paths_do_not_read_existing_prefix(tmp_path, suffix):
    source = tmp_path / "source.py"
    source.write_text("must not guess prefix")
    assert (
        ArtifactCollector()._extract_file_paths(f"Write: {source}{suffix}\n", str(tmp_path)) == []
    )


def test_report_projection_preserves_all_collection_status(tmp_path):
    from dataclasses import replace

    from ouroboros.evaluation.artifact_collector import with_artifact_file
    from ouroboros.evaluation.models import ArtifactBundle, FileArtifact

    bundle = ArtifactBundle(
        files=(FileArtifact(file_path=str(tmp_path / "source.py"), content="source"),),
        text_summary="summary",
        total_chars=6,
        scan_complete=False,
        scan_error="directory depth limit (1) exceeded",
    )
    projected = with_artifact_file(bundle, tmp_path / "report.md", "report")
    assert replace(projected, files=bundle.files, total_chars=bundle.total_chars) == bundle


def test_space_path_extraction_keeps_security_and_deduplication(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    source = root / "source file.py"
    source.write_text("safe")
    secret = root / ".env"
    secret.write_text("not collected")
    outside = tmp_path / "outside file.py"
    outside.write_text("outside")
    link = root / "linked file.py"
    link.symlink_to(outside)
    report = "\n".join(
        f"Write: {p}" for p in (source, source, secret, outside, link, root / "missing.py")
    )
    assert ArtifactCollector()._extract_file_paths(report, str(root)) == [str(source)]
