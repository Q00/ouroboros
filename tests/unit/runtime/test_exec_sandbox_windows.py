"""The Windows backend of the execution sandbox: a per-run AppContainer.

Run on the ``windows-latest`` CI job. On GitHub Actions the backend must be
available (a missing backend fails there instead of skipping); elsewhere the
tests skip with the probe's reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from ouroboros.runtime import _sandbox_probe as probe_module
from ouroboros.runtime import exec_sandbox
from ouroboros.runtime.exec_sandbox import (
    ConfinedCommand,
    SandboxBackend,
    SandboxUnavailable,
    SandboxUnavailableReason,
    confine,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="AppContainer is Windows only")

if sys.platform == "win32":
    import _winapi
    import ctypes

    from ouroboros.runtime import _confine_windows as launcher


def _require_backend() -> None:
    reason = exec_sandbox.sandbox_unavailable_reason(deny_network=False)
    if reason is None:
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail(f"the AppContainer backend must work on the CI runner: {reason.value}")
    pytest.skip(f"execution sandbox unavailable on this host: {reason.value}")


def _python(code: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", code, *args)


def _run(command: ConfinedCommand, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv built by confine
        list(command.argv),
        cwd=command.cwd,
        env=dict(command.env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _confine(layout: dict[str, Path], argv: tuple[str, ...], **kwargs: object) -> ConfinedCommand:
    command = confine(
        argv,
        cwd=str(layout["copy"]),
        writable_roots=(str(layout["copy"]),),
        temp_dir=str(layout["temp"]),
        **{"deny_network": True, **kwargs},  # type: ignore[arg-type]
    )
    assert isinstance(command, ConfinedCommand), command
    assert command.backend is SandboxBackend.APPCONTAINER
    return command


def _aces(path: Path | str) -> str:
    """The ACEs of ``path``'s DACL in SDDL, without the control flags (``P``, ``AI``).

    Rewriting a DACL through the security API sets the auto-inherited flag;
    what a grant or revocation changes is the entries.
    """
    sddl = launcher.dacl_sddl(str(path))
    return sddl[sddl.index("(") :] if "(" in sddl else ""


def _protected(path: Path | str) -> bool:
    """Whether ``path``'s DACL is protected from inheritance (the SDDL ``P`` flag)."""
    sddl = launcher.dacl_sddl(str(path))
    return "P" in sddl[2 : sddl.index("(")] if "(" in sddl else "P" in sddl[2:]


def _set_dacl(path: Path, *, null: bool = False) -> None:
    """Protect ``path``'s DACL from inheritance, keeping its entries, or make it NULL."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    assert code == 0, code
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        code = advapi32.SetNamedSecurityInfoW(
            str(path), 1, 0x80000004, None, None, None if null else dacl, None
        )
        assert code == 0, code
    finally:
        kernel32.LocalFree(descriptor)


def _container_sid(command: ConfinedCommand) -> str:
    name = command.argv[command.argv.index("--appcontainer") + 1]
    return str(launcher.appcontainer_sid(launcher._api(), name))


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path.resolve()
    paths = {name: base / name for name in ("copy", "temp", "outside")}
    for path in paths.values():
        path.mkdir()
    return paths


def test_windows_ci_runner_has_appcontainer() -> None:
    """On GitHub Actions the probe must pass, so every test below really runs there."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("only asserted on the GitHub Actions runner")
    assert exec_sandbox.filesystem_backend() is SandboxBackend.APPCONTAINER


