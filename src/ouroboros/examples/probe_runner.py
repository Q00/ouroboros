"""Run probes on the base and candidate trees and collect their outcome records.

A probe is one input: a ``call`` (Python source defining ``probe()``) or a
``command`` (argv and stdin). Each probe runs on every tree, twice per tree
(``record.REPEATS``), each time in a fresh throwaway copy of the tree
(``boundary/tree.copy_checkout``). The process is the product probe wrapper
(``examples/probe_wrapper.py``), started through the one check execution
entry point (``boundary/check_env.check_command``) and run by the
admission executor (``boundary/admission._run_in_environment``): confined by
the shared execution sandbox (writes only beneath the copy and the run's
scratch directory, no network, an allowlisted environment, the pinned
interpreter), without a shell, its whole process group killed at the per-run
deadline (``PROBE_TIMEOUT_SECONDS``, 20 s). The probe file goes to the
run's scratch directory, never into the copy. No model is called and
nothing is classified by pattern: a record holds only what the wrapper
reported, checked structurally.

One run gives one ``OutcomeRecord``:

- the wrapper's record when it reported exactly one well-formed record for
  its kind (``call``: ``returned``, ``raised``, ``not_run``; ``command``:
  ``exited``, ``not_run``), with exactly the fields of that status;
- ``timed_out`` with no fields at the deadline;
- ``exited`` with the process's ``exit_code`` when it ended without such a
  record (it died, or flooded its output and was killed).

A run the controller could not make is indeterminate: the sandbox or the
interpreter pin refused the command (``check_env.CheckUnavailable``), the
process could not be launched, the tree could not be copied, or the step's
deadline left no room for a full per-run timeout. None of those says
anything about the tree, so one indeterminate run makes the whole probe
indeterminate: its remaining runs are skipped, every one of its records is
``not_run`` with no fields (so ``examples.disagreement`` drops it as not
run), and its reason is reported in ``ProbePoolRun.indeterminate``. There is
no unconfined fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from types import MappingProxyType

from ouroboros.boundary.admission import _run_in_environment
from ouroboros.boundary.check_env import (
    CheckInterpreter,
    CheckUnavailable,
    check_command,
    check_scratch,
    resolve_check_interpreter,
)
from ouroboros.boundary.oracle_run import REAP_MARGIN_SECONDS, parse_frame
from ouroboros.boundary.tree import copy_checkout
from ouroboros.examples.record import (
    BASE_TREE_ID,
    REPEATS,
    STATUS_KEY,
    OutcomeRecord,
    ProbeStatus,
    canonical_json_bytes,
    sha256_hex,
)

PROBE_TIMEOUT_SECONDS = 20
"""Per run: the wrapper process, from launch to the end of its process group."""
DEFAULT_PARALLEL_RUNS = 4
PROBE_WRAPPER_SOURCE = (
    resources.files("ouroboros.examples").joinpath("probe_wrapper.py").read_text(encoding="utf-8")
)
LAUNCH_FAILED = "launch_failed"
COPY_FAILED = "copy_failed"
STEP_DEADLINE = "step_deadline"
"""The step's deadline left less than one full per-run timeout: the run never started."""


class ProbeKind(StrEnum):
    """What a probe runs (the first release: Python calls and commands)."""

    CALL = "call"
    COMMAND = "command"


# Statuses the wrapper may report per kind, and the exact fields of each.
_WRAPPER_FIELDS: Mapping[ProbeKind, Mapping[ProbeStatus, frozenset[str]]] = {
    ProbeKind.CALL: {
        ProbeStatus.RETURNED: frozenset({"value"}),
        ProbeStatus.RAISED: frozenset({"exception_type", "message"}),
        ProbeStatus.NOT_RUN: frozenset({"exception_type", "message"}),
    },
    ProbeKind.COMMAND: {
        ProbeStatus.EXITED: frozenset({"exit_code", "stdout", "stderr"}),
        ProbeStatus.NOT_RUN: frozenset({"exception_type", "message"}),
    },
}
_FIELD_TYPES: Mapping[str, type | tuple[type, ...]] = {
    "exception_type": str,
    "message": str,
    "stdout": str,
    "stderr": str,
    "exit_code": int,
}


