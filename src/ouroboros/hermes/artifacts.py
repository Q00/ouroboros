"""Helpers for resolving and installing packaged Hermes-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil

from ouroboros.backends.capabilities import render_backend_skill_capability_guide
from ouroboros.skills.artifacts import (
    collect_skill_bundle_dirs,
    contains_skill_bundles,
    find_repo_root_skills_dir,
    resolve_packaged_skills_dir,
)

HERMES_SKILL_CATEGORY = "autonomous-ai-agents"
HERMES_SKILL_NAME = "ouroboros"
HERMES_SKILL_CAPABILITY_GUIDE_FILENAME = "SKILL_CAPABILITY_GUIDE.md"
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


def _prepare_hermes_install_root(path: Path) -> None:
    """Create the Hermes skill root without following symlinked managed dirs."""
    _refuse_symlinked_path_component(path)
    path.mkdir(parents=True, exist_ok=True)
    _refuse_symlinked_path_component(path)


def _refuse_symlinked_path_component(path: Path) -> None:
    """Fail closed when any existing component in the install root is a symlink."""
    for candidate_path in _install_root_candidates(path):
        _refuse_symlinked_candidate_path_component(candidate_path)


def _install_root_candidates(path: Path) -> tuple[Path, ...]:
    """Return filesystem paths that may be followed for an install root."""
    if path.is_absolute():
        return (path,)

    candidates = [Path.cwd() / path]
    pwd = os.environ.get("PWD")
    if not pwd:
        return tuple(candidates)

    pwd_path = Path(pwd).expanduser()
    if not pwd_path.is_absolute():
        return tuple(candidates)

    try:
        pwd_matches_cwd = pwd_path.resolve(strict=True) == Path.cwd().resolve(strict=True)
    except OSError:
        pwd_matches_cwd = False
    if pwd_matches_cwd:
        candidates.append(pwd_path / path)

    return tuple(dict.fromkeys(candidates))


def _refuse_symlinked_candidate_path_component(path: Path) -> None:
    """Fail closed when any existing component in one install-root candidate is a symlink."""
    for component in (*reversed(path.parents), path):
        if not component.is_symlink():
            continue
        msg = (
            "Refusing to install Hermes skills into a path with a symlinked "
            f"directory component: {component}"
        )
        raise OSError(msg)


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

    if target_dir.is_symlink():
        msg = f"Refusing to install Hermes skills into symlinked directory: {target_dir}"
        raise OSError(msg)
    if target_dir.exists() and not target_dir.is_dir():
        _remove_target_path(target_dir)

    with _packaged_skills_dir() as source_root:
        _prepare_hermes_install_root(target_dir)
        source_skill_dirs = collect_skill_bundle_dirs(source_root)
        desired_skill_names = {skill_dir.name for skill_dir in source_skill_dirs}

        capability_guide_path = target_dir / HERMES_SKILL_CAPABILITY_GUIDE_FILENAME
        _remove_target_path(capability_guide_path)
        capability_guide_path.write_text(
            render_backend_skill_capability_guide("hermes"),
            encoding="utf-8",
        )

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
