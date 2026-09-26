"""Base-state admission and candidate verification for a frozen check package.

Admission answers one question with public information only: does the package
behave on the pinned base checkout the way its roles declare? A reproduction
check must reach its intended failing assertion (non-zero exit and its declared
``failure_signature`` in the output); a preservation check must pass. A
non-zero reproduction exit without the signature (setup, import, collection or
an unintended failure) is indeterminate, never admitted.

Every check runs on its own fresh copy of the checkout with a per-command
timeout. The protected bytes of that copy (every pre-existing file plus the
materialized package files) are digested before and after the command; any
modified or deleted protected file makes the result indeterminate and sets
``protected_bytes_mutated``. New files are recorded, split into declared
scratch outputs and undeclared outputs. The source checkout itself is digested
before and after the whole run.

The whole package is evaluated. A failed check stays in the result and makes
the package ``rejected``; there is no per-check subset admission. Nothing here
calls back into generation: the result is data for the caller to record.

The same executor verifies a candidate checkout against the unchanged package
(``verify_candidate``), where every check must pass.

Inputs are exactly: the package, the checkout path, and a work directory. The
module never reads reference patches, private tests, or grader outputs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
from typing import Any, Literal

from pydantic import BaseModel

from ouroboros.boundary.check_rules import PROSE_ONLY_CHECK_REASON, prose_only_checks
from ouroboros.boundary.package import (
    CheckPackage,
    CheckRole,
    CheckSpec,
    canonical_json_bytes,
    sha256_bytes,
)
from ouroboros.boundary.tree import (
    DEFAULT_UNPROTECTED_NAMES,
    added_paths,
    changed_paths,
    copy_checkout,
    manifest_digest,
    tree_manifest,
)

ADMISSION_TIMEOUT_SECONDS = 120
_OUTPUT_TAIL_CHARS = 2000


class CheckStatus(StrEnum):
    """Outcome of one check against its role contract."""

    EXPECTED = "expected"
    VIOLATED = "violated"
    INDETERMINATE = "indeterminate"


class PackageVerdict(StrEnum):
    """Whole-package admission verdict on the base checkout."""

    ADMITTED = "admitted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class CandidateVerdict(StrEnum):
    """Whole-package verdict on a candidate checkout."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class CheckExecution(BaseModel, frozen=True):
    """Receipt for one check command on one isolated copy."""

    check_id: str
    role: CheckRole
    argv: tuple[str, ...]
    cwd: str
    status: CheckStatus
    reason: str
    return_code: int | None
    timed_out: bool
    duration_seconds: float
    signature_seen: bool
    stdout_sha256: str
    stderr_sha256: str
    output_tail: str
    protected_digest_before: str
    protected_digest_after: str
    mutated_paths: tuple[str, ...]
    scratch_outputs: tuple[str, ...]
    undeclared_outputs: tuple[str, ...]


