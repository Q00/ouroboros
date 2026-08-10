"""Tests for the per-user Windows Codex HTTP MCP startup entry."""

from __future__ import annotations

from contextlib import nullcontext
import ctypes
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from ouroboros.cli import codex_http_mcp as mcp

LAUNCHER = ("C:\\Program Files\\Ouroboros\\ouroboros.exe", ["mcp", "serve"])
SETUP_LAUNCHER = (
    "C:\\Program Files\\Ouroboros\\ouroboros.exe",
    ["mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"],
)


def _prepare_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_is_windows", lambda: True)


_ORIGINAL_STARTUP_MUTEX = mcp._startup_mutex
_ORIGINAL_ACQUIRE_STARTUP_MUTEX = mcp._acquire_startup_mutex
_ORIGINAL_PARSE_WINDOWS_COMMAND_LINE = mcp._parse_windows_command_line


class _TestLease:
    def release(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _disable_windows_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_startup_mutex", lambda: nullcontext(None))
    monkeypatch.setattr(mcp, "_acquire_startup_mutex", lambda: (_TestLease(), None))
    monkeypatch.setattr(mcp, "_parse_windows_command_line", lambda _command: None)


def test_exact_existing_value_with_healthy_endpoint_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    command = mcp._startup_command(LAUNCHER)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (command, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_READY)
    monkeypatch.setattr(mcp, "_launch", lambda _command: pytest.fail("must not launch"))

    provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error is None
    assert provision == mcp.WindowsHttpProvision(command, False, None)


def test_exact_existing_value_relaunches_only_when_endpoint_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    events: list[str] = []
    command = mcp._startup_command(LAUNCHER)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (command, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_UNREACHABLE)
    process = SimpleNamespace(pid=101)
    monkeypatch.setattr(mcp, "_launch", lambda value: events.append(value) or (process, None))
    monkeypatch.setattr(mcp, "_wait_for_ouroboros_initialize", lambda: None)

    provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error is None
    assert provision == mcp.WindowsHttpProvision(command, False, process)
    assert events == [command]


def test_mismatched_existing_value_is_refused_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: ("other.exe --other", None))
    monkeypatch.setattr(mcp, "_launch", lambda _command: pytest.fail("must not launch"))

    _provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error is not None
    assert "Remove or correct" in error


def test_healthy_endpoint_without_startup_value_blocks_write_and_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (None, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_READY)
    monkeypatch.setattr(mcp, "_write_startup_value", lambda _value: pytest.fail("must not write"))
    monkeypatch.setattr(mcp, "_launch", lambda _command: pytest.fail("must not launch"))

    _provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error is not None
    assert "already responds as an Ouroboros MCP endpoint" in error


