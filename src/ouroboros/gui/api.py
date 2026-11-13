"""JSON API behind the Ouroboros console.

Handlers are plain functions ``(state, *path_groups, query, body) -> dict``
registered in :data:`ROUTES`; :mod:`ouroboros.gui.server` owns HTTP. Reads are
in-process. Writes reuse the existing write paths: ``ouroboros setup`` for
tools, the settings persistence layer for ``config.yaml``, and the providers
module for ``credentials.yaml``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
from typing import Any

from ouroboros.gui import clients as clients_mod
from ouroboros.gui import installers, providers, settings
from ouroboros.gui.jobs import Job, JobRegistry, ouroboros_command


class ApiError(Exception):
    """Raised by handlers; the server turns it into a JSON error response."""

    def __init__(self, status: int, message: str, code: str = "error", **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra


class EventStream:
    """A server-sent event source: ``events()`` yields dicts, or ``None`` for a heartbeat."""

    def __init__(
        self, gen: Iterator[dict[str, Any] | None], on_close: Callable[[], None] | None = None
    ):
        self._gen = gen
        self._on_close = on_close

    def events(self) -> Iterator[dict[str, Any] | None]:
        return self._gen

    def close(self) -> None:
        if self._on_close is not None:
            self._on_close()


class ConsoleState:
    """Process-wide state shared by every request."""

    def __init__(self, *, stop: Callable[..., None]) -> None:
        self.jobs = JobRegistry()
        self.stop = stop
        from ouroboros.gui.daemon import SharedMcp

        self.shared_mcp = SharedMcp()
        self.port = 0
        self._harness: Any = None
        self._harness_lock = threading.Lock()

    @property
    def harness(self) -> Any:
        with self._harness_lock:
            if self._harness is None:
                from ouroboros.gui.harness import Harness

                self._harness = Harness()
            return self._harness

    def health(self, started: float, port: int) -> dict[str, Any]:
        self.port = port
        return {
            "status": "ok",
            "service": "ouroboros-gui",
            "version": _version(),
            "uptime": round(time.monotonic() - started, 1),
            "pid": os.getpid(),
            "port": port,
        }


def _version() -> str:
    from ouroboros import __version__

    return __version__


def _latest_version() -> str | None:
    cache = Path.home() / ".ouroboros" / "version-check-cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    latest = data.get("latest_version") if isinstance(data, dict) else None
    return latest if isinstance(latest, str) else None


def _update_available(current: str, latest: str | None) -> bool:
    if not latest:
        return False
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        return False


def _db_path() -> str:
    from ouroboros.dashboard_web.reader import default_db_path

    return str(default_db_path())


def _recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    from ouroboros.dashboard_web.reader import PickerIndexContractError, list_recent_executions

    try:
        return list_recent_executions(_db_path(), limit=limit)
    except PickerIndexContractError:
        return []


def _recent_interviews(limit: int = 10) -> list[dict[str, Any]]:
    from ouroboros.dashboard_web.reader import PickerIndexContractError, list_recent_interviews

    try:
        return list_recent_interviews(_db_path(), limit=limit)
    except PickerIndexContractError:
        return []


def _doctor_surfaces() -> list[dict[str, Any]]:
    from ouroboros.cli.commands.doctor import collect_install_surfaces

    return [asdict(surface) for surface in collect_install_surfaces()]


# ── overview ──────────────────────────────────────────────────────────


def get_overview(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    version = _version()
    latest = _latest_version()
    cards = clients_mod.list_clients()
    surfaces = _doctor_surfaces()
    model = settings.SettingsModel()
    view_default = model.effective_default_runtime()
    return {
        "version": version,
        "latest_version": latest,
        "update_available": _update_available(version, latest),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "executable": sys.executable,
        "config_path": str(Path.home() / ".ouroboros" / "config.yaml"),
        "default_runtime": view_default,
        "llm_backend": model.effective_completion_backend(settings.Stage.INTERVIEW),
        "clients": {
            "total": len(cards),
            "installed": sum(1 for c in cards if c["installed"]),
            "connected": sum(1 for c in cards if c["integration"]["state"] == "connected"),
            "attention": sum(1 for c in cards if c["integration"]["state"] == "partial"),
        },
        "doctor": {
            status: sum(1 for s in surfaces if s["status"] == status)
            for status in ("pass", "warn", "fail")
        },
        "providers": providers.summary(),
        "runs": _recent_runs(limit=6),
        "running_jobs": [j for j in state.jobs.list() if j["status"] == "running"],
    }


def get_doctor(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return {
        "surfaces": _doctor_surfaces(),
        "version": _version(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "commands": {
            "doctor": "ouroboros doctor install",
            "mcp_doctor": "ouroboros mcp doctor",
            "update": "ouroboros update",
            "setup": "ouroboros setup",
        },
    }


# ── clients ───────────────────────────────────────────────────────────


def get_clients(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    fresh = query.get("fresh") == "1"
    cards = clients_mod.list_clients(fresh=fresh)
    for card in cards:
        card["install"] = installers.methods_for(card["id"])
        job = state.jobs.running_for("setup", card["id"]) or state.jobs.running_for(
            "install", card["id"]
        )
        card["running_job"] = job.id if job else None
    return {"clients": cards}


def _require_client(client_id: str) -> clients_mod.ClientSpec:
    for spec in clients_mod.CLIENTS:
        if spec.id == client_id:
            return spec
    raise ApiError(404, f"Unknown tool: {client_id}", "unknown_client")


def get_client_plan(
    state: ConsoleState, client_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    spec = _require_client(client_id)
    model = settings.SettingsModel()
    current_default = model.current("orchestrator.runtime_backend")
    return {
        "client": client_id,
        "command": f"ouroboros setup --runtime {client_id} --non-interactive",
        "wiring": spec.wiring,
        "current_default": settings.canonical(current_default),
        "changes_default": settings.canonical(current_default) != client_id,
        "backup": "~/.ouroboros/config.yaml.bak",
    }


def post_client_setup(
    state: ConsoleState, client_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    spec = _require_client(client_id)
    if spec.id == "claude":
        raise ApiError(
            409,
            "Claude Code gets Ouroboros from its plugin store. Run "
            "`claude plugin marketplace add Q00/ouroboros` and "
            "`claude plugin install ouroboros@ouroboros`.",
            "plugin_install",
        )
    if state.jobs.running_for("setup", client_id):
        raise ApiError(409, "Setup is already running for this tool.", "busy")
    keep_default = bool((body or {}).get("keep_default"))
    snapshot = settings.SettingsModel().raw if keep_default else None

    def restore_default(job: Job) -> str | None:
        clients_mod.invalidate()
        if job.status != "succeeded" or snapshot is None:
            return None
        from ouroboros.config_tui.fields import get_value
        from ouroboros.config_tui.persistence import apply_config_values

        keys = ("orchestrator.runtime_backend", "llm.backend")
        restore = {key: get_value(snapshot, key) for key in keys}
        apply_config_values(restore)
        clients_mod.invalidate()
        previous = restore["orchestrator.runtime_backend"] or "the previous agent"
        return f"Kept {previous} as the default agent."

    job = state.jobs.start(
        kind="setup",
        title=f"Set up {spec.label}",
        argv=ouroboros_command("setup", "--runtime", client_id, "--non-interactive"),
        meta={"key": client_id},
        on_exit=restore_default,
    )
    return {"job": job.snapshot()}


def post_client_install(
    state: ConsoleState, client_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    spec = _require_client(client_id)
    method_id = str((body or {}).get("method") or "")
    method = installers.find(client_id, method_id)
    if method is None:
        raise ApiError(404, "No one-click install for this tool and method.", "no_installer")
    if not installers.available(method):
        raise ApiError(409, f"{method['requires']} is not installed.", "missing_tool")
    if state.jobs.running_for("install", client_id):
        raise ApiError(409, "An install is already running for this tool.", "busy")
    job = state.jobs.start(
        kind="install",
        title=f"Install {spec.label}",
        argv=installers.argv(method),
        meta={"key": client_id, "method": method_id},
        on_exit=lambda _job: clients_mod.invalidate(),
    )
    return {"job": job.snapshot()}


# ── jobs ──────────────────────────────────────────────────────────────


def get_jobs(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return {"jobs": state.jobs.list()}


def get_job(
    state: ConsoleState, job_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "Job not found", "not_found")
    try:
        since = int(query.get("since", "0"))
    except ValueError:
        since = 0
    return job.snapshot(since=since)


# ── settings ──────────────────────────────────────────────────────────


def get_settings(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return settings.SettingsModel().view()


def post_settings_preview(
    state: ConsoleState, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    return settings.save(body or {}, dry_run=True)


def post_settings_save(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.config_tui.persistence import ConfigWriteError

    try:
        result = settings.save(body or {})
    except ConfigWriteError as exc:
        raise ApiError(400, str(exc), "config_rejected") from exc
    clients_mod.invalidate()
    return result


def post_settings_undo(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.config_tui.persistence import ConfigWriteError

    try:
        settings.undo()
    except ConfigWriteError as exc:
        raise ApiError(409, str(exc), "undo_failed") from exc
    clients_mod.invalidate()
    return {"ok": True}


def get_settings_models(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    backend = settings.canonical(query.get("backend", ""))
    if not backend:
        raise ApiError(400, "Pass ?backend=<name>", "missing_backend")
    return {"backend": backend, "models": settings.fetch_models(backend)}


# ── runs ──────────────────────────────────────────────────────────────


def get_runs(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return {"runs": _recent_runs(limit=30), "interviews": _recent_interviews(limit=20)}


def _board(run_id: str) -> dict[str, Any]:
    from ouroboros.dashboard_web.kanban import reduce_board
    from ouroboros.dashboard_web.reader import EventTail

    tail = EventTail(_db_path(), run_id)
    return reduce_board(tail.fetch_all(limit=5000), execution_id=run_id)


def get_run_board(
    state: ConsoleState, run_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    return _board(run_id)


def get_run_stream(state: ConsoleState, run_id: str, *, query: dict[str, str], body: Any) -> Any:
    from ouroboros.dashboard_web.kanban import reduce_board
    from ouroboros.dashboard_web.reader import EventTail

    def gen() -> Iterator[dict[str, Any] | None]:
        tail = EventTail(_db_path(), run_id)
        accumulated: list[dict[str, Any]] = []
        last_payload = ""
        last_emit = 0.0
        while True:
            new = tail.fetch_new()
            now = time.monotonic()
            if new:
                accumulated.extend(new)
                board = reduce_board(accumulated, execution_id=run_id)
                payload = json.dumps(board, default=str)
                if payload != last_payload:
                    last_payload, last_emit = payload, now
                    yield board
            elif now - last_emit > 15:
                last_emit = now
                yield None
            time.sleep(0.7)

    return EventStream(gen())


# ── providers ─────────────────────────────────────────────────────────


def get_providers(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return providers.view()


def put_provider(
    state: ConsoleState, provider_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    try:
        providers.save(provider_id, body or {})
    except providers.ProviderError as exc:
        raise ApiError(400, str(exc), "provider_rejected") from exc
    return providers.view()


def delete_provider(
    state: ConsoleState, provider_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    try:
        providers.remove(provider_id)
    except providers.ProviderError as exc:
        raise ApiError(400, str(exc), "provider_rejected") from exc
    return providers.view()


def post_provider_test(
    state: ConsoleState, provider_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    return providers.test(provider_id, body or {})


# ── chat harness ──────────────────────────────────────────────────────


def get_chat_sessions(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    return {"sessions": state.harness.list_sessions(), "defaults": state.harness.defaults()}


def post_chat_session(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    try:
        return state.harness.create(body or {})
    except ValueError as exc:
        raise ApiError(400, str(exc), "bad_request") from exc


def get_chat_session(
    state: ConsoleState, session_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    session = state.harness.get(session_id)
    if session is None:
        raise ApiError(404, "Conversation not found", "not_found")
    return session


def delete_chat_session(
    state: ConsoleState, session_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    state.harness.delete(session_id)
    return {"ok": True}


def post_chat_message(
    state: ConsoleState, session_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    try:
        return state.harness.send(session_id, str((body or {}).get("text") or ""))
    except KeyError:
        raise ApiError(404, "Conversation not found", "not_found") from None
    except ValueError as exc:
        raise ApiError(409, str(exc), "not_ready") from exc


def post_chat_action(
    state: ConsoleState, session_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    try:
        return state.harness.action(session_id, str((body or {}).get("action") or ""), body or {})
    except KeyError:
        raise ApiError(404, "Conversation not found", "not_found") from None
    except ValueError as exc:
        raise ApiError(409, str(exc), "not_ready") from exc


def get_chat_stream(
    state: ConsoleState, session_id: str, *, query: dict[str, str], body: Any
) -> Any:
    try:
        gen, close = state.harness.subscribe(session_id)
    except KeyError:
        raise ApiError(404, "Conversation not found", "not_found") from None
    return EventStream(gen, close)


# ── components ────────────────────────────────────────────────────────


def get_components(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.gui import components

    view = components.view()
    for row in view["components"]:
        job = state.jobs.running_for("component", row["extra"])
        row["running_job"] = job.id if job else None
    return view


def post_component_install(
    state: ConsoleState, extra: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    from ouroboros.gui import components

    try:
        plan = components.plan(extra)
    except KeyError:
        raise ApiError(404, f"Unknown component: {extra}", "unknown_component") from None
    except ValueError as exc:
        raise ApiError(409, str(exc), "not_available") from exc
    if plan["conflict"] and not (body or {}).get("accept_conflict"):
        raise ApiError(409, plan["conflict"], "conflict", plan=plan)
    if state.jobs.running_for("component", extra):
        raise ApiError(409, "This component is already being installed.", "busy")
    job = state.jobs.start(
        kind="component",
        title=f"Install {plan['label']}",
        argv=plan["argv"],
        meta={"key": extra},
        on_exit=lambda finished: (
            "Restart the console to load it." if finished.status == "succeeded" else None
        ),
    )
    return {"job": job.snapshot(), "plan": plan}


# ── daemon: shared MCP, login service ────────────────────────────────


def get_daemon(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.gui import daemon, mcp_clients, service

    return {
        "mcp": state.shared_mcp.status(),
        "service": service.status(),
        "stdio_servers": daemon.count_stdio_servers(),
        "clients": mcp_clients.status(state.shared_mcp.port),
    }


def post_daemon_mcp(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.gui import daemon

    action = str((body or {}).get("action") or "")
    if action == "start":
        daemon.save_settings(shared_mcp=True)
        state.shared_mcp.start()
    elif action == "stop":
        daemon.save_settings(shared_mcp=False)
        state.shared_mcp.stop()
    elif action == "restart":
        state.shared_mcp.stop()
        state.shared_mcp.start()
    else:
        raise ApiError(400, "action must be start, stop or restart", "bad_action")
    return get_daemon(state, query=query, body=body)


def post_daemon_service(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    from ouroboros.gui import service

    action = str((body or {}).get("action") or "")
    try:
        if action == "install":
            result = service.install(state.port or 10300)
            handoff = not result["this_process_is_service"]
            if handoff:
                # The service instance waits for this console to exit, then takes over.
                state.stop()
            return {"service": result, "handoff": handoff}
        if action == "uninstall":
            return {"service": service.uninstall(), "handoff": False}
    except service.ServiceError as exc:
        raise ApiError(409, str(exc), "service_error") from exc
    raise ApiError(400, "action must be install or uninstall", "bad_action")


def post_daemon_client(
    state: ConsoleState, client_id: str, *, query: dict[str, str], body: Any
) -> dict[str, Any]:
    from ouroboros.gui import mcp_clients

    use_shared = bool((body or {}).get("use_shared"))
    try:
        mcp_clients.set_shared(client_id, use_shared, port=state.shared_mcp.port)
    except mcp_clients.ClientWiringError as exc:
        raise ApiError(409, str(exc), "wiring_error") from exc
    clients_mod.invalidate()
    return get_daemon(state, query=query, body=body)


# ── server ────────────────────────────────────────────────────────────


def post_stop(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    state.stop()
    return {"ok": True}


def post_restart(state: ConsoleState, *, query: dict[str, str], body: Any) -> dict[str, Any]:
    state.stop(restart=True)
    return {"ok": True}


def _r(method: str, pattern: str, handler: Callable[..., Any]) -> tuple[str, re.Pattern[str], Any]:
    return method, re.compile(pattern), handler


_ID = r"([A-Za-z0-9_.:-]{1,128})"

ROUTES: list[tuple[str, re.Pattern[str], Any]] = [
    _r("GET", r"/api/overview", get_overview),
    _r("GET", r"/api/doctor", get_doctor),
    _r("GET", r"/api/clients", get_clients),
    _r("GET", rf"/api/clients/{_ID}/plan", get_client_plan),
    _r("POST", rf"/api/clients/{_ID}/setup", post_client_setup),
    _r("POST", rf"/api/clients/{_ID}/install", post_client_install),
    _r("GET", r"/api/jobs", get_jobs),
    _r("GET", rf"/api/jobs/{_ID}", get_job),
    _r("GET", r"/api/settings", get_settings),
    _r("POST", r"/api/settings/preview", post_settings_preview),
    _r("POST", r"/api/settings/save", post_settings_save),
    _r("POST", r"/api/settings/undo", post_settings_undo),
    _r("GET", r"/api/settings/models", get_settings_models),
    _r("GET", r"/api/runs", get_runs),
    _r("GET", rf"/api/runs/{_ID}/board", get_run_board),
    _r("GET", rf"/api/runs/{_ID}/stream", get_run_stream),
    _r("GET", r"/api/providers", get_providers),
    _r("PUT", rf"/api/providers/{_ID}", put_provider),
    _r("DELETE", rf"/api/providers/{_ID}", delete_provider),
    _r("POST", rf"/api/providers/{_ID}/test", post_provider_test),
    _r("GET", r"/api/chat/sessions", get_chat_sessions),
    _r("POST", r"/api/chat/sessions", post_chat_session),
    _r("GET", rf"/api/chat/sessions/{_ID}", get_chat_session),
    _r("DELETE", rf"/api/chat/sessions/{_ID}", delete_chat_session),
    _r("POST", rf"/api/chat/sessions/{_ID}/messages", post_chat_message),
    _r("POST", rf"/api/chat/sessions/{_ID}/actions", post_chat_action),
    _r("GET", rf"/api/chat/sessions/{_ID}/stream", get_chat_stream),
    _r("GET", r"/api/components", get_components),
    _r("POST", rf"/api/components/{_ID}/install", post_component_install),
    _r("GET", r"/api/daemon", get_daemon),
    _r("POST", r"/api/daemon/mcp", post_daemon_mcp),
    _r("POST", r"/api/daemon/service", post_daemon_service),
    _r("POST", rf"/api/daemon/clients/{_ID}", post_daemon_client),
    _r("POST", r"/api/server/stop", post_stop),
    _r("POST", r"/api/server/restart", post_restart),
]

__all__ = ["ROUTES", "ApiError", "ConsoleState", "EventStream"]
