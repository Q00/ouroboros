"""Run one command inside a per-run AppContainer, then revoke its write grants.

Run as a standalone script (standard library and ``ctypes`` only), never
imported into the controller for a run:

    python -I -S -B _confine_windows.py --appcontainer NAME --manifest PATH
        [--network] [--read PATH ...] --root DIR DEV INO ... -- ARGV...

It is the Windows backend of ``ouroboros.runtime.exec_sandbox``. Unlike the
POSIX helper (``_confine_exec.py``), which confines itself and execs, an
AppContainer is applied by the parent when it creates the process, so this
launcher stays outside the container, starts the command inside it, waits for
it, and exits with its status. It is started with the fixed bootstrap
environment; the command's environment arrives as JSON in
``OUROBOROS_SANDBOX_COMMAND_ENV`` and takes effect only in the confined
process. In order:

1. Each writable root (``--root DIR DEV INO``) is opened without following a
   reparse point and must still be the directory ``confine`` validated (same
   volume and file id), or nothing runs. The handles stay open, without
   delete sharing, until the launcher exits, so no root can be renamed or
   replaced while the command runs.
2. An AppContainer profile named ``NAME`` (a fresh random name per run) is
   created: ``CreateProcessW`` refuses an AppContainer SID without one
   (``ERROR_FILE_NOT_FOUND``). The profile exists only until the command's
   process has been created: it is deleted before the process is resumed,
   so its folder (which grants the container full control) and its
   registry storage are gone before the command runs anything.
3. Each ``--read`` path the container cannot already read is granted read
   and execute for the Ouroboros read capability (``READ_CAPABILITY``), a
   stable capability SID every run's container holds. That grant is
   persistent: it is recorded in ``--manifest`` before it is applied and
   removed only by ``remove_read_grants``.
4. Each root is granted modify (inherited by everything beneath it) for this
   run's AppContainer SID only, through the verified handle.
5. No regular file beneath a root may have another hard link (it may be
   outside the roots), or nothing runs.
6. The command is created suspended in the AppContainer with no network
   capability (``--network`` adds the client and server capabilities),
   inheriting exactly the launcher's standard handles, then assigned to a
   Job Object that kills every process in it when its last handle closes;
   the profile is deleted, and the command resumed. Process creation
   rewrites ``LOCALAPPDATA`` to ``<LOCALAPPDATA>\\Packages\\NAME\\AC`` and
   ``TEMP``/``TMP`` to its ``Temp`` subdirectory, computed from the
   ``LOCALAPPDATA`` the command's environment names; that must lie inside a
   writable root (``confine`` points it at the temp directory), and the
   launcher creates the two directories there first.
7. When the command exits, the rest of its process tree is terminated and
   the per-run grants are revoked (on every path this launcher controls:
   normal exit, failure of the command, failure of any step above). If the
   launcher itself is terminated, its job handle closes and the kernel
   kills the whole tree; the per-run grants then remain on the roots, whose
   caller deletes them, and name a SID that no process holds any more. A
   launcher terminated between creating the profile and deleting it (before
   the command is resumed) leaves the profile's folder and registry key.

Exit status: the command's, or 125 when the sandbox could not be applied,
126 or 127 when the command could not be started.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from typing import Any

COMMAND_ENV_VARIABLE = "OUROBOROS_SANDBOX_COMMAND_ENV"
READ_CAPABILITY = "ouroborosExecSandboxRead"
"""Name of the capability SID that read grants are made to (persistent)."""

EXIT_SANDBOX_FAILED = 125
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

# Win32 constants (winnt.h, winbase.h, accctrl.h).
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_SYNCHRONIZE = 0x00100000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_ALL = 0x7  # read, write, delete
_EXTENDED_FILE_ID_TYPE = 2
# OpenFileById on a volume that holds no object with that id.
_NO_SUCH_OBJECT = frozenset({2, 3, 87})
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_DACL_PROTECTED = 0x1000
_GRANT_ACCESS = 1
_REVOKE_ACCESS = 4
_NO_INHERITANCE = 0
_SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_UNKNOWN = 0
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x08
_ERROR_SUCCESS = 0

FILE_READ_EXECUTE = 0x001200A9
"""``FILE_GENERIC_READ | FILE_GENERIC_EXECUTE``: what ``--read`` grants."""
FILE_MODIFY = 0x001301BF
"""Read, write, execute and delete, without ``WRITE_DAC``/``WRITE_OWNER``."""

_SE_GROUP_ENABLED = 0x4
# Well-known capability SIDs (S-1-15-3-1, -2, -3).
_NETWORK_CAPABILITIES = ("S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-3")
_ALL_APPLICATION_PACKAGES = "S-1-15-2-1"

_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x1
_STD_HANDLES = (-10, -11, -12)  # input, output, error
_INFINITE = 0xFFFFFFFF
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROFILE_DESCRIPTION = "Ouroboros execution sandbox: one confined command"


class SandboxError(Exception):
    """The AppContainer could not be set up; the command must not run."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SandboxError("the AppContainer backend runs only on Windows")


