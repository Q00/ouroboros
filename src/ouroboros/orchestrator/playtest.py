"""Play a built browser app the way a first-time user would, and keep what the screen showed.

The completion pass reviews code, and so does the evaluation. Code reading misses
what only a running app shows: a camera that never turns with the track, a
banner that never clears, a road drawn as stray lines. This module runs a short,
bounded play script against the app in a headless Chromium-family browser
already installed on the host and saves a screenshot at each ``shot`` step, plus
the page's console errors.

No dependency is added: the browser is driven over ``--remote-debugging-pipe``
(Chrome DevTools Protocol on two file descriptors), and the app is served
read-only by :mod:`http.server` on a random loopback port for the duration of the
play. The browser gets a fresh temporary profile that is removed afterwards.
Every file the host serves, and every file it writes in the working directory,
is reached from that directory without following a symlink: the app's code, and
a worker running beside the host, may have planted one.
When no such browser is found, or on Windows (where the pipe needs handles,
not descriptors), nothing runs.

A worker fixing what the screen showed may run in a sandbox that can start
neither a browser nor a server. Its replay command (:func:`main`) therefore only
leaves a request file in a check directory and waits; the completion pass,
which runs on the host, plays the saved script and writes the answer there
(:func:`write_answer`).

A play script is JSON written by a read-only reviewer that read the code:
``{"page": "index.html", "steps": [...]}``. Every step is one of
``{"wait": ms}``, ``{"shot": name}``, ``{"key": k, "hold": ms}``,
``{"keys": [k, ...], "hold": ms}``, ``{"press": k}``, ``{"type": text}``,
``{"click_text": text}``, ``{"click": [x, y]}``,
``{"drag": [x0, y0, x1, y1, ms]}``, ``{"wheel": [x, y, dy]}`` or
``{"reload": true}``. :func:`parse_play_script` bounds it.
``click_text`` finds a control by its label and a text field by its placeholder
or label: a script that clicked a field by position, guessed from the code,
missed it and typed into nothing. A ``type`` step that finds no text field
focused says so in the report, so a reviewer of the shots does not take the
lost typing for an app that ignores it.

The play also counts the frames the page draws during each step of a second or
more, from an isolated world the page cannot see. A game that steps once a frame
looks frozen or unresponsive when its frame rate collapses, and a reviewer of the
shots sees only that: the report says when the page drew far fewer frames than
at its best in the play, and when the browser itself drew far fewer than a
user's. The count found the play's own arrow and Shift keys stopping the
browser's frames on macOS, where every keyboard game had looked frozen and
its reviews had called the steering unresponsive. The browser uses the host's
GPU, as a user's does; forcing software rendering had halved the frame rate of
a canvas game.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import errno
import functools
from http import HTTPStatus
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any
import urllib.parse
import uuid

from ouroboros.orchestrator.verify_shell import sanitized_verify_environment

VIEWPORT = (1280, 800)
MAX_STEPS = 40
MAX_SHOTS = 8
MAX_STEP_MS = 5000
MAX_SCRIPT_MS = 60000
MAX_TEXT_CHARS = 200
MAX_ERRORS = 20
LOAD_SETTLE_SECONDS = 1.5
CDP_CALL_TIMEOUT_SECONDS = 20.0
# The steps of one play; past this the play stops and keeps the shots it took, so
# browser start-up, the page load and the close still fit in the caller's bound.
MAX_PLAY_SECONDS = 120.0
# Mouse moves one drag sends: each waits for the page, which a heavy page answers slowly.
MAX_DRAG_MOVES = 15
MAX_NOTES = 20
# A step this long or longer is timed in frames. A user's browser draws about 60
# frames a second; a game that moves once a frame and caps the time a frame may
# cover runs slower than for a user once its frame rate falls far below that.
FRAME_STEP_MS = 1000
USER_FPS = 60
SLOW_FPS = 30
STALL_FPS = 20
MAX_STALL_NOTES = 3
# A worker's replay request: the host plays the script (at most PLAY_BOUND_SECONDS in
# the completion pass) and answers in the same directory.
REQUEST_PREFIX = "request-"
RESULT_PREFIX = "result-"
CHECK_WAIT_SECONDS = 200.0
CHECK_POLL_SECONDS = 0.5

BROWSER_CANDIDATES: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)

# key -> (DOM code, Windows virtual key code, text the key types)
_KEYS: dict[str, tuple[str, int, str]] = {
    "ArrowUp": ("ArrowUp", 38, ""),
    "ArrowDown": ("ArrowDown", 40, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""),
    "ArrowRight": ("ArrowRight", 39, ""),
    " ": ("Space", 32, " "),
    "Shift": ("ShiftLeft", 16, ""),
    "Control": ("ControlLeft", 17, ""),
    "Alt": ("AltLeft", 18, ""),
    "Enter": ("Enter", 13, "\r"),
    "Escape": ("Escape", 27, ""),
    "Tab": ("Tab", 9, ""),
    "Backspace": ("Backspace", 8, ""),
}
_KEY_ALIASES = {"Space": " ", "Spacebar": " ", "Up": "ArrowUp", "Down": "ArrowDown"}
_KEY_ALIASES.update({"Left": "ArrowLeft", "Right": "ArrowRight", "Esc": "Escape"})


def _key_info(key: str) -> tuple[str, str, int, str] | None:
    """(key, code, virtual key code, text) for a named key or one printable character."""
    key = _KEY_ALIASES.get(key, key)
    if key in _KEYS:
        code, vk, text = _KEYS[key]
        return key, code, vk, text
    if len(key) == 1 and key.isprintable():
        if key.isalpha():
            return key, f"Key{key.upper()}", ord(key.upper()), key
        if key.isdigit():
            return key, f"Digit{key}", ord(key), key
        return key, "", 0, key
    return None


@dataclass(frozen=True)
class PlayScript:
    page: str
    steps: tuple[dict[str, Any], ...]

    def describe(self) -> str:
        """The steps in plain words, for the reviewer who looks at the shots."""
        return "\n".join(f"{n}. {_describe(step)}" for n, step in enumerate(self.steps, 1))


@dataclass
class PlayReport:
    shots: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _describe(step: dict[str, Any]) -> str:
    kind = next(iter(step))
    if kind == "key":
        return f"hold {step['key']} for {step.get('hold', 100)} ms"
    if kind == "keys":
        return f"hold {' + '.join(step['keys'])} for {step.get('hold', 100)} ms"
    if kind == "shot":
        return f"screenshot {step['shot']}.png"
    return f"{kind} {json.dumps(step[kind], ensure_ascii=False)}"


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(min(max(value, low), high))


def _step(raw: Any) -> dict[str, Any] | None:
    """One bounded step, or ``None`` for anything outside the vocabulary."""
    if not isinstance(raw, dict) or not raw:
        return None
    w, h = VIEWPORT
    if "wait" in raw:
        ms = _int(raw["wait"], 0, MAX_STEP_MS)
        return None if ms is None else {"wait": ms}
    if "shot" in raw:
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(raw["shot"]))[:32]
        return {"shot": name} if name else None
    if "key" in raw or "keys" in raw:
        keys = raw.get("keys") if "keys" in raw else [raw.get("key")]
        if not isinstance(keys, list) or not 0 < len(keys) <= 4:
            return None
        if not all(isinstance(k, str) and _key_info(k) for k in keys):
            return None
        hold = _int(raw.get("hold", 100), 0, MAX_STEP_MS)
        if hold is None:
            return None
        return {"keys": keys, "hold": hold} if "keys" in raw else {"key": keys[0], "hold": hold}
    if "press" in raw:
        key = raw["press"]
        return {"press": key} if isinstance(key, str) and _key_info(key) else None
    if "type" in raw:
        text = raw["type"]
        return {"type": text[:MAX_TEXT_CHARS]} if isinstance(text, str) and text else None
    if "click_text" in raw:
        text = raw["click_text"]
        return {"click_text": text[:80]} if isinstance(text, str) and text.strip() else None
    if "click" in raw:
        xy = raw["click"]
        if isinstance(xy, list) and len(xy) == 2:
            x, y = _int(xy[0], 0, w - 1), _int(xy[1], 0, h - 1)
            if x is not None and y is not None:
                return {"click": [x, y]}
        return None
    if "drag" in raw:
        d = raw["drag"]
        if isinstance(d, list) and len(d) == 5:
            vals = [_int(d[i], 0, (w - 1, h - 1)[i % 2]) for i in range(4)]
            ms = _int(d[4], 50, MAX_STEP_MS)
            if None not in vals and ms is not None:
                return {"drag": [*vals, ms]}
        return None
    if "wheel" in raw:
        d = raw["wheel"]
        if isinstance(d, list) and len(d) == 3:
            x, y, dy = _int(d[0], 0, w - 1), _int(d[1], 0, h - 1), _int(d[2], -5000, 5000)
            if None not in (x, y, dy):
                return {"wheel": [x, y, dy]}
        return None
    if raw.get("reload") is True:
        return {"reload": True}
    return None


def _step_ms(step: dict[str, Any]) -> int:
    if "wait" in step:
        return int(step["wait"])
    if "key" in step or "keys" in step:
        return int(step["hold"])
    if "drag" in step:
        return int(step["drag"][4])
    if "reload" in step:
        return int(LOAD_SETTLE_SECONDS * 1000)
    return 0


def parse_play_script(reply: str, site: Path) -> PlayScript | None:
    """The bounded play script in a reviewer's reply, or ``None``.

    The reply's last JSON object counts. The page must be a file inside ``site``.
    Steps outside the vocabulary are dropped; the script stops at ``MAX_STEPS``
    steps, ``MAX_SHOTS`` shots or ``MAX_SCRIPT_MS`` of waiting and holding.
    """
    data: Any = None
    for match in re.finditer(r"\{", reply):
        try:
            data, _ = json.JSONDecoder().raw_decode(reply[match.start() :])
        except ValueError:
            continue
        if isinstance(data, dict) and "steps" in data:
            break
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None
    page = str(data.get("page") or "index.html").lstrip("/").split("?")[0].split("#")[0]
    root = site.resolve()
    target = (root / page).resolve()
    if root not in target.parents or not target.is_file():
        return None
    steps: list[dict[str, Any]] = []
    shots = total = 0
    for raw in data["steps"]:
        step = _step(raw)
        if step is None:
            continue
        if "shot" in step:
            if shots >= MAX_SHOTS:
                continue
            shots += 1
        total += _step_ms(step)
        if total > MAX_SCRIPT_MS or len(steps) >= MAX_STEPS:
            break
        steps.append(step)
    if not shots:
        return None
    return PlayScript(page=target.relative_to(root).as_posix(), steps=tuple(steps))


def installed_browser() -> str | None:
    """An installed Chromium-family browser that can be driven over a pipe."""
    if sys.platform.startswith("win"):
        return None
    for candidate in BROWSER_CANDIDATES:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def find_browser() -> str | None:
    """The browser the completion pass plays the app in; tests replace this."""
    return installed_browser()


class _Pipe:
    """Chrome DevTools Protocol over ``--remote-debugging-pipe`` (fd 3 in, fd 4 out)."""

    def __init__(self, browser: str, profile: str) -> None:
        to_read, to_write = os.pipe()
        from_read, from_write = os.pipe()
        # subprocess closes descriptors after any preexec hook, so the shell wires
        # the pipe ends to fds 3 and 4 just before it execs the browser.
        wiring = [
            f"3<&{to_read}" if to_read != 3 else "",
            f"4>&{from_write}" if from_write != 4 else "",
            f"{to_read}<&-" if to_read not in (3, 4) else "",
            f"{from_write}>&-" if from_write not in (3, 4) else "",
        ]
        w, h = VIEWPORT
        self.proc = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                'exec "$0" "$@" ' + " ".join(part for part in wiring if part),
                browser,
                "--headless=new",
                "--remote-debugging-pipe",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                # A user's keychain or keyring prompt would block a headless run.
                "--use-mock-keychain",
                "--password-store=basic",
                "--mute-audio",
                # navigator.webdriver stays false, as in a user's browser.
                "--disable-blink-features=AutomationControlled",
                # The host's GPU draws as a user's browser does; without one,
                # WebGL falls back to software rather than failing.
                "--enable-unsafe-swiftshader",
                f"--window-size={w},{h}",
                "about:blank",
            ],
            pass_fds=(to_read, from_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # The page is code the run built: the browser gets no loader or shell
            # injection from the host's environment.
            env=sanitized_verify_environment(),
        )
        os.close(to_read)
        os.close(from_write)
        self._out = os.fdopen(to_write, "wb", buffering=0)
        self._in = os.fdopen(from_read, "rb", buffering=0)
        self._buffer = b""
        self._next_id = 0
        self.events: list[dict[str, Any]] = []
        self.dialogs: list[str] = []

    def _message(self, deadline: float) -> dict[str, Any]:
        import select

        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("browser did not answer")
            ready, _, _ = select.select([self._in], [], [], remaining)
            if not ready:
                continue
            chunk = self._in.read(65536)
            if not chunk:
                raise RuntimeError("browser closed the pipe")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        message: dict[str, Any] = json.loads(raw)
        return message

    def call(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> dict[str, Any]:
        call_id = self._send(method, params, session)
        deadline = time.monotonic() + CDP_CALL_TIMEOUT_SECONDS
        while True:
            message = self._message(deadline)
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                result: dict[str, Any] = message.get("result", {})
                return result
            if message.get("method") == "Page.javascriptDialogOpening":
                # An alert, confirm or prompt blocks the page, and every call after
                # it, until it is answered; a user would click OK.
                dialog = message.get("params") or {}
                self.dialogs.append(
                    f"the page opened a {dialog.get('type', 'dialog')} dialog"
                    f" {str(dialog.get('message', ''))[:MAX_TEXT_CHARS]!r}; it was accepted"
                )
                self._send(
                    "Page.handleJavaScriptDialog",
                    {"accept": True, "promptText": str(dialog.get("defaultPrompt", ""))},
                    message.get("sessionId"),
                )
            elif len(self.events) < 2000 and "method" in message:
                self.events.append(message)

    def _send(self, method: str, params: dict[str, Any] | None, session: str | None) -> int:
        self._next_id += 1
        request: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self._out.write(json.dumps(request).encode() + b"\0")
        return self._next_id

    def kill(self) -> None:
        """End the browser now; a call waiting on it fails at once."""
        with contextlib.suppress(OSError):
            self.proc.kill()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.call("Browser.close")
            except Exception:  # noqa: BLE001 - the process is killed below anyway
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self._out, self._in):
            try:
                stream.close()
            except OSError:
                pass


def _flag(name: str) -> int:
    """An :mod:`os` open flag, or 0 where the platform has none.

    Windows lacks ``O_DIRECTORY``, ``O_NOFOLLOW`` and ``O_NONBLOCK``; nothing
    plays there (see :func:`installed_browser`), so these paths never run, but
    the module must still import.
    """
    return int(getattr(os, name, 0))


_DIR_FLAGS = os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_NOFOLLOW")


def open_dir_inside(root: Path, sub: Path, *, create: bool = False) -> int:
    """A descriptor for the directory ``sub`` under ``root``, reached without following a symlink.

    ``sub`` is relative to ``root`` or a path under it. With ``create``, each
    missing directory on the way is made. A symlink, a file or ``..`` on the way
    raises :class:`OSError`, so a planted link cannot steer a host read or write
    outside ``root``. The caller closes the descriptor.
    """
    try:
        parts = (sub.relative_to(root) if sub.is_absolute() else sub).parts
    except ValueError as exc:
        raise OSError(errno.EINVAL, "not under the app's directory", str(sub)) from exc
    fd = os.open(root, os.O_RDONLY | _flag("O_DIRECTORY"))
    try:
        for part in parts:
            if part in (".", ".."):
                raise OSError(errno.EINVAL, "not a plain path", str(sub))
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=fd)
            inner = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


def save_in(dir_fd: int, name: str, data: bytes) -> None:
    """Write ``data`` as ``name`` in the directory ``dir_fd``, whole or not at all.

    A symlink planted at ``name`` is replaced, never written through.
    """
    part = f".part-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flag("O_NOFOLLOW")
    fd = os.open(part, flags, 0o644, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.rename(part, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(part, dir_fd=dir_fd)
        raise


class _SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the app's directory read-only.

    A file reached through a symlink, anything but a regular file, and a
    directory listing are not found.
    """

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def send_head(self) -> IO[bytes] | None:
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        parts = [part for part in path.split("/") if part]
        if not parts or path.endswith("/"):
            parts.append("index.html")
        dir_fd = None
        if not any(part in (".", "..") for part in parts):
            try:
                dir_fd = open_dir_inside(Path(self.directory), Path(*parts[:-1]))
            except (OSError, ValueError):
                dir_fd = None
        fd = None
        if dir_fd is not None:
            try:
                flags = os.O_RDONLY | _flag("O_NOFOLLOW") | _flag("O_NONBLOCK")
                fd = os.open(parts[-1], flags, dir_fd=dir_fd)
            except (OSError, ValueError):
                fd = None
            finally:
                os.close(dir_fd)
        info = os.fstat(fd) if fd is not None else None
        if fd is not None and info is not None and stat.S_ISDIR(info.st_mode):
            os.close(fd)
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", urllib.parse.urlunsplit(url._replace(path=url.path + "/")))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if fd is None or info is None or not stat.S_ISREG(info.st_mode):
            if fd is not None:
                os.close(fd)
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", self.guess_type(parts[-1]))
        self.send_header("Content-Length", str(info.st_size))
        self.send_header("Last-Modified", self.date_time_string(int(info.st_mtime)))
        self.end_headers()
        return os.fdopen(fd, "rb")


