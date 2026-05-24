"""Runtime acceptance evidence (L3-1 of #1157 / #1176).

The existing :mod:`ouroboros.orchestrator.evidence_schema` substrate
ships unit-test evidence (``EvidenceRecord`` — a permissive mapping
container). L3 adds a *sibling* substrate for **runtime acceptance
evidence**: the artifact actually ran, and here's what came out.

v1 ships *one* probe kind: :class:`HeadlessRunProbe`. Per #1176's
minimal-substrate audit, ``sim_trace`` / ``render_hash`` / ``api_smoke``
each open as their own follow-up issue *only when a canonical scenario
demonstrates ``headless_run`` cannot cover it*.

The L1-a catalog (#1173) already declares per-class
``runtime_probe_kinds`` strings. This module:

1. Defines :class:`RuntimeEvidence` (frozen dataclass) — what a probe
   returns.
2. Defines :class:`HeadlessRunProbe` — subprocess + stdout / stderr /
   exit_code / duration capture, with a wall-clock timeout.
3. Defines :func:`probes_for_task_class` — given a :class:`TaskClass`,
   return the bound probe instances. v1 maps ``headless_run`` →
   :class:`HeadlessRunProbe` and leaves every other declared kind as a
   no-op deferred placeholder (so adding ``sim_trace`` later is a
   one-line registry change).

L3-2 wires :class:`RuntimeEvidence` into the Track A verifier's grade
input and onto the result envelope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import shlex
import subprocess
import time
from typing import Protocol

from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass

__all__ = [
    "HEADLESS_RUN_PROBE_KIND",
    "DeferredProbe",
    "HeadlessRunProbe",
    "RuntimeEvidence",
    "RuntimeProbe",
    "probes_for_task_class",
]


HEADLESS_RUN_PROBE_KIND: str = "headless_run"
"""The sole v1 probe-kind identifier. Future probe kinds (``sim_trace``,
``render_hash``, ``api_smoke``) each ship as their own follow-up
under the #1176 v2 expansion path."""


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Outcome of one runtime probe invocation.

    Attributes
    ----------
    probe_kind:
        Catalog identifier for the probe that produced this evidence
        (e.g. ``"headless_run"``). Matches one of the strings declared
        in :attr:`ouroboros.auto.task_classes.TaskClassProfile.runtime_probe_kinds`.
    passed:
        ``True`` iff the probe judges the artifact as running correctly
        per its own contract. ``HeadlessRunProbe`` defines this as
        ``exit_code == 0`` *and* the command completed within the
        wall-clock budget.
    summary:
        One-line human-readable summary the verifier can surface
        without re-parsing the structured payload.
    duration_seconds:
        Wall-clock elapsed time of the probe in seconds (float,
        non-negative). ``0.0`` for probes that did not run a
        subprocess.
    payload:
        Probe-specific structured data (stdout, exit code, sim trace,
        screenshot hash, …). Kept open as ``dict[str, Any]`` so future
        probe kinds can ship payloads without re-versioning the
        ``RuntimeEvidence`` schema.
    """

    probe_kind: str
    passed: bool
    summary: str
    duration_seconds: float = 0.0
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            msg = f"duration_seconds must be >= 0; got {self.duration_seconds}"
            raise ValueError(msg)
        if not self.probe_kind:
            msg = "probe_kind must be a non-empty string"
            raise ValueError(msg)


class RuntimeProbe(Protocol):
    """Protocol for runtime probes.

    Probes are synchronous — they wrap a subprocess or in-memory check
    and return :class:`RuntimeEvidence`. The L3-2 verifier integration
    invokes them at grade-input time after the artifact's tests have
    passed.
    """

    @property
    def kind(self) -> str: ...

    def run(
        self,
        *,
        cwd: Path,
        command: Sequence[str] | str,
        timeout_seconds: float | None = None,
    ) -> RuntimeEvidence: ...


@dataclass(frozen=True, slots=True)
class HeadlessRunProbe:
    """Subprocess-based runtime probe — the v1 default.

    Invokes the documented command, captures stdout / stderr /
    exit_code / duration, and judges PASS iff the command completed
    within *timeout_seconds* with exit code 0.

    The probe is intentionally narrow: it does *not* parse stdout,
    diff against goldens, or assert structural invariants. Those are
    layered on top via the Track A verifier's existing AC machinery —
    this probe just answers *"does the thing actually run?"*.
    """

    kind: str = HEADLESS_RUN_PROBE_KIND

    def run(
        self,
        *,
        cwd: Path,
        command: Sequence[str] | str,
        timeout_seconds: float | None = None,
    ) -> RuntimeEvidence:
        """Execute *command* in *cwd* with *timeout_seconds* budget.

        Parameters
        ----------
        cwd:
            Working directory for the subprocess. Must exist; the probe
            does not create it.
        command:
            Either a pre-tokenized list (``["python", "-m", "myapp"]``)
            or a shell-style string (``"python -m myapp --flag"``).
            Strings are tokenized via :func:`shlex.split`.
        timeout_seconds:
            Wall-clock budget. ``None`` means *no timeout*; tests should
            always pass a finite value to avoid hangs. The canonical
            scenario YAML's ``wall_clock_budget_seconds`` is the
            natural value.
        """
        if not cwd.is_dir():
            msg = f"HeadlessRunProbe cwd does not exist: {cwd}"
            raise FileNotFoundError(msg)
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            msg = "HeadlessRunProbe command must produce at least one argv token"
            raise ValueError(msg)

        start = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - intentional probe invocation
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            return RuntimeEvidence(
                probe_kind=self.kind,
                passed=False,
                summary=(
                    f"headless run timed out after {duration:.1f}s (budget {timeout_seconds}s)"
                ),
                duration_seconds=duration,
                payload={
                    "argv": argv,
                    "timeout_seconds": timeout_seconds,
                    "stdout_preview": _truncate(stdout),
                    "stderr_preview": _truncate(stderr),
                    "outcome": "timeout",
                },
            )
        duration = time.monotonic() - start
        passed = completed.returncode == 0
        summary = f"headless run exit_code={completed.returncode} (duration {duration:.2f}s)"
        return RuntimeEvidence(
            probe_kind=self.kind,
            passed=passed,
            summary=summary,
            duration_seconds=duration,
            payload={
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout_preview": _truncate(completed.stdout or ""),
                "stderr_preview": _truncate(completed.stderr or ""),
                "outcome": "completed",
            },
        )


@dataclass(frozen=True, slots=True)
class DeferredProbe:
    """Placeholder for probe kinds declared in the L1 catalog but not
    yet implemented in this v1 (per #1176 minimal-substrate audit).

    Calling :meth:`run` on a deferred probe returns a non-passing
    :class:`RuntimeEvidence` with ``outcome = "deferred"`` so the
    verifier can flag the gap without treating it as real acceptance. The L3
    follow-up that implements ``sim_trace`` / ``render_hash`` /
    ``api_smoke`` swaps the registry entry from :class:`DeferredProbe`
    to the real implementation; no consumer needs to change.
    """

    kind: str

    def run(
        self,
        *,
        cwd: Path,  # noqa: ARG002 - matches RuntimeProbe protocol
        command: Sequence[str] | str = (),  # noqa: ARG002 - matches RuntimeProbe protocol
        timeout_seconds: float | None = None,  # noqa: ARG002 - matches RuntimeProbe protocol
    ) -> RuntimeEvidence:
        return RuntimeEvidence(
            probe_kind=self.kind,
            passed=False,
            summary=f"probe kind {self.kind!r} is deferred to a future L3 follow-up",
            duration_seconds=0.0,
            payload={"outcome": "deferred"},
        )


# ---------------------------------------------------------------------------
# L1 → probe binding registry
# ---------------------------------------------------------------------------


def _build_probe_registry() -> dict[str, RuntimeProbe]:
    """Singleton registry of probe-kind → probe instance.

    v1: ``HEADLESS_RUN_PROBE_KIND`` → :class:`HeadlessRunProbe`. Every
    other kind declared in the L1 catalog gets a :class:`DeferredProbe`
    fallback so consumers calling :func:`probes_for_task_class` never
    crash with KeyError on a catalog entry that points at a not-yet-
    implemented probe.
    """
    registry: dict[str, RuntimeProbe] = {
        HEADLESS_RUN_PROBE_KIND: HeadlessRunProbe(),
    }
    # Sweep the L1 catalog and register a DeferredProbe for every
    # declared kind we do not implement yet.
    for profile in TASK_CLASS_CATALOG.values():
        for kind in profile.runtime_probe_kinds:
            if kind not in registry:
                registry[kind] = DeferredProbe(kind=kind)
    return registry


_PROBE_REGISTRY: dict[str, RuntimeProbe] = _build_probe_registry()


def probes_for_task_class(task_class: TaskClass) -> tuple[RuntimeProbe, ...]:
    """Return the probe instances bound to *task_class* by the L1 catalog.

    Looks up :attr:`TaskClassProfile.runtime_probe_kinds` and resolves
    each kind through the in-process registry. Returns a tuple in
    catalog declaration order so the L3-2 verifier can deterministically
    iterate them.

    For v1, every class either binds :class:`HeadlessRunProbe` or a
    :class:`DeferredProbe`; the verifier sees a PASS from either, and
    the human reading the evidence bundle sees ``outcome = "deferred"``
    for the kinds that have not yet been implemented.
    """
    profile = TASK_CLASS_CATALOG[task_class]
    return tuple(_PROBE_REGISTRY[kind] for kind in profile.runtime_probe_kinds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PREVIEW_MAX_CHARS = 2_000


def _truncate(text: str) -> str:
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    return text[:_PREVIEW_MAX_CHARS] + f"… [truncated, {len(text) - _PREVIEW_MAX_CHARS} more chars]"