def test_absent_value_is_written_and_launched_detached_without_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    writes: list[str] = []
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (None, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_UNREACHABLE)
    monkeypatch.setattr(
        mcp, "_write_startup_value", lambda value: (writes.append(value) or True, None)
    )
    monkeypatch.setattr(
        mcp.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)) or SimpleNamespace(pid=123),
    )
    monkeypatch.setattr(mcp, "_wait_for_ouroboros_initialize", lambda: None)

    provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    command = mcp._startup_command(LAUNCHER)
    assert error is None
    assert provision == mcp.WindowsHttpProvision(command, True, provision.process)
    assert writes == [command]
    assert launches == [
        (
            (command,),
            {
                "creationflags": 0x00000008 | 0x08000000,
                "close_fds": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


def test_command_line_preserves_setup_runtime_and_backend_without_duplicates() -> None:
    assert mcp._startup_command(SETUP_LAUNCHER) == subprocess.list2cmdline(
        [
            "C:\\Program Files\\Ouroboros\\ouroboros.exe",
            "mcp",
            "serve",
            "--runtime",
            "codex",
            "--llm-backend",
            "codex",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ]
    )


@pytest.mark.parametrize(
    ("launcher", "required_option"),
    [
        (
            (
                "ouroboros.exe",
                ["mcp", "serve", "--runtime", "other", "--llm-backend", "codex"],
            ),
            "--runtime",
        ),
        (
            (
                "ouroboros.exe",
                [
                    "mcp",
                    "serve",
                    "--runtime",
                    "codex",
                    "--runtime",
                    "codex",
                    "--llm-backend",
                    "codex",
                ],
            ),
            "--runtime",
        ),
    ],
)
def test_conflicting_managed_options_fail_without_registry_or_endpoint_mutation(
    monkeypatch: pytest.MonkeyPatch,
    launcher: tuple[str, list[str]],
    required_option: str,
) -> None:
    _prepare_windows(monkeypatch)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: pytest.fail("must not read"))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: pytest.fail("must not probe"))
    monkeypatch.setattr(mcp, "_write_startup_value", lambda _value: pytest.fail("must not write"))
    monkeypatch.setattr(mcp, "_launch", lambda _value: pytest.fail("must not launch"))

    _provision, error = mcp.provision_windows_codex_mcp_http(launcher)

    assert error is not None
    assert required_option in error
    assert "run setup again" in error


def test_hkcu_run_command_limit_accepts_boundary_and_refuses_overlong_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_command = mcp._startup_command(("x", []))
    padding = "a" * (mcp._STARTUP_COMMAND_MAX_LENGTH - len(base_command) - 1)
    boundary_launcher = ("x", [padding])
    boundary_command = mcp._startup_command(boundary_launcher)
    assert len(boundary_command) == mcp._STARTUP_COMMAND_MAX_LENGTH

    _prepare_windows(monkeypatch)
    writes: list[str] = []
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (None, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_UNREACHABLE)
    monkeypatch.setattr(
        mcp, "_write_startup_value", lambda value: (writes.append(value) or True, None)
    )
    monkeypatch.setattr(mcp, "_launch", lambda _value: (SimpleNamespace(pid=123), None))
    monkeypatch.setattr(mcp, "_wait_for_ouroboros_initialize", lambda: None)

    provision, error = mcp.provision_windows_codex_mcp_http(boundary_launcher)

    assert error is None
    assert provision is not None
    assert writes == [boundary_command]

    overlong_launcher = ("x", [f"{padding}a"])
    assert len(mcp._startup_command(overlong_launcher)) == mcp._STARTUP_COMMAND_MAX_LENGTH + 1
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: pytest.fail("must not read"))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: pytest.fail("must not probe"))
    monkeypatch.setattr(mcp, "_write_startup_value", lambda _value: pytest.fail("must not write"))
    monkeypatch.setattr(mcp, "_launch", lambda _value: pytest.fail("must not launch"))

    _provision, error = mcp.provision_windows_codex_mcp_http(overlong_launcher)

    assert error is not None
    assert "260-character HKCU Run limit" in error


def test_failed_initialize_rolls_back_newly_written_matching_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    command = mcp._startup_command(LAUNCHER)
    events: list[str] = []
    process = SimpleNamespace(pid=123)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (None, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_UNREACHABLE)
    monkeypatch.setattr(mcp, "_write_startup_value", lambda _value: (True, None))
    monkeypatch.setattr(mcp, "_launch", lambda _value: (process, None))
    monkeypatch.setattr(mcp, "_wait_for_ouroboros_initialize", lambda: "not ready")
    monkeypatch.setattr(
        mcp, "_terminate_process_tree", lambda _process: events.append("terminate") or None
    )
    monkeypatch.setattr(
        mcp,
        "_delete_startup_value_if_matches",
        lambda value: (events.append(f"delete:{value}") or True, None),
    )

    provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert provision is None
    assert error == "not ready"
    assert events == ["terminate", f"delete:{command}"]


def test_compare_before_delete_rolls_back_only_matching_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = mcp._startup_command(LAUNCHER)
    calls: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def query_value(_key: object, _name: str) -> tuple[str, int]:
        calls.append("query")
        return expected, 1

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=lambda *_args: Key(),
        QueryValueEx=query_value,
        DeleteValue=lambda _key, _name: calls.append("delete"),
    )
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    assert mcp._delete_startup_value_if_matches(expected) == (True, None)
    assert calls == ["query", "delete"]


