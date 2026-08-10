"""Provision the per-user Windows startup entry for Codex Desktop's HTTP MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from ouroboros.package_profiles import UVX_PYTHON_FLOOR

HTTP_ENDPOINT = "http://127.0.0.1:8765/mcp"
MANAGED_CODEX_MCP_COMMENT_LINES = (
    "# Ouroboros MCP hookup for Codex CLI.",
    "# Keep Ouroboros runtime settings and per-role model overrides in",
    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
    "# This file is only for the Codex MCP/env registration block.",
)
_STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_VALUE_NAME = "Ouroboros MCP HTTP"
_FIXED_OPTIONS = (
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
)
_STARTUP_COMMAND_MAX_LENGTH = 260
_PROBE_ATTEMPTS = 10
_PROBE_TIMEOUT_SECONDS = 2
_PROBE_RETRY_SECONDS = 0.5
_ENDPOINT_UNREACHABLE = "unreachable"
_ENDPOINT_OCCUPIED_INVALID = "occupied-invalid"
_ENDPOINT_READY = "ready-ouroboros"
_MCP_PROTOCOL_VERSION = "2025-03-26"
_STARTUP_MUTEX_NAME = r"Local\OuroborosCodexMcpHttpStartup"
_STARTUP_MUTEX_WAIT_MILLISECONDS = 5_000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


@dataclass
class WindowsHttpProvision:
    command: str
    created_value: bool
    process: subprocess.Popen[bytes] | None
    lease: _StartupMutexLease | None = field(default=None, compare=False, repr=False)


def provision_windows_codex_mcp_http(
    launcher: tuple[str, list[str]],
) -> tuple[WindowsHttpProvision | None, str | None]:
    """Create or reuse the fixed per-user startup entry and verify its endpoint."""
    if not _is_valid_launcher(launcher):
        return None, (
            "Codex HTTP MCP launcher must contain a non-empty command and string arguments."
        )
    if not _is_windows():
        return None, "Codex HTTP MCP startup setup is only supported on Windows."

    try:
        command = _startup_command(launcher)
    except ValueError as exc:
        return None, str(exc)
    if len(command) > _STARTUP_COMMAND_MAX_LENGTH:
        return None, (
            f"Windows startup command is {len(command)} characters, exceeding the "
            f"{_STARTUP_COMMAND_MAX_LENGTH}-character HKCU Run limit. Shorten the Ouroboros "
            "launcher path or arguments, then run setup again."
        )

    lease, mutex_error = _acquire_startup_mutex()
    if mutex_error is not None:
        return None, mutex_error
    assert lease is not None
    provision: WindowsHttpProvision | None = None
    try:
        existing, error = _read_startup_value()
        if error is not None:
            return None, error
        if existing is not None:
            if existing != command:
                return None, _mismatch_error()
            endpoint_state = _probe_endpoint()
            if endpoint_state == _ENDPOINT_READY:
                provision = WindowsHttpProvision(command, False, None, lease)
                return provision, None
            if endpoint_state == _ENDPOINT_OCCUPIED_INVALID:
                return None, _endpoint_occupied_error()
            provision, error = _launch_and_verify(command, False)
        else:
            endpoint_state = _probe_endpoint()
            if endpoint_state == _ENDPOINT_READY:
                return None, _endpoint_conflict_error()
            if endpoint_state == _ENDPOINT_OCCUPIED_INVALID:
                return None, _endpoint_occupied_error()
            created_value, error = _write_startup_value(command)
            if error is not None:
                return None, error
            provision, error = _launch_and_verify(command, created_value)
        if error is not None:
            return None, error
        assert provision is not None
        provision.lease = lease
        return provision, None
    finally:
        if provision is None:
            lease.release()


def commit_windows_codex_mcp_http(provision: WindowsHttpProvision) -> None:
    """Release a successfully transferred in-memory startup ownership record."""
    if provision.lease is not None:
        provision.lease.release()
        provision.lease = None


def commit_windows_codex_mcp_http_batch(provisions: list[WindowsHttpProvision]) -> None:
    """Commit all startup ownership records after the encompassing setup succeeds."""
    for provision in provisions:
        commit_windows_codex_mcp_http(provision)


def retain_windows_codex_mcp_http(
    provision: WindowsHttpProvision, pending: list[WindowsHttpProvision] | None
) -> None:
    """Commit now or retain ownership until the encompassing setup succeeds."""
    if pending is None:
        commit_windows_codex_mcp_http(provision)
    else:
        pending.append(provision)


def rollback_windows_codex_mcp_http_batch(
    provisions: list[WindowsHttpProvision],
) -> list[str]:
    """Undo startup ownership records in reverse order and collect cleanup errors."""
    return [
        error
        for provision in reversed(provisions)
        if (error := rollback_windows_codex_mcp_http(provision)) is not None
    ]


def render_windows_codex_http_mcp_section() -> str:
    """Render the managed Codex URL entry for the Windows HTTP server."""
    return (
        "\n".join(MANAGED_CODEX_MCP_COMMENT_LINES)
        + f"\n\n[mcp_servers.ouroboros]\nurl = {json.dumps(HTTP_ENDPOINT, ensure_ascii=False)}\n"
    )


def has_managed_codex_mcp_comment(
    raw: str,
    line_starts_structure: Callable[[list[str], int], bool],
    is_ouroboros_table_header: Callable[[str], bool],
) -> bool:
    """Return whether the Ouroboros MCP table carries the managed comment."""
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if not line_starts_structure(lines, index) or not is_ouroboros_table_header(line.strip()):
            continue
        comment_end = index
        while comment_end > 0 and not lines[comment_end - 1].strip():
            comment_end -= 1
        comment_start = comment_end - len(MANAGED_CODEX_MCP_COMMENT_LINES)
        if comment_start >= 0 and lines[comment_start:comment_end] == list(
            MANAGED_CODEX_MCP_COMMENT_LINES
        ):
            return True
    return False


def rollback_windows_codex_mcp_http(provision: WindowsHttpProvision) -> str | None:
    """Undo only startup state and process launched by this invocation."""
    errors: list[str] = []
    try:
        if provision.process is not None:
            termination_error = _terminate_process_tree(provision.process)
            if termination_error is not None:
                errors.append(termination_error)
        if provision.created_value:
            _removed, cleanup_error = _delete_startup_value_if_matches(provision.command)
            if cleanup_error is not None:
                errors.append(cleanup_error)
    finally:
        commit_windows_codex_mcp_http(provision)
    return "; ".join(errors) or None


def remove_windows_codex_mcp_startup(
    launcher: tuple[str, list[str]], *, dry_run: bool = False
) -> tuple[bool, str | None]:
    """Remove the exact managed Run value without affecting prior-session processes."""
    if not _is_valid_launcher(launcher):
        return False, (
            "Codex HTTP MCP launcher must contain a non-empty command and string arguments."
        )
    if not _is_windows():
        return False, "Codex HTTP MCP startup setup is only supported on Windows."
    try:
        command = _startup_command(launcher)
    except ValueError as exc:
        return False, str(exc)
    if len(command) > _STARTUP_COMMAND_MAX_LENGTH:
        return False, (
            f"Windows startup command is {len(command)} characters, exceeding the "
            f"{_STARTUP_COMMAND_MAX_LENGTH}-character HKCU Run limit. Shorten the Ouroboros "
            "launcher path or arguments, then run setup again."
        )

    with _startup_mutex() as mutex_error:
        if mutex_error is not None:
            return False, mutex_error
        existing, error = _read_startup_value()
        if error is not None:
            return False, error
        if existing is None:
            return False, None
        if existing != command and not _is_managed_startup_variant(existing):
            return False, None
        if dry_run:
            return True, None
        return _delete_startup_value_if_matches(existing)


def _launch_and_verify(
    command: str, created_value: bool
) -> tuple[WindowsHttpProvision | None, str | None]:
    process, error = _launch(command)
    if error is not None:
        provision = WindowsHttpProvision(command, created_value, process)
        return None, _with_rollback_error(provision, error)
    provision = WindowsHttpProvision(command, created_value, process)
    readiness_error = _wait_for_ouroboros_initialize()
    if readiness_error is not None:
        return None, _with_rollback_error(provision, readiness_error)
    return provision, None


def _with_rollback_error(provision: WindowsHttpProvision, error: str) -> str:
    rollback_error = rollback_windows_codex_mcp_http(provision)
    if rollback_error is not None:
        return f"{error}; {rollback_error}"
    return error


def _is_windows() -> bool:
    return os.name == "nt"


def _is_valid_launcher(launcher: object) -> bool:
    return (
        isinstance(launcher, tuple)
        and len(launcher) == 2
        and isinstance(launcher[0], str)
        and bool(launcher[0])
        and isinstance(launcher[1], list)
        and all(isinstance(argument, str) for argument in launcher[1])
    )


def _startup_command(launcher: tuple[str, list[str]]) -> str:
    return subprocess.list2cmdline([launcher[0], *_managed_arguments(launcher[1])])


def _managed_arguments(arguments: list[str]) -> list[str]:
    managed = list(arguments)
    for option, required_value in zip(_FIXED_OPTIONS[::2], _FIXED_OPTIONS[1::2], strict=True):
        values = _option_values(managed, option)
        if not values:
            managed.extend((option, required_value))
            continue
        if len(values) != 1:
            raise ValueError(
                f"Codex HTTP MCP launcher specifies {option} more than once. Keep exactly "
                f"{option} {required_value}, then run setup again."
            )
        if values[0] != required_value:
            raise ValueError(
                f"Codex HTTP MCP launcher specifies {option} {values[0]!r}, but requires "
                f"{option} {required_value!r}. Correct the launcher, then run setup again."
            )
    return managed


def _option_values(arguments: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == option:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
                raise ValueError(
                    f"Codex HTTP MCP launcher is missing a value for {option}. Use "
                    f"{option} <value>, then run setup again."
                )
            values.append(arguments[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument.removeprefix(f"{option}="))
    return values


def _is_managed_startup_variant(command: str) -> bool:
    arguments = _parse_windows_command_line(command)
    if arguments is None or len(arguments) < 1 + len(_FIXED_OPTIONS):
        return False
    executable = os.path.basename(arguments[0]).lower()
    if (
        executable not in {"uvx", "uvx.exe", "ouroboros", "ouroboros.exe"}
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?\.exe", executable) is None
    ):
        return False
    prefix, suffix = arguments[1 : -len(_FIXED_OPTIONS)], arguments[-len(_FIXED_OPTIONS) :]
    if tuple(suffix) != _FIXED_OPTIONS:
        return False
    if executable.startswith("uvx"):
        return prefix in (
            ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
            ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
            ["ouroboros", "mcp", "serve"],
            [
                "--isolated",
                "--python",
                UVX_PYTHON_FLOOR,
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
            ],
        )
    if executable.startswith("python"):
        return prefix == ["-m", "ouroboros", "mcp", "serve"]
    return prefix == ["mcp", "serve"]


def _parse_windows_command_line(command: str) -> list[str] | None:
    try:
        argc = ctypes.c_int()
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        command_line_to_argv = shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        argv = command_line_to_argv(command, ctypes.byref(argc))
        if not argv:
            return None
        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            local_free(argv)
    except OSError:
        return None


class _StartupMutexLease:
    def __init__(
        self,
        handle: object,
        release_mutex: Callable[[object], object],
        close_handle: Callable[[object], object],
    ) -> None:
        self._handle = handle
        self._release_mutex = release_mutex
        self._close_handle = close_handle
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._release_mutex(self._handle)
            self._close_handle(self._handle)


def _acquire_startup_mutex() -> tuple[_StartupMutexLease | None, str | None]:
    """Acquire the Ouroboros startup mutex scoped to the current Windows login session."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        wait_for_single_object = kernel32.WaitForSingleObject
        release_mutex = kernel32.ReleaseMutex
        close_handle = kernel32.CloseHandle
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        wait_for_single_object.restype = ctypes.c_ulong
        release_mutex.argtypes = (ctypes.c_void_p,)
        release_mutex.restype = ctypes.c_bool
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        handle = create_mutex(None, False, _STARTUP_MUTEX_NAME)
    except OSError as exc:
        return None, f"Could not create Windows startup mutex: {exc}"
    if not handle:
        return (
            None,
            f"Could not create Windows startup mutex (Win32 error {ctypes.get_last_error()}).",
        )
    wait_result = wait_for_single_object(handle, _STARTUP_MUTEX_WAIT_MILLISECONDS)
    if wait_result in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
        return _StartupMutexLease(handle, release_mutex, close_handle), None
    close_handle(handle)
    if wait_result == _WAIT_TIMEOUT:
        return (
            None,
            f"Could not acquire Windows startup mutex: timed out after "
            f"{_STARTUP_MUTEX_WAIT_MILLISECONDS} ms.",
        )
    return (
        None,
        "Could not acquire Windows startup mutex: WaitForSingleObject failed "
        f"(Win32 error {ctypes.get_last_error()}).",
    )


