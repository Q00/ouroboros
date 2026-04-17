"""Shared helpers for resolving packaged Ouroboros skill bundles."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib.resources
from pathlib import Path

SKILL_ENTRYPOINT = "SKILL.md"


def is_skill_bundle_dir(path: Path) -> bool:
    """Return whether ``path`` is a packaged skill bundle directory."""
    return path.is_dir() and path.joinpath(SKILL_ENTRYPOINT).is_file()


def contains_skill_bundles(skills_dir: Path) -> bool:
    """Return whether ``skills_dir`` contains at least one packaged skill bundle."""
    if not skills_dir.is_dir():
        return False

    return any(is_skill_bundle_dir(child) for child in skills_dir.iterdir())


def collect_skill_bundle_dirs(source_root: Path) -> tuple[Path, ...]:
    """Enumerate packaged skill bundle directories in deterministic order."""
    if not source_root.is_dir():
        return ()

    return tuple(
        sorted(
            (child for child in source_root.iterdir() if is_skill_bundle_dir(child)),
            key=lambda child: child.name,
        )
    )


def find_repo_root_skills_dir(anchor_file: str | Path) -> Path | None:
    """Return the repo-root ``skills`` directory for editable installs when available."""
    for parent in Path(anchor_file).resolve().parents:
        candidate = parent / "skills"
        if contains_skill_bundles(candidate):
            return candidate
    return None


@contextmanager
def resolve_packaged_skills_dir(
    *,
    skills_dir: str | Path | None = None,
    anchor_file: str | Path,
    package: str = "ouroboros.skills",
) -> Iterator[Path]:
    """Resolve the packaged skills source directory for installed and editable modes."""
    if skills_dir is not None:
        yield Path(skills_dir).expanduser()
        return

    try:
        packaged_skills = importlib.resources.files(package)
        if packaged_skills.is_dir():
            with importlib.resources.as_file(packaged_skills) as resolved_dir:
                if contains_skill_bundles(resolved_dir):
                    yield resolved_dir
                    return
    except (ImportError, FileNotFoundError, ModuleNotFoundError):
        pass

    repo_root_skills = find_repo_root_skills_dir(anchor_file)
    if repo_root_skills is not None:
        yield repo_root_skills
        return

    msg = "Packaged Ouroboros skills directory could not be located"
    raise FileNotFoundError(msg)


__all__ = [
    "SKILL_ENTRYPOINT",
    "collect_skill_bundle_dirs",
    "contains_skill_bundles",
    "find_repo_root_skills_dir",
    "is_skill_bundle_dir",
    "resolve_packaged_skills_dir",
]
