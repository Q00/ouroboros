"""Playing a built browser app in a headless browser: the script bounds and the driver."""

from __future__ import annotations

import base64
import functools
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from ouroboros.orchestrator import playtest
from ouroboros.orchestrator.playtest import (
    MAX_SHOTS,
    MAX_STEPS,
    PlayScript,
    parse_play_script,
)


def _site(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<p>hi</p>")
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "play.html").write_text("<p>game</p>")
    return tmp_path


def _reply(steps: list, page: str = "index.html") -> str:
    return "Plan:\n" + json.dumps({"page": page, "steps": steps})


def test_the_script_is_the_json_object_with_steps_in_the_reply(tmp_path) -> None:
    reply = 'Note {"a": 1}. ' + _reply([{"shot": "menu"}, {"press": "Enter"}], "/game/play.html")

    script = parse_play_script(reply, _site(tmp_path))

    assert script == PlayScript(page="game/play.html", steps=({"shot": "menu"}, {"press": "Enter"}))


@pytest.mark.parametrize(
    "reply",
    [
        "NONE",
        _reply([{"shot": "a"}], "../outside.html"),
        _reply([{"shot": "a"}], "missing.html"),
        _reply([{"press": "Enter"}]),  # no screenshot: nothing to look at
        '{"page": "index.html", "steps": "shot"}',
    ],
)
def test_no_script_for_a_page_outside_the_app_or_nothing_to_look_at(tmp_path, reply) -> None:
    assert parse_play_script(reply, _site(tmp_path)) is None


def test_steps_outside_the_vocabulary_are_dropped_and_values_bounded(tmp_path) -> None:
    steps = [
        {"shot": "a b/../c"},
        {"wait": 999999},
        {"key": "F13", "hold": 10},
        {"keys": ["ArrowUp", "Left"], "hold": -5},
        {"key": "w"},
        {"click": [5000, -3]},
        {"drag": [0, 0, 9999, 9999, 1]},
        {"wheel": [10, 10, 99999]},
        {"type": "x" * 500},
        {"eval": "alert(1)"},
        {"reload": "yes"},
        {"reload": True},
    ]

    script = parse_play_script(_reply(steps), _site(tmp_path))

    assert script is not None
    assert script.steps == (
        {"shot": "abc"},
        {"wait": 5000},
        {"keys": ["ArrowUp", "Left"], "hold": 0},
        {"key": "w", "hold": 100},
        {"click": [1279, 0]},
        {"drag": [0, 0, 1279, 799, 50]},
        {"wheel": [10, 10, 5000]},
        {"type": "x" * 200},
        {"reload": True},
    )


def test_the_script_stops_at_its_step_shot_and_time_bounds(tmp_path) -> None:
    site = _site(tmp_path)
    many_shots = [{"shot": f"s{n}"} for n in range(MAX_SHOTS + 5)]
    long_play = [{"shot": "a"}] + [{"key": "ArrowUp", "hold": 5000}] * 20
    many_steps = [{"shot": "a"}] + [{"press": "Enter"}] * (MAX_STEPS + 10)

    shots = parse_play_script(_reply(many_shots), site)
    timed = parse_play_script(_reply(long_play), site)
    counted = parse_play_script(_reply(many_steps), site)

    assert shots is not None and len(shots.steps) == MAX_SHOTS
    assert timed is not None and len(timed.steps) == 1 + 12  # 60 s of holding
    assert counted is not None and len(counted.steps) == MAX_STEPS


def test_the_steps_are_described_for_the_reviewer(tmp_path) -> None:
    script = parse_play_script(
        _reply(
            [{"click_text": "Start"}, {"keys": ["ArrowUp", "Shift"], "hold": 900}, {"shot": "x"}]
        ),
        _site(tmp_path),
    )

    assert script is not None
    assert script.describe() == (
        '1. click_text "Start"\n2. hold ArrowUp + Shift for 900 ms\n3. screenshot x.png'
    )


def test_a_browser_is_found_among_the_candidates(tmp_path, monkeypatch) -> None:
    browser = tmp_path / "chrome"
    browser.write_text("#!/bin/sh\n")
    browser.chmod(0o755)
    monkeypatch.setattr(playtest, "BROWSER_CANDIDATES", ("/no/such/browser", str(browser)))
    monkeypatch.setattr(playtest.sys, "platform", "darwin")

    assert playtest.installed_browser() == str(browser)


