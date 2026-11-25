"""An Auto run's completion pass, as the console's run card shows it.

After a run's criteria pass, its job goes on with the completion pass
(:mod:`ouroboros.orchestrator.completion_pass`): it checks the code against the
request, plays the app in a browser, fixes what it found, plays it again and
keeps the version a first-time user would rather get. That takes minutes, and
the run already reads "completed" when it starts, so the card showed nothing
of it and the chat looked stalled.

The pass announces each stage on the execution's own stream as
``execution.completion_pass.updated``: ``stage``, ``bound_seconds``, the number
of ``defects`` a fix or repair was given, and ``status`` on the last stage,
``done``. The run board's tail does not read that event family, so
:class:`StageTail` reads it on its own, read-only and by the execution id.
:func:`summary` folds the stages into the small dict the card draws.
:func:`shots` lists the screenshots of the latest play, which the play saved in
the chat's folder under ``.ouroboros/playtest/round-<n>/``, and
:func:`read_shot` opens one of them for the console's API, and nothing else.

Everything kept is bounded: a fixed number of milestones and screenshot names,
and every stage, status and file name must match a strict pattern.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any

from ouroboros.orchestrator.playtest import open_dir_inside

# Mirrors ``completion_pass.STAGE_EVENT`` and ``completion_pass.PLAYTEST_DIR``
# (a test pins both); importing that module would load the whole pass.
STAGE_EVENT = "execution.completion_pass.updated"
PLAYTEST_DIR = Path(".ouroboros") / "playtest"

# One read takes at most this many stage rows. A pass announces about sixteen.
READ_LIMIT = 64
MAX_STEPS = 16
# ``playtest.MAX_SHOTS``: a play saves at most eight screenshots.
MAX_SHOTS = 8
MAX_SHOT_BYTES = 8 * 1024 * 1024
MAX_DEFECTS = 999
# A shot older than its play's announcement (less this slack) is from an
# earlier Auto run in the same folder: a play does not clear its round folder.
STALE_SLACK_SECONDS = 2.0

_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PLAY = re.compile(r"^play(?:_([2-9]))?$")
_LOOK = re.compile(r"^play_review(?:_([2-9]))?$")
_PICK = re.compile(r"^pick_[1-9](?:_swapped)?$")
ROUND_DIR = re.compile(r"^round-([1-9])$")
# ``playtest``: ``f"{n:02d}-{name}.png"``, the name cut to ``[A-Za-z0-9_-]{1,32}``.
SHOT_NAME = re.compile(r"^(\d{2})-([A-Za-z0-9_-]{1,32})\.png$")
STATUSES = frozenset({"clean", "fixed", "fix_failed", "review_failed"})
_PNG = b"\x89PNG\r\n\x1a\n"


class StageTail:
    """Reads the stage rows of one execution appended since the last read.

    The store is opened read-only for each read, so a run that has not
    written a stage yet, or a store that cannot be read right now, gives no
    rows and is read again on the next poll.
    """

    def __init__(self, db_path: str | Path, execution_id: str) -> None:
        self.db_path = Path(db_path)
        self.execution_id = execution_id
        self.cursor = 0

    def read(self) -> list[dict[str, Any]]:
        """``{"stage", "defects", "status", "at"}`` for each new stage row, oldest first."""
        try:
            conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "SELECT rowid, payload, timestamp FROM events "
                "WHERE aggregate_id = ? AND aggregate_type = 'execution' "
                "AND event_type = ? AND rowid > ? ORDER BY rowid LIMIT ?",
                [self.execution_id, STAGE_EVENT, self.cursor, READ_LIMIT],
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        stages: list[dict[str, Any]] = []
        for rowid, payload, timestamp in rows:
            self.cursor = max(self.cursor, int(rowid))
            stage = _stage_row(payload, timestamp)
            if stage is not None:
                stages.append(stage)
        return stages


def _stage_row(payload: object, timestamp: object) -> dict[str, Any] | None:
    try:
        data = json.loads(payload) if isinstance(payload, str | bytes) else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    stage = data.get("stage")
    if not isinstance(stage, str) or not _STAGE.match(stage):
        return None
    defects = data.get("defects")
    status = data.get("status")
    return {
        "stage": stage,
        "defects": (
            min(defects, MAX_DEFECTS)
            if isinstance(defects, int) and not isinstance(defects, bool) and defects >= 0
            else None
        ),
        "status": status if isinstance(status, str) and status in STATUSES else None,
        "at": _epoch(timestamp),
    }


def _epoch(timestamp: object) -> float | None:
    """Seconds since the epoch of a stored UTC timestamp (``2026-11-23 04:04:57.129168``)."""
    from datetime import UTC, datetime

    if not isinstance(timestamp, str):
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:  # SQLite keeps naive UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def play_round(stage: str) -> int | None:
    """The round a play stage plays (``play`` → 1, ``play_2`` → 2), else ``None``."""
    match = _PLAY.match(stage)
    if not match:
        return None
    return int(match.group(1) or 1)


def _milestone(stage: dict[str, Any]) -> dict[str, Any] | None:
    """The step a stage starts on the card's line, or ``None`` when it continues one."""
    name = stage["stage"]
    round_no = play_round(name)
    if round_no is not None:
        return {"kind": "play", "round": round_no}
    if name in ("fix", "repair"):
        return {"kind": name, "defects": stage["defects"]}
    if name == "review":
        return {"kind": "review"}
    if _PICK.match(name):
        return {"kind": "pick"}
    return None  # play_plan, play_review*, recheck, done: part of the step before


