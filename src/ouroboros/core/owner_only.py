"""Owner-only writes for files that hold interview and data content.

Interview transcripts, Seeds, and fan-out records all carry whatever a data
lookup returned and whatever the user confirmed about it. They are written
under the process umask by default, which on a typical `022` leaves them
world-readable for their whole lifetime — indefinitely, in the case of
interview state and Seeds.

The protection cannot be an instruction to the writer to be careful: every
call site would have to remember, and one that forgets is invisible. It is a
function, applied at every site that persists this class of content, that
creates the file with mode `0600` rather than fixing the mode afterwards — so
the content never exists at the umask default even briefly.
"""

from __future__ import annotations

from contextlib import suppress
import errno
import logging
import os
from pathlib import Path
import stat
from uuid import uuid4

from ouroboros.core.filesystem_capability import (
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)

#: Files: readable and writable by the owner only.
OWNER_ONLY_FILE = 0o600
#: Directories: additionally traversable by the owner only.
OWNER_ONLY_DIR = 0o700
#: How much of the target name a temporary may borrow. Bounds the temporary
#: at 70 characters however long the target is.
_TMP_NAME_PREFIX_CHARS = 32

_log = logging.getLogger(__name__)


def _posix() -> bool:
    """Whether the platform can represent the owner-only mode.

    A function rather than an inline ``os.name`` check so tests can probe
    the non-POSIX branch without patching the global ``os.name`` — which
    pathlib also reads, turning every ``Path()`` into a ``WindowsPath``.
    """
    return os.name == "posix"


#: Emitted once per process on a platform where the mode cannot be enforced.
_degradation_warned = False