_CLICK_TEXT_JS = """(() => {
  const want = %s.trim().toLowerCase();
  const seen = (e) => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
  const field = (e) => e.matches('textarea, [contenteditable=""], [contenteditable=true]')
    || (e.matches('input') && !/^(button|submit|reset|image)$/i.test(e.type));
  const names = (e) => (field(e)
    ? [e.placeholder, e.getAttribute('aria-label'), ...[...(e.labels || [])].map((l) => l.innerText)]
    : [e.innerText || e.value || e.getAttribute('aria-label')]
  ).map((n) => (n || '').trim().toLowerCase()).filter(Boolean);
  const all = [...document.querySelectorAll('button, a, [role=button], input, textarea, [contenteditable], label, summary')].filter(seen);
  const hit = all.find((e) => names(e).includes(want)) || all.find((e) => names(e).some((n) => n.includes(want)));
  if (!hit) return null;
  const b = hit.getBoundingClientRect();
  return [b.x + b.width / 2, b.y + b.height / 2];
})()"""


_FIELD_FOCUSED_JS = """(() => {
  const e = document.activeElement;
  if (!e) return false;
  if (e.isContentEditable) return true;
  if (e.disabled || e.readOnly) return false;
  if (e.tagName === 'TEXTAREA') return true;
  return e.tagName === 'INPUT' && !/^(button|submit|reset|image|checkbox|radio|range|color|file|hidden)$/i.test(e.type);
})()"""


