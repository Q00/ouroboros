"""Fail-closed executable version attestation for CLI runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import subprocess


class CliExecutableVersionState(StrEnum):
    """Outcome of probing or comparing one CLI version attestation.

    Probe unavailability is deliberately not represented by ``None``. A
    timeout and an execution failure have different operational meaning, and
    neither is positive evidence that an executable stayed the same. The
    ``CHANGED`` state is produced only by comparing two successful probes.
    """

    VERIFIED = "verified"
    TIMED_OUT = "timed_out"
    EXECUTION_FAILED = "execution_failed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class CliExecutableVersionAttestation:
    """Structured version evidence for the selected CLI executable."""

    state: CliExecutableVersionState
    identity: str | None = None
    filesystem_identity: tuple[int, int] | None = None


type IdentityReader = Callable[[], object]
type FilesystemIdentityReader = Callable[[], tuple[int, int] | None]
type HashPayload = Callable[[object], str]


def read_cli_executable_filesystem_identity(
    executable_path: str | None,
) -> tuple[int, int] | None:
    """Return the effective executable target's device/inode pair."""
    if executable_path is None:
        return None
    try:
        value = Path(executable_path).stat()
    except OSError:
        return None
    return value.st_dev, value.st_ino


def read_cli_executable_generation_identity(
    executable_path: str | None,
) -> tuple[int, int, int, int, int] | None:
    """Return target metadata that exposes same-inode mutate/restore ABA."""
    if executable_path is None:
        return None
    try:
        value = Path(executable_path).stat()
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_cli_executable_symlink_identity(executable_path: str | None) -> dict[str, str] | None:
    """Return launch-path symlink target identity without dereferencing it."""
    if executable_path is None:
        return None
    path = Path(executable_path)
    try:
        if not path.is_symlink():
            return None
        raw_target = os.readlink(path)
    except OSError:
        return None
    target_path = Path(raw_target)
    if not target_path.is_absolute():
        target_path = path.parent / target_path
    return {
        "raw_target": raw_target,
        "resolved_target": str(target_path.expanduser().absolute()),
    }


def read_cli_executable_symlink_generation_identity(
    executable_path: str | None,
) -> tuple[int, int, int, int, int] | None:
    """Return launch-symlink metadata, or ``None`` for a direct path."""
    if executable_path is None:
        return None
    path = Path(executable_path)
    try:
        if not path.is_symlink():
            return None
        value = path.lstat()
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_cli_executable_content_identity(executable_path: str | None) -> str | None:
    """Return the selected CLI byte digest without executing it."""
    if executable_path is None:
        return None
    try:
        executable_bytes = Path(executable_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(executable_bytes).hexdigest()


def probe_cli_executable_version_attestation(
    executable_path: str | None,
    *,
    filesystem_identity: FilesystemIdentityReader,
    filesystem_generation_identity: IdentityReader,
    content_identity: IdentityReader,
    symlink_identity: IdentityReader,
    symlink_generation_identity: IdentityReader,
    hash_payload: HashPayload,
) -> CliExecutableVersionAttestation:
    """Probe one executable generation and reject mixed or ABA evidence.

    The version process spans multiple syscalls. Positive evidence is emitted
    only when the effective target, bytes, and launch symlink are unchanged
    before and after the process. Generation identities include ctime, which
    makes an in-probe mutate-and-restore (ABA) visible even when the final
    bytes, symlink text, device, and inode equal their initial values.
    """
    failed = CliExecutableVersionAttestation(CliExecutableVersionState.EXECUTION_FAILED)
    if executable_path is None:
        return failed

    before_filesystem = filesystem_identity()
    before_generation = filesystem_generation_identity()
    before_content = content_identity()
    before_symlink = symlink_identity()
    before_symlink_generation = symlink_generation_identity()
    if before_filesystem is None or before_generation is None or before_content is None:
        return failed

    try:
        result = subprocess.run(
            [executable_path, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CliExecutableVersionAttestation(CliExecutableVersionState.TIMED_OUT)
    except (OSError, UnicodeError):
        return failed

    version_output = (result.stdout or result.stderr).strip()
    if result.returncode != 0 or not version_output:
        return failed

    # Sample in reverse order after the process so every component is bounded
    # by generation checks. This rejects same-inode writes, symlink retargets,
    # atomic replacement, and mutate/restore ABA during the probe window.
    after_symlink_generation = symlink_generation_identity()
    after_symlink = symlink_identity()
    after_content = content_identity()
    after_generation = filesystem_generation_identity()
    after_filesystem = filesystem_identity()
    if (
        after_filesystem != before_filesystem
        or after_generation != before_generation
        or after_content != before_content
        or after_symlink != before_symlink
        or after_symlink_generation != before_symlink_generation
    ):
        return failed

    device, inode = before_filesystem
    return CliExecutableVersionAttestation(
        CliExecutableVersionState.VERIFIED,
        hash_payload(
            {
                "content_sha256": before_content,
                "filesystem": {"device": device, "inode": inode},
                "symlink": before_symlink,
                "version_output": version_output,
            }
        ),
        (device, inode),
    )


def compare_cli_executable_version_attestations(
    initialized: CliExecutableVersionAttestation,
    current: CliExecutableVersionAttestation,
) -> CliExecutableVersionState:
    """Compare positive evidence without equating two missing probes."""
    if initialized.state is not CliExecutableVersionState.VERIFIED:
        return initialized.state
    if current.state is not CliExecutableVersionState.VERIFIED:
        return current.state
    if (
        initialized.identity is None
        or current.identity is None
        or initialized.filesystem_identity is None
        or current.filesystem_identity is None
    ):
        return CliExecutableVersionState.EXECUTION_FAILED
    if initialized.filesystem_identity != current.filesystem_identity:
        return CliExecutableVersionState.CHANGED
    if initialized.identity == current.identity:
        return CliExecutableVersionState.VERIFIED
    return CliExecutableVersionState.CHANGED


def require_unchanged_cli_version_attestation(
    display_name: str,
    initialized: CliExecutableVersionAttestation | None,
    current_probe: Callable[[], CliExecutableVersionAttestation],
) -> None:
    """Apply the shared initialization/check-time fail-closed policy."""
    if initialized is None:
        raise RuntimeError(
            f"{display_name} version attestation was not captured during "
            "runtime initialization; start a new execution session"
        )
    if initialized.state is CliExecutableVersionState.TIMED_OUT:
        raise RuntimeError(
            f"{display_name} version attestation timed out during runtime "
            "initialization; execution is blocked without claiming executable drift; "
            "start a new execution session"
        )
    if initialized.state is CliExecutableVersionState.EXECUTION_FAILED:
        raise RuntimeError(
            f"{display_name} version attestation failed during runtime "
            "initialization; execution is blocked without claiming executable drift; "
            "start a new execution session"
        )

    comparison = compare_cli_executable_version_attestations(initialized, current_probe())
    if comparison is CliExecutableVersionState.VERIFIED:
        return
    if comparison is CliExecutableVersionState.TIMED_OUT:
        raise RuntimeError(
            f"{display_name} version attestation timed out while verifying the "
            "executable; execution is blocked without claiming executable drift; retry "
            "the execution or start a new execution session"
        )
    if comparison is CliExecutableVersionState.EXECUTION_FAILED:
        raise RuntimeError(
            f"{display_name} version attestation failed while verifying the "
            "executable; execution is blocked without claiming executable drift; retry "
            "the execution or start a new execution session"
        )
    raise RuntimeError(
        f"{display_name} executable version changed after runtime initialization; "
        "start a new execution session"
    )