def test_concurrently_changed_value_is_preserved_during_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = mcp._startup_command(LAUNCHER)
    calls: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def query_value(_key: object, _name: str) -> tuple[str, int]:
        calls.append("query")
        return "foreign command", 1

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=lambda *_args: Key(),
        QueryValueEx=query_value,
        DeleteValue=lambda _key, _name: calls.append("delete"),
    )
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    assert mcp._delete_startup_value_if_matches(expected) == (False, None)
    assert calls == ["query"]


def test_startup_mutex_releases_after_write_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    values: dict[str, str] = {}

    def query_value(_key: object, name: str) -> tuple[str, int]:
        if name not in values:
            raise FileNotFoundError
        return values[name], 1

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(),
        OpenKey=lambda *_args: Key(),
        QueryValueEx=query_value,
        SetValueEx=lambda _key, name, _reserved, _type, value: values.__setitem__(name, value),
        DeleteValue=lambda _key, name: values.pop(name),
    )
    kernel32 = SimpleNamespace(
        CreateMutexW=Function(lambda *_args: events.append("create") or 1),
        WaitForSingleObject=Function(lambda *_args: events.append("wait") or mcp._WAIT_OBJECT_0),
        ReleaseMutex=Function(lambda *_args: events.append("release") or True),
        CloseHandle=Function(lambda *_args: events.append("close") or True),
    )
    monkeypatch.setattr(mcp, "_startup_mutex", _ORIGINAL_STARTUP_MUTEX)
    monkeypatch.setattr(mcp, "_acquire_startup_mutex", _ORIGINAL_ACQUIRE_STARTUP_MUTEX)
    monkeypatch.setattr(mcp.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    assert mcp._write_startup_value("managed") == (True, None)
    assert mcp._delete_startup_value_if_matches("managed") == (True, None)
    assert events == ["create", "wait", "release", "close"] * 2
    assert kernel32.CreateMutexW.argtypes == (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    assert kernel32.CreateMutexW.restype == ctypes.c_void_p
    assert kernel32.WaitForSingleObject.argtypes == (ctypes.c_void_p, ctypes.c_ulong)
    assert kernel32.WaitForSingleObject.restype == ctypes.c_ulong
    assert kernel32.ReleaseMutex.argtypes == (ctypes.c_void_p,)
    assert kernel32.ReleaseMutex.restype == ctypes.c_bool
    assert kernel32.CloseHandle.argtypes == (ctypes.c_void_p,)
    assert kernel32.CloseHandle.restype == ctypes.c_bool


def test_startup_mutex_reports_timeout_and_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        def __init__(self, result: int | bool) -> None:
            self.result = result

        def __call__(self, *_args: object) -> int | bool:
            return self.result

    def mutex_result(wait_result: int) -> str | None:
        kernel32 = SimpleNamespace(
            CreateMutexW=Function(1),
            WaitForSingleObject=Function(wait_result),
            ReleaseMutex=Function(True),
            CloseHandle=Function(True),
        )
        monkeypatch.setattr(mcp.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
        monkeypatch.setattr(mcp, "_acquire_startup_mutex", _ORIGINAL_ACQUIRE_STARTUP_MUTEX)
        monkeypatch.setattr(mcp.ctypes, "get_last_error", lambda: 123, raising=False)
        with _ORIGINAL_STARTUP_MUTEX() as error:
            return error

    assert (
        mutex_result(mcp._WAIT_TIMEOUT)
        == "Could not acquire Windows startup mutex: timed out after 5000 ms."
    )
    assert mutex_result(0xFFFFFFFF) == (
        "Could not acquire Windows startup mutex: WaitForSingleObject failed (Win32 error 123)."
    )


def test_startup_mutex_balances_nested_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Function:
        def __init__(self, name: str, result: int | bool) -> None:
            self.name, self.result = name, result

        def __call__(self, *_args: object) -> int | bool:
            events.append(self.name)
            return self.result

    kernel32 = SimpleNamespace(
        CreateMutexW=Function("create", 1),
        WaitForSingleObject=Function("wait", mcp._WAIT_OBJECT_0),
        ReleaseMutex=Function("release", True),
        CloseHandle=Function("close", True),
    )
    monkeypatch.setattr(mcp, "_startup_mutex", _ORIGINAL_STARTUP_MUTEX)
    monkeypatch.setattr(mcp, "_acquire_startup_mutex", _ORIGINAL_ACQUIRE_STARTUP_MUTEX)
    monkeypatch.setattr(mcp.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)

    with _ORIGINAL_STARTUP_MUTEX() as first_error:
        assert first_error is None
        with _ORIGINAL_STARTUP_MUTEX() as second_error:
            assert second_error is None

    assert events == ["create", "wait", "create", "wait", "release", "close", "release", "close"]


def test_competing_provision_and_remove_wait_for_registry_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.RLock()
    first_at_probe = threading.Event()
    allow_first_to_finish = threading.Event()
    second_started = threading.Event()
    queries: list[str] = []
    values: dict[str, str] = {}

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def query_value(_key: object, name: str) -> tuple[str, int]:
        queries.append(threading.current_thread().name)
        if name not in values:
            raise FileNotFoundError
        return values[name], 1

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(),
        OpenKey=lambda *_args: Key(),
        QueryValueEx=query_value,
        SetValueEx=lambda _key, name, _reserved, _type, value: values.__setitem__(name, value),
        DeleteValue=lambda _key, name: values.pop(name),
    )
    kernel32 = SimpleNamespace(
        CreateMutexW=Function(lambda *_args: 1),
        WaitForSingleObject=Function(lambda *_args: (lock.acquire(), mcp._WAIT_OBJECT_0)[1]),
        ReleaseMutex=Function(lambda *_args: lock.release() or True),
        CloseHandle=Function(lambda *_args: True),
    )

    def probe() -> str:
        first_at_probe.set()
        assert allow_first_to_finish.wait(1)
        return mcp._ENDPOINT_UNREACHABLE

    monkeypatch.setattr(mcp, "_startup_mutex", _ORIGINAL_STARTUP_MUTEX)
    monkeypatch.setattr(mcp, "_acquire_startup_mutex", _ORIGINAL_ACQUIRE_STARTUP_MUTEX)
    monkeypatch.setattr(mcp, "_is_windows", lambda: True)
    monkeypatch.setattr(mcp.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)
    monkeypatch.setattr(mcp, "_probe_endpoint", probe)
    monkeypatch.setattr(mcp, "_launch", lambda _command: (None, None))
    monkeypatch.setattr(mcp, "_wait_for_ouroboros_initialize", lambda: None)

    def provision_and_commit() -> None:
        provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)
        assert error is None and provision is not None
        mcp.commit_windows_codex_mcp_http(provision)

    first = threading.Thread(target=provision_and_commit, name="provision")
    second = threading.Thread(
        target=lambda: (second_started.set(), mcp.remove_windows_codex_mcp_startup(LAUNCHER)),
        name="remove",
    )
    first.start()
    assert first_at_probe.wait(1)
    second.start()
    assert second_started.wait(1)
    assert queries == ["provision"]
    allow_first_to_finish.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert queries[-1] == "remove"


@pytest.mark.parametrize(
    ("arguments", "managed"),
    [
        (
            [
                "uvx.exe",
                "--isolated",
                "--python",
                mcp.UVX_PYTHON_FLOOR,
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                *mcp._FIXED_OPTIONS,
            ],
            True,
        ),
        (["ouroboros.exe", "mcp", "serve", *mcp._FIXED_OPTIONS], True),
        (["python.exe", "-m", "ouroboros", "mcp", "serve", *mcp._FIXED_OPTIONS], True),
        (
            [
                "uvx.exe",
                "--isolated",
                "--python",
                "3.12",
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                *mcp._FIXED_OPTIONS,
            ],
            False,
        ),
        (
            ["uvx.exe", "--from", "other", "ouroboros", "mcp", "serve", *mcp._FIXED_OPTIONS],
            False,
        ),
        (["not-uvx.exe", "mcp", "serve", *mcp._FIXED_OPTIONS], False),
        (
            [
                "ouroboros.exe",
                "mcp",
                "serve",
                "--runtime",
                "other",
                "--llm-backend",
                "codex",
                *mcp._FIXED_OPTIONS[4:],
            ],
            False,
        ),
        (
            [
                "python.exe",
                "-m",
                "ouroboros",
                "mcp",
                "serve",
                "--runtime",
                "codex",
                "--llm-backend",
                "other",
                *mcp._FIXED_OPTIONS[4:],
            ],
            False,
        ),
    ],
)
def test_managed_startup_variant_rejects_lookalikes(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], managed: bool
) -> None:
    monkeypatch.setattr(mcp, "_parse_windows_command_line", lambda _command: arguments)
    assert mcp._is_managed_startup_variant("ignored") is managed


