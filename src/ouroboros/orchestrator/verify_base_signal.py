"""Whether a failing ``verify_command`` carries any signal about the patch.

The verify gate rejects an acceptance criterion when its ``verify_command``
exits non-zero. A command that also exits non-zero on the untouched base says
nothing about the patch: a bare full-suite ``python -m pytest -q`` fails on any
repository with an unrelated failing test, and a command with a broken path
fails everywhere. Rejecting on it, and spending the retry budget on it, decides
the run by construction (50 of 150 product Seeds on Ambig-SWE; every executed
run with one ended ``failed`` although the grader resolved 19 of them).

Before a failing command can reject, the gate runs it once on a fresh copy of
the base snapshot the check package already takes before any worker starts
(``boundary.binding_flow.snapshot_base``). The snapshot is used only while its
tree still has the digest recorded when it was taken, so a worker cannot make
a command look signal-free by editing the snapshot. If the command fails on the
base as well, the gate records ``no_signal=True``: the criterion is not rejected and
not retried on that command. Without a snapshot, or when the base run cannot be
judged (timeout, start failure, digest mismatch), the gate keeps its usual
verdict.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from typing import Any

import structlog

from ouroboros.boundary.tree import copy_checkout, tree_digest
from ouroboros.orchestrator.verify_command_runner import run_with_shell
from ouroboros.orchestrator.verify_gate_outcome import _VerifyGateOutcome
from ouroboros.orchestrator.verify_shell import project_verify_environment

log = structlog.get_logger(__name__)


class VerifyBaseSignal:
    """Run a verify command once on the untouched base, cached per command."""

    def __init__(self, base_snapshot: Path | None = None, base_digest: str | None = None) -> None:
        self._base_snapshot = base_snapshot
        self._base_digest = base_digest
        self._fails_on_base: dict[str, bool] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_acceptance_authority(cls, authority: Any) -> VerifyBaseSignal:
        """Read the base snapshot and its pinned digest from a check-package authority."""
        state = getattr(authority, "state", None)
        snapshot = getattr(state, "base_snapshot", None)
        if not isinstance(snapshot, Path):
            return cls()
        admission = getattr(state, "admission", None)
        digest = getattr(admission, "base_tree_digest", None) or getattr(
            state, "base_snapshot_digest", None
        )
        return cls(snapshot, digest if isinstance(digest, str) else None)

    @property
    def available(self) -> bool:
        return self._base_snapshot is not None and self._base_digest is not None

    async def fails_on_base(self, command: str, *, shell_path: str, timeout_seconds: float) -> bool:
        """Return True only when ``command`` exits non-zero on the untouched base."""
        if not self.available:
            return False
        lock = self._locks.setdefault(command, asyncio.Lock())
        async with lock:
            if command not in self._fails_on_base:
                self._fails_on_base[command] = await self._run_on_base(
                    command, shell_path=shell_path, timeout_seconds=timeout_seconds
                )
            return self._fails_on_base[command]

    async def _run_on_base(self, command: str, *, shell_path: str, timeout_seconds: float) -> bool:
        assert self._base_snapshot is not None
        with tempfile.TemporaryDirectory(prefix="ooo-verify-base-") as scratch:
            base = Path(scratch) / "base"
            try:
                await asyncio.to_thread(copy_checkout, self._base_snapshot, base)
                digest = await asyncio.to_thread(tree_digest, base)
            except OSError as exc:
                log.warning("verify_gate.base_signal.unavailable", error=str(exc))
                return False
            if digest != self._base_digest:
                log.warning("verify_gate.base_signal.snapshot_changed")
                return False
            run = await run_with_shell(
                (shell_path, "-c", command),
                cwd=str(base),
                env=project_verify_environment(base),
                timeout_seconds=timeout_seconds,
            )
        judged = run.start_error is None and not run.timed_out
        fails = judged and run.returncode != 0
        log.info(
            "verify_gate.base_signal.measured",
            base_returncode=run.returncode if judged else None,
            base_judged=judged,
            fails_on_base=fails,
        )
        return fails


def no_signal_outcome(
    returncode: int, output_tail: str, workspace_digest: str | None, replay_required: bool
) -> _VerifyGateOutcome:
    """The gate outcome for a command that fails both on the base and after the patch."""
    return _VerifyGateOutcome(
        passed=True,
        reason=(
            f"verify_command exited with status {returncode} here and also exits non-zero "
            "on the untouched base snapshot; it carries no signal about this work"
        ),
        output_tail=output_tail,
        workspace_digest=workspace_digest,
        replay_required=replay_required,
        no_signal=True,
    )


__all__ = ["VerifyBaseSignal", "no_signal_outcome"]
