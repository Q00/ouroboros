"""The execution sandbox for controller-run commands (``runtime/exec_sandbox.py``).

Tests that need the real backend of this host (``sandbox-exec`` on macOS,
Landlock on Linux) skip with the probe's reason where it is unavailable; the
refusal path is tested on every host by standing in for the probe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from ouroboros.runtime import _confine_exec, exec_sandbox
from ouroboros.runtime import _sandbox_probe as probe_module
from ouroboros.runtime.exec_sandbox import (
    DEFAULT_ENV_PASSTHROUGH,
    ConfinedCommand,
    NetworkPlan,
    SandboxBackend,
    SandboxUnavailable,
    SandboxUnavailableReason,
    build_environment,
    confine,
)


def _require_backend(*, deny_network: bool = False) -> None:
    reason = exec_sandbox.sandbox_unavailable_reason(deny_network=deny_network)
    if reason is not None:
        pytest.skip(f"execution sandbox unavailable on this host: {reason.value}")


def _run(command: ConfinedCommand) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv built by confine
        list(command.argv),
        cwd=command.cwd,
        env=dict(command.env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _python(code: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", code, *args)


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("copy", "temp", "outside")}
    for path in paths.values():
        path.mkdir()
    return paths


class TestRealBackend:
    def test_writes_inside_succeed_and_writes_outside_are_blocked(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        copy, outside = layout["copy"].resolve(), layout["outside"].resolve()
        code = (
            "import os, sys, tempfile\n"
            "open('inside.txt', 'w').write('ok')\n"
            "os.mkdir('made'); os.rename('inside.txt', 'made/moved.txt')\n"
            "fd, name = tempfile.mkstemp(); os.write(fd, b'tmp'); os.close(fd)\n"
            "open(os.devnull, 'w').write('discarded')\n"
            "try:\n"
            "    open(os.path.join(sys.argv[1], 'escaped.txt'), 'w').write('no')\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            "sys.exit(9)\n"
        )
        command = confine(
            _python(code, str(outside)),
            cwd=str(copy),
            writable_roots=(str(copy),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)
        assert command.backend in (SandboxBackend.SANDBOX_EXEC, SandboxBackend.LANDLOCK)

        result = _run(command)

        assert result.returncode == 0, result.stderr
        assert (copy / "made" / "moved.txt").read_text(encoding="utf-8") == "ok"
        assert len(list(layout["temp"].iterdir())) == 1
        assert not (outside / "escaped.txt").exists()

    def test_a_shell_script_cannot_write_outside_the_copy(
        self, layout: dict[str, Path], tmp_path: Path
    ) -> None:
        _require_backend()
        copy = layout["copy"].resolve()
        target = layout["outside"].resolve() / "from_script.txt"
        script = copy / "run.sh"
        script.write_text(f"#!/bin/sh\necho x > {target}\n", encoding="utf-8")
        script.chmod(0o755)
        command = confine(
            ("./run.sh",),
            cwd=str(copy),
            writable_roots=(str(copy),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)

        result = _run(command)

        assert result.returncode != 0
        assert not target.exists()

    def test_metadata_outside_cannot_change(self, layout: dict[str, Path]) -> None:
        _require_backend()
        victim = layout["outside"].resolve() / "victim"
        victim.write_text("keep", encoding="utf-8")
        victim.chmod(0o600)
        os.utime(victim, (1_000_000, 1_000_000))
        before = os.stat(victim)
        code = (
            "import os, sys\n"
            "path = sys.argv[1]\n"
            "fd = os.open(path, os.O_RDONLY)\n"
            "for attempt in (lambda: os.chmod(path, 0o644), lambda: os.fchmod(fd, 0o644),\n"
            "                lambda: os.utime(path, (0, 0)),\n"
            "                lambda: os.chown(path, os.getuid(), -1)):\n"
            "    try:\n"
            "        attempt()\n"
            "    except OSError:\n"
            "        continue\n"
            "    sys.exit(3)\n"
        )
        command = confine(
            _python(code, str(victim)),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)

        result = _run(command)

        after = os.stat(victim)
        assert result.returncode == 0, result.stderr
        assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)

    def test_a_root_swapped_for_a_symlink_after_confine_runs_nothing(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        copy, outside = layout["copy"].resolve(), layout["outside"].resolve()
        command = confine(
            _python("open('escaped.txt', 'w').write('x')"),
            cwd=str(copy),
            writable_roots=(str(copy),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)
        copy.rename(copy.with_name("copy-moved"))
        copy.symlink_to(outside, target_is_directory=True)

        result = _run(command)

        assert result.returncode == _confine_exec.EXIT_SANDBOX_FAILED, result.stderr
        assert not (outside / "escaped.txt").exists()

    def test_a_root_holding_a_hard_link_is_refused(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", lambda: SandboxBackend.LANDLOCK)
        outside_file = layout["outside"] / "shared.txt"
        outside_file.write_text("keep", encoding="utf-8")
        os.link(outside_file, layout["copy"] / "alias.txt")

        result = confine(
            ("true",),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )

        assert isinstance(result, SandboxUnavailable)
        assert result.reason is SandboxUnavailableReason.ALIASED_WRITABLE_ROOT
        assert result.detail.endswith("alias.txt")

    def test_a_hard_link_added_after_confine_runs_nothing(self, layout: dict[str, Path]) -> None:
        _require_backend()
        copy = layout["copy"].resolve()
        victim = layout["outside"].resolve() / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        command = confine(
            _python("open('alias.txt', 'w').write('ESCAPED')"),
            cwd=str(copy),
            writable_roots=(str(copy),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)
        os.link(victim, copy / "alias.txt")

        result = _run(command)

        assert result.returncode == _confine_exec.EXIT_SANDBOX_FAILED, result.stderr
        assert victim.read_text(encoding="utf-8") == "KEEP"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
    def test_a_hard_link_in_an_unreadable_subtree_runs_nothing(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        copy = layout["copy"].resolve()
        victim = layout["outside"].resolve() / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        hidden = copy / "hidden"
        hidden.mkdir()
        os.link(victim, hidden / "alias.txt")
        hidden.chmod(0o300)
        try:
            refused = confine(
                ("true",),
                cwd=str(copy),
                writable_roots=(str(copy),),
                temp_dir=str(layout["temp"]),
                deny_network=False,
            )
            hidden.chmod(0o700)
            (hidden / "alias.txt").unlink()
            command = confine(
                _python("open('hidden/alias.txt', 'w').write('ESCAPED')"),
                cwd=str(copy),
                writable_roots=(str(copy),),
                temp_dir=str(layout["temp"]),
                deny_network=False,
            )
            assert isinstance(command, ConfinedCommand)
            # The link and the unreadable directory appear after confine().
            os.link(victim, hidden / "alias.txt")
            hidden.chmod(0o300)

            result = _run(command)
        finally:
            hidden.chmod(0o700)

        assert isinstance(refused, SandboxUnavailable)
        assert refused.reason is SandboxUnavailableReason.ALIASED_WRITABLE_ROOT
        assert result.returncode == _confine_exec.EXIT_SANDBOX_FAILED, result.stderr
        assert victim.read_text(encoding="utf-8") == "KEEP"

    def test_writable_root_with_quote_and_backslash_in_its_name(self, tmp_path: Path) -> None:
        _require_backend()
        root = tmp_path / 'we"ird\\dir'
        temp = tmp_path / "temp"
        root.mkdir()
        temp.mkdir()
        command = confine(
            _python("open('f', 'w').write('ok')"),
            cwd=str(root),
            writable_roots=(str(root),),
            temp_dir=str(temp),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand)

        result = _run(command)

        assert result.returncode == 0, result.stderr
        assert (root / "f").read_text(encoding="utf-8") == "ok"

    def test_network_is_denied_when_requested(self, layout: dict[str, Path]) -> None:
        _require_backend(deny_network=True)
        code = (
            "import socket, sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3).close()\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        except OSError:
            pytest.skip("this host has no network to deny")
        command = confine(
            _python(code),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=True,
        )
        assert isinstance(command, ConfinedCommand) and command.network_denied

        result = _run(command)

        assert result.returncode == 0, result.stderr

    def test_loopback_stays_available_when_network_is_denied(self, layout: dict[str, Path]) -> None:
        _require_backend(deny_network=True)
        code = (
            "import socket, sys\n"
            "server = socket.create_server(('127.0.0.1', 0))\n"
            "client = socket.create_connection(server.getsockname(), timeout=3)\n"
            "peer, _ = server.accept()\n"
            "client.sendall(b'ping')\n"
            "sys.exit(0 if peer.recv(4) == b'ping' else 1)\n"
        )
        command = confine(
            _python(code),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=True,
        )
        assert isinstance(command, ConfinedCommand) and command.network_denied

        result = _run(command)

        assert result.returncode == 0, result.stderr

    def test_child_environment_is_the_allowlist(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_backend()
        monkeypatch.setenv("OUROBOROS_TEST_SECRET", "leak")
        monkeypatch.setenv("LANG", "C.UTF-8")
        command = confine(
            _python("import json, os; print(json.dumps(dict(os.environ)))"),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
            env_set={"EXTRA": "1"},
        )
        assert isinstance(command, ConfinedCommand)

        result = _run(command)

        env = json.loads(result.stdout)
        # ``__CF_USER_TEXT_ENCODING`` is added by macOS to every process.
        env.pop("__CF_USER_TEXT_ENCODING", None)
        temp = os.path.realpath(layout["temp"])
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == env["HOME"] == temp
        assert env["LANG"] == "C.UTF-8" and env["EXTRA"] == "1"
        assert "OUROBOROS_TEST_SECRET" not in env
        allowed = {*DEFAULT_ENV_PASSTHROUGH, "TMPDIR", "TMP", "TEMP", "HOME", "EXTRA"}
        assert set(env) <= allowed


class TestOtherProcessEnvironments:
    _READER = (
        "import os, sys\n"
        "leaked = []\n"
        "for part in ('environ', 'mem', 'maps'):\n"
        "    try:\n"
        "        with open(f'/proc/{os.getppid()}/{part}', 'rb') as handle:\n"
        "            leaked.append(b'hunter2-sandbox-secret' in handle.read(1 << 20))\n"
        "    except OSError:\n"
        "        pass\n"
        "with open('/proc/self/environ', 'rb') as handle:\n"
        "    handle.read()\n"
        "sys.exit(3 if any(leaked) else 0)\n"
    )

    @staticmethod
    def _through_parent_holding_a_secret(argv: tuple[str, ...], env: dict[str, str]) -> int:
        """Run ``argv`` as the child of a process whose environment holds the secret.

        ``/proc/<pid>/environ`` shows the environment a process started with,
        so the secret must be in the parent's initial environment.
        """
        launcher = (
            "import json, subprocess, sys\n"
            "argv, env = json.loads(sys.argv[1])\n"
            "sys.exit(subprocess.run(argv, env=env).returncode)\n"
        )
        parent_env = {**env, "OUROBOROS_TEST_PARENT_SECRET": "hunter2-sandbox-secret"}
        return subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-I", "-c", launcher, json.dumps([list(argv), env])],
            env=parent_env,
            timeout=60,
            check=False,
        ).returncode

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
    def test_confined_child_cannot_read_the_parent_environment(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        unconfined = self._through_parent_holding_a_secret(
            _python(self._READER), {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        )
        assert unconfined == 3, "a same-user /proc read works unconfined"
        command = confine(
            _python(self._READER),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )
        assert isinstance(command, ConfinedCommand) and command.isolates_process_environments

        confined = self._through_parent_holding_a_secret(command.argv, dict(command.env))

        assert confined == 0

    @pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
    def test_macos_reports_that_it_cannot_hide_other_environments(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        command = confine(
            ("true",),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
        )

        assert isinstance(command, ConfinedCommand)
        assert command.isolates_process_environments is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux only")
def test_linux_ci_runner_has_landlock() -> None:
    """On GitHub Actions the Linux backend must exist, so its tests really run there."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("only asserted on the GitHub Actions runner")
    abi = _confine_exec.landlock_abi()
    assert abi >= _confine_exec.MIN_LANDLOCK_ABI, (
        f"Landlock ABI {abi} unusable on this runner (kernel {os.uname().release})"
    )
    assert exec_sandbox.filesystem_backend() is SandboxBackend.LANDLOCK


