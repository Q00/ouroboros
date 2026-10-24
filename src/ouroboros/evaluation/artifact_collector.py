"""ArtifactCollector — collects actual source files from execution output.

Extracts file paths from Write/Edit tool calls in execution output,
reads the files, and bundles them for the semantic evaluator.
Falls back to scanning project_dir for source files when no paths
can be extracted from the artifact text.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import logging
import os
from pathlib import Path
import re
import stat

from ouroboros.config.models import get_config_dir
from ouroboros.evaluation.models import ArtifactBundle, FileArtifact

logger = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 150_000  # ~37K tokens
MAX_FILE_SIZE = 100 * 1024  # 100KB per file (accommodate large artifact summaries)
MAX_SCAN_ENTRIES = 50_000
MAX_SCAN_DEPTH = 32

EVAL_ARTIFACT_FILENAME = ".ouroboros_eval_artifact.md"


def eval_artifact_path(project_dir: Path) -> Path:
    """Where evaluation keeps one project's artifact text: the data dir, never the project.

    Written into the project, the file landed in the user's tree and shipped
    in their patch. It is keyed by the resolved project path so concurrent
    evaluations of different projects do not share one file.
    """
    key = hashlib.sha256(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return get_config_dir() / "data" / "eval_artifacts" / key / EVAL_ARTIFACT_FILENAME


def with_artifact_file(bundle: ArtifactBundle, path: Path, text: str) -> ArtifactBundle:
    """Put the artifact text first among ``bundle``'s files.

    The evaluation prompt omits the inline artifact text whenever file
    artifacts are present, so the artifact must be one of the files. A bundle
    without files keeps the inline text and is returned unchanged.
    """
    if not bundle.files or any(item.file_path == str(path) for item in bundle.files):
        return bundle
    content = text[:MAX_FILE_SIZE]
    artifact = FileArtifact(
        file_path=str(path), content=content, truncated=len(content) < len(text)
    )
    return replace(
        bundle,
        files=(artifact, *bundle.files),
        total_chars=bundle.total_chars + len(content),
    )


# Binary/generated extensions to skip — everything else is collected.
_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Images
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".bmp",
        # Fonts
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        # Media
        ".mp4",
        ".mp3",
        ".wav",
        ".avi",
        ".mov",
        ".webm",
        # Archives
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        # Compiled/binary
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".o",
        ".a",
        ".wasm",
        # Lockfiles / generated
        ".lock",
        ".map",
        ".min.js",
        ".min.css",
        # Data dumps
        ".sqlite",
        ".db",
        ".bin",
        ".dat",
    }
)

# Directories to skip during directory scan fallback.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".next",
        "dist",
        "build",
        ".venv",
        "venv",
        ".cache",
        ".tox",
        "coverage",
        ".nyc_output",
    }
)

# Sensitive filenames that must never be collected — blocks credential
# leaks via artifact bundles (env files, SSH keys, cloud credentials, etc.).
_SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.staging",
        ".env.test",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".my.cnf",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials.json",
        "service-account.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        ".htpasswd",
    }
)

# Sensitive file extensions (certificates, private keys, terraform vars).
_SENSITIVE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pem",
        ".key",
        ".crt",
        ".cert",
        ".pfx",
        ".p12",
        ".keystore",
        ".jks",
        ".tfvars",
    }
)

# Allowlist for legitimate template/example env files — these are
# commonly checked into repositories and contain no real secrets.
_ENV_TEMPLATE_ALLOWLIST: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.dist",
        ".env.template",
    }
)


def _is_sensitive_file(fname: str) -> bool:
    """Return True when ``fname`` should be excluded as sensitive.

    The check is case-insensitive. Template/example env files listed in
    ``_ENV_TEMPLATE_ALLOWLIST`` are explicitly permitted. Any file whose
    name is in ``_SENSITIVE_FILENAMES``, whose extension is in
    ``_SENSITIVE_EXTENSIONS``, or whose name starts with ``.env.``
    (catching unlisted dotenv variants) is treated as sensitive.
    """
    lowered = fname.lower()
    if lowered in _ENV_TEMPLATE_ALLOWLIST:
        return False
    if lowered in _SENSITIVE_FILENAMES:
        return True
    ext = os.path.splitext(lowered)[1]
    if ext in _SENSITIVE_EXTENSIONS:
        return True
    # Catch unlisted dotenv variants (e.g. .env.ci, .env.foo).
    return lowered.startswith(".env.")


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
        scan_complete = True
        scan_error: str | None = None
        used_fallback_scan = not file_paths
        if not file_paths:
            # Fallback: scan project_dir for source files when the artifact
            # text doesn't contain recognizable file path patterns (e.g.
            # prose summaries from orchestrator execution).
            file_paths, scan_complete, scan_error = self._scan_directory(project_dir)

        artifacts: list[FileArtifact] = []
        total_chars = 0

        for path in file_paths:
            content = self._read_file(path)
            if content is None:
                if used_fallback_scan:
                    scan_complete = False
                    scan_error = scan_error or "a scanned file could not be read"
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
            scan_complete=scan_complete,
            scan_error=scan_error,
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
            r"(?:Write|Edit)(?:[ \t]+to)?:[ \t]*([^\r\n]+)",
            r"(?:file_path|File):[ \t]*([^\r\n]+)",
            r"Created[ \t]+(?:file[ \t]+)?([^\r\n]+)",
        ]

        real_project = os.path.realpath(project_dir) if project_dir else None

        paths: list[str] = []
        seen: set[str] = set()

        for pattern in patterns:
            for match in re.finditer(pattern, output):
                # Native evidence renders the complete path on one line. Do not
                # split at spaces or guess a prefix when trailing prose or an
                # unmatched quote makes the reported path invalid.
                path = match.group(1).strip()
                if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
                    path = path[1:-1]
                if not path.startswith("/"):
                    continue
                if path in seen:
                    continue
                # Block sensitive files (env, credentials, private keys) even
                # when they appear in tool-call output.
                if _is_sensitive_file(os.path.basename(path)):
                    continue
                try:
                    if not os.path.isfile(path):
                        continue
                    # Path boundary check: only allow files within project_dir
                    if real_project and not os.path.realpath(path).startswith(
                        real_project + os.sep
                    ):
                        continue
                except OSError:
                    # File vanished between stat calls, permission denied,
                    # or unreadable symlink — skip defensively.
                    continue
                paths.append(path)
                seen.add(path)

        return paths

    def _scan_directory(self, project_dir: str) -> tuple[list[str], bool, str | None]:
        """Walk project_dir for source files when no paths were extractable.

        Skips binary/generated files and common dependency/cache directories.
        Does not follow symlinks. Returns an incomplete status when the scan
        cannot cover the full workspace within its entry and depth limits.
        Files are sorted by modification time (newest first) so the evaluator
        sees the most recently changed code. The total character budget
        (MAX_TOTAL_CHARS) limits how many are actually read upstream.
        """
        real_project = os.path.realpath(project_dir)
        paths: list[tuple[float, str]] = []
        if not os.path.isdir(real_project):
            return [], False, "workspace directory is unavailable"

        scan_error: str | None = None
        visited = 0
        pending_directories = [(real_project, 0)]

        while pending_directories:
            root, depth = pending_directories.pop()
            try:
                with os.scandir(root) as iterator:
                    entries = []
                    for entry in iterator:
                        visited += 1
                        if visited >= MAX_SCAN_ENTRIES:
                            scan_error = f"entry budget ({MAX_SCAN_ENTRIES}) reached"
                            break
                        entries.append(entry)
            except OSError as error:
                scan_error = f"unreadable directory: {error.filename or '<unknown>'}"
                break

            child_directories: list[str] = []
            for entry in sorted(entries, key=lambda item: item.name):
                full = entry.path
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                            continue
                        if depth >= MAX_SCAN_DEPTH:
                            scan_error = f"directory depth limit ({MAX_SCAN_DEPTH}) exceeded"
                            break
                        child_directories.append(full)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    fname = entry.name
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in _SKIP_EXTENSIONS or _is_sensitive_file(fname):
                        continue
                    if os.path.getsize(full) > MAX_FILE_SIZE:
                        continue
                    mtime = os.path.getmtime(full)
                except OSError:
                    # A partial inventory must not be passed to the semantic
                    # evaluator as if the scan covered the whole workspace.
                    scan_error = f"unreadable path: {entry.name}"
                    break
                paths.append((mtime, full))

            if scan_error:
                break
            pending_directories.extend((path, depth + 1) for path in reversed(child_directories))

        # Newest files first — most likely to be the implementation.
        paths.sort(reverse=True)
        return [p for _, p in paths], scan_error is None, scan_error

    def _read_file(self, file_path: str) -> str | None:
        """Read a file with size limits."""
        try:
            if not stat.S_ISREG(os.stat(file_path).st_mode):
                return None
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

        # Look for current Task sections and legacy AC sections that reference this file.
        sections = re.split(r"### (?:Task|AC) (\d+):", output)
        for i in range(1, len(sections) - 1, 2):
            task_num = int(sections[i])
            section_text = re.split(r"### (?:Task|AC) \d+:", sections[i + 1])[0]
            if basename in section_text or file_path in section_text:
                indices.append(task_num - 1)  # Convert to 0-based

        return indices
