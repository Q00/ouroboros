"""Exercise the Windows venv redirector, not just Popen's keyword arguments."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import textwrap
from typing import Any
from uuid import uuid4

import pytest

from ouroboros.mcp.job_manager import JobStatus
from tests.integration.mcp.detached_probe_process import accepting_parent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console API")


class _VisibleWindows:
    """Read-only observations of windows carrying this probe's unique title."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.samples = 0
        self.windows: dict[int, dict[str, Any]] = {}
        self.error: str | None = None

    async def observe(self, stop: asyncio.Event) -> None:
        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
            user.EnumWindows.restype = wintypes.BOOL
            user.IsWindowVisible.argtypes = (wintypes.HWND,)
            user.IsWindowVisible.restype = wintypes.BOOL
            user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
            user.GetWindowTextW.restype = ctypes.c_int
            user.GetWindowThreadProcessId.argtypes = (
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            )
            user.GetWindowThreadProcessId.restype = wintypes.DWORD

            def visit(hwnd: int, _parameter: int) -> bool:
                if user.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(4096)
                    user.GetWindowTextW(hwnd, title, len(title))
                    if self.token in title.value:
                        host_pid = wintypes.DWORD()
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(host_pid))
                        # A Terminal/conhost PID is evidence, never cleanup authority.
                        self.windows[int(hwnd)] = {
                            "hwnd": int(hwnd),
                            "host_pid": host_pid.value,
                            "title": title.value,
                        }
                return True

            callback = callback_type(visit)
            while True:
                ctypes.set_last_error(0)
                if not user.EnumWindows(callback, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.samples += 1
                if stop.is_set():
                    break
                await asyncio.sleep(0.05)
        except (AttributeError, OSError) as error:
            self.error = f"{type(error).__name__}: {error}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "sample_count": self.samples,
            "sample_interval_seconds": 0.05,
            "windows": list(self.windows.values()),
            "error": self.error,
        }


async def _run_console_probe(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    *,
    legacy: bool,
    visible: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    directory.mkdir()
    hooks = directory / "hooks"
    hooks.mkdir()
    observation = directory / "console.json"
    token = f"ooo-console-probe-{uuid4().hex}"
    # Observe Python startup and then continue into the genuine worker entrypoint.
    # Neither the worker nor process creation is mocked or replaced by this hook.
    (hooks / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import os
            import sys
            if "ouroboros.mcp.detached_worker" in sys.orig_argv:
                import ctypes
                import json
                from pathlib import Path
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetConsoleWindow.argtypes = ()
                kernel.GetConsoleWindow.restype = ctypes.c_void_p
                kernel.SetConsoleTitleW.argtypes = (ctypes.c_wchar_p,)
                kernel.SetConsoleTitleW.restype = ctypes.c_int
                hwnd = kernel.GetConsoleWindow() or 0
                token = os.environ["OOO_TEST_CONSOLE_TOKEN"]
                title_set = bool(kernel.SetConsoleTitleW(token)) if hwnd else False
                Path(os.environ["OOO_TEST_CONSOLE_FILE"]).write_text(json.dumps({
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "hwnd": hwnd,
                    "token": token,
                    "title_set": title_set,
                    "executable": sys.executable,
                    "prefix": sys.prefix,
                    "base_prefix": sys.base_prefix,
                }), encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OOO_TEST_CONSOLE_FILE", str(observation))
    monkeypatch.setenv("OOO_TEST_CONSOLE_TOKEN", token)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(hooks), *sys.path)))
    observer = _VisibleWindows(token)
    stop = asyncio.Event()
    observing = asyncio.create_task(observer.observe(stop)) if visible else None
    if observing is not None:
        # Take the first sample before launching any of the test's processes.
        await asyncio.sleep(0)
    try:
        async with accepting_parent(
            database_url=f"sqlite+aiosqlite:///{directory / 'events.db'}",
            cwd=Path.cwd(),
            home=directory / "home",
            delay=3.0,
            legacy_windows_console=legacy,
        ) as probe:
            await probe.wait_worker_exit()
            result = await probe.manager.get_snapshot(probe.job_id)
            assert result.status == JobStatus.COMPLETED
            assert result.result_text == "detached probe complete"
            console = json.loads(observation.read_text(encoding="utf-8"))
            assert probe.record["legacy_windows_console"] is legacy
            assert console["pid"] == probe.record["owner_pid"]
            expected_parent = (
                probe.record["launcher_pid"]
                if console["pid"] != probe.record["launcher_pid"]
                else probe.record["parent_pid"]
            )
            assert console["parent_pid"] == expected_parent
            assert console["prefix"] != console["base_prefix"]
            assert Path(console["executable"]) == Path(sys.executable)
            assert console["token"] == token
    finally:
        stop.set()
        if observing is not None:
            await observing
            (directory / "visible-windows.json").write_text(
                json.dumps(observer.snapshot(), indent=2), encoding="utf-8"
            )
    request.node.user_properties.extend(
        [
            (f"{directory.name}_console_hwnd", console["hwnd"]),
            (f"{directory.name}_console_evidence", str(observation)),
        ]
    )
    if visible:
        request.node.user_properties.extend(
            [
                (f"{directory.name}_visible_windows", len(observer.windows)),
                (f"{directory.name}_visibility_evidence", str(directory / "visible-windows.json")),
            ]
        )
    return console, observer.snapshot()


def _require_legacy_console(console: dict[str, Any]) -> None:
    if not console["hwnd"]:
        pytest.skip(
            "Legacy detached venv did not allocate a console; regression precondition unverified"
        )


def _require_visibility_observation(console: dict[str, Any], observation: dict[str, Any]) -> None:
    if observation["error"]:
        pytest.skip(
            "Visible-window observation unavailable; visibility unverified: " + observation["error"]
        )
    if console["hwnd"] and not console["title_set"]:
        pytest.skip("Cannot label the owned console window; visibility unverified")
    assert observation["sample_count"] > 0


@pytest.mark.asyncio
async def test_venv_worker_completes_without_allocating_a_console_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Check console association, which does not itself prove desktop visibility."""
    request.node.user_properties.append(("validation_scope", "console_association"))
    if sys.prefix == sys.base_prefix:
        pytest.skip("Run with a Windows venv to exercise its Python redirector")
    legacy, _ = await _run_console_probe(tmp_path / "legacy", monkeypatch, request, legacy=True)
    _require_legacy_console(legacy)
    console, _ = await _run_console_probe(
        tmp_path / "candidate", monkeypatch, request, legacy=False
    )
    assert console["hwnd"] == 0


@pytest.mark.asyncio
async def test_venv_worker_completes_without_a_visible_console_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Only a visible legacy control makes the candidate's empty observations meaningful."""
    request.node.user_properties.append(("validation_scope", "visible_window"))
    if sys.prefix == sys.base_prefix:
        pytest.skip("Run with a Windows venv to exercise its Python redirector")
    legacy, control = await _run_console_probe(
        tmp_path / "legacy", monkeypatch, request, legacy=True, visible=True
    )
    _require_legacy_console(legacy)
    _require_visibility_observation(legacy, control)
    if not control["windows"]:
        pytest.skip(
            "Legacy console window was not visible/detectable in this session; visibility unverified"
        )
    console, candidate = await _run_console_probe(
        tmp_path / "candidate", monkeypatch, request, legacy=False, visible=True
    )
    assert candidate["windows"] == [], candidate["windows"]
    _require_visibility_observation(console, candidate)
