"""Owned process handles and handshakes for the model-free detached probes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

from ouroboros.mcp.job_manager import JobManager
from ouroboros.orchestrator.heartbeat import is_process_identity_alive, process_start_time
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)

_IDENTITY_HOOK = textwrap.dedent(
    """
    import ctypes
    import importlib.machinery
    import importlib.util
    import json
    import os
    from pathlib import Path
    import sys

    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    elif sys.platform == "linux":
        identity = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    elif sys.platform == "darwin":
        import subprocess
        identity = subprocess.check_output(
            ["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True, timeout=3,
        ).strip()
    else:
        identity = None
    destination = Path(os.environ["OOO_TEST_PROCESS_IDENTITIES"]) / f"{os.getpid()}.json"
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(identity), encoding="utf-8")
    temporary.replace(destination)

    # Preserve an existing window/runtime probe instead of replacing its startup hook.
    own_directory = Path(__file__).resolve().parent
    search = [entry for entry in sys.path if Path(entry).resolve() != own_directory]
    previous = importlib.machinery.PathFinder.find_spec("sitecustomize", search)
    if previous is not None and previous.loader is not None:
        module = importlib.util.module_from_spec(previous)
        previous.loader.exec_module(module)
    """
)

_LAUNCH_PARENT = textwrap.dedent(
    """
    import asyncio
    import json
    import os
    from pathlib import Path
    import sys

    from ouroboros.mcp import detached_jobs
    from ouroboros.mcp.detached_jobs import DetachedJobRequest, launch_detached_job
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.persistence.event_store import EventStore

    async def main():
        database_url, cwd, delay, tool_name, receipt, legacy_windows_console = sys.argv[1:]
        receipt = Path(receipt)
        store = EventStore(database_url)
        manager = JobManager(store, durable_jobs=True)
        job_id = await manager.allocate_job_id()
        record = {
            "parent_pid": os.getpid(), "job_id": job_id,
            "legacy_windows_console": legacy_windows_console == "1",
        }
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.IsProcessInJob.argtypes = (
                wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
            )
            in_job = wintypes.BOOL()
            if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(in_job)):
                raise ctypes.WinError(ctypes.get_last_error())
            record["parent_in_job_object"] = bool(in_job.value)

        def publish():
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            temporary.replace(receipt)

        if legacy_windows_console == "1":
            from types import SimpleNamespace
            original_subprocess = detached_jobs.subprocess
            def legacy_popen(*args, **kwargs):
                kwargs["creationflags"] = (
                    original_subprocess.DETACHED_PROCESS
                    | original_subprocess.CREATE_NEW_PROCESS_GROUP
                )
                return original_subprocess.Popen(*args, **kwargs)
            # Only this accepting child's module reference changes. The real
            # worker and Popen still run, with the pre-fix creation flags.
            detached_jobs.subprocess = SimpleNamespace(
                **{**vars(original_subprocess), "Popen": legacy_popen}
            )

        spawn = detached_jobs._spawn_worker
        def observed_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            if os.name == "nt":
                # This is the owned Popen handle, never a PID reopened after exit.
                kernel.GetProcessTimes.argtypes = (
                    wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                )
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(int(child._handle), *(ctypes.byref(t) for t in times)):
                    raise ctypes.WinError(ctypes.get_last_error())
                identity = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                identity_file = Path(os.environ["OOO_TEST_PROCESS_IDENTITIES"]) / f"{child.pid}.json"
                temporary = identity_file.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(identity), encoding="utf-8")
                temporary.replace(identity_file)
            record["launcher_pid"] = child.pid
            publish()
            return child
        detached_jobs._spawn_worker = observed_spawn
        argument_name = (
            "nested_delay_seconds"
            if tool_name == "__detached_nested_probe__"
            else "delay_seconds"
        )
        try:
            await launch_detached_job(
                job_manager=manager,
                event_store=store,
                request=DetachedJobRequest(
                    job_id=job_id,
                    tool_name=tool_name,
                    arguments={argument_name: float(delay)},
                    database_url=database_url,
                    cwd=cwd,
                ),
            )
            created = (await store.replay("job", job_id))[0]
            record["owner_pid"] = created.data["owner_pid"]
            record["ready"] = True
            publish()
            # The test acquires handles before allowing normal or forced exit.
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            await store.close()

    asyncio.run(main())
    """
)


class ProbeProcess:
    """Observe and clean up only a process belonging to this test's probe."""

    def __init__(self, pid: int, identity: str) -> None:
        self.pid = pid
        self.identity = identity
        self.start_time = process_start_time(pid)
        self.handle = None
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            self.kernel.TerminateProcess.restype = wintypes.BOOL
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.kernel.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            # Retaining the handle prevents cleanup from targeting a reused PID.
            self.handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
            if not self.handle and ctypes.get_last_error() != 87:
                raise ctypes.WinError(ctypes.get_last_error())
            if self.handle:
                try:
                    times = [wintypes.FILETIME() for _ in range(4)]
                    if not self.kernel.GetProcessTimes(
                        self.handle, *(ctypes.byref(value) for value in times)
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                    observed = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                    if observed != identity:
                        raise AssertionError(
                            f"probe PID {pid} was reused before handle acquisition"
                        )
                except BaseException:
                    self.close()
                    raise
        else:
            observed = self._current_identity()
            if observed is not None and observed != identity:
                raise AssertionError(f"probe PID {pid} was reused before observation")

    def _current_identity(self) -> str | None:
        if sys.platform == "linux":
            try:
                return Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                return None
        if sys.platform == "darwin":
            result = subprocess.run(
                ["ps", "-p", str(self.pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return result.stdout.strip() or None
        return None

    def alive(self) -> bool:
        if os.name == "nt":
            if not self.handle:
                return False
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return result != 0
        # An orphan can briefly be a zombie until its new parent reaps it.
        with suppress(OSError):
            state = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return False
        return (
            is_process_identity_alive(self.pid, self.start_time)
            and self._current_identity() == self.identity
        )

    async def wait(self, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.alive():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"probe process {self.pid} did not exit within {timeout}s")
            await asyncio.sleep(0.05)

    def kill(self) -> None:
        if not self.alive():
            return
        if os.name == "nt":
            if not self.kernel.TerminateProcess(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        elif self.start_time is not None:
            # No unverified PID-only force kill on platforms without identity data.
            with suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class DetachedProbe:
    parent: subprocess.Popen
    store: EventStore
    manager: JobManager
    identities: Path
    record: dict = field(default_factory=dict)
    processes: dict[int, ProbeProcess] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        return self.record["job_id"]

    def track(self, pid: int) -> ProbeProcess:
        if pid not in self.processes:
            identity = json.loads((self.identities / f"{pid}.json").read_text(encoding="utf-8"))
            if not isinstance(identity, str) or not identity:
                raise AssertionError(f"probe PID {pid} has no supported creation identity")
            self.processes[pid] = ProbeProcess(pid, identity)
        return self.processes[pid]

    async def wait_worker_exit(self) -> None:
        await asyncio.gather(
            *(self.track(self.record[key]).wait() for key in ("launcher_pid", "owner_pid"))
        )

    async def cleanup(self) -> None:
        # This database belongs exclusively to the test, including nested jobs.
        job_ids = {self.job_id} if self.record.get("job_id") else set()
        try:
            events = await self.store.query_events(event_type="mcp.job.created")
            job_ids.update(event.aggregate_id for event in events)
        except Exception:
            logger.warning("Cannot discover probe jobs during cleanup", exc_info=True)
        for job_id in job_ids:
            # A persisted PID can already be recycled. Teardown may cancel the
            # job, but must never acquire new authority to kill that PID.
            try:
                await self.manager.cancel_job(job_id)
            except Exception:
                logger.warning("Cannot cooperatively cancel probe %s", job_id, exc_info=True)
        if self.parent.stdin is not None:
            self.parent.stdin.close()
        if self.parent.poll() is None:
            accepting = self.processes.get(self.record.get("parent_pid"))
            if accepting is not None:
                accepting.kill()
            else:
                # Popen retains its own process handle independently of the PID.
                self.parent.kill()
        results = await asyncio.gather(
            *(process.wait() for process in self.processes.values()), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            for process in self.processes.values():
                process.kill()
            await asyncio.gather(*(process.wait() for process in self.processes.values()))
        self.parent.wait(timeout=15)


@asynccontextmanager
async def accepting_parent(
    *,
    database_url: str,
    cwd: Path,
    home: Path,
    delay: float,
    nested: bool = False,
    exit_mode: str = "normal",
    legacy_windows_console: bool = False,
):
    if legacy_windows_console and os.name != "nt":
        raise ValueError("The legacy console control requires Windows")
    home.mkdir(parents=True, exist_ok=True)
    receipt = home / "parent.json"
    identities = home / "process-identities"
    identities.mkdir()
    hooks = home / "identity-hook"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(_IDENTITY_HOOK, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        OUROBOROS_DASHBOARD="0",
        OUROBOROS_TELEMETRY="0",
        OOO_TEST_PROCESS_IDENTITIES=str(identities),
        PYTHONPATH=os.pathsep.join((str(hooks), env.get("PYTHONPATH", ""))),
    )
    with (home / "parent.log").open("w", encoding="utf-8") as log:
        parent = subprocess.Popen(  # noqa: S603 - fixed interpreter/test program
            [
                sys.executable,
                "-c",
                _LAUNCH_PARENT,
                database_url,
                str(cwd),
                str(delay),
                "__detached_nested_probe__" if nested else "__detached_probe__",
                str(receipt),
                "1" if legacy_windows_console else "0",
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        store = EventStore(database_url)
        probe = DetachedProbe(parent, store, JobManager(store), identities)
        failed = False
        try:
            deadline = asyncio.get_running_loop().time() + 30.0
            while not probe.record.get("ready"):
                if receipt.exists():
                    probe.record = json.loads(receipt.read_text(encoding="utf-8"))
                    for key in ("parent_pid", "launcher_pid", "owner_pid"):
                        if (
                            key in probe.record
                            and (identities / f"{probe.record[key]}.json").exists()
                        ):
                            probe.track(probe.record[key])
                if parent.poll() is not None or asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"accepting parent did not become ready: {home / 'parent.log'}"
                    )
                await asyncio.sleep(0.02)

            for key in ("parent_pid", "launcher_pid", "owner_pid"):
                probe.track(probe.record[key])
            accepting = probe.track(probe.record["parent_pid"])
            if exit_mode == "forced":
                accepting.kill()
            else:
                assert parent.stdin is not None
                parent.stdin.write(b"\n")
                parent.stdin.close()
            await accepting.wait()
            # Popen owns the venv launcher, which exits after the real parent.
            await asyncio.to_thread(parent.wait, 15)
            if exit_mode == "normal":
                assert parent.returncode == 0
            else:
                assert parent.returncode != 0
            yield probe
        except BaseException:
            failed = True
            raise
        finally:
            cleanup_error = None
            try:
                await probe.cleanup()
            except Exception as error:
                cleanup_error = error
            finally:
                if parent.stdin is not None:
                    parent.stdin.close()
                for process in probe.processes.values():
                    process.close()
                try:
                    await store.close()
                except Exception as error:
                    cleanup_error = cleanup_error or error
            if cleanup_error is not None:
                if not failed:
                    raise cleanup_error
                logger.warning("Probe cleanup failed after a test failure", exc_info=cleanup_error)