def key_event(name: str, down: bool) -> dict[str, Any] | None:
    """The DevTools key event that presses (``down``) or releases key ``name``."""
    info = _key_info(name)
    if info is None:
        return None
    dom_key, code, vk, text = info
    # No nativeVirtualKeyCode: on macOS it names a Mac key, and a Windows code
    # there made an arrow key or Shift stop the browser drawing frames for the
    # rest of the play, so every keyboard game looked frozen.
    params: dict[str, Any] = {
        "type": "keyDown" if down else "keyUp",
        "key": dom_key,
        "code": code,
        "windowsVirtualKeyCode": vk,
    }
    if down and text:
        params["text"] = text
    return params


# Counts the frames the page draws, from a world of its own the page cannot see.
_FRAME_COUNTER_JS = """(() => {
  if (!globalThis.ouroborosFrames) {
    let n = 0;
    const tick = () => { n += 1; requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    globalThis.ouroborosFrames = () => n;
  }
  return globalThis.ouroborosFrames();
})()"""


class _FrameCounter:
    """Frames the page has drawn, counted in an isolated world.

    A navigation or reload drops the world. A read that finds it gone starts a
    new one at once, so the step about to run is still timed; a read names the
    world it counted in, and two reads from different worlds time nothing.
    """

    def __init__(self, cdp: Callable[..., dict[str, Any]]) -> None:
        self._cdp = cdp
        self._context: int | None = None
        self._world = 0

    def forget(self) -> None:
        """The page was reloaded: the next read starts counting in a new world."""
        self._context = None

    def read(self) -> tuple[int, int] | None:
        """(world, frames drawn since it began), or ``None`` when no world starts."""
        for _ in range(2):
            try:
                if self._context is None:
                    frame = self._cdp("Page.getFrameTree")["frameTree"]["frame"]["id"]
                    world = self._cdp(
                        "Page.createIsolatedWorld",
                        {"frameId": frame, "worldName": "ouroboros-play"},
                    )
                    self._context = int(world["executionContextId"])
                    self._world += 1
                found = self._cdp(
                    "Runtime.evaluate",
                    {
                        "expression": _FRAME_COUNTER_JS,
                        "contextId": self._context,
                        "returnByValue": True,
                    },
                )
            except (RuntimeError, KeyError, TypeError, ValueError):  # gone with a navigation
                self._context = None
                continue
            value = found.get("result", {}).get("value")
            if isinstance(value, int | float) and not isinstance(value, bool):
                return self._world, int(value)
            self._context = None
        return None