@dataclass(frozen=True, slots=True)
class Probe:
    """One input: ``source`` for a ``call``, ``argv`` and ``stdin`` for a ``command``.

    Its identity is ``probe_id``, the sha256 of its canonical bytes: the
    canonical JSON of its kind and its declared fields. Two probes with the
    same kind and inputs have the same id, whoever wrote them.
    """

    kind: ProbeKind
    source: str = ""
    argv: tuple[str, ...] = ()
    stdin: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ProbeKind(self.kind))
        object.__setattr__(self, "argv", tuple(self.argv))
        if not isinstance(self.source, str) or not isinstance(self.stdin, str):
            raise ValueError("source and stdin must be strings")
        if not all(isinstance(item, str) for item in self.argv):
            raise ValueError("argv items must be strings")
        if self.kind is ProbeKind.CALL:
            if not self.source or self.argv or self.stdin:
                raise ValueError("a call probe has source only")
        elif not self.argv or self.source:
            raise ValueError("a command probe has argv (and stdin) only")

    def declared(self) -> dict[str, object]:
        """The probe's declared fields as JSON data."""
        if self.kind is ProbeKind.CALL:
            return {"kind": self.kind.value, "source": self.source}
        return {"kind": self.kind.value, "argv": list(self.argv), "stdin": self.stdin}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.declared())

    @property
    def probe_id(self) -> str:
        return sha256_hex(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class ProbeRun:
    """One run's record, and why it is indeterminate (``None`` when it ran)."""

    record: OutcomeRecord
    indeterminate: str | None = None


@dataclass(frozen=True, slots=True)
class ProbePoolRun:
    """Two records per (probe, tree), ordered; indeterminate probes by id with a reason."""

    records: tuple[OutcomeRecord, ...]
    indeterminate: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "indeterminate", MappingProxyType(dict(self.indeterminate)))


def _not_run(probe_id: str, tree_id: str, repeat: int) -> OutcomeRecord:
    return OutcomeRecord(probe_id, tree_id, repeat, ProbeStatus.NOT_RUN)


def wrapper_record(
    stdout: bytes, nonce: str, kind: ProbeKind, probe_id: str, tree_id: str, repeat: int
) -> OutcomeRecord | None:
    """The one well-formed record line in ``stdout``, or ``None``.

    The line is '<nonce> <json>' (``probe_wrapper._report``), parsed with
    the controller's bounded frame parser (``oracle_run.parse_frame``). It
    must be the only such line, carry a status the wrapper reports for
    ``kind``, and hold exactly that status's fields with their JSON types.
    """
    prefix = (nonce + " ").encode("ascii")
    lines = [line for line in stdout.split(b"\n") if line.startswith(prefix)]
    if len(lines) != 1:
        return None
    payload = parse_frame(lines[0][len(prefix) :].rstrip(b"\r"))
    if payload is None:
        return None
    try:
        status = ProbeStatus(payload.pop(STATUS_KEY, None))
    except ValueError:
        return None
    expected = _WRAPPER_FIELDS[kind].get(status)
    if expected is None or set(payload) != expected:
        return None
    for key, value in payload.items():
        wanted = _FIELD_TYPES.get(key)
        if wanted is not None and (isinstance(value, bool) or not isinstance(value, wanted)):
            return None
    try:
        return OutcomeRecord(probe_id, tree_id, repeat, status, payload)
    except ValueError:
        return None


def _write_probe(probe: Probe, scratch: Path) -> Path:
    if probe.kind is ProbeKind.CALL:
        path = scratch / "probe.py"
        path.write_bytes(probe.source.encode("utf-8"))
    else:
        path = scratch / "probe.json"
        path.write_bytes(json.dumps({"argv": list(probe.argv), "stdin": probe.stdin}).encode())
    return path


def fits(deadline: float | None, timeout: int) -> bool:
    """Whether a full run (``timeout`` plus the reap margin) ends before ``deadline``."""
    return deadline is None or time.monotonic() + timeout + REAP_MARGIN_SECONDS <= deadline