def test_no_browser_on_windows_or_when_none_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(playtest, "BROWSER_CANDIDATES", ("/no/such/browser", "no-such-browser"))
    monkeypatch.setattr(playtest.sys, "platform", "darwin")
    assert playtest.installed_browser() is None
    monkeypatch.setattr(playtest.sys, "platform", "win32")
    assert playtest.installed_browser() is None


@pytest.mark.parametrize(("focused", "noted"), [(True, False), (False, True), (None, True)])
def test_typing_with_no_text_field_focused_is_noted(focused, noted) -> None:
    calls: list[tuple[str, Any]] = []

    def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, params))
        return {"result": {"value": focused}} if method == "Runtime.evaluate" else {}

    report = playtest.PlayReport()
    unused: Any = None
    playtest._run_step({"type": "walk"}, cdp, unused, unused, unused, unused, unused, report)

    assert calls[-1] == ("Input.insertText", {"text": "walk"})  # typed as the user would anyway
    assert bool(report.notes) is noted
    if noted:
        assert "typed 'walk', so the text went nowhere" in report.notes[0]


def _hold(ms: int) -> dict[str, Any]:
    return {"key": "ArrowUp", "hold": ms}


def test_frame_rates_note_a_stall_and_a_browser_slower_than_a_users() -> None:
    assert playtest.frame_notes([]) == []
    assert playtest.frame_notes([(2, _hold(4000), 60.0), (4, _hold(4000), 45.0)]) == []

    stalled = playtest.frame_notes(
        [(2, _hold(4000), 60.0), (4, _hold(4000), 0.2), (5, {"wait": 2000}, 0.0)]
    )
    assert stalled == [
        "while step 4 (hold ArrowUp for 4000 ms) ran, the page drew 0.2 frames a second,"
        " against 60 at its best in this play: its drawing stalled then, so a shot after"
        " it may show a frozen screen rather than what the controls did",
        "while step 5 (wait 2000) ran, the page drew 0.0 frames a second, against 60 at"
        " its best in this play: its drawing stalled then, so a shot after it may show a"
        " frozen screen rather than what the controls did",
    ]

    slow = playtest.frame_notes([(2, _hold(4000), 14.0), (3, _hold(4000), 9.0)])
    assert slow == [
        "this browser drew at most 14 frames a second, where a user's draws about 60:"
        " what moves once a frame moved more slowly here than it would for a user, so"
        " slow motion or a control that seems to barely respond may be the play's, not"
        " the app's"
    ]

    many = playtest.frame_notes(
        [(1, _hold(2000), 60.0)] + [(n, _hold(2000), 1.0) for n in range(2, 8)]
    )
    assert len(many) == playtest.MAX_STALL_NOTES


def test_the_frame_counter_starts_in_its_own_world_and_again_at_once_after_a_navigation() -> None:
    calls: list[str] = []
    answers: list[Any] = [
        {"frameTree": {"frame": {"id": "f"}}},
        {"executionContextId": 7},
        {"result": {"value": 0}},
        {"result": {"value": 42}},
        RuntimeError("Runtime.evaluate: Cannot find context with specified id"),
        {"frameTree": {"frame": {"id": "g"}}},
        {"executionContextId": 8},
        {"result": {"value": 0}},
        {"frameTree": {"frame": {"id": "h"}}},
        {"executionContextId": 9},
        {"result": {"value": 3}},
    ]
    contexts: list[int] = []

    def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append(method)
        if method == "Runtime.evaluate":
            assert params is not None
            contexts.append(params["contextId"])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    counter = playtest._FrameCounter(cdp)

    assert counter.read() == (1, 0)  # started in a world of its own
    assert counter.read() == (1, 42)
    assert counter.read() == (2, 0)  # the world went with a navigation: a new one, at once
    counter.forget()
    assert counter.read() == (3, 3)  # a reload: counting starts again in a new world
    assert contexts == [7, 7, 7, 8, 9]
    assert calls.count("Page.createIsolatedWorld") == 3
    assert answers == []


@pytest.mark.parametrize(
    "answers",
    [
        [RuntimeError("no frame tree"), RuntimeError("no frame tree")],
        [{"frameTree": {"frame": {"id": "f"}}}, {"executionContextId": 7}, {"result": {}}] * 2,
    ],
)
def test_the_frame_counter_gives_up_after_a_second_failed_start(answers: list[Any]) -> None:
    def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert playtest._FrameCounter(cdp).read() is None
    assert answers == []