class _Win32:
    """The DLLs this launcher calls, loaded by absolute path from System32."""

    def __init__(self) -> None:
        _require_windows()
        from ctypes import wintypes

        # kernel32 is a KnownDLL: the loader maps it from System32 whatever
        # the name. Every other DLL is loaded by its absolute System32 path.
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(260)
        if not self.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
            raise SandboxError("GetSystemDirectoryW failed")
        system = buffer.value
        self.advapi32 = ctypes.WinDLL(os.path.join(system, "advapi32.dll"), use_last_error=True)
        self.userenv = ctypes.WinDLL(os.path.join(system, "userenv.dll"), use_last_error=True)
        self.kernelbase = ctypes.WinDLL(os.path.join(system, "kernelbase.dll"), use_last_error=True)
        # restype, then argtypes, for every function called; ``bool`` stands
        # for a BOOL result checked for zero.
        h, d, p, w = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR
        pp, ps = ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)
        sids = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        signatures: dict[Any, dict[str, tuple[Any, ...]]] = {
            self.kernel32: {
                "CreateFileW": (h, w, d, d, p, d, d, h),
                "OpenFileById": (h, h, p, d, d, p, d),
                "CloseHandle": (bool, h),
                "GetFileInformationByHandleEx": (bool, h, ctypes.c_int, p, d),
                "GetStdHandle": (h, d),
                "SetHandleInformation": (bool, h, d, d),
                "CreateJobObjectW": (h, p, w),
                "SetInformationJobObject": (bool, h, ctypes.c_int, p, d),
                "AssignProcessToJobObject": (bool, h, h),
                "TerminateJobObject": (bool, h, wintypes.UINT),
                "TerminateProcess": (bool, h, wintypes.UINT),
                "ResumeThread": (d, h),
                "WaitForSingleObject": (d, h, d),
                "GetExitCodeProcess": (bool, h, ctypes.POINTER(d)),
                "InitializeProcThreadAttributeList": (bool, p, d, d, ps),
                "UpdateProcThreadAttribute": (
                    bool,
                    p,
                    d,
                    ctypes.c_size_t,
                    p,
                    ctypes.c_size_t,
                    p,
                    p,
                ),
                "DeleteProcThreadAttributeList": (None, p),
                "CreateProcessW": (bool, w, wintypes.LPWSTR, p, p, wintypes.BOOL, d, p, w, p, p),
                "LocalFree": (p, p),
            },
            self.advapi32: {
                "GetSecurityInfo": (d, h, ctypes.c_int, d, p, p, pp, p, pp),
                "SetSecurityInfo": (d, h, ctypes.c_int, d, p, p, p, p),
                "GetNamedSecurityInfoW": (d, w, ctypes.c_int, d, p, p, pp, p, pp),
                "SetNamedSecurityInfoW": (d, wintypes.LPWSTR, ctypes.c_int, d, p, p, p, p),
                "SetEntriesInAclW": (d, wintypes.ULONG, p, p, pp),
                "GetAce": (bool, p, d, pp),
                "GetSecurityDescriptorControl": (
                    bool,
                    p,
                    ctypes.POINTER(wintypes.WORD),
                    ctypes.POINTER(d),
                ),
                "EqualSid": (bool, p, p),
                "GetLengthSid": (d, p),
                "FreeSid": (p, p),
                "ConvertSidToStringSidW": (bool, p, ctypes.POINTER(ctypes.c_wchar_p)),
                "ConvertStringSidToSidW": (bool, w, pp),
                "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
                    bool,
                    p,
                    d,
                    d,
                    ctypes.POINTER(ctypes.c_wchar_p),
                    p,
                ),
            },
            self.userenv: {
                "CreateAppContainerProfile": (ctypes.c_long, w, w, w, p, d, pp),
                "DeleteAppContainerProfile": (ctypes.c_long, w),
                "DeriveAppContainerSidFromAppContainerName": (ctypes.c_long, w, pp),
            },
            self.kernelbase: {
                "DeriveCapabilitySidsFromName": (
                    bool,
                    w,
                    sids,
                    ctypes.POINTER(d),
                    sids,
                    ctypes.POINTER(d),
                ),
            },
        }
        for dll, functions in signatures.items():
            for name, (restype, *argtypes) in functions.items():
                function = getattr(dll, name)
                function.restype = wintypes.BOOL if restype is bool else restype
                function.argtypes = argtypes

    @staticmethod
    def error(what: str, code: int | None = None) -> SandboxError:
        code = ctypes.get_last_error() if code is None else code
        return SandboxError(f"{what} failed: {ctypes.FormatError(code).strip()} ({code})")  # type: ignore[attr-defined]