async def run_probe(
    probe: Probe,
    tree: Path,
    *,
    tree_id: str,
    repeat: int,
    interpreter: CheckInterpreter,
    work_dir: Path,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> ProbeRun:
    """Run ``probe`` once on a fresh copy of ``tree``; the copy is removed afterwards.

    With a step ``deadline`` (``time.monotonic()`` seconds), the process is
    launched only when a full ``timeout`` still fits before it once the copy
    is ready (``fits``); otherwise nothing runs and the run is indeterminate
    (``STEP_DEADLINE``).
    """
    probe_id = probe.probe_id
    run_dir = Path(tempfile.mkdtemp(prefix="probe-run-", dir=work_dir))
    try:
        copy = run_dir / "tree"
        try:
            await asyncio.to_thread(copy_checkout, tree, copy)
        except (OSError, shutil.Error) as exc:
            return ProbeRun(
                _not_run(probe_id, tree_id, repeat), f"{COPY_FAILED}:{type(exc).__name__}"
            )
        if not fits(deadline, timeout):
            return ProbeRun(_not_run(probe_id, tree_id, repeat), STEP_DEADLINE)
        with check_scratch(run_dir) as scratch:
            nonce = secrets.token_hex(16)
            path = _write_probe(probe, scratch)
            argv = ("python3", "-I", "-B", "-c", PROBE_WRAPPER_SOURCE, probe.kind.value)
            command = check_command(
                (*argv, nonce, str(path)),
                cwd=copy,
                writable_root=copy,
                interpreter=interpreter,
                scratch=scratch,
            )
            if isinstance(command, CheckUnavailable):
                return ProbeRun(_not_run(probe_id, tree_id, repeat), command.reason)
            completed = await _run_in_environment(command, timeout)
    finally:
        await asyncio.to_thread(shutil.rmtree, run_dir, True)
    if completed.unavailable is not None:
        return ProbeRun(_not_run(probe_id, tree_id, repeat), completed.unavailable)
    if completed.launch_error is not None:
        return ProbeRun(_not_run(probe_id, tree_id, repeat), LAUNCH_FAILED)
    if completed.timed_out:
        return ProbeRun(OutcomeRecord(probe_id, tree_id, repeat, ProbeStatus.TIMED_OUT))
    record = None
    if not completed.output_overflow:
        record = wrapper_record(completed.stdout, nonce, probe.kind, probe_id, tree_id, repeat)
    if record is None:
        fields = {"exit_code": completed.return_code}
        record = OutcomeRecord(probe_id, tree_id, repeat, ProbeStatus.EXITED, fields)
    return ProbeRun(record)


def _unique_probes(probes: Iterable[Probe]) -> list[Probe]:
    """The pool, deduplicated by probe id and ordered by it."""
    by_id: dict[str, Probe] = {}
    for probe in probes:
        by_id.setdefault(probe.probe_id, probe)
    return [by_id[probe_id] for probe_id in sorted(by_id)]


def _check_trees(trees: Mapping[str, Path]) -> None:
    if BASE_TREE_ID not in trees:
        raise ValueError(f"trees must include the base tree {BASE_TREE_ID!r}")
    if len(trees) < 2:
        raise ValueError("trees must include at least one candidate")
    if not all(isinstance(tree_id, str) and tree_id for tree_id in trees):
        raise ValueError("tree ids must be non-empty strings")


async def run_probe_pool(
    probes: Iterable[Probe],
    trees: Mapping[str, Path],
    *,
    interpreter: CheckInterpreter | None = None,
    work_dir: Path | None = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    parallel: int = DEFAULT_PARALLEL_RUNS,
    deadline: float | None = None,
) -> ProbePoolRun:
    """Run every probe on every tree, twice per tree; return every record.

    ``trees`` maps a tree id to its directory and must hold ``BASE_TREE_ID``
    and at least one candidate. ``interpreter`` is pinned once for the
    whole pool (default: ``check_env.resolve_check_interpreter`` on the base
    tree), so every tree runs the same interpreter. At most ``parallel``
    runs are in flight. ``deadline`` (``time.monotonic()`` seconds) is the
    step's: a run starts only when a full ``timeout`` plus the reap margin
    still fits before it, checked again after its copy is made and before
    its process starts, so a run is never cut short by the step.
    Copies and scratch directories are made in ``work_dir`` (default: a
    temporary directory, removed afterwards). The result satisfies
    ``examples.disagreement.find_disagreements``: exactly one record per
    (probe, tree, repeat).
    """
    _check_trees(trees)
    if parallel < 1:
        raise ValueError("parallel must be at least 1")
    pool = _unique_probes(probes)
    pinned = interpreter or resolve_check_interpreter(trees[BASE_TREE_ID])
    cells = [
        (probe, tree_id, repeat)
        for probe in pool
        for tree_id in sorted(trees)
        for repeat in REPEATS
    ]
    gate = asyncio.Semaphore(parallel)
    runs: dict[tuple[str, str, int], ProbeRun] = {}
    reasons: dict[str, str] = {}

    async def run_cell(probe: Probe, tree_id: str, repeat: int, root: Path) -> None:
        probe_id = probe.probe_id
        async with gate:
            if probe_id in reasons:
                return
            if not fits(deadline, timeout):
                reasons.setdefault(probe_id, STEP_DEADLINE)
                return
            run = await run_probe(
                probe,
                trees[tree_id],
                tree_id=tree_id,
                repeat=repeat,
                interpreter=pinned,
                work_dir=root,
                timeout=timeout,
                deadline=deadline,
            )
        runs[(probe_id, tree_id, repeat)] = run
        if run.indeterminate is not None:
            reasons.setdefault(probe_id, run.indeterminate)

    async def run_all(root: Path) -> None:
        await asyncio.gather(*(run_cell(*cell, root) for cell in cells))

    if work_dir is not None:
        await run_all(work_dir)
    else:
        root = Path(tempfile.mkdtemp(prefix="ouroboros-probes-"))
        try:
            await run_all(root)
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)
    return _collect(cells, runs, reasons)


def _collect(
    cells: Sequence[tuple[Probe, str, int]],
    runs: Mapping[tuple[str, str, int], ProbeRun],
    reasons: Mapping[str, str],
) -> ProbePoolRun:
    records: list[OutcomeRecord] = []
    for probe, tree_id, repeat in cells:
        probe_id = probe.probe_id
        if probe_id in reasons:
            records.append(_not_run(probe_id, tree_id, repeat))
        else:
            records.append(runs[(probe_id, tree_id, repeat)].record)
    return ProbePoolRun(tuple(records), {key: reasons[key] for key in sorted(reasons)})


__all__ = [
    "COPY_FAILED",
    "DEFAULT_PARALLEL_RUNS",
    "LAUNCH_FAILED",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_WRAPPER_SOURCE",
    "STEP_DEADLINE",
    "fits",
    "Probe",
    "ProbeKind",
    "ProbePoolRun",
    "ProbeRun",
    "run_probe",
    "run_probe_pool",
    "wrapper_record",
]
