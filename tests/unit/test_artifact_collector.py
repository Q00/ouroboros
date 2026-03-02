"""Unit tests for ArtifactCollector."""

from __future__ import annotations

import os
import tempfile

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

    def test_no_paths_in_output(self) -> None:
        tmpdir = self._create_project({"a.py": "code"})
        output = "No file operations here."

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 0
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

    def test_ac_association_extraction(self) -> None:
        tmpdir = self._create_project({"task.py": "code"})
        path = os.path.join(tmpdir, "task.py")
        output = f"### AC 2: [PASS] Create tasks\nWrite: {path}\n### AC 3: [FAIL] Delete tasks\nOther stuff\n"

        collector = ArtifactCollector()
        bundle = collector.collect(output, project_dir=tmpdir)
        assert len(bundle.files) == 1
        assert 1 in bundle.files[0].ac_indices  # AC 2 → 0-based index 1
