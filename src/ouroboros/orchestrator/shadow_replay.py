"""Opt-in shadow-replay baseline harness (frugality-proof AC5).

The deterministic frugality proof
(:mod:`ouroboros.orchestrator.frugality_proof`) can only judge the hypothesis
"decomposed children run token-frugal at a cheaper tier" once every counted row
carries a BASELINE: *what would this child have cost at the PARENT's tier/effort?*
That baseline is the last unproduced axis
(:data:`~ouroboros.orchestrator.frugality_proof.EVENT_SHADOW_REPLAY`). This module
produces it by re-executing a decomposed child's exact prompt at the parent's
model tier and reasoning effort, measuring the token spend, and emitting
``execution.ac.shadow_replay``.

Default OFF, on purpose. Replaying doubles a child's token cost, so this is an
experiment harness — never a production default. It is gated by
``OUROBOROS_SHADOW_REPLAY`` (see :func:`shadow_replay_enabled_from_env`) read once
in the runner and threaded to the executor, and it runs fire-and-forget: any
failure degrades to a warning and never disturbs the real AC's result.

Workspace isolation (a HARD safety requirement)
------------------------------------------------
The baseline re-executes the same prompt WITH TOOLS, which would mutate the real
task workspace a second time. The baseline therefore always runs in a disposable
copy of the task cwd (:func:`isolated_workspace`): a detached ``git worktree`` at
the current HEAD when the cwd is a git repo, else a filtered ``shutil.copytree``.
If isolation cannot be established the replay is SKIPPED — the harness never
re-executes against the live workspace.

Why this harness does NOT set ``grounding_regression`` (the honest branch)
--------------------------------------------------------------------------
``grounding_regression`` on the deliver-verdict axis means "the child at the cheap
tier newly REJECTED a claim the parent-tier baseline ACCEPTED". Computing it needs
BOTH the child's deliver verdict (already produced by the live executor) AND a
BASELINE deliver verdict. A baseline deliver verdict requires a baseline
:class:`~ouroboros.harness.deliver_gate.EvidenceManifest`, and that manifest is
built by :func:`ouroboros.harness.journal.normalize_events` from RECORDED journal
events (``tool.call.started``/``tool.call.returned``/``llm.call.requested``/
``llm.call.returned``) — not from the raw ``AgentMessage`` stream this harness
consumes. The baseline runs via a bare ``adapter.execute_task`` in an isolated
workspace whose events are deliberately NOT recorded (recording them would splice
a second execution's evidence into the proof's event stream). There is no offline
normalizer from ``AgentMessage`` to an ``EvidenceManifest``, so a baseline verdict
is not computable here without standing up the full journal-emitting executor
against a throwaway store — well beyond this minimal harness.

Rather than FAKE the flag, this harness emits ``execution.ac.shadow_replay``
WITHOUT ``grounding_regression``. The proof then honestly reports the grounding
axis as unclosed for real runs (rows lack ``grounding_regression`` and are
excluded by ``FrugalityTriadRow.has_all_axes``), instead of asserting a
regression measurement it never made. The emitter DOES expose an optional
``grounding_regression`` parameter (see
:meth:`ExecutionEventEmitter.emit_deliver_verdict`) so the deterministic proof
machine can be exercised end-to-end and so a future baseline-manifest design can
close the axis without a contract change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

from ouroboros.core.worktree import is_git_repo
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.effort_routing import resolve_execute_effort
from ouroboros.orchestrator.model_routing import resolve_execute_model

if TYPE_CHECKING:
    from ouroboros.orchestrator.execution_runtime_scope import ACRuntimeIdentity
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor

log = get_logger(__name__)

# Env flag that arms the harness. Off unless explicitly enabled — replaying
# doubles token cost, so this can never be a silent production default.
SHADOW_REPLAY_ENV = "OUROBOROS_SHADOW_REPLAY"
_ENABLED_TOKENS = frozenset({"1", "true", "on"})

# ``baseline_mode`` recorded on every shadow-replay event; the proof stores it
# on the row for provenance (a future harness could add other baseline modes).
BASELINE_MODE = "shadow_replay"

# Hard cap on a single baseline re-execution so a stuck replay can never hang the
# (already-completed) real AC indefinitely. 15 min mirrors the long end of the
# executor's own dispatch budgets; the replay is awaited, so this bounds it.
_BASELINE_TIMEOUT_SECONDS = 900.0

# Bound for the git-worktree add/remove subprocesses used to isolate the cwd.
_GIT_TIMEOUT_SECONDS = 60.0

# Directories never worth copying into a non-git baseline workspace: they are
# large, regenerable, and never part of the task's source of truth. The ``.git``
# directory is likewise excluded — a copytree baseline does not need history, and
# copying it is both slow and risks corrupting index/lock state.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".next",
    "target",
)


def shadow_replay_enabled_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the shadow-replay harness is armed via ``OUROBOROS_SHADOW_REPLAY``.

    Enabled only for the explicit truthy tokens ``1``/``true``/``on`` (case- and
    whitespace-insensitive); anything else — including unset — stays OFF so the
    doubled-cost experiment is never entered by accident.
    """
    env = environ if environ is not None else os.environ
    return env.get(SHADOW_REPLAY_ENV, "").strip().lower() in _ENABLED_TOKENS


