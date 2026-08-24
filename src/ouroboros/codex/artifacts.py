"""Helpers for resolving and installing packaged Codex-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import importlib.resources
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Literal

from ouroboros.backends.capabilities import render_backend_skill_capability_guide
from ouroboros.codex.home import resolve_codex_home
from ouroboros.skills.artifacts import (
    SKILL_ENTRYPOINT,
    collect_skill_bundle_dirs,
    resolve_packaged_skills_dir,
)

CODEX_RULE_FILENAME = "ouroboros.md"
CODEX_SKILL_NAMESPACE = "ouroboros-"
_SKILL_CAPABILITY_GUIDE_MARKER = "<!-- ouroboros:skill-capability-guide -->"
_RULE_NAMESPACE = Path(CODEX_RULE_FILENAME).stem
_RULE_SUFFIX = Path(CODEX_RULE_FILENAME).suffix
# Errnos that mean "this kernel or filesystem does not implement the no-replace
# rename flag" rather than "the rename failed". ``ENOSYS`` is a kernel older than
# renameat2 (< 3.15); ``EINVAL`` and ``EOPNOTSUPP`` are filesystems whose rename
# implementation rejects the flag — NFS does this, as do some FUSE and overlay
# mounts. Nothing else may be swallowed: ``EEXIST`` in particular is the
# no-replace guarantee doing its job and must reach the caller.
_RENAME_FLAG_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP})


def _render_codex_rules(source: str) -> str:
    """Return Codex rules with the generated skill capability guide appended."""
    base = source.split(_SKILL_CAPABILITY_GUIDE_MARKER, 1)[0].rstrip()
    guide = render_backend_skill_capability_guide("codex").rstrip()
    return f"{base}\n\n{_SKILL_CAPABILITY_GUIDE_MARKER}\n{guide}\n"


@dataclass(frozen=True, slots=True)
class CodexPackagedSkill:
    """A packaged self-contained Ouroboros skill ready for Codex install."""

    skill_name: str
    source_dir: Path
    install_dir_name: str

    @property
    def skill_md_path(self) -> Path:
        """Return the packaged `SKILL.md` entrypoint for this skill."""
        return self.source_dir / SKILL_ENTRYPOINT


@dataclass(frozen=True, slots=True)
class CodexManagedArtifact:
    """A packaged Codex artifact managed by Ouroboros setup/update flows."""

    artifact_type: Literal["rule", "skill"]
    source_path: Path
    relative_install_path: Path


@dataclass(frozen=True, slots=True)
class CodexArtifactInstallResult:
    """Installed Codex artifact paths produced by an artifact refresh."""

    rules_path: Path
    skill_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CodexArtifactGeneration:
    """Exact target generation authored by one artifact mutation."""

    target_path: Path
    source_path: Path | None = None
    contents: bytes | None = None
    missing: bool = False


CodexArtifactGenerationCallback = Callable[[CodexArtifactGeneration], None]
CodexArtifactPreMutationCallback = Callable[[Path], None]


@dataclass(frozen=True, slots=True)
class CodexPackagedAssets:
    """Resolved packaged skills and the matching Codex rule assets."""

    skills: tuple[CodexPackagedSkill, ...]
    rules: tuple[Path, ...]

    @property
    def rules_path(self) -> Path:
        """Return the primary packaged rules file for legacy single-file consumers."""
        return _select_primary_packaged_codex_rule(self.rules)

    @property
    def managed_artifacts(self) -> tuple[CodexManagedArtifact, ...]:
        """Return the desired managed Codex artifacts derived from the packaged bundle."""
        return (
            *(
                CodexManagedArtifact(
                    artifact_type="rule",
                    source_path=rule_path,
                    relative_install_path=Path("rules") / rule_path.name,
                )
                for rule_path in self.rules
            ),
            *(
                CodexManagedArtifact(
                    artifact_type="skill",
                    source_path=skill.source_dir,
                    relative_install_path=Path("skills") / skill.install_dir_name,
                )
                for skill in self.skills
            ),
        )

    @property
    def managed_relative_install_paths(self) -> tuple[Path, ...]:
        """Return deterministic relative install paths for every managed Codex artifact."""
        return tuple(artifact.relative_install_path for artifact in self.managed_artifacts)


def _collect_packaged_codex_skills(source_root: Path) -> tuple[CodexPackagedSkill, ...]:
    """Enumerate packaged skill directories in a deterministic order."""
    skill_dirs = collect_skill_bundle_dirs(source_root)
    return tuple(
        CodexPackagedSkill(
            skill_name=source_dir.name,
            source_dir=source_dir,
            install_dir_name=f"{CODEX_SKILL_NAMESPACE}{source_dir.name}",
        )
        for source_dir in skill_dirs
    )


def _is_packaged_codex_rule_asset(path: Path) -> bool:
    """Return whether ``path`` is a packaged Ouroboros Codex rule asset."""
    return path.is_file() and _is_namespaced_rule_artifact(path)


def _collect_packaged_codex_rules(source_root: Path) -> tuple[Path, ...]:
    """Enumerate packaged rule assets in a deterministic order."""
    if not source_root.is_dir():
        return ()

    return tuple(
        sorted(
            (
                source_path
                for source_path in source_root.iterdir()
                if _is_packaged_codex_rule_asset(source_path)
            ),
            key=lambda source_path: (source_path.name != CODEX_RULE_FILENAME, source_path.name),
        )
    )


def _select_primary_packaged_codex_rule(rule_paths: tuple[Path, ...]) -> Path:
    """Return the primary packaged rule used by single-file consumers."""
    for rule_path in rule_paths:
        if rule_path.name == CODEX_RULE_FILENAME:
            return rule_path

    if not rule_paths:
        msg = "Packaged Ouroboros rules file could not be located"
        raise FileNotFoundError(msg)

    return rule_paths[0]


def _load_packaged_codex_skills(source_root: Path) -> tuple[CodexPackagedSkill, ...]:
    """Resolve packaged skills and fail fast when the bundle is empty."""
    skills = _collect_packaged_codex_skills(source_root)
    if skills:
        return skills

    msg = f"Packaged Ouroboros skills directory did not contain any `{SKILL_ENTRYPOINT}` files"
    raise FileNotFoundError(msg)


@contextmanager
def _packaged_codex_skills_dir(*, skills_dir: str | Path | None = None) -> Iterator[Path]:
    """Resolve the packaged skills source directory for Codex skill installs."""
    with resolve_packaged_skills_dir(
        skills_dir=skills_dir,
        anchor_file=__file__,
    ) as resolved_dir:
        yield resolved_dir


@contextmanager
def resolve_packaged_codex_skill_path(
    skill_name: str,
    *,
    skills_dir: str | Path | None = None,
) -> Iterator[Path]:
    """Resolve the packaged ``SKILL.md`` entrypoint for one Codex skill."""
    normalized_skill_name = skill_name.strip()
    if not normalized_skill_name:
        msg = "skill_name must be a non-empty string"
        raise ValueError(msg)

    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        skill_md_path = source_root / normalized_skill_name / SKILL_ENTRYPOINT
        if not skill_md_path.is_file():
            msg = f"Packaged Ouroboros skill could not be located: {normalized_skill_name}"
            raise FileNotFoundError(msg)
        yield skill_md_path


@contextmanager
def _packaged_codex_rules(
    *,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
) -> Iterator[tuple[Path, ...]]:
    """Resolve packaged Codex rule assets."""
    if rules_path is not None and rules_dir is not None:
        msg = "Pass only one of `rules_path` or `rules_dir` when resolving Codex rules"
        raise ValueError(msg)

    if rules_path is not None:
        resolved_path = Path(rules_path).expanduser()
        if not resolved_path.is_file():
            msg = f"Packaged Ouroboros rules file could not be located: {resolved_path}"
            raise FileNotFoundError(msg)
        yield (resolved_path,)
        return

    if rules_dir is not None:
        resolved_dir = Path(rules_dir).expanduser()
        packaged_rules = _collect_packaged_codex_rules(resolved_dir)
        if packaged_rules:
            yield packaged_rules
            return

        msg = f"Packaged Ouroboros rules directory did not contain any managed rule assets: {resolved_dir}"
        raise FileNotFoundError(msg)

    package_root = importlib.resources.files("ouroboros.codex")
    with importlib.resources.as_file(package_root) as resolved_root:
        packaged_rules = _collect_packaged_codex_rules(resolved_root / "rules")
        if packaged_rules:
            yield packaged_rules
            return

        packaged_rules = _collect_packaged_codex_rules(resolved_root)
        if packaged_rules:
            yield packaged_rules
            return

    for parent in Path(__file__).resolve().parents:
        packaged_rules = _collect_packaged_codex_rules(parent / "rules")
        if packaged_rules:
            yield packaged_rules
            return

        packaged_rules = _collect_packaged_codex_rules(parent)
        if packaged_rules:
            yield packaged_rules
            return

    msg = "Packaged Ouroboros rules file could not be located"
    raise FileNotFoundError(msg)


@contextmanager
def _packaged_codex_rules_path(*, rules_path: str | Path | None = None) -> Iterator[Path]:
    """Resolve the primary packaged Codex rules markdown path."""
    with _packaged_codex_rules(rules_path=rules_path) as packaged_rules:
        yield _select_primary_packaged_codex_rule(packaged_rules)


@contextmanager
def resolve_packaged_codex_assets(
    *,
    skills_dir: str | Path | None = None,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
) -> Iterator[CodexPackagedAssets]:
    """Resolve packaged Codex skills and the matching rules for setup/update."""
    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        skills = _load_packaged_codex_skills(source_root)
        with _packaged_codex_rules(
            rules_path=rules_path,
            rules_dir=rules_dir,
        ) as packaged_rules:
            yield CodexPackagedAssets(
                skills=skills,
                rules=packaged_rules,
            )


def load_packaged_codex_rules() -> str:
    """Load the packaged Codex rules markdown."""
    with _packaged_codex_rules_path() as resolved_rules_path:
        return _render_codex_rules(resolved_rules_path.read_text(encoding="utf-8"))


def load_packaged_codex_skill(
    skill_name: str,
    *,
    skills_dir: str | Path | None = None,
) -> str:
    """Load the packaged ``SKILL.md`` markdown for one Codex skill."""
    with resolve_packaged_codex_skill_path(
        skill_name,
        skills_dir=skills_dir,
    ) as resolved_skill_path:
        return resolved_skill_path.read_text(encoding="utf-8")


def _remove_installed_artifact(path: Path) -> None:
    """Delete an installed Codex artifact regardless of whether it is a file or directory."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return

    path.unlink()