def summary(stages: list[dict[str, Any]], cwd: str | None) -> dict[str, Any] | None:
    """What the card shows of the pass: the stage it is on, its steps so far and the latest shots.

    ``None`` before the first stage. ``stage`` is the last stage announced and
    ``round`` the play it is about (for a play and the look at it).
    ``defects`` is what the last fix or repair was given, and ``status`` is
    set once the pass is ``done``.
    """
    if not stages:
        return None
    steps: list[dict[str, Any]] = []
    for stage in stages:
        step = _milestone(stage)
        if step is None or (step["kind"] == "pick" and steps and steps[-1]["kind"] == "pick"):
            continue
        steps.append(step)
    last = stages[-1]
    look = _LOOK.match(last["stage"])
    round_no = play_round(last["stage"]) or (int(look.group(1) or 1) if look else None)
    defects = next(
        (s["defects"] for s in reversed(stages) if s["stage"] in ("fix", "repair")), None
    )
    done = last["stage"] == "done"
    return {
        "stage": last["stage"],
        "round": round_no,
        "defects": defects,
        "status": last["status"] if done else None,
        "done": done,
        "steps": steps[-MAX_STEPS:],
        "shots": shots(cwd, stages) if cwd else None,
    }


def shots(cwd: str, stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The latest play's screenshots in ``cwd``: ``{"round", "names"}``, or ``None``.

    The latest round with a screenshot saved since its play was announced;
    the names in the order the play took them, at most :data:`MAX_SHOTS`.
    """
    plays: dict[int, float | None] = {}
    for stage in stages:
        round_no = play_round(stage["stage"])
        if round_no is not None:
            plays[round_no] = stage["at"]
    for round_no in sorted(plays, reverse=True):
        names = _round_names(Path(cwd), round_no, plays[round_no])
        if names:
            return {"round": round_no, "names": names}
    return None


def _round_names(cwd: Path, round_no: int, since: float | None) -> list[str]:
    try:
        fd = open_dir_inside(cwd, PLAYTEST_DIR / f"round-{round_no}")
    except OSError:
        return []
    try:
        names: list[str] = []
        for name in sorted(os.listdir(fd)):
            if not SHOT_NAME.match(name):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if since is not None and info.st_mtime < since - STALE_SLACK_SECONDS:
                continue
            names.append(name)
        return names[:MAX_SHOTS]
    except OSError:
        return []
    finally:
        os.close(fd)


def read_shot(cwd: str, round_dir: str, name: str) -> bytes:
    """The bytes of one play screenshot in ``cwd``; :class:`OSError` for anything else.

    ``round_dir`` must be ``round-<n>`` and ``name`` a play's screenshot name.
    The folder is reached without following a symlink, the file must be a
    regular PNG file no larger than :data:`MAX_SHOT_BYTES`, and a symlink in
    its place is refused.
    """
    if not ROUND_DIR.match(round_dir) or not SHOT_NAME.match(name):
        raise OSError(errno.ENOENT, "not a play screenshot", f"{round_dir}/{name}")
    dir_fd = open_dir_inside(Path(cwd), PLAYTEST_DIR / round_dir)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SHOT_BYTES:
            raise OSError(errno.EINVAL, "not a play screenshot", name)
        data = handle.read(MAX_SHOT_BYTES + 1)
    if len(data) > MAX_SHOT_BYTES or not data.startswith(_PNG):
        raise OSError(errno.EINVAL, "not a play screenshot", name)
    return data


__all__ = [
    "MAX_SHOTS",
    "STAGE_EVENT",
    "StageTail",
    "play_round",
    "read_shot",
    "shots",
    "summary",
]