@contextlib.contextmanager
def isolated_workspace(cwd: str) -> Iterator[str | None]:
    """Yield a disposable copy of ``cwd`` for a baseline re-execution, else ``None``.

    A git repo is isolated with a detached ``git worktree`` pinned at the current
    HEAD — cheap, and it captures the committed starting point the child ran from.
    A non-git cwd is isolated with a filtered ``shutil.copytree``. The copy is
    always removed on exit (the worktree is unregistered with ``git worktree
    remove --force``; the temp tree is ``rmtree``'d). ``None`` is yielded when
    isolation cannot be established so the caller SKIPS the replay rather than
    ever running it against the live workspace.
    """
    base = tempfile.mkdtemp(prefix="ooo-shadow-replay-")
    target = Path(base) / "workspace"
    use_git = False
    established = False
    try:
        use_git = is_git_repo(cwd)
        if use_git:
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(target)],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
            established = result.returncode == 0
            if not established:
                log.warning(
                    "parallel_executor.ac.shadow_replay.worktree_add_failed",
                    cwd=cwd,
                    stderr=result.stderr.strip()[:500],
                )
        else:
            shutil.copytree(cwd, target, ignore=_COPY_IGNORE, symlinks=True)
            established = True
        yield str(target) if established else None
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.shadow_replay.isolation_error",
            cwd=cwd,
            error=str(exc),
        )
        yield None
    finally:
        if use_git and established:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(target)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=_GIT_TIMEOUT_SECONDS,
                    check=False,
                )
        shutil.rmtree(base, ignore_errors=True)


async def run_shadow_replay(
    executor: ParallelACExecutor,
    *,
    runtime_identity: ACRuntimeIdentity,
    execution_id: str,
    session_id: str,
    ac_index: int,
    is_sub_ac: bool,
    prompt: str,
    system_prompt: str,
    tools: list[str],
    decomposition_trustworthy: bool,
) -> None:
    """Re-run a decomposed child at the parent tier/effort and emit its baseline.

    HARD RULE: fire-and-forget. This never raises into the executor — every
    failure degrades to a warning — and it emits nothing unless a real baseline
    token spend was measured (missing usage stays missing, never fabricated).
    The caller awaits it, so the only cost to the real AC is the replay's own
    (isolated, timeout-bounded) runtime, which is acceptable in experiment mode.
    """
    try:
        await _run_shadow_replay_inner(
            executor,
            runtime_identity=runtime_identity,
            execution_id=execution_id,
            session_id=session_id,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            decomposition_trustworthy=decomposition_trustworthy,
        )
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.shadow_replay.failed",
            ac_id=runtime_identity.ac_id,
            error=str(exc),
        )