class Sid:
    """A SID owned by this process, copied into a buffer it frees itself."""

    def __init__(self, api: _Win32, pointer: int) -> None:
        length = api.advapi32.GetLengthSid(pointer)
        self.buffer = ctypes.create_string_buffer(ctypes.string_at(pointer, length), length)
        self.api = api

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self.buffer)

    def __str__(self) -> str:
        text = ctypes.c_wchar_p()
        if not self.api.advapi32.ConvertSidToStringSidW(self.pointer, ctypes.byref(text)):
            raise self.api.error("ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            self.api.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def sid_from_string(api: _Win32, text: str) -> Sid:
    pointer = ctypes.c_void_p()
    if not api.advapi32.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
        raise api.error(f"ConvertStringSidToSidW({text})")
    try:
        return Sid(api, pointer.value or 0)
    finally:
        api.kernel32.LocalFree(pointer)


def appcontainer_sid(api: _Win32, name: str) -> Sid:
    """The AppContainer SID for ``name``, derived without creating a profile."""
    pointer = ctypes.c_void_p()
    result = api.userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(pointer))
    if result != 0 or not pointer.value:
        raise api.error(f"DeriveAppContainerSidFromAppContainerName({name!r})", result & 0xFFFF)
    try:
        return Sid(api, pointer.value)
    finally:
        api.advapi32.FreeSid(pointer)


def create_profile(api: _Win32, name: str) -> Sid:
    """Create the AppContainer profile ``name`` and return its SID.

    A profile that already exists is refused: it was not created for this
    run, so nothing about who else holds its SID is known.
    """
    pointer = ctypes.c_void_p()
    result = api.userenv.CreateAppContainerProfile(
        name, name, _PROFILE_DESCRIPTION, None, 0, ctypes.byref(pointer)
    )
    if result != 0 or not pointer.value:
        raise api.error(f"CreateAppContainerProfile({name!r})", result & 0xFFFF)
    try:
        return Sid(api, pointer.value)
    finally:
        api.advapi32.FreeSid(pointer)


def delete_profile(api: _Win32, name: str) -> None:
    """Delete the AppContainer profile ``name`` (its folder and registry storage)."""
    result = api.userenv.DeleteAppContainerProfile(name)
    if result != 0:
        raise api.error(f"DeleteAppContainerProfile({name!r})", result & 0xFFFF)


def capability_sid(api: _Win32, name: str = READ_CAPABILITY) -> Sid:
    """The capability SID (``S-1-15-3-1024-...``) for capability ``name``."""
    from ctypes import wintypes

    groups = ctypes.POINTER(ctypes.c_void_p)()
    capabilities = ctypes.POINTER(ctypes.c_void_p)()
    group_count, capability_count = wintypes.DWORD(), wintypes.DWORD()
    if not api.kernelbase.DeriveCapabilitySidsFromName(
        name,
        ctypes.byref(groups),
        ctypes.byref(group_count),
        ctypes.byref(capabilities),
        ctypes.byref(capability_count),
    ):
        raise api.error(f"DeriveCapabilitySidsFromName({name!r})")
    try:
        if capability_count.value < 1:
            raise SandboxError(f"DeriveCapabilitySidsFromName({name!r}) returned no SID")
        return Sid(api, capabilities[0])
    finally:
        for array, count in ((groups, group_count.value), (capabilities, capability_count.value)):
            for index in range(count):
                api.kernel32.LocalFree(array[index])
            api.kernel32.LocalFree(ctypes.cast(array, ctypes.c_void_p))


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TrusteeW),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _AclHeader(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_ushort),
        ("AceCount", ctypes.c_ushort),
        ("Sbz2", ctypes.c_ushort),
    ]


def _entries(dacl: int) -> list[tuple[int, int, int, int]]:
    """``(type, flags, mask, sid pointer)`` of each ACE in ``dacl``."""
    if not dacl:
        return []
    header = _AclHeader.from_address(dacl)
    result = []
    for index in range(header.AceCount):
        ace = ctypes.c_void_p()
        if not _api().advapi32.GetAce(dacl, index, ctypes.byref(ace)):
            raise _api().error("GetAce")
        head = _AceHeader.from_address(ace.value or 0)
        mask = ctypes.c_uint32.from_address((ace.value or 0) + 4).value
        result.append((head.AceType, head.AceFlags, mask, (ace.value or 0) + 8))
    return result