def test_parse_windows_command_line_sets_pointer_signatures_and_frees_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freed: list[object] = []
    arguments = (ctypes.c_wchar_p * 2)("old.exe", "mcp")
    argv = ctypes.cast(arguments, ctypes.POINTER(ctypes.c_wchar_p))

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)

    def command_line_to_argv(_command: str, count: object) -> object:
        ctypes.cast(count, ctypes.POINTER(ctypes.c_int)).contents.value = 2
        return argv

    shell32 = SimpleNamespace(CommandLineToArgvW=Function(command_line_to_argv))
    kernel32 = SimpleNamespace(LocalFree=Function(lambda value: freed.append(value)))
    monkeypatch.setattr(
        mcp.ctypes,
        "WinDLL",
        lambda name, **_kwargs: shell32 if name == "shell32" else kernel32,
        raising=False,
    )
    monkeypatch.setattr(mcp, "_parse_windows_command_line", _ORIGINAL_PARSE_WINDOWS_COMMAND_LINE)

    assert _ORIGINAL_PARSE_WINDOWS_COMMAND_LINE("old.exe mcp") == ["old.exe", "mcp"]
    assert shell32.CommandLineToArgvW.argtypes == (
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    )
    assert shell32.CommandLineToArgvW.restype == ctypes.POINTER(ctypes.c_wchar_p)
    assert kernel32.LocalFree.argtypes == (ctypes.c_void_p,)
    assert kernel32.LocalFree.restype == ctypes.c_void_p
    assert ctypes.cast(freed[0], ctypes.c_void_p).value == ctypes.cast(argv, ctypes.c_void_p).value


