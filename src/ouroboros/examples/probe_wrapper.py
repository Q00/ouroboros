"""Ouroboros probe wrapper (product code; a probe supplies inputs only).

Runs one probe on one tree and reports one outcome record. The controller
(``examples/probe_runner.py``) starts it through the check execution entry
point (``boundary/check_env.check_command``) in the tree's interpreter as
``python -I -B -c <this source> <role> <nonce> <path>``, with a throwaway
copy of the tree as the working directory. ``path`` is a file in the run's
scratch directory, outside the copy.

call <nonce> <path>
    ``path`` holds the probe's Python source. The copy (and its ``src``
    directory) lead ``sys.path``; the source is imported as a module and its
    ``probe()`` is called with no arguments. Status ``not_run`` when the
    source does not import or has no callable ``probe``; ``raised`` when
    ``probe()`` raises (any ``BaseException``, ``SystemExit`` included);
    ``returned`` otherwise, with the return value encoded by ``encode``.
    Everything the probe prints is discarded: fds 1 and 2 point to the null
    device while it runs.

command <nonce> <path>
    ``path`` holds ``{"argv": [...], "stdin": "..."}``. The command runs
    without a shell, in the copy, with ``stdin`` on its standard input.
    Status ``not_run`` when it cannot be started (``OSError``); ``exited``
    otherwise, with its exit status and the first ``STREAM_LIMIT`` bytes of
    each output stream, decoded as UTF-8 with backslash escapes (longer
    output is cut at the limit). The command stays in this process's group,
    so the controller's deadline ends it with this process.

The record is one line, '<nonce> <json>', written to a private copy of the
original stdout. The JSON object holds ``status`` plus the outcome fields of
``examples/record.py``: ``value`` (returned); ``exception_type`` and
``message`` (raised, not_run); ``exit_code``, ``stdout`` and ``stderr``
(exited). ``exception_type`` is always the class's module plus its qualified
name (``builtins.ValueError``). The probe runs in this same process, so it
can write a record itself: a record reports what the tree's code did for the
probe's inputs, never proof that this wrapper wrote it. A forged record can
only misreport the tree it ran on.

Encoding of a returned value (``encode``), deterministic for equal values:
``None``, ``bool``, ``str`` and finite ``float`` as they are; an ``int`` up
to ``MAX_INT_BITS`` bits as it is; ``list`` and ``tuple`` as a list; ``set``
and ``frozenset`` as the list of their encoded items sorted by JSON text (as
``boundary/harness.py`` reports them); a ``dict`` with only ``str`` keys as
an object. Anything else (another type, a non-finite float, a larger int, a
dict with other keys, nesting past ``MAX_DEPTH``) becomes
``{"$object": [<qualified type name>, <text>]}``, where text is ``repr``
cut to ``MAX_TEXT`` characters (for a large int, its bit length). An encoded
value longer than ``MAX_VALUE_CHARS`` as JSON text is replaced by
``{"$object": [<qualified type name>, "sha256:<hex>"]}`` over that text.

Like ``boundary/harness.py``, this file runs in the task's interpreter,
which may be as old as Python 3.5: it imports only the standard library and
uses no f-string, no variable annotation, and no annotation newer than 3.5.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List

OBJECT_KEY = "$object"
MAX_DEPTH = 50
MAX_TEXT = 300
MAX_MESSAGE = 2000
MAX_INT_BITS = 3000
MAX_VALUE_CHARS = 256 * 1024
STREAM_LIMIT = 64 * 1024
_CHUNK = 65536
PROBE_MODULE = "ouroboros_probe"


def _type_name(value: Any) -> str:
    """``module.qualname`` of ``value``'s type; never raises."""
    try:
        kind = type(value)
        return str.__str__(kind.__module__) + "." + str.__str__(kind.__qualname__)
    except BaseException:
        return "?"


def _text(value: Any, limit: int) -> str:
    """``repr(value)`` cut to ``limit`` characters; a fixed marker when it raises."""
    try:
        return str.__str__(repr(value))[:limit]
    except BaseException:
        return "<unrenderable " + _type_name(value) + ">"


def _message(exc: BaseException) -> str:
    """``str(exc)`` cut to ``MAX_MESSAGE``; ``repr`` or a marker when it raises."""
    try:
        return str.__str__(str(exc))[:MAX_MESSAGE]
    except BaseException:
        return _text(exc, MAX_MESSAGE)


