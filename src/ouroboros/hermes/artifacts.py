"""Helpers for resolving and installing packaged Hermes-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

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
_SWAP_MARKER = ".ouroboros-managed-swap"
_SWAP_MARKER_CONTENT = "ouroboros-hermes-swap-v1\n"


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
    backup_prefix = f".{target_dir.name}.old."

    # Validate the complete destination chain before inspecting or mutating
    # any target or recovery path beneath it.
    _prepare_hermes_install_root(target_dir.parent)
    if target_dir.is_symlink():
        msg = f"Refusing to install Hermes skills into symlinked directory: {target_dir}"
        raise OSError(msg)
    if target_dir.exists() and not target_dir.is_dir():
        _remove_target_path(target_dir)
    live_marker = target_dir / _SWAP_MARKER
    if target_dir.exists() and (live_marker.exists() or live_marker.is_symlink()):
        msg = f"Refusing to overwrite reserved Hermes swap marker: {live_marker}"
        raise OSError(msg)
    managed_backups = [
        candidate
        for candidate in target_dir.parent.glob(f"{backup_prefix}*")
        if not candidate.is_symlink()
        and candidate.is_dir()
        and not candidate.joinpath(_SWAP_MARKER).is_symlink()
        and candidate.joinpath(_SWAP_MARKER).is_file()
        and candidate.joinpath(_SWAP_MARKER).read_text(encoding="utf-8") == _SWAP_MARKER_CONTENT
    ]
    if managed_backups and not target_dir.exists():
        if len(managed_backups) != 1:
            msg = "Refusing ambiguous Hermes skill recovery with multiple managed backups"
            raise OSError(msg)
        os.replace(managed_backups[0], target_dir)
    elif managed_backups:
        # A previous publish may have succeeded before its old-generation
        # cleanup failed.  Remove every stale managed backup before moving the
        # current live generation so recovery can never choose by random UUID.
        for managed_backup in managed_backups:
            _remove_target_path(managed_backup)

    with _packaged_skills_dir() as source_root:
        source_skill_dirs = collect_skill_bundle_dirs(source_root)
        desired_skill_names = {skill_dir.name for skill_dir in source_skill_dirs}

        # Build the complete replacement beside the live generation.  A
        # mid-copy failure must never remove a previously working install.
        staging_dir = Path(tempfile.mkdtemp(prefix=".ouroboros-skills-", dir=target_dir.parent))
        cleanup_staging_dir: Path | None = staging_dir
        try:
            if target_dir.is_dir():
                shutil.copytree(target_dir, staging_dir, dirs_exist_ok=True, symlinks=True)

            capability_guide_path = staging_dir / HERMES_SKILL_CAPABILITY_GUIDE_FILENAME
            _remove_target_path(capability_guide_path)
            capability_guide_path.write_text(
                render_backend_skill_capability_guide("hermes"),
                encoding="utf-8",
            )

            for artifact_name in _LEGACY_PACKAGE_ARTIFACTS:
                _remove_target_path(staging_dir / artifact_name)
            _remove_target_path(staging_dir / _SWAP_MARKER)

            for source_skill_dir in source_skill_dirs:
                destination_skill_dir = staging_dir / source_skill_dir.name
                _remove_target_path(destination_skill_dir)
                shutil.copytree(source_skill_dir, destination_skill_dir, symlinks=True)

            if prune:
                for existing_path in staging_dir.iterdir():
                    if existing_path.name in desired_skill_names:
                        continue
                    if (
                        existing_path.is_dir()
                        and existing_path.joinpath(_SKILL_ENTRYPOINT).is_file()
                    ):
                        _remove_target_path(existing_path)

            backup_dir = target_dir.with_name(f"{backup_prefix}{uuid4().hex}")
            if target_dir.exists() or target_dir.is_symlink():
                marker_path = target_dir / _SWAP_MARKER
                previous_marker = marker_path.read_bytes() if marker_path.is_file() else None
                try:
                    marker_path.write_text(_SWAP_MARKER_CONTENT, encoding="utf-8")
                    os.replace(target_dir, backup_dir)
                except BaseException:
                    if previous_marker is None:
                        _remove_target_path(marker_path)
                    else:
                        marker_path.write_bytes(previous_marker)
                    raise
            try:
                os.replace(staging_dir, target_dir)
            except OSError:
                if backup_dir.exists() and not target_dir.exists():
                    os.replace(backup_dir, target_dir)
                raise
            _remove_target_path(backup_dir)
            cleanup_staging_dir = None
        finally:
            if cleanup_staging_dir is not None:
                _remove_target_path(cleanup_staging_dir)

    return target_dir