def test_a_key_is_sent_without_a_native_key_code() -> None:
    assert playtest.key_event("ArrowUp", True) == {
        "type": "keyDown",
        "key": "ArrowUp",
        "code": "ArrowUp",
        "windowsVirtualKeyCode": 38,
    }
    assert playtest.key_event("Shift", False) == {
        "type": "keyUp",
        "key": "Shift",
        "code": "ShiftLeft",
        "windowsVirtualKeyCode": 16,
    }
    assert playtest.key_event("a", True)["text"] == "a"  # type: ignore[index]
    assert playtest.key_event("F13", True) is None


def test_console_errors_are_kept_once_without_the_favicon() -> None:
    events = [
        {
            "method": "Runtime.exceptionThrown",
            "params": {
                "exceptionDetails": {"exception": {"description": "TypeError: lap is undefined"}}
            },
        },
        {
            "method": "Runtime.exceptionThrown",
            "params": {
                "exceptionDetails": {"exception": {"description": "TypeError: lap is undefined"}}
            },
        },
        {
            "method": "Log.entryAdded",
            "params": {
                "entry": {"level": "error", "text": "404", "url": "http://127.0.0.1:1/favicon.ico"}
            },
        },
        {
            "method": "Runtime.consoleAPICalled",
            "params": {"type": "error", "args": [{"value": "boom"}]},
        },
        {
            "method": "Runtime.consoleAPICalled",
            "params": {"type": "log", "args": [{"value": "ok"}]},
        },
    ]

    assert playtest._errors(events) == ["TypeError: lap is undefined", "boom"]


@pytest.mark.skipif(
    os.environ.get("OUROBOROS_PLAYTEST_LIVE") != "1",
    reason="starts a real headless browser; set OUROBOROS_PLAYTEST_LIVE=1",
)
def test_a_real_browser_plays_a_page(tmp_path) -> None:
    browser = playtest.installed_browser()
    if browser is None:
        pytest.skip("no Chromium-family browser installed")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<button id=b onclick=\"this.textContent='Started'\">Start</button><p id=k></p>"
        "<input id=t placeholder='New task' onkeydown=\"if (event.key === 'Enter') k.textContent = this.value\">"
        "<button onclick=\"this.textContent = confirm('Delete it?') ? 'Gone' : 'Kept'\">Del</button>"
        "<script>addEventListener('keydown', e => k.textContent = e.key);"
        "setTimeout(() => { throw new Error('late failure') }, 10);"
        "console.error('seen as ' + navigator.userAgent.includes('Headless') + navigator.webdriver);"
        "</script>"
    )
    script = parse_play_script(
        _reply(
            [
                {"shot": "before"},
                {"type": "lost"},
                {"click_text": "new task"},
                {"type": "walk"},
                {"press": "Enter"},
                {"shot": "typed"},
                {"click_text": "start"},
                {"key": "ArrowUp", "hold": 50},
                {"click_text": "missing"},
                {"click_text": "del"},
                {"shot": "after"},
                {"wait": 1200},
                {"reload": True},
                {"wait": 1200},
            ]
        ),
        site,
    )
    assert script is not None

    report = playtest.play(site, script, site / "shots", browser)

    assert [p.name for p in report.shots] == ["01-before.png", "02-typed.png", "03-after.png"]
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in report.shots)
    assert any("late failure" in line for line in report.errors)
    assert "seen as falsefalse" in report.errors  # the page cannot tell it from a user's Chrome
    assert report.notes == [
        "the page opened a confirm dialog 'Delete it?'; it was accepted",
        "no text field had focus when the play typed 'lost', so the text went nowhere:"
        " the play's earlier click missed the field",
        "no visible control labelled 'missing'",
    ]


@pytest.mark.skipif(
    os.environ.get("OUROBOROS_PLAYTEST_LIVE") != "1",
    reason="starts a real headless browser; set OUROBOROS_PLAYTEST_LIVE=1",
)
def test_a_real_browser_notes_the_step_during_which_the_page_stopped_drawing(tmp_path) -> None:
    browser = playtest.installed_browser()
    if browser is None:
        pytest.skip("no Chromium-family browser installed")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<canvas id=c></canvas><script>"
        "const x = c.getContext('2d'); let n = 0;"
        "(function f() { x.fillRect(n++ % 300, 0, 2, 2); requestAnimationFrame(f); })();"
        "addEventListener('keydown', (e) => { if (e.key === 'Shift') setInterval(() => {"
        " const t = performance.now(); while (performance.now() - t < 500); }, 0); });"
        "</script>"
    )
    script = parse_play_script(
        _reply([{"wait": 2000}, {"key": "Shift", "hold": 2000}, {"shot": "after"}]), site
    )
    assert script is not None

    report = playtest.play(site, script, site / "shots", browser)

    assert len(report.notes) == 1
    assert report.notes[0].startswith("while step 2 (hold Shift for 2000 ms) ran, the page drew")
    assert "its drawing stalled then" in report.notes[0]


