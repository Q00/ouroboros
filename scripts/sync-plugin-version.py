#!/usr/bin/env python3
"""Sync plugin version fields with hatch-vcs (git tag) version.

Usage:
    python scripts/sync-plugin-version.py          # dry-run
    python scripts/sync-plugin-version.py --write  # actually update files

Called by CI (dev-publish.yml) before build to keep plugin metadata in sync.

Each target is replaced atomically. Catchable failures roll back files already
replaced, but an uncatchable process or machine crash can leave a mixed version
across targets; ordinary files cannot provide a durable multi-file transaction.
"""

import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ROOT = ROOT
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
CODEX_PLUGIN_JSON = ROOT / ".codex-plugin" / "plugin.json"
_DEFAULT_CODEX_PLUGIN_JSON = CODEX_PLUGIN_JSON
MCP_JSON = ROOT / ".claude-plugin" / ".mcp.json"
_DEFAULT_MCP_JSON = MCP_JSON
CODEX_MCP_JSON = ROOT / ".mcp.codex.json"
_DEFAULT_CODEX_MCP_JSON = CODEX_MCP_JSON
# The uvx --from requirement in the SHIPPED plugin MCP descriptors (#2066):
# pinned to the plugin version so a plugin update changes the uvx cache key
# and the served package moves with it. Both the Claude plugin descriptor
# and the Codex plugin descriptor (declared by .codex-plugin/plugin.json)
# share this pin; repository-root development launchers stay unpinned. An
# unpinned spec is accepted on read so the first sync can introduce the pin.
MCP_FROM_SPEC_RE = re.compile(r"^ouroboros-ai\[mcp\](?:==(?P<version>[0-9A-Za-z.!+]+))?$")
SETUP_SKILL_MD = ROOT / "skills" / "setup" / "SKILL.md"
BUNDLED_SETUP_SKILL_MD = ROOT / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
VERSION_MARKER_RE = re.compile(r"<!-- ooo:VERSION:([0-9A-Za-z.]+) -->")
VERSION_MARKER_ENVELOPE_RE = re.compile(r"<!-- ooo:VERSION:(.*?) -->", re.DOTALL)
_MAX_CONFLICT_RESTORE_EXCHANGES = 8
_QUARANTINE_DIR_NAME = "ouroboros-sync-plugin-version-quarantine"
_PathGeneration = tuple[int, int, int, int, int, int, int, int, int, int, str]


class _OwnedWriteError(RuntimeError):
    """Error raised after this process owns a completed pathname replacement."""

    def __init__(self, path: Path, generation: _PathGeneration, error: BaseException) -> None:
        super().__init__(str(error))
        self.path = path
        self.generation = generation
        self.__cause__ = error