def test_windows_only_behavior_does_not_touch_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_is_windows", lambda: False)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: pytest.fail("must not read"))

    _provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error == "Codex HTTP MCP startup setup is only supported on Windows."


def _initialize_response(payload: bytes) -> object:
    class Response:
        def read(self) -> bytes:
            return payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    return Response()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'{"jsonrpc":"2.0","id":2,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"other-mcp","version":"1"}}}',
        b'{"jsonrpc":"1.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}',
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
    ],
)
def test_initialize_rejects_malformed_or_non_ouroboros_response(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(
        mcp.urllib.request, "urlopen", lambda *_args, **_kwargs: _initialize_response(payload)
    )

    assert not mcp._initialize_reports_ouroboros()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}',
        b'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"ouroboros-mcp","version":"1"}}}\n\n',
    ],
)
def test_initialize_accepts_valid_json_and_sse(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(
        mcp.urllib.request, "urlopen", lambda *_args, **_kwargs: _initialize_response(payload)
    )

    assert mcp._initialize_reports_ouroboros()


def test_vacant_endpoint_does_not_issue_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_endpoint_is_reachable", lambda: False)
    monkeypatch.setattr(
        mcp, "_initialize_reports_ouroboros", lambda: pytest.fail("must not initialize")
    )

    assert mcp._probe_endpoint() == mcp._ENDPOINT_UNREACHABLE


def test_foreign_tcp_listener_is_occupied_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_endpoint_is_reachable", lambda: True)
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _initialize_response(
            b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"1","capabilities":{},"serverInfo":{"name":"foreign-mcp","version":"1"}}}'
        ),
    )

    assert mcp._probe_endpoint() == mcp._ENDPOINT_OCCUPIED_INVALID