def _tagged(value: Any, text: str) -> Dict[str, Any]:
    return {OBJECT_KEY: [_type_name(value), text]}


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _encode(value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return _tagged(value, _text(value, MAX_TEXT))
    if value is None or isinstance(value, (bool, str)):
        return str.__str__(value) if isinstance(value, str) else value
    if isinstance(value, int):
        if value.bit_length() > MAX_INT_BITS:
            return _tagged(value, str(value.bit_length()) + " bits")
        return int(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return _tagged(value, _text(value, MAX_TEXT))
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_encode(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_encode(item, depth + 1) for item in value]
        return sorted(items, key=_json_text)
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {str.__str__(key): _encode(item, depth + 1) for key, item in value.items()}
    return _tagged(value, _text(value, MAX_TEXT))


def encode(value: Any) -> Any:
    """``value`` as JSON data (see the module docstring); never raises."""
    try:
        encoded = _encode(value, 0)
        text = _json_text(encoded)
    except BaseException:
        return _tagged(value, _text(value, MAX_TEXT))
    if len(text) > MAX_VALUE_CHARS:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return _tagged(value, "sha256:" + digest)
    return encoded


def _failure(status: str, exc: BaseException) -> Dict[str, Any]:
    return {"status": status, "exception_type": _type_name(exc), "message": _message(exc)}


def _report(channel: int, nonce: str, record: Dict[str, Any]) -> None:
    data = ("\n" + nonce + " " + _json_text(record) + "\n").encode("utf-8")
    while data:
        data = data[os.write(channel, data) :]


def _private_stdout() -> int:
    """A private copy of fd 1 for the record; fds 1 and 2 then point to the null device."""
    channel = os.dup(1)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    return channel


def _call(nonce: str, path: str) -> None:
    channel = _private_stdout()
    cwd = os.getcwd()
    for entry in (os.path.join(cwd, "src"), cwd):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        spec = importlib.util.spec_from_file_location(PROBE_MODULE, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[PROBE_MODULE] = module
        spec.loader.exec_module(module)
        probe = module.probe
        if not callable(probe):
            raise TypeError("probe is not callable")
    except BaseException as exc:
        _report(channel, nonce, _failure("not_run", exc))
        os._exit(0)
    try:
        value = probe()
    except BaseException as exc:
        _report(channel, nonce, _failure("raised", exc))
        os._exit(0)
    _report(channel, nonce, {"status": "returned", "value": encode(value)})
    os._exit(0)


def _drain(stream: Any, kept: List[bytes]) -> None:
    """Read ``stream`` to its end; keep its first ``STREAM_LIMIT`` bytes."""
    size = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        if size < STREAM_LIMIT:
            kept.append(chunk[: STREAM_LIMIT - size])
            size += len(kept[-1])
    stream.close()


def _feed(stream: Any, data: bytes) -> None:
    try:
        stream.write(data)
    except OSError:
        pass
    try:
        stream.close()
    except OSError:
        pass


def _decoded(chunks: List[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "backslashreplace")


def _command(nonce: str, path: str) -> None:
    channel = _private_stdout()
    with open(path, "rb") as handle:
        spec = json.loads(handle.read().decode("utf-8"))
    try:
        process = subprocess.Popen(
            [str(item) for item in spec["argv"]],
            cwd=os.getcwd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        _report(channel, nonce, _failure("not_run", exc))
        os._exit(0)
    out = []  # type: List[bytes]
    err = []  # type: List[bytes]
    threads = [
        threading.Thread(target=_feed, args=(process.stdin, spec["stdin"].encode("utf-8"))),
        threading.Thread(target=_drain, args=(process.stdout, out)),
        threading.Thread(target=_drain, args=(process.stderr, err)),
    ]
    for thread in threads:
        thread.daemon = True
        thread.start()
    code = process.wait()
    for thread in threads:
        thread.join()
    record = {
        "status": "exited",
        "exit_code": code,
        "stdout": _decoded(out),
        "stderr": _decoded(err),
    }
    _report(channel, nonce, record)
    os._exit(0)


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "call" and len(sys.argv) == 4:
        _call(sys.argv[2], sys.argv[3])
    elif role == "command" and len(sys.argv) == 4:
        _command(sys.argv[2], sys.argv[3])
    else:
        sys.stderr.write("usage: probe_wrapper call|command <nonce> <path>\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
