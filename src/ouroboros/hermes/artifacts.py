"""Helpers for resolving and installing packaged Hermes-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
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
_SWAP_INTENT_SUFFIX = ".intent"
_CLEANUP_MARKER = ".ouroboros-managed-cleanup"
_CLEANUP_ACTIVE = ".ouroboros-cleanup-active"
_EXPECTED_UNSET = object()


def _tree_fingerprint(path: Path) -> tuple[object, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink():
        return ("link", os.readlink(path))
    if path.is_file():
        return ("file", info.st_mode, path.read_bytes())
    if path.is_dir():
        return (
            "dir",
            info.st_mode,
            tuple((child.name, _tree_fingerprint(child)) for child in sorted(path.iterdir())),
        )
    return ("other", info.st_mode)


def _swap_intent_path(backup: Path) -> Path:
    return backup.with_name(backup.name + _SWAP_INTENT_SUFFIX)


def _publish_swap_intent(intent: Path, content: str, token: str) -> None:
    """Publish an intent atomically so recovery never sees partial bytes."""
    staging = intent.with_name(intent.name + f".staging.{token}")
    try:
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, intent)
    except BaseException:
        _remove_target_path(staging)
        raise


def _fingerprint_digest(fingerprint: tuple[object, ...] | None) -> str:
    return hashlib.sha256(repr(fingerprint).encode()).hexdigest()


def _intent_content(operation: str, backup: Path, token: str, digest: str) -> str:
    return f"ouroboros-hermes-{operation}-v2:{backup.name}:{token}:{digest}\n"


def _ownership_content(operation: str, backup: Path, token: str, digest: str) -> str:
    return f"ouroboros-hermes-owned-v2:{operation}:{backup.name}:{token}:{digest}\n"


def _owned_backup_fingerprint(backup: Path) -> tuple[object, ...] | None:
    fingerprint = _tree_fingerprint(backup)
    if fingerprint is None or fingerprint[0] != "dir":
        return fingerprint
    children = tuple(child for child in fingerprint[2] if child[0] != _SWAP_MARKER)
    return (*fingerprint[:2], children)


def _restore_owned_backup(backup: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise OSError(f"Refusing to overwrite concurrent Hermes generation: {target}")
    os.replace(backup, target)
    try:
        _remove_target_path(target / _SWAP_MARKER)
    except BaseException:
        if target.exists() and not backup.exists():
            os.replace(target, backup)
        raise


def _read_swap_record(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise OSError(f"Refusing malformed Hermes swap record: {path}") from exc


def _assert_owned_swap_backup(
    path: Path,
    *,
    operation: str,
    token: str,
    digest: str,
    backup_identity: Path | None = None,
) -> None:
    """Revalidate exact backup ownership at a destructive boundary."""
    backup = backup_identity or path
    marker = path / _SWAP_MARKER
    if (
        path.is_symlink()
        or not path.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or _read_swap_record(marker) != _ownership_content(operation, backup, token, digest)
        or _fingerprint_digest(_owned_backup_fingerprint(path)) != digest
    ):
        raise OSError(f"Refusing foreign Hermes swap backup: {path}")


def _marker_staging_path(target: Path, token: str) -> Path:
    return target.with_name(f".{target.name}.marker.{token}")


def _retirement_path(backup: Path, token: str) -> Path:
    return backup.with_name(f".{backup.name.removeprefix('.')}.retired.{token}")


def _cleanup_path(backup: Path, token: str) -> Path:
    return backup.with_name(f".{backup.name.removeprefix('.')}.cleanup.{token}")


def _cleanup_content(operation: str, backup: Path, token: str, digest: str) -> str:
    return f"ouroboros-hermes-cleanup-v1:{operation}:{backup.name}:{token}:{digest}\n"


def _assert_owned_cleanup(
    cleanup: Path,
    backup: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> None:
    marker = cleanup / _CLEANUP_MARKER
    if (
        cleanup.is_symlink()
        or not cleanup.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or _read_swap_record(marker) != _cleanup_content(operation, backup, token, digest)
        or {child.name for child in cleanup.iterdir()}
        - {_CLEANUP_MARKER, _CLEANUP_ACTIVE, "generation"}
    ):
        raise OSError(f"Refusing foreign Hermes cleanup container: {cleanup}")


def _finish_owned_cleanup(
    cleanup: Path,
    backup: Path,
    intent: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> None:
    """Finish deletion only inside an authenticated private container."""
    _assert_owned_cleanup(
        cleanup,
        backup,
        operation=operation,
        token=token,
        digest=digest,
    )
    generation = cleanup / "generation"
    active = cleanup / _CLEANUP_ACTIVE
    if generation.exists() or generation.is_symlink():
        if not active.exists():
            _assert_owned_swap_backup(
                generation,
                operation=operation,
                token=token,
                digest=digest,
                backup_identity=backup,
            )
            _publish_swap_intent(
                active,
                _cleanup_content(operation, backup, token, digest),
                token,
            )
        elif (
            active.is_symlink()
            or not active.is_file()
            or _read_swap_record(active) != _cleanup_content(operation, backup, token, digest)
        ):
            raise OSError(f"Refusing foreign Hermes cleanup state: {active}")
        _assert_owned_swap_backup(
            generation,
            operation=operation,
            token=token,
            digest=digest,
            backup_identity=backup,
        )
        # Portable pathname APIs cannot close the fingerprint-to-unlink race
        # for every descendant.  Preserve the authenticated prior generation
        # with one atomic directory rename instead of risking deletion of a
        # concurrent replacement.
        retirement = _retirement_path(backup, token)
        if retirement.exists() or retirement.is_symlink():
            raise OSError(f"Refusing occupied Hermes retirement path: {retirement}")
        os.replace(generation, retirement)
    _remove_target_path(active)
    _remove_target_path(cleanup / _CLEANUP_MARKER)
    cleanup.rmdir()
    intent.unlink(missing_ok=True)


def _ownership_marker_matches(
    target: Path,
    backup: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> bool:
    marker = target / _SWAP_MARKER
    return (
        marker.is_file()
        and not marker.is_symlink()
        and _read_swap_record(marker) == _ownership_content(operation, backup, token, digest)
    )


def _publish_ownership_marker(
    target: Path,
    backup: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> None:
    """Publish an ownership marker without exposing partial canonical bytes."""
    staging = _marker_staging_path(target, token)
    try:
        staging.write_text(_ownership_content(operation, backup, token, digest), encoding="utf-8")
        os.replace(staging, target / _SWAP_MARKER)
    except BaseException:
        _remove_target_path(staging)
        raise


def _retire_owned_backup(
    backup: Path,
    intent: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> None:
    """Atomically detach an owned backup before fallible recursive cleanup."""
    _assert_owned_swap_backup(backup, operation=operation, token=token, digest=digest)
    retirement = _retirement_path(backup, token)
    if retirement.exists() or retirement.is_symlink():
        raise OSError(f"Refusing occupied Hermes retirement path: {retirement}")
    try:
        os.replace(backup, retirement)
    except BaseException:
        if retirement.exists() and not backup.exists():
            _assert_owned_swap_backup(
                retirement,
                operation=operation,
                token=token,
                digest=digest,
                backup_identity=backup,
            )
        raise
    _finish_owned_retirement(
        retirement,
        backup,
        intent,
        operation=operation,
        token=token,
        digest=digest,
    )


def _finish_owned_retirement(
    retirement: Path,
    backup: Path,
    intent: Path,
    *,
    operation: str,
    token: str,
    digest: str,
) -> None:
    """Validate and finish a committed backup-to-retirement rename."""
    cleanup = _cleanup_path(backup, token)
    if cleanup.exists() or cleanup.is_symlink():
        if retirement.exists() or retirement.is_symlink():
            raise OSError("Refusing ambiguous Hermes retirement and cleanup generations")
        _finish_owned_cleanup(
            cleanup,
            backup,
            intent,
            operation=operation,
            token=token,
            digest=digest,
        )
        return
    try:
        _assert_owned_swap_backup(
            retirement,
            operation=operation,
            token=token,
            digest=digest,
            backup_identity=backup,
        )
    except BaseException:
        if retirement.exists() and not backup.exists():
            os.replace(retirement, backup)
        raise
    cleanup.mkdir(mode=0o700)
    try:
        _publish_swap_intent(
            cleanup / _CLEANUP_MARKER,
            _cleanup_content(operation, backup, token, digest),
            token,
        )
        os.replace(retirement, cleanup / "generation")
    except BaseException:
        generation = cleanup / "generation"
        if generation.exists() and not retirement.exists():
            _assert_owned_swap_backup(
                generation,
                operation=operation,
                token=token,
                digest=digest,
                backup_identity=backup,
            )
        elif not any(cleanup.iterdir()):
            cleanup.rmdir()
        raise
    _finish_owned_cleanup(
        cleanup,
        backup,
        intent,
        operation=operation,
        token=token,
        digest=digest,
    )


def _recover_swap_intents(target: Path, backup_prefix: str) -> None:
    """Recover every externally-marked rename window before touching live state."""
    if target.is_symlink():
        raise OSError(f"Refusing Hermes recovery through symlinked directory target: {target}")
    records: list[tuple[Path, Path, str, str, str]] = []
    for intent in target.parent.glob(f"{backup_prefix}*{_SWAP_INTENT_SUFFIX}"):
        backup = intent.with_name(intent.name.removesuffix(_SWAP_INTENT_SUFFIX))
        suffix = backup.name.removeprefix(backup_prefix)
        if intent.is_symlink() or not intent.is_file() or not re.fullmatch(r"[0-9a-f]{32}", suffix):
            raise OSError(f"Refusing malformed Hermes swap intent: {intent}")
        content = _read_swap_record(intent)
        match = re.fullmatch(
            rf"ouroboros-hermes-(swap|remove)-v2:{re.escape(backup.name)}:"
            r"([0-9a-f]{32}):([0-9a-f]{64})\n",
            content,
        )
        if match is None or backup.is_symlink() or backup.exists() and not backup.is_dir():
            raise OSError(f"Refusing malformed Hermes swap intent: {intent}")
        operation, token, digest = match.groups()
        if backup.exists():
            _assert_owned_swap_backup(
                backup,
                operation=operation,
                token=token,
                digest=digest,
            )
        records.append((intent, backup, operation, token, digest))
    if len(records) > 1:
        raise OSError("Refusing ambiguous Hermes recovery with multiple swap intents")
    for intent, backup, operation, token, digest in records:
        retirement = _retirement_path(backup, token)
        cleanup = _cleanup_path(backup, token)
        if cleanup.exists() or cleanup.is_symlink():
            if backup.exists() or retirement.exists() or retirement.is_symlink():
                raise OSError("Refusing ambiguous Hermes cleanup generations")
            _finish_owned_cleanup(
                cleanup,
                backup,
                intent,
                operation=operation,
                token=token,
                digest=digest,
            )
            continue
        if retirement.exists() or retirement.is_symlink():
            if backup.exists():
                raise OSError("Refusing ambiguous Hermes backup and retirement generations")
            _finish_owned_retirement(
                retirement,
                backup,
                intent,
                operation=operation,
                token=token,
                digest=digest,
            )
            continue
        if operation == "swap" and not target.exists() and backup.exists():
            _restore_owned_backup(backup, target)
        elif operation == "remove" and not target.exists() and backup.exists():
            _retire_owned_backup(backup, intent, operation=operation, token=token, digest=digest)
        elif target.exists() and backup.exists():
            if operation == "remove":
                raise OSError("Refusing concurrent Hermes mutation during removal recovery")
            _retire_owned_backup(backup, intent, operation=operation, token=token, digest=digest)
        if not intent.exists():
            continue
        if not backup.exists():
            marker = target / _SWAP_MARKER
            staging = _marker_staging_path(target, token)
            if marker.is_file() and not marker.is_symlink():
                if _read_swap_record(marker) == _ownership_content(
                    operation, backup, token, digest
                ):
                    _remove_target_path(marker)
                else:
                    raise OSError(f"Refusing foreign Hermes swap marker: {marker}")
            elif target.exists() and digest != _fingerprint_digest(None):
                if _fingerprint_digest(_tree_fingerprint(target)) != digest:
                    raise OSError(f"Hermes live generation changed during recovery: {target}")
            _remove_target_path(staging)
            _remove_target_path(intent)


def atomic_swap_generation(
    target: Path,
    replacement: Path,
    *,
    expected: tuple[object, ...] | None | object = _EXPECTED_UNSET,
    expected_check: Callable[[], bool] | None = None,
) -> None:
    """Publish a complete sibling generation with restart-recognizable recovery."""
    backup_prefix = f".{target.name}.old."
    _recover_swap_intents(target, backup_prefix)
    backup = target.with_name(f".{target.name}.old.{uuid4().hex}")
    intent = _swap_intent_path(backup)
    token = uuid4().hex
    checked_fingerprint = _tree_fingerprint(target)
    digest = _fingerprint_digest(checked_fingerprint)
    _publish_swap_intent(intent, _intent_content("swap", backup, token, digest), token)
    if target.exists():
        if target.joinpath(_SWAP_MARKER).exists() or target.joinpath(_SWAP_MARKER).is_symlink():
            _remove_target_path(intent)
            raise OSError(f"Refusing unowned Hermes swap marker: {target / _SWAP_MARKER}")
        if expected_check is not None and not expected_check():
            _remove_target_path(intent)
            raise OSError(f"Hermes live generation changed during publication: {target}")
        if expected is not _EXPECTED_UNSET and _tree_fingerprint(target) != expected:
            _remove_target_path(intent)
            raise OSError(f"Hermes live generation changed during publication: {target}")
        try:
            _publish_ownership_marker(
                target,
                backup,
                operation="swap",
                token=token,
                digest=digest,
            )
        except BaseException:
            if not _ownership_marker_matches(
                target,
                backup,
                operation="swap",
                token=token,
                digest=digest,
            ):
                _remove_target_path(intent)
            raise
        try:
            os.replace(target, backup)
        except BaseException:
            if not backup.exists():
                _remove_target_path(target / _SWAP_MARKER)
                _remove_target_path(intent)
            raise
        if _owned_backup_fingerprint(backup) != checked_fingerprint:
            _restore_owned_backup(backup, target)
            _remove_target_path(intent)
            raise OSError(f"Hermes live generation changed during publication: {target}")
    try:
        os.replace(replacement, target)
    except BaseException:
        if backup.exists() and not target.exists():
            _restore_owned_backup(backup, target)
            _remove_target_path(intent)
        elif not (backup.exists() and target.exists() and not replacement.exists()):
            _remove_target_path(intent)
        raise
    if backup.exists():
        _retire_owned_backup(backup, intent, operation="swap", token=token, digest=digest)
    else:
        _remove_target_path(intent)


def atomic_remove_generation(
    target: Path, *, expected_check: Callable[[], bool] | None = None
) -> None:
    """Remove a generation through a restart-recognizable backup rename."""
    backup_prefix = f".{target.name}.old."
    _recover_swap_intents(target, backup_prefix)
    backup = target.with_name(f".{target.name}.old.{uuid4().hex}")
    intent = _swap_intent_path(backup)
    token = uuid4().hex
    checked_fingerprint = _tree_fingerprint(target)
    digest = _fingerprint_digest(checked_fingerprint)
    _publish_swap_intent(intent, _intent_content("remove", backup, token, digest), token)
    if target.joinpath(_SWAP_MARKER).exists() or target.joinpath(_SWAP_MARKER).is_symlink():
        _remove_target_path(intent)
        raise OSError(f"Refusing unowned Hermes swap marker: {target / _SWAP_MARKER}")
    if expected_check is not None and not expected_check():
        _remove_target_path(intent)
        raise OSError(f"Hermes live generation changed during removal: {target}")
    try:
        _publish_ownership_marker(
            target,
            backup,
            operation="remove",
            token=token,
            digest=digest,
        )
    except BaseException:
        if not _ownership_marker_matches(
            target,
            backup,
            operation="remove",
            token=token,
            digest=digest,
        ):
            _remove_target_path(intent)
        raise
    try:
        os.replace(target, backup)
    except BaseException:
        if not backup.exists():
            _remove_target_path(target / _SWAP_MARKER)
            _remove_target_path(intent)
        raise
    if _owned_backup_fingerprint(backup) != checked_fingerprint:
        _restore_owned_backup(backup, target)
        _remove_target_path(intent)
        raise OSError(f"Hermes live generation changed during removal: {target}")
    _retire_owned_backup(backup, intent, operation="remove", token=token, digest=digest)


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
    _recover_swap_intents(target_dir, backup_prefix)
    if target_dir.is_symlink():
        msg = f"Refusing to install Hermes skills into symlinked directory: {target_dir}"
        raise OSError(msg)
    if target_dir.exists() and not target_dir.is_dir():
        # This path is operator-owned unless a managed generation was
        # published there.  Never delete an unowned file/socket/device just
        # to make the destination usable; refuse before staging or swapping.
        msg = f"Refusing to replace non-directory Hermes skill target: {target_dir}"
        raise OSError(msg)
    live_marker = target_dir / _SWAP_MARKER
    if target_dir.exists() and (live_marker.exists() or live_marker.is_symlink()):
        msg = f"Refusing to overwrite reserved Hermes swap marker: {live_marker}"
        raise OSError(msg)
    with _packaged_skills_dir() as source_root:
        source_skill_dirs = collect_skill_bundle_dirs(source_root)
        desired_skill_names = {skill_dir.name for skill_dir in source_skill_dirs}
        source_generation = _tree_fingerprint(target_dir)

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

            atomic_swap_generation(target_dir, staging_dir, expected=source_generation)
            cleanup_staging_dir = None
        finally:
            if cleanup_staging_dir is not None:
                _remove_target_path(cleanup_staging_dir)

    return target_dir