def _prepare_managed_install_root(path: Path) -> None:
    """Create an artifact root without following attacker-controlled symlinks."""
    _refuse_symlinked_path_component(path)
    path.mkdir(parents=True, exist_ok=True)
    _refuse_symlinked_path_component(path)


def _codex_home_candidate(codex_dir: str | Path | None) -> Path:
    """Return the user/config supplied Codex home path before symlink resolution."""
    if codex_dir is not None:
        return Path(codex_dir).expanduser()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _refuse_symlinked_path_component(path: Path) -> None:
    """Fail closed when any existing component in an install root is a symlink."""
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
            "Refusing to install Codex artifacts into a path with a symlinked "
            f"directory component: {component}"
        )
        raise OSError(msg)


def _installed_artifact_exists(path: Path) -> bool:
    """Return whether an installed artifact path occupies the leaf, including symlinks."""
    return path.exists() or path.is_symlink()


def _raise_rename_error(source_path: Path, target_path: Path) -> None:
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), str(target_path))
    raise OSError(
        error_number,
        os.strerror(error_number),
        f"{source_path} -> {target_path}",
    )


def _release_directory_reservation(reservation_fd: int, target_path: Path) -> None:
    """Remove a failed publication's reservation, and nothing else.

    A writer may remove or rename the reservation and put their own directory at
    the same path before publication fails. Descriptor liveness alone cannot
    prove pathname ownership: a renamed reservation remains linked. Cleanup
    therefore requires both a live descriptor and a current target pathname
    with the same device/inode identity. A removed reservation reports zero
    links locally (or ``ESTALE`` on NFS), while a renamed reservation and its
    replacement have different identities because the original inode is still
    live. Anything that leaves ownership unproven is treated as not ours and
    left alone.
    """
    try:
        reservation_stat = os.fstat(reservation_fd)
        target_stat = os.stat(target_path, follow_symlinks=False)
    except OSError:
        return
    if reservation_stat.st_nlink <= 0:
        return
    if (reservation_stat.st_dev, reservation_stat.st_ino) != (
        target_stat.st_dev,
        target_stat.st_ino,
    ):
        return
    try:
        os.rmdir(target_path)
    except OSError:
        pass


