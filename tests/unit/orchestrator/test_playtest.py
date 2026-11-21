"""Playing a built browser app in a headless browser: the script bounds and the driver."""

from __future__ import annotations

import json
import os
from pathlib import Path

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
        "<script>addEventListener('keydown', e => k.textContent = e.key);"
        "setTimeout(() => { throw new Error('late failure') }, 10);</script>"
    )
    script = parse_play_script(
        _reply(
            [
                {"shot": "before"},
                {"click_text": "start"},
                {"key": "ArrowUp", "hold": 50},
                {"click_text": "missing"},
                {"shot": "after"},
            ]
        ),
        site,
    )
    assert script is not None

    report = playtest.play(site, script, tmp_path / "shots", browser)

    assert [p.name for p in report.shots] == ["01-before.png", "02-after.png"]
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in report.shots)
    assert any("late failure" in line for line in report.errors)
    assert report.notes == ["no visible control labelled 'missing'"]


def test_the_replay_command_plays_a_saved_script_and_lists_its_shots(
    tmp_path, monkeypatch, capsys
) -> None:
    site = _site(tmp_path)
    script_file = site / "script.json"
    script_file.write_text(json.dumps({"page": "index.html", "steps": [{"shot": "a"}]}))
    old = site / "check" / "01-stale.png"
    old.parent.mkdir()
    old.write_bytes(b"old")
    calls = []

    def fake_play(where, script, out_dir, browser):
        calls.append((where, script.page, out_dir, browser))
        shot = out_dir / "01-a.png"
        shot.write_bytes(b"png")
        return playtest.PlayReport(shots=[shot], errors=["TypeError: x"])

    monkeypatch.setattr(playtest, "installed_browser", lambda: "/bin/chrome")
    monkeypatch.setattr(playtest, "play", fake_play)

    code = playtest.main([str(site), str(script_file), "check"])

    assert code == 0
    assert calls == [(site, "index.html", site / "check", "/bin/chrome")]
    assert not old.exists()  # shots from an earlier check never pass for new ones
    out = capsys.readouterr().out
    assert f"- {site / 'check' / '01-a.png'}" in out
    assert "page error: TypeError: x" in out


@pytest.mark.parametrize(
    ("args", "browser", "code", "message"),
    [
        (["only-one"], "/bin/chrome", 2, "usage:"),
        (["{site}", "{site}/missing.json", "check"], "/bin/chrome", 2, "cannot read"),
        (["{site}", "{site}/bad.json", "check"], "/bin/chrome", 2, "not valid"),
        (["{site}", "{site}/good.json", "check"], None, 1, "no Chromium-family browser"),
    ],
)
def test_the_replay_command_says_why_it_did_not_play(
    tmp_path, monkeypatch, capsys, args, browser, code, message
) -> None:
    site = _site(tmp_path)
    (site / "bad.json").write_text('{"page": "../x.html", "steps": [{"shot": "a"}]}')
    (site / "good.json").write_text('{"page": "index.html", "steps": [{"shot": "a"}]}')
    monkeypatch.setattr(playtest, "installed_browser", lambda: browser)

    assert playtest.main([a.format(site=site) for a in args]) == code
    assert message in capsys.readouterr().out
