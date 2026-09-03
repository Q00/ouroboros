"""Fail-closed executable version attestation for CLI runtimes."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import stat
import subprocess


class CliExecutableVersionState(StrEnum):
    """Outcome of probing or comparing one CLI version attestation.

    Probe unavailability is deliberately not represented by ``None``. A
    timeout, execution failure, and indeterminate authority evidence have
    different operational meaning, and none is positive evidence that an
    executable stayed the same. ``CHANGED`` is reserved for evidence tied to
    the executable or its semantic symlink chain, not broad parent-directory
    generation churn that could have come from an unrelated sibling.
    """

    VERIFIED = "verified"
    TIMED_OUT = "timed_out"
    EXECUTION_FAILED = "execution_failed"
    INDETERMINATE = "indeterminate"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class CliExecutableVersionAttestation:
    """Structured version evidence for the selected CLI executable."""

    state: CliExecutableVersionState
    identity: str | None = None
    filesystem_identity: tuple[int, int] | None = None
    nonexecuting_identity: str | None = None


type IdentityReader = Callable[[], object]
type FilesystemIdentityReader = Callable[[], tuple[int, int] | None]
type HashPayload = Callable[[object], str]


def _only_parent_generations_changed(
    before: object,
    after: object,
) -> bool:
    """Return whether resolution evidence differs only in parent generations.

    A containing directory's generation changes for both authority-entry ABA
    and unrelated sibling churn. It is therefore useful fail-closed evidence,
    but cannot truthfully prove that the executable itself changed.
    """
    if not isinstance(before, tuple) or not isinstance(after, tuple) or before == after:
        return False
    if len(before) != len(after):
        return False
    for before_entry, after_entry in zip(before, after, strict=True):
        if (
            not isinstance(before_entry, tuple)
            or not isinstance(after_entry, tuple)
            or len(before_entry) != 4
            or len(after_entry) != 4
            or (before_entry[0], before_entry[1], before_entry[3])
            != (after_entry[0], after_entry[1], after_entry[3])
        ):
            return False
    return True


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


def _stat_generation(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def read_cli_executable_resolution_chain_identity(
    executable_path: str | None,
) -> tuple[tuple[object, ...], ...] | None:
    """Resolve every path/symlink hop with generation-bearing evidence.

    The terminal executable and every visited symlink are paired with their
    containing directory generation. That makes an atomic swap-and-restore of
    an authority-bearing entry visible even when its original inode is put
    back unchanged, without treating unrelated churn in ordinary ancestors as
    executable churn. Resolution is lexical and bounded to 40 symlink
    traversals; loops and read errors fail closed.
    """
    if executable_path is None:
        return None
    absolute_path = Path(os.path.abspath(os.path.expanduser(executable_path)))
    pending = deque(absolute_path.parts[1:])
    resolved_parent = Path(absolute_path.anchor)
    evidence: list[tuple[object, ...]] = []
    symlink_count = 0

    try:
        while pending:
            component = pending.popleft()
            if component in {"", "."}:
                continue
            if component == "..":
                resolved_parent = resolved_parent.parent
                continue

            candidate = resolved_parent / component
            parent_generation = _stat_generation(resolved_parent.stat())
            node_stat = candidate.lstat()
            raw_target: str | None = None
            if stat.S_ISLNK(node_stat.st_mode):
                symlink_count += 1
                if symlink_count > 40:
                    return None
                raw_target = os.readlink(candidate)
            if raw_target is not None or not pending:
                evidence.append(
                    (
                        str(candidate),
                        raw_target,
                        parent_generation,
                        _stat_generation(node_stat),
                    )
                )

            if raw_target is None:
                resolved_parent = candidate
                continue

            target = Path(raw_target)
            if target.is_absolute():
                resolved_parent = Path(target.anchor)
                target_parts = target.parts[1:]
            else:
                target_parts = target.parts
            pending.extendleft(reversed(target_parts))
    except OSError:
        return None
    return tuple(evidence)


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
    initialized: CliExecutableVersionAttestation | None,
    filesystem_identity: FilesystemIdentityReader,
    resolution_chain_identity: IdentityReader,
    content_identity: IdentityReader,
    hash_payload: HashPayload,
) -> CliExecutableVersionAttestation:
    """Probe one executable generation and reject mixed or ABA evidence.

    The version process spans multiple syscalls. Positive evidence is emitted
    only when the effective target, bytes, and complete symlink chain are
    unchanged before and after the process. Generation identities include
    ctime and containing-directory state, making an in-probe swap-and-restore
    visible even when final bytes, symlink text, device, and inode match.
    """
    failed = CliExecutableVersionAttestation(CliExecutableVersionState.EXECUTION_FAILED)
    if executable_path is None:
        return failed

    before_filesystem = filesystem_identity()
    before_resolution_chain = resolution_chain_identity()
    before_content = content_identity()
    before_symlink = read_cli_executable_symlink_identity(executable_path)
    if before_filesystem is None or before_resolution_chain is None or before_content is None:
        return failed

    device, inode = before_filesystem
    semantic_symlink_chain = tuple(
        (entry[0], entry[1]) for entry in before_resolution_chain if entry[1] is not None
    )
    nonexecuting_chain = tuple((entry[0], entry[1], entry[3]) for entry in before_resolution_chain)
    nonexecuting_identity = hash_payload(
        {
            "content_sha256": before_content,
            "executable_path": executable_path,
            "filesystem": {"device": device, "inode": inode},
            "resolution_chain": nonexecuting_chain,
            "symlink_chain": semantic_symlink_chain,
        }
    )
    if initialized is not None:
        if (
            initialized.state is not CliExecutableVersionState.VERIFIED
            or initialized.nonexecuting_identity is None
        ):
            return failed
        if initialized.nonexecuting_identity != nonexecuting_identity:
            return CliExecutableVersionAttestation(
                CliExecutableVersionState.CHANGED,
                filesystem_identity=before_filesystem,
                nonexecuting_identity=nonexecuting_identity,
            )

    probe_state = CliExecutableVersionState.VERIFIED
    version_output: str | None = None
    try:
        result = subprocess.run(
            [executable_path, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        probe_state = CliExecutableVersionState.TIMED_OUT
    except (OSError, UnicodeError):
        probe_state = CliExecutableVersionState.EXECUTION_FAILED
    else:
        version_output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 or not version_output:
            probe_state = CliExecutableVersionState.EXECUTION_FAILED

    # Sample in reverse order after the process so authority-bearing entries
    # are bounded by generation checks. This rejects same-inode writes,
    # symlink retargets, atomic replacement, and probe-window ABA.
    after_symlink = read_cli_executable_symlink_identity(executable_path)
    after_content = content_identity()
    after_resolution_chain = resolution_chain_identity()
    after_filesystem = filesystem_identity()
    authority_changed = (
        after_filesystem != before_filesystem
        or after_content != before_content
        or after_symlink != before_symlink
    )
    resolution_changed = after_resolution_chain != before_resolution_chain
    if authority_changed or resolution_changed:
        state = CliExecutableVersionState.CHANGED
        if not authority_changed and _only_parent_generations_changed(
            before_resolution_chain,
            after_resolution_chain,
        ):
            state = CliExecutableVersionState.INDETERMINATE
        return CliExecutableVersionAttestation(
            state,
            filesystem_identity=before_filesystem,
            nonexecuting_identity=nonexecuting_identity,
        )
    if probe_state is not CliExecutableVersionState.VERIFIED:
        return CliExecutableVersionAttestation(
            probe_state,
            filesystem_identity=before_filesystem,
            nonexecuting_identity=nonexecuting_identity,
        )

    assert version_output is not None
    return CliExecutableVersionAttestation(
        CliExecutableVersionState.VERIFIED,
        hash_payload(
            {
                "content_sha256": before_content,
                "filesystem": {"device": device, "inode": inode},
                "symlink": before_symlink,
                "symlink_chain": semantic_symlink_chain,
                "version_output": version_output,
            }
        ),
        (device, inode),
        nonexecuting_identity,
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
        or initialized.nonexecuting_identity is None
        or current.nonexecuting_identity is None
    ):
        return CliExecutableVersionState.EXECUTION_FAILED
    if initialized.filesystem_identity != current.filesystem_identity:
        return CliExecutableVersionState.CHANGED
    if initialized.nonexecuting_identity != current.nonexecuting_identity:
        return CliExecutableVersionState.CHANGED
    if initialized.identity == current.identity:
        return CliExecutableVersionState.VERIFIED
    return CliExecutableVersionState.CHANGED


def verify_cli_executable_identity_unchanged(
    *,
    display_name: str,
    cli_path: str,
    codex_native: bool,
    path_identity_snapshot: str | None,
    content_identity_snapshot: str | None,
    version_attestation_snapshot: CliExecutableVersionAttestation | None,
    current_path_identity: Callable[[], str | None],
    current_content_identity: Callable[[], str | None],
    current_version_attestation: Callable[
        [CliExecutableVersionAttestation | None], CliExecutableVersionAttestation
    ],
) -> None:
    """Raise on executable drift or unavailable version-attestation evidence.

    Pure policy over the runtime's initialization snapshots; the runtime
    decides what to do with the error (observe and re-attest).
    """
    if path_identity_snapshot is None:
        cli_candidate = Path(cli_path).expanduser()
        if not cli_candidate.is_absolute():
            if codex_native:
                raise RuntimeError(
                    "Codex CLI executable was unresolved at runtime initialization; "
                    "start a new execution session"
                )
        elif cli_candidate.exists():
            raise RuntimeError(
                f"{display_name} executable appeared after runtime initialization; "
                "start a new execution session"
            )
        require_unchanged_cli_version_attestation(
            display_name,
            version_attestation_snapshot,
            lambda: current_version_attestation(None),
        )
        return
    if current_path_identity() != path_identity_snapshot:
        raise RuntimeError(
            f"{display_name} executable changed after runtime initialization; "
            "start a new execution session"
        )
    if current_content_identity() != content_identity_snapshot:
        raise RuntimeError(
            f"{display_name} executable changed after runtime initialization; "
            "start a new execution session"
        )
    require_unchanged_cli_version_attestation(
        display_name,
        version_attestation_snapshot,
        lambda: current_version_attestation(version_attestation_snapshot),
    )


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
    if initialized.state is CliExecutableVersionState.INDETERMINATE:
        raise RuntimeError(
            f"{display_name} executable authority was indeterminate during runtime "
            "initialization; execution is blocked without claiming executable drift; "
            "start a new execution session"
        )
    if initialized.state is CliExecutableVersionState.CHANGED:
        raise RuntimeError(
            f"{display_name} executable changed during runtime initialization; "
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
    if comparison is CliExecutableVersionState.INDETERMINATE:
        raise RuntimeError(
            f"{display_name} executable authority became indeterminate while verifying "
            "the executable; execution is blocked without claiming executable drift; "
            "retry the execution or start a new execution session"
        )
    raise RuntimeError(
        f"{display_name} executable version changed after runtime initialization; "
        "start a new execution session"
    )
