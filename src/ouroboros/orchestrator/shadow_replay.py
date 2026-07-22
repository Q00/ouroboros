"""Opt-in shadow-replay baseline harness (frugality-proof AC5).

The deterministic frugality proof
(:mod:`ouroboros.orchestrator.frugality_proof`) can only judge the hypothesis
"decomposed children run token-frugal at a cheaper tier" once every counted row
carries a BASELINE: *what would this child have cost at the PARENT's tier/effort?*
That opt-in baseline is the experiment-only axis
(:data:`~ouroboros.orchestrator.frugality_proof.EVENT_SHADOW_REPLAY`). This module
produces it by re-executing a decomposed child's workspace-remapped prompt at the
parent's model tier and reasoning effort, measuring the token spend, and emitting
``execution.ac.shadow_replay``.

Default OFF, on purpose. Replaying doubles a child's token cost, so this is an
experiment harness — never a production default. It is gated by
``OUROBOROS_SHADOW_REPLAY`` (see :func:`shadow_replay_enabled_from_env`) read once
in the runner and threaded to the executor, and it runs fire-and-forget: any
failure degrades to a warning and never disturbs the real AC's result.

Replay isolation (a HARD safety requirement)
---------------------------------------------
The baseline re-executes the same prompt WITH TOOLS, which would mutate the real
task workspace a second time. The executor therefore captures a disposable,
filtered filesystem snapshot of the task cwd (:func:`isolated_workspace`)
*immediately before* dispatching the live child, then gives that frozen snapshot
to the replay only after the child succeeds. This ordering matters: creating a
detached worktree after the live child finishes would silently drop both the
caller's pre-existing uncommitted files and the exact pre-child state, so the two
models would not receive the same input. Exact live-cwd strings in the generated
prompt are remapped to the snapshot path.

The snapshot deliberately excludes ``.git`` instead of sharing a worktree's Git
metadata with an untrusted baseline agent. A shared object database/ref namespace
would let replayed tools mutate the live repository even when source files live in
another directory. The trade-off is explicit: prompts whose behavior depends on
Git history/status cannot be compared faithfully by this harness and should be
treated as unsupported experiment inputs. Ordinary tracked modifications and
untracked task files are copied, while large regenerable dependency/cache folders
are filtered. If isolation cannot be established (including an escaping symlink),
the replay is SKIPPED — the harness never re-executes against the live workspace.

The copy is input state, not a security boundary. A replay runtime must separately
attest strict read/write confinement to that cwd *and* isolation of network, MCP,
API, deployment, messaging, database, and every other external side-effect path.
No bundled production runtime currently attests both, so all of them skip replay
and emit no baseline. Likewise, ACs with ``verify_command`` are unsupported: a
host-side shell would sit outside the runtime sandbox and could escape via an
absolute path or ``cd ../..``.

How grounding regression stays honest without a baseline journal
-----------------------------------------------------------------
``grounding_regression`` on the deliver-verdict axis means "the child at the cheap
tier newly REJECTED a claim the parent-tier baseline ACCEPTED". Computing it needs
BOTH the child's deliver verdict (already produced by the live executor) AND a
BASELINE deliver verdict. A baseline deliver verdict requires a baseline
:class:`~ouroboros.harness.deliver_gate.EvidenceManifest`, and that manifest is
built from recorded journal events. The live path may additionally admit an
accepted attempt's ``execution.tool.started`` rows, but only after typed evidence
exactly matches them and the independent harness verifier passes. Mutation starts
also require one correlated successful completion. The baseline
runs via a bare ``adapter.execute_task`` in an isolated workspace whose events are
deliberately NOT recorded (recording them would splice a second execution's
evidence into the proof's event stream). The raw baseline stream does pass the
fat-harness typed-evidence and transcript verifier against the snapshot, but there
is no throwaway journal/TraceGuard manifest, so a paired baseline deliver verdict
is not computable without expanding the experiment harness substantially.

Rather than fake a paired baseline verdict, the live deliver-verdict producer
uses an explicit fail-closed policy. A journal-grounded accepted child records
``grounding_regression=false`` because it was not rejected at the lower tier. A
rejected child records ``grounding_regression=true`` conservatively: without a
baseline journal Ouroboros cannot prove the parent would also reject it, so the
zero-regression veto wins. The event records
``grounding_regression_mode=fail_closed_live_traceguard`` to distinguish this
policy from a future exact paired-verdict comparison. This replay module remains
responsible only for the paired token baseline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
import contextlib
import inspect
import os
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.effort_routing import resolve_execute_effort
from ouroboros.orchestrator.model_routing import resolve_execute_model

if TYPE_CHECKING:
    from ouroboros.core.seed import AcceptanceCriterionSpec
    from ouroboros.orchestrator.adapter import AgentMessage
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

# A replay runtime must explicitly attest that *all* file-capable execution
# paths (built-in shell/edit tools and any extension/MCP tools) are confined to
# its supplied cwd for both reads and writes. No bundled production runtime
# currently makes this stronger-than-workspace-write guarantee, so shadow replay
# remains safely disabled there until a runtime implements the contract.
STRICT_FILESYSTEM_ISOLATION = "strict-cwd-read-write"
STRICT_EXTERNAL_EFFECT_ISOLATION = "no-network-no-external-side-effects"

# Hard cap on a single baseline re-execution so a stuck replay can never hang the
# (already-completed) real AC indefinitely. 15 min mirrors the long end of the
# executor's own dispatch budgets; the replay is awaited, so this bounds it.
_BASELINE_TIMEOUT_SECONDS = 900.0

# A runtime may own a persistent subprocess/session pool (notably codex-mcp).
# Teardown is best-effort but bounded so a wedged closer cannot defeat the replay
# timeout and hold the already-completed live AC open indefinitely.
_RUNTIME_CLOSE_TIMEOUT_SECONDS = 15.0

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
    """Yield a frozen disposable copy of ``cwd``, or ``None`` on unsafe isolation.

    The copy captures the live filesystem state at context entry, including
    ordinary uncommitted tracked files and untracked task files. ``.git`` and
    large regenerable dependency/cache directories are excluded. Omitting Git
    metadata is intentional: a detached worktree still shares refs/objects with
    the live repository, so an agent with Bash access could mutate live Git state.

    Symlinks are preserved only when their resolved target remains inside the
    disposable snapshot. An absolute or escaping link would let replayed tools
    reach the live filesystem, so the entire snapshot is rejected and ``None`` is
    yielded. The temp tree is always removed on exit.
    """
    base: str | None = None
    try:
        try:
            base = tempfile.mkdtemp(prefix="ooo-shadow-replay-")
            target = Path(base) / "workspace"
            shutil.copytree(cwd, target, ignore=_COPY_IGNORE, symlinks=True)
            has_escaping_symlink = _snapshot_has_escaping_symlink(target)
        except Exception as exc:
            log.warning(
                "parallel_executor.ac.shadow_replay.isolation_error",
                cwd=cwd,
                error=str(exc),
            )
            yield None
            return

        if has_escaping_symlink:
            log.warning(
                "parallel_executor.ac.shadow_replay.unsafe_symlink",
                cwd=cwd,
            )
            yield None
            return

        # Keep the yield outside the setup-exception handler. Exceptions raised by
        # the caller's replay body must propagate to its own fire-and-forget guard,
        # not be mistaken for a copy failure (a contextmanager may yield only once).
        yield str(target)
    finally:
        if base is not None:
            shutil.rmtree(base, ignore_errors=True)


def _snapshot_has_escaping_symlink(root: Path) -> bool:
    """Return whether any copied symlink resolves outside ``root``.

    ``copytree(..., symlinks=True)`` does not dereference links, so checking the
    completed snapshot before yielding it is enough to prevent a baseline runtime
    from following a link back into the live workspace. Broken links are resolved
    non-strictly; a broken path that still stays under ``root`` is safe to retain.
    """
    resolved_root = root.resolve()
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(current_root) / name
            if not candidate.is_symlink():
                continue
            try:
                resolved_target = candidate.resolve(strict=False)
                resolved_target.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                return True
    return False


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
    ac_content: str,
    ac_spec: AcceptanceCriterionSpec | None = None,
    isolated_cwd: str | None = None,
    suggested_tier: str | None = None,
) -> None:
    """Re-run a decomposed child at the parent tier/effort and emit its baseline.

    HARD RULE: fire-and-forget. This never raises into the executor — every
    failure degrades to a warning — and it emits nothing unless a real baseline
    token spend was measured (missing usage stays missing, never fabricated).
    The caller awaits it, so the only cost to the real AC is the replay's own
    (isolated, timeout-bounded) runtime, which is acceptable in experiment mode.
    ``isolated_cwd`` must be the snapshot captured before the live dispatch;
    ``None`` is an isolation failure and always skips rather than recopying the
    already-mutated live workspace. ``suggested_tier`` is the same execution-profile
    starting tier used by the live child; replaying without it could compare a
    profile-pinned frugal child against the router's stronger default and fabricate
    a tier reduction that never occurred.
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
            ac_content=ac_content,
            ac_spec=ac_spec,
            isolated_cwd=isolated_cwd,
            suggested_tier=suggested_tier,
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
    ac_content: str,
    ac_spec: AcceptanceCriterionSpec | None,
    isolated_cwd: str | None,
    suggested_tier: str | None,
) -> None:
    # The hypothesis is only about decomposed children (a top-level AC has no
    # parent baseline). Guard here too, independent of the call site.
    if not is_sub_ac:
        return

    if not decomposition_trustworthy:
        log.debug(
            "parallel_executor.ac.shadow_replay.skipped_untrusted_decomposition",
            ac_id=runtime_identity.ac_id,
        )
        return

    # This experiment has its own ephemeral workspace and never decides the
    # live AC's acceptance, but it still must not sidestep the executor's six
    # captured entry roots. Import lazily to avoid the module cycle: the
    # executor imports this harness at module load time.
    from ouroboros.orchestrator.parallel_executor import _invoke_execution_authority_guard

    _invoke_execution_authority_guard(executor)

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
        retry_attempt=runtime_identity.retry_attempt,
        suggested_tier=suggested_tier,
    )
    if parent_decision.model is None:
        log.debug(
            "parallel_executor.ac.shadow_replay.skipped_no_baseline_model",
            ac_id=runtime_identity.ac_id,
            baseline_tier=parent_decision.tier,
        )
        return

    if isolated_cwd is None:
        log.warning(
            "parallel_executor.ac.shadow_replay.skipped_no_snapshot",
            ac_id=runtime_identity.ac_id,
        )
        return

    backend = getattr(executor._adapter, "runtime_backend", None)
    baseline_spend = await _measure_baseline_spend(
        executor,
        backend=backend,
        model=parent_decision.model,
        isolated_cwd=isolated_cwd,
        prompt=prompt,
        system_prompt=system_prompt,
        tools=tools,
        retry_attempt=runtime_identity.retry_attempt,
        ac_content=ac_content,
        ac_spec=ac_spec,
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
    retry_attempt: int,
    ac_content: str,
    ac_spec: AcceptanceCriterionSpec | None,
) -> float | None:
    """Run the baseline once in the isolated cwd and sum its runtime token spend.

    Builds a THROWAWAY runtime pinned to the parent-tier ``model`` and the
    isolated cwd while preserving the live adapter's permission/LLM-backend
    context (adapters pin cwd at construction, so a fresh runtime is how the
    baseline gets an isolated workspace). The runtime is never executed unless
    it explicitly advertises strict cwd read/write confinement plus isolation of
    network and every external side-effect tool. The generated prompt's live cwd
    is remapped to the snapshot path, then replayed at the parent's reasoning
    effort with a fresh session (``resume_handle=None``).

    Usage is accepted only when the stream ends in one unambiguous successful
    terminal *and* the baseline independently passes the same fat-harness typed
    evidence schema plus transcript verifier, bound to ``isolated_cwd``. Safe,
    path-contained expected-artifact checks must also pass. ACs with a shell
    ``verify_command`` are unsupported until that command has an independent
    sandbox. A runtime error, unsupported isolation contract, semantic/evidence
    rejection, missing/ambiguous terminal, or missing usage all return ``None`` —
    each is "no baseline", never a fabricated denominator.
    Any runtime that exposes ``aclose`` is torn down in ``finally`` (bounded and
    best-effort).
    """
    # Lazy imports break the import cycle (parallel_executor imports this module)
    # and mirror the executor's own lazy ``create_agent_runtime`` use.
    from ouroboros.orchestrator.parallel_executor import _harvest_token_spend
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    try:
        baseline_runtime = create_agent_runtime(
            backend=backend,
            permission_mode=_runtime_context_string(executor._adapter, "permission_mode"),
            model=model,
            cwd=isolated_cwd,
            llm_backend=_runtime_context_string(executor._adapter, "llm_backend"),
        )
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.shadow_replay.runtime_build_failed",
            backend=backend,
            model=model,
            error=str(exc),
        )
        return None

    try:
        if not _runtime_has_strict_replay_isolation(baseline_runtime, isolated_cwd):
            log.warning(
                "parallel_executor.ac.shadow_replay.unsupported_isolation",
                backend=backend,
                model=model,
            )
            return None

        # Parent-strength effort (NOT lowered): resolve against the baseline runtime so
        # the reasoning_effort kwarg is only passed when that runtime enforces it —
        # identical to how the live leaf lays itself on the effort capability contract.
        _, effort_kwargs = resolve_execute_effort(
            baseline_runtime,
            base_effort=executor._reasoning_effort,
            is_decomposed_child=False,
            retry_attempt=retry_attempt,
        )

        messages: list[AgentMessage] = []
        baseline_prompt = _remap_prompt_workspace(
            prompt,
            source_cwd=_runtime_source_cwd(executor),
            isolated_cwd=isolated_cwd,
        )
        # Do NOT break out of this loop: the SDK generator owns anyio cancel scopes
        # and closing it early can cancel sibling tasks (see the decomposition loop's
        # note). Let it complete; the timeout bounds a stuck baseline instead.
        async with asyncio.timeout(_BASELINE_TIMEOUT_SECONDS):
            async for message in baseline_runtime.execute_task(
                prompt=baseline_prompt,
                tools=tools,
                system_prompt=system_prompt,
                resume_handle=None,
                **effort_kwargs,
            ):
                messages.append(message)

        if not _has_unambiguous_successful_terminal(messages):
            log.debug(
                "parallel_executor.ac.shadow_replay.baseline_not_successful",
                backend=backend,
                model=model,
            )
            return None

        if not await _baseline_semantically_accepted(
            executor,
            messages=messages,
            ac_content=ac_content,
            ac_spec=ac_spec,
            isolated_cwd=isolated_cwd,
        ):
            log.debug(
                "parallel_executor.ac.shadow_replay.baseline_evidence_rejected",
                backend=backend,
                model=model,
            )
            return None

        harvested = _harvest_token_spend(messages)
        if harvested is None:
            return None
        baseline_token_spend, _breakdown = harvested
        return baseline_token_spend
    finally:
        await _close_runtime_best_effort(baseline_runtime)