def test_readiness_requires_a_ready_ouroboros_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "_PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_OCCUPIED_INVALID)

    assert mcp._wait_for_ouroboros_initialize() is not None


@pytest.mark.parametrize("existing", [None, mcp._startup_command(LAUNCHER)])
def test_occupied_endpoint_refuses_registry_write_and_launch(
    monkeypatch: pytest.MonkeyPatch, existing: str | None
) -> None:
    _prepare_windows(monkeypatch)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (existing, None))
    monkeypatch.setattr(mcp, "_probe_endpoint", lambda: mcp._ENDPOINT_OCCUPIED_INVALID)
    monkeypatch.setattr(mcp, "_write_startup_value", lambda _value: pytest.fail("must not write"))
    monkeypatch.setattr(
        mcp.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not launch")
    )

    _provision, error = mcp.provision_windows_codex_mcp_http(LAUNCHER)

    assert error is not None
    assert "occupied" in error


def test_write_startup_value_accepts_exact_concurrent_value_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = mcp._startup_command(LAUNCHER)
    calls: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(),
        QueryValueEx=lambda _key, _name: calls.append("query") or (command, 1),
        SetValueEx=lambda *_args: calls.append("set"),
    )
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    assert mcp._write_startup_value(command) == (False, None)
    assert calls == ["query"]


def test_write_startup_value_refuses_foreign_concurrent_value_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(),
        QueryValueEx=lambda _key, _name: calls.append("query") or ("foreign.exe", 1),
        SetValueEx=lambda *_args: calls.append("set"),
    )
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    created, error = mcp._write_startup_value(mcp._startup_command(LAUNCHER))

    assert not created
    assert error is not None
    assert calls == ["query"]


@pytest.mark.parametrize(
    "concurrent_value",
    [mcp._startup_command(LAUNCHER), "foreign.exe --other"],
)
def test_write_startup_value_refuses_non_string_concurrent_value_without_overwrite(
    monkeypatch: pytest.MonkeyPatch, concurrent_value: str
) -> None:
    calls: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKeyEx=lambda *_args: Key(),
        QueryValueEx=lambda _key, _name: calls.append("query") or (concurrent_value, 4),
        SetValueEx=lambda *_args: calls.append("set"),
    )
    monkeypatch.setattr(mcp, "_winreg", lambda: registry)

    created, error = mcp._write_startup_value(mcp._startup_command(LAUNCHER))

    assert not created
    assert error is not None
    assert calls == ["query"]


def test_rollback_terminates_only_its_recorded_process_without_deleting_retained_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=123)
    provision = mcp.WindowsHttpProvision("command", False, process)
    terminated: list[object] = []
    monkeypatch.setattr(
        mcp, "_terminate_process_tree", lambda target: terminated.append(target) or None
    )
    monkeypatch.setattr(
        mcp,
        "_delete_startup_value_if_matches",
        lambda _value: pytest.fail("must not delete retained startup value"),
    )

    assert mcp.rollback_windows_codex_mcp_http(provision) is None
    assert len(terminated) == 1
    assert terminated[0] is provision.process