def frame_notes(rates: list[tuple[int, dict[str, Any], float]]) -> list[str]:
    """What the frame rates of the timed steps tell a reviewer of the shots.

    ``rates`` holds, per timed step, its number, the step and the frames a
    second the page drew while it ran.
    """
    if not rates:
        return []
    best = max(rate for _, _, rate in rates)
    notes: list[str] = []
    if best < SLOW_FPS:
        notes.append(
            f"this browser drew at most {best:.0f} frames a second, where a user's draws"
            f" about {USER_FPS}: what moves once a frame moved more slowly here than it"
            " would for a user, so slow motion or a control that seems to barely respond"
            " may be the play's, not the app's"
        )
    stalls = [
        (number, step, rate) for number, step, rate in rates if rate < STALL_FPS and rate < best / 3
    ]
    for number, step, rate in stalls[:MAX_STALL_NOTES]:
        notes.append(
            f"while step {number} ({_describe(step)}) ran, the page drew {rate:.1f} frames"
            f" a second, against {best:.0f} at its best in this play: its drawing stalled"
            " then, so a shot after it may show a frozen screen rather than what the"
            " controls did"
        )
    return notes


def _errors(events: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for event in events:
        method, params = event.get("method"), event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            found.append(str(exception.get("description") or details.get("text") or "exception"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            args = params.get("args") or []
            found.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error" and "favicon.ico" not in str(entry.get("url", "")):
                found.append(str(entry.get("text", "")))
    unique = list(dict.fromkeys(line.strip()[:300] for line in found if line.strip()))
    return unique[:MAX_ERRORS]


class PlayStopped(Exception):
    """The caller asked the play to stop."""


STOPPED_NOTE = "the play was stopped at its time limit"


def play(
    site: Path,
    script: PlayScript,
    out_dir: Path,
    browser: str,
    stop: threading.Event | None = None,
) -> PlayReport:
    """Run ``script`` against ``site`` and save its shots in ``out_dir``, a directory under it. Blocking.

    Setting ``stop`` ends the play early: the browser is killed at once, no
    screenshot is saved after it, and the play returns once it has cleaned up.
    """
    stop = stop or threading.Event()
    out_fd = open_dir_inside(site, out_dir, create=True)
    report = PlayReport()

    def shoot(name: str, data: bytes) -> Path:
        if stop.is_set():
            raise PlayStopped
        save_in(out_fd, name, data)
        return out_dir / name

    def pause(seconds: float) -> None:
        if stop.wait(seconds):
            raise PlayStopped

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(_SiteHandler, directory=str(site))
        )
    except BaseException:
        os.close(out_fd)
        raise
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="ouroboros-playtest-")
    pipe: _Pipe | None = None
    finished = threading.Event()
    try:
        pipe_ = _Pipe(browser, profile)
        pipe = pipe_

        def stopper() -> None:
            while not finished.is_set():
                if stop.wait(0.05):
                    pipe_.kill()
                    return

        threading.Thread(target=stopper, daemon=True).start()
        target = pipe_.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = pipe_.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]

        def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            return pipe_.call(method, params, session)

        for domain in ("Page.enable", "Runtime.enable", "Log.enable"):
            cdp(domain)
        # The page sees an ordinary Chrome: a page that told the headless browser
        # apart could show it something users never see.
        agent = str(pipe_.call("Browser.getVersion").get("userAgent", ""))
        if "HeadlessChrome" in agent:
            cdp(
                "Emulation.setUserAgentOverride",
                {"userAgent": agent.replace("HeadlessChrome", "Chrome")},
            )
        w, h = VIEWPORT
        cdp(
            "Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
        )
        cdp("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/{script.page}"})
        pause(LOAD_SETTLE_SECONDS)

        def key(name: str, down: bool) -> None:
            params = key_event(name, down)
            if params is not None:
                cdp("Input.dispatchKeyEvent", params)

        def mouse(kind: str, x: float, y: float, held: bool = False, **extra: Any) -> None:
            button = "left" if held or kind in ("mousePressed", "mouseReleased") else "none"
            params = {"type": kind, "x": x, "y": y, "button": button, "buttons": int(held)}
            cdp("Input.dispatchMouseEvent", {**params, **extra})

        def click(x: float, y: float) -> None:
            mouse("mouseMoved", x, y)
            mouse("mousePressed", x, y, True, clickCount=1)
            mouse("mouseReleased", x, y, clickCount=1)

        counter = _FrameCounter(cdp)
        rates: list[tuple[int, dict[str, Any], float]] = []
        started = time.monotonic()
        for number, step in enumerate(script.steps, 1):
            if time.monotonic() - started > MAX_PLAY_SECONDS:
                report.notes.append(
                    f"the play stopped before step {number} of {len(script.steps)}:"
                    f" its {MAX_PLAY_SECONDS:.0f} s ran out because the page answered slowly"
                )
                break
            try:
                if number == 1:
                    counter.read()  # starts counting
                timed = _step_ms(step) >= FRAME_STEP_MS and "reload" not in step
                before = counter.read() if timed else None
                began = time.monotonic()
                _run_step(step, cdp, key, mouse, click, shoot, pause, report)
                if "reload" in step:
                    counter.forget()
                after = counter.read() if before is not None else None
                if before is not None and after is not None and after[0] == before[0]:
                    frames = after[1] - before[1]
                    rates.append((number, step, frames / (time.monotonic() - began)))
            except Exception as exc:
                if stop.is_set():  # the killed browser fails whatever call was waiting
                    report.notes.append(STOPPED_NOTE)
                    break
                if not isinstance(exc, TimeoutError):
                    raise
                report.notes.append(
                    f"the page stopped answering at step {number} ({_describe(step)})"
                )
                break
        report.errors = _errors(pipe_.events)
        report.notes = [*pipe_.dialogs, *report.notes, *frame_notes(rates)][:MAX_NOTES]
    finally:
        finished.set()
        if pipe is not None:
            pipe.close()
        server.shutdown()
        server.server_close()
        shutil.rmtree(profile, ignore_errors=True)
        os.close(out_fd)
    return report


