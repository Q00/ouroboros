"""The structured view of an Auto chat, folded from ``ouroboros auto --progress-jsonl``.

The console starts ``ouroboros auto`` with ``--progress-jsonl <file>`` and
follows that file (:class:`ProgressTail`): each line is one progress event the
pipeline emitted (``ouroboros.auto.progress.AutoProgressEvent``). The events are
folded (:func:`fold`) into ``session["auto"]``, the dict the page draws as a
phase stepper, an automatic interview, a Seed card with its review grade, and
the run card. What the events do not carry (the Seed itself, the run's
execution id, the round limit) is read from the auto session record the
pipeline persists (:func:`load_facts`).

Everything kept here is saved with the chat, so it is bounded: a fixed number
of rounds, phase changes and grades, and a cut on every text. A cut text ends
in ``…``, and the number of rounds dropped from the front is kept.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

# How much one poll reads, and the longest line it accepts. A line longer than
# the read is skipped whole (and counted), never parsed in pieces.
READ_BYTES = 256 * 1024
# A final drain reads at most this many chunks; the file is written by one run.
_FINAL_CHUNKS = 64

MAX_ROUNDS = 60
MAX_PHASES = 40
MAX_GRADES = 20
MAX_FINDINGS = 3
MAX_TEXT = 1500
MAX_MESSAGE = 300
MAX_FINDING = 300
MAX_SEED_ITEMS = 40
MAX_SEED_ITEM = 1000
MAX_SEED_YAML = 64 * 1024

# The shape of the saved view. A chat saved with another version is rebuilt
# from its progress file when the console loads it (:func:`upgrade_view`).
VIEW_VERSION = 2
# What the events do not carry: facts from the auto session record and how
# the job ended. An upgraded view keeps them from the view it replaces.
_NOT_FROM_EVENTS = (
    "max_rounds",
    "max_repair_rounds",
    "findings",
    "execution_id",
    "seed_loaded",
    "outcome",
    "read_error",
)

KINDS = frozenset({"phase", "grade", "repair", "question", "answer"})
_PHASE = re.compile(r"^[a-z_]{1,40}$")
_GRADE = re.compile(r"^[A-Za-z][+-]?$")
# The interview server puts the ambiguity it measured before a question
# (orchestrator/capabilities/question_text.py: ``(ambiguity: 0.43) <question>``).
_AMBIGUITY = re.compile(r"^\(\s*ambiguity\s*:\s*(\d+(?:\.\d+)?|\.\d+)\s*\)\s*")
# Phases whose arrival means the persisted record has something new for the page.
_FACT_PHASES = frozenset(
    {
        "seed_generation",
        "review",
        "repair",
        "run",
        "ralph_handoff",
        "evaluate",
        "unstuck_lateral",
        "complete",
        "blocked",
        "failed",
    }
)


def progress_file(chats_root: Path, session_id: str) -> Path:
    """Where the Auto chat ``session_id`` asks ``ouroboros auto`` to write its events."""
    return chats_root / "auto" / f"{session_id}.jsonl"


def cut(text: str, limit: int) -> str:
    """``text`` when it fits, else its first ``limit - 1`` characters and ``…``."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class ProgressTail:
    """Reads the whole lines appended to a JSON-lines file since the last read.

    The offset only moves past a line once its newline is in, so a line the
    writer is still writing is read on a later poll. Each read is bounded by
    :data:`READ_BYTES`; a single line longer than that is skipped and counted
    in :attr:`skipped`, as is a line that is not a valid event. A read that
    fails leaves the offset where it was and says why in :attr:`error`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.skipped = 0
        self.error: str | None = None  # why the last read failed, if it did
        self._discarding = False  # inside a line too long to read

    def read(self, *, final: bool = False) -> list[dict[str, Any]]:
        """The events in the lines completed since the last read.

        ``final`` is for after the writer exited: it drains the file (in at
        most ``_FINAL_CHUNKS`` reads) and also accepts a last line that has no
        newline, when that line is a whole event.
        """
        events: list[dict[str, Any]] = []
        for _ in range(_FINAL_CHUNKS if final else 1):
            chunk = self._chunk()
            if chunk is None:
                break
            more = len(chunk) == READ_BYTES
            events.extend(self._lines(chunk, final=final and not more))
            if not more:
                break
        return events

    def _chunk(self) -> bytes | None:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(READ_BYTES)
        except FileNotFoundError:
            self.error = None
            return None  # no event yet: the writer creates the file on its first one
        except OSError as exc:
            self.error = cut(str(exc), MAX_MESSAGE)
            return None
        self.error = None
        return chunk or None

    def _lines(self, chunk: bytes, *, final: bool) -> list[dict[str, Any]]:
        end = chunk.rfind(b"\n")
        if end < 0 and len(chunk) == READ_BYTES:
            # One line longer than a whole read: skip it to its newline.
            self.offset += len(chunk)
            if not self._discarding:
                self.skipped += 1
            self._discarding = True
            return []
        if final:
            # The writer is gone: a last line without its newline is read too,
            # and counted as skipped when it is not a whole event.
            body = chunk
        elif end < 0:
            return []  # still being written
        else:
            body = chunk[: end + 1]
        self.offset += len(body)
        events: list[dict[str, Any]] = []
        for index, raw in enumerate(body.split(b"\n")):
            if index == 0 and self._discarding:
                self._discarding = False  # the tail of the over-long line
                continue
            if not raw.strip():
                continue
            event = parse_event(raw)
            if event is None:
                self.skipped += 1
            else:
                events.append(event)
        return events


def _optional(value: object, kind: type) -> bool:
    return value is None or (isinstance(value, kind) and not isinstance(value, bool))


def parse_event(raw: bytes | str) -> dict[str, Any] | None:
    """One progress event as a dict, or ``None`` when the line is not one.

    Every field is checked against the type ``AutoProgressEvent`` gives it; a
    line with a field of another type is refused whole, not converted.
    """
    import json

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    phase = data.get("phase")
    session = data.get("auto_session_id")
    if kind not in KINDS or not isinstance(phase, str) or not _PHASE.match(phase):
        return None
    if not isinstance(session, str) or not session or len(session) > 80:
        return None
    if not isinstance(data.get("message", ""), str):
        return None
    for name in ("question", "answer", "answer_source", "timestamp", "grade"):
        if not _optional(data.get(name), str):
            return None
    if not _optional(data.get("round"), int):
        return None
    grade = data.get("grade")
    if grade is not None and not _GRADE.match(grade):
        return None
    return {
        "auto_session_id": session,
        "phase": phase,
        "kind": kind,
        "message": cut(str(data.get("message") or ""), MAX_MESSAGE),
        "round": data.get("round"),
        "grade": grade,
        "question": data.get("question"),
        "answer": data.get("answer"),
        "answer_source": cut(data["answer_source"], 40) if data.get("answer_source") else None,
        "timestamp": cut(data["timestamp"], 40) if data.get("timestamp") else None,
    }


def new_view() -> dict[str, Any]:
    """The empty view of an Auto chat that has just started."""
    return {
        "v": VIEW_VERSION,
        "auto_session_id": None,
        "phase": None,
        "message": None,
        "phases": [],
        "rounds": [],
        "rounds_dropped": 0,
        "ambiguity_trend": [],
        "max_rounds": None,
        "max_repair_rounds": None,
        "grade": None,
        "grades": [],
        "review_message": None,
        "findings": [],
        "repair_round": 0,
        "execution_id": None,
        "seed_loaded": False,
        "outcome": None,
        "skipped": 0,
        "read_error": None,
    }


def _round_entry(view: dict[str, Any], number: int) -> dict[str, Any] | None:
    """The round ``number`` of the view, added in order when new; ``None`` once dropped."""
    rounds: list[dict[str, Any]] = view["rounds"]
    for entry in reversed(rounds):
        if entry["round"] == number:
            return entry
    if view["rounds_dropped"] and rounds and number < rounds[0]["round"]:
        return None  # older than what the view keeps
    entry = {
        "round": number,
        "question": None,
        "answer": None,
        "answer_source": None,
        "ambiguity": None,
    }
    rounds.append(entry)
    rounds.sort(key=lambda item: item["round"])
    while len(rounds) > MAX_ROUNDS:
        rounds.pop(0)
        view["rounds_dropped"] += 1
    return entry if entry in rounds else None


def split_ambiguity(text: str) -> tuple[str, float | None]:
    """A question without its ``(ambiguity: 0.43)`` prefix, and that value.

    Only a number from 0 to 1 in exactly that shape is taken, and only when a
    question follows it; anything else comes back unchanged, with ``None``.
    """
    match = _AMBIGUITY.match(text)
    if match is None:
        return text, None
    value = float(match.group(1))
    rest = text[match.end() :]
    if not 0.0 <= value <= 1.0 or not rest.strip():
        return text, None
    return rest, value


def _record_ambiguity(view: dict[str, Any], entry: dict[str, Any], value: float) -> None:
    """Keep ``value`` on its round and in the view's trend, one point per round."""
    entry["ambiguity"] = value
    trend: list[dict[str, Any]] = view.setdefault("ambiguity_trend", [])
    for point in trend:
        if point["round"] == entry["round"]:
            point["value"] = value
            return
    trend.append({"round": entry["round"], "value": value})
    trend.sort(key=lambda point: point["round"])
    del trend[:-MAX_ROUNDS]