def package_owned_directory(path: Path) -> bool:
    """Whether ``path`` lies inside the package namespace ``~/.ouroboros``.

    Ownership of a DIRECTORY is decided by provenance, not by which module
    happens to call mkdir. The interview state directory, the
    fan-out registry, and the plugin state directory all default under the
    package namespace but are caller-suppliable, and a probe showed an
    explicitly supplied 0755 directory being narrowed to 0700 — revoking
    access this package had no business revoking. Inside ``~/.ouroboros``
    the directories are this package's to repair (a 0755 one inherited
    from an older version included); outside it they are the caller's.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        namespace = (Path.home() / ".ouroboros").resolve()
    except OSError:
        return False
    return resolved == namespace or namespace in resolved.parents


def fsync_parent_directory(file_path: Path) -> bool:
    """Flush the directory entry so the rename itself survives a crash.

    Public because durability has to be RETRYABLE: a record whose
    content reached disk but whose directory entry was not confirmed is
    already readable and already terminal, so the recovery that helps is
    flushing again — not rewriting content that is not the problem.

    Returns whether durability could be confirmed. A filesystem that cannot
    fsync a directory (``EINVAL``/``ENOTSUP``) is reported as confirmed: it
    never owed the guarantee, so treating it as a failure would make every
    write on that filesystem look suspect.
    """
    if not _posix():
        return True

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(file_path.parent, flags)
    except OSError as error:
        return error.errno in (errno.EINVAL, errno.ENOTSUP)
    try:
        durability_confirmed = _fsync_directory_fd(directory_fd)
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            durability_confirmed = False
    return durability_confirmed


def _fsync_directory_fd(directory_fd: int) -> bool:
    try:
        os.fsync(directory_fd)
    except OSError as error:
        return error.errno in (errno.EINVAL, errno.ENOTSUP)
    return True


def ensure_directory_no_symlinks(path: Path, *, mode: int = OWNER_ONLY_DIR) -> Path:
    """Create a directory chain without following existing symlink components."""
    requested = Path(os.path.abspath(path.expanduser()))
    _refuse_symlinked_existing_components(requested)
    target = Path(os.path.realpath(requested))
    if _directory_dirfd_creation_available():
        _mkdir_nofollow_directory_chain(target, mode=mode)
    else:
        target.mkdir(parents=True, exist_ok=True, mode=mode)
        _refuse_symlinked_existing_components(target)
    return target


def _directory_dirfd_creation_available() -> bool:
    return (
        _posix()
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )


def _mkdir_nofollow_directory_chain(path: Path, *, mode: int) -> None:
    if path.anchor != os.sep:
        msg = "no-follow directory creation requires an absolute POSIX path"
        raise ValueError(msg)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(os.sep, flags)
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."} or Path(component).name != component:
                msg = "directory capability components must be canonical names"
                raise ValueError(msg)
            try:
                os.mkdir(component, mode=mode, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)


def _refuse_symlinked_existing_components(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        if component.is_symlink() and not _allowed_platform_symlink_component(component):
            msg = f"Refusing to create directory through symlinked component: {component}"
            raise OSError(msg)


def _allowed_platform_symlink_component(component: Path) -> bool:
    return component in {Path("/tmp"), Path("/var")} and Path(os.path.realpath(component)) in {
        Path("/private/tmp"),
        Path("/private/var"),
    }


def write_owner_only(path: Path, text: str, *, encoding: str = "utf-8") -> bool:
    """Write ``text`` to ``path`` as a durable owner-only file.

    The content is written to a NEW file created at ``0600`` and then renamed
    over the target, so the mode is established at creation and never depends
    on repairing an existing file's permissions. That matters because the
    repair can fail — a filesystem without chmod, an EPERM on a file owned by
    someone else — and the earlier version suppressed that failure and wrote
    anyway, leaving the new secret at the old ``0644``. Here a
    failure to establish the mode is a failure to write: nothing sensitive
    reaches disk at a wider mode, ever.

    The replacement is atomic, so a reader never observes a partial file, and
    the temporary lives beside the target so the rename stays within one
    filesystem. Contents and directory entry are both fsync'd, and the return
    value reports whether that durability could be confirmed — callers that
    persist state a user would notice losing should log when it could not.

    Establishing the mode at creation is also what keeps an *existing*
    world-readable file from staying that way. Preserving the previous mode,
    the usual behaviour for an atomic-write helper, would carry a `0644` file
    written by an older version forward forever.

    Directories are not touched: a caller may write into a directory that is
    not this package's to re-permission. Call :func:`secure_directory` only
    for directories this package creates and owns.
    """
    target = Path(path)
    if not _posix():
        # The owner-only guarantee is scoped to POSIX. `os.open`
        # with 0o600 on native Windows only sets the CRT read/write flags —
        # access stays governed by the inherited ACL, and `st_mode` reflects
        # the flags, so the mode check below would be vacuous there. Round 71
        # made this path refuse outright; the review pointed out that native
        # Windows is an advertised (experimental) platform and refusing every
        # Interview/Seed/PM/Auto write makes it unusable. So the write
        # proceeds atomically under the directory's inherited ACL, the
        # degradation is stated loudly once per process instead of being
        # discovered, and no surface may advertise owner-only permissions on
        # this platform. An owner-only DACL implementation can restore the
        # guarantee later; a mode check that cannot fail must not stand in
        # for it meanwhile.
        global _degradation_warned
        if not _degradation_warned:
            _degradation_warned = True
            _log.warning(
                "owner-only file permissions cannot be established on this "
                "platform; %s is written under the directory's inherited ACL",
                target,
            )
        return _write_atomic_unscoped(target, text, encoding=encoding)
    target = ensure_directory_no_symlinks(target.parent, mode=0o777) / target.name
    if nofollow_directory_capabilities_available() and os.rename in os.supports_dir_fd:
        return _write_owner_only_dirfd(target, text, encoding=encoding)
    # The temporary must not be longer than the filesystem allows just because
    # the target is near the limit. Embedding the WHOLE target name
    # added a fixed 38 characters, so a caller that had carefully bounded its
    # filename to the 255-byte limit — the Auto Seed writer does exactly that —
    # got ENAMETOOLONG from the temporary instead. Only a bounded prefix is
    # kept, for debuggability; uniqueness comes from the uuid, and the
    # `.NAME.tmp-` shape is what leftover sweeps match on.
    tmp_path = target.with_name(f".{target.name[:_TMP_NAME_PREFIX_CHARS]}.tmp-{uuid4().hex}")
    # Held only until a file object takes ownership of it. If wrapping the
    # descriptor fails, nothing else will ever close it, so the cleanup path
    # has to.
    raw_fd: int | None = None
    try:
        raw_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY_FILE)
        # Checked BEFORE a single byte is written. This check exists
        # for the filesystem that ignores or widens the requested mode, and
        # verifying it after the write meant that on exactly that filesystem
        # the content had already existed group- or world-readable — the
        # window the check was added to close. fstat on the descriptor, not
        # stat on the path, so nothing can be swapped underneath it.
        if stat.S_IMODE(os.fstat(raw_fd).st_mode) != OWNER_ONLY_FILE:
            raise OSError(f"cannot create {target} with owner-only permissions on this filesystem")
        handle = os.fdopen(raw_fd, "w", encoding=encoding)
        raw_fd = None
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        if raw_fd is not None:
            with suppress(OSError):
                os.close(raw_fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    return fsync_parent_directory(target)


def _write_owner_only_dirfd(target: Path, text: str, *, encoding: str) -> bool:
    """Write through a held parent directory capability."""
    if target.name in {"", ".", ".."} or Path(target.name).name != target.name:
        msg = "owner-only target must be a canonical file name"
        raise ValueError(msg)
    parent = Path(os.path.abspath(target.parent))
    directory_chain = open_nofollow_directory_chain(parent)
    raw_fd: int | None = None
    tmp_name = f".{target.name[:_TMP_NAME_PREFIX_CHARS]}.tmp-{uuid4().hex}"
    try:
        parent_fd = directory_chain.leaf_fd
        raw_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            OWNER_ONLY_FILE,
            dir_fd=parent_fd,
        )
        if stat.S_IMODE(os.fstat(raw_fd).st_mode) != OWNER_ONLY_FILE:
            raise OSError(f"cannot create {target} with owner-only permissions on this filesystem")
        handle = os.fdopen(raw_fd, "w", encoding=encoding)
        raw_fd = None
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        return _fsync_directory_fd(parent_fd) and directory_chain.postvalidate()
    except BaseException:
        if raw_fd is not None:
            with suppress(OSError):
                os.close(raw_fd)
        with suppress(OSError):
            os.unlink(tmp_name, dir_fd=directory_chain.leaf_fd)
        raise
    finally:
        directory_chain.close()


def _write_atomic_unscoped(target: Path, text: str, *, encoding: str) -> bool:
    """The atomic half of :func:`write_owner_only`, without the mode contract.

    Used only where the platform cannot represent the contract (native
    Windows). Same temporary shape, same replace, same cleanup — only the
    permission establishment and its verification are absent, because there
    is nothing true they could verify there.
    """
    tmp_path = target.with_name(f".{target.name[:_TMP_NAME_PREFIX_CHARS]}.tmp-{uuid4().hex}")
    raw_fd: int | None = None
    try:
        raw_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY_FILE)
        handle = os.fdopen(raw_fd, "w", encoding=encoding)
        raw_fd = None
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        if raw_fd is not None:
            with suppress(OSError):
                os.close(raw_fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    return fsync_parent_directory(target)


def secure_directory(path: Path) -> None:
    """Create ``path`` if needed and make it owner-only.

    Call this ONLY for directories this package creates and owns — the
    interview state directory and the fan-out registry directory. A Seed is
    written wherever the caller asks, which may be a shared project
    directory, and narrowing that from 0755 to 0700 would be this package
    changing something that is not its own. The Seed file itself is
    still owner-only through :func:`write_owner_only`.

    ``mkdir``'s mode argument is ignored when the directory already exists, so
    an inherited `0755` state directory keeps its permissions unless it is
    chmod'd explicitly. Failure is suppressed: a directory we do not own is
    not ours to re-permission, and refusing to run there would be worse than
    proceeding with the file mode we do control.

    Ownership is checked HERE rather than trusted to the caller:
    every one of these directories is caller-suppliable, and a supplied
    ``0755`` directory outside the package namespace was being narrowed to
    ``0700`` — revoking collaborator access this package had no business
    revoking. Outside ``~/.ouroboros`` the directory is created if missing
    and otherwise left exactly as found; the files written into it are still
    owner-only through :func:`write_owner_only`.
    """
    if not package_owned_directory(path):
        ensure_directory_no_symlinks(path, mode=0o777)
        return
    ensure_directory_no_symlinks(path, mode=OWNER_ONLY_DIR)
    with suppress(OSError):
        os.chmod(path, OWNER_ONLY_DIR)
