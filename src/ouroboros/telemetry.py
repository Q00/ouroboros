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
  caller, and silently drops events on any failure.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import platform
import queue
import ssl
import threading
import time
from typing import Any
import urllib.request
import uuid

from ouroboros import __version__

# PostHog project API key. This is a *public, write-only* key (it can only
# ingest events, never read them) — embedding it in an open-source repo is
# the documented PostHog pattern. Empty string disables telemetry entirely.
_EMBEDDED_API_KEY = "phc_mSoetD4ExLDDCi3vNua635NhwRTgHfRaCG9WYNKmrvv5"
_DEFAULT_HOST = "https://us.i.posthog.com"

_QUEUE_MAX = 256
_BATCH_MAX = 25
_HTTP_TIMEOUT_SECONDS = 4.0

# Tools that skills poll in loops (job status, HUDs, projections). Captured
# at 1/_POLL_SAMPLE_RATE with a ``sample_rate`` property so absolute counts
# can be re-weighted in PostHog without flooding ingestion.
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
    }
)
_POLL_SAMPLE_RATE = 50

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

_lock = threading.Lock()
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_context: dict[str, Any] = {}
_poll_counters: dict[str, int] = {}
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


def _load_state() -> dict[str, Any]:
    global _state_cache
    with _lock:
        if _state_cache is not None:
            return _state_cache
        path = _state_path()
        state: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("distinct_id"), str):
                state = raw
        except Exception:
            state = {}
        if not state.get("distinct_id"):
            state = {
                "distinct_id": str(uuid.uuid4()),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "notice_shown": False,
            }
            _write_state(state)
        _state_cache = state
        return state


def _write_state(state: dict[str, Any]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def distinct_id() -> str:
    """Stable anonymous id for this machine/user."""
    return str(_load_state()["distinct_id"])


def set_context(**props: Any) -> None:
    """Merge properties (e.g. runtime_backend) into every subsequent event."""
    with _lock:
        for key, value in props.items():
            if value is not None:
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
            atexit.register(flush)


def flush(timeout: float = 1.5) -> None:
    """Best-effort wait for queued events to be sent (bounded, never raises)."""
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


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """Queue one event. Non-blocking, never raises, drops when queue is full."""
    try:
        if not is_enabled():
            return
        props = _base_properties()
        if properties:
            props.update({k: v for k, v in properties.items() if v is not None})
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
            with _lock:
                count = _poll_counters.get(name, 0)
                _poll_counters[name] = count + 1
            if count % _POLL_SAMPLE_RATE:
                return
            sample_rate = _POLL_SAMPLE_RATE
        funnel = _TOOL_FUNNEL.get(name)
        capture(
            "command_run",
            {
                "command": funnel or name.removeprefix("ouroboros_"),
                "tool": name,
                "source": "mcp",
                "is_funnel": funnel is not None,
                "ok": ok,
                "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
                "error_type": error_type,
                "sample_rate": sample_rate if sample_rate > 1 else None,
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
    "Opt out anytime: export OUROBOROS_TELEMETRY=0  |  details: TELEMETRY.md"
)


def show_first_run_notice() -> None:
    """Print the one-time telemetry notice to stderr (safe for MCP stdio)."""
    try:
        if not is_enabled():
            return
        state = _load_state()
        if state.get("notice_shown"):
            return
        state["notice_shown"] = True
        _write_state(state)
        import sys

        print(f"\n{_NOTICE}\n", file=sys.stderr)
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Clear module caches so tests can run isolated (not public API)."""
    global _state_cache
    with _lock:
        _state_cache = None
        _context.clear()
        _poll_counters.clear()


__all__ = [
    "capture",
    "capture_cli_command",
    "capture_tool_call",
    "distinct_id",
    "flush",
    "is_enabled",
    "set_context",
    "show_first_run_notice",
]
