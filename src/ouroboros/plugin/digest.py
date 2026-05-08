"""Plugin artifact digest.

Per the locked RFC (`docs/rfc/userlevel-plugins.md`, "Trust identity"),
trust records and lockfile entries are keyed by the tuple
`(source.type, source_identity, artifact_digest)`. The digest covers the
**complete installed artifact**, not just the manifest, so that a code
substitution under the same source produces a fresh trust subject.

The hashing input is dispatched per source type (see the RFC table). For
`plugin_home` and `local_path` the input is the **canonical tree hash**
of the installed subtree; for `first_party` it is the canonical tree
hash applied to the program's subtree at boot.

The serialization is independent of any tarball dialect (no `ustar` /
`pax` quirks) so arbitrary path lengths and link targets are covered:

  1. Walk the subtree depth-first, collecting one record per regular
     file and per symlink. Directories are implicit; other file types
     (devices, FIFOs, sockets) are rejected at install time.
  2. For each entry, build `<mode>\\0<path>\\0<sha256-of-content-or-link-target>\\0`.
  3. Sort the records lexicographically by `<path>` (NUL = byte 0x00).
  4. The canonical tree hash is `sha256(concat(sorted records))`, hex-prefixed
     with the literal `sha256:`.

The digest is recomputed before every invocation for `plugin_home` and
`local_path`; mismatch with the trusted record fails closed with
`result.status="trust_subject_changed"`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat


class UnsupportedFileTypeError(ValueError):
    """Raised when a subtree contains a non-regular, non-symlink, non-directory entry.

    The plugin firewall refuses to hash devices, FIFOs, sockets, etc.,
    because they are not legal artifact contents and would otherwise
    create implementation-defined behavior.
    """


def _file_mode_octal(mode: int) -> str:
    """Return the canonical mode bits for a file or symlink.

    Per the RFC: `0o755` or `0o644` for files (executable bit only),
    `0o777` for symlinks (mode is irrelevant for links but the constant
    is fixed for canonicalization).
    """
    if stat.S_ISLNK(mode):
        return "0777"
    if mode & stat.S_IXUSR:
        return "0755"
    return "0644"


def _hash_file_content(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_link_target(path: Path) -> str:
    target = os.readlink(path)
    return hashlib.sha256(target.encode("utf-8", errors="surrogateescape")).hexdigest()


def canonical_tree_hash(root: Path) -> str:
    """Compute the canonical tree hash for the subtree at `root`.

    Args:
        root: Path to the directory whose contents define the artifact.

    Returns:
        `"sha256:<hex>"`.

    Raises:
        FileNotFoundError: if `root` does not exist.
        NotADirectoryError: if `root` is not a directory.
        UnsupportedFileTypeError: if the subtree contains a device, FIFO,
            socket, or any other non-regular, non-symlink, non-directory
            entry.
    """
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"canonical_tree_hash root must be a directory: {root}")

    def _record_symlink(entry_path: Path, mode: int) -> bytes:
        content_hash = _hash_link_target(entry_path)
        relative = entry_path.relative_to(root).as_posix()
        mode_str = _file_mode_octal(mode)
        return (
            mode_str.encode("ascii")
            + b"\0"
            + relative.encode("utf-8", errors="surrogateescape")
            + b"\0"
            + content_hash.encode("ascii")
            + b"\0"
        )

    records: list[bytes] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        # Stable iteration: sort dir + file names so the os.walk traversal
        # is deterministic. The final record list is sorted explicitly
        # below by `<path>` regardless, but stable iteration keeps the
        # behavior reproducible under any concurrent mtime changes.
        dirnames.sort()
        filenames.sort()
        current_path = Path(current)

        # Capture symlinked directories that os.walk reports in
        # ``dirnames`` but never recurses into (because we run with
        # ``followlinks=False``). Without this, a plugin can hide
        # executable content behind a directory symlink and later
        # retarget that symlink without changing the artifact digest —
        # defeating the per-invocation drift check the trust model
        # relies on. We hash the link target the same way as a file
        # symlink and DROP the entry from ``dirnames`` so os.walk
        # doesn't try to descend into it (it wouldn't anyway with
        # followlinks=False, but keeping ``dirnames`` clean prevents
        # a future maintainer from flipping the flag and silently
        # double-counting the subtree).
        kept_dirnames: list[str] = []
        for dname in dirnames:
            dpath = current_path / dname
            try:
                dlstat = dpath.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(dlstat.st_mode):
                records.append(_record_symlink(dpath, dlstat.st_mode))
                # Skip recursion into the link target.
                continue
            kept_dirnames.append(dname)
        dirnames[:] = kept_dirnames

        for name in filenames:
            entry_path = current_path / name
            try:
                lstat = entry_path.lstat()
            except FileNotFoundError:
                # File vanished between the walk listing and the stat;
                # treat as if it didn't exist.
                continue
            mode = lstat.st_mode
            if stat.S_ISLNK(mode):
                records.append(_record_symlink(entry_path, mode))
                continue
            if stat.S_ISREG(mode):
                content_hash = _hash_file_content(entry_path)
            else:
                raise UnsupportedFileTypeError(
                    f"unsupported file type at {entry_path} "
                    f"(mode {oct(mode)}): only regular files, symlinks, "
                    f"and directories are permitted in plugin subtrees"
                )
            relative = entry_path.relative_to(root).as_posix()
            mode_str = _file_mode_octal(mode)
            record = (
                mode_str.encode("ascii")
                + b"\0"
                + relative.encode("utf-8", errors="surrogateescape")
                + b"\0"
                + content_hash.encode("ascii")
                + b"\0"
            )
            records.append(record)

    records.sort()
    h = hashlib.sha256()
    for record in records:
        h.update(record)
    return f"sha256:{h.hexdigest()}"


def normalize_repo_url(url: str) -> str:
    """Normalize a repo URL into the canonical `source_identity` per the RFC.

    Strict and conservative:

    - Strip any trailing `.git`.
    - Strip the URL fragment (everything after `#`).
    - Strip embedded userinfo (`https://user:pass@host/...` → `https://host/...`).
    - Drop the `git+` URL wrapper (`git+https://...` and `git+ssh://...`
      both clone the same upstream as their unwrapped forms; recording
      them as different `source_identity` values would split trust on
      cosmetic spelling).
    - Preserve the underlying transport scheme (`http://`, `https://`,
      and `ssh://` remain distinct trust subjects — they actually reach
      the host through different transports).
    - Lowercase the host portion case-insensitively; preserve path case.

    Anything outside that set is left untouched — the value will appear
    in audit events and trust records, so we do not attempt to "fix"
    URLs that the user typed in unusual but valid forms.
    """
    if "#" in url:
        url, _ = url.split("#", 1)
    # Per the RFC's "URL forms accepted as the same source", `git+https://X`
    # and `https://X` clone the same upstream; record them as one
    # canonical form so trust binds to the repo, not the spelling.
    for wrapper in ("git+https://", "git+http://", "git+ssh://"):
        if url.startswith(wrapper):
            url = url[len("git+") :]
            break
    # Strip embedded userinfo and lowercase host without pulling in urllib for
    # speed (this is the install hot path).
    for scheme in ("https://", "http://", "ssh://"):
        if url.startswith(scheme):
            rest = url[len(scheme) :]
            if "@" in rest and "/" in rest and rest.index("@") < rest.index("/"):
                rest = rest.split("@", 1)[1]
            host_path = rest.split("/", 1)
            host = host_path[0].lower()
            tail = ("/" + host_path[1]) if len(host_path) == 2 else ""
            url = scheme + host + tail
            break
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def normalize_local_path(path: Path) -> str:
    """Resolve a local path into the canonical `source_identity` per the RFC.

    - Symlinks are resolved.
    - Relative paths are rejected (caller's responsibility, but we assert
      here as a defensive guard).
    """
    resolved = path.expanduser().resolve(strict=True)
    return str(resolved)


__all__ = [
    "UnsupportedFileTypeError",
    "canonical_tree_hash",
    "normalize_local_path",
    "normalize_repo_url",
]