def _grants(dacl: int, sids: list[Sid], access: int, *, subtree: bool) -> bool:
    """Whether ``dacl`` allows ``access`` on this object to one of ``sids``.

    With ``subtree``, the allowing entry must also be inherited by every
    file and directory beneath the object.
    """
    for kind, flags, mask, sid in _entries(dacl):
        if kind != _ACCESS_ALLOWED_ACE_TYPE or flags & _INHERIT_ONLY_ACE:
            continue
        if subtree and flags & _SUB_CONTAINERS_AND_OBJECTS_INHERIT != (
            _SUB_CONTAINERS_AND_OBJECTS_INHERIT
        ):
            continue
        if mask & access == access and any(_api().advapi32.EqualSid(sid, s.pointer) for s in sids):
            return True
    return False


def _names(dacl: int, sid: Sid) -> bool:
    """Whether any ACE in ``dacl`` names ``sid``."""
    return any(_api().advapi32.EqualSid(entry[3], sid.pointer) for entry in _entries(dacl))


def _new_dacl(dacl: int, sid: Sid, mode: int, access: int, inheritance: int) -> ctypes.c_void_p:
    entry = _ExplicitAccessW(
        grfAccessPermissions=access,
        grfAccessMode=mode,
        grfInheritance=inheritance,
        Trustee=_TrusteeW(
            TrusteeForm=_TRUSTEE_IS_SID, TrusteeType=_TRUSTEE_IS_UNKNOWN, ptstrName=sid.pointer
        ),
    )
    new = ctypes.c_void_p()
    code = _api().advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new))
    if code != _ERROR_SUCCESS:
        raise _api().error("SetEntriesInAclW", code)
    return new


def dacl_sddl(path: str) -> str:
    """The DACL of ``path`` in SDDL (diagnostics and tests)."""
    api = _api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = api.advapi32.GetNamedSecurityInfoW(
        path,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != _ERROR_SUCCESS:
        raise api.error(f"GetNamedSecurityInfoW({path})", code)
    try:
        text = ctypes.c_wchar_p()
        if not api.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None
        ):
            raise api.error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return str(text.value)
        finally:
            api.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        api.kernel32.LocalFree(descriptor)


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class _FileIdDescriptor(ctypes.Structure):
    # FILE_ID_DESCRIPTOR with Type = ExtendedFileIdType and a FILE_ID_128.
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("Type", ctypes.c_int),
        ("FileId", ctypes.c_ubyte * 16),
    ]


_DACL_ACCESS = _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES


def _identity(handle: int, what: str) -> tuple[int, int, int]:
    """``(volume serial, file id, attributes)`` of the object ``handle`` names."""
    api = _api()
    tag, identity = _FileAttributeTagInfo(), _FileIdInfo()
    if not api.kernel32.GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
    ) or not api.kernel32.GetFileInformationByHandleEx(
        handle, _FILE_ID_INFO, ctypes.byref(identity), ctypes.sizeof(identity)
    ):
        raise api.error(f"GetFileInformationByHandleEx({what})")
    file_id = int.from_bytes(bytes(identity.FileId), "little")
    return identity.VolumeSerialNumber, file_id, tag.FileAttributes


