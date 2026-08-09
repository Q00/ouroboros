"""Anonymous usage telemetry (PostHog).

Privacy contract — see TELEMETRY.md at the repository root:

- Never collects code, prompts, seed content, file paths, or arguments.
  Only event names and coarse properties (command, backend, version, os,
  duration, success) are sent.
- Identity is a random UUID stored in ``~/.ouroboros/telemetry.json``.
  No PII, no machine fingerprinting.
- Opt out any time: ``DO_NOT_TRACK=1``, ``OUROBOROS_TELEMETRY=0``, or
  ``telemetry.enabled: false`` in ``~/.ouroboros/config.yaml``.
- Fire-and-forget: events go through a bounded queue drained by a daemon
  thread using stdlib urllib. Telemetry never raises, never blocks the
  caller, and silently drops events on any failure. The worker thread is a
  daemon, so process exit never waits on it either: events still queued or
  in flight when the process terminates are dropped, not delivered.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import random
import ssl
import threading
import time
from typing import Any
import urllib.request
import uuid

from ouroboros import __version__

# PostHog project API key. This is a *public, write-only* key (it can only
# ingest events, never read them) — embedding it in an open-source repo is
# the documented PostHog pattern. An empty constant (e.g. stripped in a fork)
# disables telemetry entirely; a blank/unset OUROBOROS_POSTHOG_API_KEY env
# var does not — it just falls back to this embedded key (see _api_key()).
_EMBEDDED_API_KEY = "phc_mSoetD4ExLDDCi3vNua635NhwRTgHfRaCG9WYNKmrvv5"
_DEFAULT_HOST = "https://us.i.posthog.com"

_QUEUE_MAX = 256
_BATCH_MAX = 25
_HTTP_TIMEOUT_SECONDS = 4.0

# Tools that skills poll in loops (job status, HUDs, projections). Captured
# at 1/_POLL_SAMPLE_RATE via an independent per-call random draw (not a
# per-process counter) with a ``sample_rate`` property so absolute counts
# can be re-weighted in PostHog without flooding ingestion. A per-process
# counter would always capture call #1, so short-lived processes (a fresh
# MCP session that polls once or twice before exiting) would be captured at
# ~1:1 instead of 1/50 -- an up-to-50x over-count skewed toward exactly the
# processes least representative of steady-state usage. A per-call draw
# keeps the probability 1/50 regardless of how long the process lives.
_POLLING_TOOLS = frozenset(
    {
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_session_status",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_ac_dashboard",
        "ouroboros_ac_tree_hud",
        "ouroboros_session_signal_targets",
        "ouroboros_lineage_status",
        "ouroboros_project_status",
    }
)
_POLL_SAMPLE_RATE = 50
# Module-level so a test can seed it for a deterministic capture/skip
# pattern (telemetry._poll_rng.seed(...)); a fresh Random() per call would
# make sampling untestable, and reusing threading._lock's RNG would couple
# unrelated concerns. random.Random() is not cryptographically secure, which
# is fine here -- this is a sampling coin flip, not a security boundary.
_poll_rng = random.Random()

# Funnel step per MCP tool. Everything else is still captured (tool property)
# but these get a stable ``command`` value so the interview -> seed -> run ->
# evaluate -> evolve funnel can be built without knowing tool names.
_TOOL_FUNNEL: dict[str, str] = {
    "ouroboros_interview": "interview",
    "ouroboros_pm_interview": "pm",
    "ouroboros_generate_seed": "seed",
    "ouroboros_execute_seed": "run",
    "ouroboros_start_execute_seed": "run",
    "ouroboros_evolve_step": "evolve",
    "ouroboros_start_evolve_step": "evolve",
    "ouroboros_evolve_rewind": "evolve",
    "ouroboros_auto": "auto",
    "ouroboros_start_auto": "auto",
    "ouroboros_evaluate": "evaluate",
    "ouroboros_start_evaluate": "evaluate",
    "ouroboros_qa": "qa",
    "ouroboros_ralph": "ralph",
    "ouroboros_start_ralph": "ralph",
    "ouroboros_lateral_think": "unstuck",
}

# CLI subcommand -> funnel command normalization; None means "do not capture".
# "mcp" is excluded because hosts (Claude Code, Codex) spawn `ouroboros mcp
# serve` automatically per session — counting it as a terminal command would
# inflate direct-CLI usage. Serve boots emit `mcp_serve_started` instead.
_CLI_SKIP = frozenset({"dispatch", "job", "mcp"})
_CLI_FUNNEL = {"init": "interview"}

# These handlers acknowledge durable background-job submission. Their return
# value says only whether work was accepted, never whether the workflow later
# completed or passed verification. Keeping them structurally separate avoids
# turning queue acceptance into a successful run in analytics.
_ASYNC_SUBMISSION_TOOLS = frozenset(
    {
        "ouroboros_start_execute_seed",
        "ouroboros_start_evolve_step",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_ralph",
    }
)

_JOB_FUNNEL: dict[str, str] = {
    "execute_seed": "run",
    "evolve_step": "evolve",
    "auto": "auto",
    "evaluate": "evaluate",
    "ralph": "ralph",
}

# Executable form of the TELEMETRY.md event table. Auditing call sites for
# compliance is not the same as enforcing it: capture() and set_context()
# below drop anything outside these sets before it reaches the queue, so a
# call site cannot accidentally (or maliciously) smuggle an unlisted event
# name or property -- e.g. prompt text or a file path -- into an outgoing
# batch, even if a future edit passes one in.
#
# TELEMETRY.md's "What is sent" table and the sets below are one contract in
# two places (SSOT pairing) -- edit both together, or the doc lies. Sets are
# literal per (event, variant), not composed from a shared base + context
# pool: variants deliberately differ in what they carry (e.g.
# mcp_serve_started omits python_version; the cli command_run variant omits
# every backend/provider context key entirely), so a generic union would
# either under- or over-grant for at least one variant.
_CONTEXT_ALLOWLIST = frozenset(
    {
        "runtime_backend",
        "execute_runtime_backend",
        "interview_llm_backend",
        "evaluate_llm_backend",
    }
)
_COMMAND_RUN_MCP_KEYS = frozenset(
    {
        "command",
        "tool",
        "source",
        "is_funnel",
        "phase",
        "accepted",
        "ok",
        "duration_ms",
        "error_type",
        "sample_rate",
        "runtime_backend",
        "execute_runtime_backend",
        "interview_llm_backend",
        "evaluate_llm_backend",
        "frontdoor",
        "app_version",
        "os",
        "python_version",
        "ci",
    }
)
_COMMAND_RUN_CLI_KEYS = frozenset(
    {
        "command",
        "source",
        "is_funnel",
        "app_version",
        "os",
        "python_version",
        "frontdoor",
        "ci",
    }
)
_WORKFLOW_OUTCOME_KEYS = frozenset(
    {
        "command",
        "phase",
        "terminal_status",
        "ok",
        "verified",
        "final_approved",
        "$insert_id",
        "runtime_backend",
        "execute_runtime_backend",
        "interview_llm_backend",
        "evaluate_llm_backend",
        "app_version",
        "os",
        "python_version",
        "frontdoor",
        "ci",
    }
)
_MCP_SERVE_STARTED_KEYS = frozenset(
    {"transport", "tool_count", "frontdoor", "app_version", "os", "ci"}
)
# Bound on any single string property. Dropped, not truncated -- a truncated
# value could still leak the start of a prompt or path.
_MAX_PROPERTY_STR_LEN = 200

_lock = threading.Lock()
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_context: dict[str, Any] = {}
_state_cache: dict[str, Any] | None = None


def _api_key() -> str:
    return os.environ.get("OUROBOROS_POSTHOG_API_KEY", "").strip() or _EMBEDDED_API_KEY


def _host() -> str:
    return os.environ.get("OUROBOROS_POSTHOG_HOST", "").strip() or _DEFAULT_HOST


def is_enabled() -> bool:
    """Whether telemetry may send events (key present + user has not opted out)."""
    if not _api_key():
        return False
    try:
        from ouroboros.config.loader import get_telemetry_enabled

        return get_telemetry_enabled()
    except Exception:
        return False


def _state_path() -> Path:
    return Path.home() / ".ouroboros" / "telemetry.json"


def _read_valid_state(path: Path) -> dict[str, Any] | None:
    """Read and validate telemetry.json, or None if absent/unparseable/empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(raw, dict) and isinstance(raw.get("distinct_id"), str) and raw["distinct_id"]:
        return raw
    return None


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    """Write state via a same-directory temp file + os.replace (atomic swap).

    Unlike a direct ``path.write_text``, this can never leave a truncated or
    half-written file on disk for another process to read mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.{os.getpid()}.tmp"
    try:
        tmp_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _load_state() -> dict[str, Any]:
    global _state_cache
    with _lock:
        if _state_cache is not None:
            return _state_cache
        path = _state_path()
        # Distinguish ABSENT (nothing to fix; race is over who creates it
        # first) from INVALID (a file exists but is corrupt/unparseable; a
        # create-if-not-exists primitive cannot fix that -- see
        # _repair_state).
        file_absent = False
        raw_text: str | None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw_text = None
            file_absent = True
        except Exception:
            raw_text = None  # exists but unreadable -- treat as invalid, not absent

        state: dict[str, Any] = {}
        if raw_text is not None:
            try:
                raw = json.loads(raw_text)
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("distinct_id"), str)
                    and raw["distinct_id"]
                ):
                    state = raw
            except Exception:
                state = {}

        if not state.get("distinct_id"):
            candidate = {
                "distinct_id": str(uuid.uuid4()),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "notice_shown": False,
            }
            state = (
                _publish_new_state(path, candidate)
                if file_absent
                else _repair_state(path, candidate)
            )
        _state_cache = state
        return state


def _publish_new_state(path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish a freshly generated identity, then adopt the winner.

    Concurrent processes (multiple MCP sessions starting at once, the
    installer racing a first `ooo` invocation) can all observe a missing
    telemetry.json in the same instant. A plain read-then-write here would
    let each one mint its own uuid and overwrite the file, so different
    processes would end up caching different ids while only one survives on
    disk. Instead: publish the candidate via a same-directory temp file plus
    `os.link` (atomic create-if-not-exists — the content is fully written
    before the link is made, so there is no partial-write window on this
    path), falling back to O_CREAT|O_EXCL on filesystems without hard-link
    support. Either way, every process then re-reads the file and adopts
    whatever `distinct_id` actually landed there, rather than trusting its
    own candidate. This is the process-local half of the fix; the installer
    coordinates with the same create-if-not-exists protocol.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return candidate

    payload = json.dumps(candidate, indent=2) + "\n"
    tmp_path = path.parent / f"telemetry.json.{os.getpid()}.tmp"
    try:
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.link(tmp_path, path)
        except FileExistsError:
            pass  # another process already published first; adopt it below
        except OSError:
            # No hard-link support (e.g. some network mounts) — fall back to
            # an atomic create-exclusive write. This has a brief
            # partial-write window between open and close, covered by the
            # bounded re-read retry below.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload.encode("utf-8"))
                finally:
                    os.close(fd)
            except FileExistsError:
                pass
            except Exception:
                pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    for attempt in range(3):
        state = _read_valid_state(path)
        if state is not None:
            return state
        if attempt < 2:
            time.sleep(0.02)
    return candidate


def _repair_state(path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    """Atomically replace a corrupt/invalid telemetry.json, then adopt the winner.

    ``_publish_new_state``'s create-if-absent primitives (``os.link`` /
    ``O_EXCL``) both refuse when the target already exists, so neither can
    fix a file that is present but unparseable or missing ``distinct_id`` --
    every process would otherwise mint its own uuid on every call, forever,
    and no id would ever persist. Exactly one process wins a repair lock,
    atomically replaces the bad file, and every process (winner and losers
    alike) adopts whatever ends up on disk afterward. Without that, retention
    and weekly-active metrics would fragment across as many ids as there were
    racing processes, every time the file happened to get corrupted.
    """
    lock_path = path.with_name(path.name + ".repair.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except Exception:
        fd = None  # lock held by another repairer, or lock creation failed

    if fd is not None:
        try:
            # Re-validate under the lock: another process may have repaired
            # the file between our first read and acquiring this lock.
            state = _read_valid_state(path)
            if state is None:
                try:
                    _atomic_write(path, candidate)
                except Exception:
                    pass
                state = _read_valid_state(path) or candidate
        finally:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
        return state

    # Someone else holds the lock: wait, bounded, for a valid file to land.
    for _ in range(10):
        time.sleep(0.02)
        state = _read_valid_state(path)
        if state is not None:
            return state

    # Lock never cleared (stale lock from a killed process) -- take over the
    # replace ourselves rather than waiting forever, accepting the tiny race.
    try:
        _atomic_write(path, candidate)
    except Exception:
        pass
    for attempt in range(3):
        state = _read_valid_state(path)
        if state is not None:
            return state
        if attempt < 2:
            time.sleep(0.02)
    return candidate


def _write_state(state: dict[str, Any]) -> None:
    try:
        _atomic_write(_state_path(), state)
    except Exception:
        pass


def distinct_id() -> str:
    """Stable anonymous id for this machine/user."""
    return str(_load_state()["distinct_id"])


def _is_allowed_scalar(value: Any) -> bool:
    """Whether a property value is a plain scalar within the size bound.

    Dicts, lists, and other structured values are rejected outright: the
    whitelist covers keys, but an allowed key with an unbounded/nested value
    would still be a hole for the same reason unlisted keys are.
    """
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_PROPERTY_STR_LEN
    return False


def _sanitize_properties(props: dict[str, Any], allowed_keys: frozenset[str]) -> dict[str, Any]:
    """Keep only allowlisted keys with plain, bounded scalar values."""
    return {k: v for k, v in props.items() if k in allowed_keys and _is_allowed_scalar(v)}


def set_context(**props: Any) -> None:
    """Merge properties (e.g. runtime_backend) into every subsequent event.

    Only keys in _CONTEXT_ALLOWLIST are accepted: this is a global sink read
    by _base_properties() on every capture() call, so an unvetted key here
    would leak into every event, not just one call site's.
    """
    with _lock:
        for key, value in props.items():
            if key in _CONTEXT_ALLOWLIST and _is_allowed_scalar(value):
                _context[key] = value


def _detect_frontdoor() -> str | None:
    """Best-effort detection of the host CLI that spawned this process."""
    env = os.environ
    if env.get("CLAUDECODE"):
        return "claude"
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        return "codex"
    return None


def _base_properties() -> dict[str, Any]:
    props: dict[str, Any] = {
        "app_version": __version__,
        "os": platform.system().lower(),
        "python_version": platform.python_version(),
    }
    frontdoor = _detect_frontdoor()
    if frontdoor:
        props["frontdoor"] = frontdoor
    # CI runs are excluded from the published counting rule (TELEMETRY.md);
    # stamping them lets every insight filter ci != true.
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        props["ci"] = True
    with _lock:
        props.update(_context)
    return props


def _post(events: list[dict[str, Any]]) -> None:
    api_key = _api_key()
    if not api_key or not events:
        return
    payload = json.dumps({"api_key": api_key, "batch": events}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https scheme
        f"{_host()}/batch/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(  # noqa: S310 - fixed https scheme
        request,
        timeout=_HTTP_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    ):
        pass


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        batch = [item]
        try:
            while len(batch) < _BATCH_MAX:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            _post(batch)
        except Exception:
            pass
        finally:
            for _ in batch:
                _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="ouroboros-telemetry", daemon=True)
            _worker.start()


def flush(timeout: float = 1.5) -> None:
    """Best-effort wait for queued events to be sent (bounded, never raises).

    Not called automatically at process exit — the worker thread is a daemon,
    so the process can terminate with events still queued or in flight, and
    those are silently dropped. Callers that need delivery before exiting
    (e.g. a short-lived script that wants its one event to land) may call
    this explicitly.

    The one production caller is ``mcp/detached_worker.py``'s ``main()``: a
    detached job worker is a short-lived background process, not an
    interactive command, and its terminal ``workflow_outcome`` event is the
    only event the published counting rule (TELEMETRY.md) reads. Without an
    explicit flush before that process exits, the queued event would be
    dropped along with everything else still in flight, silently
    undercounting every detached run. A bounded exit delay there does not
    violate the "never blocks a command" contract above -- there is no
    interactive command left to block by the time this runs.
    """
    try:
        deadline = time.monotonic() + timeout
        with _queue.all_tasks_done:
            while _queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                _queue.all_tasks_done.wait(remaining)
    except Exception:
        pass


def _resolve_allowed_keys(event: str, properties: dict[str, Any] | None) -> frozenset[str] | None:
    """Look up the exact per-event(-variant) property set for `event`.

    None means the event is not on the disclosed table at all -- capture()
    drops it. `command_run` has two variants selected by the
    caller-provided `source` property (`"cli"` vs. everything else,
    including absent, which is `"mcp"`); every other event's set is fixed
    by its name alone.
    """
    if event == "command_run":
        source = (properties or {}).get("source")
        return _COMMAND_RUN_CLI_KEYS if source == "cli" else _COMMAND_RUN_MCP_KEYS
    if event == "workflow_outcome":
        return _WORKFLOW_OUTCOME_KEYS
    if event == "mcp_serve_started":
        return _MCP_SERVE_STARTED_KEYS
    return None


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """Queue one event. Non-blocking, never raises, drops when queue is full.

    Enforces the event/property whitelist: an event (or command_run source
    variant) not on the disclosed table is dropped entirely, and any
    property not on that exact variant's set is dropped before the
    properties dict is built. See _resolve_allowed_keys.
    """
    try:
        if not is_enabled():
            return
        allowed_keys = _resolve_allowed_keys(event, properties)
        if allowed_keys is None:
            return  # not on the disclosed event table -- drop, don't guess
        props = _base_properties()
        if properties:
            props.update({k: v for k, v in properties.items() if v is not None})
        props = _sanitize_properties(props, allowed_keys)
        _queue.put_nowait(
            {
                "event": event,
                "distinct_id": distinct_id(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "properties": props,
            }
        )
        _ensure_worker()
    except Exception:
        pass


def capture_tool_call(
    name: str,
    *,
    ok: bool,
    duration_ms: float | None = None,
    error_type: str | None = None,
) -> None:
    """Capture one MCP tool invocation (the single funnel chokepoint)."""
    try:
        if not name.startswith("ouroboros_"):
            return
        sample_rate = 1
        if name in _POLLING_TOOLS:
            if _poll_rng.random() >= 1.0 / _POLL_SAMPLE_RATE:
                return  # independent per-call draw -- see _POLLING_TOOLS comment
            sample_rate = _POLL_SAMPLE_RATE
        funnel = _TOOL_FUNNEL.get(name)
        properties: dict[str, Any] = {
            "command": funnel or name.removeprefix("ouroboros_"),
            "tool": name,
            "source": "mcp",
            "is_funnel": funnel is not None,
            "phase": "submission" if name in _ASYNC_SUBMISSION_TOOLS else "completion",
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "error_type": error_type,
            "sample_rate": sample_rate if sample_rate > 1 else None,
        }
        if name in _ASYNC_SUBMISSION_TOOLS:
            properties["accepted"] = ok
        else:
            properties["ok"] = ok
        capture("command_run", properties)
    except Exception:
        pass


def capture_job_outcome(
    job_id: str,
    job_type: str,
    *,
    terminal_status: str,
    result_meta: dict[str, Any] | None = None,
) -> None:
    """Capture a durable background-job terminal transition.

    ``command_run`` submission receipts and durable outcomes are deliberately
    different events. Evaluation completion is also different from verified
    success: only an explicit ``final_approved is True`` earns ``verified``.
    The PostHog insert id is a one-way digest, so retries can be de-duplicated
    without disclosing the internal job identifier.
    """
    try:
        normalized_status = terminal_status.strip().lower()
        meta = result_meta if isinstance(result_meta, dict) else {}
        final_approved = meta.get("final_approved")
        verified = (
            normalized_status == "completed" and job_type == "evaluate" and final_approved is True
        )
        capture(
            "workflow_outcome",
            {
                "command": _JOB_FUNNEL.get(job_type, job_type),
                "phase": "terminal",
                "terminal_status": normalized_status,
                "ok": normalized_status == "completed",
                "verified": verified,
                "final_approved": (final_approved if isinstance(final_approved, bool) else None),
                "$insert_id": hashlib.sha256(
                    f"ouroboros-job-outcome\0{job_id}".encode()
                ).hexdigest(),
            },
        )
    except Exception:
        pass


def capture_cli_command(subcommand: str | None) -> None:
    """Capture a direct ``ooo <subcommand>`` invocation."""
    try:
        if not subcommand or subcommand in _CLI_SKIP:
            return
        capture(
            "command_run",
            {
                "command": _CLI_FUNNEL.get(subcommand, subcommand),
                "source": "cli",
                "is_funnel": subcommand in ("auto", "init", "interview", "seed", "run", "qa", "pm"),
            },
        )
    except Exception:
        pass


_NOTICE = (
    "Ouroboros collects anonymous usage data (commands, versions, success rates - "
    "never code, prompts, or file contents) to guide improvements and to publish "
    "aggregate adoption stats.\n"
    "Opt out anytime: export OUROBOROS_TELEMETRY=0  |  details: "
    "https://github.com/Q00/ouroboros/blob/main/TELEMETRY.md"
)


_NOTICE_MARKER_STALE_SECONDS = 10.0


def _claim_notice_marker(marker_path: Path) -> bool:
    """Try to become the one process that prints the first-run notice.

    True means this process won the ``O_EXCL`` create and should print and
    persist ``notice_shown``. False means someone else already holds it.

    A process can crash between creating the marker and finishing the print
    + persist step in ``show_first_run_notice``, leaving a marker behind
    with ``notice_shown`` still false forever -- every later process would
    then see the marker, assume a live claimer, and silently never disclose.
    That's distinguished from an actually-live claimer by marker age: a
    marker younger than ``_NOTICE_MARKER_STALE_SECONDS`` is plausibly still
    mid-print (return False, don't duplicate); one older than that is crash
    residue, so reclaim it once. A benign duplicate print is preferable to
    silently violating the disclosure contract.
    """
    try:
        fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except Exception:
        return True  # best-effort marker; don't let creation failure silence the notice forever

    try:
        age = time.time() - marker_path.stat().st_mtime
    except Exception:
        return False  # can't tell; assume a live claimer rather than risk a duplicate flood

    if age < _NOTICE_MARKER_STALE_SECONDS:
        return False  # a concurrent claimer is plausibly still mid-print

    # Stale marker from a crashed claimer -- reclaim once. If someone else
    # reclaims it first, they own the print and we return quietly.
    try:
        marker_path.unlink(missing_ok=True)
        fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def show_first_run_notice() -> None:
    """Print the one-time telemetry notice to stderr (safe for MCP stdio).

    Concurrent first-use processes (multiple MCP sessions starting together)
    can all read ``notice_shown=False`` before any of them has written the
    file, so the json check alone isn't enough to keep the print to once.
    Claimed via ``_claim_notice_marker`` instead. ``notice_shown`` is
    persisted into telemetry.json as before (the installer reads that
    field), via the now-atomic ``_write_state`` -- but only *after* printing:
    a crash between the print and the persist leaves the flag false, so the
    notice may print again next time (benign duplicate) rather than the flag
    getting set with nothing ever having been shown (silent, permanent
    non-disclosure).
    """
    try:
        if not is_enabled():
            return
        state = _load_state()
        if state.get("notice_shown"):
            return
        marker_path = _state_path().with_name("telemetry.notice")
        if not _claim_notice_marker(marker_path):
            return

        import sys

        print(f"\n{_NOTICE}\n", file=sys.stderr)
        state["notice_shown"] = True
        _write_state(state)
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Clear module caches so tests can run isolated (not public API)."""
    global _state_cache
    with _lock:
        _state_cache = None
        _context.clear()
        # Reseed from OS entropy so a test that seeded _poll_rng for a
        # deterministic capture/skip pattern can't leak that determinism
        # into an unrelated later test.
        _poll_rng.seed()


__all__ = [
    "capture",
    "capture_cli_command",
    "capture_job_outcome",
    "capture_tool_call",
    "distinct_id",
    "flush",
    "is_enabled",
    "set_context",
    "show_first_run_notice",
]