class AdmissionResult(BaseModel, frozen=True):
    """Whole-package admission receipt on the pinned base checkout."""

    schema_version: Literal["ouroboros.check_admission.v1"] = "ouroboros.check_admission.v1"
    package_sha256: str
    seed_digest: str
    base_tree_digest: str
    base_tree_digest_after: str
    verdict: PackageVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[CheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter: str | None = None
    interpreter_source: str | None = None

    def event_summary(self) -> dict[str, Any]:
        """Return the journal payload: statuses and digests, no argv or output."""
        return _journal_safe(self)


class CandidateVerification(BaseModel, frozen=True):
    """Receipt for running the unchanged frozen package on a candidate."""

    schema_version: Literal["ouroboros.candidate_verification.v1"] = (
        "ouroboros.candidate_verification.v1"
    )
    package_sha256: str
    artifact_tree_digest: str
    artifact_tree_digest_after: str
    verdict: CandidateVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[CheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter: str | None = None
    interpreter_source: str | None = None

    def event_summary(self) -> dict[str, Any]:
        """Return the journal payload: statuses and digests, no argv or output."""
        return _journal_safe(self)


_JOURNAL_EXCLUDED_CHECK_FIELDS = frozenset({"argv", "output_tail"})
# Optional receipt fields: omitted when unset (callers that pass no
# interpreter keep byte-identical receipts), and the interpreter's absolute
# path stays in the stored receipt only, never in the journal.
_OPTIONAL_RECEIPT_FIELDS = ("interpreter", "interpreter_source")
_JOURNAL_EXCLUDED_RECEIPT_FIELDS = frozenset({"interpreter"})


def _receipt_dump(receipt: BaseModel) -> dict[str, Any]:
    data = receipt.model_dump(mode="json")
    for key in _OPTIONAL_RECEIPT_FIELDS:
        if data.get(key) is None:
            data.pop(key, None)
    return data


def _journal_safe(receipt: BaseModel) -> dict[str, Any]:
    """Dump a receipt without check argv or output text.

    The event journal is readable by other tools, so it carries statuses,
    reasons, and digests only. The complete receipt (argv, output tails) is a
    separately stored artifact, see ``write_receipt``.
    """
    data = _receipt_dump(receipt)
    for key in _JOURNAL_EXCLUDED_RECEIPT_FIELDS:
        data.pop(key, None)
    data["checks"] = [
        {key: value for key, value in check.items() if key not in _JOURNAL_EXCLUDED_CHECK_FIELDS}
        for check in data["checks"]
    ]
    return data


def write_receipt(receipt: AdmissionResult | CandidateVerification, directory: Path) -> Path:
    """Write a complete receipt to ``<directory>/<sha256>.json`` (create-only)."""
    directory.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(_receipt_dump(receipt))
    target = directory / f"{sha256_bytes(data)}.json"
    if not target.exists():
        with open(target, "xb") as handle:
            handle.write(data)
    return target


@dataclass(frozen=True, slots=True)
class _Completed:
    return_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    launch_error: str | None
    duration: float


def _command_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    # Same rule as mechanical verification: a nested-server sentinel must not
    # leak into the checked process.
    env.pop("_OUROBOROS_NESTED", None)
    return env


_PYTHON_ARGV0 = frozenset({"python3", "python"})


def _resolved_argv(argv: Sequence[str], interpreter: str | None) -> tuple[str, ...]:
    """Replace a bare ``python3``/``python`` with ``interpreter`` when one is given."""
    if interpreter and argv and argv[0] in _PYTHON_ARGV0:
        return (interpreter, *argv[1:])
    return tuple(argv)


async def _run_argv(
    argv: Sequence[str],
    cwd: Path,
    timeout: int,
    *,
    env: Mapping[str, str] | None = None,
    interpreter: str | None = None,
) -> _Completed:
    """Run ``argv`` without a shell; kill its whole process group on timeout."""
    started = time.monotonic()
    posix = sys.platform != "win32"
    try:
        process = await asyncio.create_subprocess_exec(
            *_resolved_argv(argv, interpreter),
            cwd=cwd,
            env=_command_env(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=posix,
        )
    except OSError as exc:
        return _Completed(None, b"", b"", False, f"{type(exc).__name__}: {exc}", 0.0)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        _kill(process, posix)
        try:
            # A descendant that left the process group can keep the pipes open;
            # never let draining them outlive the command budget.
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
            stdout, stderr = b"", b""
        return _Completed(
            process.returncode, stdout, stderr, True, None, time.monotonic() - started
        )
    except asyncio.CancelledError:
        _kill(process, posix)
        await process.wait()
        raise
    return _Completed(process.returncode, stdout, stderr, False, None, time.monotonic() - started)


def _kill(process: asyncio.subprocess.Process, posix: bool) -> None:
    if process.returncode is not None:
        return
    try:
        if posix:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _package_preconditions(package: CheckPackage, manifest: Mapping[str, str]) -> list[str]:
    """Return reasons the package cannot be run faithfully on this checkout."""
    reasons: list[str] = []
    for item in package.files:
        if item.path in manifest or any(_is_under(p, item.path) for p in manifest):
            reasons.append(f"package_path_collision:{item.path}")
    for scratch in package.scratch_paths:
        if any(_is_under(p, scratch) for p in manifest):
            reasons.append(f"scratch_overlaps_checkout:{scratch}")
    for ref in package.base_files:
        if manifest.get(ref.path) != ref.sha256:
            reasons.append(f"base_file_mismatch:{ref.path}")
    if not package.checks:
        reasons.append("no_checks")
    return reasons


def _materialize(package: CheckPackage, root: Path) -> None:
    for item in package.files:
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(item.content.encode("utf-8"))


def _safe_name(check_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", check_id)[:64] or "check"


def _classify(
    check: CheckSpec,
    completed: _Completed,
    *,
    mutated: bool,
    signature_seen: bool,
    on_base: bool,
) -> tuple[CheckStatus, str]:
    if mutated:
        return CheckStatus.INDETERMINATE, "protected_bytes_mutated"
    if completed.launch_error is not None:
        return CheckStatus.INDETERMINATE, "launch_failed"
    if completed.timed_out:
        return CheckStatus.INDETERMINATE, "timeout"
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
    interpreter: str | None = None,
) -> CheckExecution:
    copy_checkout(source, copy_root)
    protected = tree_manifest(copy_root, unprotected_names=unprotected)
    _materialize(package, copy_root)
    protected.update({item.path: item.sha256 for item in package.files})
    digest_before = manifest_digest(protected)
    cwd = copy_root / check.cwd
    if cwd.is_dir():
        completed = await _run_argv(check.argv, cwd, timeout, env=env, interpreter=interpreter)
    else:
        completed = _Completed(None, b"", b"", False, f"cwd missing: {check.cwd}", 0.0)
    after = tree_manifest(copy_root, unprotected_names=unprotected)
    mutated_paths = changed_paths(protected, after)
    new_paths = added_paths(protected, after)
    scratch = tuple(p for p in new_paths if any(_is_under(p, s) for s in package.scratch_paths))
    undeclared = tuple(p for p in new_paths if p not in scratch)
    combined = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
    signature = check.failure_signature
    signature_seen = bool(signature) and signature in combined
    status, reason = _classify(
        check,
        completed,
        mutated=bool(mutated_paths),
        signature_seen=signature_seen,
        on_base=on_base,
    )
    tail = combined if completed.launch_error is None else completed.launch_error
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
    interpreter: str | None = None,
    extra_preconditions: tuple[str, ...] = (),
) -> _Run:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"checkout not found: {source}")
    started_at = datetime.now(UTC)
    source_manifest = tree_manifest(source, unprotected_names=unprotected_names)
    source_before = manifest_digest(source_manifest)
    preconditions = (
        *_package_preconditions(package, source_manifest),
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
    interpreter: str | None = None,
    interpreter_source: str | None = None,
    reject_prose_only_checks: bool = False,
) -> AdmissionResult:
    """Run the whole package on isolated copies of the pinned base checkout.

    ``env`` replaces the process environment of every check (default: this
    process's environment); ``interpreter`` replaces a bare ``python3`` or
    ``python`` in a check's argv, and it and ``interpreter_source`` are
    recorded in the receipt. With ``reject_prose_only_checks`` a check that
    only matches text in prose files (``boundary/check_rules.py``) makes the
    package ``rejected`` (``prose_only_check:<check_id>``) before any command
    runs.

    Verdict rules, in order: a precondition failure (package path collides with
    the checkout, scratch overlaps it, a pinned base file differs, or there are
    no checks) is ``indeterminate``; any protected-byte mutation is
    ``indeterminate`` with ``protected_bytes_mutated``; any violated check is
    ``rejected``; any other indeterminate check is ``indeterminate``; otherwise
    ``admitted``.
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
        extra_preconditions=tuple(
            f"{PROSE_ONLY_CHECK_REASON}:{check_id}"
            for check_id in (prose_only_checks(package) if reject_prose_only_checks else ())
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
    if any(reason.startswith(f"{PROSE_ONLY_CHECK_REASON}:") for reason in run.preconditions):
        verdict = PackageVerdict.REJECTED
    elif run.preconditions or mutated:
        verdict = PackageVerdict.INDETERMINATE
    elif violated:
        verdict = PackageVerdict.REJECTED
    elif undecided:
        verdict = PackageVerdict.INDETERMINATE
    else:
        verdict = PackageVerdict.ADMITTED
    return AdmissionResult(
        package_sha256=package.sha256,
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
        interpreter=interpreter,
        interpreter_source=interpreter_source,
    )


async def verify_candidate(
    package: CheckPackage,
    candidate_checkout: Path,
    *,
    timeout_seconds: int = ADMISSION_TIMEOUT_SECONDS,
    work_dir: Path | None = None,
    keep_copies: bool = False,
    unprotected_names: frozenset[str] = DEFAULT_UNPROTECTED_NAMES,
    env: Mapping[str, str] | None = None,
    interpreter: str | None = None,
    interpreter_source: str | None = None,
) -> CandidateVerification:
    """Run the unchanged frozen package on a candidate checkout.

    Every check must exit 0. A reproduction check fails only when it exits
    non-zero with its ``failure_signature``; a non-zero exit without it (setup,
    import, or an unintended failure) is indeterminate, as on the base. A
    preservation check has no signature, so any non-zero exit fails. Verdict
    rules, in order: precondition failure or protected-byte mutation is
    ``indeterminate``; any failing check is ``fail``; any other indeterminate
    check is ``indeterminate``; otherwise ``pass``. ``artifact_tree_digest`` is the candidate identity that the
    selector revalidates.
    """
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
        artifact_tree_digest=run.source_digest_before,
        artifact_tree_digest_after=run.source_digest_after,
        verdict=verdict,
        reasons=tuple(reasons),
        protected_bytes_mutated=mutated,
        timeout_seconds=timeout_seconds,
        checks=run.checks,
        started_at=run.started_at,
        completed_at=run.completed_at,
        interpreter=interpreter,
        interpreter_source=interpreter_source,
    )