def fold(view: dict[str, Any], event: dict[str, Any]) -> bool:
    """Apply one event to ``view``; ``True`` when the persisted record is worth reading now.

    Rounds are paired by number: a question and the answer to it land on the
    same entry whichever arrives first, and an answer that repeats its
    question fills a question that was never seen. A question's
    ``(ambiguity: …)`` prefix is kept as the round's ``ambiguity`` and in
    ``ambiguity_trend``, not in its text.
    """
    reload = False
    if view["auto_session_id"] != event["auto_session_id"]:
        view["auto_session_id"] = event["auto_session_id"]
        reload = True  # the round limit and anything already persisted
    phase = event["phase"]
    if phase != view["phase"]:
        view["phase"] = phase
        view["phases"].append({"phase": phase, "at": event["timestamp"]})
        del view["phases"][:-MAX_PHASES]
        reload = reload or phase in _FACT_PHASES
    view["message"] = event["message"] or view["message"]
    kind = event["kind"]
    number = event["round"]
    if kind in ("question", "answer") and number is not None and number > 0:
        entry = _round_entry(view, number)
        if entry is not None:
            if event["question"]:
                question, ambiguity = split_ambiguity(event["question"].strip())
                if kind == "question" or not entry["question"]:
                    entry["question"] = cut(question, MAX_TEXT)
                if ambiguity is not None and (kind == "question" or entry.get("ambiguity") is None):
                    _record_ambiguity(view, entry, ambiguity)
            if kind == "answer":
                entry["answer"] = cut((event["answer"] or "").strip(), MAX_TEXT) or None
                entry["answer_source"] = event["answer_source"]
    elif kind == "grade" and event["grade"]:
        view["grade"] = event["grade"]
        view["grades"].append(
            {
                "grade": event["grade"],
                "repair_round": view["repair_round"],
                "at": event["timestamp"],
            }
        )
        del view["grades"][:-MAX_GRADES]
        view["review_message"] = event["message"] or None
        reload = True
    elif kind == "repair" and number is not None and number > view["repair_round"]:
        view["repair_round"] = number
        view["review_message"] = event["message"] or None
        reload = True
    elif kind == "phase" and phase in ("review", "repair"):
        view["review_message"] = event["message"] or None
    return reload