def _run_step(
    step: dict[str, Any],
    cdp: Callable[..., dict[str, Any]],
    key: Callable[[str, bool], None],
    mouse: Callable[..., None],
    click: Callable[[float, float], None],
    shoot: Callable[[str, bytes], Path],
    pause: Callable[[float], None],
    report: PlayReport,
) -> None:
    """Run one step of a play script against the attached page."""
    if "wait" in step:
        pause(step["wait"] / 1000)
    elif "shot" in step:
        data = cdp("Page.captureScreenshot", {"format": "png"})["data"]
        name = f"{len(report.shots) + 1:02d}-{step['shot']}.png"
        report.shots.append(shoot(name, base64.b64decode(data)))
    elif "key" in step or "keys" in step:
        keys = step.get("keys") or [step["key"]]
        for name in keys:
            key(name, True)
        pause(step["hold"] / 1000)
        for name in reversed(keys):
            key(name, False)
    elif "press" in step:
        key(step["press"], True)
        key(step["press"], False)
    elif "type" in step:
        focused = cdp("Runtime.evaluate", {"expression": _FIELD_FOCUSED_JS, "returnByValue": True})
        if focused["result"].get("value") is not True:
            report.notes.append(
                f"no text field had focus when the play typed {step['type']!r}, so the text"
                " went nowhere: the play's earlier click missed the field"
            )
        cdp("Input.insertText", {"text": step["type"]})
    elif "click" in step:
        click(*step["click"])
    elif "click_text" in step:
        found = cdp(
            "Runtime.evaluate",
            {
                "expression": _CLICK_TEXT_JS % json.dumps(step["click_text"]),
                "returnByValue": True,
            },
        )["result"].get("value")
        if isinstance(found, list) and len(found) == 2:
            click(float(found[0]), float(found[1]))
        else:
            report.notes.append(f"no visible control labelled {step['click_text']!r}")
    elif "drag" in step:
        x0, y0, x1, y1, ms = step["drag"]
        moves = max(2, min(MAX_DRAG_MOVES, ms // 30))
        mouse("mouseMoved", x0, y0)
        mouse("mousePressed", x0, y0, True, clickCount=1)
        for i in range(1, moves + 1):
            mouse("mouseMoved", x0 + (x1 - x0) * i / moves, y0 + (y1 - y0) * i / moves, True)
            pause(ms / moves / 1000)
        mouse("mouseReleased", x1, y1, clickCount=1)
    elif "wheel" in step:
        x, y, dy = step["wheel"]
        mouse("mouseWheel", x, y, deltaX=0, deltaY=dy)
    elif "reload" in step:
        cdp("Page.reload")
        pause(LOAD_SETTLE_SECONDS)


def write_answer(check_fd: int, nonce: str, answer: dict[str, Any]) -> None:
    """Write the answer to play request ``nonce`` in the check directory ``check_fd``.

    Its reader never sees half of it, and a symlink planted there is replaced.
    """
    save_in(check_fd, f"{RESULT_PREFIX}{nonce}.json", json.dumps(answer).encode())


def request_play(check_dir: Path, wait_seconds: float | None = None) -> dict[str, Any] | None:
    """Ask the host running the completion pass to play its saved script; its answer, or ``None``.

    A sandboxed worker can neither start a browser nor open a port, so it only
    leaves a request file here, and the host plays the page and answers next to it.
    """
    nonce = uuid.uuid4().hex
    check_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".request-", dir=check_dir)
    os.close(fd)
    os.replace(tmp, check_dir / f"{REQUEST_PREFIX}{nonce}")
    result = check_dir / f"{RESULT_PREFIX}{nonce}.json"
    deadline = time.monotonic() + (CHECK_WAIT_SECONDS if wait_seconds is None else wait_seconds)
    while True:
        if result.is_file():
            try:
                answer = json.loads(result.read_text())
            except (OSError, ValueError):
                answer = None
            if isinstance(answer, dict):
                return answer
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(CHECK_POLL_SECONDS, left))


def main(argv: list[str] | None = None) -> int:
    """``python -m ouroboros.orchestrator.playtest <check_dir>``.

    Asks the completion pass to replay its saved play script and prints the
    screenshots that play saved, so a worker fixing what the screen showed can
    look at the page after each change.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m ouroboros.orchestrator.playtest <check_dir>")
        return 2
    answer = request_play(Path(args[0]))
    if answer is None:
        print("no play answered: this command works only while the completion pass is fixing")
        return 1
    if answer.get("error"):
        print(answer["error"])
        return 1
    print("Screenshots, in order:")
    for path in answer.get("shots", ()):
        print(f"- {path}")
    for line in answer.get("errors", ()):
        print(f"page error: {line}")
    for line in answer.get("notes", ()):
        print(f"note: {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module entry point
    raise SystemExit(main())


__all__ = [
    "BROWSER_CANDIDATES",
    "CHECK_WAIT_SECONDS",
    "MAX_SHOTS",
    "MAX_STEPS",
    "STOPPED_NOTE",
    "PlayReport",
    "PlayScript",
    "PlayStopped",
    "find_browser",
    "installed_browser",
    "main",
    "open_dir_inside",
    "parse_play_script",
    "play",
    "request_play",
    "save_in",
    "write_answer",
]