def _has_unambiguous_successful_terminal(messages: list[AgentMessage]) -> bool:
    """Return whether a runtime stream proves one explicit successful outcome.

    ``AgentMessage.is_final`` / ``is_error`` are the adapter-level lifecycle
    contract shared by runtimes. Requiring exactly one final message, requiring it
    to be the last message, and allowlisting ``subtype=success`` keeps malformed,
    incomplete, and contradictory streams fail-closed. In particular, token usage
    attached to a terminal error must never become a shadow-replay denominator.
    """
    terminal_messages = [message for message in messages if message.is_final]
    if len(terminal_messages) != 1:
        return False

    terminal = terminal_messages[0]
    if messages[-1] is not terminal:
        return False
    if terminal.is_error or terminal.data.get("subtype") != "success":
        return False

    # Claude SDK results also carry ``is_error``. A contradictory or malformed
    # value makes the outcome ambiguous even when ``subtype`` says success.
    is_error = terminal.data.get("is_error")
    return is_error is None or is_error is False


def _runtime_has_strict_replay_isolation(runtime: object, isolated_cwd: str) -> bool:
    """Return whether a throwaway runtime attests complete replay isolation.

    Filesystem confinement alone is insufficient: an MCP/API/deploy/Slack/DB
    tool could repeat an external side effect even while the cwd is sandboxed.
    The runtime must separately attest that network and every extension tool are
    disabled or redirected to an isolated test double for this invocation.
    """
    if getattr(runtime, "shadow_replay_filesystem_isolation", None) != STRICT_FILESYSTEM_ISOLATION:
        return False
    if (
        getattr(runtime, "shadow_replay_external_effect_isolation", None)
        != STRICT_EXTERNAL_EFFECT_ISOLATION
    ):
        return False
    runtime_cwd = getattr(runtime, "working_directory", None)
    if not isinstance(runtime_cwd, str) or not runtime_cwd.strip():
        runtime_cwd = getattr(runtime, "cwd", None)
    if not isinstance(runtime_cwd, str) or not runtime_cwd.strip():
        return False
    try:
        return Path(runtime_cwd).resolve(strict=False) == Path(isolated_cwd).resolve(strict=False)
    except OSError:
        return False


