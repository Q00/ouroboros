"""Generation-bound filesystem publication and removal primitives.

Shared artifact mutations claim and pin exact generations, reject symlink
redirection, and publish with atomic no-replace renames. Tree removal ends at
logical namespace retirement: after ownership re-authentication, the complete
generation is renamed to an unpredictable terminal ``.retired`` tombstone and
is never recursively mutated. Concurrent descendant replacements therefore
survive inside the retired generation instead of being selected by stale
pathnames. The transaction ledger remains limited to replayable import and
shared-container intents.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat as stat_module
import tempfile

from ouroboros.core.file_lock import file_lock


class UnownedArtifactError(OSError):
    """The target of a publication or removal is not the caller-owned generation."""


_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)
_LINK_DIR_FD_SUPPORTED = _DIR_FD_SUPPORTED and os.link in os.supports_dir_fd

_RENAME_NOREPLACE_LINUX = 1  # include/uapi/linux/fs.h RENAME_NOREPLACE
_RENAME_EXCL_DARWIN = 0x00000004  # sys/stdio.h RENAME_EXCL


def _load_rename_no_replace() -> Callable[[int, str, int, str], int] | None:
    """Resolve the platform's atomic no-replace renameat variant, if any."""
    if os.name != "posix":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    import sys

    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2

        def _linux(src_fd: int, src: str, dst_fd: int, dst: str) -> int:
            return int(
                renameat2(
                    src_fd,
                    src.encode(),
                    dst_fd,
                    dst.encode(),
                    _RENAME_NOREPLACE_LINUX,
                )
            )

        return _linux
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        renameatx_np = libc.renameatx_np

        def _darwin(src_fd: int, src: str, dst_fd: int, dst: str) -> int:
            return int(
                renameatx_np(src_fd, src.encode(), dst_fd, dst.encode(), _RENAME_EXCL_DARWIN)
            )

        return _darwin
    return None


_RENAME_NO_REPLACE = _load_rename_no_replace()

# Only regular files and directories can be owned generations: any other
# entry type (FIFO, socket, device, symlink) is rejected before the ownership
# predicate runs, so content reads can never block on a special file.
_OWNABLE_ENTRY_TYPES = (stat_module.S_IFREG, stat_module.S_IFDIR)


_INTENT_MARKER = ".ouroboros-intent"