@pytest.mark.skipif(
    os.environ.get("OUROBOROS_PLAYTEST_LIVE") != "1",
    reason="starts a real headless browser; set OUROBOROS_PLAYTEST_LIVE=1",
)
def test_a_real_browser_times_the_first_step_after_the_page_navigates(
    tmp_path, monkeypatch
) -> None:
    browser = playtest.installed_browser()
    if browser is None:
        pytest.skip("no Chromium-family browser installed")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<a href='game.html'>Start</a>")
    (site / "game.html").write_text("<p>Race</p>")
    script = parse_play_script(
        _reply(
            [
                {"wait": 1200},
                {"click_text": "start"},
                {"key": "ArrowUp", "hold": 2000},
                {"reload": True},
                {"wait": 1200},
                {"shot": "after"},
            ]
        ),
        site,
    )
    assert script is not None
    timed: list[tuple[int, float]] = []
    notes = playtest.frame_notes

    def keep(rates: list[tuple[int, dict[str, Any], float]]) -> list[str]:
        timed.extend((number, rate) for number, _, rate in rates)
        return notes(rates)

    monkeypatch.setattr(playtest, "frame_notes", keep)

    report = playtest.play(site, script, site / "shots", browser)

    assert [number for number, _ in timed] == [1, 3, 5]
    assert all(rate > 30 for _, rate in timed)
    assert report.notes == []


@pytest.mark.skipif(
    os.environ.get("OUROBOROS_PLAYTEST_LIVE") != "1",
    reason="starts a real headless browser; set OUROBOROS_PLAYTEST_LIVE=1",
)
@pytest.mark.parametrize(
    "name", ["Type to filter", "Search", "Find a task", "filter", "  search  "]
)
def test_a_real_browser_reaches_a_field_by_its_placeholder_or_any_label(
    tmp_path, name: str
) -> None:
    browser = playtest.installed_browser()
    if browser is None:
        pytest.skip("no Chromium-family browser installed")
    site = tmp_path / "site"
    site.mkdir()
    # The visible <label> sits after the field, so a click on it is not what finds the field.
    (site / "index.html").write_text(
        "<button>Go</button>"
        "<input id=q placeholder='Type to filter' aria-label='Search'>"
        "<p>between</p><label for=q>Find a task</label>"
    )
    script = parse_play_script(
        _reply([{"click_text": name}, {"type": "milk"}, {"shot": "typed"}]), site
    )
    assert script is not None

    report = playtest.play(site, script, site / "shots", browser)

    assert report.notes == []  # the typing reached the field


def _answer_next_request(check_dir: Path, answer: dict) -> threading.Thread:
    """A host that answers the first request left in ``check_dir``."""

    def host() -> None:
        for _ in range(200):
            requests = list(check_dir.glob("request-*")) if check_dir.is_dir() else []
            if requests:
                nonce = requests[0].name.removeprefix("request-")
                requests[0].unlink()
                fd = os.open(check_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    playtest.write_answer(fd, nonce, answer)
                finally:
                    os.close(fd)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=host, daemon=True)
    thread.start()
    return thread


def test_the_replay_command_asks_the_host_to_play_and_lists_its_shots(tmp_path, capsys) -> None:
    check_dir = tmp_path / ".ouroboros" / "playtest" / "check"
    host = _answer_next_request(
        check_dir,
        {"shots": ["check/n/01-a.png"], "errors": ["TypeError: x"], "notes": ["no button"]},
    )

    code = playtest.main([str(check_dir)])

    host.join(5)
    assert code == 0
    out = capsys.readouterr().out
    assert "Screenshots, in order:\n- check/n/01-a.png" in out
    assert "page error: TypeError: x" in out
    assert "note: no button" in out
    assert [p.name for p in check_dir.iterdir()][0].startswith("result-")  # nothing else is left