def get_version() -> str:
    """Get version from hatch-vcs (same source as the Python package)."""
    # Try hatch first
    try:
        result = subprocess.run(
            ["hatch", "version"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Fallback: parse git describe like hatch-vcs does.
    # dev-publish.yml intentionally runs this script before installing hatch,
    # so this branch must preserve the same next-dev source version that the
    # subsequent hatch-vcs package build will produce.
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return version_from_git_describe(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    sys.exit("Error: cannot determine version (no hatch, no git tags)")


def version_from_git_describe(desc: str) -> str:
    """Return a hatch-vcs compatible version from ``git describe`` output."""
    normalized = desc.removeprefix("v")
    match = re.fullmatch(r"(?P<base>.+)-(?P<distance>\d+)-g[0-9a-f]+(?:-dirty)?", normalized)
    if match is None:
        return normalized
    next_version = _guess_next_dev_base(match.group("base"))
    return f"{next_version}.dev{match.group('distance')}"


def _guess_next_dev_base(version: str) -> str:
    """Approximate hatch-vcs/setuptools-scm ``guess-next-dev`` for tags."""
    match = re.fullmatch(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)", version)
    if match is not None:
        patch = int(match.group("patch")) + 1
        return f"{match.group('major')}.{match.group('minor')}.{patch}"

    prerelease = re.fullmatch(
        r"(?P<prefix>\d+\.\d+\.\d+(?P<label>a|alpha|b|beta|rc))(?P<num>\d+)",
        version,
    )
    if prerelease is not None:
        return f"{prerelease.group('prefix')}{int(prerelease.group('num')) + 1}"

    return version


def normalize_version(v: str) -> str:
    """Normalize version for plugin metadata.

    Keeps pre-release tags (alpha/beta/rc) but strips dev suffixes.
    e.g. 0.26.0b4 -> 0.26.0b4, 0.26.0.dev3 -> 0.26.0, 0.26.0b4.dev1 -> 0.26.0b4
    """
    # Match semver + optional pre-release (a/alpha/b/beta/rc + number)
    # and an optional hatch-vcs development suffix.
    match = re.fullmatch(
        r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
        r"(?:(?P<label>a|alpha|b|beta|rc)(?P<pre>[0-9]*))?"
        r"(?:\.dev[0-9]+)?",
        v,
    )
    if match is None:
        raise ValueError(f"unsupported version: {v}")
    public = ".".join(str(int(match.group(name))) for name in ("major", "minor", "patch"))
    label = match.group("label")
    if label is None:
        return public
    canonical_label = {"alpha": "a", "beta": "b"}.get(label, label)
    prerelease = int(match.group("pre") or "0")
    return f"{public}{canonical_label}{prerelease}"


def require_canonical_version(v: str) -> str:
    normalized = normalize_version(v)
    if normalized != v:
        raise ValueError(f"release version must be canonical: {v} (canonical: {normalized})")
    return normalized


def _read_version_marker(text: str, path: Path) -> str:
    """Return one well-formed marker value, rejecting malformed duplicates."""
    if text.count("<!-- ooo:VERSION:") != 1:
        raise ValueError(f"expected exactly one version marker in {path}")
    envelopes = list(VERSION_MARKER_ENVELOPE_RE.finditer(text))
    matches = list(VERSION_MARKER_RE.finditer(text))
    if len(envelopes) != 1 or len(matches) != 1 or envelopes[0].span() != matches[0].span():
        raise ValueError(f"expected exactly one version marker in {path}")
    marker_value = matches[0].group(1)
    try:
        normalized_marker = normalize_version(marker_value)
    except ValueError as exc:
        raise ValueError(f"expected exactly one version marker in {path}") from exc
    if normalized_marker != marker_value:
        raise ValueError(f"expected exactly one version marker in {path}")
    return marker_value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-RFC JSON constant: {value}")


def _parse_json_bytes(content: bytes) -> object:
    return json.loads(
        content.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )


def _load_json(path: Path) -> object:
    return _parse_json_bytes(path.read_bytes())


def _exchange_paths(source: Path, destination: Path) -> bool:
    """Atomically exchange two pathnames when the host filesystem supports it."""
    if sys.platform.startswith("linux"):
        function_name = "renameat2"
        at_fdcwd = -100
    elif sys.platform == "darwin":
        function_name = "renameatx_np"
        at_fdcwd = -2
    else:
        return False
    try:
        exchange = getattr(ctypes.CDLL(None, use_errno=True), function_name)
    except AttributeError:
        return False
    exchange.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    exchange.restype = ctypes.c_int
    result = exchange(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        0x00000002,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
        return False
    raise OSError(error, os.strerror(error), destination)


def _path_generation(path: Path) -> _PathGeneration:
    """Return the pathname identity and mutable inode generation fields."""
    metadata = path.stat(follow_symlinks=False)
    # Path exchange itself updates ctime on APFS, so ctime cannot distinguish
    # an external writer from the ownership transfer being validated here.
    content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        int(getattr(metadata, "st_flags", 0)),
        int(getattr(metadata, "st_gen", 0)),
        content_digest,
    )


def _git_directory() -> Path | None:
    marker = ROOT / ".git"
    if marker.is_dir():
        git_dir = marker
    else:
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        prefix = "gitdir:"
        if not value.startswith(prefix):
            return None
        git_dir = Path(value.removeprefix(prefix).strip())
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
    try:
        common_value = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return git_dir
    common_dir = Path(common_value)
    return common_dir if common_dir.is_absolute() else (git_dir / common_dir).resolve()


def _quarantine_directory(path: Path) -> Path:
    """Return a writable quarantine directory on the target filesystem."""
    target_device = path.parent.stat().st_dev
    git_dir = _git_directory()
    candidates: list[Path] = []
    if git_dir is not None:
        candidates.append(git_dir / _QUARANTINE_DIR_NAME)
    candidates.extend(
        [
            ROOT / f".{_QUARANTINE_DIR_NAME}",
            path.parent / f".{_QUARANTINE_DIR_NAME}",
        ]
    )
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.parent.stat().st_dev != target_device:
                continue
            candidate.mkdir(parents=True, exist_ok=True)
            if candidate.stat().st_dev == target_device:
                return candidate
        except OSError:
            continue
    raise RuntimeError(f"could not create same-filesystem write quarantine for {path}")


def _quarantine_displaced_path(temp_path: Path, path: Path) -> Path:
    """Move a displaced inode to durable storage instead of unlinking it."""
    # Quarantines persist because an uncooperative pre-exchange descriptor has
    # no observable point at which it is safe to delete the displaced inode.
    quarantine_dir = _quarantine_directory(path)
    fd, quarantine_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".displaced",
        dir=quarantine_dir,
    )
    os.close(fd)
    quarantine_path = Path(quarantine_name)
    try:
        os.replace(temp_path, quarantine_path)
    except BaseException:
        quarantine_path.unlink(missing_ok=True)
        raise
    return quarantine_path


def _restore_latest_exchanged_content(
    temp_path: Path,
    path: Path,
    *,
    expected_displaced: _PathGeneration,
) -> bool:
    """Restore the newest observed external edit without deleting a later writer."""
    for _ in range(_MAX_CONFLICT_RESTORE_EXCHANGES):
        candidate = _path_generation(temp_path)
        if not _exchange_paths(temp_path, path):
            return False
        displaced = _path_generation(temp_path)
        if displaced == expected_displaced:
            return True
        expected_displaced = candidate
    return False


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    expected_current: bytes | None = None,
    expected_generation: _PathGeneration | None = None,
) -> _PathGeneration:
    """Durably replace one target without exposing a partial file."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    preserve_temp = False
    replaced_target = False
    quarantine_path: Path | None = None
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fchmod(temp_file.fileno(), mode)
            os.fsync(temp_file.fileno())
        staged_generation = _path_generation(temp_path)
        if expected_current is not None:
            if not path.exists():
                raise RuntimeError(f"write conflict for {path}: file changed since preflight")
            if not _exchange_paths(temp_path, path):
                raise RuntimeError(
                    f"write conflict for {path}: atomic path exchange is unavailable"
                )
            replaced_target = True
            displaced_generation = _path_generation(temp_path)
            if temp_path.read_bytes() != expected_current or (
                expected_generation is not None and displaced_generation != expected_generation
            ):
                replaced_target = False
                preserve_temp = True
                try:
                    restored = _restore_latest_exchanged_content(
                        temp_path,
                        path,
                        expected_displaced=staged_generation,
                    )
                except BaseException:
                    raise
                if not restored:
                    raise RuntimeError(
                        f"write conflict for {path}: could not restore exchanged target; "
                        f"preserved displaced content at {temp_path}"
                    )
                raise RuntimeError(
                    f"write conflict for {path}: file changed since preflight; "
                    f"preserved exchanged content at {temp_path}"
                )
            preserve_temp = True
            quarantine_path = _quarantine_displaced_path(temp_path, path)
        else:
            os.replace(temp_path, path)
            replaced_target = True
        directories = [path.parent]
        if quarantine_path is not None and quarantine_path.parent != path.parent:
            directories.append(quarantine_path.parent)
        for directory in directories:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return staged_generation
    except BaseException as exc:
        if replaced_target and not isinstance(exc, _OwnedWriteError):
            raise _OwnedWriteError(path, staged_generation, exc) from exc
        raise
    finally:
        if not preserve_temp:
            active_error = sys.exception()
            try:
                temp_path.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                if active_error is not None:
                    active_error.add_note(
                        f"also failed to remove temporary file {temp_path}: {cleanup_error}"
                    )
                elif replaced_target:
                    raise _OwnedWriteError(
                        path, staged_generation, cleanup_error
                    ) from cleanup_error
                else:
                    raise


def update_version_marker(
    path: Path,
    version: str,
    *,
    expected_current: bytes | None = None,
    expected_generation: _PathGeneration | None = None,
) -> _PathGeneration | None:
    """Update <!-- ooo:VERSION:X.Y.Z --> marker in a text file."""
    original = expected_current if expected_current is not None else path.read_bytes()
    text = original.decode("utf-8")
    _read_version_marker(text, path)

    old_marker = VERSION_MARKER_RE.search(text)
    assert old_marker is not None
    old_marker_bytes = old_marker.group(0).encode("ascii")
    new_marker_bytes = f"<!-- ooo:VERSION:{version} -->".encode("ascii")
    content = original.replace(old_marker_bytes, new_marker_bytes, 1)
    if original == content:
        return None
    owned_generation = _atomic_write_bytes(
        path,
        content,
        expected_current=original,
        expected_generation=expected_generation,
    )

    try:
        updated = path.read_bytes()
        if updated != content:
            raise RuntimeError(f"failed to verify version marker update in {path}")
        updated_matches = list(VERSION_MARKER_RE.finditer(updated.decode("utf-8")))
        if len(updated_matches) != 1 or updated_matches[0].group(1) != version:
            raise RuntimeError(f"failed to verify version marker update in {path}")
    except BaseException as exc:
        raise _OwnedWriteError(path, owned_generation, exc) from exc
    return owned_generation


def update_json(
    path: Path,
    version: str,
    *,
    nested_key: str | None = None,
    expected_current: bytes | None = None,
    expected_generation: _PathGeneration | None = None,
) -> _PathGeneration | None:
    """Update version in a JSON file. Returns the owned generation if changed."""
    original = expected_current if expected_current is not None else path.read_bytes()
    data = _parse_json_bytes(original)
    if not isinstance(data, dict):
        raise TypeError("top-level JSON value must be an object")

    if nested_key:
        # marketplace.json: plugins[0].version
        target = data
        for key in nested_key.split("."):
            if key.isdigit():
                target = target[int(key)]
            else:
                target = target[key]
        old = target.get("version")
        target["version"] = version
    else:
        old = data.get("version")
        data["version"] = version

    if old == version:
        return None

    content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return _atomic_write_bytes(
        path,
        content,
        expected_current=original,
        expected_generation=expected_generation,
    )


def _read_mcp_from_spec(data: object) -> tuple[int, str]:
    """Locate the single uvx ``--from`` requirement for ``ouroboros-ai[mcp]``.

    Returns the args index of the requirement and its current version pin
    (empty string when unpinned). Raises ``ValueError`` on any shape that
    would make the rewrite ambiguous.
    """
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value must be an object")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")
    server = servers.get("ouroboros")
    if not isinstance(server, dict):
        raise ValueError("mcpServers.ouroboros must be an object")
    args = server.get("args")
    if not isinstance(args, list):
        raise ValueError("mcpServers.ouroboros.args must be an array")
    from_positions = [
        index
        for index, arg in enumerate(args)
        if arg == "--from" or (isinstance(arg, str) and arg.startswith("--from="))
    ]
    if len(from_positions) != 1:
        raise ValueError("expected exactly one --from option")
    positions = [
        index + 1
        for index, arg in enumerate(args[:-1])
        if arg == "--from"
        and isinstance(args[index + 1], str)
        and args[index + 1].startswith("ouroboros-ai[mcp]")
    ]
    if len(positions) != 1:
        raise ValueError("expected exactly one --from ouroboros-ai[mcp] requirement")
    spec = args[positions[0]]
    match = MCP_FROM_SPEC_RE.match(spec)
    if match is None:
        raise ValueError(f"unrecognized ouroboros-ai[mcp] requirement: {spec}")
    return positions[0], match.group("version") or ""


def _mcp_pinned_content(original: bytes, version: str) -> tuple[bytes, str]:
    """Return the pinned ``.mcp.json`` bytes and the old pin for *version*."""
    data = _parse_json_bytes(original)
    index, old_pin = _read_mcp_from_spec(data)
    assert isinstance(data, dict)  # _read_mcp_from_spec validated the shape
    data["mcpServers"]["ouroboros"]["args"][index] = f"ouroboros-ai[mcp]=={version}"
    content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return content, old_pin


def update_mcp_from_pin(
    path: Path,
    version: str,
    *,
    expected_current: bytes | None = None,
    expected_generation: _PathGeneration | None = None,
) -> _PathGeneration | None:
    """Pin the plugin MCP server requirement to *version* (#2066)."""
    original = expected_current if expected_current is not None else path.read_bytes()
    content, _old_pin = _mcp_pinned_content(original, version)
    if original == content:
        return None
    owned_generation = _atomic_write_bytes(
        path,
        content,
        expected_current=original,
        expected_generation=expected_generation,
    )
    try:
        updated = path.read_bytes()
        if updated != content:
            raise RuntimeError(f"failed to verify MCP requirement pin in {path}")
        _index, new_pin = _read_mcp_from_spec(_parse_json_bytes(updated))
        if new_pin != version:
            raise RuntimeError(f"failed to verify MCP requirement pin in {path}")
    except BaseException as exc:
        raise _OwnedWriteError(path, owned_generation, exc) from exc
    return owned_generation


def _run() -> None:
    write = "--write" in sys.argv

    # Allow explicit version override (e.g. --version 0.26.0b6)
    # Used by the release process to sync BEFORE tagging.
    explicit_version = None
    for i, arg in enumerate(sys.argv):
        if arg == "--version" and i + 1 < len(sys.argv):
            explicit_version = sys.argv[i + 1]

    raw_version = explicit_version or get_version()
    try:
        if "--require-canonical" in sys.argv:
            version = require_canonical_version(raw_version)
        else:
            version = normalize_version(raw_version)
    except ValueError as exc:
        sys.exit(f"Error: {exc}")

    print(f"Source version: {raw_version}")
    print(f"Plugin version: {version}")
    print()

    targets = [
        (PLUGIN_JSON, None),
        (MARKETPLACE_JSON, "plugins.0"),
    ]
    codex_plugin_json = (
        ROOT / ".codex-plugin" / "plugin.json"
        if CODEX_PLUGIN_JSON == _DEFAULT_CODEX_PLUGIN_JSON and ROOT != _DEFAULT_ROOT
        else CODEX_PLUGIN_JSON
    )
    if codex_plugin_json.exists():
        targets.append((codex_plugin_json, None))
    originals: dict[Path, bytes] = {}
    original_generations: dict[Path, _PathGeneration] = {}
    setup_markers: dict[Path, tuple[str, str]] = {}
    for path in (SETUP_SKILL_MD, BUNDLED_SETUP_SKILL_MD):
        if not path.exists():
            sys.exit(f"Error: required setup skill not found: {path.relative_to(ROOT)}")
        original_generations[path] = _path_generation(path)
        original = path.read_bytes()
        originals[path] = original
        text = original.decode("utf-8")
        try:
            old_marker = _read_version_marker(text, path)
        except ValueError:
            sys.exit(f"Error: expected exactly one version marker in {path.relative_to(ROOT)}")
        setup_markers[path] = (text, old_marker)

    json_targets: list[tuple[Path, str | None, object, str]] = []
    expected_writes: dict[Path, bytes] = {}
    for path, nested in targets:
        if not path.exists():
            sys.exit(f"Error: required plugin metadata not found: {path.relative_to(ROOT)}")

        try:
            original_generations[path] = _path_generation(path)
            original = path.read_bytes()
            originals[path] = original
            data = _parse_json_bytes(original)
            if not isinstance(data, dict):
                raise TypeError("top-level JSON value must be an object")
            target: object = data
            if nested:
                for key in nested.split("."):
                    if key.isdigit():
                        if not isinstance(target, list):
                            raise TypeError("numeric path component requires an array")
                        target = target[int(key)]
                    else:
                        if not isinstance(target, dict):
                            raise TypeError("named path component requires an object")
                        target = target[key]
            if not isinstance(target, dict):
                raise TypeError("version target must be an object")
            old = target.get("version")
            if not isinstance(old, str):
                raise TypeError("version must be a string")
            target["version"] = version
            expected_writes[path] = (
                original
                if old == version
                else (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            sys.exit(f"Error: could not validate {path.relative_to(ROOT)}: {exc}")
        json_targets.append((path, nested, data, old))

    # The shipped plugin MCP descriptors pin the served package to the plugin
    # version (#2066). Optional like the Codex manifest so relocated test
    # roots without a descriptor skip it; the real repository has both.
    descriptor_candidates = (
        ROOT / ".claude-plugin" / ".mcp.json"
        if MCP_JSON == _DEFAULT_MCP_JSON and ROOT != _DEFAULT_ROOT
        else MCP_JSON,
        ROOT / ".mcp.codex.json"
        if CODEX_MCP_JSON == _DEFAULT_CODEX_MCP_JSON and ROOT != _DEFAULT_ROOT
        else CODEX_MCP_JSON,
    )
    mcp_descriptors: list[tuple[Path, str]] = []
    for descriptor in descriptor_candidates:
        if not descriptor.exists():
            continue
        try:
            original_generations[descriptor] = _path_generation(descriptor)
            original = descriptor.read_bytes()
            originals[descriptor] = original
            pinned_content, old_pin = _mcp_pinned_content(original, version)
            expected_writes[descriptor] = original if old_pin == version else pinned_content
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            sys.exit(f"Error: could not validate {descriptor.relative_to(ROOT)}: {exc}")
        mcp_descriptors.append((descriptor, old_pin))

    changed = False
    for path, (_text, old_marker) in setup_markers.items():
        expected_writes[path] = originals[path].replace(
            f"<!-- ooo:VERSION:{old_marker} -->".encode("ascii"),
            f"<!-- ooo:VERSION:{version} -->".encode("ascii"),
            1,
        )
    attempted: list[Path] = []
    owned_writes: dict[Path, _PathGeneration] = {}
    try:
        for path, nested, _data, old in json_targets:
            if old == version:
                print(f"  OK    {path.relative_to(ROOT)} ({old})")
            elif write:
                attempted.append(path)
                owned_generation = update_json(
                    path,
                    version,
                    nested_key=nested,
                    expected_current=originals[path],
                    expected_generation=original_generations[path],
                )
                if owned_generation is None:
                    sys.exit(f"Error: failed to update {path.relative_to(ROOT)}")
                owned_writes[path] = owned_generation
                print(f"  WRITE {path.relative_to(ROOT)} ({old} -> {version})")
                changed = True
            else:
                print(f"  DRIFT {path.relative_to(ROOT)} ({old} != {version})")
                changed = True

        for descriptor, old_pin in mcp_descriptors:
            shown_pin = old_pin or "unpinned"
            if old_pin == version:
                print(f"  OK    {descriptor.relative_to(ROOT)} ({shown_pin})")
            elif write:
                attempted.append(descriptor)
                owned_generation = update_mcp_from_pin(
                    descriptor,
                    version,
                    expected_current=originals[descriptor],
                    expected_generation=original_generations[descriptor],
                )
                if owned_generation is None:
                    sys.exit(f"Error: failed to update {descriptor.relative_to(ROOT)}")
                owned_writes[descriptor] = owned_generation
                print(f"  WRITE {descriptor.relative_to(ROOT)} ({shown_pin} -> {version})")
                changed = True
            else:
                print(f"  DRIFT {descriptor.relative_to(ROOT)} ({shown_pin} != {version})")
                changed = True

        for path, (_text, old_marker) in setup_markers.items():
            if old_marker == version:
                print(f"  OK    {path.relative_to(ROOT)} ({old_marker})")
            elif write:
                attempted.append(path)
                owned_generation = update_version_marker(
                    path,
                    version,
                    expected_current=originals[path],
                    expected_generation=original_generations[path],
                )
                if owned_generation is None:
                    sys.exit(f"Error: failed to update {path.relative_to(ROOT)}")
                owned_writes[path] = owned_generation
                print(f"  WRITE {path.relative_to(ROOT)} ({old_marker} -> {version})")
                changed = True
            else:
                print(f"  DRIFT {path.relative_to(ROOT)} ({old_marker} != {version})")
                changed = True

        final_expectations = expected_writes if write else originals
        for path, expected in final_expectations.items():
            if path.read_bytes() != expected:
                phase = "post-write" if write else "validation"
                raise RuntimeError(
                    f"{phase} conflict for {path}: final content differs from preflight plan"
                )
    except BaseException as primary_error:
        if not write:
            raise
        if isinstance(primary_error, _OwnedWriteError):
            owned_writes[primary_error.path] = primary_error.generation
        rollback_failures: list[BaseException] = []
        for path in attempted:
            try:
                current = path.read_bytes()
                if current == originals[path]:
                    continue
                if current != expected_writes[path]:
                    raise RuntimeError(
                        f"rollback conflict for {path}: file changed after version sync write"
                    )
                if _path_generation(path) != owned_writes.get(path):
                    raise RuntimeError(
                        f"rollback conflict for {path}: file generation changed after "
                        "version sync write"
                    )
                _atomic_write_bytes(
                    path,
                    originals[path],
                    expected_current=expected_writes[path],
                    expected_generation=owned_writes[path],
                )
            except BaseException as rollback_error:
                failure = RuntimeError(f"failed to roll back {path}: {rollback_error}")
                failure.__cause__ = rollback_error
                rollback_failures.append(failure)
        if rollback_failures:
            raise BaseExceptionGroup(
                "version synchronization failed and rollback was incomplete",
                [primary_error, *rollback_failures],
            ) from None
        raise

    if changed and not write:
        print("\nRun with --write to update files.")
        sys.exit(1)


def _lock_path() -> Path:
    root_key = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"ouroboros-sync-plugin-version-{root_key}.lock"


def main() -> None:
    """Serialize invocations; the OS releases this lock if the process crashes."""
    lock_path = _lock_path()
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        _run()


if __name__ == "__main__":
    main()
