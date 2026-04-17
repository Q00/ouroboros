"""Helpers for resolving and installing packaged Hermes-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import shutil

from ouroboros.skills.artifacts import (
    collect_skill_bundle_dirs,
    contains_skill_bundles,
    find_repo_root_skills_dir,
    resolve_packaged_skills_dir,
)

HERMES_SKILL_CATEGORY = "autonomous-ai-agents"
HERMES_SKILL_NAME = "ouroboros"
_SKILL_ENTRYPOINT = "SKILL.md"
_LEGACY_PACKAGE_ARTIFACTS = ("__init__.py", "artifacts.py", "__pycache__")


def _contains_skill_bundles(skills_dir: Path) -> bool:
    """Return whether ``skills_dir`` contains at least one packaged skill bundle."""
    return contains_skill_bundles(skills_dir)


def _repo_root_skills_dir() -> Path | None:
    """Return the repo-root ``skills`` directory for editable installs when available."""
    return find_repo_root_skills_dir(__file__)


@contextmanager
def _packaged_skills_dir() -> Iterator[Path]:
    """Resolve the packaged skills source directory."""
    repo_root_skills = _repo_root_skills_dir()
    if repo_root_skills is not None:
        yield repo_root_skills
        return

    with resolve_packaged_skills_dir(anchor_file=__file__) as resolved_dir:
        yield resolved_dir


def _remove_target_path(path: Path) -> None:
    """Remove a file, directory, or symlink path."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def install_hermes_skills(
    *,
    hermes_dir: str | Path | None = None,
    prune: bool = False,
) -> Path:
    """Install packaged Ouroboros skills into ~/.hermes/skills/autonomous-ai-agents/ouroboros/."""
    resolved_hermes_dir = (
        Path(hermes_dir).expanduser() if hermes_dir is not None else Path.home() / ".hermes"
    )

    target_dir = resolved_hermes_dir / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME

    if target_dir.exists() and not target_dir.is_dir():
        _remove_target_path(target_dir)

    with _packaged_skills_dir() as source_root:
        target_dir.mkdir(parents=True, exist_ok=True)
        source_skill_dirs = collect_skill_bundle_dirs(source_root)
        desired_skill_names = {skill_dir.name for skill_dir in source_skill_dirs}

        for artifact_name in _LEGACY_PACKAGE_ARTIFACTS:
            _remove_target_path(target_dir / artifact_name)

        for source_skill_dir in source_skill_dirs:
            destination_skill_dir = target_dir / source_skill_dir.name
            _remove_target_path(destination_skill_dir)
            shutil.copytree(source_skill_dir, destination_skill_dir)

        if prune:
            for existing_path in target_dir.iterdir():
                if existing_path.name in desired_skill_names:
                    continue
                if existing_path.is_dir() and existing_path.joinpath(_SKILL_ENTRYPOINT).is_file():
                    _remove_target_path(existing_path)

    return target_dir