@pytest.mark.parametrize(
    ("args", "answer", "code", "message"),
    [
        (["a", "b"], None, 2, "usage:"),
        (["{check}"], None, 1, "no play answered"),
        (["{check}"], {"error": "the play did not finish: boom"}, 1, "did not finish: boom"),
    ],
)
def test_the_replay_command_says_why_it_did_not_play(
    tmp_path, monkeypatch, capsys, args, answer, code, message
) -> None:
    check_dir = tmp_path / "check"
    monkeypatch.setattr(playtest, "CHECK_WAIT_SECONDS", 0.3)
    if answer is not None:
        _answer_next_request(check_dir, answer)

    assert playtest.main([a.format(check=check_dir) for a in args]) == code
    assert message in capsys.readouterr().out


def test_a_shot_or_an_answer_replaces_a_planted_symlink_instead_of_writing_through_it(
    tmp_path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    check_dir = tmp_path / "check"
    check_dir.mkdir()
    (check_dir / "01-a.png").symlink_to(outside)
    (check_dir / "result-n.json").symlink_to(outside)

    fd = playtest.open_dir_inside(tmp_path, check_dir)
    try:
        playtest.save_in(fd, "01-a.png", b"png")
        playtest.write_answer(fd, "n", {"shots": []})
    finally:
        os.close(fd)

    assert outside.read_text() == "keep"
    assert (check_dir / "01-a.png").read_bytes() == b"png"
    assert json.loads((check_dir / "result-n.json").read_text()) == {"shots": []}
    assert [p.name for p in check_dir.iterdir() if p.name.startswith(".")] == []


def test_a_host_directory_is_reached_without_following_a_link(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "app"
    root.mkdir()
    (root / "a").symlink_to(outside)

    for sub in (Path("a"), Path("a/b"), Path("../outside"), outside, root / "a" / "b"):
        with pytest.raises(OSError):
            playtest.open_dir_inside(root, sub, create=True)
    fd = playtest.open_dir_inside(root, root / "x" / "y", create=True)
    try:
        playtest.save_in(fd, "f.txt", b"hi")
    finally:
        os.close(fd)

    assert list(outside.iterdir()) == []
    assert (root / "x" / "y" / "f.txt").read_bytes() == b"hi"


def _get(site: Path, path: str) -> tuple[int, bytes, str | None]:
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(playtest._SiteHandler, directory=str(site))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Location")
    finally:
        server.shutdown()
        server.server_close()


def test_the_app_is_served_only_from_its_own_directory(tmp_path) -> None:
    site = tmp_path / "app"
    (site / "js").mkdir(parents=True)
    (site / "index.html").write_text("<p>app</p>")
    (site / "js" / "main.js").write_text("ok()")
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.js").write_text("secret")
    (site / "leak.txt").symlink_to(tmp_path / "secret.txt")
    (site / "linked").symlink_to(tmp_path / "elsewhere")
    os.mkfifo(site / "pipe.js")

    assert _get(site, "/")[:2] == (200, b"<p>app</p>")
    assert _get(site, "/js/main.js?v=1")[:2] == (200, b"ok()")
    assert _get(site, "/js") == (301, b"", "/js/")
    for path in ("/leak.txt", "/linked/x.js", "/../secret.txt", "/js/..", "/js/", "/pipe.js"):
        status, body, _ = _get(site, path)
        assert status == 404 and b"secret" not in body, path


def _pipe_with(incoming: list[dict]) -> Any:
    """A ``_Pipe`` whose browser sends ``incoming`` and records what it was sent."""
    pipe = object.__new__(playtest._Pipe)
    pipe._next_id, pipe.events, pipe.dialogs, pipe._out = 0, [], [], io.BytesIO()
    pipe._message = lambda _deadline: incoming.pop(0)
    return pipe


def test_a_dialog_is_accepted_as_a_user_would_and_noted() -> None:
    pipe = _pipe_with(
        [
            {
                "method": "Page.javascriptDialogOpening",
                "sessionId": "s1",
                "params": {"type": "prompt", "message": "Name?", "defaultPrompt": "x"},
            },
            {"id": 2, "result": {}},  # the answer to the dialog
            {"id": 1, "result": {"ok": True}},
        ]
    )

    assert pipe.call("Input.dispatchMouseEvent", {"type": "mouseReleased"}, "s1") == {"ok": True}

    sent = [json.loads(raw) for raw in pipe._out.getvalue().split(b"\0") if raw]
    assert sent[1] == {
        "id": 2,
        "method": "Page.handleJavaScriptDialog",
        "params": {"accept": True, "promptText": "x"},
        "sessionId": "s1",
    }
    assert pipe.dialogs == ["the page opened a prompt dialog 'Name?'; it was accepted"]
    assert pipe.events == []


class _FakePipe:
    """A browser that answers every call at once, never for ``hang_on``, and for
    ``block_on`` only once it is killed."""

    hang_on = ""
    block_on = ""
    made: list[_FakePipe] = []

    def __init__(self, browser: str, profile: str) -> None:
        self.events: list[dict] = []
        self.dialogs: list[str] = []
        self.killed = threading.Event()
        self.made.append(self)

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        if method == self.hang_on:
            raise TimeoutError("browser did not answer")
        if method == self.block_on:
            self.killed.wait(10)
            raise RuntimeError("browser closed the pipe")
        return {"targetId": "t", "sessionId": "s", "data": base64.b64encode(b"png").decode()}

    def kill(self) -> None:
        self.killed.set()

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("hang_on", "limit", "shots", "note"),
    [
        ("", 0.05, ["01-a.png"], "the play stopped before step 3 of 3: its 0 s ran out"),
        ("Input.dispatchMouseEvent", 60.0, ["01-a.png"], "stopped answering at step 2 (click"),
    ],
)
def test_a_slow_or_hung_page_ends_the_play_with_the_shots_it_took(
    tmp_path, monkeypatch, hang_on, limit, shots, note
) -> None:
    site = _site(tmp_path)
    monkeypatch.setattr(playtest, "_Pipe", type("P", (_FakePipe,), {"hang_on": hang_on}))
    monkeypatch.setattr(playtest, "MAX_PLAY_SECONDS", limit)
    monkeypatch.setattr(playtest, "LOAD_SETTLE_SECONDS", 0.0)
    script = PlayScript(
        page="index.html",
        steps=({"shot": "a"}, {"click": [1, 1]} if hang_on else {"wait": 100}, {"shot": "b"}),
    )

    report = playtest.play(site, script, tmp_path / "shots", "/bin/chrome")

    assert [p.name for p in report.shots] == shots
    assert len(report.notes) == 1 and note in report.notes[0]


def test_the_module_imports_where_the_platform_lacks_posix_open_flags() -> None:
    # Windows has no O_DIRECTORY, O_NOFOLLOW or O_NONBLOCK; nothing plays there, but the
    # completion pass, and so every run, imports this module.
    code = (
        "import os\n"
        "for name in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK'):\n"
        "    delattr(os, name)\n"
        "import ouroboros.orchestrator.completion_pass\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("block_on", ["", "Input.dispatchMouseEvent"])
def test_a_stopped_play_kills_the_browser_and_saves_nothing_after(
    tmp_path, monkeypatch, block_on
) -> None:
    site = _site(tmp_path)
    pipe = type("P", (_FakePipe,), {"block_on": block_on, "made": []})
    monkeypatch.setattr(playtest, "_Pipe", pipe)
    monkeypatch.setattr(playtest, "LOAD_SETTLE_SECONDS", 0.0)
    middle = {"click": [1, 1]} if block_on else {"wait": 5000}
    script = PlayScript(page="index.html", steps=({"shot": "a"}, middle, {"shot": "b"}))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()

    report = playtest.play(site, script, site / "shots", "/bin/chrome", stop)

    assert time.monotonic() - started < 2
    assert [p.name for p in report.shots] == ["01-a.png"]
    assert sorted(p.name for p in (site / "shots").iterdir()) == ["01-a.png"]
    assert pipe.made[0].killed.is_set()
    assert report.notes == [playtest.STOPPED_NOTE]


@pytest.mark.skipif(
    os.environ.get("OUROBOROS_PLAYTEST_LIVE") != "1",
    reason="starts a real headless browser; set OUROBOROS_PLAYTEST_LIVE=1",
)
def test_a_real_browser_stops_when_told(tmp_path) -> None:
    browser = playtest.installed_browser()
    if browser is None:
        pytest.skip("no Chromium-family browser installed")
    site = _site(tmp_path)
    script = PlayScript(page="index.html", steps=({"shot": "a"}, {"wait": 5000}, {"shot": "b"}))
    stop = threading.Event()
    threading.Timer(2.5, stop.set).start()
    started = time.monotonic()

    report = playtest.play(site, script, site / "shots", browser, stop)

    assert time.monotonic() - started < 8
    assert [p.name for p in report.shots] == ["01-a.png"]
    assert report.notes == [playtest.STOPPED_NOTE]