def _open(path: str, access: int, share: int) -> int | None:
    """A handle on ``path`` itself (a reparse point is not followed), or None."""
    handle = _api().kernel32.CreateFileW(
        path,
        access,
        share,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    return None if not handle or handle == _INVALID_HANDLE_VALUE else int(handle)


def _open_by_id(path: str, volume: int, file_id: int) -> int | None:
    """Reopen the object with ``file_id`` on the volume ``path`` was on, wherever
    it is now; None when that volume holds no such object any more.

    Raises SandboxError when it cannot be told (the volume is not reachable).
    """
    api = _api()
    anchor, _ = os.path.splitdrive(path)
    hint = _open(anchor + "\\", _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL)
    if hint is None:
        raise api.error(f"the volume of {path} cannot be opened")
    try:
        descriptor = _FileIdDescriptor(
            dwSize=ctypes.sizeof(_FileIdDescriptor),
            Type=_EXTENDED_FILE_ID_TYPE,
            FileId=(ctypes.c_ubyte * 16)(*file_id.to_bytes(16, "little")),
        )
        handle = api.kernel32.OpenFileById(
            hint,
            ctypes.byref(descriptor),
            _DACL_ACCESS,
            _FILE_SHARE_ALL,
            None,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            if code in _NO_SUCH_OBJECT:
                return None
            raise api.error(f"OpenFileById({path})", code)
    finally:
        api.kernel32.CloseHandle(hint)
    if _identity(int(handle), path)[:2] != (volume, file_id):
        api.kernel32.CloseHandle(handle)
        return None
    return int(handle)


def update_dacl(handle: int, sid: Sid, mode: int, access: int, what: str) -> None:
    """Grant ``sid`` (inherited by everything beneath a directory) or revoke every
    entry naming it, on the object ``handle`` names.

    The rest of the DACL is kept as it is: its entries, and whether it is
    protected from inheritance. A NULL DACL (no access control: everyone may
    do anything) cannot take a grant for one principal without replacing
    that meaning, so granting on one is refused; revoking on one is a no-op,
    since nothing names ``sid``.
    """
    api = _api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = api.advapi32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != _ERROR_SUCCESS:
        raise api.error(f"GetSecurityInfo({what})", code)
    try:
        if not dacl.value:
            if mode == _REVOKE_ACCESS:
                return
            raise SandboxError(f"{what} has a NULL DACL (no access control); it is not changed")
        if mode == _REVOKE_ACCESS and not _names(dacl.value, sid):
            return
        from ctypes import wintypes

        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not api.advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise api.error(f"GetSecurityDescriptorControl({what})")
        protection = (
            _PROTECTED_DACL_SECURITY_INFORMATION
            if control.value & _SE_DACL_PROTECTED
            else _UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        directory = _identity(handle, what)[2] & _FILE_ATTRIBUTE_DIRECTORY
        inheritance = _SUB_CONTAINERS_AND_OBJECTS_INHERIT if directory else _NO_INHERITANCE
        new = _new_dacl(dacl.value, sid, mode, access, inheritance)
        try:
            code = api.advapi32.SetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | protection,
                None,
                None,
                new,
                None,
            )
        finally:
            api.kernel32.LocalFree(new)
        if code != _ERROR_SUCCESS:
            raise api.error(f"SetSecurityInfo({what})", code)
    finally:
        api.kernel32.LocalFree(descriptor)


def readable_by_containers(handle: int, capability: Sid, what: str) -> bool:
    """Whether every AppContainer, or the read capability, may already read the
    object ``handle`` names (a NULL DACL lets everyone)."""
    api = _api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = api.advapi32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != _ERROR_SUCCESS:
        return False
    try:
        if not dacl.value:
            return True
        everyone = sid_from_string(api, _ALL_APPLICATION_PACKAGES)
        directory = bool(_identity(handle, what)[2] & _FILE_ATTRIBUTE_DIRECTORY)
        return _grants(dacl.value, [everyone, capability], FILE_READ_EXECUTE, subtree=directory)
    finally:
        api.kernel32.LocalFree(descriptor)


def grant_read(paths: list[str], manifest: str, capability: Sid) -> list[str]:
    """Grant the read capability on each path containers cannot already read.

    Each grant is bound to the object, not its name: the object is opened
    once, its volume and file id are appended to ``manifest`` before its DACL
    changes (so a grant is never left unrecorded), and the DACL is changed
    through that handle. A path this user may not open for ``WRITE_DAC`` is
    left alone (the command may then fail to read it: fail closed). Returns
    the paths granted.
    """
    granted = []
    for path in paths:
        handle = _open(path, _DACL_ACCESS, _FILE_SHARE_ALL)
        if handle is None:
            continue
        try:
            if readable_by_containers(handle, capability, path):
                continue
            volume, file_id, _ = _identity(handle, path)
            os.makedirs(os.path.dirname(manifest), exist_ok=True)
            record = {"path": path, "volume": volume, "file_id": file_id}
            with open(manifest, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({**record, "capability": READ_CAPABILITY}) + "\n")
            try:
                update_dacl(handle, capability, _GRANT_ACCESS, FILE_READ_EXECUTE, path)
            except SandboxError:
                continue
            granted.append(path)
        finally:
            _api().kernel32.CloseHandle(handle)
    return granted


def remove_read_grants(manifest: str) -> list[str]:
    """Remove every entry for the read capability from each object in ``manifest``.

    Each object is reopened by its volume and file id, so one renamed or
    moved within its volume since the grant is still found, and a new object
    at the recorded path is not touched. An object that no longer exists
    took its entries with it. The manifest is deleted once every recorded
    object is clean or gone; if any cannot be reached or changed, it is kept.
    Returns the recorded paths of the objects cleaned.
    """
    if not os.path.exists(manifest):
        return []
    capability = capability_sid(_api())
    records: dict[tuple[int, int], str] = {}
    with open(manifest, encoding="utf-8") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
                key = (int(entry["volume"]), int(entry["file_id"]))
                path = str(entry["path"])
            except (ValueError, KeyError, TypeError):
                continue
            records.setdefault(key, path)
    cleaned, failed = [], []
    for (volume, file_id), path in records.items():
        try:
            handle = _open_by_id(path, volume, file_id)
            if handle is None:
                continue
            try:
                update_dacl(handle, capability, _REVOKE_ACCESS, 0, path)
            finally:
                _api().kernel32.CloseHandle(handle)
        except SandboxError:
            failed.append(path)
            continue
        cleaned.append(path)
    if not failed:
        os.remove(manifest)
    return cleaned


class Root:
    """A writable root opened by handle and verified against ``confine``'s claim."""

    def __init__(self, path: str, device: int, inode: int) -> None:
        api = _api()
        self.path = path
        # No delete sharing: the root cannot be renamed or replaced while held.
        handle = _open(path, _DACL_ACCESS | _SYNCHRONIZE, _FILE_SHARE_READ | _FILE_SHARE_WRITE)
        if handle is None:
            raise api.error(f"writable root {path} cannot be opened")
        self.handle = handle
        try:
            volume, file_id, attributes = _identity(handle, path)
            if not attributes & _FILE_ATTRIBUTE_DIRECTORY or (
                attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise SandboxError(f"writable root {path} is not a plain directory")
            if (volume, file_id) != (device, inode):
                raise SandboxError(f"writable root {path} is not the directory that was confined")
        except BaseException:
            api.kernel32.CloseHandle(handle)
            raise

    def update(self, sid: Sid, mode: int, access: int) -> None:
        """Grant (inherited by everything beneath) or revoke ``sid`` through the handle."""
        update_dacl(self.handle, sid, mode, access, f"writable root {self.path}")

    def close(self) -> None:
        _api().kernel32.CloseHandle(self.handle)


def refuse_root_aliases(roots: list[Root]) -> None:
    """Refuse when a regular file beneath a root has another hard link.

    The other link may be outside the roots, and the grant made on the root
    would let the command change that file. Links (symbolic links,
    junctions) are not descended into; the walk must be complete, so an
    entry that cannot be examined refuses too. The roots are held open
    without delete sharing, so each walk starts at the verified directory.
    """

    def unreadable(error: OSError) -> None:
        raise SandboxError(f"a writable root cannot be fully inspected for aliases: {error}")

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root.path, onerror=unreadable):
            dirnames[:] = [
                name
                for name in dirnames
                if not os.path.isjunction(os.path.join(dirpath, name))
                and not os.path.islink(os.path.join(dirpath, name))
            ]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    status = os.lstat(path)
                except OSError as exc:
                    unreadable(exc)
                if not os.path.islink(path) and status.st_nlink > 1:
                    raise SandboxError(
                        f"{path} in a writable root has {status.st_nlink} hard links"
                    )


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SidAndAttributes)),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    ]


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def kill_on_close_job() -> int:
    """A Job Object that terminates every process in it when its last handle closes."""
    api = _api()
    job = api.kernel32.CreateJobObjectW(None, None)
    if not job:
        raise api.error("CreateJobObjectW")
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = api.error("SetInformationJobObject")
        api.kernel32.CloseHandle(job)
        raise error
    return int(job)