@contextmanager
def _startup_mutex() -> Iterator[str | None]:
    lease, error = _acquire_startup_mutex()
    try:
        yield error
    finally:
        if lease is not None:
            lease.release()


def _winreg() -> object:
    import winreg

    return winreg


def _read_startup_value() -> tuple[str | None, str | None]:
    with _startup_mutex() as mutex_error:
        if mutex_error is not None:
            return None, mutex_error
        registry = _winreg()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER, _STARTUP_KEY, 0, registry.KEY_READ
            ) as key:
                value, value_type = registry.QueryValueEx(key, _STARTUP_VALUE_NAME)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"Could not read Windows startup entry {_STARTUP_VALUE_NAME!r}: {exc}"
        if value_type != registry.REG_SZ or not isinstance(value, str):
            return "", None
        return value, None


def _write_startup_value(value: str) -> tuple[bool, str | None]:
    """Set an absent Run value without overwriting a concurrent owner."""
    with _startup_mutex() as mutex_error:
        if mutex_error is not None:
            return False, mutex_error
        registry = _winreg()
        try:
            with registry.CreateKeyEx(
                registry.HKEY_CURRENT_USER,
                _STARTUP_KEY,
                0,
                registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE,
            ) as key:
                try:
                    existing, value_type = registry.QueryValueEx(key, _STARTUP_VALUE_NAME)
                except FileNotFoundError:
                    registry.SetValueEx(key, _STARTUP_VALUE_NAME, 0, registry.REG_SZ, value)
                    return True, None
                if (
                    value_type == registry.REG_SZ
                    and isinstance(existing, str)
                    and existing == value
                ):
                    return False, None
                return False, _mismatch_error()
        except OSError as exc:
            return False, f"Could not write Windows startup entry {_STARTUP_VALUE_NAME!r}: {exc}"