def _runtime_source_cwd(executor: ParallelACExecutor) -> str | None:
    """Return the live cwd embedded by AtomicPromptBuilder, when known."""
    task_cwd = getattr(executor, "_task_cwd", None)
    if isinstance(task_cwd, str) and task_cwd:
        return task_cwd
    return _runtime_context_string(executor._adapter, "working_directory")


def _remap_prompt_workspace(prompt: str, *, source_cwd: str | None, isolated_cwd: str) -> str:
    """Remap exact live-workspace references to the frozen snapshot path."""
    if source_cwd is None:
        return prompt
    candidates = {source_cwd}
    try:
        candidates.add(str(Path(source_cwd).resolve(strict=False)))
    except OSError:
        pass
    remapped = prompt
    for candidate in sorted((value for value in candidates if value), key=len, reverse=True):
        remapped = remapped.replace(candidate, isolated_cwd)
    return remapped


async def _baseline_semantically_accepted(
    executor: ParallelACExecutor,
    *,
    messages: list[AgentMessage],
    ac_content: str,
    ac_spec: AcceptanceCriterionSpec | None,
    isolated_cwd: str,
) -> bool:
    """Run fat-harness evidence + verifier (+ contract gate) for a baseline."""
    if not executor._fat_harness_mode or executor._execution_profile is None:
        return False
    # The runtime's sandbox cannot confine this host-side shell command. Until
    # verify commands have their own independently attested sandbox, executing
    # one here could mutate an absolute live path or escape via ``cd ../..``.
    if ac_spec is not None and ac_spec.verify_command:
        return False
    terminal = messages[-1]
    has_success_contract = ac_spec is not None and bool(ac_spec.verify_command)
    has_expected_artifacts = ac_spec is not None and bool(ac_spec.expected_artifacts)
    verify_gate_active = executor._run_verify_commands
    typed_evidence, typed_validation, typed_error = executor._observe_atomic_typed_evidence(
        ac_content=ac_content,
        final_message=terminal.content,
        success=True,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
    )
    # Do not look up ``executor._run_atomic_verifier_pass`` dynamically here.
    # The live executor registered a constructor-captured root, and the registry
    # both revalidates that root and invokes it directly.
    from ouroboros.orchestrator.parallel_executor import (
        _FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS,
        _invoke_execution_authority_entry,
    )

    verifier_verdict = _invoke_execution_authority_entry(
        executor,
        _FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS,
        ac_content=ac_content,
        final_message=terminal.content,
        success=True,
        messages=tuple(messages),
        typed_evidence=typed_evidence,
        typed_validation=typed_validation,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
        force_runtime_transcript=True,
        task_cwd_override=isolated_cwd,
    )
    if (
        executor._fat_harness_acceptance_error(
            runtime_success=True,
            typed_evidence=typed_evidence,
            typed_validation=typed_validation,
            typed_error=typed_error,
            verifier_verdict=verifier_verdict,
        )
        is not None
    ):
        return False
    if ac_spec is not None and verify_gate_active and ac_spec.expected_artifacts:
        # Import lazily with the executor cycle already resolved. This is a pure,
        # path-contained existence check; no baseline-controlled subprocess runs.
        from ouroboros.orchestrator.parallel_executor import _missing_expected_artifacts

        return not _missing_expected_artifacts(ac_spec.expected_artifacts, isolated_cwd)
    return True


def _runtime_context_string(adapter: object, name: str) -> str | None:
    """Read a non-blank runtime context string without leaking mock/sentinel values."""
    value = getattr(adapter, name, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _close_runtime_best_effort(runtime: object) -> None:
    """Bounded best-effort teardown for a throwaway baseline runtime."""
    closer = getattr(runtime, "aclose", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            async with asyncio.timeout(_RUNTIME_CLOSE_TIMEOUT_SECONDS):
                await result
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.shadow_replay.runtime_close_failed",
            runtime_backend=getattr(runtime, "runtime_backend", None),
            error=str(exc),
        )
