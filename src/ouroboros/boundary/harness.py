"""Ouroboros oracle harness (product code; the constructor writes data only).

Two process roles and one in-process role.

target <nonce> <call_kind> <symbol> <setup>
    Runs in the project interpreter (-I -B) with the checkout copy under test
    as cwd. It first makes the oracle's declared setup calls (``setup``, a
    JSON list of ``{"symbol", "args", "kwargs"}``; each symbol must resolve to
    a callable defined in the checkout, like the target), so a library that
    must be configured before use (``settings.configure(...)``, then
    ``django.setup()``) can be called. A setup call that fails is reported as
    ``import_error`` with a ``setup:`` detail, never as ``missing``. It then
    imports and resolves the symbol, then writes the frame
    '<nonce> {"phase": "resolved", ...}' (``resolve`` is ``ok``, ``missing``,
    ``import_error``, or ``unprovable`` where the platform cannot prove the
    code is a checkout file without following links). Only after that frame does the
    controller send ONE call on stdin: the inputs, never an expectation. The
    observation is written as '<nonce> {"phase": "result", ...}'. An input
    may name an object instead of spelling a JSON value: a JSON object whose
    only key is ``"$symbol"`` (``SYMBOL_REF``) holding a dotted import path is
    replaced by the object that path imports (a class such as
    ``django.db.models.Model``), anywhere inside the arguments or ``init``.
    An input that does not resolve ends the process without a result frame
    (a crash: indeterminate on the base, a failed case on a candidate).
    A returned ``set`` or ``frozenset`` is reported as the list of its items
    sorted by their JSON text, as a tuple is reported as a list. Frames go to
    the process's original stdout; everything the target code prints goes to
    stderr, which the controller discards. The candidate's code runs in this
    same process once it is imported, so it can write frames itself: a frame
    reports what the candidate's code computed, never proof that the bound
    callable ran. The nonce only keeps the target's own prints apart from its
    report (see ``boundary/oracle_run.py``, "What an observation is").

cli <nonce> <script|module> <name> <count> <proven files> <args>
    Runs a CLI oracle's target from the bytes of the files the controller
    proved, never from a pathname opened anew (see the cli role below).

compare(request)
    Called inside the controller process (never a separate process), which
    imported this module before any target ran. It receives the frozen oracle
    data, the binding, and the observations the controller parsed and
    validated from the target frames, and decides every case. A case whose
    observation cannot be judged (malformed, oversized, or out of range)
    fails; it never makes the other cases undecided.

The module imports nothing outside the standard library: a target process
runs its source text with ``-I -B -c`` in the project interpreter, where the
``ouroboros`` package is not importable. ``boundary/oracle.py`` reads this
file's text once (``ORACLE_HARNESS_SOURCE``) and every oracle package
freezes it, so a package carries the exact harness it was admitted with.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
from typing import Any

MAX_REPR = 300
SYMBOL_REF = "$symbol"
"""The key of an input that names an importable object instead of a JSON value."""


# ---------------------------------------------------------------- target role


class _Missing(Exception):
    pass


class _Unprovable(Exception):
    """This platform cannot prove where the target's code comes from."""


def _plain(value: Any, depth: int = 0) -> Any:
    if depth > 50:
        raise TypeError("too deep")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("non-finite float")
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        # No JSON form keeps a set; its items sorted by their JSON text are
        # one deterministic list, whatever the iteration order was.
        items = [_plain(item, depth + 1) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("non-string key")
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    raise TypeError(type(value).__name__)


def symbol_ref(value: Any) -> str | None:
    """The dotted path ``value`` names when it is a symbol reference, else ``None``."""
    if isinstance(value, dict) and len(value) == 1 and SYMBOL_REF in value:
        name = value[SYMBOL_REF]
        return name if isinstance(name, str) else ""
    return None


def symbol_refs(value: Any, depth: int = 0) -> list[str]:
    """Every symbol reference inside a JSON input value, in order."""
    if depth > 50:
        raise ValueError("too deep")
    name = symbol_ref(value)
    if name is not None:
        return [name]
    if isinstance(value, list):
        return [ref for item in value for ref in symbol_refs(item, depth + 1)]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in symbol_refs(item, depth + 1)]
    return []