def test_terminate_process_tree_uses_system_taskkill_for_recorded_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_directory = "C:\\Windows\\System32"
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Kernel32:
        def GetSystemDirectoryW(self, buffer: object, _size: int) -> int:
            buffer.value = system_directory
            return len(system_directory)

    ctypes = SimpleNamespace(
        create_unicode_buffer=lambda _size: SimpleNamespace(value=""),
        windll=SimpleNamespace(kernel32=Kernel32()),
    )
    monkeypatch.setitem(sys.modules, "ctypes", ctypes)
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(),
    )

    assert mcp._terminate_process_tree(SimpleNamespace(pid=4321)) is None
    assert calls == [
        (
            [
                mcp.os.path.join(system_directory, "taskkill.exe"),
                "/PID",
                "4321",
                "/T",
                "/F",
            ],
            {
                "check": True,
                "close_fds": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


def test_commit_is_a_noop() -> None:
    provision = mcp.WindowsHttpProvision("command", True, SimpleNamespace(pid=123))

    assert mcp.commit_windows_codex_mcp_http(provision) is None
    assert provision == mcp.WindowsHttpProvision("command", True, provision.process)


def test_batch_ownership_helpers_preserve_commit_and_reverse_rollback_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisions = [
        mcp.WindowsHttpProvision("first", False, None),
        mcp.WindowsHttpProvision("second", True, None),
    ]
    committed: list[str] = []
    rolled_back: list[str] = []
    pending: list[mcp.WindowsHttpProvision] = []
    monkeypatch.setattr(
        mcp, "commit_windows_codex_mcp_http", lambda provision: committed.append(provision.command)
    )
    monkeypatch.setattr(
        mcp,
        "rollback_windows_codex_mcp_http",
        lambda provision: rolled_back.append(provision.command) or f"{provision.command} failed",
    )

    assert mcp.retain_windows_codex_mcp_http(provisions[0], None) is None
    assert mcp.retain_windows_codex_mcp_http(provisions[1], pending) is None
    assert pending == [provisions[1]]
    assert mcp.rollback_windows_codex_mcp_http_batch(provisions) == [
        "second failed",
        "first failed",
    ]
    mcp.commit_windows_codex_mcp_http_batch(provisions)

    assert committed == ["first", "first", "second"]
    assert rolled_back == ["second", "first"]


def test_render_windows_codex_http_mcp_section_uses_managed_comment_and_endpoint() -> None:
    assert mcp.render_windows_codex_http_mcp_section() == (
        "\n".join(mcp.MANAGED_CODEX_MCP_COMMENT_LINES)
        + '\n\n[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:8765/mcp"\n'
    )


def test_remove_startup_deletes_only_exact_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_windows(monkeypatch)
    command = mcp._startup_command(LAUNCHER)
    deleted: list[str] = []
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (command, None))
    monkeypatch.setattr(
        mcp, "_delete_startup_value_if_matches", lambda value: (deleted.append(value) or True, None)
    )

    assert mcp.remove_windows_codex_mcp_startup(LAUNCHER) == (True, None)
    assert deleted == [command]


def test_remove_startup_deletes_observed_managed_launcher_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    observed = "old launcher command"
    deleted: list[str] = []
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (observed, None))
    monkeypatch.setattr(mcp, "_is_managed_startup_variant", lambda value: value == observed)
    monkeypatch.setattr(
        mcp, "_delete_startup_value_if_matches", lambda value: (deleted.append(value) or True, None)
    )

    assert mcp.remove_windows_codex_mcp_startup(LAUNCHER) == (True, None)
    assert deleted == [observed]


def test_remove_startup_preserves_mismatched_and_dry_run_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    command = mcp._startup_command(LAUNCHER)
    deleted: list[str] = []
    monkeypatch.setattr(
        mcp, "_delete_startup_value_if_matches", lambda value: (deleted.append(value) or True, None)
    )

    monkeypatch.setattr(mcp, "_read_startup_value", lambda: ("foreign.exe", None))
    assert mcp.remove_windows_codex_mcp_startup(LAUNCHER) == (False, None)

    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (command, None))
    assert mcp.remove_windows_codex_mcp_startup(LAUNCHER, dry_run=True) == (True, None)
    assert deleted == []


def test_remove_startup_does_not_delete_foreign_value_after_exact_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_windows(monkeypatch)
    command = mcp._startup_command(LAUNCHER)
    monkeypatch.setattr(mcp, "_read_startup_value", lambda: (command, None))
    monkeypatch.setattr(mcp, "_delete_startup_value_if_matches", lambda _value: (False, None))

    removed, error = mcp.remove_windows_codex_mcp_startup(LAUNCHER)

    assert not removed
    assert error is None
