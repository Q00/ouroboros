"""Helpers for resolving project directories from seed and runtime context."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_path_against_base(raw_path: str | Path | None, *, stable_base: Path) -> Path | None:
    """Resolve a path against a stable base directory."""
    if raw_path is None:
        return None

    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (stable_base / candidate).resolve()


def project_path_candidates_from_seed(seed: Any) -> tuple[str, ...]:
    """Extract likely project directories from seed metadata and brownfield refs."""
    if seed is None:
        return ()

    candidates: list[str] = []
    seed_meta = getattr(seed, "metadata", None)
    if seed_meta is not None:
        project_dir = getattr(seed_meta, "project_dir", None) or getattr(
            seed_meta,
            "working_directory",
            None,
        )
        if isinstance(project_dir, str) and project_dir:
            candidates.append(project_dir)

    brownfield_context = getattr(seed, "brownfield_context", None)
    context_references = getattr(brownfield_context, "context_references", ()) or ()

    for reference in context_references:
        path = getattr(reference, "path", None)
        role = getattr(reference, "role", None)
        if isinstance(path, str) and path and role == "primary":
            candidates.append(path)

    for reference in context_references:
        path = getattr(reference, "path", None)
        if isinstance(path, str) and path and path not in candidates:
            candidates.append(path)

    return tuple(candidates)


def resolve_seed_project_path(seed: Any, *, stable_base: Path) -> Path | None:
    """Resolve the highest-priority project path encoded in a seed."""
    for candidate in project_path_candidates_from_seed(seed):
        resolved = resolve_path_against_base(candidate, stable_base=stable_base)
        if resolved is not None:
            return resolved
    return None
