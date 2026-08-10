"""Codex MCP setup helpers and immutable Windows task provisioning."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from urllib import error as urlerror
from urllib import request as urlrequest
import uuid
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

from ouroboros.core.file_lock import _windows_directory_lease
from ouroboros.orchestrator.heartbeat import is_process_identity_alive

_LEGACY_TASK_NAME = "Ouroboros MCP HTTP"
_LEGACY_RUNNER_NAME = "ouroboros-mcp-http.ps1"
_ROOT_NAME = "codex-desktop-mcp-v1"
_GENERATIONS_NAME = "generations"
_INSTALLATION_ID = "ouroboros-codex-desktop-mcp-v1"
_SCHEMA_VERSION = 1
_PREPARE_EXPIRY_SECONDS = 60
_READINESS_TIMEOUT_SECONDS = 10.0
_READINESS_POLL_INTERVAL_SECONDS = 0.1
_WATCH_INTERVAL_SECONDS = 0.25


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("dwState", ctypes.c_ulong),
        ("dwLocalAddr", ctypes.c_ulong),
        ("dwLocalPort", ctypes.c_ulong),
        ("dwRemoteAddr", ctypes.c_ulong),
        ("dwRemotePort", ctypes.c_ulong),
        ("dwOwningPid", ctypes.c_ulong),
    ]


def tcp_listener_owned_by(pid: int, port: int) -> bool:
    """Return whether the Windows TCP table attributes the listening port to pid."""
    if sys.platform != "win32":
        return True

    import socket

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_bool,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    get_table.restype = ctypes.c_ulong

    size = ctypes.c_ulong()
    # AF_INET and TCP_TABLE_OWNER_PID_LISTENER.
    result = get_table(None, ctypes.byref(size), False, 2, 3, 0)
    if result not in {0, 122} or size.value < ctypes.sizeof(ctypes.c_ulong):
        return False

    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, 2, 3, 0) != 0:
        return False

    count = ctypes.c_ulong.from_buffer_copy(buffer.raw[:4]).value
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    for index in range(count):
        offset = ctypes.sizeof(ctypes.c_ulong) + index * row_size
        if offset + row_size > len(buffer):
            return False
        row = _MibTcpRowOwnerPid.from_buffer_copy(buffer.raw, offset)
        if (
            row.dwState == 2
            and socket.ntohs(row.dwLocalPort & 0xFFFF) == port
            and row.dwOwningPid == pid
        ):
            return True
    return False


def _process_identity_alive(pid: int, start_marker: int | float) -> bool:
    """Verify one PID is still the process that produced its receipt marker."""
    if sys.platform != "win32" or not isinstance(start_marker, int):
        return is_process_identity_alive(pid, start_marker)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        creation = ctypes.c_uint64()
        exit_time = ctypes.c_uint64()
        kernel_time = ctypes.c_uint64()
        user_time = ctypes.c_uint64()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return False
        return creation.value == start_marker and exit_time.value == 0
    finally:
        kernel32.CloseHandle(handle)


_operation_lock = threading.RLock()
_CODEX_MCP_HTTP_SECTION_TEMPLATE = '# Ouroboros MCP hookup for Codex CLI.\n# Keep Ouroboros runtime settings and per-role model overrides in\n# ~/.ouroboros/config.yaml (for example: clarification.default_model,\n# llm.qa_model, evaluation.semantic_model, consensus.*).\n# This file is only for the Codex MCP/env registration block.\n\n[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:8765/mcp"\nenabled = true\n'


def render_codex_mcp_http_section() -> str:
    return _CODEX_MCP_HTTP_SECTION_TEMPLATE


def has_active_plugin_scoped_codex_mcp(data: dict[str, object]) -> bool:
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False
    plugin = plugins.get("ouroboros@ouroboros")
    if plugin is None:
        return False
    if not isinstance(plugin, dict):
        return True
    if plugin.get("enabled") is False:
        return False
    servers = plugin.get("mcp_servers")
    if not isinstance(servers, dict):
        return True
    server = servers.get("ouroboros")
    return not isinstance(server, dict) or server.get("enabled") is not False


def plugin_scoped_codex_mcp_error() -> str:
    return 'Active plugin-scoped Ouroboros MCP configuration prevents adding a global Ouroboros MCP server. Disable plugins."ouroboros@ouroboros" or its mcp_servers.ouroboros entry before rerunning setup.'


def _windows_command_line_argument(value: str) -> str:
    escaped = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
        elif character == '"':
            escaped.extend(("\\" * (backslashes * 2 + 1), character))
            backslashes = 0
        else:
            escaped.extend(("\\" * backslashes, character))
            backslashes = 0
    escaped.extend(("\\" * (backslashes * 2), '"'))
    return "".join(escaped)


def _current_windows_identity() -> str | None:
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    identity = result.stdout.strip().upper() if result.returncode == 0 else ""
    return identity if identity.startswith("S-1-") else None


def _normalized_sid(value: str | None) -> str | None:
    value = value.strip().upper() if isinstance(value, str) else ""
    return value if value.startswith("S-1-") else None


def _account_sid(value: str) -> str | None:
    """Resolve a Scheduler-exported account name to its canonical SID."""
    normalized = _normalized_sid(value)
    if normalized is not None:
        return normalized
    if sys.platform != "win32" or not value:
        return None

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    lookup = advapi32.LookupAccountNameW
    lookup.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    lookup.restype = ctypes.c_bool
    sid_size = ctypes.c_ulong()
    domain_size = ctypes.c_ulong()
    use = ctypes.c_ulong()
    lookup(
        None,
        value,
        None,
        ctypes.byref(sid_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(use),
    )
    if ctypes.get_last_error() != 122 or sid_size.value == 0:
        return None

    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value + 1)
    if not lookup(
        None,
        value,
        sid,
        ctypes.byref(sid_size),
        domain,
        ctypes.byref(domain_size),
        ctypes.byref(use),
    ):
        return None
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_bool
    converted = ctypes.c_wchar_p()
    if not convert(sid, ctypes.byref(converted)):
        return None
    try:
        return _normalized_sid(converted.value)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree(converted)


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_directory(path: Path) -> None:
    if _is_reparse_point(path):
        raise OSError(f"Refusing reparse-point Codex Desktop MCP path: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise OSError(f"Codex Desktop MCP directory disappeared: {path}") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"Codex Desktop MCP path is not a directory: {path}")


def _require_regular(path: Path) -> None:
    if _is_reparse_point(path):
        raise OSError(f"Refusing reparse-point Codex Desktop MCP path: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise OSError(f"Codex Desktop MCP file disappeared: {path}") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"Codex Desktop MCP path is not a regular file: {path}")


def _check_ancestors(path: Path) -> None:
    for ancestor in reversed((path, *path.parents)):
        _require_directory(ancestor)


def _physical_config_dir(config_dir: Path) -> Path:
    requested = config_dir.absolute()
    missing: list[Path] = []
    cursor = requested
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            raise OSError("Could not locate a physical Codex Desktop MCP parent.")
        cursor = cursor.parent
    _check_ancestors(cursor)
    for directory in reversed(missing):
        _require_directory(directory.parent)
        directory.mkdir(exist_ok=True)
        _require_directory(directory)
    physical = requested.resolve(strict=True)
    _check_ancestors(physical)
    return physical


def _safe_descendant(root: Path, path: Path, *, directory: bool = False) -> None:
    _check_ancestors(root)
    _require_directory(root)
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise OSError("Codex Desktop MCP path escapes lifecycle root.") from None
    cursor = root
    for part in parts:
        cursor /= part
        if cursor.exists():
            (_require_directory if cursor != path or directory else _require_regular)(cursor)


def _read_regular(path: Path) -> bytes:
    _require_regular(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Codex Desktop MCP file changed type: {path}")
        return b"".join(iter(lambda: os.read(fd, 65536), b""))
    finally:
        os.close(fd)


def _write_new(path: Path, contents: bytes) -> None:
    if _is_reparse_point(path):
        raise OSError(f"Refusing reparse-point Codex Desktop MCP path: {path}")
    try:
        with path.open("xb") as file:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        raise OSError(f"Create-once lifecycle artifact already exists: {path}") from None


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


@contextmanager
def _windows_operation_lock() -> Iterator[None]:
    if sys.platform != "win32":
        with _operation_lock:
            yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "Global\\OuroborosCodexDesktopMcpV1")
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not claim Codex Desktop MCP setup mutex.")
    try:
        if kernel32.WaitForSingleObject(handle, 30000) not in {0, 128}:
            raise OSError("Could not claim Codex Desktop MCP setup mutex before timeout.")
        yield
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def _bootstrap(root: Path) -> None:
    _require_directory(root.parent)
    root.mkdir(exist_ok=True)
    _require_directory(root)
    generations = root / _GENERATIONS_NAME
    generations.mkdir(exist_ok=True)
    _safe_descendant(root, generations, directory=True)


def _generation_name(generations: Path) -> str:
    maximum = 0
    for child in generations.iterdir():
        if _is_reparse_point(child):
            raise OSError("Refusing reparse-point generation.")
        parts = child.name.split("-", 2)
        if (
            len(parts) == 3
            and parts[0] == "gen"
            and (len(parts[1]) == 20)
            and parts[1].isdigit()
            and (len(parts[2]) == 32)
            and all(c in "0123456789abcdef" for c in parts[2])
        ):
            maximum = max(maximum, int(parts[1]))
    sequence = max(time.time_ns(), maximum + 1)
    if sequence >= 10**20:
        raise OSError("Codex Desktop MCP immutable generation sequence is exhausted.")
    return f"gen-{sequence:020d}-{uuid.uuid4().hex}"


def _publish_generation(
    root: Path, mode: str, launcher: tuple[str, list[str]] | None = None
) -> tuple[str, str | None]:
    if mode not in {"serve", "disabled"} or (mode == "serve" and launcher is None):
        raise ValueError("Invalid lifecycle generation.")
    generations = root / _GENERATIONS_NAME
    prior = _desired_generation(root)
    _safe_descendant(root, generations, directory=True)
    for _ in range(8):
        generation = _generation_name(generations)
        staging = root / f".prepare-{uuid.uuid4().hex}"
        final = generations / generation
        token = uuid.uuid4().hex if mode == "serve" else None
        try:
            staging.mkdir()
            _safe_descendant(root, staging, directory=True)
            manifest: dict[str, object] = {
                "schema_version": _SCHEMA_VERSION,
                "installation_id": _INSTALLATION_ID,
                "generation": generation,
                "mode": mode,
                "state": "prepared",
                "prepared_ns": time.time_ns(),
                "expires_ns": time.time_ns() + _PREPARE_EXPIRY_SECONDS * 1000000000,
            }
            if prior is not None and prior[2] and prior[1].get("mode") == "serve":
                manifest["prior_generation"] = prior[0]
            if launcher is not None:
                manifest["command"] = launcher[0]
                manifest["arguments"] = launcher[1]
            if token is not None:
                manifest["prepare_token"] = token
            _write_new(
                staging / "manifest.json",
                (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            )
            os.rename(staging, final)
            _safe_descendant(root, final, directory=True)
            _safe_descendant(root, final / "manifest.json")
            return (generation, token)
        except FileExistsError:
            continue
    raise OSError("Could not allocate immutable Codex Desktop MCP generation.")


def _load_generation(root: Path, generation: str) -> dict[str, object]:
    path = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, path, directory=True)
    manifest = path / "manifest.json"
    _safe_descendant(root, manifest)
    try:
        value = json.loads(_read_regular(manifest))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("Lifecycle manifest is invalid.") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != _SCHEMA_VERSION
        or value.get("installation_id") != _INSTALLATION_ID
        or (value.get("generation") != generation)
        or (value.get("state") != "prepared")
        or (value.get("mode") not in {"serve", "disabled"})
    ):
        raise OSError("Lifecycle manifest ownership mismatch.")
    return value


def _generation_is_committed(root: Path, generation: str) -> bool:
    marker = root / _GENERATIONS_NAME / generation / "commit.json"
    if not marker.exists():
        return False
    _safe_descendant(root, marker)
    return _read_regular(marker) == b"committed\n"


def _commit_generation(root: Path, generation: str) -> None:
    _load_generation(root, generation)
    _write_new(root / _GENERATIONS_NAME / generation / "commit.json", b"committed\n")


def _abort_generation(root: Path, generation: str) -> None:
    _load_generation(root, generation)
    _write_new(root / _GENERATIONS_NAME / generation / "abort.json", b"aborted\n")


def _valid_generations(root: Path) -> list[tuple[str, dict[str, object], bool]]:
    generations = root / _GENERATIONS_NAME
    _safe_descendant(root, generations, directory=True)
    values = []
    for child in generations.iterdir():
        if _is_reparse_point(child):
            raise OSError("Refusing reparse-point generation.")
        if not child.is_dir() or not child.name.startswith("gen-"):
            continue
        manifest = _load_generation(root, child.name)
        if (child / "abort.json").exists():
            _safe_descendant(root, child / "abort.json")
            continue
        values.append((child.name, manifest, _generation_is_committed(root, child.name)))
    return sorted(values, key=lambda value: value[0], reverse=True)


def _desired_generation(root: Path) -> tuple[str, dict[str, object], bool] | None:
    now = time.time_ns()
    for value in _valid_generations(root):
        if value[2]:
            return value
        expires = value[1].get("expires_ns")
        if isinstance(expires, int) and expires > now:
            return value
    return None


def validate_managed_lifecycle(
    root_value: str, generation: str, installation_id: str, token: str
) -> bool:
    try:
        root = Path(root_value)
        _check_ancestors(root)
        _require_directory(root)
        if installation_id != _INSTALLATION_ID:
            return False
        desired = _desired_generation(root)
        if desired is None or desired[0] != generation or desired[1].get("mode") != "serve":
            return False
        return desired[2] or (
            isinstance(desired[1].get("prepare_token"), str)
            and token == desired[1]["prepare_token"]
        )
    except OSError:
        return False


def _receipt(
    root: Path,
    generation: str,
    phase: str,
    pid: int,
    start_marker: int | float | None,
    token: str,
) -> None:
    _load_generation(root, generation)
    directory = root / _GENERATIONS_NAME / generation
    document = {
        "installation_id": _INSTALLATION_ID,
        "generation": generation,
        "phase": phase,
        "pid": pid,
        "start_marker": start_marker,
        "token": token,
    }
    _write_new(
        directory / f"{phase}-{uuid.uuid4().hex}.json",
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def publish_server_receipt(
    root_value: str,
    generation: str,
    phase: str,
    pid: int,
    start_marker: int | float | None,
    token: str,
) -> bool:
    try:
        root = Path(root_value)
        _check_ancestors(root)
        _receipt(root, generation, phase, pid, start_marker, token)
        return True
    except OSError:
        return False


def lifecycle_should_stop(
    root_value: str, generation: str, installation_id: str, token: str
) -> bool:
    """Stop only for committed successors or a standby-backed serve handoff."""
    try:
        root = Path(root_value)
        if installation_id != _INSTALLATION_ID:
            return True
        own = _load_generation(root, generation)
        if own.get("prepare_token") != token and not _generation_is_committed(root, generation):
            return True
        desired = _desired_generation(root)
        if desired is None:
            return True
        if desired[0] == generation:
            return False
        if desired[2]:
            return True
        if desired[1].get("mode") != "serve":
            return False
        standby_token = desired[1].get("prepare_token")
        return isinstance(standby_token, str) and _receipt_matches(
            root, desired[0], "standby", standby_token
        )
    except OSError:
        return True


def managed_prepare_requires_recovery(root_value: str, generation: str) -> bool:
    """True only when this uncommitted serve prepare expired or was aborted."""
    try:
        root = Path(root_value)
        manifest = _load_generation(root, generation)
        if _generation_is_committed(root, generation) or manifest.get("mode") != "serve":
            return False
        directory = root / _GENERATIONS_NAME / generation
        aborted = (directory / "abort.json").exists()
        if aborted:
            _safe_descendant(root, directory / "abort.json")
        expired = (
            isinstance(manifest.get("expires_ns"), int) and manifest["expires_ns"] <= time.time_ns()
        )
        return aborted or expired
    except OSError:
        return False


def _task_name(generation: str) -> str:
    return f"Ouroboros Codex MCP v1 {generation}"


def _task_arguments(arguments: list[str], root: Path, generation: str, token: str) -> str:
    values = [
        *arguments,
        "--codex-lifecycle-root",
        str(root),
        "--codex-lifecycle-generation",
        generation,
        "--codex-lifecycle-installation",
        _INSTALLATION_ID,
        "--codex-lifecycle-token",
        token,
    ]
    return " ".join(_windows_command_line_argument(value) for value in values)


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if child.tag.rsplit("}", 1)[-1] == name]


def _text(element: ElementTree.Element, path: tuple[str, ...]) -> str | None:
    current = element
    for name in path:
        children = _children(current, name)
        if len(children) != 1:
            return None
        current = children[0]
    return current.text


def _is_owned_task(
    task_xml: str,
    identity: str,
    command: str,
    arguments: str,
    task_name: str | None = None,
) -> bool:
    try:
        root = ElementTree.fromstring(task_xml)
    except ElementTree.ParseError:
        return False
    allowed_root = {"RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"}
    if (
        {child.tag.rsplit("}", 1)[-1] for child in root} != allowed_root
        or set(root.attrib) - {"version"}
        or root.attrib.get("version") not in {"1.2", "1.3", "1.4"}
    ):
        return False

    registration = _children(root, "RegistrationInfo")
    allowed_registration = {"Description", "Author", "Date", "URI"}
    if (
        len(registration) != 1
        or registration[0].attrib
        or {child.tag.rsplit("}", 1)[-1] for child in registration[0]} - allowed_registration
        or len(_children(registration[0], "Description")) != 1
        or any(child.attrib for child in registration[0])
    ):
        return False
    if _children(registration[0], "URI"):
        uri = _text(registration[0], ("URI",))
        if task_name is None or uri != f"\\{task_name}":
            return False
    principals, triggers, actions, settings = (
        _children(root, name) for name in ("Principals", "Triggers", "Actions", "Settings")
    )
    if (
        not all(len(x) == 1 for x in (principals, triggers, actions, settings))
        or len(list(triggers[0])) != 1
        or len(list(actions[0])) != 1
    ):
        return False
    principal, trigger, action = (
        _children(principals[0], "Principal"),
        _children(triggers[0], "LogonTrigger"),
        _children(actions[0], "Exec"),
    )
    expected_settings = {
        "MultipleInstancesPolicy",
        "StartWhenAvailable",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "ExecutionTimeLimit",
        "RestartOnFailure",
        "AllowStartOnDemand",
        "Hidden",
        "Enabled",
    }
    normalized_settings = (expected_settings - {"AllowStartOnDemand", "Enabled"}) | {
        "IdleSettings",
        "UseUnifiedSchedulingEngine",
    }
    settings_names = {child.tag.rsplit("}", 1)[-1] for child in settings[0]}
    idle_settings = _children(settings[0], "IdleSettings")
    if (
        frozenset(settings_names)
        not in {frozenset(expected_settings), frozenset(normalized_settings)}
        or len(_children(settings[0], "RestartOnFailure")) != 1
        or len(idle_settings) not in {0, 1}
        or (
            idle_settings
            and {child.tag.rsplit("}", 1)[-1] for child in idle_settings[0]}
            != {"StopOnIdleEnd", "RestartOnIdle"}
        )
        or (
            idle_settings
            and (
                _text(idle_settings[0], ("StopOnIdleEnd",)) != "true"
                or _text(idle_settings[0], ("RestartOnIdle",)) != "false"
            )
        )
        or (
            "UseUnifiedSchedulingEngine" in settings_names
            and _text(settings[0], ("UseUnifiedSchedulingEngine",)) != "true"
        )
    ):
        return False
    if (
        len(principal) != 1
        or len(trigger) != 1
        or len(action) != 1
        or principal[0].get("id") != "Author"
        or actions[0].get("Context") != "Author"
        or set(principals[0].attrib)
        or set(triggers[0].attrib)
        or set(settings[0].attrib)
        or set(actions[0].attrib) != {"Context"}
        or set(principal[0].attrib) != {"id"}
        or trigger[0].attrib
        or action[0].attrib
        or {child.tag.rsplit("}", 1)[-1] for child in principal[0]}
        not in ({"UserId", "LogonType"}, {"UserId", "LogonType", "RunLevel"})
        or {child.tag.rsplit("}", 1)[-1] for child in trigger[0]}
        not in ({"UserId"}, {"UserId", "Enabled"})
        or {child.tag.rsplit("}", 1)[-1] for child in action[0]} != {"Command", "Arguments"}
        or any(child.attrib for child in principal[0])
        or any(child.attrib for child in trigger[0])
        or any(child.attrib for child in action[0])
        or any(child.attrib for child in settings[0])
        or any(child.attrib for child in _children(settings[0], "RestartOnFailure")[0])
        or (idle_settings and any(child.attrib for child in idle_settings[0]))
    ):
        return False
    return (
        _text(root, ("RegistrationInfo", "Description")) == _INSTALLATION_ID
        and _normalized_sid(_text(principal[0], ("UserId",))) == _normalized_sid(identity)
        and _text(principal[0], ("LogonType",)) == "InteractiveToken"
        and _text(principal[0], ("RunLevel",)) in {None, "LeastPrivilege"}
        and _account_sid(_text(trigger[0], ("UserId",)) or "") == _normalized_sid(identity)
        and _text(trigger[0], ("Enabled",)) in {None, "true"}
        and _text(action[0], ("Command",)) == command
        and _text(action[0], ("Arguments",)) == arguments
        and _text(settings[0], ("MultipleInstancesPolicy",)) == "IgnoreNew"
        and _text(settings[0], ("StartWhenAvailable",)) == "true"
        and _text(settings[0], ("DisallowStartIfOnBatteries",)) == "false"
        and _text(settings[0], ("StopIfGoingOnBatteries",)) == "false"
        and _text(settings[0], ("ExecutionTimeLimit",)) == "PT0S"
        and _text(settings[0], ("RestartOnFailure", "Interval")) == "PT1M"
        and _text(settings[0], ("RestartOnFailure", "Count")) == "3"
        and _text(settings[0], ("AllowStartOnDemand",)) in {None, "true"}
        and _text(settings[0], ("Hidden",)) == "true"
        and _text(settings[0], ("Enabled",)) in {None, "true"}
    )


def _create_task_xml(identity: str, command: str, arguments: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>{_INSTALLATION_ID}</Description></RegistrationInfo>
<Triggers><LogonTrigger><UserId>{xml_escape(identity)}</UserId><Enabled>true</Enabled></LogonTrigger></Triggers>
<Principals><Principal id="Author"><UserId>{xml_escape(identity)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure><AllowStartOnDemand>true</AllowStartOnDemand><Hidden>true</Hidden><Enabled>true</Enabled></Settings>
<Actions Context="Author"><Exec><Command>{xml_escape(command)}</Command><Arguments>{xml_escape(arguments)}</Arguments></Exec></Actions>
</Task>"""