def upgrade_view(old: dict[str, Any], progress: Path) -> dict[str, Any] | None:
    """The current shape of a view saved by an older console, losing none of it.

    The old rounds are always migrated in place first (each question's
    ``(ambiguity: …)`` prefix becomes its ``ambiguity``), so every saved round,
    phase, grade and fact is kept. The chat's progress file is then folded
    again, and that replay is merged in only when it covers the saved history:
    every saved phase and grade in order, every saved round with at least what
    it had, and no fewer repairs or dropped rounds. A file that is empty,
    unreadable, truncated or rotated cannot cover it, so the migrated view is
    the answer. A merged replay only adds: a value the replay lacks is kept
    from the saved view.

    ``None`` when ``old`` is not a view this console can read safely (a newer
    version, or entries of the wrong shape): the caller keeps it as saved.
    """
    version = old.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version >= VIEW_VERSION:
        return None
    if not _well_formed(old):
        return None
    migrated = _migrate_rounds(old)
    if not progress.is_file():
        return migrated
    replay = new_view()
    tail = ProgressTail(progress)
    for event in tail.read(final=True):
        fold(replay, event)
    replay["skipped"] = tail.skipped
    if not _replay_covers(migrated, replay):
        return migrated
    merged = dict(migrated)
    for key, value in replay.items():
        if key in _NOT_FROM_EVENTS or key in ("rounds", "ambiguity_trend"):
            continue  # facts the events never carry stand; the two lists merge below
        if value is None or value == [] or (key != "skipped" and value == 0):
            continue  # nothing to add: keep what was saved
        merged[key] = value
    # Round by round, the replay adds to what was saved and never blanks it.
    saved_rounds = {entry["round"]: entry for entry in migrated["rounds"]}
    merged["rounds"] = [
        {
            **saved_rounds.get(entry["round"], {}),
            **{key: value for key, value in entry.items() if value is not None},
        }
        for entry in replay["rounds"]
    ]
    points = {point["round"]: point for point in migrated["ambiguity_trend"]}
    points.update({point["round"]: point for point in replay["ambiguity_trend"]})
    merged["ambiguity_trend"] = [points[number] for number in sorted(points)][-MAX_ROUNDS:]
    return merged


def _well_formed(old: dict[str, Any]) -> bool:
    """Whether the lists a migration and a replay compare are the shapes they expect."""
    for name, field_name, kind in (
        ("rounds", "round", int),
        ("phases", "phase", str),
        ("grades", "grade", str),
    ):
        entries = old.get(name, [])
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            value = entry.get(field_name)
            if not isinstance(value, kind) or isinstance(value, bool):
                return False
    return True


