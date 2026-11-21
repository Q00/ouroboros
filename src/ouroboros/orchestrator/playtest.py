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
When no such browser is found, or on Windows (where the pipe needs handles,
not descriptors), nothing runs.

A play script is JSON written by a read-only reviewer that read the code:
``{"page": "index.html", "steps": [...]}``. Every step is one of
``{"wait": ms}``, ``{"shot": name}``, ``{"key": k, "hold": ms}``,
``{"keys": [k, ...], "hold": ms}``, ``{"press": k}``, ``{"type": text}``,
``{"click_text": text}``, ``{"click": [x, y]}``,
``{"drag": [x0, y0, x1, y1, ms]}``, ``{"wheel": [x, y, dy]}`` or
``{"reload": true}``. :func:`parse_play_script` bounds it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import functools
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

VIEWPORT = (1280, 800)
MAX_STEPS = 40
MAX_SHOTS = 8
MAX_STEP_MS = 5000
MAX_SCRIPT_MS = 60000
MAX_TEXT_CHARS = 200
MAX_ERRORS = 20
LOAD_SETTLE_SECONDS = 1.5
CDP_CALL_TIMEOUT_SECONDS = 20.0

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
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                f"--window-size={w},{h}",
                "about:blank",
            ],
            pass_fds=(to_read, from_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(to_read)
        os.close(from_write)
        self._out = os.fdopen(to_write, "wb", buffering=0)
        self._in = os.fdopen(from_read, "rb", buffering=0)
        self._buffer = b""
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

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
        self._next_id += 1
        request: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self._out.write(json.dumps(request).encode() + b"\0")
        deadline = time.monotonic() + CDP_CALL_TIMEOUT_SECONDS
        while True:
            message = self._message(deadline)
            if message.get("id") == self._next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                result: dict[str, Any] = message.get("result", {})
                return result
            if len(self.events) < 2000:
                self.events.append(message)

    def close(self) -> None:
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


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


_CLICK_TEXT_JS = """(() => {
  const want = %s.toLowerCase();
  const seen = (e) => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
  const label = (e) => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase();
  const all = [...document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit], label, summary')].filter(seen);
  const hit = all.find((e) => label(e) === want) || all.find((e) => label(e).includes(want));
  if (!hit) return null;
  const b = hit.getBoundingClientRect();
  return [b.x + b.width / 2, b.y + b.height / 2];
})()"""


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


def play(site: Path, script: PlayScript, out_dir: Path, browser: str) -> PlayReport:
    """Run ``script`` against ``site`` and save its shots in ``out_dir``. Blocking."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = PlayReport()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(site))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="ouroboros-playtest-")
    pipe: _Pipe | None = None
    try:
        pipe_ = _Pipe(browser, profile)
        pipe = pipe_
        target = pipe_.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = pipe_.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]

        def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            return pipe_.call(method, params, session)

        for domain in ("Page.enable", "Runtime.enable", "Log.enable"):
            cdp(domain)
        w, h = VIEWPORT
        cdp(
            "Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
        )
        cdp("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/{script.page}"})
        time.sleep(LOAD_SETTLE_SECONDS)

        def key(name: str, down: bool) -> None:
            info = _key_info(name)
            if info is None:
                return
            dom_key, code, vk, text = info
            params: dict[str, Any] = {
                "type": "keyDown" if down else "keyUp",
                "key": dom_key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            }
            if down and text:
                params["text"] = text
            cdp("Input.dispatchKeyEvent", params)

        def mouse(kind: str, x: float, y: float, held: bool = False, **extra: Any) -> None:
            button = "left" if held or kind in ("mousePressed", "mouseReleased") else "none"
            params = {"type": kind, "x": x, "y": y, "button": button, "buttons": int(held)}
            cdp("Input.dispatchMouseEvent", {**params, **extra})

        def click(x: float, y: float) -> None:
            mouse("mouseMoved", x, y)
            mouse("mousePressed", x, y, True, clickCount=1)
            mouse("mouseReleased", x, y, clickCount=1)

        for step in script.steps:
            if "wait" in step:
                time.sleep(step["wait"] / 1000)
            elif "shot" in step:
                data = cdp("Page.captureScreenshot", {"format": "png"})["data"]
                path = out_dir / f"{len(report.shots) + 1:02d}-{step['shot']}.png"
                path.write_bytes(base64.b64decode(data))
                report.shots.append(path)
            elif "key" in step or "keys" in step:
                keys = step.get("keys") or [step["key"]]
                for name in keys:
                    key(name, True)
                time.sleep(step["hold"] / 1000)
                for name in reversed(keys):
                    key(name, False)
            elif "press" in step:
                key(step["press"], True)
                key(step["press"], False)
            elif "type" in step:
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
                moves = max(2, ms // 30)
                mouse("mouseMoved", x0, y0)
                mouse("mousePressed", x0, y0, True, clickCount=1)
                for i in range(1, moves + 1):
                    mouse(
                        "mouseMoved", x0 + (x1 - x0) * i / moves, y0 + (y1 - y0) * i / moves, True
                    )
                    time.sleep(ms / moves / 1000)
                mouse("mouseReleased", x1, y1, clickCount=1)
            elif "wheel" in step:
                x, y, dy = step["wheel"]
                mouse("mouseWheel", x, y, deltaX=0, deltaY=dy)
            elif "reload" in step:
                cdp("Page.reload")
                time.sleep(LOAD_SETTLE_SECONDS)
        report.errors = _errors(pipe_.events)
    finally:
        if pipe is not None:
            pipe.close()
        server.shutdown()
        server.server_close()
        shutil.rmtree(profile, ignore_errors=True)
    return report


__all__ = [
    "BROWSER_CANDIDATES",
    "MAX_SHOTS",
    "MAX_STEPS",
    "PlayReport",
    "PlayScript",
    "find_browser",
    "installed_browser",
    "parse_play_script",
    "play",
]