def _task_xml(schtasks: str, name: str) -> str | None:
    result = subprocess.run(
        [schtasks, "/Query", "/TN", name, "/XML"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _ensure_task(
    schtasks: str,
    identity: str,
    root: Path,
    generation: str,
    command: str,
    arguments: list[str],
    token: str,
) -> str:
    name = _task_name(generation)
    task_args = _task_arguments(arguments, root, generation, token)
    existing = _task_xml(schtasks, name)
    if existing is not None:
        if not _is_owned_task(existing, identity, command, task_args, name):
            raise OSError("Generation task belongs to another owner.")
        return name
    xml_path = root / f"task-{uuid.uuid4().hex}.xml"
    _write_new(xml_path, _create_task_xml(identity, command, task_args).encode("utf-16"))
    subprocess.run(
        [schtasks, "/Create", "/TN", name, "/XML", str(xml_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    observed = _task_xml(schtasks, name)
    if observed is None or not _is_owned_task(observed, identity, command, task_args, name):
        raise OSError("Could not create or prove ownership of generation task.")
    return name


def _legacy_artifacts_present(schtasks: str, config_dir: Path) -> bool:
    result = subprocess.run(
        [schtasks, "/Query", "/TN", _LEGACY_TASK_NAME, "/XML"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 or (config_dir / _LEGACY_RUNNER_NAME).exists()


def _mcp_initialize_response(response: object, request_id: str) -> bool:
    """Validate a streamable HTTP initialize result in JSON or SSE form."""
    if getattr(response, "status", getattr(response, "getcode", lambda: None)()) != 200:
        return False
    headers = getattr(response, "headers", None)
    content_type = (
        headers.get("Content-Type", "").split(";", 1)[0].lower() if headers is not None else ""
    )
    if content_type not in {"application/json", "text/event-stream"}:
        return False
    try:
        body = response.read().decode("utf-8")
    except UnicodeDecodeError:
        return False

    payloads = [body]
    if content_type == "text/event-stream":
        payloads = [
            "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            for event in body.replace("\r\n", "\n").split("\n\n")
        ]

    for payload in payloads:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        result = (
            message.get("result")
            if isinstance(message, dict) and message.get("id") == request_id
            else None
        )
        if (
            isinstance(result, dict)
            and result.get("protocolVersion") == "2025-03-26"
            and isinstance(result.get("serverInfo"), dict)
            and result["serverInfo"].get("name") == "ouroboros-mcp"
        ):
            return True
    return False


def _wait_for_http_readiness() -> bool:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "ouroboros-readiness",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ouroboros-setup", "version": "1"},
            },
        }
    ).encode()
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = None
        try:
            response = urlrequest.urlopen(
                urlrequest.Request(
                    "http://127.0.0.1:8765/mcp",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    method="POST",
                ),
                timeout=1,
            )
            if _mcp_initialize_response(response, "ouroboros-readiness"):
                return True
        except (OSError, urlerror.URLError, urlerror.HTTPError, UnicodeDecodeError):
            pass
        finally:
            if response is not None:
                response.close()
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    return False


def _receipt_matches(root: Path, generation: str, phase: str, token: str | None = None) -> bool:
    directory = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, directory, directory=True)
    for receipt in directory.glob(f"{phase}-*.json"):
        _safe_descendant(root, receipt)
        try:
            value = json.loads(_read_regular(receipt))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(value, dict)
            or value.get("installation_id") != _INSTALLATION_ID
            or value.get("generation") != generation
            or value.get("phase") != phase
            or not isinstance(value.get("pid"), int)
            or isinstance(value.get("pid"), bool)
            or value["pid"] <= 0
            or (token is not None and value.get("token") != token)
        ):
            continue

        start_marker = value.get("start_marker")
        if not isinstance(start_marker, int | float) or isinstance(start_marker, bool):
            continue

        pid = value["pid"]
        alive = _process_identity_alive(pid, start_marker)
        if phase == "ready" and alive and tcp_listener_owned_by(pid, 8765):
            return True
        if phase == "standby" and alive:
            return True
        if phase == "stopped" and not alive:
            return True
    return False


def _latest_ready_identity(root: Path, generation: str) -> tuple[int, int | float] | None:
    """Return the currently live listener identity from a ready receipt."""
    directory = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, directory, directory=True)
    for receipt in sorted(directory.glob("ready-*.json"), reverse=True):
        _safe_descendant(root, receipt)
        try:
            value = json.loads(_read_regular(receipt))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        pid = value.get("pid")
        marker = value.get("start_marker")
        if (
            value.get("installation_id") == _INSTALLATION_ID
            and value.get("generation") == generation
            and value.get("phase") == "ready"
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and isinstance(marker, int | float)
            and not isinstance(marker, bool)
            and _process_identity_alive(pid, marker)
            and tcp_listener_owned_by(pid, 8765)
        ):
            return pid, marker
    return None


def _latest_standby_identity(root: Path, generation: str) -> tuple[int, int | float] | None:
    """Return the live process identity that acknowledged standby."""
    directory = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, directory, directory=True)
    for receipt in sorted(directory.glob("standby-*.json"), reverse=True):
        _safe_descendant(root, receipt)
        try:
            value = json.loads(_read_regular(receipt))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        pid = value.get("pid")
        marker = value.get("start_marker")
        if (
            value.get("installation_id") == _INSTALLATION_ID
            and value.get("generation") == generation
            and value.get("phase") == "standby"
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and isinstance(marker, int | float)
            and not isinstance(marker, bool)
            and _process_identity_alive(pid, marker)
        ):
            return pid, marker
    return None