class TestUnavailable:
    def test_no_filesystem_backend_is_indeterminate_and_runs_nothing(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", lambda: None)

        result = confine(
            ("touch", str(layout["outside"] / "ran")),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
        )

        assert isinstance(result, SandboxUnavailable)
        assert result.reason is SandboxUnavailableReason.SANDBOX_UNAVAILABLE
        assert result.outcome == "indeterminate"

    def test_network_denial_unavailable_is_indeterminate_only_when_requested(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", lambda: SandboxBackend.LANDLOCK)
        monkeypatch.setattr(exec_sandbox, "_process_has_only_loopback", lambda: False)
        monkeypatch.setattr(exec_sandbox, "_unshare_prefix", lambda: None)
        kwargs = {
            "cwd": str(layout["copy"]),
            "writable_roots": (str(layout["copy"]),),
            "temp_dir": str(layout["temp"]),
        }

        denied = confine(("true",), deny_network=True, **kwargs)  # type: ignore[arg-type]
        allowed = confine(("true",), deny_network=False, **kwargs)  # type: ignore[arg-type]

        assert isinstance(denied, SandboxUnavailable)
        assert denied.reason is SandboxUnavailableReason.NETWORK_ISOLATION_UNAVAILABLE
        assert isinstance(allowed, ConfinedCommand) and not allowed.network_denied

    def test_unsupported_platform_has_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(exec_sandbox.sys, "platform", "win32")

        assert exec_sandbox.filesystem_backend.__wrapped__() is None  # type: ignore[attr-defined]

    @pytest.mark.parametrize("root", ["relative/dir", "/nonexistent/ouroboros-sandbox-root"])
    def test_invalid_writable_root(self, tmp_path: Path, root: str) -> None:
        result = confine(
            ("true",), cwd=str(tmp_path), writable_roots=(root,), temp_dir=str(tmp_path)
        )

        assert isinstance(result, SandboxUnavailable)
        assert result.reason is SandboxUnavailableReason.INVALID_WRITABLE_ROOT


class TestNetworkPlan:
    @pytest.mark.parametrize(
        ("interfaces", "unshare", "plan"),
        [
            ([(1, "lo")], None, NetworkPlan.CURRENT_NAMESPACE),
            ([(1, "lo"), (2, "eth0")], ("unshare", "--"), NetworkPlan.NEW_NAMESPACE),
            ([(1, "lo"), (2, "eth0")], None, None),
            ([], None, None),
        ],
    )
    def test_network_plan_reads_the_namespace_on_every_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        interfaces: list[tuple[int, str]],
        unshare: tuple[str, ...] | None,
        plan: NetworkPlan | None,
    ) -> None:
        monkeypatch.setattr(exec_sandbox.socket, "if_nameindex", lambda: interfaces)
        monkeypatch.setattr(exec_sandbox, "_unshare_prefix", lambda: unshare)

        assert exec_sandbox._network_plan(SandboxBackend.LANDLOCK, True) is plan
        assert exec_sandbox._network_plan(SandboxBackend.LANDLOCK, False) is NetworkPlan.ALLOW
        assert exec_sandbox._network_plan(SandboxBackend.SANDBOX_EXEC, True) is NetworkPlan.PROFILE

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux network namespaces")
    def test_network_interface_appearing_after_the_check_runs_nothing(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parent's only-loopback check is not proof: the helper checks again."""
        _require_backend()
        if exec_sandbox._process_has_only_loopback():
            pytest.skip("this host's namespace already has only loopback")
        # The controller saw only ``lo``; by the time the command starts, the
        # namespace has other interfaces (this host's real ones).
        monkeypatch.setattr(exec_sandbox, "_process_has_only_loopback", lambda: True)
        marker = layout["copy"].resolve() / "ran"
        command = confine(
            _python(f"open({str(marker)!r}, 'w').close()"),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=True,
        )
        assert isinstance(command, ConfinedCommand) and command.network_denied

        result = _run(command)

        assert result.returncode == _confine_exec.EXIT_SANDBOX_FAILED, result.stderr
        assert "network is not isolated" in result.stderr
        assert not marker.exists()

    def test_network_final_check_refuses_other_interfaces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import socket as socket_module

        monkeypatch.setenv(_confine_exec.COMMAND_ENV_VARIABLE, "{}")
        monkeypatch.setattr(socket_module, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
        status = os.stat(tmp_path)
        root = ["--root", str(tmp_path), str(status.st_dev), str(status.st_ino)]

        result = _confine_exec.main(["--require-loopback-only", *root, "--", "true"])

        assert result == _confine_exec.EXIT_SANDBOX_FAILED


class TestLaunchers:
    """Launchers run before confinement, so they never come from ``PATH``."""

    @staticmethod
    def _fake(directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        fake = directory / name
        fake.write_text("#!/bin/sh\ntouch /tmp/launcher-hijacked\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def test_path_is_never_consulted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("unshare", "sandbox-exec", "true"):
            self._fake(tmp_path / "bin", name)
        monkeypatch.setenv("PATH", f"bin{os.pathsep}{tmp_path / 'bin'}")
        monkeypatch.chdir(tmp_path)

        for name in ("unshare", "sandbox-exec"):
            found = exec_sandbox._trusted_launcher(name)
            assert found is None or (
                os.path.isabs(found) and not found.startswith(str(tmp_path.resolve()))
            )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root owns every file it creates")
    def test_a_user_owned_or_relative_launcher_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake(tmp_path / "bin", "unshare")
        monkeypatch.chdir(tmp_path)

        assert exec_sandbox._trusted_launcher("unshare", (str(tmp_path / "bin"),)) is None
        assert exec_sandbox._trusted_launcher("unshare", ("bin",)) is None

    def test_a_symlink_resolves_to_its_canonical_root_owned_target(self, tmp_path: Path) -> None:
        target = os.path.realpath("/bin/sh")
        if not exec_sandbox._root_owned_and_unwritable(target):
            pytest.skip("/bin/sh is not root-owned here")
        (tmp_path / "sh").symlink_to(target)

        assert exec_sandbox._trusted_launcher("sh", (str(tmp_path),)) == target

    def test_the_confined_argv_starts_with_an_absolute_trusted_launcher(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend(deny_network=True)
        command = confine(
            ("true",),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=True,
        )

        assert isinstance(command, ConfinedCommand)
        assert os.path.isabs(command.argv[0])
        assert command.argv[0] in {
            exec_sandbox._trusted_launcher("sandbox-exec"),
            exec_sandbox._trusted_launcher("unshare"),
            exec_sandbox._interpreter(),
        }


class TestOffSwitch:
    def test_disabled_runs_unconfined_and_says_so(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", lambda: None)

        result = confine(
            ("true",),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            enabled=False,
        )

        assert isinstance(result, ConfinedCommand)
        assert result.backend is SandboxBackend.DISABLED
        assert result.argv == ("true",) and not result.network_denied
        assert result.env == result.command_env
        assert exec_sandbox.sandbox_unavailable_reason(enabled=False) is None
        assert (
            exec_sandbox.sandbox_unavailable_reason()
            is SandboxUnavailableReason.SANDBOX_UNAVAILABLE
        )


class TestEnvironment:
    def test_built_from_scratch(self) -> None:
        source = {"PATH": "/bin", "LANG": "C", "HOME": "/home/u", "AWS_SECRET_ACCESS_KEY": "x"}

        env = build_environment("/scratch/tmp", source=source, overrides={"X": "1"})

        assert env == {
            "PATH": "/bin",
            "LANG": "C",
            "TMPDIR": "/scratch/tmp",
            "TMP": "/scratch/tmp",
            "TEMP": "/scratch/tmp",
            "HOME": "/scratch/tmp",
            "X": "1",
        }

    def test_empty_passthrough_values_are_preserved(self) -> None:
        source = {"PATH": "", "HOME": "", "LANG": ""}

        env = build_environment(
            "/scratch/tmp", source=source, passthrough=(*DEFAULT_ENV_PASSTHROUGH, "HOME")
        )

        assert env["PATH"] == "" and env["HOME"] == "" and env["LANG"] == ""

    def test_home_is_kept_only_when_passed_through(self) -> None:
        source = {"PATH": "/bin", "HOME": "/home/u"}

        env = build_environment(
            "/scratch/tmp", source=source, passthrough=(*DEFAULT_ENV_PASSTHROUGH, "HOME")
        )

        assert env["HOME"] == "/home/u"


class TestNothingRunsBeforeConfinement:
    """The command's environment takes effect only when the command is exec'd."""

    def test_launchers_start_with_the_bootstrap_environment(
        self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exec_sandbox, "filesystem_backend", lambda: SandboxBackend.LANDLOCK)
        monkeypatch.setattr(exec_sandbox, "_process_has_only_loopback", lambda: False)
        monkeypatch.setattr(exec_sandbox, "_unshare_prefix", lambda: ("unshare", "--"))
        loader = {"LD_PRELOAD": "./evil.so", "DYLD_INSERT_LIBRARIES": "./evil.dylib"}

        command = confine(
            ("./run_tests.sh",),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            env_set=loader,
        )

        assert isinstance(command, ConfinedCommand)
        assert set(command.env) == {"PATH", _confine_exec.COMMAND_ENV_VARIABLE}
        assert command.env["PATH"] == os.defpath
        carried = json.loads(command.env[_confine_exec.COMMAND_ENV_VARIABLE])
        assert carried == dict(command.command_env)
        assert carried["LD_PRELOAD"] == "./evil.so"

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc LD_PRELOAD")
    def test_a_preload_the_command_names_reaches_only_the_command(
        self, layout: dict[str, Path]
    ) -> None:
        _require_backend()
        missing = "/nonexistent/ouroboros-sandbox-preload.so"
        command = confine(
            _python("pass"),
            cwd=str(layout["copy"]),
            writable_roots=(str(layout["copy"]),),
            temp_dir=str(layout["temp"]),
            deny_network=False,
            env_set={"LD_PRELOAD": missing},
        )
        assert isinstance(command, ConfinedCommand)

        result = _run(command)

        # The dynamic loader reports the missing preload once per process that
        # starts with it: the command, and none of the launchers before it.
        assert result.returncode == 0, result.stderr
        assert result.stderr.count(missing) == 1, result.stderr


class TestProbeMatrix:
    _ALL_OK = dict.fromkeys(probe_module.REQUIRED, "ok")

    def _confined(self, **overrides: str) -> dict[str, object]:
        outside = dict.fromkeys(probe_module.REQUIRED, "denied")
        outside.update(overrides)
        return {"outside": outside, "inside": {"create": "ok"}, "unchanged": True}

    def test_every_possible_mutation_must_be_denied(self) -> None:
        baseline = {"outside": {**self._ALL_OK, "setxattr": "ok"}}

        assert exec_sandbox._matrix_confines(baseline, self._confined(setxattr="denied"))
        assert not exec_sandbox._matrix_confines(baseline, self._confined(setxattr="ok"))
        assert not exec_sandbox._matrix_confines(baseline, self._confined(chmod="ok"))

    def test_a_probe_that_proves_nothing_confines_nothing(self) -> None:
        baseline = {"outside": {**self._ALL_OK, "chmod": "unsupported"}}

        assert not exec_sandbox._matrix_confines(baseline, self._confined())
        assert not exec_sandbox._matrix_confines(None, self._confined())

    def test_outside_changes_or_failed_inside_writes_fail_the_probe(self) -> None:
        baseline = {"outside": dict(self._ALL_OK)}
        changed = {**self._confined(), "unchanged": False}
        blocked_inside = {**self._confined(), "inside": {"create": "denied"}}

        assert not exec_sandbox._matrix_confines(baseline, changed)
        assert not exec_sandbox._matrix_confines(baseline, blocked_inside)


class TestMetadataFilter:
    @pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
    def test_every_jump_lands_on_a_return(self, machine: str) -> None:
        program = _confine_exec.metadata_filter(machine)
        ret = 0x06
        deny, allow = program[-2], program[-1]

        assert allow == (ret, 0, 0, 0x7FFF0000) and deny == (ret, 0, 0, 0x00050001)
        for index, (code, jt, jf, _k) in enumerate(program):
            if code in (0x15, 0x35):
                for offset in (jt, jf):
                    assert index + 1 + offset < len(program)

    @staticmethod
    def _evaluate(program: list[tuple[int, int, int, int]], data: dict[int, int]) -> int:
        """Run the classic-BPF program on a seccomp_data given as {offset: u32}."""
        accumulator, pc = 0, 0
        while True:
            code, jt, jf, k = program[pc]
            if code == 0x20:
                accumulator = data.get(k, 0)
                pc += 1
            elif code in (0x15, 0x35):
                taken = accumulator == k if code == 0x15 else accumulator >= k
                pc += 1 + (jt if taken else jf)
            elif code == 0x06:
                return k
            else:  # pragma: no cover - the builder emits no other opcode
                raise AssertionError(hex(code))

    @pytest.mark.parametrize(
        ("machine", "arch", "ioctl", "fchmod", "read"),
        [("x86_64", 0xC000003E, 16, 91, 0), ("aarch64", 0xC00000B7, 29, 52, 63)],
    )
    def test_ioctl_is_an_allowlist(
        self, machine: str, arch: int, ioctl: int, fchmod: int, read: int
    ) -> None:
        program = _confine_exec.metadata_filter(machine)
        allow, eperm = 0x7FFF0000, 0x00050001

        def verdict(nr: int, arg1: int = 0, audit_arch: int = arch) -> int:
            return self._evaluate(program, {0: nr, 4: audit_arch, 24: arg1})

        for request in _confine_exec.ALLOWED_IOCTLS.values():
            assert verdict(ioctl, request) == allow
        # fs-verity, fscrypt policy, chattr flags, fsxattr, and an arbitrary request.
        for request in (0x40806685, 0x800C6613, 0x40086602, 0x401C5820, 0x12345678):
            assert verdict(ioctl, request) == eperm
        assert verdict(fchmod) == eperm
        assert verdict(read) == allow
        assert verdict(read, audit_arch=0x40000003) == eperm  # i386 compat call

    def test_unknown_architecture_is_refused(self) -> None:
        with pytest.raises(_confine_exec.SandboxError):
            _confine_exec.metadata_filter("riscv64")


class TestLandlockAccessMask:
    def test_rights_follow_the_abi(self) -> None:
        refer, truncate = 1 << 13, 1 << 14

        assert _confine_exec.handled_write_access(0) == 0
        abi1 = _confine_exec.handled_write_access(1)
        assert abi1 and not abi1 & (refer | truncate)
        assert _confine_exec.handled_write_access(2) == abi1 | refer
        assert _confine_exec.handled_write_access(3) == abi1 | refer | truncate
        assert _confine_exec.handled_write_access(8) == abi1 | refer | truncate
        # Reading and executing are never handled.
        read_rights = (1 << 0) | (1 << 2) | (1 << 3)
        assert not _confine_exec.handled_write_access(8) & read_rights

    def test_helper_refuses_an_abi_that_cannot_deny_truncation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(_confine_exec, "landlock_abi", lambda: 2)
        monkeypatch.setenv(_confine_exec.COMMAND_ENV_VARIABLE, "{}")

        status = os.stat(tmp_path)
        root = ["--root", str(tmp_path), str(status.st_dev), str(status.st_ino)]
        status = _confine_exec.main(["--landlock", *root, "--", "true"])

        assert status == _confine_exec.EXIT_SANDBOX_FAILED
        assert _confine_exec.MIN_LANDLOCK_ABI == 3

    def test_helper_refuses_without_a_command(self) -> None:
        assert _confine_exec.main(["--root", "/tmp", "1", "2"]) == _confine_exec.EXIT_SANDBOX_FAILED

    def test_helper_refuses_a_root_that_is_not_the_confined_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_confine_exec.COMMAND_ENV_VARIABLE, "{}")
        status = os.stat(tmp_path)
        wrong_inode = ["--root", str(tmp_path), str(status.st_dev), str(status.st_ino + 1)]

        assert _confine_exec.main([*wrong_inode, "--", "true"]) == _confine_exec.EXIT_SANDBOX_FAILED