def environment_block(env: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    """``env`` as a Unicode environment block, sorted case-insensitively."""
    folded: dict[str, tuple[str, str]] = {}
    for name, value in env.items():
        if not name or "=" in name[1:] or "\0" in name or "\0" in value:
            raise SandboxError(f"environment variable {name!r} cannot be passed on Windows")
        folded[name.upper()] = (name, value)
    entries = [f"{name}={value}" for _key, (name, value) in sorted(folded.items())]
    text = "\0".join(entries) + "\0\0" if entries else "\0\0"
    return ctypes.create_unicode_buffer(text, len(text))


def resolve_executable(command: str, env: dict[str, str]) -> str | None:
    """The file ``command`` names, or None; a bare name is never searched for.

    ``confine`` resolves a bare name on the absolute ``PATH`` entries only
    (``ouroboros.evaluation.command_dispatch.prepare_command``), so a name
    still bare here was not found there. Searching again would reach the
    current directory, the writable copy, where a file could shadow the
    tool the command names. A name with a directory is taken as given.
    """
    import ntpath

    if not ntpath.dirname(command) and not ntpath.splitdrive(command)[0]:
        return None
    pathext = next((value for name, value in env.items() if name.upper() == "PATHEXT"), None)
    if pathext is not None:
        os.environ["PATHEXT"] = pathext
    found = shutil.which(command)
    return os.path.abspath(found) if found else None


def _inheritable_std_handles() -> list[int | None]:
    api = _api()
    handles: list[int | None] = []
    for which in _STD_HANDLES:
        handle = api.kernel32.GetStdHandle(which & 0xFFFFFFFF)
        if not handle or handle == _INVALID_HANDLE_VALUE:
            handles.append(None)
            continue
        if not api.kernel32.SetHandleInformation(
            handle, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
        ):
            handles.append(None)
            continue
        handles.append(int(handle))
    return handles


def launch(
    executable: str,
    argv: list[str],
    env: dict[str, str],
    container: Sid,
    capabilities: list[Sid],
    job: int,
) -> tuple[int, int]:
    """Create ``argv`` suspended in the AppContainer and put it in ``job``.

    Returns the process and (suspended) thread handles. It inherits exactly
    the launcher's standard handles (``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``).
    """
    api = _api()
    entries = (_SidAndAttributes * max(len(capabilities), 1))()
    for index, capability in enumerate(capabilities):
        entries[index] = _SidAndAttributes(capability.pointer, _SE_GROUP_ENABLED)
    security = _SecurityCapabilities(
        AppContainerSid=container.pointer,
        Capabilities=ctypes.cast(entries, ctypes.POINTER(_SidAndAttributes))
        if capabilities
        else None,
        CapabilityCount=len(capabilities),
    )
    std = _inheritable_std_handles()
    unique = sorted({handle for handle in std if handle is not None})
    handle_list = (ctypes.c_void_p * max(len(unique), 1))(*unique)
    attribute_count = 2 if unique else 1
    size = ctypes.c_size_t()
    api.kernel32.InitializeProcThreadAttributeList(None, attribute_count, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    if not api.kernel32.InitializeProcThreadAttributeList(
        attributes, attribute_count, 0, ctypes.byref(size)
    ):
        raise api.error("InitializeProcThreadAttributeList")
    try:
        if not api.kernel32.UpdateProcThreadAttribute(
            attributes,
            0,
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(security),
            ctypes.sizeof(security),
            None,
            None,
        ):
            raise api.error("UpdateProcThreadAttribute(SECURITY_CAPABILITIES)")
        if unique and not api.kernel32.UpdateProcThreadAttribute(
            attributes,
            0,
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            handle_list,
            ctypes.sizeof(ctypes.c_void_p) * len(unique),
            None,
            None,
        ):
            raise api.error("UpdateProcThreadAttribute(HANDLE_LIST)")
        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = ctypes.addressof(attributes)
        if unique:
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput = std[0], std[1]
            startup.StartupInfo.hStdError = std[2]
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = environment_block(env)
        info = _ProcessInformation()
        if not api.kernel32.CreateProcessW(
            executable,
            command_line,
            None,
            None,
            bool(unique),
            _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT,
            block,
            None,
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            code = ctypes.get_last_error()
            raise OSError(0, ctypes.FormatError(code).strip(), executable, code)  # type: ignore[attr-defined]
    finally:
        api.kernel32.DeleteProcThreadAttributeList(attributes)
    if not api.kernel32.AssignProcessToJobObject(job, info.hProcess):
        error = api.error("AssignProcessToJobObject")
        api.kernel32.TerminateProcess(info.hProcess, EXIT_SANDBOX_FAILED)
        api.kernel32.CloseHandle(info.hThread)
        api.kernel32.CloseHandle(info.hProcess)
        raise error
    return int(info.hProcess), int(info.hThread)


def _variable(env: dict[str, str], name: str) -> str | None:
    return next((value for key, value in env.items() if key.upper() == name), None)


def prepare_profile_directories(env: dict[str, str], name: str, roots: list[Root]) -> None:
    """Create the directories process creation will point ``LOCALAPPDATA``,
    ``TEMP`` and ``TMP`` at, under the command's ``LOCALAPPDATA``, which must
    lie inside a writable root (this launcher is not confined: it must not
    create anything outside the roots)."""
    base = _variable(env, "LOCALAPPDATA")
    if not base or not os.path.isabs(base):
        raise SandboxError("LOCALAPPDATA must name a directory inside a writable root")
    real = os.path.normcase(os.path.realpath(base))
    inside = any(
        os.path.commonpath((real, os.path.normcase(root.path))) == os.path.normcase(root.path)
        for root in roots
        if os.path.splitdrive(real)[0].lower() == os.path.splitdrive(root.path)[0].lower()
    )
    if not inside:
        raise SandboxError(f"LOCALAPPDATA {base} is not inside a writable root")
    os.makedirs(os.path.join(real, "Packages", name, "AC", "Temp"), exist_ok=True)


def resume(thread: int, process: int) -> None:
    api = _api()
    if api.kernel32.ResumeThread(thread) == 0xFFFFFFFF:
        error = api.error("ResumeThread")
        api.kernel32.TerminateProcess(process, EXIT_SANDBOX_FAILED)
        raise error


def wait(process: int) -> int:
    """Wait for ``process`` and return its exit status as a signed 32-bit value."""
    from ctypes import wintypes

    api = _api()
    api.kernel32.WaitForSingleObject(process, _INFINITE)
    status = wintypes.DWORD()
    if not api.kernel32.GetExitCodeProcess(process, ctypes.byref(status)):
        raise api.error("GetExitCodeProcess")
    code = status.value
    return code - (1 << 32) if code >= 1 << 31 else code


_API: _Win32 | None = None


def _api() -> _Win32:
    global _API
    if _API is None:
        _API = _Win32()
    return _API


def _parse(arguments: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {"network": False, "read": [], "roots": []}
    index = 0
    while index < len(arguments) and arguments[index] != "--":
        option = arguments[index]
        if option == "--network":
            parsed["network"] = True
            index += 1
        elif option in ("--appcontainer", "--manifest", "--read") and index + 1 < len(arguments):
            value = arguments[index + 1]
            if option == "--read":
                parsed["read"].append(value)
            else:
                parsed[option[2:]] = value
            index += 2
        elif option == "--root" and index + 3 < len(arguments):
            try:
                device, inode = int(arguments[index + 2]), int(arguments[index + 3])
            except ValueError:
                raise SandboxError("--root DEV and INO must be integers") from None
            parsed["roots"].append((arguments[index + 1], device, inode))
            index += 4
        else:
            raise SandboxError(f"unexpected argument {option!r}")
    if index + 1 >= len(arguments):
        raise SandboxError(
            "usage: --appcontainer NAME --manifest PATH [--network] [--read PATH ...] "
            "--root DIR DEV INO ... -- ARGV..."
        )
    if "appcontainer" not in parsed or "manifest" not in parsed or not parsed["roots"]:
        raise SandboxError("--appcontainer, --manifest and at least one --root are required")
    parsed["argv"] = arguments[index + 1 :]
    return parsed


def _command_environment() -> dict[str, str]:
    raw = os.environ.get(COMMAND_ENV_VARIABLE)
    if raw is None:
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not set")
    try:
        env = json.loads(raw)
    except ValueError as exc:
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not JSON: {exc}") from None
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not a string mapping")
    return env


def main(arguments: list[str]) -> int:
    roots: list[Root] = []
    granted: list[Root] = []
    container: Sid | None = None
    profile: str | None = None
    job: int | None = None
    try:
        try:
            parsed = _parse(arguments)
            env = _command_environment()
            api = _api()
            for path, device, inode in parsed["roots"]:
                roots.append(Root(path, device, inode))
            prepare_profile_directories(env, parsed["appcontainer"], roots)
            container = create_profile(api, parsed["appcontainer"])
            profile = parsed["appcontainer"]
            reader = capability_sid(api)
            grant_read(parsed["read"], parsed["manifest"], reader)
            for root in roots:
                granted.append(root)
                root.update(container, _GRANT_ACCESS, FILE_MODIFY)
            refuse_root_aliases(roots)
            job = kill_on_close_job()
            capabilities = [reader]
            if parsed["network"]:
                capabilities += [sid_from_string(api, text) for text in _NETWORK_CAPABILITIES]
        except (SandboxError, OSError) as exc:
            sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
            return EXIT_SANDBOX_FAILED
        command = parsed["argv"]
        executable = resolve_executable(command[0], env)
        if executable is None:
            sys.stderr.write(f"ouroboros exec sandbox: {command[0]}: not found\n")
            return EXIT_NOT_FOUND
        try:
            process, thread = launch(executable, command, env, container, capabilities, job)
        except SandboxError as exc:
            sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
            return EXIT_SANDBOX_FAILED
        except OSError as exc:
            sys.stderr.write(f"ouroboros exec sandbox: {command[0]}: {exc.strerror}\n")
            return EXIT_NOT_EXECUTABLE
        try:
            # The profile folder grants the container full control: it must be
            # gone before the command runs anything.
            delete_profile(api, profile)
            profile = None
            resume(thread, process)
        except SandboxError as exc:
            api.kernel32.TerminateProcess(process, EXIT_SANDBOX_FAILED)
            api.kernel32.CloseHandle(thread)
            api.kernel32.CloseHandle(process)
            sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
            return EXIT_SANDBOX_FAILED
        try:
            return wait(process)
        finally:
            # The command has exited: end whatever it left running before
            # its grants are revoked.
            api.kernel32.TerminateJobObject(job, 1)
            api.kernel32.CloseHandle(thread)
            api.kernel32.CloseHandle(process)
    finally:
        if profile is not None:
            try:
                delete_profile(_api(), profile)
            except SandboxError as exc:
                sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
        for root in granted:
            try:
                root.update(container, _REVOKE_ACCESS, 0)  # type: ignore[arg-type]
            except SandboxError as exc:
                sys.stderr.write(f"ouroboros exec sandbox: revoking {root.path}: {exc}\n")
        for root in roots:
            root.close()
        if job is not None:
            _api().kernel32.CloseHandle(job)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