def _raw_standby_identity(
    root: Path, generation: str, token: str
) -> tuple[int, int | float] | None:
    """Read an owned standby identity even after that process has exited."""
    directory = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, directory, directory=True)
    for receipt in sorted(directory.glob("standby-*.json"), reverse=True):
        _safe_descendant(root, receipt)
        try:
            value = json.loads(_read_regular(receipt))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        pid = value.get("pid")
        marker = value.get("start_marker")
        if (
            value.get("installation_id") == _INSTALLATION_ID
            and value.get("generation") == generation
            and value.get("phase") == "standby"
            and value.get("token") == token
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and isinstance(marker, int | float)
            and not isinstance(marker, bool)
        ):
            return pid, marker
    return None


def _stopped_identity_matches(
    root: Path, generation: str, identity: tuple[int, int | float]
) -> bool:
    """Require a stopped receipt for the exact previous live process."""
    pid, marker = identity
    directory = root / _GENERATIONS_NAME / generation
    _safe_descendant(root, directory, directory=True)
    for receipt in directory.glob("stopped-*.json"):
        _safe_descendant(root, receipt)
        try:
            value = json.loads(_read_regular(receipt))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("installation_id") == _INSTALLATION_ID
            and value.get("generation") == generation
            and value.get("phase") == "stopped"
            and value.get("pid") == pid
            and value.get("start_marker") == marker
            and not _process_identity_alive(pid, marker)
        ):
            return True
    return False


