"""Base-state admission and candidate verification for a frozen check package.

Admission answers one question with public information only: does the package
behave on the pinned base checkout the way its roles declare? A reproduction
check must reach its intended failing assertion (non-zero exit and its declared
``failure_signature`` in the output); a preservation check must pass. A
non-zero reproduction exit without the signature (setup, import, collection or
an unintended failure) is indeterminate, never admitted.

Every check runs on its own fresh copy of the checkout with a per-command
timeout, confined by the shared execution sandbox through the check execution
entry point (``check_env.check_command``): it can write only beneath that
copy and its scratch directory and has no network. Where the sandbox cannot
confine it, the check is indeterminate with the sandbox's reason and nothing
runs. The protected bytes of that copy (every pre-existing file plus the
materialized package files) are digested before and after the command; any
modified or deleted protected file makes the result indeterminate and sets
``protected_bytes_mutated``. New files are recorded, split into declared
scratch outputs and undeclared outputs. The source checkout itself is digested
before and after the whole run.

Every check of the package is run, and admission is per check
(``boundary/per_check.py``): a check that contradicts its own role on the base
is excluded and the rest of the package is admitted; anything else that fails
(a setup error, a mutation, a precondition) leaves the package unadmitted.
Nothing here calls back into generation: the result is data for the caller
to record.

The same executor verifies a candidate checkout against the unchanged package
(``verify_candidate``), where every check must pass.

Inputs are exactly: the package, the checkout path, and a work directory. The
module never reads reference patches, private tests, or grader outputs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from typing import Literal

from ouroboros.boundary.binding import Binding, CheckTier
from ouroboros.boundary.check_env import (
    CheckCommand,
    CheckInterpreter,
    CheckUnavailable,
    check_command,
    check_scratch,
    default_interpreter,
    spawn_check_process,
)
from ouroboros.boundary.footprint import OracleFootprint
from ouroboros.boundary.oracle import OracleResult, is_oracle_file
from ouroboros.boundary.oracle_run import (
    CappedOutput,
    kill_check_group,
    reap_check_process,
    run_oracle_check,
)
from ouroboros.boundary.package import (
    CheckPackage,
    CheckRole,
    CheckSpec,
    sha256_bytes,
)
from ouroboros.boundary.per_check import (
    HELD_OUT_NOT_DISCRIMINATING,
    held_out_all_passed,
    per_check_admission,
)
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
    PackageReceipt,
    PackageVerdict,
)
from ouroboros.boundary.tree import (
    DEFAULT_UNPROTECTED_NAMES,
    UNREADABLE,
    added_paths,
    changed_paths,
    copy_checkout,
    manifest_digest,
    tree_manifest,
    unreadable_paths,
)

ADMISSION_TIMEOUT_SECONDS = 120
_OUTPUT_TAIL_CHARS = 2000
# Per stream of a script check; more is ``output_oversized``.
_SCRIPT_OUTPUT_LIMIT = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Completed:
    return_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    launch_error: str | None
    duration: float
    output_overflow: bool = False
    unavailable: str | None = None
    """Set when nothing ran (``check_env.CheckUnavailable``): an indeterminate check."""


CANDIDATE_UNREADABLE = "candidate_unreadable"
CANDIDATE_LAYOUT = "candidate_layout"
"""A candidate path the controller must create or enter is a symlink or not a directory."""


async def _run_argv(
    argv: Sequence[str],
    cwd: Path,
    timeout: int,
    *,
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
    scratch_parent: Path | None = None,
    writable_root: Path | None = None,
) -> _Completed:
    """Run ``argv`` without a shell; kill its whole process group on timeout.

    The argv, environment and working directory come from the one check
    execution entry point (``check_env.check_command``): confined to writing
    beneath ``writable_root`` (default ``cwd``; a check passes its checkout
    copy) and its scratch directory in ``scratch_parent``, with ``env``
    (default: this process's) only as the source of the allowlisted values.
    When it refuses (``CheckUnavailable``) nothing runs.
    """
    with check_scratch(scratch_parent) as scratch:
        command = check_command(
            argv,
            cwd=cwd,
            writable_root=writable_root or cwd,
            interpreter=interpreter or default_interpreter(),
            scratch=scratch,
            source=env,
        )
        if isinstance(command, CheckUnavailable):
            return _Completed(None, b"", b"", False, None, 0.0, unavailable=command.reason)
        return await _run_in_environment(command, timeout)


async def _run_in_environment(command: CheckCommand, timeout: int) -> _Completed:
    """Run ``command`` until it exits or its deadline passes, then end its process group.

    One absolute deadline, taken before the launch, bounds reading the
    output, killing the whole process group (whether or not its leader has
    exited) and reaping it (``oracle_run.reap_check_process``, at most
    ``REAP_MARGIN_SECONDS`` past the deadline). A run that ends past its
    deadline is a timeout.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    deadline = loop.time() + timeout
    try:
        process = await spawn_check_process(
            command, stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return _Completed(None, b"", b"", False, f"{type(exc).__name__}: {exc}", 0.0)
    # Both streams are read under a hard cap while they stream; past it the
    # group is killed, so a flooding check never grows controller memory.
    out, err = CappedOutput(_SCRIPT_OUTPUT_LIMIT), CappedOutput(_SCRIPT_OUTPUT_LIMIT)

    async def read(output: CappedOutput, reader: asyncio.StreamReader | None) -> None:
        await output.fill(reader)
        if output.overflow:
            kill_check_group(process)

    async def drain() -> None:
        await asyncio.gather(read(out, process.stdout), read(err, process.stderr))
        await process.wait()

    timed_out = False
    try:
        await asyncio.wait_for(drain(), timeout=max(deadline - loop.time(), 0))
    except TimeoutError:
        timed_out = True
    finally:
        # Also on cancellation (Ctrl-C, MCP cancel): nothing outlives the run.
        await reap_check_process(process, deadline)
        refused = process.launch_problem()
    if refused is not None:
        # Not started from the pinned interpreter: nothing of the check ran.
        return _Completed(
            None, b"", b"", False, None, time.monotonic() - started, unavailable=refused
        )
    # An overflow that ended in the timeout is still an overflow.
    return _Completed(
        process.returncode,
        out.data,
        err.data,
        timed_out or loop.time() > deadline,
        None,
        time.monotonic() - started,
        out.overflow or err.overflow,
    )


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _ancestors(path: str) -> list[str]:
    parts = PurePosixPath(path).parts
    return ["/".join(parts[:end]) for end in range(1, len(parts))]


def _package_preconditions(
    package: CheckPackage,
    manifest: Mapping[str, str],
    occupied: Sequence[str] = (),
    *,
    on_base: bool = True,
) -> list[str]:
    """Return reasons the package cannot be run faithfully on this checkout.

    A package path collides with the checkout when the checkout has that
    path, anything beneath it, or any of its ancestors as a file or a
    symbolic link (a readable manifest entry), or has a directory at that
    path (``occupied``): materializing it there would replace a checkout
    file or follow a link out of the copy. An entry that cannot be read (a
    named pipe, a socket, a device, an unreadable directory) is not a
    collision: the checkout cannot be copied faithfully, which is decided
    per check (``candidate_unreadable``). The pinned base files are
    compared on the base only (``on_base``): a candidate may change them,
    and its checks run on its own copied contents.
    """
    usable = {path for path, value in manifest.items() if value != UNREADABLE}
    reasons: list[str] = []
    for item in package.files:
        if (
            item.path in usable
            or any(_is_under(p, item.path) for p in usable)
            or any(ancestor in usable for ancestor in _ancestors(item.path))
            or item.path in occupied
        ):
            reasons.append(f"package_path_collision:{item.path}")
    for scratch in package.scratch_paths:
        if any(_is_under(p, scratch) for p in manifest):
            reasons.append(f"scratch_overlaps_checkout:{scratch}")
    for ref in package.base_files if on_base else ():
        if manifest.get(ref.path) != ref.sha256:
            reasons.append(f"base_file_mismatch:{ref.path}")
    if not package.checks:
        reasons.append("no_checks")
    return reasons


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_DIR_FD_SUPPORTED = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd


def _open_subdirectory(parent_fd: int, name: str) -> int:
    """Create ``name`` under ``parent_fd`` if absent and open it, never through a link."""
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _write_new_file(root: Path, relative: str, data: bytes) -> None:
    """Create ``root/relative`` with ``data`` without following any link below ``root``.

    Each ancestor is entered through a directory descriptor opened with
    ``O_NOFOLLOW``, and the file is created exclusively relative to its
    parent's descriptor, so a symbolic link or a non-directory anywhere on the
    path raises ``OSError`` instead of redirecting the write. Where the
    platform has no ``dir_fd`` support, every existing ancestor is checked with
    ``lstat`` first.
    """
    *parents, name = PurePosixPath(relative).parts
    if not _DIR_FD_SUPPORTED:
        current = root
        for part in parents:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                current.mkdir()
                continue
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(f"not a directory inside the copy: {relative}")
        with open(current / name, "xb") as handle:
            handle.write(data)
        return
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in parents:
            child = _open_subdirectory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        target = os.open(name, _NEW_FILE_FLAGS, 0o644, dir_fd=descriptor)
        with os.fdopen(target, "wb") as handle:
            handle.write(data)
    finally:
        os.close(descriptor)


def _materialize(package: CheckPackage, root: Path) -> None:
    """Write the package's generated files into the copy ``root`` (never through a link)."""
    for item in package.files:
        if is_oracle_file(item.path):
            continue  # oracle files are never materialized (boundary/oracle_run.py)
        _write_new_file(root, item.path, item.content.encode("utf-8"))


def _directory_inside(root: Path, relative: str) -> Path | None:
    """``root/relative`` when every component is a real directory (no link), else ``None``."""
    current = root
    for part in PurePosixPath(relative).parts:
        if part == ".":
            continue
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return None
        if not stat.S_ISDIR(mode):
            return None
    return current


def _occupied_paths(package: CheckPackage, source: Path) -> tuple[str, ...]:
    """Package-file paths where ``source`` has a directory (the file manifest lists none)."""
    return tuple(
        item.path for item in package.files if _directory_inside(source, item.path) is not None
    )


def _safe_name(check_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", check_id)[:64] or "check"


def _classify(
    check: CheckSpec,
    completed: _Completed,
    *,
    mutated: bool,
    signature_seen: bool,
    on_base: bool,
    oracle_undecided: bool = False,
    held_out_passes_on_base: bool = False,
) -> tuple[CheckStatus, str]:
    if completed.unavailable is not None:
        return CheckStatus.INDETERMINATE, completed.unavailable
    if mutated:
        return CheckStatus.INDETERMINATE, "protected_bytes_mutated"
    if completed.launch_error is not None:
        return CheckStatus.INDETERMINATE, "launch_failed"
    if completed.output_overflow:
        # Killed at the output cap: on a candidate its own code flooded the
        # check (a failure, like an oversized oracle observation); on the
        # base nothing was decided.
        if on_base:
            return CheckStatus.INDETERMINATE, "output_oversized"
        return CheckStatus.VIOLATED, "output_oversized"
    if completed.timed_out:
        return CheckStatus.INDETERMINATE, "timeout"
    if oracle_undecided:
        # The oracle never observed the target (setup failure, malformed
        # frame): no evidence either way, whatever the check's role.
        return CheckStatus.INDETERMINATE, "failure_signature_absent"
    passed = completed.return_code == 0
    if not on_base:
        if passed:
            return CheckStatus.EXPECTED, "passed"
        if check.role is CheckRole.REPRODUCTION:
            # Same rule as on the base: a failure that never reached the
            # intended assertion is not evidence against the candidate.
            if signature_seen:
                return CheckStatus.VIOLATED, "reproduction_still_failing"
            return CheckStatus.INDETERMINATE, "failure_signature_absent"
        return CheckStatus.VIOLATED, "preservation_failed"
    if check.role is CheckRole.PRESERVATION:
        if passed:
            return CheckStatus.EXPECTED, "preservation_passed"
        return CheckStatus.VIOLATED, "preservation_failed"
    if passed:
        return CheckStatus.VIOLATED, "reproduction_passed_on_base"
    if signature_seen and held_out_passes_on_base:
        return CheckStatus.VIOLATED, HELD_OUT_NOT_DISCRIMINATING
    if signature_seen:
        return CheckStatus.EXPECTED, "reached_failing_assertion"
    return CheckStatus.INDETERMINATE, "failure_signature_absent"


async def _execute_check(
    package: CheckPackage,
    check: CheckSpec,
    source: Path,
    copy_root: Path,
    timeout: int,
    *,
    on_base: bool,
    unprotected: frozenset[str],
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
    bindings: Mapping[str, Binding] | None = None,
    tier: str | None = None,
    include_held_out: bool = True,
    footprint: OracleFootprint | None = None,
) -> CheckExecution:
    try:
        copy_checkout(source, copy_root)
        protected = tree_manifest(copy_root, unprotected_names=unprotected)
    except (OSError, shutil.Error) as exc:
        if on_base:
            raise
        # A candidate that cannot be copied faithfully decides nothing.
        return _unreadable_execution(check, tier, f"{type(exc).__name__}")
    try:
        _materialize(package, copy_root)
    except OSError as exc:
        if on_base:
            raise
        # A candidate path the package needs is a link or a file: nothing is
        # written through it and nothing about the candidate is decided.
        return _unreadable_execution(check, tier, type(exc).__name__, reason=CANDIDATE_LAYOUT)
    protected.update(
        {item.path: item.sha256 for item in package.files if not is_oracle_file(item.path)}
    )
    digest_before = manifest_digest(protected)
    oracle = package.oracle_for(check.check_id)
    binding = (bindings or {}).get(check.check_id)
    inside = _directory_inside(copy_root, check.cwd)
    if inside is None and not on_base and os.path.lexists(copy_root / check.cwd):
        # The candidate put a link or a file where the check's directory is.
        return _unreadable_execution(check, tier, check.cwd, reason=CANDIDATE_LAYOUT)
    cwd = inside if inside is not None else copy_root / check.cwd
    oracle_run = None
    if inside is None:
        completed = _Completed(None, b"", b"", False, f"cwd missing: {check.cwd}", 0.0)
    elif oracle is not None:
        # Target processes in the project interpreter; the comparison runs in
        # this process (boundary/oracle_run.py).
        oracle_run = await run_oracle_check(
            {item.path: item.content for item in package.files},
            oracle,
            cwd,
            timeout_seconds=timeout,
            on_base=on_base,
            env=env,
            interpreter=interpreter,
            binding=binding,
            scratch_parent=copy_root.parent,
            writable_root=copy_root,
            include_held_out=include_held_out,
            footprint=footprint,
        )
        completed = _Completed(
            oracle_run.return_code,
            oracle_run.output.encode("utf-8"),
            b"",
            oracle_run.timed_out,
            oracle_run.launch_error,
            oracle_run.duration,
            unavailable=oracle_run.unavailable,
        )
    else:
        completed = await _run_argv(
            check.argv,
            cwd,
            timeout,
            env=env,
            interpreter=interpreter,
            scratch_parent=copy_root.parent,
            writable_root=copy_root,
        )
    after = tree_manifest(copy_root, unprotected_names=unprotected)
    mutated_paths = changed_paths(protected, after)
    new_paths = added_paths(protected, after)
    scratch = tuple(p for p in new_paths if any(_is_under(p, s) for s in package.scratch_paths))
    undeclared = tuple(p for p in new_paths if p not in scratch)
    combined = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
    signature = check.failure_signature
    if oracle_run is not None:
        # Decided by the in-process comparison's structured result, never by
        # text the target could print.
        combined = oracle_run.output
        signature_seen = oracle_run.signature_seen
    else:
        signature_seen = bool(signature) and signature in combined
    oracle_result = (
        OracleResult.model_validate(oracle_run.result)
        if oracle_run is not None and oracle_run.result is not None
        else None
    )
    status, reason = _classify(
        check,
        completed,
        mutated=bool(mutated_paths),
        signature_seen=signature_seen,
        on_base=on_base,
        oracle_undecided=oracle_run is not None and oracle_run.return_code not in (0, 1),
        held_out_passes_on_base=include_held_out and held_out_all_passed(oracle_result),
    )
    tail = combined if completed.launch_error is None else completed.launch_error
    if completed.unavailable is not None:
        tail = completed.unavailable
    return CheckExecution(
        check_id=check.check_id,
        role=check.role,
        argv=check.argv,
        cwd=check.cwd,
        status=status,
        reason=reason,
        return_code=completed.return_code,
        timed_out=completed.timed_out,
        duration_seconds=round(completed.duration, 3),
        signature_seen=signature_seen,
        stdout_sha256=sha256_bytes(completed.stdout),
        stderr_sha256=sha256_bytes(completed.stderr),
        output_tail=tail[-_OUTPUT_TAIL_CHARS:],
        protected_digest_before=digest_before,
        protected_digest_after=manifest_digest({p: after.get(p, "") for p in protected}),
        mutated_paths=mutated_paths,
        scratch_outputs=scratch,
        undeclared_outputs=undeclared,
        tier=tier,
        binding=binding if binding is not None else (oracle.default_binding if oracle else None),
        oracle_result=oracle_result,
    )


def _unreadable_execution(
    check: CheckSpec, tier: str | None, detail: str, *, reason: str = CANDIDATE_UNREADABLE
) -> CheckExecution:
    """An indeterminate check of a candidate whose files could not all be read or used."""
    empty = sha256_bytes(b"")
    return CheckExecution(
        check_id=check.check_id,
        role=check.role,
        argv=check.argv,
        cwd=check.cwd,
        status=CheckStatus.INDETERMINATE,
        reason=reason,
        return_code=None,
        timed_out=False,
        duration_seconds=0.0,
        signature_seen=False,
        stdout_sha256=empty,
        stderr_sha256=empty,
        output_tail=detail,
        protected_digest_before=empty,
        protected_digest_after=empty,
        mutated_paths=(),
        scratch_outputs=(),
        undeclared_outputs=(),
        tier=tier,
    )


@dataclass(frozen=True, slots=True)
class _Run:
    checks: tuple[CheckExecution, ...]
    preconditions: tuple[str, ...]
    source_digest_before: str
    source_digest_after: str
    started_at: datetime
    completed_at: datetime


async def _run_package(
    package: CheckPackage,
    source: Path,
    *,
    timeout_seconds: int,
    work_dir: Path | None,
    keep_copies: bool,
    on_base: bool,
    unprotected_names: frozenset[str],
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
    extra_preconditions: tuple[str, ...] = (),
    bindings: Mapping[str, Binding] | None = None,
    check_tiers: Mapping[str, str] | None = None,
    only_checks: frozenset[str] | None = None,
    include_held_out: bool = True,
    footprint: OracleFootprint | None = None,
) -> _Run:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"checkout not found: {source}")
    started_at = datetime.now(UTC)
    source_manifest = tree_manifest(source, unprotected_names=unprotected_names)
    source_before = manifest_digest(source_manifest)
    unreadable = unreadable_paths(source_manifest)
    if unreadable and on_base:
        raise PermissionError(f"base checkout has unreadable paths: {', '.join(unreadable[:5])}")
    # A directory at a package-file path: on the base the package cannot
    # exist in this repository (a collision); on a candidate the worker put
    # it there, and it decides nothing, check by check (``candidate_layout``).
    occupied = _occupied_paths(package, source)
    preconditions = (
        *_package_preconditions(
            package, source_manifest, occupied if on_base else (), on_base=on_base
        ),
        *extra_preconditions,
    )
    owned_work_dir = work_dir is None
    root = Path(tempfile.mkdtemp(prefix="ouroboros-check-")) if work_dir is None else work_dir
    root.mkdir(parents=True, exist_ok=True)
    if not owned_work_dir and any(root.iterdir()):
        raise ValueError(f"work_dir must be new or empty: {root}")
    executions: list[CheckExecution] = []
    try:
        if not preconditions:
            for index, check in enumerate(package.checks):
                if only_checks is not None and check.check_id not in only_checks:
                    continue
                if unreadable:
                    # The candidate cannot be copied faithfully: nothing about
                    # it is decided, whatever the check would have said.
                    executions.append(
                        _unreadable_execution(
                            check, (check_tiers or {}).get(check.check_id), unreadable[0]
                        )
                    )
                    continue
                if occupied:
                    executions.append(
                        _unreadable_execution(
                            check,
                            (check_tiers or {}).get(check.check_id),
                            occupied[0],
                            reason=CANDIDATE_LAYOUT,
                        )
                    )
                    continue
                copy_root = root / f"{index:03d}-{_safe_name(check.check_id)}"
                try:
                    executions.append(
                        await _execute_check(
                            package,
                            check,
                            source,
                            copy_root,
                            timeout_seconds,
                            on_base=on_base,
                            unprotected=unprotected_names,
                            env=env,
                            interpreter=interpreter,
                            bindings=bindings,
                            tier=(check_tiers or {}).get(check.check_id),
                            include_held_out=include_held_out,
                            footprint=footprint,
                        )
                    )
                finally:
                    if not keep_copies:
                        shutil.rmtree(copy_root, ignore_errors=True)
    finally:
        if owned_work_dir and not keep_copies:
            shutil.rmtree(root, ignore_errors=True)
    source_after = manifest_digest(tree_manifest(source, unprotected_names=unprotected_names))
    return _Run(
        checks=tuple(executions),
        preconditions=preconditions,
        source_digest_before=source_before,
        source_digest_after=source_after,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )


def _mutation_reasons(run: _Run) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if run.source_digest_before != run.source_digest_after:
        reasons.append("source_checkout_mutated")
    for execution in run.checks:
        if execution.mutated_paths:
            reasons.append(f"protected_bytes_mutated:{execution.check_id}")
    return bool(reasons), reasons


async def admit_check_package(
    package: CheckPackage,
    base_checkout: Path,
    *,
    timeout_seconds: int = ADMISSION_TIMEOUT_SECONDS,
    work_dir: Path | None = None,
    keep_copies: bool = False,
    unprotected_names: frozenset[str] = DEFAULT_UNPROTECTED_NAMES,
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
) -> AdmissionResult:
    """Run the package on isolated copies of the pinned base checkout, admitting per check.

    The tier of every check is decided here, from what its base run showed
    (``base_run_tiers``), and recorded on the check and in the receipt. Every check is confined by the check execution entry point
    (``check_env.check_command``: it writes only beneath its own copy and
    scratch directory, without network); ``env`` (default: this process's
    environment) is only where the allowlisted variables take their values
    from. ``interpreter`` (pinned, ``check_env.CheckInterpreter``) replaces a
    bare ``python3`` or ``python`` in a check's argv, and its path and source
    are recorded in the receipt.

    Verdict rules, in order: a precondition failure (package path collides with
    the checkout, scratch overlaps it, a pinned base file differs, or there are
    no checks) is ``indeterminate``; any protected-byte mutation is
    ``indeterminate`` with ``protected_bytes_mutated``; any violated check is
    ``rejected``; any other indeterminate check is ``indeterminate``; otherwise
    ``admitted``. The per-check rule (``boundary/per_check.py``) is then
    applied to that verdict: a reproduction check that passes on the base
    (for an oracle, also one whose every held-out case passes there,
    ``held_out_not_discriminating``), or a preservation check that fails on
    it, is excluded (tier ``C``, ``excluded_checks``) and the rest of the
    package is admitted.
    """
    run = await _run_package(
        package,
        base_checkout,
        timeout_seconds=timeout_seconds,
        work_dir=work_dir,
        keep_copies=keep_copies,
        on_base=True,
        unprotected_names=unprotected_names,
        env=env,
        interpreter=interpreter,
    )
    tiers = base_run_tiers(package, run.checks)
    run = replace(
        run,
        checks=tuple(
            execution.model_copy(update={"tier": CheckTier(tiers[execution.check_id])})
            for execution in run.checks
        ),
    )
    mutated, reasons = _mutation_reasons(run)
    reasons = [*run.preconditions, *reasons]
    violated = [c for c in run.checks if c.status is CheckStatus.VIOLATED]
    undecided = [c for c in run.checks if c.status is CheckStatus.INDETERMINATE]
    reasons.extend(f"{c.reason}:{c.check_id}" for c in violated)
    reasons.extend(
        f"{c.reason}:{c.check_id}" for c in undecided if c.reason != "protected_bytes_mutated"
    )
    if run.preconditions or mutated:
        verdict = PackageVerdict.INDETERMINATE
    elif violated:
        verdict = PackageVerdict.REJECTED
    elif undecided:
        verdict = PackageVerdict.INDETERMINATE
    else:
        verdict = PackageVerdict.ADMITTED
    result = AdmissionResult(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        base_tree_digest=run.source_digest_before,
        base_tree_digest_after=run.source_digest_after,
        verdict=verdict,
        reasons=tuple(reasons),
        protected_bytes_mutated=mutated,
        timeout_seconds=timeout_seconds,
        checks=run.checks,
        started_at=run.started_at,
        completed_at=run.completed_at,
        interpreter=interpreter.path if interpreter is not None else None,
        interpreter_source=interpreter.source if interpreter is not None else None,
        interpreter_sha256=interpreter.sha256 if interpreter is not None else None,
        interpreter_realpath_sha256=(
            interpreter.realpath_sha256 if interpreter is not None else None
        ),
        check_tiers=tiers or None,
    )
    return per_check_admission(result)


async def verify_candidate(
    package: CheckPackage,
    candidate_checkout: Path,
    *,
    timeout_seconds: int = ADMISSION_TIMEOUT_SECONDS,
    work_dir: Path | None = None,
    keep_copies: bool = False,
    unprotected_names: frozenset[str] = DEFAULT_UNPROTECTED_NAMES,
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
    bindings: Mapping[str, Binding] | None = None,
    only_checks: Sequence[str] | None = None,
    check_tiers: Mapping[str, CheckTier | str] | None = None,
    include_held_out: bool = True,
    footprint: OracleFootprint | None = None,
) -> CandidateVerification:
    """Run the unchanged frozen package on a candidate checkout.

    ``bindings`` (check id to late binding) are what each oracle check's
    target process resolves and calls (``boundary/oracle_run.py``); an oracle
    check without one runs through its frozen default binding. ``only_checks`` restricts the run
    to those check ids (checks of unverified criteria are not run).
    ``check_tiers`` is recorded on each check and in the receipt. With
    ``include_held_out`` false an oracle check runs its visible cases only
    (the per-attempt gate): no held-out input reaches a target process.
    ``footprint`` (``boundary/footprint.py``) collects which changed functions
    each oracle check's target processes entered; it decides nothing here.

    Every check must exit 0. A reproduction check fails only when it exits
    non-zero with its ``failure_signature``; a non-zero exit without it (setup,
    import, or an unintended failure) is indeterminate, as on the base. A
    preservation check has no signature, so any non-zero exit fails. Verdict
    rules, in order: precondition failure or protected-byte mutation is
    ``indeterminate``; any failing check is ``fail``; any other indeterminate
    check is ``indeterminate``; otherwise ``pass``. ``artifact_tree_digest`` is the candidate identity that the
    selector revalidates.
    """
    tiers = {key: CheckTier(value).value for key, value in (check_tiers or {}).items()}
    selection = None if only_checks is None else frozenset(only_checks)
    # A selection is a non-empty subset of the package's checks; anything else
    # runs nothing and is indeterminate, never a pass with zero executions.
    known = {check.check_id for check in package.checks}
    refused: tuple[str, ...] = ()
    if selection is not None and not selection:
        refused = ("no_checks",)
    elif selection is not None and not selection <= known:
        refused = ("unknown_checks",)
    run = await _run_package(
        package,
        candidate_checkout,
        timeout_seconds=timeout_seconds,
        work_dir=work_dir,
        keep_copies=keep_copies,
        on_base=False,
        unprotected_names=unprotected_names,
        env=env,
        interpreter=interpreter,
        bindings=bindings,
        check_tiers=tiers,
        only_checks=selection,
        include_held_out=include_held_out,
        extra_preconditions=refused,
        footprint=footprint,
    )
    mutated, reasons = _mutation_reasons(run)
    reasons = [*run.preconditions, *reasons]
    violated = [c for c in run.checks if c.status is CheckStatus.VIOLATED]
    undecided = [c for c in run.checks if c.status is CheckStatus.INDETERMINATE]
    reasons.extend(f"{c.reason}:{c.check_id}" for c in violated)
    reasons.extend(
        f"{c.reason}:{c.check_id}" for c in undecided if c.reason != "protected_bytes_mutated"
    )
    if run.preconditions or mutated:
        verdict = CandidateVerdict.INDETERMINATE
    elif violated:
        verdict = CandidateVerdict.FAIL
    elif undecided:
        verdict = CandidateVerdict.INDETERMINATE
    else:
        verdict = CandidateVerdict.PASS
    return CandidateVerification(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        artifact_tree_digest=run.source_digest_before,
        artifact_tree_digest_after=run.source_digest_after,
        verdict=verdict,
        reasons=tuple(reasons),
        protected_bytes_mutated=mutated,
        timeout_seconds=timeout_seconds,
        checks=run.checks,
        started_at=run.started_at,
        completed_at=run.completed_at,
        interpreter=interpreter.path if interpreter is not None else None,
        interpreter_source=interpreter.source if interpreter is not None else None,
        check_tiers=tiers or None,
        bindings=dict(sorted(bindings.items())) if bindings else None,
    )


BINDING_ADMISSION_TIMEOUT_SECONDS = 120


class BindingAdmission(PackageReceipt):
    """One base run of a frozen oracle through a late (worker-declared) binding.

    A reproduction oracle must fail on the base through that binding, with its
    frozen failure signature; a preservation oracle must pass. Otherwise the
    binding is invalid: it would let the candidate "pass" through code that
    already behaved this way before the worker (``binding_passes_on_base``), or
    it points at code whose base behavior the oracle cannot describe
    (``binding_fails_on_base``). A timeout is indeterminate
    (``binding_admission_timeout``); there is exactly one run and no retry.
    """

    schema_version: Literal["ouroboros.binding_admission.v2"] = "ouroboros.binding_admission.v2"
    check_id: str
    criterion_key: str
    binding: Binding
    base_tree_digest: str
    valid: bool
    indeterminate: bool
    reason: str
    execution: CheckExecution


async def admit_binding(
    package: CheckPackage,
    check_id: str,
    binding: Binding,
    base_checkout: Path,
    *,
    timeout_seconds: int = BINDING_ADMISSION_TIMEOUT_SECONDS,
    unprotected_names: frozenset[str] = DEFAULT_UNPROTECTED_NAMES,
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None = None,
    include_held_out: bool = True,
) -> BindingAdmission:
    """Run one oracle check through ``binding`` on an isolated base copy (no model call).

    With ``include_held_out`` false only the visible cases run (a binding
    validated while the worker may still act on what a process saw).
    """
    oracle = package.oracle_for(check_id)
    if oracle is None:
        raise ValueError(f"{check_id} is not an oracle check")
    check = next(item for item in package.checks if item.check_id == check_id)
    base = base_checkout.resolve()
    root = Path(tempfile.mkdtemp(prefix="ouroboros-binding-"))
    try:
        base_digest = manifest_digest(tree_manifest(base, unprotected_names=unprotected_names))
        execution = await _execute_check(
            package,
            check,
            base,
            root / f"000-{_safe_name(check_id)}",
            timeout_seconds,
            on_base=True,
            unprotected=unprotected_names,
            env=env,
            interpreter=interpreter,
            bindings={check_id: binding},
            tier=CheckTier.A_PRIME.value,
            include_held_out=include_held_out,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if execution.status is CheckStatus.EXPECTED:
        valid, indeterminate, reason = True, False, "binding_admitted_on_base"
    elif execution.timed_out:
        valid, indeterminate, reason = False, True, "binding_admission_timeout"
    elif execution.status is CheckStatus.VIOLATED:
        valid, indeterminate = False, False
        reason = (
            "binding_invalid:binding_passes_on_base"
            if check.role is CheckRole.REPRODUCTION
            else "binding_invalid:binding_fails_on_base"
        )
    else:
        valid, indeterminate, reason = False, True, f"binding_admission_{execution.reason}"
    return BindingAdmission(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        check_id=check_id,
        criterion_key=oracle.criterion_key,
        binding=binding,
        base_tree_digest=base_digest,
        valid=valid,
        indeterminate=indeterminate,
        reason=reason,
        execution=execution,
    )


def base_run_tiers(package: CheckPackage, executions: Sequence[CheckExecution]) -> dict[str, str]:
    """The admitted tier of every check that ran on the base, from what that run showed.

    An oracle check's tier is ``OracleSpec.base_run_tier`` of what its target
    process resolved on the base (the harness resolves inside the checkout
    only). A model-written script check is ``S``: it claims no target, its
    pass is advisory and only its failure counts. Nothing here reads the
    files to guess a target.
    """
    tiers: dict[str, str] = {}
    for execution in executions:
        oracle = package.oracle_for(execution.check_id)
        if oracle is None:
            tiers[execution.check_id] = CheckTier.S.value
            continue
        resolve = execution.oracle_result.resolve if execution.oracle_result else None
        tiers[execution.check_id] = oracle.base_run_tier(resolve).value
    return tiers