def _rename_noreplace_fallback(source_path: Path, target_path: Path) -> None:
    """Rename without replacing where no atomic no-replace primitive is available.

    Callers treat a ``FileExistsError`` as "another writer owns this name" and
    anything else as a failed commit, so publication must never consume a name it
    did not reserve.

    Files publish with ``os.link``, which fails with ``EEXIST`` on an occupied
    target: fully atomic, no weaker than ``RENAME_NOREPLACE``.

    Directories cannot be hard-linked. ``os.mkdir`` reserves the name atomically —
    it fails with ``EEXIST`` rather than replacing — and POSIX ``rename`` then
    consumes that reservation. Failure cleanup removes the pathname only while
    the live reservation descriptor and the current target still identify the
    same directory, so a writer who removed or renamed the reservation and took
    the name over is left alone.

    One gap on the success path is not closable here: a writer who removes the
    reservation and recreates an empty directory at the same path before the
    rename has it replaced. ``mkdir`` returns no handle, so the reservation
    cannot be pinned at creation, and comparing inodes does not help because a
    recreated directory routinely reuses the same inode number. The exposure is
    bounded to an empty directory: a racing writer with any content in it makes
    the rename fail with ``ENOTEMPTY`` and keeps their tree. Closing it fully
    needs ``RENAME_NOREPLACE``, which by definition this branch does not have.
    """
    if source_path.is_dir() and not source_path.is_symlink():
        os.mkdir(target_path)
        reservation = os.open(target_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            # POSIX rename removes an empty destination directory, so this only
            # consumes the reservation made above. A writer that filled it in
            # the meantime makes the rename fail instead of losing their tree.
            os.rename(source_path, target_path)
        except BaseException:
            _release_directory_reservation(reservation, target_path)
            raise
        finally:
            os.close(reservation)
        return

    os.link(source_path, target_path, follow_symlinks=False)
    # The link published the target, so this is the commit boundary. Dropping the
    # staging name is cleanup: reporting its failure would tell the caller the
    # commit failed, and the rollback would then delete the generation that is
    # already live and strand the previous one in its backup sibling.
    try:
        source_path.unlink()
    except OSError:
        pass


def _rename_noreplace(source_path: Path, target_path: Path) -> None:
    """Atomically rename without replacing a target created by another writer."""
    if os.name == "nt":
        os.rename(source_path, target_path)
        return

    source_bytes = os.fsencode(source_path)
    target_bytes = os.fsencode(target_path)
    libc = ctypes.CDLL(None, use_errno=True)

    if sys.platform == "darwin":
        rename_exclusive = libc.renamex_np
        rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename_exclusive.restype = ctypes.c_int
        if rename_exclusive(source_bytes, target_bytes, 0x00000004) != 0:
            _raise_rename_error(source_path, target_path)
        return

    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename_exclusive = libc.renameat2
        rename_exclusive.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        if rename_exclusive(-100, source_bytes, -100, target_bytes, 0x00000001) == 0:
            return
        if ctypes.get_errno() not in _RENAME_FLAG_UNSUPPORTED_ERRNOS:
            _raise_rename_error(source_path, target_path)
        # The kernel or the filesystem rejected RENAME_NOREPLACE itself, so no
        # rename was attempted. Degrade instead of failing the install.

    _rename_noreplace_fallback(source_path, target_path)


def _artifact_fingerprint(path: Path) -> str:
    """Return a content, topology, and mode fingerprint without following symlinks."""
    digest = hashlib.sha256()

    def _visit(candidate: Path, relative_path: str) -> None:
        candidate_stat = candidate.lstat()
        mode = candidate_stat.st_mode
        digest.update(relative_path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(mode)).encode("ascii"))
        digest.update(b"\0")

        if stat.S_ISLNK(mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
            return
        if stat.S_ISREG(mode):
            digest.update(b"file\0")
            with candidate.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            return
        if stat.S_ISDIR(mode):
            digest.update(b"directory\0")
            for child in sorted(candidate.iterdir(), key=lambda item: item.name):
                child_relative = f"{relative_path}/{child.name}" if relative_path else child.name
                _visit(child, child_relative)
            return

        digest.update(f"other:{stat.S_IFMT(mode)}".encode("ascii"))

    _visit(path, "")
    return digest.hexdigest()


def _atomic_install_file(
    target_path: Path,
    *,
    source_path: Path,
    contents: bytes | None = None,
    before_mutation: CodexArtifactPreMutationCallback | None = None,
    on_generation: CodexArtifactGenerationCallback | None = None,
    transaction: _CodexArtifactBatchTransaction | None = None,
) -> None:
    """Atomically replace one managed file with complete packaged bytes."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        if contents is None:
            shutil.copy2(source_path, temp_path)
        else:
            temp_path.write_bytes(contents)
            temp_path.chmod(source_path.stat().st_mode & 0o7777)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    _commit_staged_artifact(
        staging_path=temp_path,
        target_path=target_path,
        generation=CodexArtifactGeneration(
            target_path,
            source_path=source_path,
            contents=contents,
        ),
        before_mutation=before_mutation,
        on_generation=on_generation,
        transaction=transaction,
    )


def _stage_install_directory(source_path: Path, target_path: Path) -> Path:
    """Copy one skill into a setup-owned sibling before touching its target."""
    staging_path = Path(
        tempfile.mkdtemp(
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=str(target_path.parent),
        )
    )
    try:
        shutil.copytree(source_path, staging_path, dirs_exist_ok=True)
    except BaseException:
        _remove_installed_artifact(staging_path)
        raise
    return staging_path


def _vacant_sibling_path(target_path: Path, *, suffix: str) -> Path:
    """Reserve a collision-resistant sibling name, then leave it vacant for rename."""
    reserved_path = Path(
        tempfile.mkdtemp(
            prefix=f".{target_path.name}.",
            suffix=suffix,
            dir=str(target_path.parent),
        )
    )
    reserved_path.rmdir()
    return reserved_path


def _record_current_generation(
    target_path: Path,
    on_generation: CodexArtifactGenerationCallback | None,
) -> None:
    """Record a restored generation from the target's current topology."""
    if on_generation is not None:
        on_generation(CodexArtifactGeneration(target_path, source_path=target_path))


def _dispose_committed_backup(backup_path: Path) -> None:
    """Detach a superseded generation before best-effort recursive cleanup.

    Renaming the backup is the commit boundary: failures before that point leave
    the complete prior generation available for rollback. Once detached, cleanup
    cannot affect either the active target or the rollback source, so a partial
    filesystem deletion is intentionally treated as post-commit housekeeping.
    """
    disposal_path = _vacant_sibling_path(backup_path, suffix=".discard")
    os.replace(backup_path, disposal_path)
    try:
        _remove_installed_artifact(disposal_path)
    except BaseException:
        # The active artifact transaction is already committed. A partially
        # removed setup-owned sibling is safer than rolling back from damaged
        # data or reporting failure after the managed target has changed.
        pass


@dataclass(slots=True)
class _CodexArtifactBatchEntry:
    """One committed artifact mutation retained for batch rollback."""

    target_path: Path
    rollback_path: Path | None
    target_is_active: bool
    active_fingerprint: str | None
    before_mutation: CodexArtifactPreMutationCallback | None
    on_generation: CodexArtifactGenerationCallback | None


class _CodexArtifactBatchTransaction:
    """Keep prior artifact generations until an entire rules/skills batch commits."""

    def __init__(self) -> None:
        self._entries: list[_CodexArtifactBatchEntry] = []

    def record_install(
        self,
        *,
        target_path: Path,
        rollback_path: Path | None,
        active_fingerprint: str,
        before_mutation: CodexArtifactPreMutationCallback | None,
        on_generation: CodexArtifactGenerationCallback | None,
    ) -> None:
        self._entries.append(
            _CodexArtifactBatchEntry(
                target_path=target_path,
                rollback_path=rollback_path,
                target_is_active=True,
                active_fingerprint=active_fingerprint,
                before_mutation=before_mutation,
                on_generation=on_generation,
            )
        )

    def record_removal(
        self,
        *,
        target_path: Path,
        rollback_path: Path,
        before_mutation: CodexArtifactPreMutationCallback | None,
        on_generation: CodexArtifactGenerationCallback | None,
    ) -> None:
        self._entries.append(
            _CodexArtifactBatchEntry(
                target_path=target_path,
                rollback_path=rollback_path,
                target_is_active=False,
                active_fingerprint=None,
                before_mutation=before_mutation,
                on_generation=on_generation,
            )
        )

    def commit(self) -> None:
        """Atomically detach every rollback generation, then clean it best-effort."""
        for entry in self._entries:
            if entry.rollback_path is None:
                continue
            disposal_path = _vacant_sibling_path(entry.target_path, suffix=".discard")
            os.replace(entry.rollback_path, disposal_path)
            entry.rollback_path = disposal_path

        cleanup_paths = tuple(
            entry.rollback_path for entry in self._entries if entry.rollback_path is not None
        )
        self._entries.clear()
        for cleanup_path in cleanup_paths:
            try:
                _remove_installed_artifact(cleanup_path)
            except BaseException:
                pass

    def rollback(self) -> None:
        """Restore every pre-batch generation in reverse mutation order."""
        rollback_error: BaseException | None = None
        for entry in reversed(self._entries):
            try:
                self._rollback_entry(entry)
            except BaseException as exc:
                if rollback_error is None:
                    rollback_error = exc
        self._entries.clear()
        if rollback_error is not None:
            raise rollback_error

    @staticmethod
    def _rollback_entry(entry: _CodexArtifactBatchEntry) -> None:
        if entry.before_mutation is not None:
            entry.before_mutation(entry.target_path)

        if not entry.target_is_active:
            if _installed_artifact_exists(entry.target_path):
                msg = f"Managed Codex artifact changed during batch rollback: {entry.target_path}"
                raise OSError(msg)
            if entry.rollback_path is None:
                msg = f"Managed Codex artifact rollback is missing its prior generation: {entry.target_path}"
                raise OSError(msg)
            try:
                _rename_noreplace(entry.rollback_path, entry.target_path)
            except FileExistsError as restore_conflict:
                msg = f"Managed Codex artifact changed during batch rollback: {entry.target_path}"
                raise OSError(msg) from restore_conflict
            _record_current_generation(entry.target_path, entry.on_generation)
            return

        if not _installed_artifact_exists(entry.target_path):
            msg = f"Managed Codex artifact changed during batch rollback: {entry.target_path}"
            raise OSError(msg)

        displaced_path = _vacant_sibling_path(entry.target_path, suffix=".rollback")
        os.replace(entry.target_path, displaced_path)
        try:
            displaced_fingerprint = _artifact_fingerprint(displaced_path)
        except OSError as fingerprint_error:
            _restore_displaced_concurrent_generation(entry.target_path, displaced_path)
            msg = f"Could not verify managed Codex artifact during batch rollback: {entry.target_path}"
            raise OSError(msg) from fingerprint_error
        if displaced_fingerprint != entry.active_fingerprint:
            _restore_displaced_concurrent_generation(entry.target_path, displaced_path)
            msg = f"Managed Codex artifact changed during batch rollback: {entry.target_path}"
            raise OSError(msg)

        try:
            if entry.rollback_path is None:
                if entry.on_generation is not None:
                    entry.on_generation(CodexArtifactGeneration(entry.target_path, missing=True))
            else:
                _rename_noreplace(entry.rollback_path, entry.target_path)
                _record_current_generation(entry.target_path, entry.on_generation)
        except FileExistsError as restore_conflict:
            if _installed_artifact_exists(displaced_path):
                try:
                    _remove_installed_artifact(displaced_path)
                except BaseException:
                    pass
            msg = f"Managed Codex artifact changed during batch rollback: {entry.target_path}"
            raise OSError(msg) from restore_conflict
        except BaseException:
            if _installed_artifact_exists(displaced_path):
                _restore_displaced_concurrent_generation(entry.target_path, displaced_path)
                _record_current_generation(entry.target_path, entry.on_generation)
            raise

        if _installed_artifact_exists(displaced_path):
            try:
                _remove_installed_artifact(displaced_path)
            except BaseException:
                pass


def _restore_displaced_concurrent_generation(target_path: Path, displaced_path: Path) -> None:
    """Put a concurrently changed target back without overwriting another writer."""
    try:
        _rename_noreplace(displaced_path, target_path)
    except FileExistsError as restore_conflict:
        msg = (
            "Managed Codex artifact changed again during batch rollback; "
            f"preserved the displaced generation at {displaced_path}"
        )
        raise OSError(msg) from restore_conflict


def _acquire_rollback_generation(
    target_path: Path,
    *,
    before_mutation: CodexArtifactPreMutationCallback | None,
    operation: Literal["activation", "removal"],
) -> Path:
    """Move the exact validated target generation into a rollback sibling."""
    rollback_fingerprint = _artifact_fingerprint(target_path)
    if before_mutation is not None:
        before_mutation(target_path)
    backup_path = _vacant_sibling_path(target_path, suffix=".backup")
    os.replace(target_path, backup_path)
    try:
        acquired_fingerprint = _artifact_fingerprint(backup_path)
    except OSError as fingerprint_error:
        try:
            _rename_noreplace(backup_path, target_path)
        except FileExistsError:
            pass
        msg = f"Could not verify managed Codex artifact before {operation}: {target_path}"
        raise OSError(msg) from fingerprint_error
    if acquired_fingerprint != rollback_fingerprint:
        try:
            _rename_noreplace(backup_path, target_path)
        except FileExistsError:
            pass
        msg = f"Managed Codex artifact changed before {operation}: {target_path}"
        raise OSError(msg)
    return backup_path


def _commit_staged_artifact(
    *,
    staging_path: Path,
    target_path: Path,
    generation: CodexArtifactGeneration,
    before_mutation: CodexArtifactPreMutationCallback | None,
    on_generation: CodexArtifactGenerationCallback | None,
    transaction: _CodexArtifactBatchTransaction | None = None,
) -> None:
    """Commit one staged artifact while retaining the previous generation until success."""
    active_fingerprint = _artifact_fingerprint(staging_path)
    backup_path: Path | None = None
    prepared_generation = False
    staged_generation_active = False
    try:
        if _installed_artifact_exists(target_path):
            backup_path = _acquire_rollback_generation(
                target_path,
                before_mutation=before_mutation,
                operation="activation",
            )
            if on_generation is not None:
                on_generation(CodexArtifactGeneration(target_path, missing=True))

        if before_mutation is not None:
            before_mutation(target_path)
        if on_generation is not None:
            on_generation(generation)
            prepared_generation = True
        _rename_noreplace(staging_path, target_path)
        staged_generation_active = True
        if _installed_artifact_exists(staging_path):
            # A fallback publication that could not drop its staging name leaves
            # a hard link aliasing the generation now live at the target. The
            # commit already succeeded, so this is only tidy-up — but the alias
            # must not survive, or refreshes accumulate pinned old inodes.
            try:
                _remove_installed_artifact(staging_path)
            except OSError:
                pass

        if transaction is not None:
            transaction.record_install(
                target_path=target_path,
                rollback_path=backup_path,
                active_fingerprint=active_fingerprint,
                before_mutation=before_mutation,
                on_generation=on_generation,
            )
            backup_path = None
        elif backup_path is not None and _installed_artifact_exists(backup_path):
            _dispose_committed_backup(backup_path)
            backup_path = None
    except BaseException as commit_error:
        if _installed_artifact_exists(staging_path):
            _remove_installed_artifact(staging_path)
        if backup_path is not None and _installed_artifact_exists(backup_path):
            if staged_generation_active and _installed_artifact_exists(target_path):
                if before_mutation is not None:
                    before_mutation(target_path)
                os.replace(target_path, staging_path)
                if _artifact_fingerprint(staging_path) != active_fingerprint:
                    _restore_displaced_concurrent_generation(target_path, staging_path)
                    msg = f"Managed Codex artifact changed during rollback: {target_path}"
                    raise OSError(msg)
                _rename_noreplace(backup_path, target_path)
                _record_current_generation(target_path, on_generation)
                try:
                    _remove_installed_artifact(staging_path)
                except BaseException:
                    pass
            elif _installed_artifact_exists(target_path):
                msg = f"Managed Codex artifact changed during rollback: {target_path}"
                raise OSError(msg) from commit_error
            else:
                _rename_noreplace(backup_path, target_path)
                _record_current_generation(target_path, on_generation)
        elif prepared_generation and not _installed_artifact_exists(target_path):
            if on_generation is not None:
                on_generation(CodexArtifactGeneration(target_path, missing=True))
        raise


def _remove_artifact_transactionally(
    target_path: Path,
    *,
    before_mutation: CodexArtifactPreMutationCallback | None,
    on_generation: CodexArtifactGenerationCallback | None,
    transaction: _CodexArtifactBatchTransaction | None = None,
) -> None:
    """Remove one managed artifact, retaining rollback data until commit."""
    backup_path = _acquire_rollback_generation(
        target_path,
        before_mutation=before_mutation,
        operation="removal",
    )
    try:
        if on_generation is not None:
            on_generation(CodexArtifactGeneration(target_path, missing=True))
        if transaction is not None:
            transaction.record_removal(
                target_path=target_path,
                rollback_path=backup_path,
                before_mutation=before_mutation,
                on_generation=on_generation,
            )
        else:
            _dispose_committed_backup(backup_path)
        backup_path = None
    except BaseException:
        if _installed_artifact_exists(backup_path) and not _installed_artifact_exists(target_path):
            _rename_noreplace(backup_path, target_path)
            _record_current_generation(target_path, on_generation)
        raise


def _is_namespaced_rule_artifact(path: Path) -> bool:
    """Return whether a rules entry is managed by Ouroboros."""
    if path.name == CODEX_RULE_FILENAME:
        return True

    return path.name.startswith(f"{_RULE_NAMESPACE}-") and path.name.endswith(_RULE_SUFFIX)


def install_codex_rules(
    *,
    codex_dir: str | Path | None = None,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
    prune: bool = False,
    on_generation: CodexArtifactGenerationCallback | None = None,
    before_mutation: CodexArtifactPreMutationCallback | None = None,
    _transaction: _CodexArtifactBatchTransaction | None = None,
) -> Path:
    """Install or refresh packaged Ouroboros rules into ``~/.codex/rules``."""
    _refuse_symlinked_path_component(_codex_home_candidate(codex_dir) / "rules")
    resolved_codex_dir = resolve_codex_home(codex_dir)
    target_root = resolved_codex_dir / "rules"
    _prepare_managed_install_root(target_root)

    installed_names: set[str] = set()
    primary_target_path: Path | None = None
    with _packaged_codex_rules(
        rules_path=rules_path,
        rules_dir=rules_dir,
    ) as packaged_rules:
        primary_source_path = _select_primary_packaged_codex_rule(packaged_rules)
        for source_path in packaged_rules:
            target_path = target_root / source_path.name
            if source_path == primary_source_path:
                rendered = _render_codex_rules(source_path.read_text(encoding="utf-8"))
                _atomic_install_file(
                    target_path,
                    source_path=source_path,
                    contents=rendered.encode("utf-8"),
                    before_mutation=before_mutation,
                    on_generation=on_generation,
                    transaction=_transaction,
                )
                primary_target_path = target_path
            else:
                _atomic_install_file(
                    target_path,
                    source_path=source_path,
                    before_mutation=before_mutation,
                    on_generation=on_generation,
                    transaction=_transaction,
                )
            installed_names.add(target_path.name)

    if prune:
        for installed_path in tuple(target_root.iterdir()):
            if installed_path.name in installed_names:
                continue
            if _is_namespaced_rule_artifact(installed_path):
                _remove_artifact_transactionally(
                    installed_path,
                    before_mutation=before_mutation,
                    on_generation=on_generation,
                    transaction=_transaction,
                )

    if primary_target_path is None:
        msg = "Packaged Ouroboros rules file could not be located"
        raise FileNotFoundError(msg)

    return primary_target_path


def install_codex_skills(
    *,
    codex_dir: str | Path | None = None,
    skills_dir: str | Path | None = None,
    prune: bool = False,
    on_generation: CodexArtifactGenerationCallback | None = None,
    before_mutation: CodexArtifactPreMutationCallback | None = None,
    _transaction: _CodexArtifactBatchTransaction | None = None,
) -> tuple[Path, ...]:
    """Install or refresh packaged Ouroboros skills into ``~/.codex/skills/ouroboros-*``."""
    _refuse_symlinked_path_component(_codex_home_candidate(codex_dir) / "skills")
    resolved_codex_dir = resolve_codex_home(codex_dir)
    target_root = resolved_codex_dir / "skills"
    _prepare_managed_install_root(target_root)

    installed_paths: list[Path] = []
    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        packaged_skills = _load_packaged_codex_skills(source_root)
        installed_names = {packaged_skill.install_dir_name for packaged_skill in packaged_skills}

        for packaged_skill in packaged_skills:
            target_path = target_root / packaged_skill.install_dir_name
            staging_path = _stage_install_directory(packaged_skill.source_dir, target_path)
            _commit_staged_artifact(
                staging_path=staging_path,
                target_path=target_path,
                generation=CodexArtifactGeneration(
                    target_path,
                    source_path=packaged_skill.source_dir,
                ),
                before_mutation=before_mutation,
                on_generation=on_generation,
                transaction=_transaction,
            )
            installed_paths.append(target_path)

        if prune:
            for installed_path in target_root.iterdir():
                if (
                    installed_path.name.startswith(CODEX_SKILL_NAMESPACE)
                    and installed_path.name not in installed_names
                ):
                    _remove_artifact_transactionally(
                        installed_path,
                        before_mutation=before_mutation,
                        on_generation=on_generation,
                        transaction=_transaction,
                    )

    return tuple(installed_paths)


def install_codex_artifacts(
    *,
    codex_dir: str | Path | None = None,
    prune: bool = True,
    on_generation: CodexArtifactGenerationCallback | None = None,
    before_mutation: CodexArtifactPreMutationCallback | None = None,
) -> CodexArtifactInstallResult:
    """Install or refresh all packaged Ouroboros Codex artifacts.

    This intentionally only touches managed Codex rules and skills. It does
    not read or write ``~/.codex/config.toml`` or ``~/.ouroboros/config.yaml``.
    """
    resolved_codex_dir = resolve_codex_home(codex_dir)
    install_roots = (
        resolved_codex_dir,
        resolved_codex_dir / "rules",
        resolved_codex_dir / "skills",
    )
    root_topology = {path: _installed_artifact_exists(path) for path in install_roots}
    transaction = _CodexArtifactBatchTransaction()
    try:
        rules_path = install_codex_rules(
            codex_dir=codex_dir,
            prune=prune,
            on_generation=on_generation,
            before_mutation=before_mutation,
            _transaction=transaction,
        )
        skill_paths = install_codex_skills(
            codex_dir=codex_dir,
            prune=prune,
            on_generation=on_generation,
            before_mutation=before_mutation,
            _transaction=transaction,
        )
        transaction.commit()
    except BaseException as install_error:
        try:
            transaction.rollback()
        except BaseException as rollback_error:
            raise rollback_error from install_error
        for install_root in reversed(install_roots):
            if root_topology[install_root]:
                continue
            try:
                install_root.rmdir()
            except (FileNotFoundError, NotADirectoryError, OSError):
                pass
        raise
    return CodexArtifactInstallResult(rules_path=rules_path, skill_paths=skill_paths)


__all__ = [
    "CodexArtifactInstallResult",
    "CodexArtifactGeneration",
    "CodexArtifactGenerationCallback",
    "CodexArtifactPreMutationCallback",
    "CodexManagedArtifact",
    "CodexPackagedAssets",
    "CodexPackagedSkill",
    "CODEX_RULE_FILENAME",
    "CODEX_SKILL_NAMESPACE",
    "install_codex_artifacts",
    "install_codex_rules",
    "install_codex_skills",
    "load_packaged_codex_skill",
    "load_packaged_codex_rules",
    "resolve_packaged_codex_assets",
    "resolve_packaged_codex_skill_path",
]