def named_inputs(value: Any) -> Any:
    """``value`` with every symbol reference replaced by its dotted path (a string).

    What a reference implementation receives: it runs without the project,
    so it gets the name of the object, never the object.
    """
    name = symbol_ref(value)
    if name is not None:
        return name
    if isinstance(value, list):
        return [named_inputs(item) for item in value]
    if isinstance(value, dict):
        return {key: named_inputs(item) for key, item in value.items()}
    return value


def _lookup(symbol: str) -> tuple[Any, Any, int, list[str]]:
    """``(module, object, split, parts)`` for an importable dotted ``symbol``."""
    import importlib

    parts = symbol.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing == module_name or module_name.startswith(missing + "."):
                continue
            raise
        target = module
        for name in parts[split:]:
            if not hasattr(target, name):
                raise _Missing(symbol + ": " + name + " not found")
            target = getattr(target, name)
        return module, target, split, parts
    raise _Missing(symbol + ": module not found")


def _inputs(value: Any) -> Any:
    """``value`` with every symbol reference replaced by the object it imports."""
    name = symbol_ref(value)
    if name is not None:
        return _lookup(name)[1]
    if isinstance(value, list):
        return [_inputs(item) for item in value]
    if isinstance(value, dict):
        return {key: _inputs(item) for key, item in value.items()}
    return value


def _setup(calls: list[dict[str, Any]]) -> None:
    """Make the oracle's declared setup calls, each to a callable of the checkout."""
    for call in calls:
        target = _resolve(call["symbol"], "function")
        target(*_inputs(call.get("args") or []), **_inputs(call.get("kwargs") or {}))


def _resolve(symbol: str, kind: str) -> Any:
    module, target, split, parts = _lookup(symbol)
    if kind == "method":
        if len(parts) - split != 2:
            raise _Missing(symbol + ": not module.Class.method")
        owner = module
        for name in parts[split:-1]:
            owner = getattr(owner, name)
        _require_inside_checkout(symbol, target, owner)
        return (owner, parts[-1])
    if not callable(target):
        raise _Missing(symbol + ": not callable")
    _require_inside_checkout(symbol, target, None)
    return target


def _defining_file(target: Any, owner: Any) -> str | None:
    """The source file the resolved target's code comes from, as this process loaded it."""
    function = target
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    code = getattr(getattr(function, "__func__", function), "__code__", None)
    if code is not None:
        return str(code.co_filename)
    module = sys.modules.get(str(getattr(owner if owner is not None else target, "__module__", "")))
    filename = getattr(module, "__file__", None)
    return filename if isinstance(filename, str) else None


def _checkout_file(filename: str | None) -> bool:
    """Whether ``filename`` is a regular file of the checkout, reached without a link.

    The same proof as ``ouroboros.core.filesystem_capability``'s
    ``resolve_checkout_file``, which this process cannot import (it runs
    with ``-I`` in the project interpreter): the working directory is held,
    ``filename`` must lie lexically below it, every directory of the path is
    opened by name from the one before it with ``O_NOFOLLOW``, and the file
    itself must open without following a link as a regular file. Nothing is
    resolved through ``realpath``. Raises ``_Unprovable`` where this
    platform has no ``O_NOFOLLOW`` or ``dir_fd`` support.
    """
    _require_nofollow()
    root = os.getcwd()
    if not isinstance(filename, str) or not filename.startswith(root.rstrip(os.sep) + os.sep):
        return False
    try:
        descriptor = _open_checkout_file(filename[len(root.rstrip(os.sep)) + 1 :].split(os.sep))
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(descriptor).st_mode)
    finally:
        os.close(descriptor)


