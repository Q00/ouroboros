"""ArtifactCollector — collects actual source files from execution output.

Extracts file paths from Write/Edit tool calls in execution output,
reads the files, and bundles them for the semantic evaluator.
"""

from __future__ import annotations

import logging
import os
import re

from ouroboros.evaluation.models import ArtifactBundle, FileArtifact

logger = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 150_000  # ~37K tokens
MAX_FILES = 30
MAX_FILE_SIZE = 50 * 1024  # 50KB per file


class ArtifactCollector:
    """Collects file artifacts from execution output.

    Parses execution output for file paths referenced in Write/Edit
    tool calls, reads the actual files, and bundles them with AC
    association and token budget management.
    """

    def collect(
        self,
        execution_output: str,
        project_dir: str | None = None,
    ) -> ArtifactBundle:
        """Collect file artifacts from execution output.

        Args:
            execution_output: Raw execution output text.
            project_dir: Project root directory. If None, skips collection.

        Returns:
            ArtifactBundle with collected files.
        """
        if not project_dir:
            return ArtifactBundle(text_summary=execution_output)

        file_paths = self._extract_file_paths(execution_output, project_dir)
        if not file_paths:
            return ArtifactBundle(text_summary=execution_output)

        artifacts: list[FileArtifact] = []
        total_chars = 0

        for path in file_paths[:MAX_FILES]:
            content = self._read_file(path)
            if content is None:
                continue

            truncated = False
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                break

            if len(content) > remaining:
                content = content[:remaining]
                truncated = True

            # Extract AC associations from output context
            ac_indices = self._find_ac_associations(path, execution_output)

            artifacts.append(
                FileArtifact(
                    file_path=path,
                    content=content,
                    ac_indices=tuple(ac_indices),
                    truncated=truncated,
                )
            )
            total_chars += len(content)

        return ArtifactBundle(
            files=tuple(artifacts),
            text_summary=execution_output,
            total_chars=total_chars,
        )

    def _extract_file_paths(
        self,
        output: str,
        project_dir: str | None = None,
    ) -> list[str]:
        """Extract file paths from Write/Edit tool calls in output.

        Validates that paths are within project_dir to prevent
        reading arbitrary files from the filesystem.
        """
        # Match common patterns from Claude tool use output
        patterns = [
            r"(?:Write|Edit)(?:\s+to)?:\s*(/[^\s]+)",
            r"(?:file_path|File):\s*(/[^\s]+)",
            r"Created\s+(?:file\s+)?(/[^\s]+)",
        ]

        real_project = os.path.realpath(project_dir) if project_dir else None

        paths: list[str] = []
        seen: set[str] = set()

        for pattern in patterns:
            for match in re.finditer(pattern, output):
                path = match.group(1)
                if path in seen:
                    continue
                if not os.path.isfile(path):
                    continue
                # Path boundary check: only allow files within project_dir
                if real_project and not os.path.realpath(path).startswith(real_project + os.sep):
                    continue
                paths.append(path)
                seen.add(path)

        return paths

    def _read_file(self, file_path: str) -> str | None:
        """Read a file with size limits."""
        try:
            size = os.path.getsize(file_path)
            if size > MAX_FILE_SIZE:
                return None
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, PermissionError):
            return None

    def _find_ac_associations(
        self,
        file_path: str,
        output: str,
    ) -> list[int]:
        """Find which ACs are associated with a file path."""
        indices: list[int] = []
        basename = os.path.basename(file_path)

        # Look for AC sections that reference this file
        ac_sections = re.split(r"### AC (\d+):", output)
        for i in range(1, len(ac_sections) - 1, 2):
            ac_num = int(ac_sections[i])
            section_text = ac_sections[i + 1].split("### AC")[0]
            if basename in section_text or file_path in section_text:
                indices.append(ac_num - 1)  # Convert to 0-based

        return indices
