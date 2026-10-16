"""Lexical index for the brownfield registry.

Scan and register time derive a short search document per repository from
files that are cheap to read (README head, package manifest name and
description, top-level entry names). The document is stored next to the
registry row so ranking a query never touches the filesystem.

A stat-only fingerprint of the files the document is built from lets a
rescan skip repositories whose inputs did not change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Any

from ouroboros.core.bm25 import rank
from ouroboros.persistence.brownfield import BrownfieldRepo

_README_NAMES: tuple[str, ...] = ("README.md", "README.rst", "README.txt", "README", "CLAUDE.md")
_MANIFEST_NAMES: tuple[str, ...] = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
_README_HEAD_CHARS = 2000
_MANIFEST_MAX_BYTES = 256 * 1024
_MAX_TOP_LEVEL_ENTRIES = 60
_PATH_SEGMENTS = 3

# Field weights, applied by repeating a field's text in the document.
_NAME_WEIGHT = 3
_DESC_WEIGHT = 2
_MANIFEST_WEIGHT = 2


def repo_fingerprint(repo_path: Path) -> str:
    """Return a stat-only fingerprint of the inputs to :func:`build_repo_document`.

    It covers the repository root's mtime (which changes when top-level
    entries are added or removed) and the size and mtime of every README and
    manifest candidate. No file contents are read.
    """
    parts: list[str] = []
    try:
        parts.append(f".:{repo_path.stat().st_mtime_ns}")
    except OSError:
        return ""
    for name in (*_README_NAMES, *_MANIFEST_NAMES):
        try:
            st = (repo_path / name).stat()
        except OSError:
            continue
        parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha1("\n".join(parts).encode(), usedforsecurity=False).hexdigest()


def _read_head(path: Path, max_chars: int) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read(max_chars)
    except OSError:
        return ""


def _string_fields(table: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("name", "description"):
        value = table.get(key)
        if isinstance(value, str):
            values.append(value)
    keywords = table.get("keywords")
    if isinstance(keywords, list):
        values.extend(k for k in keywords if isinstance(k, str))
    return values


def _manifest_text(repo_path: Path) -> list[str]:
    """Name, description and keywords from the package manifests present."""
    values: list[str] = []
    for name in _MANIFEST_NAMES:
        path = repo_path / name
        try:
            if not path.is_file() or path.stat().st_size > _MANIFEST_MAX_BYTES:
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            if name == "pyproject.toml":
                project = tomllib.loads(raw).get("project")
                if isinstance(project, dict):
                    values.extend(_string_fields(project))
            elif name == "Cargo.toml":
                package = tomllib.loads(raw).get("package")
                if isinstance(package, dict):
                    values.extend(_string_fields(package))
            elif name == "package.json":
                data = json.loads(raw)
                if isinstance(data, dict):
                    values.extend(_string_fields(data))
            elif name == "go.mod":
                for line in raw.splitlines():
                    if line.startswith("module "):
                        values.append(line.removeprefix("module ").strip())
                        break
        except (tomllib.TOMLDecodeError, ValueError):
            continue
    return values


def _top_level_names(repo_path: Path) -> list[str]:
    try:
        names = sorted(n for n in os.listdir(repo_path) if not n.startswith("."))
    except OSError:
        return []
    return names[:_MAX_TOP_LEVEL_ENTRIES]


def build_repo_document(repo_path: Path) -> str:
    """Build the stored search document for one repository.

    Reads at most the README head, small manifests, and one directory
    listing. The repository name, description, and path are not included:
    they live in the registry row and are added at query time, so a renamed
    repository ranks under its current name.
    """
    sections: list[str] = []
    manifest = _manifest_text(repo_path)
    if manifest:
        sections.extend([" ".join(manifest)] * _MANIFEST_WEIGHT)
    for name in _README_NAMES:
        path = repo_path / name
        if path.is_file():
            head = _read_head(path, _README_HEAD_CHARS)
            if head:
                sections.append(head)
                break
    top = _top_level_names(repo_path)
    if top:
        sections.append(" ".join(top))
    return "\n".join(sections)


def index_repo(repo_path: Path, known_fingerprint: str | None) -> tuple[str, str] | None:
    """Return ``(document, fingerprint)``, or None when the fingerprint is unchanged."""
    fingerprint = repo_fingerprint(repo_path)
    if known_fingerprint is not None and fingerprint == known_fingerprint:
        return None
    return build_repo_document(repo_path), fingerprint


def query_document(repo: BrownfieldRepo, stored_document: str | None) -> str:
    """Compose the ranked text for a repo from its registry row and stored document.

    Repos without a stored document (registered before indexing existed, or
    not rescanned yet) still rank on name, description, and path segments.
    """
    segments = Path(repo.path).parts[-_PATH_SEGMENTS:]
    sections = [repo.name] * _NAME_WEIGHT
    if repo.desc:
        sections.extend([repo.desc] * _DESC_WEIGHT)
    sections.append(" ".join(segments))
    if stored_document:
        sections.append(stored_document)
    return "\n".join(sections)


def rank_repos(
    query: str,
    repos: Sequence[BrownfieldRepo],
    documents: Mapping[str, str],
) -> list[tuple[BrownfieldRepo, float]]:
    """Rank registered repos for ``query``; ties break by name, then path."""
    texts = [query_document(r, documents.get(r.path)) for r in repos]
    tie_keys = [f"{r.name.lower()}\0{r.path}" for r in repos]
    return [(repos[s.index], s.score) for s in rank(query, texts, tie_keys=tie_keys)]


__all__ = [
    "build_repo_document",
    "index_repo",
    "query_document",
    "rank_repos",
    "repo_fingerprint",
]