class TestWrites:
    def test_every_kind_of_write_outside_is_denied_and_writes_inside_succeed(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        copy, outside = layout["copy"], layout["outside"]
        # The probe is copied into the root: the container reads only granted paths.
        shutil.copy(probe_module.__file__, copy / "probe.py")
        nested = copy / "pkg" / "deep"
        nested.mkdir(parents=True)
        (nested / "existing.txt").write_text("old", encoding="utf-8")
        inside = copy / "inside"
        inside.mkdir()
        probe_module.prepare(str(outside))
        before = probe_module.snapshot(str(outside))
        code = (
            "import json, runpy, sys\n"
            "probe = runpy.run_path('probe.py')\n"
            "result = probe['run'](sys.argv[1], sys.argv[2])\n"
            "open('pkg/deep/existing.txt', 'a').write('+new')\n"
            "print(json.dumps(result))\n"
        )
        command = _confine(layout, _python(code, str(inside), str(outside)), deny_network=False)

        result = _run(command)

        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        denied = {name for name, outcome in report["outside"].items() if outcome == "denied"}
        assert set(probe_module.REQUIRED) <= denied, report
        assert "ok" not in report["outside"].values(), report
        assert set(report["inside"].values()) == {"ok"}, report
        assert (nested / "existing.txt").read_text(encoding="utf-8") == "old+new"
        assert probe_module.snapshot(str(outside)) == before

    def test_a_link_beneath_a_root_does_not_carry_the_write_grant(
        self, layout: dict[str, Path]
    ) -> None:
        """Granting the root must not propagate through a junction or a symlink."""
        _require_backend()
        copy, outside = layout["copy"], layout["outside"]
        (outside / "kept.txt").write_text("keep", encoding="utf-8")
        _winapi.CreateJunction(str(outside), str(copy / "junction"))
        links = ["junction"]
        try:
            os.symlink(outside, copy / "symlink", target_is_directory=True)
            links.append("symlink")
        except OSError:
            pass  # no symlink privilege on this host: the junction still covers it
        code = (
            "import sys\n"
            "escaped = []\n"
            "for link in sys.argv[1:]:\n"
            "    if open(f'{link}/kept.txt').read() != 'keep':\n"
            "        sys.exit(4)\n"
            "    for attempt in (lambda: open(f'{link}/new.txt', 'w').write('x'),\n"
            "                    lambda: open(f'{link}/kept.txt', 'a').write('x')):\n"
            "        try:\n"
            "            attempt()\n"
            "            escaped.append(link)\n"
            "        except OSError:\n"
            "            pass\n"
            "sys.exit(3 if escaped else 0)\n"
        )
        before = launcher.dacl_sddl(str(outside / "kept.txt"))
        command = _confine(layout, _python(code, *links))

        result = _run(command)

        assert result.returncode == 0, (result.returncode, result.stderr)
        assert (outside / "kept.txt").read_text(encoding="utf-8") == "keep"
        assert not (outside / "new.txt").exists()
        assert _container_sid(command) not in launcher.dacl_sddl(str(outside / "kept.txt"))
        assert _container_sid(command) not in before


class TestProfile:
    def test_the_profile_folder_is_gone_before_the_command_runs(
        self, layout: dict[str, Path]
    ) -> None:
        """The profile folder grants the container full control: it must not exist."""
        _require_backend()
        code = (
            "import os, sys\n"
            "folder = sys.argv[1]\n"
            "try:\n"
            "    os.makedirs(folder, exist_ok=True)\n"
            "    open(os.path.join(folder, 'escaped.txt'), 'w').write('x')\n"
            "except OSError:\n"
            "    sys.exit(0 if not os.path.exists(folder) else 5)\n"
            "sys.exit(3)\n"
        )
        command = _confine(layout, ("placeholder",))
        name = command.argv[command.argv.index("--appcontainer") + 1]
        folder = Path(os.environ["LOCALAPPDATA"]) / "Packages" / name / "AC"
        argv = (*command.argv[: command.argv.index("--") + 1], *_python(code, str(folder)))

        result = subprocess.run(  # noqa: S603 - the confined argv with another command
            list(argv),
            cwd=command.cwd,
            env=dict(command.env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, (result.returncode, result.stderr)
        assert not folder.parent.exists()

    def test_temp_and_localappdata_stay_inside_the_temp_directory(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        code = "import json, os; print(json.dumps(dict(os.environ)))"
        command = _confine(layout, _python(code))

        result = _run(command)

        assert result.returncode == 0, result.stderr
        env = json.loads(result.stdout)
        temp = layout["temp"]
        for name in ("TMP", "TEMP", "LOCALAPPDATA"):
            assert Path(env[name]).is_relative_to(temp), (name, env[name])
            assert Path(env[name]).is_dir()


class TestProfileDirectories:
    """The unconfined launcher creates the profile directories inside the roots only."""

    def test_a_junction_in_the_profile_path_is_refused(self, layout: dict[str, Path]) -> None:
        _require_backend()
        outside = layout["outside"]
        _winapi.CreateJunction(str(outside), str(layout["temp"] / "Packages"))
        command = _confine(layout, _python("open('ran.txt', 'w').write('x')"))

        result = _run(command)

        assert result.returncode == launcher.EXIT_SANDBOX_FAILED, result.stderr
        assert "not a plain directory" in result.stderr
        assert list(outside.iterdir()) == []
        assert not (layout["copy"] / "ran.txt").exists()

    def test_the_effective_localappdata_is_the_one_checked(self, layout: dict[str, Path]) -> None:
        """Names differ only in case: the last one wins, for the check and the child alike."""
        _require_backend()
        outside = layout["outside"]
        command = _confine(
            layout,
            _python("open('ran.txt', 'w').write('x')"),
            env_set={"LocalAppData": str(outside)},
        )

        result = _run(command)

        assert result.returncode == launcher.EXIT_SANDBOX_FAILED, result.stderr
        assert "not inside a writable root" in result.stderr
        assert list(outside.iterdir()) == []
        assert not (layout["copy"] / "ran.txt").exists()


class TestDispatch:
    """The repository's Windows dispatch policy holds inside the sandbox."""

    @staticmethod
    def _source(*directories: object) -> dict[str, str]:
        system = os.path.join(exec_sandbox._windows_directory(), "System32")
        path = os.pathsep.join([*map(str, directories), ".", system])
        return {"PATH": path, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}

    _PASSTHROUGH = (*exec_sandbox.DEFAULT_ENV_PASSTHROUGH, "PATHEXT")

    def test_a_workspace_file_never_shadows_the_tool_on_path(self, layout: dict[str, Path]) -> None:
        _require_backend()
        copy = layout["copy"]
        interpreter = Path(sys.executable)
        # ``shutil.which`` on Windows would try the working directory first.
        (copy / f"{interpreter.stem}.cmd").write_text("@echo x> shadow.txt\r\n", encoding="utf-8")
        (copy / "only-here.cmd").write_text("@echo x> shadow.txt\r\n", encoding="utf-8")
        source = self._source(interpreter.parent)
        code = "open('ran-from-path.txt', 'w').close()"

        tool = _confine(
            layout,
            (interpreter.stem, "-I", "-c", code),
            env_source=source,
            env_passthrough=self._PASSTHROUGH,
        )
        only = _confine(
            layout, ("only-here",), env_source=source, env_passthrough=self._PASSTHROUGH
        )

        resolved = tool.argv[tool.argv.index("--") + 1]
        assert Path(resolved).parent == interpreter.parent, resolved
        assert _run(tool).returncode == 0
        assert (copy / "ran-from-path.txt").exists()
        # Present only in the working directory (and "." on PATH): not found.
        assert _run(only).returncode == launcher.EXIT_NOT_FOUND
        assert not (copy / "shadow.txt").exists()

    def test_an_absolute_path_entry_naming_the_copy_is_honored(
        self, layout: dict[str, Path]
    ) -> None:
        """Explicit configuration, as with execvpe on POSIX: replay remaps the
        worker's workspace paths, PATH included, onto the copy on purpose."""
        _require_backend()
        tools = layout["copy"] / "bin"
        tools.mkdir()
        shutil.copy(sys.executable, tools / "tool.exe")

        command = _confine(
            layout,
            ("tool", "--version"),
            env_source=self._source(tools),
            env_passthrough=self._PASSTHROUGH,
        )

        resolved = Path(command.argv[command.argv.index("--") + 1])
        assert resolved.parent == tools and resolved.stem.lower() == "tool", resolved

    def test_a_batch_file_is_refused(self, layout: dict[str, Path]) -> None:
        """cmd.exe will not run a batch file in an AppContainer: indeterminate, not failed."""
        _require_backend()
        copy, tools = layout["copy"], layout["temp"] / "bin"
        tools.mkdir()
        (tools / "tool.cmd").write_text("@echo x> ran.txt\r\n", encoding="utf-8")
        (copy / "run.cmd").write_text("@echo %1> ran.txt\r\n", encoding="utf-8")
        kwargs = {
            "cwd": str(copy),
            "writable_roots": (str(copy),),
            "temp_dir": str(layout["temp"]),
            "env_source": self._source(tools),
            "env_passthrough": self._PASSTHROUGH,
        }

        results = [
            confine(argv, **kwargs)  # type: ignore[arg-type]
            for argv in ((".\\run.cmd", "plain"), (".\\run.cmd", "%PATH%"), ("tool", "test"))
        ]

        for result in results:
            assert isinstance(result, SandboxUnavailable), result
            assert result.reason is SandboxUnavailableReason.WINDOWS_BATCH_FILE
        assert Path(results[2].detail).parent == tools  # resolved on PATH, not the copy
        assert not (copy / "ran.txt").exists()


class TestNetwork:
    _CONNECT = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    # A server in this process, a client in a second process of the container.
    _OWN_LOOPBACK = (
        "import socket, subprocess, sys\n"
        "server = socket.create_server(('127.0.0.1', 0))\n"
        "server.settimeout(10)\n"
        "port = server.getsockname()[1]\n"
        "client = ('import socket; socket.create_connection((\"127.0.0.1\", %d), '\n"
        "          'timeout=5).sendall(b\"ping\")' % port)\n"
        "subprocess.run([sys.executable, '-I', '-c', client], check=True, timeout=30)\n"
        "peer, _ = server.accept()\n"
        "sys.exit(0 if peer.recv(4) == b'ping' else 1)\n"
    )

    def test_network_is_denied(self, layout: dict[str, Path]) -> None:
        _require_backend()
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=5).close()
        except OSError:
            pytest.skip("this host has no network to deny")
        denied = _confine(layout, _python(self._CONNECT, "1.1.1.1", "53"), deny_network=True)
        allowed = _confine(layout, _python(self._CONNECT, "1.1.1.1", "53"), deny_network=False)

        assert denied.network_denied and not allowed.network_denied
        assert _run(denied).returncode == 0
        # With the client capability the same connection works: the denial
        # comes from the missing capability, not from this host.
        assert _run(allowed).returncode == 1

    @pytest.mark.parametrize("deny_network", [True, False])
    def test_loopback_connects_only_the_commands_own_processes(
        self, layout: dict[str, Path], deny_network: bool
    ) -> None:
        """As in a new Linux namespace: the command's processes reach each other
        over loopback; a loopback server outside the container is unreachable."""
        _require_backend()
        outside = socket.create_server(("127.0.0.1", 0))
        outside.settimeout(0.5)
        try:
            port = str(outside.getsockname()[1])
            own = _confine(layout, _python(self._OWN_LOOPBACK), deny_network=deny_network)
            out = _confine(
                layout, _python(self._CONNECT, "127.0.0.1", port), deny_network=deny_network
            )

            own_result, out_result = _run(own), _run(out)

            with pytest.raises(TimeoutError):
                outside.accept()
        finally:
            outside.close()
        assert own_result.returncode == 0, own_result.stderr
        assert out_result.returncode == 0, "a loopback server outside the container was reached"

    def test_the_null_device_is_unavailable(self, layout: dict[str, Path]) -> None:
        """Documented in the contract: an AppContainer cannot open NUL."""
        _require_backend()
        code = (
            "import os, sys\n"
            "try:\n"
            "    open(os.devnull, 'w').close()\n"
            "except PermissionError:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )

        assert _run(_confine(layout, _python(code))).returncode == 0


def _process_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) != 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


class TestTimeout:
    # The grandchild records its pid, then writes ``late.txt`` after a delay
    # that outlasts every timeout below: it must never get there.
    _TREE = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c',\n"
        '    \'import os, time; open("started.txt", "w").write(str(os.getpid())); \'\n'
        '    \'time.sleep(12); open("late.txt", "w").write("x")\'])\n'
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )

    def test_killing_the_launcher_kills_the_whole_tree(self, layout: dict[str, Path]) -> None:
        _require_backend()
        command = _confine(layout, _python(self._TREE))
        process = subprocess.Popen(  # noqa: S603 - argv built by confine
            list(command.argv),
            cwd=command.cwd,
            env=dict(command.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        grandchild = int(process.stdout.readline())
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)

        process.kill()  # what a caller's timeout does
        process.communicate(timeout=60)

        deadline = time.monotonic() + 10
        while _process_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _process_alive(grandchild)
        time.sleep(13)
        assert not (layout["copy"] / "late.txt").exists()

    def test_the_callers_timeout_path_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """Through the replay's own runner: timeout, tree killed, roots deletable."""
        import asyncio

        from ouroboros.orchestrator.verify_command_runner import _run_process

        _require_backend()
        scratch = tmp_path.resolve() / "scratch"
        layout = {"copy": scratch / "workspace", "temp": scratch / "tmp"}
        for path in layout.values():
            path.mkdir(parents=True)
        command = _confine(layout, _python(self._TREE))
        sid = _container_sid(command)

        run = asyncio.run(
            _run_process(command.argv, cwd=command.cwd, env=command.env, timeout_seconds=8)
        )

        assert run.timed_out
        started = layout["copy"] / "started.txt"
        assert started.exists(), "the tree never started: the timeout proves nothing"
        assert not _process_alive(int(started.read_text(encoding="utf-8")))
        time.sleep(13)
        assert not (layout["copy"] / "late.txt").exists()
        # The caller deletes its roots; the per-run grants go with them.
        shutil.rmtree(scratch)
        assert not scratch.exists()
        for path in exec_sandbox._windows_read_paths(()):
            assert sid not in launcher.dacl_sddl(path)


class TestGrants:
    def test_write_grants_are_revoked_after_every_run(self, layout: dict[str, Path]) -> None:
        _require_backend()
        copy, temp = layout["copy"], layout["temp"]
        (copy / "sub").mkdir()
        (copy / "sub" / "file.txt").write_text("x", encoding="utf-8")
        # A protected root keeps its protection through grant and revocation.
        _set_dacl(copy)
        watched = [copy, copy / "sub", copy / "sub" / "file.txt", temp]
        before = {path: (_aces(path), _protected(path)) for path in watched}
        assert before[copy][1] and not before[temp][1]
        commands = {
            "succeeds": (_python("open('made.txt', 'w').write('x')"), 0),
            "fails": (_python("import sys; sys.exit(3)"), 3),
            "is not found": (("ouroboros-no-such-command",), 127),
        }
        for label, (argv, status) in commands.items():
            command = _confine(layout, argv)
            sid = _container_sid(command)

            result = _run(command)

            assert result.returncode == status, (label, result.stderr)
            after = {path: (_aces(path), _protected(path)) for path in watched}
            assert after == before, label
            if (copy / "made.txt").exists():
                assert sid not in launcher.dacl_sddl(str(copy / "made.txt")), label

    def test_the_persistent_read_grant_is_recorded_and_removable(
        self, layout: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_backend()
        manifest = tmp_path.resolve() / "state" / "read-grants.jsonl"
        monkeypatch.setattr(exec_sandbox, "_read_grant_manifest", lambda: manifest)
        deps = tmp_path.resolve() / "deps"
        deps.mkdir()
        (deps / "module.txt").write_text("dependency", encoding="utf-8")
        original = _aces(deps)
        _winapi.CreateJunction(str(deps), str(layout["copy"] / "deps"))
        read = _python("import sys; sys.exit(0 if open('deps/module.txt').read() else 1)")

        result = _run(_confine(layout, read))

        assert result.returncode == 0, result.stderr
        recorded = [json.loads(line)["path"] for line in manifest.read_text().splitlines()]
        assert recorded == [str(deps)]
        capability = str(launcher.capability_sid(launcher._api()))
        assert capability in launcher.dacl_sddl(str(deps))
        assert capability in launcher.dacl_sddl(str(deps / "module.txt"))

        removed = exec_sandbox.remove_persistent_read_grants()

        assert removed == (str(deps),)
        assert _aces(deps) == original
        assert capability not in launcher.dacl_sddl(str(deps / "module.txt"))
        assert not manifest.exists()
        # Without the grant the container cannot read it; the next run grants it again.
        second = _confine(layout, read)
        assert _run(second).returncode == 0
        assert exec_sandbox.remove_persistent_read_grants() == (str(deps),)

    def test_removal_follows_the_granted_object_not_its_name(
        self, layout: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dependency renamed after its grant is still cleaned; the new object
        at the old name is not touched; a deleted one needs nothing."""
        _require_backend()
        manifest = tmp_path.resolve() / "state" / "read-grants.jsonl"
        monkeypatch.setattr(exec_sandbox, "_read_grant_manifest", lambda: manifest)
        base = tmp_path.resolve()
        deps, gone = base / "deps", base / "gone"
        for tree in (deps, gone):
            tree.mkdir()
            (tree / "module.txt").write_text("dependency", encoding="utf-8")
            _winapi.CreateJunction(str(tree), str(layout["copy"] / tree.name))
        read = _python(
            "import sys\n"
            "sys.exit(0 if open('deps/module.txt').read() and open('gone/module.txt').read() else 1)"
        )
        assert _run(_confine(layout, read)).returncode == 0
        capability = str(launcher.capability_sid(launcher._api()))
        moved = base / "deps-moved"
        deps.rename(moved)
        deps.mkdir()
        replacement = _aces(deps)
        shutil.rmtree(gone)
        assert capability in launcher.dacl_sddl(str(moved / "module.txt"))

        removed = exec_sandbox.remove_persistent_read_grants()

        assert removed == (str(deps),)
        assert capability not in launcher.dacl_sddl(str(moved))
        assert capability not in launcher.dacl_sddl(str(moved / "module.txt"))
        assert _aces(deps) == replacement
        assert not manifest.exists()

    def test_a_null_dacl_root_is_refused_and_left_as_it_was(self, layout: dict[str, Path]) -> None:
        """A NULL DACL means no access control; one grant cannot be added to it."""
        _require_backend()
        copy = layout["copy"]
        _set_dacl(copy, null=True)
        before = launcher.dacl_sddl(str(copy))
        assert "NO_ACCESS_CONTROL" in before
        command = _confine(layout, _python("open('ran.txt', 'w').write('x')"))

        result = _run(command)

        assert result.returncode == launcher.EXIT_SANDBOX_FAILED, result.stderr
        assert "NULL DACL" in result.stderr
        assert not (copy / "ran.txt").exists()
        assert launcher.dacl_sddl(str(copy)) == before


class TestUnavailable:
    def test_no_backend_when_the_appcontainer_cannot_be_created(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing AppContainer setup makes the probe fail: nothing runs, ever."""

        def refuse(api: object, name: str) -> object:
            raise launcher.SandboxError(f"CreateAppContainerProfile({name!r}) failed")

        marker = layout["copy"] / "ran.txt"
        before = _aces(layout["copy"])
        status = os.stat(layout["copy"])
        env = {"PATH": os.defpath, "LOCALAPPDATA": str(layout["copy"])}
        monkeypatch.setenv(launcher.COMMAND_ENV_VARIABLE, json.dumps(env))
        monkeypatch.setattr(launcher, "create_profile", refuse)
        argv = [
            "--appcontainer",
            "ouroboros.sandbox.test",
            "--manifest",
            str(layout["temp"] / "grants.jsonl"),
            "--root",
            str(layout["copy"]),
            str(status.st_dev),
            str(status.st_ino),
            "--",
            *_python(f"open({str(marker)!r}, 'w').close()"),
        ]

        # The root itself verifies: the refusal below is the AppContainer's.
        launcher.Root(str(layout["copy"]), status.st_dev, status.st_ino).close()

        assert launcher.main(argv) == launcher.EXIT_SANDBOX_FAILED
        assert not marker.exists()
        assert _aces(layout["copy"]) == before

    def test_an_invalid_appcontainer_name_reports_the_sandbox_unavailable(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the real Win32 call fails, the probe fails, confine refuses."""
        # 65 valid characters: one over CreateAppContainerProfile's limit, and
        # short enough that every path built from it stays under MAX_PATH.
        name = "ouroboros.sandbox." + "a" * 47
        assert len(name) == 65
        with pytest.raises(launcher.SandboxError, match="CreateAppContainerProfile"):
            launcher.create_profile(launcher._api(), name)
        monkeypatch.setattr(exec_sandbox, "_appcontainer_name", lambda: name)
        probe = exec_sandbox.filesystem_backend.__wrapped__  # type: ignore[attr-defined]
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", probe)

        result = confine(
            _python("pass"),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
        )

        assert isinstance(result, SandboxUnavailable)
        assert result.reason is SandboxUnavailableReason.SANDBOX_UNAVAILABLE

    def test_a_root_swapped_after_confine_runs_nothing(self, layout: dict[str, Path]) -> None:
        _require_backend()
        copy, outside = layout["copy"], layout["outside"]
        command = _confine(layout, _python("open('escaped.txt', 'w').write('x')"))
        copy.rename(copy.with_name("copy-moved"))
        _winapi.CreateJunction(str(outside), str(copy))

        result = _run(command)

        assert result.returncode == launcher.EXIT_SANDBOX_FAILED, result.stderr
        assert not (outside / "escaped.txt").exists()


class TestOtherProcesses:
    _READER = (
        "import ctypes, sys\n"
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel32.OpenProcess.restype = ctypes.c_void_p\n"
        "opened = []\n"
        "for pid in map(int, sys.argv[1:]):\n"
        "    # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ: what reading\n"
        "    # another process's environment block needs.\n"
        "    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)\n"
        "    if handle:\n"
        "        opened.append(pid)\n"
        "        kernel32.CloseHandle(ctypes.c_void_p(handle))\n"
        "    elif ctypes.get_last_error() != 5:\n"
        "        sys.exit(4)\n"
        "sys.exit(3 if opened else 0)\n"
    )

    def test_another_process_environment_cannot_be_read(self, layout: dict[str, Path]) -> None:
        _require_backend()
        secret_holder = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-I", "-c", "import time; time.sleep(120)"],
            env={**os.environ, "OUROBOROS_TEST_SECRET": "hunter2-sandbox-secret"},
        )
        try:
            targets = (str(os.getpid()), str(secret_holder.pid))
            unconfined = subprocess.run(  # noqa: S603 - fixed argv
                _python(self._READER, *targets), timeout=60, check=False
            )
            assert unconfined.returncode == 3, "the same read works unconfined"
            command = _confine(layout, _python(self._READER, *targets))

            confined = _run(command)
        finally:
            secret_holder.kill()
            secret_holder.wait(timeout=30)

        assert command.isolates_process_environments
        assert confined.returncode == 0, confined.stderr