def _migrate_rounds(old: dict[str, Any]) -> dict[str, Any]:
    """``old`` in the current shape, with each round's prefix split into its ``ambiguity``."""
    view = {**new_view(), **old, "v": VIEW_VERSION, "ambiguity_trend": []}
    rounds = [dict(entry) for entry in old.get("rounds") or []]
    view["rounds"] = rounds
    for entry in rounds:
        kept = entry.get("ambiguity")
        entry["ambiguity"] = None
        ambiguity = kept if isinstance(kept, float) else None
        if isinstance(entry.get("question"), str):
            question, found = split_ambiguity(entry["question"])
            entry["question"] = question
            ambiguity = found if found is not None else ambiguity
        if ambiguity is not None:
            _record_ambiguity(view, entry, ambiguity)
    return view


def _replay_covers(saved: dict[str, Any], replay: dict[str, Any]) -> bool:
    """Whether a replayed view holds at least everything ``saved`` holds from events."""
    if saved.get("auto_session_id") and replay["auto_session_id"] != saved["auto_session_id"]:
        return False  # another session's file, or none of it
    for name, field_name in (("phases", "phase"), ("grades", "grade")):
        kept = [entry[field_name] for entry in saved.get(name) or []]
        again = [entry[field_name] for entry in replay[name]]
        if again[: len(kept)] != kept:
            return False
    for name in ("repair_round", "rounds_dropped"):
        if replay[name] < int(saved.get(name) or 0):
            return False
    by_round = {entry["round"]: entry for entry in replay["rounds"]}
    for entry in saved.get("rounds") or []:
        again = by_round.get(entry["round"])
        if again is None:
            return False
        if any(
            entry.get(key) and not again.get(key) for key in ("question", "answer", "answer_source")
        ):
            return False
    return True


def item_text(item: Any) -> str:
    """A spec item as one sentence (criteria may be strings or structured dicts)."""
    if isinstance(item, dict):
        for key in ("description", "text", "criterion", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item)


def seed_fields(seed: dict[str, Any], seed_path: str | None) -> dict[str, Any]:
    """The ``seed`` message fields for a Seed dict, bounded like the rest of the view."""
    import yaml

    goal = seed.get("goal")
    criteria = [item_text(c) for c in seed.get("acceptance_criteria") or []]
    constraints = [item_text(c) for c in seed.get("constraints") or []]
    text = yaml.dump(seed, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return {
        "text": cut(str(goal or ""), MAX_TEXT),
        "acceptance_criteria": [cut(c, MAX_SEED_ITEM) for c in criteria[:MAX_SEED_ITEMS]],
        "acceptance_total": len(criteria),
        "constraints": [cut(c, MAX_SEED_ITEM) for c in constraints[:MAX_SEED_ITEMS]],
        # Too long to keep in the chat: the page offers the file instead.
        "seed_yaml": text if len(text) <= MAX_SEED_YAML else None,
        "seed_path": seed_path,
        "source": "auto",
    }


def load_facts(auto_session_id: str, store: Any = None) -> dict[str, Any] | None:
    """What the persisted auto session record adds to the events, or ``None`` if unreadable.

    ``store`` is an ``AutoStore`` (the default one, under ``~/.ouroboros/data``,
    is where ``ouroboros auto`` saves). The record is written by an atomic
    rename, so a read never sees half of it.
    """
    if store is None:
        from ouroboros.auto.state import AutoStore

        store = AutoStore()
    try:
        state = store.load(auto_session_id)
    except (ValueError, OSError):
        return None
    findings = [
        cut(str(item.get("message")), MAX_FINDING)
        for item in (state.findings or [])
        if isinstance(item, dict) and item.get("message")
    ]
    return {
        "max_rounds": state.max_interview_rounds,
        "max_repair_rounds": state.max_repair_rounds,
        "execution_id": state.execution_id,
        "job_id": state.job_id,
        "seed": dict(state.seed_artifact) if state.seed_artifact else None,
        "seed_path": state.seed_path,
        "grade": state.last_grade,
        "findings": findings[:MAX_FINDINGS],
    }


def apply_facts(view: dict[str, Any], facts: dict[str, Any]) -> None:
    """Copy the record's facts into ``view`` (the Seed itself becomes a message)."""
    for key in ("max_rounds", "max_repair_rounds"):
        if isinstance(facts.get(key), int):
            view[key] = facts[key]
    if facts.get("execution_id"):
        view["execution_id"] = facts["execution_id"]
    if facts.get("grade") and not view["grade"]:
        view["grade"] = facts["grade"]
    view["findings"] = list(facts.get("findings") or [])


__all__ = [
    "ProgressTail",
    "apply_facts",
    "fold",
    "item_text",
    "load_facts",
    "new_view",
    "parse_event",
    "progress_file",
    "seed_fields",
    "split_ambiguity",
    "upgrade_view",
]