def _wait_for_stopped_identity(
    root: Path, generation: str, identity: tuple[int, int | float]
) -> bool:
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _stopped_identity_matches(root, generation, identity):
            return True
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_identity_death(
    root: Path, generation: str, identity: tuple[int, int | float]
) -> bool:
    """Wait for a stopped receipt or prove the exact replacement is dead."""
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _stopped_identity_matches(root, generation, identity):
            return True
        if not _process_identity_alive(*identity):
            return True
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    return False


def wait_for_standby_handoff(root_value: str, generation: str) -> bool:
    """Wait until the exact prior live process has stopped for this standby."""
    try:
        root = Path(root_value)
        manifest = _load_generation(root, generation)
        prior_generation = manifest.get("prior_generation")
        if not isinstance(prior_generation, str):
            return True
        prior_identity = _latest_ready_identity(root, prior_generation)
        if prior_identity is None:
            return True

        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _stopped_identity_matches(root, prior_generation, prior_identity):
                return True
            if not validate_managed_lifecycle(
                root_value,
                generation,
                _INSTALLATION_ID,
                str(manifest.get("prepare_token", "")),
            ):
                recover_managed_lifecycle(root_value, generation)
                return False
            time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    except OSError:
        return False
    return False


def _wait_for_receipt(root: Path, generation: str, phase: str, token: str | None = None) -> bool:
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _receipt_matches(root, generation, phase, token):
            return True
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    return False


