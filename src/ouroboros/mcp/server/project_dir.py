"""Infer a project directory from a Seed or from execution output.

Evaluation needs a working directory to run mechanical checks in, and the Seed
does not always carry one. These helpers recover it from what is available: the
Seed's own metadata, its brownfield references, or -- failing both -- the paths
an execution wrote to, walked upward until something that looks like a project
root appears.

Split out of ``mcp/server/adapter.py`` (Q00/ouroboros#1754), which re-exports
them. Nothing here is about being an MCP server; the adapter was only their
address.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

_PROJECT_ROOT_MARKERS = (
    # VCS (most universal — nearly every project has one)
    ".git",
    # Python
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    # Node.js
    "package.json",
    # Rust
    "Cargo.toml",
    # Go
    "go.mod",
    # Java / Kotlin
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    # Ruby
    "Gemfile",
    # PHP
    "composer.json",
)


def _looks_like_project_root(path: object) -> bool:
    """Return True when the given path looks like a project root."""
    if not isinstance(path, Path):
        return False

    return any((path / marker).exists() for marker in _PROJECT_ROOT_MARKERS)


def _project_dir_from_seed(seed: Any) -> str | None:
    """Extract a likely project directory from seed metadata or brownfield context."""
    if seed is None:
        return None

    seed_meta = getattr(seed, "metadata", None)
    if seed_meta:
        project_dir = getattr(seed_meta, "project_dir", None) or getattr(
            seed_meta,
            "working_directory",
            None,
        )
        if project_dir:
            return str(project_dir)

    brownfield_context = getattr(seed, "brownfield_context", None)
    context_references = getattr(brownfield_context, "context_references", ()) or ()

    for reference in context_references:
        path = getattr(reference, "path", None)
        role = getattr(reference, "role", None)
        if isinstance(path, str) and path and role == "primary":
            return path

    for reference in context_references:
        path = getattr(reference, "path", None)
        if isinstance(path, str) and path:
            return path

    return None


def _project_dir_from_artifact(artifact: str) -> str | None:
    """Extract a likely project root from Write/Edit/File tool output."""
    # Match quoted paths (spaces allowed) and unquoted paths.
    # Examples:  Write: /foo/bar.py  |  File: "/path with spaces/bar.py"
    write_matches: list[str] = []
    for m in re.finditer(r'(?:Write|Edit|File): (?:"([^"]+)"|(.+))', artifact):
        path_candidate = m.group(1) or m.group(2)
        if path_candidate:
            write_matches.append(path_candidate.strip())
    for path_str in write_matches:
        candidate = Path(path_str).parent
        for _ in range(10):
            if _looks_like_project_root(candidate):
                return str(candidate)
            if candidate == candidate.parent:
                break
            candidate = candidate.parent

    return None


__all__ = [
    "_PROJECT_ROOT_MARKERS",
    "_looks_like_project_root",
    "_project_dir_from_artifact",
    "_project_dir_from_seed",
]
