"""An Auto run's card shows the completion pass that runs after its criteria pass.

The pass (``orchestrator/completion_pass.py``) plays the app in a browser,
fixes what it found and plays it again, announcing each stage as
``execution.completion_pass.updated``. The card used to stop following the run
at its terminal row, which comes before the pass, so a chat showed nothing for
the 10 to 25 minutes the pass took. These tests drive the console's own
follower against a real EventStore, read the stages and screenshots the way the
card does, and fetch a screenshot through the console's API.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

import pytest

from ouroboros.dashboard_web.reader import default_db_path
from ouroboros.events.base import BaseEvent
from ouroboros.gui import completion_view, harness, jobs
from ouroboros.gui.server import serve_background
from ouroboros.persistence.event_store import EventStore

EXECUTION = "exec_9a1b2c3d4e5f"
SESSION = "orch_5f4e3d2c1b0a"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WAIT_SECONDS = 15.0

# The stages of one real pass (a galaxy app: 16 problems found, 3 left after the fix).
REAL_PASS = (
    {"stage": "review"},
    {"stage": "play_plan"},
    {"stage": "play"},
    {"stage": "play_review"},
    {"stage": "fix", "defects": 16},
    {"stage": "recheck"},
    {"stage": "play_2"},
    {"stage": "play_review_2"},
    {"stage": "repair", "defects": 3},
    {"stage": "play_3"},
    {"stage": "pick_2"},
    {"stage": "pick_2_swapped"},
    {"stage": "pick_1"},
    {"stage": "done", "status": "fixed"},
)


def _stage(data: dict[str, Any], at: float | None = None) -> dict[str, Any]:
    return {"defects": None, "status": None, "at": at, **data}


def _shots(cwd: Path, round_no: int, *names: str) -> Path:
    folder = cwd / ".ouroboros" / "playtest" / f"round-{round_no}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(PNG)
    return folder


def test_the_mirrored_names_match_the_pass_and_the_play() -> None:
    from ouroboros.orchestrator import completion_pass, playtest

    assert completion_view.STAGE_EVENT == completion_pass.STAGE_EVENT
    assert completion_view.PLAYTEST_DIR == completion_pass.PLAYTEST_DIR
    assert completion_view.MAX_SHOTS == playtest.MAX_SHOTS


def test_a_pass_is_told_as_steps_with_the_stage_it_is_on(tmp_path: Path) -> None:
    stages = [_stage(s) for s in REAL_PASS]
    assert completion_view.summary([], str(tmp_path)) is None

    half = completion_view.summary(stages[:5], str(tmp_path))
    assert half is not None
    assert (half["stage"], half["round"], half["defects"], half["done"], half["status"]) == (
        "fix",
        None,
        16,
        False,
        None,
    )
    assert half["steps"] == [
        {"kind": "review"},
        {"kind": "play", "round": 1},
        {"kind": "fix", "defects": 16},
    ]

    looking = completion_view.summary(stages[:8], str(tmp_path))
    assert looking is not None
    assert (looking["stage"], looking["round"]) == ("play_review_2", 2)

    whole = completion_view.summary(stages, str(tmp_path))
    assert whole is not None
    assert (whole["stage"], whole["done"], whole["status"], whole["defects"]) == (
        "done",
        True,
        "fixed",
        3,
    )
    # Three pick stages are one step: picking the version to keep.
    assert whole["steps"] == [
        {"kind": "review"},
        {"kind": "play", "round": 1},
        {"kind": "fix", "defects": 16},
        {"kind": "play", "round": 2},
        {"kind": "repair", "defects": 3},
        {"kind": "play", "round": 3},
        {"kind": "pick"},
    ]
    assert whole["shots"] is None  # no play saved a screenshot in this folder


def test_the_latest_play_with_fresh_screenshots_is_shown(tmp_path: Path) -> None:
    now = time.time()
    one = _shots(tmp_path, 1, "01-after-opening.png", "02-after-dragging-to-rotate.png")
    stages = [_stage({"stage": "play"}, now - 60)]
    assert completion_view.shots(str(tmp_path), stages) == {
        "round": 1,
        "names": ["01-after-opening.png", "02-after-dragging-to-rotate.png"],
    }

    # Round 2 is announced; until it saves a screenshot, round 1's stay on the card.
    stages.append(_stage({"stage": "play_2"}, now))
    two = _shots(tmp_path, 2)
    # A round-2 file from an earlier Auto run in this folder is older than this play.
    (two / "01-after-opening.png").write_bytes(PNG)
    os.utime(two / "01-after-opening.png", (now - 3600, now - 3600))
    assert completion_view.shots(str(tmp_path), stages)["round"] == 1  # type: ignore[index]

    (two / "02-after-zooming.png").write_bytes(PNG)
    (two / "notes.txt").write_text("not a shot")
    (two / "03-link.png").symlink_to(one / "01-after-opening.png")
    assert completion_view.shots(str(tmp_path), stages) == {
        "round": 2,
        "names": ["02-after-zooming.png"],
    }

    many = _shots(tmp_path, 3, *(f"{n:02d}-step.png" for n in range(1, 12)))
    assert many.is_dir()
    stages.append(_stage({"stage": "play_3"}, now))
    shown = completion_view.shots(str(tmp_path), stages)
    assert shown is not None
    assert len(shown["names"]) == completion_view.MAX_SHOTS


def test_only_a_play_screenshot_inside_the_folder_is_read(tmp_path: Path) -> None:
    folder = _shots(tmp_path, 1, "01-after-opening.png")
    assert completion_view.read_shot(str(tmp_path), "round-1", "01-after-opening.png") == PNG

    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    (folder / "02-link.png").symlink_to(outside)
    (folder / "03-not-png.png").write_bytes(b"GIF89a")
    for round_dir, name in (
        ("round-1", "02-link.png"),  # a symlink in the round folder
        ("round-1", "03-not-png.png"),  # not a PNG
        ("round-1", "../../../secret.png"),
        ("round-0", "01-after-opening.png"),
        ("check", "01-after-opening.png"),
        ("round-1", "01-after-opening.txt"),
        ("round-1", "missing.png"),
    ):
        with pytest.raises(OSError):
            completion_view.read_shot(str(tmp_path), round_dir, name)

    # A round folder that is a symlink is refused too.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "01-after-opening.png").write_bytes(PNG)
    (folder.parent / "round-2").symlink_to(elsewhere)
    with pytest.raises(OSError):
        completion_view.read_shot(str(tmp_path), "round-2", "01-after-opening.png")


def test_a_screenshot_larger_than_the_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shots(tmp_path, 1, "01-after-opening.png")
    monkeypatch.setattr(completion_view, "MAX_SHOT_BYTES", len(PNG) - 1)
    with pytest.raises(OSError):
        completion_view.read_shot(str(tmp_path), "round-1", "01-after-opening.png")


def _get(server: Any, path: str, *, token: bool = True) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    conn.request("GET", path, headers={"X-Ouroboros-Token": server.token} if token else {})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, response.getheader("Content-Type") or "", body


def test_the_console_serves_a_chats_play_screenshot_and_nothing_else(tmp_path: Path) -> None:
    _shots(tmp_path, 1, "01-after-opening.png")
    (tmp_path / "app.js").write_text("secret")
    server, _thread = serve_background(port=0)
    try:
        server.state.harness._sessions["c1"] = {"id": "c1", "cwd": str(tmp_path), "messages": []}
        base = "/api/chat/sessions/c1/playtest"
        assert _get(server, f"{base}/round-1/01-after-opening.png") == (200, "image/png", PNG)
        assert _get(server, f"{base}/round-1/01-after-opening.png", token=False)[0] == 401
        assert (
            _get(server, "/api/chat/sessions/nope/playtest/round-1/01-after-opening.png")[0] == 404
        )
        assert _get(server, f"{base}/round-1/02-missing.png")[0] == 404
        for path in (
            f"{base}/round-1/../../../app.js",
            f"{base}/round-1/%2e%2e%2fapp.js",
            f"{base}/round-1/app.js",
            f"{base}/check/01-after-opening.png",
        ):
            status, ctype, _ = _get(server, path)
            assert status == 404, path
            assert ctype.startswith("application/json"), path
    finally:
        server.shutdown()
        server.server_close()


def test_the_stage_tail_reads_only_its_executions_stage_rows(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE events (id TEXT, aggregate_type TEXT, aggregate_id TEXT, "
        "event_type TEXT, payload TEXT, timestamp TEXT)"
    )
    rows = [
        ("execution", EXECUTION, completion_view.STAGE_EVENT, {"stage": "review"}),
        ("execution", "exec_other", completion_view.STAGE_EVENT, {"stage": "fix", "defects": 9}),
        ("execution", EXECUTION, "execution.terminal", {"stage": "fix"}),
        ("execution", EXECUTION, completion_view.STAGE_EVENT, {"stage": "Bad stage"}),
        ("execution", EXECUTION, completion_view.STAGE_EVENT, {"stage": "fix", "defects": True}),
        ("execution", EXECUTION, completion_view.STAGE_EVENT, {"stage": "done", "status": "odd"}),
    ]
    for index, (agg_type, agg_id, event_type, data) in enumerate(rows):
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            [str(index), agg_type, agg_id, event_type, json.dumps(data), "2026-11-23 04:02:20.9"],
        )
    conn.commit()
    tail = completion_view.StageTail(db, EXECUTION)
    read = tail.read()
    assert [(s["stage"], s["defects"], s["status"]) for s in read] == [
        ("review", None, None),
        ("fix", None, None),  # a boolean is not a count
        ("done", None, None),  # an unknown status is not said
    ]
    assert read[0]["at"] == pytest.approx(1795406540.9)
    assert tail.read() == []
    conn.execute(
        "INSERT INTO events VALUES ('9', 'execution', ?, ?, ?, '2026-11-23 04:03:00')",
        [EXECUTION, completion_view.STAGE_EVENT, json.dumps({"stage": "play"})],
    )
    conn.commit()
    conn.close()
    assert [s["stage"] for s in tail.read()] == ["play"]
    assert completion_view.StageTail(tmp_path / "missing.db", EXECUTION).read() == []


class _AutoChat:
    """One Auto chat whose run card the console's real follower keeps up to date."""

    def __init__(self, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(harness, "_RUN_POLL_SEC", 0.02)
        self.h = harness.Harness(jobs.JobRegistry())
        self.job = jobs.Job(id="j-auto", kind="auto", title="t", argv=["ouroboros", "auto"])
        self.job.status = "running"
        self.h._jobs._jobs[self.job.id] = self.job
        self.h._sessions["c1"] = {
            "id": "c1",
            "title": "galaxy",
            "cwd": str(cwd),
            "mode": "auto",
            "status": "running",
            "version": 0,
            "messages": [
                {
                    "id": "r1",
                    "role": "assistant",
                    "kind": "run",
                    "text": "",
                    "ts": 0,
                    "execution_id": EXECUTION,
                    "run_status": "running",
                    "progress": None,
                }
            ],
        }
        self.thread = threading.Thread(
            target=self.h._follow_run,
            args=("c1", EXECUTION, None),
            kwargs={"auto_job_id": self.job.id},
            daemon=True,
        )

    def card(self) -> dict[str, Any]:
        session = self.h.get("c1") or {}
        return next(m for m in session["messages"] if m["kind"] == "run")

    async def shows(self, check: Any, what: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if check(self.card()):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"the card never showed {what}: {self.card()}")


def _pass(card: dict[str, Any]) -> dict[str, Any]:
    return (card.get("progress") or {}).get("completion") or {}


@pytest.mark.asyncio
async def test_an_auto_card_follows_the_pass_after_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _AutoChat(tmp_path, monkeypatch)
    store = EventStore(f"sqlite+aiosqlite:///{default_db_path()}")
    await store.initialize()

    async def stage(**data: Any) -> None:
        await store.append(
            BaseEvent(
                type=completion_view.STAGE_EVENT,
                aggregate_type="execution",
                aggregate_id=EXECUTION,
                data={"bound_seconds": 1800.0, "attempt": 1, **data},
            )
        )

    try:
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={"execution_id": EXECUTION, "seed_goal": "A 3D galaxy"},
            )
        )
        chat.thread.start()
        await store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={"summary": {"success_count": 1}},
            )
        )
        await chat.shows(lambda card: card["run_status"] == "completed", "the run completed")
        assert not _pass(chat.card())

        # The run has ended; its job goes on with the pass, and the card follows it.
        await stage(stage="review")
        await stage(stage="play")
        _shots(tmp_path, 1, "01-after-opening.png", "02-after-dragging-to-rotate.png")
        await stage(stage="play_review")
        await stage(stage="fix", defects=16)
        await chat.shows(
            lambda card: (
                _pass(card).get("stage") == "fix"
                and (_pass(card).get("shots") or {}).get("names")
                == ["01-after-opening.png", "02-after-dragging-to-rotate.png"]
            ),
            "the fix with round 1's screenshots",
        )
        shown = _pass(chat.card())
        assert shown["defects"] == 16
        assert shown["steps"][-1] == {"kind": "fix", "defects": 16}
        assert chat.thread.is_alive()

        await stage(stage="done", status="fixed")
        await chat.shows(
            lambda card: _pass(card).get("done") and _pass(card).get("status") == "fixed",
            "the pass done",
        )
        assert chat.card()["run_status"] == "completed"
    finally:
        chat.job.status = "succeeded"
        chat.thread.join(timeout=WAIT_SECONDS)
        await store.close()
    assert not chat.thread.is_alive()


@pytest.mark.asyncio
async def test_a_chat_run_that_is_not_auto_still_stops_at_its_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _AutoChat(tmp_path, monkeypatch)
    chat.thread = threading.Thread(
        target=chat.h._follow_run, args=("c1", EXECUTION, None), daemon=True
    )
    store = EventStore(f"sqlite+aiosqlite:///{default_db_path()}")
    await store.initialize()
    try:
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={"execution_id": EXECUTION, "seed_goal": "A 3D galaxy"},
            )
        )
        await store.append(
            BaseEvent(
                type=completion_view.STAGE_EVENT,
                aggregate_type="execution",
                aggregate_id=EXECUTION,
                data={"stage": "review", "bound_seconds": 1800.0},
            )
        )
        await store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={"summary": {"success_count": 1}},
            )
        )
        chat.thread.start()
        chat.thread.join(timeout=WAIT_SECONDS)
        assert not chat.thread.is_alive()
        assert chat.card()["run_status"] == "completed"
        assert "completion" not in (chat.card().get("progress") or {})
    finally:
        await store.close()