async def _run_shadow_replay_inner(
    executor: ParallelACExecutor,
    *,
    runtime_identity: ACRuntimeIdentity,
    execution_id: str,
    session_id: str,
    ac_index: int,
    is_sub_ac: bool,
    prompt: str,
    system_prompt: str,
    tools: list[str],
    decomposition_trustworthy: bool,
) -> None:
    # The hypothesis is only about decomposed children (a top-level AC has no
    # parent baseline). Guard here too, independent of the call site.
    if not is_sub_ac:
        return

    router = executor._model_router
    if router is None:
        # Model routing dormant → no parent tier to price the baseline at.
        log.debug(
            "parallel_executor.ac.shadow_replay.skipped_no_router",
            ac_id=runtime_identity.ac_id,
        )
        return

    # The parent-strength decision is exactly what a NON-decomposed (top-level)
    # unit would resolve to on this backend: base_tier + its model. Reusing the
    # live routing resolver keeps the baseline tier definitionally "the parent's".
    parent_decision, _ = resolve_execute_model(
        executor._adapter,
        router=router,
        is_decomposed_child=False,
        retry_attempt=0,
    )
    if parent_decision.model is None:
        log.debug(
            "parallel_executor.ac.shadow_replay.skipped_no_baseline_model",
            ac_id=runtime_identity.ac_id,
            baseline_tier=parent_decision.tier,
        )
        return

    cwd = executor._task_cwd or getattr(executor._adapter, "working_directory", None)
    if not cwd:
        log.warning(
            "parallel_executor.ac.shadow_replay.skipped_no_cwd",
            ac_id=runtime_identity.ac_id,
        )
        return

    backend = getattr(executor._adapter, "runtime_backend", None)

    with isolated_workspace(cwd) as isolated_cwd:
        if isolated_cwd is None:
            # Isolation failed — never replay against the live workspace.
            log.warning(
                "parallel_executor.ac.shadow_replay.skipped_isolation_failed",
                ac_id=runtime_identity.ac_id,
                cwd=cwd,
            )
            return

        baseline_spend = await _measure_baseline_spend(
            executor,
            backend=backend,
            model=parent_decision.model,
            isolated_cwd=isolated_cwd,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

    if baseline_spend is None:
        # No runtime usage telemetry observed — the proof treats missing as
        # missing and emits nothing rather than a fabricated baseline.
        log.debug(
            "parallel_executor.ac.shadow_replay.no_usage",
            ac_id=runtime_identity.ac_id,
        )
        return

    await executor._event_emitter.emit_shadow_replay(
        runtime_identity=runtime_identity,
        execution_id=execution_id,
        session_id=session_id,
        ac_index=ac_index,
        is_sub_ac=is_sub_ac,
        baseline_token_spend=baseline_spend,
        baseline_mode=BASELINE_MODE,
        baseline_model=parent_decision.model,
        baseline_tier=parent_decision.tier,
        decomposition_trustworthy=decomposition_trustworthy,
    )
    log.info(
        "parallel_executor.ac.shadow_replay.emitted",
        ac_id=runtime_identity.ac_id,
        baseline_token_spend=baseline_spend,
        baseline_model=parent_decision.model,
        baseline_tier=parent_decision.tier,
    )


async def _measure_baseline_spend(
    executor: ParallelACExecutor,
    *,
    backend: str | None,
    model: str,
    isolated_cwd: str,
    prompt: str,
    system_prompt: str,
    tools: list[str],
) -> float | None:
    """Run the baseline once in the isolated cwd and sum its runtime token spend.

    Builds a THROWAWAY runtime pinned to the parent-tier ``model`` and the
    isolated cwd (adapters pin cwd at construction, so a fresh runtime is how the
    baseline gets an isolated workspace), runs the same prompt at the parent's
    reasoning effort with a fresh session (``resume_handle=None``), and sums
    ``data["usage"]`` across the stream. Returns ``None`` when the runtime cannot
    be built or reports no usage — both are "no baseline", never a fabricated one.
    The baseline output is discarded; only the spend matters.
    """
    # Lazy imports break the import cycle (parallel_executor imports this module)
    # and mirror the executor's own lazy ``create_agent_runtime`` use.
    from ouroboros.orchestrator.parallel_executor import _harvest_token_spend
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    try:
        baseline_runtime = create_agent_runtime(
            backend=backend,
            model=model,
            cwd=isolated_cwd,
        )
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.shadow_replay.runtime_build_failed",
            backend=backend,
            model=model,
            error=str(exc),
        )
        return None

    # Parent-strength effort (NOT lowered): resolve against the baseline runtime so
    # the reasoning_effort kwarg is only passed when that runtime enforces it —
    # identical to how the live leaf lays itself on the effort capability contract.
    _, effort_kwargs = resolve_execute_effort(
        baseline_runtime,
        base_effort=executor._reasoning_effort,
        is_decomposed_child=False,
        retry_attempt=0,
    )

    messages: list = []
    # Do NOT break out of this loop: the SDK generator owns anyio cancel scopes
    # and closing it early can cancel sibling tasks (see the decomposition loop's
    # note). Let it complete; the timeout bounds a stuck baseline instead.
    async with asyncio.timeout(_BASELINE_TIMEOUT_SECONDS):
        async for message in baseline_runtime.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=None,
            **effort_kwargs,
        ):
            messages.append(message)

    harvested = _harvest_token_spend(messages)
    if harvested is None:
        return None
    baseline_token_spend, _breakdown = harvested
    return baseline_token_spend