def _write_intent_marker(container_fd: int, canonical_name: str, *, exist_ok: bool = False) -> None:
    """Record which canonical artifact a container's contents belong to."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= os.O_TRUNC if exist_ok else os.O_EXCL
    try:
        fd = os.open(_INTENT_MARKER, flags, 0o600, dir_fd=container_fd)
    except FileExistsError:
        return
    try:
        os.write(fd, canonical_name.encode("utf-8"))
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _read_intent_marker(container_fd: int) -> str | None:
    try:
        fd = os.open(_INTENT_MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=container_fd)
    except OSError:
        return None
    try:
        return os.read(fd, 4096).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


_TRANSACTION_LEDGER_ENV = "OUROBOROS_FS_TRANSACTION_DIR"
_CONTAINER_PATTERN = re.compile(r"^\..+\.([0-9a-f]{16})\.(importing|discarding)$")


def _transaction_ledger_root() -> Path:
    """Return the exclusively writer-owned transaction-ledger root.

    Shared-parent import/discard containers are forgeable by any same-user
    writer. Destructive recovery authority therefore lives here instead: a
    ledger record binds the canonical path, operation, and exact container.
    Private erasure quarantines also live under this root and are created,
    replayed, and retired relative to the same pinned descriptor as their
    records, so replacing the configured pathname cannot split the authority.
    """
    override = os.environ.get(_TRANSACTION_LEDGER_ENV)
    if override:
        return Path(override)
    return Path.home() / ".ouroboros" / "fs-transactions"


def _container_nonce(container: str) -> str | None:
    match = _CONTAINER_PATTERN.match(container)
    return match.group(1) if match else None


def _ledger_canonical(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


class _LedgerAuthority:
    """The pinned writer-owned transaction-ledger root.

    Locking, record creation, reads, retirement, listing, and private
    quarantine lifecycle operations all run relative to one directory
    descriptor opened once. A ledger root renamed and recreated
    mid-transaction can therefore never split a publisher and a reconciler
    across different lock, record, or residue authorities: whoever opened
    this authority keeps acting on the exact directory generation it pinned,
    necessarily reading and retiring the same records.
    """

    def __init__(self) -> None:
        root = _transaction_ledger_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        self.fd = os.open(root, flags)

    def close(self) -> None:
        os.close(self.fd)

    @contextmanager
    def lock(self, canonical: str) -> Iterator[None]:
        """Serialize one canonical path's ledger/container lifecycle.

        Ledger-record creation and container publication are separate
        filesystem effects, so on their own they are independently
        interleavable. This lock turns them into one generation: a
        cooperating publisher holds it from before its record is written
        until its container exists (or the transaction rolled back), and
        reconciliation acquires it before replaying or retiring anything.
        Acquisition therefore proves no live publisher is inside its
        record-before-container window — a directory snapshot taken under
        the lock is evidence of completion, never a guess. The lock file is
        opened relative to the pinned root descriptor, keyed by the
        canonical path, so holders never touch the operator-shared parent
        and a swapped root cannot hand out an unrelated lock. Never nest
        acquisitions: ``flock`` conflicts across descriptors within one
        process as well.
        """
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        with file_lock(self.path / key, parent_fd=self.fd):
            yield

    def record(
        self,
        nonce: str,
        *,
        canonical: Path,
        container: str,
        operation: str,
        identity: tuple[int, int, int] | None = None,
    ) -> None:
        """Durably record a transaction before its container becomes discoverable."""
        record: dict[str, object] = {
            "canonical": _ledger_canonical(canonical),
            "container": container,
            "operation": operation,
        }
        if identity is not None:
            record["identity"] = list(identity)
        payload = json.dumps(record).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(f"{nonce}.json", flags, 0o600, dir_fd=self.fd)
        try:
            os.write(fd, payload)
            with suppress(OSError):
                os.fsync(fd)
        finally:
            os.close(fd)
        with suppress(OSError):
            os.fsync(self.fd)

    def read(self, nonce: str) -> dict[str, object] | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(f"{nonce}.json", flags, dir_fd=self.fd)
        except OSError:
            return None
        try:
            raw = os.read(fd, 1 << 16)
        except OSError:
            return None
        finally:
            os.close(fd)
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return record if isinstance(record, dict) else None

    def retire(self, nonce: str) -> None:
        with suppress(OSError):
            os.unlink(f"{nonce}.json", dir_fd=self.fd)
        with suppress(OSError):
            os.fsync(self.fd)

    def nonces(self) -> list[str]:
        try:
            names = os.listdir(self.fd)
        except OSError:
            return []
        return [name[: -len(".json")] for name in names if name.endswith(".json")]


@contextmanager
def _ledger_authority() -> Iterator[_LedgerAuthority]:
    authority = _LedgerAuthority()
    try:
        yield authority
    finally:
        authority.close()


def _digest_into(digest: hashlib._Hash, path: Path, relative: bytes) -> bool:
    entry = os.lstat(path)
    kind = stat_module.S_IFMT(entry.st_mode)
    if kind == stat_module.S_IFLNK:
        digest.update(b"link\0" + relative + b"\0" + os.fsencode(os.readlink(path)) + b"\0")
        return True
    if kind == stat_module.S_IFREG:
        digest.update(b"file\0" + relative + b"\0")
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        digest.update(b"\0")
        return True
    if kind == stat_module.S_IFDIR:
        digest.update(b"dir\0" + relative + b"\0")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            if not _digest_into(digest, child, relative + b"/" + os.fsencode(child.name)):
                return False
        return True
    return False


def _entry_digest(path: Path) -> str | None:
    """Content digest of one file, tree, or symlink entry; None when unsupported.

    This is the *generation* of an entry — structure plus every descendant's
    bytes — so publication can be bound to the authored content, not merely
    the top-level inode, which an in-place write would leave unchanged.
    """
    digest = hashlib.sha256()
    try:
        complete = _digest_into(digest, path, b"")
    except OSError:
        return None
    return digest.hexdigest() if complete else None


def _file_content_digest(content: str) -> str:
    """The :func:`_entry_digest` value a regular file with *content* will have."""
    digest = hashlib.sha256()
    digest.update(b"file\0" + b"" + b"\0")
    digest.update(content.encode("utf-8"))
    digest.update(b"\0")
    return digest.hexdigest()


def _rename_no_replace_between(src_fd: int, src_name: str, dst_fd: int, dst_name: str) -> None:
    """Atomically rename across two held descriptors without replacing the target."""
    if _RENAME_NO_REPLACE is not None:
        result = _RENAME_NO_REPLACE(src_fd, src_name, dst_fd, dst_name)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(code, os.strerror(code), dst_name)
        if code not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(code, os.strerror(code), dst_name)
    # Last-resort guarded rename; every supported platform takes the branch above.
    try:
        os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst_name)


class _PinnedEntry:
    """One held entry capability: an ``O_NOFOLLOW`` descriptor plus its identity.

    While the descriptor is open the filesystem cannot recycle the inode, so
    the identity tuple stays unique for the transaction's lifetime. ``fd`` is
    ``None`` only for symlink entries (which every flow rejects) and on the
    non-``*at`` fallback, where identity checks are best effort.
    """

    __slots__ = ("fd", "identity")

    def __init__(self, fd: int | None, identity: tuple[int, int, int]) -> None:
        self.fd = fd
        self.identity = identity

    def close(self) -> None:
        if self.fd is not None:
            with suppress(OSError):
                os.close(self.fd)
            self.fd = None


class _PinnedParent:
    """One held parent-directory capability for name-relative mutations."""

    __slots__ = ("fd", "path")

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self.fd = fd

    def revalidate(self) -> None:
        """Fail closed if the canonical parent path no longer names the pinned inode.

        Ownership predicates and tree builds read through the canonical
        pathname; this check brackets those reads so a parent renamed away and
        replaced by a symlink or substitute directory cannot redirect them.
        """
        if self.fd is None:
            return
        try:
            opened = os.fstat(self.fd)
            current = os.stat(self.path)
        except OSError as exc:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "artifact parent directory changed during mutation",
                str(self.path),
            ) from exc
        if not stat_module.S_ISDIR(current.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "artifact parent directory changed during mutation",
                str(self.path),
            )

    def is_symlink(self, name: str) -> bool:
        if self.fd is None:
            return (self.path / name).is_symlink()
        try:
            return stat_module.S_ISLNK(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def entry_identity(self, name: str) -> tuple[int, int, int] | None:
        """(device, inode, file-type) of one entry without following links."""
        try:
            if self.fd is None:
                entry = os.lstat(self.path / name)
            else:
                entry = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return None
        return (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))

    def pin_entry(self, name: str) -> _PinnedEntry | None:
        """Hold one entry open ``O_NOFOLLOW`` so its inode cannot be recycled.

        The returned capability keeps the claimed inode alive for the duration
        of a transaction: a writer that unlinks the entry and recreates it
        necessarily gets a *different* inode, so identity re-checks against
        the pinned identity detect every replacement — a bare
        ``(device, inode)`` tuple without a held descriptor would be defeated
        by immediate inode reuse. A symlink entry is returned identity-only
        (the type marks it for rejection); ``None`` means the entry is gone.

        The open must never block: ``O_PATH`` where the platform has it
        (which also never triggers device side effects), ``O_NONBLOCK``
        otherwise, so a FIFO or other special file planted at a managed path
        cannot stall the transaction — its type is rejected by every caller
        before any content read.
        """
        if self.fd is None:
            identity = self.entry_identity(name)
            return None if identity is None else _PinnedEntry(None, identity)
        flags = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_PATH"):
            flags |= os.O_PATH
        else:
            flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        try:
            entry_fd = os.open(name, flags, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                identity = self.entry_identity(name)
                return None if identity is None else _PinnedEntry(None, identity)
            identity = self.entry_identity(name)
            return None if identity is None else _PinnedEntry(None, identity)
        entry = os.fstat(entry_fd)
        return _PinnedEntry(
            entry_fd, (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))
        )

    def remove_entry(
        self, name: str, expected_identity: tuple[int, int, int] | None = None
    ) -> None:
        """Delete one held entry relative to the pinned descriptor.

        With *expected_identity*, the deletion is bound to the exact inode the
        caller validated: an entry that no longer carries that identity raises
        :class:`UnownedArtifactError` instead of being deleted.
        """
        if self.fd is None:
            if expected_identity is not None and self.entry_identity(name) != expected_identity:
                raise UnownedArtifactError(
                    f"claimed generation changed identity before removal: {self.path / name}"
                )
            remove_path(self.path / name)
            return
        try:
            entry = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            if expected_identity is not None:
                raise UnownedArtifactError(
                    f"claimed generation disappeared before removal: {self.path / name}"
                ) from None
            return
        identity = (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))
        if expected_identity is not None and identity != expected_identity:
            raise UnownedArtifactError(
                f"claimed generation changed identity before removal: {self.path / name}"
            )
        if stat_module.S_ISDIR(entry.st_mode):
            shutil.rmtree(name, dir_fd=self.fd)
        else:
            os.unlink(name, dir_fd=self.fd)

    def make_staging_dir(self, prefix: str) -> str:
        if self.fd is None:
            return Path(tempfile.mkdtemp(prefix=prefix, suffix=".tmp", dir=str(self.path))).name
        name = f"{prefix}{os.urandom(8).hex()}.tmp"
        os.mkdir(name, mode=0o700, dir_fd=self.fd)
        return name

    def import_entry(self, source: Path, name: str, *, canonical_name: str | None = None) -> None:
        """Bring a privately built entry under the pinned parent, fd-relative only.

        *source* lives in a private staging workspace nobody else can reach,
        so reading it by pathname is safe; every write lands through the
        pinned descriptor (``dst_dir_fd`` rename, or a descriptor-relative
        recursive copy across filesystems), so a canonical parent renamed and
        replaced by a symlink during a build can never redirect the import.
        """
        if self.fd is None:
            shutil.move(os.fspath(source), self.path / name)
            return
        try:
            os.rename(os.fspath(source), name, dst_dir_fd=self.fd)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        # Cross-filesystem: assemble inside a self-describing 0700 intent
        # container, then commit with one atomic fd-relative rename. The
        # ledger record written first — in the writer-owned transaction root,
        # not the shared parent — is what authorizes recovery to reconcile
        # this container after a crash; the in-container intent marker is
        # discovery metadata only. The whole record-to-commit lifetime runs
        # under the canonical path's ledger lock, so a concurrent
        # reconciliation can never observe (and retire) the record while the
        # container has not been published yet, nor replay the container out
        # from under the still-running copy.
        canonical_path = self.path / (canonical_name or name)
        with _ledger_authority() as ledger, ledger.lock(_ledger_canonical(canonical_path)):
            nonce = os.urandom(8).hex()
            container = f".{name}.{nonce}.importing"
            ledger.record(
                nonce,
                canonical=canonical_path,
                container=container,
                operation="importing",
            )
            try:
                os.mkdir(container, mode=0o700, dir_fd=self.fd)
            except OSError:
                ledger.retire(nonce)
                raise
            container_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            )
            container_fd = os.open(container, container_flags, dir_fd=self.fd)
            try:
                _write_intent_marker(container_fd, canonical_name or name)
                try:
                    _import_tree_fd(source, container_fd, "entry")
                    os.rename("entry", name, src_dir_fd=container_fd, dst_dir_fd=self.fd)
                except BaseException:
                    with suppress(OSError):
                        self._discard_container_entry(
                            container,
                            container_fd,
                            canonical_name=canonical_name or name,
                            ledger=ledger,
                        )
                    raise
                os.unlink(_INTENT_MARKER, dir_fd=container_fd)
            finally:
                os.close(container_fd)
            try:
                os.rmdir(container, dir_fd=self.fd)
            except OSError:
                pass
            else:
                ledger.retire(nonce)

    def _discard_container_entry(
        self, container: str, container_fd: int, *, canonical_name: str, ledger: _LedgerAuthority
    ) -> None:
        """Retire a validated tree generation without recursively mutating it."""
        _write_intent_marker(container_fd, canonical_name, exist_ok=True)
        held = os.fstat(container_fd)
        current = os.stat(container, dir_fd=self.fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise UnownedArtifactError(
                f"retirement container changed generation: {self.path / container}"
            )

        retired = f".{canonical_name}.{os.urandom(8).hex()}.retired"
        _rename_no_replace_between(self.fd, container, self.fd, retired)
        captured = os.stat(retired, dir_fd=self.fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (captured.st_dev, captured.st_ino):
            raise UnownedArtifactError(
                f"retired generation changed identity: {self.path / retired}"
            )
        self.fsync()

        source_nonce = _container_nonce(container)
        if source_nonce is not None:
            ledger.retire(source_nonce)

    def quarantined_remove(
        self,
        name: str,
        expected_identity: tuple[int, int, int],
        *,
        reauthenticate: Callable[[Path], bool] | None = None,
        canonical_name: str | None = None,
    ) -> None:
        """Destroy one validated entry through a descriptor-pinned quarantine.

        A bare no-follow ``stat`` followed by a pathname ``unlink`` leaves a
        window in which a watcher can swap the claim name; instead the entry
        is atomically renamed into a fresh, unpredictable ``0700`` quarantine
        directory — held open through its own ``O_NOFOLLOW`` descriptor, so
        the container itself cannot be swapped either — and re-validated
        *there* against the pinned identity. With *reauthenticate*, the
        ownership predicate runs again on the isolated entry, so a tree whose
        descendants were modified after the original predicate read is
        preserved even though its top-level inode never changed. Only an
        entry that passes every re-check is destroyed; anything else — and
        any entry whose destruction fails partway — is moved back out of the
        quarantine untouched, under its original or a discoverable name, and
        the removal fails with :class:`UnownedArtifactError` (or the
        underlying error).
        """
        if self.fd is None:
            if self.entry_identity(name) != expected_identity or (
                reauthenticate is not None and not reauthenticate(self.path / name)
            ):
                raise UnownedArtifactError(
                    f"claimed generation changed before removal: {self.path / name}"
                )
            remove_path(self.path / name)
            return
        quarantine = self.make_staging_dir(prefix=f".{name}.")
        created = os.stat(quarantine, dir_fd=self.fd, follow_symlinks=False)
        quarantine_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            quarantine_fd = os.open(quarantine, quarantine_flags, dir_fd=self.fd)
        except OSError as exc:
            raise UnownedArtifactError(
                f"could not pin the removal quarantine beside {self.path / name}"
            ) from exc
        try:
            held = os.fstat(quarantine_fd)
            if (held.st_dev, held.st_ino) != (created.st_dev, created.st_ino):
                raise UnownedArtifactError(
                    f"removal quarantine changed identity beside {self.path / name}"
                )

            def _entry_identity() -> tuple[int, int, int] | None:
                try:
                    entry = os.stat("entry", dir_fd=quarantine_fd, follow_symlinks=False)
                except OSError:
                    return None
                return (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))

            def _evict() -> None:
                try:
                    _rename_no_replace_between(quarantine_fd, "entry", self.fd, name)
                except OSError:
                    with suppress(OSError):
                        _rename_no_replace_between(
                            quarantine_fd,
                            "entry",
                            self.fd,
                            f"{name}.{os.urandom(4).hex()}.evicted",
                        )

            try:
                os.rename(name, "entry", src_dir_fd=self.fd, dst_dir_fd=quarantine_fd)
            except FileNotFoundError:
                raise UnownedArtifactError(
                    f"claimed generation disappeared before removal: {self.path / name}"
                ) from None
            identity = _entry_identity()
            if identity != expected_identity:
                # The atomic move captured a raced-in replacement, not the
                # validated generation: put it back out untouched and abort.
                _evict()
                raise UnownedArtifactError(
                    f"claimed generation changed identity before removal: {self.path / name}"
                )
            if reauthenticate is not None:
                quarantined_path = self.path / quarantine / "entry"
                try:
                    self.revalidate()
                    still_owned = (
                        reauthenticate(quarantined_path) and _entry_identity() == expected_identity
                    )
                    self.revalidate()
                except BaseException:
                    _evict()
                    raise
                if not still_owned:
                    _evict()
                    raise UnownedArtifactError(
                        f"claimed generation changed content before removal: {self.path / name}"
                    )
            if identity[2] == stat_module.S_IFDIR:
                # Removal commits with the atomic move into the quarantine; a
                # recursive deletion that fails partway must never restore a
                # half-destroyed tree, so trees are discarded commit-first.
                # A discard whose cleanup cannot complete leaves the residue
                # in an intent-marked tombstone and raises, so the removal is
                # reported truthfully instead of claiming a finished cleanup.
                # Under the ledger lock: a failed discard hands the residue a
                # tombstone ledger record, and that record-before-rename
                # window must not be observable by a concurrent reconciler.
                with (
                    _ledger_authority() as ledger,
                    ledger.lock(_ledger_canonical(self.path / (canonical_name or name))),
                ):
                    self._discard_container_entry(
                        quarantine,
                        quarantine_fd,
                        canonical_name=canonical_name or name,
                        ledger=ledger,
                    )
            else:
                try:
                    os.unlink("entry", dir_fd=quarantine_fd)
                except OSError:
                    # A single unlink is atomic and leaves the file intact on
                    # failure, so restoring it from the quarantine is safe and
                    # keeps the generation discoverable for a retry.
                    _evict()
                    raise
        finally:
            os.close(quarantine_fd)
            # The container is removed on success and failure alike; an
            # eviction that could not empty it leaves it for the operator.
            with suppress(OSError):
                os.rmdir(quarantine, dir_fd=self.fd)

    def lexists(self, name: str) -> bool:
        if self.fd is None:
            return os.path.lexists(self.path / name)
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def is_directory(self, name: str) -> bool:
        if self.fd is None:
            candidate = self.path / name
            return not candidate.is_symlink() and candidate.is_dir()
        try:
            return stat_module.S_ISDIR(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def replace(self, source_name: str, target_name: str) -> None:
        if self.fd is None:
            os.replace(self.path / source_name, self.path / target_name)
            return
        os.rename(source_name, target_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def rename_no_replace(self, source_name: str, target_name: str) -> None:
        """Atomically rename; raise FileExistsError when the target exists."""
        if self.fd is not None and _RENAME_NO_REPLACE is not None:
            result = _RENAME_NO_REPLACE(self.fd, source_name, self.fd, target_name)
            if result == 0:
                return
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError(code, os.strerror(code), target_name)
            if code not in (errno.ENOSYS, errno.EINVAL):
                raise OSError(code, os.strerror(code), target_name)
        if os.name == "nt":  # pragma: no cover - Windows rename is natively no-replace
            os.rename(self.path / source_name, self.path / target_name)
            return
        # Last-resort guarded rename: the window between the check and the
        # rename is not atomic, but every supported platform takes one of the
        # atomic branches above.
        if self.lexists(target_name):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target_name)
        self.replace(source_name, target_name)

    def link_no_clobber(self, source_name: str, target_name: str) -> bool:
        """Atomically link *source_name* to *target_name*; False when it exists."""
        if not _LINK_DIR_FD_SUPPORTED or self.fd is None:
            raise NotImplementedError
        try:
            os.link(
                source_name,
                target_name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(source_name, dir_fd=self.fd)
        return True

    def create_temp_file(self, prefix: str) -> tuple[int, str]:
        if self.fd is None:
            return tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(self.path))
        name = f"{prefix}{os.urandom(8).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, 0o600, dir_fd=self.fd), name

    def unlink(self, name: str) -> None:
        with suppress(OSError):
            if self.fd is None:
                (self.path / name).unlink()
            else:
                os.unlink(name, dir_fd=self.fd)

    def fsync(self) -> None:
        if self.fd is None:
            return
        with suppress(OSError):
            os.fsync(self.fd)


def _import_tree_fd(source: Path, parent_fd: int, name: str) -> None:
    """Copy a privately built entry into *parent_fd* using only fd-relative writes.

    *source* is trusted (it lives in a private workspace), so pathname reads
    are safe; every created entry lands relative to a held descriptor, so no
    concurrent parent swap can redirect a single write. Only regular files,
    directories, and symlinks are importable.
    """
    entry = os.lstat(source)
    mode = stat_module.S_IMODE(entry.st_mode)
    if stat_module.S_ISLNK(entry.st_mode):
        os.symlink(os.readlink(source), name, dir_fd=parent_fd)
        return
    if stat_module.S_ISREG(entry.st_mode):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        try:
            with source.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    os.write(fd, chunk)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
        finally:
            os.close(fd)
        return
    if stat_module.S_ISDIR(entry.st_mode):
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
        try:
            for child in sorted(source.iterdir(), key=lambda item: item.name):
                _import_tree_fd(child, child_fd, child.name)
            if hasattr(os, "fchmod"):
                os.fchmod(child_fd, mode)
        finally:
            os.close(child_fd)
        return
    raise OSError(errno.EINVAL, "unsupported staged entry type", str(source))


def _nofollow_components(
    parent: Path, trusted_ancestor: Path | None
) -> tuple[Path, list[str], bool]:
    """Split *parent* into a base, the components to walk no-follow, and whether
    the base is a caller-declared trusted root that must itself be pinned."""
    if trusted_ancestor is None:
        return parent.parent, [parent.name], False
    ancestor = Path(trusted_ancestor)
    try:
        relative = parent.relative_to(ancestor)
    except ValueError:
        return parent.parent, [parent.name], False
    return ancestor, [part for part in relative.parts if part not in ("", ".")], True


def _symlink_component_error(component: Path) -> OSError:
    return OSError(
        errno.ELOOP,
        "refusing to operate through a symlinked artifact parent component",
        str(component),
    )


def _symlink_trusted_root_error(root: Path) -> OSError:
    return OSError(
        errno.ELOOP,
        "refusing to operate under a symlinked trusted root",
        str(root),
    )


@contextmanager
def _pinned_parent(
    parent: Path,
    *,
    trusted_ancestor: Path | None,
    create: bool,
) -> Iterator[_PinnedParent]:
    """Pin *parent*, walking every component below the trusted ancestor no-follow.

    A caller-declared trusted root is pinned by walking every component of
    its absolute path from the filesystem root with ``O_NOFOLLOW``: a symlink
    at the root itself *or anywhere on the way to it* (for example a
    configured ``/profile-link/agent`` whose ``profile-link`` is a symlink)
    is rejected instead of silently redirecting every mutation into its
    target. An operator whose profile legitimately lives behind a symlink
    must configure the resolved path instead.
    """
    base, components, base_is_trusted_root = _nofollow_components(parent, trusted_ancestor)
    if not _DIR_FD_SUPPORTED:
        if base_is_trusted_root:  # pragma: no cover - non-posix best effort
            probe = Path(os.path.abspath(base))
            for ancestor in (probe, *probe.parents):
                if ancestor.is_symlink():
                    raise _symlink_trusted_root_error(ancestor)
        current = base
        for name in components:  # pragma: no cover - non-posix best effort
            current = current / name
            if current.is_symlink():
                raise _symlink_component_error(current)
            if create:
                current.mkdir(exist_ok=True)
        yield _PinnedParent(path=parent, fd=None)
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | os.O_NOFOLLOW
    if base_is_trusted_root:
        resolved_base = Path(os.path.abspath(base))
        anchor = resolved_base.anchor or os.sep
        fd = os.open(anchor, flags)
        try:
            walked = Path(anchor)
            for name in resolved_base.parts[len(Path(anchor).parts) :]:
                walked = walked / name
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(name, mode=0o755, dir_fd=fd)
                try:
                    child = os.open(name, nofollow_flags, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                        raise _symlink_trusted_root_error(walked) from exc
                    raise
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
    else:
        if create:
            base.mkdir(parents=True, exist_ok=True)
        fd = os.open(base, flags)
    try:
        walked = base
        for name in components:
            walked = walked / name
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o755, dir_fd=fd)
            try:
                child = os.open(name, nofollow_flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise _symlink_component_error(walked) from exc
                raise
            os.close(fd)
            fd = child
        yield _PinnedParent(path=parent, fd=fd)
    finally:
        os.close(fd)


def _claim_name(name: str, operation: str) -> str:
    return f".{name}.{os.urandom(8).hex()}.{operation}"


_CLAIM_NAME_PATTERN = re.compile(r"^\.(?P<name>.+)\.[0-9a-f]{16}\.(?:removing|replacing)$")


def find_orphaned_claims(parent: Path) -> tuple[str, ...]:
    """Canonical entry names that have interrupted claim siblings under *parent*.

    A claim sibling left behind by a crashed transaction encodes the canonical
    name it was renamed from; discovery uses this to notice managed state that
    is currently hidden under a claim name.
    """
    try:
        entries = os.listdir(parent)
    except OSError:
        return ()
    names = {
        match.group("name") for entry in entries if (match := _CLAIM_NAME_PATTERN.match(entry))
    }
    return tuple(sorted(names))


def _claim_siblings(parent: _PinnedParent, name: str) -> list[str]:
    pattern = re.compile(rf"^\.{re.escape(name)}\.[0-9a-f]{{16}}\.(?:removing|replacing)$")
    try:
        entries = os.listdir(parent.fd if parent.fd is not None else parent.path)
    except OSError:
        return []
    return sorted(entry for entry in entries if pattern.match(entry))


def _reconcile_stale_containers(parent: _PinnedParent, path: Path) -> bool:
    """Reconcile interrupted import/discard containers that belong to *path*."""
    if parent.fd is None:
        return False
    canonical = _ledger_canonical(path)
    changed = False
    container_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    with _ledger_authority() as ledger, ledger.lock(canonical):
        try:
            entries = os.listdir(parent.fd)
        except OSError:
            return False
        for entry in sorted(entries):
            match = _CONTAINER_PATTERN.match(entry)
            if match is None:
                continue
            record = ledger.read(match.group(1))
            if (
                record is None
                or record.get("container") != entry
                or record.get("canonical") != canonical
                or record.get("operation") != match.group(2)
            ):
                continue
            try:
                container_fd = os.open(entry, container_flags, dir_fd=parent.fd)
            except OSError:
                continue
            try:
                marker = _read_intent_marker(container_fd)
                if marker is not None and marker != path.name:
                    continue
                with suppress(OSError):
                    parent._discard_container_entry(
                        entry, container_fd, canonical_name=path.name, ledger=ledger
                    )
                    changed = True
            finally:
                os.close(container_fd)

        with suppress(OSError):
            try:
                remaining = frozenset(os.listdir(parent.fd))
            except OSError:
                return changed
            for nonce in ledger.nonces():
                record = ledger.read(nonce)
                if record is None or record.get("canonical") != canonical:
                    continue
                operation = record.get("operation")
                location = record.get("container")
                if operation in ("importing", "discarding") and location not in remaining:
                    ledger.retire(nonce)
    return changed


def _recover_claims(parent: _PinnedParent, path: Path, is_owned: Callable[[Path], bool]) -> bool:
    """Reconcile interrupted claim siblings of *path* left by a crashed transaction.

    Claim-name syntax is discovery metadata, not ownership evidence: any
    shared-directory writer can forge a claim-shaped sibling, so ownership is
    authenticated *while the entry is still under its claim name* — through a
    pinned entry descriptor — before anything is promoted or deleted. Only an
    owned claim is restored (no-clobber) when the canonical path is absent, or
    deleted as a leftover when the canonical path is occupied. A claim that
    fails authentication is left untouched as a collision for the operator.
    """
    changed = _reconcile_stale_containers(parent, path)
    for claim_name in _claim_siblings(parent, path.name):
        pin = parent.pin_entry(claim_name)
        if pin is None:
            continue
        try:
            claimed = path.with_name(claim_name)
            try:
                parent.revalidate()
                owned = (
                    pin.identity[2] in _OWNABLE_ENTRY_TYPES
                    and parent.entry_identity(claim_name) == pin.identity
                    and is_owned(claimed)
                    and parent.entry_identity(claim_name) == pin.identity
                )
                parent.revalidate()
            except OSError:
                continue
            if not owned:
                continue
            if not parent.lexists(path.name):
                if parent.entry_identity(claim_name) == pin.identity and _restore_claimed(
                    parent, claim_name, path.name
                ):
                    changed = True
                continue
            with suppress(OSError):
                parent.quarantined_remove(
                    claim_name, pin.identity, reauthenticate=is_owned, canonical_name=path.name
                )
                changed = True
        finally:
            pin.close()
    if changed:
        parent.fsync()
    return changed


def recover_owned_claims(
    path: Path,
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> bool:
    """Reconcile interrupted claim state beside *path*; True when anything changed.

    Every publish/remove transaction on a path also runs this reconciliation
    first, so calling it explicitly is only needed for discovery flows that
    must observe recovered state without mutating the artifact.
    """
    try:
        with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=False) as parent:
            return _recover_claims(parent, path, is_owned)
    except FileNotFoundError:
        return False


def has_recoverable_claim(
    path: Path,
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> bool:
    """Read-only discovery: does an authenticated owned claim sibling of *path* exist?

    Authenticates under the claim name exactly like recovery — pinned entry,
    ownable type before any content read, identity-bracketed predicate — but
    mutates nothing. Discovery flows use this to decide whether managed state
    exists; claim-name syntax alone is never evidence, so a forged or
    unrelated claim-shaped sibling reports False.
    """
    try:
        with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=False) as parent:
            for claim_name in _claim_siblings(parent, path.name):
                pin = parent.pin_entry(claim_name)
                if pin is None:
                    continue
                try:
                    try:
                        parent.revalidate()
                        owned = (
                            pin.identity[2] in _OWNABLE_ENTRY_TYPES
                            and parent.entry_identity(claim_name) == pin.identity
                            and is_owned(path.with_name(claim_name))
                            and parent.entry_identity(claim_name) == pin.identity
                        )
                        parent.revalidate()
                    except OSError:
                        continue
                    if owned:
                        return True
                finally:
                    pin.close()
    except OSError:
        return False
    return False


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore_claimed(claimed: Path, path: Path) -> bool:
    """Put a claimed generation back unless the canonical path was recreated.

    Returns True when restored. When another process recreated *path* while
    the claim was held, restoring would clobber that new generation, so both
    are preserved instead: the recreated entry stays canonical and the
    claimed one remains beside it under its claim name.
    """
    try:
        with _pinned_parent(path.parent, trusted_ancestor=None, create=False) as parent:
            return _restore_claimed(parent, claimed.name, path.name)
    except FileNotFoundError:
        return False


def _restore_claimed(parent: _PinnedParent, claimed_name: str, name: str) -> bool:
    if _LINK_DIR_FD_SUPPORTED and parent.fd is not None and not parent.is_directory(claimed_name):
        try:
            restored = parent.link_no_clobber(claimed_name, name)
        except FileNotFoundError:
            return False
        except OSError:
            pass  # fall through to the rename below
        else:
            if restored:
                parent.fsync()
            return restored
    try:
        parent.rename_no_replace(claimed_name, name)
    except (FileNotFoundError, FileExistsError):
        return False
    parent.fsync()
    return True


def claim_and_remove_owned(
    path: Path,
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> bool:
    """Atomically claim *path* and delete it only if the claimed generation is owned.

    Returns False without touching anything when *path* is missing, and
    restores the claimed generation (no-clobber) when it is a symlink or
    fails the ownership re-check — the concurrent-replacement case.
    """
    claimed_name = _claim_name(path.name, "removing")
    claimed = path.with_name(claimed_name)
    try:
        with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=False) as parent:
            _recover_claims(parent, path, is_owned)
            try:
                parent.replace(path.name, claimed_name)
            except FileNotFoundError:
                return False
            parent.fsync()
            pin = parent.pin_entry(claimed_name)
            try:
                try:
                    parent.revalidate()
                    owned = (
                        pin is not None
                        and pin.identity[2] in _OWNABLE_ENTRY_TYPES
                        and is_owned(claimed)
                        and parent.entry_identity(claimed_name) == pin.identity
                    )
                    parent.revalidate()
                except BaseException:
                    _restore_claimed(parent, claimed_name, path.name)
                    raise
                if not owned or pin is None:
                    _restore_claimed(parent, claimed_name, path.name)
                    return False
                try:
                    parent.quarantined_remove(
                        claimed_name,
                        pin.identity,
                        reauthenticate=is_owned,
                        canonical_name=path.name,
                    )
                except UnownedArtifactError:
                    # The claimed sibling no longer carries the validated
                    # identity (the pinned descriptor makes inode reuse
                    # impossible, so this is a real replacement); deleting it
                    # would destroy a generation nobody attributed.
                    _restore_claimed(parent, claimed_name, path.name)
                    return False
                except OSError:
                    # A transient removal failure must not strand the
                    # generation under an undiscoverable claim name: restore
                    # the canonical route so discovery still sees it and a
                    # retry can succeed.
                    _restore_claimed(parent, claimed_name, path.name)
                    raise
            finally:
                if pin is not None:
                    pin.close()
            parent.fsync()
            return True
    except FileNotFoundError:
        return False


def publish_owned_file(
    path: Path,
    content: str,
    *,
    is_owned: Callable[[Path], bool],
    mode: int | None = None,
    trusted_ancestor: Path | None = None,
    require_existing: bool = False,
) -> None:
    """Atomically publish one owned file generation.

    Any existing entry is claimed (renamed aside) and re-validated before the
    replacement, and the final rename is no-replace — an operator file,
    symlink, or generation recreated after the caller's own checks is
    preserved untouched and the publication fails with
    :class:`UnownedArtifactError` instead of replacing it.
    """

    def _build(
        parent: _PinnedParent, target_name: str
    ) -> tuple[str, _PinnedEntry | None, str | None]:
        fd, temp_name = parent.create_temp_file(prefix=f".{target_name}.")
        staged_pin: _PinnedEntry | None = None
        try:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            # Pin the authored inode through the write descriptor itself, so
            # the staged generation is bound with no window at all.
            entry = os.fstat(fd)
            staged_pin = _PinnedEntry(
                os.dup(fd), (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            if staged_pin is not None:
                staged_pin.close()
            parent.unlink(temp_name)
            raise
        return temp_name, staged_pin, _file_content_digest(content)

    _publish_owned(
        path,
        _build,
        is_owned=is_owned,
        trusted_ancestor=trusted_ancestor,
        require_existing=require_existing,
    )


def publish_owned_tree(
    path: Path,
    build: Callable[[Path], None],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> None:
    """Atomically publish one owned directory-tree generation.

    *build* receives a staging directory in a private workspace (never a path
    under the shared parent) and must fill it with the complete new
    generation; the finished tree is then imported beside *path* through
    descriptor-bound writes only and replaces the canonical path under the
    same claim/verify/no-replace discipline as :func:`publish_owned_file`.
    A shared parent renamed away and replaced by a symlink during the build
    therefore cannot redirect a single builder write.
    """

    def _build(
        parent: _PinnedParent, target_name: str
    ) -> tuple[str, _PinnedEntry | None, str | None]:
        staging_name = f".{target_name}.{os.urandom(8).hex()}.tmp"
        with tempfile.TemporaryDirectory(prefix="ouroboros-staging-") as private_root:
            workspace = Path(private_root) / "tree"
            workspace.mkdir(mode=0o700)
            build(workspace)
            authored = _entry_digest(workspace)
            parent.import_entry(workspace, staging_name, canonical_name=target_name)
        return staging_name, parent.pin_entry(staging_name), authored

    _publish_owned(path, _build, is_owned=is_owned, trusted_ancestor=trusted_ancestor)


def publish_owned_entry(
    path: Path,
    build: Callable[[Path], None],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
    require_existing: bool = False,
) -> None:
    """Atomically publish one owned generation of arbitrary topology.

    *build* receives a nonexistent staging path in a private workspace (never
    a path under the shared parent) and must create the complete entry there
    — a file, a directory tree, or a symlink. The finished entry is imported
    beside *path* through descriptor-bound writes only and replaces the
    canonical path under the same claim/verify/no-replace discipline as
    :func:`publish_owned_file`; rollback flows use this to restore
    pre-transaction snapshots without a separate check-then-restore sequence.
    """

    def _build(
        parent: _PinnedParent, target_name: str
    ) -> tuple[str, _PinnedEntry | None, str | None]:
        staged_name = f".{target_name}.{os.urandom(8).hex()}.tmp"
        with tempfile.TemporaryDirectory(prefix="ouroboros-staging-") as private_root:
            workspace = Path(private_root) / "entry"
            build(workspace)
            if not os.path.lexists(workspace):
                raise OSError(errno.ENOENT, "entry build produced no staged generation", str(path))
            authored = _entry_digest(workspace)
            parent.import_entry(workspace, staged_name, canonical_name=target_name)
        return staged_name, parent.pin_entry(staged_name), authored

    _publish_owned(
        path,
        _build,
        is_owned=is_owned,
        trusted_ancestor=trusted_ancestor,
        require_existing=require_existing,
    )


def _publish_owned(
    path: Path,
    build: Callable[[_PinnedParent, str], tuple[str, _PinnedEntry | None, str | None]],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None,
    require_existing: bool = False,
) -> None:
    with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=True) as parent:
        _recover_claims(parent, path, is_owned)
        claimed_name: str | None = None
        pin: _PinnedEntry | None = None
        try:
            if parent.lexists(path.name):
                claimed_name = _claim_name(path.name, "replacing")
                claimed = path.with_name(claimed_name)
                parent.replace(path.name, claimed_name)
                parent.fsync()
                pin = parent.pin_entry(claimed_name)
                try:
                    parent.revalidate()
                    owned = (
                        pin is not None
                        and pin.identity[2] in _OWNABLE_ENTRY_TYPES
                        and is_owned(claimed)
                        and parent.entry_identity(claimed_name) == pin.identity
                    )
                    parent.revalidate()
                except BaseException:
                    _restore_claimed(parent, claimed_name, path.name)
                    raise
                if not owned:
                    _restore_claimed(parent, claimed_name, path.name)
                    raise UnownedArtifactError(f"preserved user-managed file at {path}")
            elif require_existing:
                raise UnownedArtifactError(f"artifact disappeared before publication: {path}")
            try:
                staged_name, staged_pin, staged_digest = build(parent, path.name)
            except BaseException:
                if claimed_name is not None:
                    _restore_claimed(parent, claimed_name, path.name)
                raise

            def _discard_staged() -> None:
                """Remove the staged entry, bound to the authored identity."""
                if staged_pin is not None:
                    with suppress(OSError, UnownedArtifactError):
                        # A staged entry that no longer carries the authored
                        # identity is a raced-in replacement: leave it.
                        parent.quarantined_remove(
                            staged_name, staged_pin.identity, canonical_name=path.name
                        )
                    return
                with suppress(OSError):
                    parent.remove_entry(staged_name)

            try:
                try:
                    parent.revalidate()
                    if (
                        staged_pin is None
                        or staged_digest is None
                        or parent.entry_identity(staged_name) != staged_pin.identity
                        or _entry_digest(path.with_name(staged_name)) != staged_digest
                    ):
                        # The randomly named staging entry was swapped or its
                        # content mutated in place after the build — the
                        # content generation, not just the inode, must match
                        # what this transaction authored.
                        raise UnownedArtifactError(
                            f"staged generation changed before publication: {path}"
                        )
                    parent.revalidate()
                    parent.rename_no_replace(staged_name, path.name)
                except FileExistsError as exc:
                    _discard_staged()
                    if claimed_name is not None:
                        _restore_claimed(parent, claimed_name, path.name)
                    raise UnownedArtifactError(
                        f"canonical path was recreated during publication: {path}"
                    ) from exc
                except BaseException:
                    _discard_staged()
                    if claimed_name is not None:
                        _restore_claimed(parent, claimed_name, path.name)
                    raise
                if (
                    parent.entry_identity(path.name) != staged_pin.identity
                    or _entry_digest(path) != staged_digest
                ):
                    # The rename itself was raced, or the content was mutated
                    # in place across the commit: an unauthored generation now
                    # sits at the canonical name. Pull it aside untouched.
                    with suppress(OSError):
                        parent.replace(path.name, _claim_name(path.name, "replacing"))
                    if claimed_name is not None:
                        _restore_claimed(parent, claimed_name, path.name)
                    raise UnownedArtifactError(
                        f"canonical path was raced during publication: {path}"
                    )
            finally:
                if staged_pin is not None:
                    staged_pin.close()
            if claimed_name is not None and pin is not None:
                with suppress(UnownedArtifactError):
                    # An identity change means the claimed sibling is no longer
                    # the generation this transaction validated (the pinned
                    # descriptor rules out inode reuse); leave it for recovery.
                    parent.quarantined_remove(
                        claimed_name,
                        pin.identity,
                        reauthenticate=is_owned,
                        canonical_name=path.name,
                    )
        finally:
            if pin is not None:
                pin.close()
        parent.fsync()