def _require_nofollow() -> tuple[int, int]:
    """``O_NOFOLLOW`` and ``O_DIRECTORY``; ``_Unprovable`` where no-follow opens are unavailable."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None or os.open not in os.supports_dir_fd:
        raise _Unprovable("no-follow checkout resolution is unavailable")
    return nofollow, directory_flag


def _open_checkout_file(parts: list[str]) -> int:
    """A descriptor of the working directory's file ``parts``, opened without following a link.

    Every directory of the path is opened by name from the one before it
    with ``O_NOFOLLOW``, and so is the file. ``OSError`` for a link, a
    missing entry or a non-canonical part; ``_Unprovable`` where this
    platform has no ``O_NOFOLLOW`` or ``dir_fd`` support.
    """
    nofollow, directory_flag = _require_nofollow()
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("not a path below the working directory")
    held: list[int] = []
    try:
        held.append(os.open(".", os.O_RDONLY | directory_flag))
        for part in parts[:-1]:
            held.append(os.open(part, os.O_RDONLY | directory_flag | nofollow, dir_fd=held[-1]))
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        return os.open(parts[-1], flags, dir_fd=held[-1])
    finally:
        for descriptor in reversed(held):
            os.close(descriptor)


def _require_inside_checkout(symbol: str, target: Any, owner: Any) -> None:
    """The target is resolved only when its code is a file of the checkout under test.

    What the target process imported, not what a static look at the files
    suggests: a workspace ``json.py`` does not stand in for the standard
    library's ``json`` (already imported by this harness), a package linked in
    from outside the checkout is outside it, a name re-exported from another
    library is that library's code, and code whose file does not exist in
    the checkout (compiled from a string, or naming a removed file) is not
    the checkout's. A link anywhere in the path is not followed.
    """
    if not _checkout_file(_defining_file(target, owner)):
        raise _Missing(symbol + ": not defined in the checkout")


def _run(target: Any, kind: str, call: dict[str, Any]) -> dict[str, Any]:
    try:
        if kind == "method":
            owner, name = target
            instance = owner(**(call.get("init") or {}))
            value = getattr(instance, name)(*call["args"], **call["kwargs"])
        else:
            value = target(*call["args"], **call["kwargs"])
    except BaseException as exc:
        return {
            "case_id": call["case_id"],
            "outcome": "raised",
            "exception": [klass.__name__ for klass in type(exc).__mro__],
            "repr": (type(exc).__name__ + ": " + str(exc))[:MAX_REPR],
        }
    entry = {"case_id": call["case_id"], "outcome": "returned", "repr": repr(value)[:MAX_REPR]}
    try:
        entry["value"] = _plain(value)
        entry["encodable"] = True
    except Exception:
        entry["encodable"] = False
    return entry


def _read_all(fd: int) -> str:
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _target(nonce: str, kind: str, symbol: str, setup: str) -> None:
    # Frames use a private copy of the original stdout; fd 1 now points to
    # stderr, so nothing the target code prints can look like a frame.
    frames = os.dup(1)
    os.dup2(2, 1)

    def frame(payload: dict[str, Any]) -> None:
        data = ("\n" + nonce + " " + json.dumps(payload) + "\n").encode("utf-8")
        while data:
            data = data[os.write(frames, data) :]

    cwd = os.getcwd()
    for path in (os.path.join(cwd, "src"), cwd):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        _setup(json.loads(setup))
    except _Unprovable as exc:
        frame({"phase": "resolved", "resolve": "unprovable", "detail": str(exc)[:500]})
        os._exit(0)
    except BaseException as exc:
        # A failed setup is never the target missing: on the base it is
        # indeterminate, on a candidate it fails every case.
        frame(
            {
                "phase": "resolved",
                "resolve": "import_error",
                "detail": ("setup: " + type(exc).__name__ + ": " + str(exc))[:500],
            }
        )
        os._exit(0)
    try:
        target = _resolve(symbol, kind)
    except _Missing as exc:
        frame({"phase": "resolved", "resolve": "missing", "detail": str(exc)[:500]})
        os._exit(0)
    except _Unprovable as exc:
        # Neither missing nor resolved: the check cannot decide on this host.
        frame({"phase": "resolved", "resolve": "unprovable", "detail": str(exc)[:500]})
        os._exit(0)
    except BaseException as exc:
        frame(
            {
                "phase": "resolved",
                "resolve": "import_error",
                "detail": (type(exc).__name__ + ": " + str(exc))[:500],
            }
        )
        os._exit(0)
    frame({"phase": "resolved", "resolve": "ok", "detail": ""})
    call = json.loads(_read_all(0))
    try:
        for key in ("args", "kwargs", "init"):
            call[key] = _inputs(call.get(key))
    except BaseException:
        # An input that names nothing importable: no observation at all.
        os._exit(3)
    frame({"phase": "result", "entry": _run(target, kind, call)})
    os._exit(0)


# ------------------------------------------------------------------- cli role
#
# cli <nonce> <script|module> <name> <count> (<path> <device> <inode> <sha256>){count} <arg>...
#     Runs a CLI oracle's target in the project interpreter (-I -B), with the
#     checkout copy under test as cwd: ``python <name> <arg>...`` for a
#     script, ``python -m <name> <arg>...`` for a module. The controller has
#     proven each file (its checkout-relative path, device, inode and the
#     SHA-256 of its bytes; never an expected value); each is opened here
#     again without following a link, must be that same file holding those
#     same bytes, and the bytes read from that descriptor are what runs,
#     never the pathname opened anew. The outcome is one frame
#     '<nonce> {"phase": "resolved", "resolve": ...}' on stderr (``ok``, or
#     ``unprovable`` when a file is not the proven one), written before any
#     target code runs; stderr is then pointed at the null device, so the
#     target cannot write to that channel. The target's stdout is the
#     observation.


def _proven_files(proofs: list[str]) -> list[tuple[str, bytes]]:
    """Each proven ``(path, device, inode, sha256)`` as ``(absolute path, bytes)``.

    ``OSError`` when a file is not the proven one.
    """
    import hashlib

    root = os.getcwd()
    files = []
    for index in range(0, len(proofs), 4):
        relative, device, inode, digest = proofs[index : index + 4]
        descriptor = _open_checkout_file(relative.split("/"))
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
                int(device),
                int(inode),
            ):
                raise OSError(relative + ": not the proven file")
            chunks = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != digest:
            raise OSError(relative + ": not the proven file")
        files.append((os.path.join(root, *relative.split("/")), data))
    return files


def _stdio_as_configured() -> None:
    """Stdio as ``PYTHONIOENCODING`` / ``PYTHONUTF8`` set it (``-I`` ignores both)."""
    encoding, _sep, errors = os.environ.get("PYTHONIOENCODING", "").partition(":")
    if not encoding and os.environ.get("PYTHONUTF8") == "1":
        encoding = "utf-8"
    if not encoding and not errors:
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding=encoding or None, errors=errors or None)


def _run_main(path: str, source: bytes, main: Any) -> None:
    sys.modules["__main__"] = main
    exec(compile(source, path, "exec", dont_inherit=True), main.__dict__)


def _cli(nonce: str, kind: str, name: str, count: str, rest: list[str]) -> None:
    def frame(payload: dict[str, Any]) -> None:
        data = ("\n" + nonce + " " + json.dumps(payload) + "\n").encode("utf-8")
        while data:
            data = data[os.write(2, data) :]

    number = int(count)
    try:
        files = _proven_files(rest[: 4 * number])
    except (_Unprovable, OSError, ValueError) as exc:
        frame({"phase": "resolved", "resolve": "unprovable", "detail": str(exc)[:500]})
        os._exit(0)
    frame({"phase": "resolved", "resolve": "ok", "detail": ""})
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 2)
    os.close(null)
    _stdio_as_configured()
    arguments = rest[4 * number :]
    bootstrap = sys.modules["_frozen_importlib"]
    loader = sys.modules["_frozen_importlib_external"].SourceFileLoader
    main = type(sys)("__main__")
    main.__dict__["__builtins__"] = sys.modules["builtins"]
    if kind == "script":
        path, source = files[0]
        # As ``python <name>`` sets them: argv as given, ``__file__`` absolute,
        # the script's directory first on the path.
        main.__dict__.update(
            __file__=path, __cached__=None, __loader__=loader("__main__", path), __spec__=None
        )
        sys.argv = [name, *arguments]
        sys.path.insert(0, os.path.dirname(path))
        _run_main(path, source, main)
        return
    # As ``python -m <name>`` runs it: the working directory first on the
    # path, every parent package imported from its proven ``__init__.py``,
    # then the module (or a package's ``__main__``) run as ``__main__``.
    parts = name.split(".")
    packages = parts if len(files) == len(parts) + 1 else parts[:-1]
    sys.path.insert(0, os.getcwd())
    for index, (path, source) in enumerate(files[: len(packages)]):
        dotted = ".".join(packages[: index + 1])
        spec = bootstrap.ModuleSpec(dotted, loader(dotted, path), origin=path, is_package=True)
        spec.submodule_search_locations = [os.path.dirname(path)]
        spec.has_location = True
        module = bootstrap.module_from_spec(spec)
        sys.modules[dotted] = module
        exec(compile(source, path, "exec", dont_inherit=True), module.__dict__)
        if index:
            setattr(sys.modules[".".join(packages[:index])], packages[index], module)
    path, source = files[-1]
    dotted = name + ".__main__" if len(files) == len(parts) + 1 else name
    spec = bootstrap.ModuleSpec(dotted, loader(dotted, path), origin=path)
    spec.has_location = True
    main.__dict__.update(
        __file__=path,
        __cached__=None,
        __loader__=spec.loader,
        __package__=dotted.rpartition(".")[0],
        __spec__=spec,
    )
    sys.argv = [path, *arguments]
    _run_main(path, source, main)


# ---------------------------------------------------------- shared call shape


def split_args(
    params: list[str], arg_map: dict[str, Any], args: dict[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    if not arg_map:
        return [], {name: args[name] for name in params}
    positional = {}
    keywords = {}
    for name in params:
        target = arg_map[name]
        if isinstance(target, int):
            positional[target] = args[name]
        else:
            keywords[target] = args[name]
    return [positional[index] for index in sorted(positional)], keywords


def cli_argv(
    interpreter: str,
    cwd: str,
    symbol: str,
    params: list[str],
    arg_map: dict[str, Any],
    args: dict[str, Any],
) -> list[str]:
    if symbol.startswith("-m "):
        prefix = [interpreter, "-B", "-m", symbol[3:]]
    elif symbol.endswith(".py"):
        prefix = [interpreter, "-B", symbol]
    else:
        prefix = [os.path.join(cwd, symbol)]

    def text(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value)

    positional = {}
    flags = []
    for name in params:
        target = arg_map.get(name, "--" + name) if arg_map else "--" + name
        if isinstance(target, int):
            positional[target] = text(args[name])
        else:
            flags += [target, text(args[name])]
    return prefix + [positional[index] for index in sorted(positional)] + flags


# --------------------------------------------------------------- compare role


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= MAX_REPR else text[:MAX_REPR] + "..."


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def equal(expected: Any, observed: Any, approx: float | None) -> bool:
    if approx is not None and _number(expected):
        return _number(observed) and abs(float(observed) - float(expected)) <= approx
    if isinstance(expected, bool) or isinstance(observed, bool):
        return type(expected) is type(observed) and expected == observed
    if isinstance(expected, int) and _number(observed):
        # An integer expectation is exact.
        return observed == expected
    if _number(expected) and _number(observed):
        # Float arithmetic: 0 + 3 * 0.1 is 0.30000000000000004. A bound of a
        # few units in the last place keeps an exact decimal expectation
        # meaningful; anything wider needs the case's own "approx".
        e, o = float(expected), float(observed)
        if not (math.isfinite(e) and math.isfinite(o)):
            return e == o
        return abs(o - e) <= 16 * math.ulp(max(abs(e), abs(o)))
    if isinstance(expected, list) and isinstance(observed, list):
        return len(expected) == len(observed) and all(
            equal(e, o, approx) for e, o in zip(expected, observed, strict=True)
        )
    if isinstance(expected, dict) and isinstance(observed, dict):
        return set(expected) == set(observed) and all(
            equal(expected[k], observed[k], approx) for k in expected
        )
    return expected == observed


def _render_call(symbol: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    parts = [_short(a) for a in args] + [k + "=" + _short(v) for k, v in kwargs.items()]
    return symbol.rsplit(".", 1)[-1] + "(" + ", ".join(parts) + ")"


ABNORMAL = ("crashed", "timeout", "malformed", "unresolved")
MALFORMED = "malformed or oversized output"


def _abnormal(entry: dict[str, Any], call_text: str, expected_text: str) -> str:
    outcome = entry.get("outcome")
    if outcome == "timeout":
        seen = "timeout"
    elif outcome == "crashed":
        seen = "crash (exit " + str(entry.get("exit")) + ")"
    elif outcome == "unresolved":
        seen = "no target on this call (" + str(entry.get("detail") or "")[:200] + ")"
    else:
        seen = MALFORMED
    return call_text + ": expected " + expected_text + ", observed " + seen


def _judge_python(
    case: dict[str, Any], entry: dict[str, Any] | None, call_text: str
) -> tuple[bool, str]:
    expect = case["expect"]
    if expect["kind"] == "raises":
        expected_text = "to raise " + expect["exception"]
    else:
        expected_text = _short(expect["value"])
    if entry is None:
        return False, call_text + ": no observation"
    if entry["outcome"] in ABNORMAL:
        return False, _abnormal(entry, call_text, expected_text)
    if expect["kind"] == "raises":
        if entry["outcome"] == "raised" and expect["exception"] in entry["exception"]:
            return True, ""
        seen = entry["repr"] if entry["outcome"] == "raised" else "returned " + entry["repr"]
        return False, call_text + ": expected to raise " + expect[
            "exception"
        ] + ", observed " + seen
    if entry["outcome"] != "returned":
        return False, call_text + ": expected " + expected_text + ", raised " + entry["repr"]
    if not entry.get("encodable"):
        return False, (
            call_text
            + ": expected "
            + expected_text
            + ", observed "
            + entry["repr"]
            + " (not a plain value)"
        )
    if equal(expect["value"], entry["value"], expect.get("approx")):
        return True, ""
    return False, call_text + ": expected " + expected_text + ", observed " + _short(entry["value"])


def _judge_cli(case: dict[str, Any], entry: dict[str, Any]) -> tuple[bool, str]:
    call_text = entry.get("call") or case["case_id"]
    expect = case["expect"]
    if entry["outcome"] in ABNORMAL:
        return False, _abnormal(entry, call_text, "a completed command")
    problems = []
    code = entry.get("exit_code")
    if expect.get("exit_code") is not None and code != expect["exit_code"]:
        problems.append("exit " + str(code) + " (expected " + str(expect["exit_code"]) + ")")
    out = entry.get("stdout") or ""
    if expect.get("stdout") is not None and out.rstrip("\n") != expect["stdout"].rstrip("\n"):
        problems.append("stdout " + _short(out) + " (expected " + _short(expect["stdout"]) + ")")
    if expect.get("stdout_contains") is not None and expect["stdout_contains"] not in out:
        problems.append("stdout " + _short(out) + " lacks " + _short(expect["stdout_contains"]))
    return not problems, (call_text + ": " + "; ".join(problems)) if problems else ""


def compare(request: dict[str, Any]) -> dict[str, Any]:
    check_id = request["check_id"]
    spec = next(item for item in request["oracle"]["oracles"] if item["check_id"] == check_id)
    binding = request.get("binding")
    source = "declared" if binding is not None else "default"
    binding = binding or spec["default_binding"]
    resolve = request["resolve"]
    detail = request.get("detail") or ""
    observed = request.get("observations") or {}
    cases = []
    for case in spec["cases"]:
        entry = observed.get(case["case_id"])
        call_text = case["case_id"]
        try:
            if spec["call_kind"] == "cli":
                call_text = (entry or {}).get("call") or case["case_id"]
            else:
                args, kwargs = split_args(
                    spec["params"], binding.get("arg_map") or {}, case["args"]
                )
                call_text = _render_call(binding["symbol"], args, kwargs)
            if resolve in ("missing", "import_error"):
                passed, text = False, call_text + ": " + detail
            elif resolve != "ok":
                passed, text = False, ""
            elif spec["call_kind"] == "cli":
                passed, text = (
                    (False, call_text + ": no observation")
                    if entry is None
                    else _judge_cli(case, entry)
                )
            else:
                passed, text = _judge_python(case, entry, call_text)
        except Exception:
            # Overflow, recursion, or a shape the rules above do not expect:
            # this case fails; the other cases are still decided.
            passed, text = False, call_text + ": observed " + MALFORMED
        cases.append(
            {
                "case_id": case["case_id"],
                "held_out": bool(case.get("held_out")),
                "passed": passed,
                "detail": text,
            }
        )
    return {
        "check_id": check_id,
        "criterion_key": spec["criterion_key"],
        "binding_source": source,
        "symbol": binding["symbol"],
        "call_kind": spec["call_kind"],
        "resolve": resolve,
        "cases": cases,
    }


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "target":
        _target(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else "[]")
    elif role == "cli":
        _cli(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6:])
    else:
        sys.stderr.write(
            "usage: harness target <nonce> <kind> <symbol> <setup> | cli <nonce> ...\n"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