def _delete_startup_value_if_matches(expected: str) -> tuple[bool, str | None]:
    with _startup_mutex() as mutex_error:
        if mutex_error is not None:
            return False, mutex_error
        registry = _winreg()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                _STARTUP_KEY,
                0,
                registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE,
            ) as key:
                value, value_type = registry.QueryValueEx(key, _STARTUP_VALUE_NAME)
                if value_type != registry.REG_SZ or value != expected:
                    return False, None
                registry.DeleteValue(key, _STARTUP_VALUE_NAME)
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            return False, f"Could not remove Windows startup entry {_STARTUP_VALUE_NAME!r}: {exc}"
        return True, None


def _launch(command: str) -> tuple[subprocess.Popen[bytes] | None, str | None]:
    try:
        process = subprocess.Popen(
            command,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            ),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return None, f"Could not launch {_STARTUP_VALUE_NAME!r}: {exc}"
    return process, None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> str | None:
    try:
        import ctypes

        buffer_size = 32768
        system_directory = ctypes.create_unicode_buffer(buffer_size)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(system_directory, buffer_size)
        if not length or length >= buffer_size:
            raise OSError("GetSystemDirectoryW failed")
        subprocess.run(
            [
                os.path.join(system_directory.value, "taskkill.exe"),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            check=True,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not terminate {_STARTUP_VALUE_NAME!r} process tree: {exc}"
    return None


def _mismatch_error() -> str:
    return (
        f"Existing Windows startup entry {_STARTUP_VALUE_NAME!r} does not match the required "
        "Ouroboros Codex HTTP command. Remove or correct that value manually, then run setup "
        "again."
    )


def _endpoint_conflict_error() -> str:
    return (
        f"{HTTP_ENDPOINT} already responds as an Ouroboros MCP endpoint, but Windows startup "
        f"entry {_STARTUP_VALUE_NAME!r} is absent. Stop or reconfigure the existing server, "
        "then run setup again."
    )


def _endpoint_occupied_error() -> str:
    return (
        f"{HTTP_ENDPOINT} is occupied by a process that did not return a valid Ouroboros MCP "
        "initialize response. Stop or reconfigure that process, then run setup again."
    )


def _wait_for_ouroboros_initialize() -> str | None:
    for attempt in range(_PROBE_ATTEMPTS):
        if _probe_endpoint() == _ENDPOINT_READY:
            return None
        if attempt + 1 < _PROBE_ATTEMPTS:
            time.sleep(_PROBE_RETRY_SECONDS)
    return (
        f"Startup entry {_STARTUP_VALUE_NAME!r} launched, but {HTTP_ENDPOINT} did not return "
        "a valid Ouroboros MCP initialize response."
    )


def _probe_endpoint() -> str:
    if not _endpoint_is_reachable():
        return _ENDPOINT_UNREACHABLE
    return _ENDPOINT_READY if _initialize_reports_ouroboros() else _ENDPOINT_OCCUPIED_INVALID


def _endpoint_is_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _initialize_reports_ouroboros() -> bool:
    request = urllib.request.Request(
        HTTP_ENDPOINT,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ouroboros-setup", "version": "1"},
                },
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, TimeoutError, UnicodeDecodeError):
        return False
    return _response_is_ouroboros(payload)


def _response_is_ouroboros(payload: str) -> bool:
    for candidate in _json_payloads(payload):
        try:
            response = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(response, Mapping):
            continue
        if (
            response.get("jsonrpc") != "2.0"
            or type(response.get("id")) is not int
            or response["id"] != 1
        ):
            continue
        result = response.get("result")
        if not isinstance(result, Mapping):
            continue
        protocol_version = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        server_info = result.get("serverInfo")
        if (
            protocol_version != _MCP_PROTOCOL_VERSION
            or not isinstance(capabilities, Mapping)
            or not isinstance(server_info, Mapping)
        ):
            continue
        if (
            server_info.get("name") == "ouroboros-mcp"
            and isinstance(server_info.get("version"), str)
            and server_info["version"]
        ):
            return True
    return False


def _json_payloads(payload: str) -> list[str]:
    candidates = [payload]
    candidates.extend(
        line.removeprefix("data:").strip()
        for line in payload.splitlines()
        if line.startswith("data:")
    )
    return candidates