def _restart_prior(schtasks: str, root: Path, identity: str) -> None:
    desired = _desired_generation(root)
    if desired is None or desired[1].get("mode") != "serve" or (not desired[2]):
        return
    manifest = desired[1]
    command, arguments = (manifest.get("command"), manifest.get("arguments"))
    if not isinstance(command, str) or not isinstance(arguments, list):
        return
    token = manifest.get("prepare_token") if isinstance(manifest.get("prepare_token"), str) else ""
    name = _ensure_task(schtasks, identity, root, desired[0], command, arguments, token)
    subprocess.run([schtasks, "/Run", "/TN", name], capture_output=True, text=True, check=False)


def recover_managed_lifecycle(root_value: str, generation: str) -> bool:
    """Restart the exact committed predecessor after a failed prepare expires."""
    schtasks = shutil.which("schtasks")
    identity = _current_windows_identity()
    if schtasks is None or identity is None:
        return False

    try:
        root = Path(root_value)
        _check_ancestors(root)
        manifest = _load_generation(root, generation)
        directory = root / _GENERATIONS_NAME / generation
        aborted = (directory / "abort.json").exists()
        if aborted:
            _safe_descendant(root, directory / "abort.json")
        expired = (
            isinstance(manifest.get("expires_ns"), int) and manifest["expires_ns"] <= time.time_ns()
        )
        if _generation_is_committed(root, generation) or not (aborted or expired):
            return False

        prior_generation = manifest.get("prior_generation")
        if not isinstance(prior_generation, str):
            return False
        prior = _load_generation(root, prior_generation)
        if not _generation_is_committed(root, prior_generation) or prior.get("mode") != "serve":
            return False
        command = prior.get("command")
        arguments = prior.get("arguments")
        token = prior.get("prepare_token")
        if (
            not isinstance(command, str)
            or not isinstance(arguments, list)
            or not all(isinstance(argument, str) for argument in arguments)
            or not isinstance(token, str)
        ):
            return False
        name = _ensure_task(schtasks, identity, root, prior_generation, command, arguments, token)
        result = subprocess.run(
            [schtasks, "/Run", "/TN", name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def _operate(config_dir: Path, launcher: tuple[str, list[str]] | None, mode: str) -> str | None:
    schtasks, identity = (shutil.which("schtasks"), _current_windows_identity())
    if schtasks is None or identity is None:
        return "Could not find Windows Task Scheduler or current user."
    generation: str | None = None
    root: Path | None = None
    setup_lease: object | None = None
    replacement_started = False
    try:
        with _windows_operation_lock():
            physical = _physical_config_dir(config_dir)
            if _legacy_artifacts_present(schtasks, physical):
                return "Legacy Ouroboros MCP HTTP task or runner detected; manually remove it before setup."
            root = physical / _ROOT_NAME
            _bootstrap(root)
            # This lease is the accidental-concurrency boundary. Same-user hostile
            # mutation is outside the lifecycle threat model; ordinary path/junction
            # replacement is rejected by the pinned lease plus repeated validation.
            setup_lease = _windows_directory_lease(root)
            setup_lease.__enter__()
            _check_ancestors(root)
            _require_directory(root)
            prior = _desired_generation(root)
            prior_identity = (
                _latest_ready_identity(root, prior[0])
                if prior is not None and prior[1].get("mode") == "serve"
                else None
            )
            generation, token = _publish_generation(root, mode, launcher)
            if mode == "disabled":
                _commit_generation(root, generation)
                if prior is not None and prior_identity is not None:
                    try:
                        _wait_for_stopped_identity(root, prior[0], prior_identity)
                    except OSError:
                        pass
                if setup_lease is not None:
                    setup_lease.__exit__(None, None, None)
                    setup_lease = None
                return None

            assert launcher is not None and token is not None
            command, arguments = (launcher[0], list(launcher[1]))
            for option, value in (
                ("--runtime", "codex"),
                ("--llm-backend", "codex"),
                ("--transport", "streamable-http"),
                ("--host", "127.0.0.1"),
                ("--port", "8765"),
            ):
                if option not in arguments:
                    arguments.extend((option, value))
            name = _ensure_task(schtasks, identity, root, generation, command, arguments, token)
            if (
                subprocess.run(
                    [schtasks, "/Run", "/TN", name],
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode
                != 0
            ):
                raise OSError("Could not start MCP generation task.")
            replacement_started = True
            if not _wait_for_receipt(root, generation, "standby", token):
                raise OSError("MCP generation did not enter standby.")
            if prior is not None and prior_identity is not None:
                if not _wait_for_stopped_identity(root, prior[0], prior_identity):
                    raise OSError("Existing MCP server did not stop.")
            if (
                not _wait_for_receipt(root, generation, "ready", token)
                or not _wait_for_http_readiness()
            ):
                raise OSError("MCP generation did not become ready.")
            _commit_generation(root, generation)
    except OSError as exc:
        recovery_error: str | None = None
        if generation is not None and root is not None:
            try:
                _abort_generation(root, generation)
                if replacement_started:
                    standby_identity = _raw_standby_identity(root, generation, token or "")
                    if standby_identity is None:
                        recovery_error = (
                            "Replacement standby receipt is missing or invalid; "
                            "predecessor recovery was not started."
                        )
                    else:
                        replacement_alive = _process_identity_alive(*standby_identity)
                        if replacement_alive and not _wait_for_identity_death(
                            root, generation, standby_identity
                        ):
                            recovery_error = (
                                "Replacement did not stop; predecessor recovery was not started."
                            )
                        if recovery_error is None:
                            _restart_prior(schtasks, root, identity)
                else:
                    _restart_prior(schtasks, root, identity)
            except OSError:
                recovery_error = "Could not complete predecessor recovery."
        if setup_lease is not None:
            setup_lease.__exit__(None, None, None)
        return recovery_error or str(exc)[:240]
    if setup_lease is not None:
        setup_lease.__exit__(None, None, None)
    return None


def provision_windows_codex_mcp_http(
    config_dir: Path, launcher: tuple[str, list[str]]
) -> str | None:
    return _operate(config_dir, launcher, "serve")


def remove_windows_codex_mcp_http(config_dir: Path) -> str | None:
    return _operate(config_dir, None, "disabled")


def finalize_windows_codex_mcp_service(
    config_dir: Path,
    codex_config: Path,
    *,
    is_setup_managed_entry: Callable[[dict[str, object], str], bool],
    resolve_launcher: Callable[[], tuple[str, list[str]] | None],
) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        raw = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return f"Could not inspect final Codex MCP config: {exc}"
    servers = parsed.get("mcp_servers")
    entry = servers.get("ouroboros") if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or not is_setup_managed_entry(entry, raw):
        return None
    if "url" not in entry:
        return remove_windows_codex_mcp_http(config_dir)
    launcher = resolve_launcher()
    return (
        "Could not find the Ouroboros MCP launcher, Windows Task Scheduler, or current user."
        if launcher is None
        else provision_windows_codex_mcp_http(config_dir, launcher)
    )
