"""Review and complete the product of a run whose Seed carries expected experience.

An ``ooo auto`` Seed for new work carries research on what a typical current
product of its kind includes (``SourcedScope.expected_experience``). The run's
one worker attempt is told to build to that list, but it is one attempt, and
the formal evaluation judges only the Seed's criteria. A product that runs but
misses capabilities users expect, or that breaks its own basic use while it
adds them, was therefore approved as it stood.

After such a run completes, before the formal evaluation, so the evaluation
judges the finished product:

1. a reviewer reads the code (``Read``/``Glob``/``Grep``, permission mode
   ``default``) and lists concrete defects: first anything in the request's
   basic use that does not work for a first-time user, then each expected line
   the code does not do yet;
2. when the app runs in a web browser from a static page and a Chromium-family
   browser is installed, it is played: a read-only reviewer reads the code and
   writes a short play script for a first-time user, the script runs in that
   browser headless (:mod:`ouroboros.orchestrator.playtest`), and a read-only
   reviewer looks at the screenshots and lists what that user sees broken.
   Code reading misses a view that never shows the road ahead, a banner that
   never clears, or a galaxy that is never drawn. The play is about half a
   minute, so its shots are named for what the user did, and an outcome a
   real user needs longer to reach is not a defect: a review that asked for
   one had a fix count three laps in a second and a repair end the race at
   once. An app that keeps what the user makes is reloaded once near the end,
   and the look counts text a drag left selected and what a reload lost: both
   cost Auto blind comparisons the play had not looked for. It also counts text
   too small or faint to read at a glance and a control the user needs that
   shows only under the pointer: blind judges named these on most Auto to-do
   apps that worked, and the play had not looked for them either. The first look
   also names, at most three, what makes the screen look unfinished next to a
   released product of its kind (a main view that leaves much of the window
   empty, a sparse scene, panels that crowd the view), each marked
   ``unfinished:``: blind judges held these against Auto products that worked,
   and neither look had named them. Only the first three marked lines of a look
   reach a worker. The second look does not ask for them. The play also notes rules a stylesheet's browser
   dropped and controls left outside a window the page cannot scroll: a
   bracket left open made one page drop every rule after it, and two workers
   told only that its controls were not visible added rules the browser
   dropped too;
3. a worker with the run's tools and permission mode fixes what steps 1 and 2
   listed, basic use first. It gets the screenshots and, when it has a shell,
   a command that has the script played again so it can look at the page after
   a change. A worker may run in a sandbox that can start neither a browser nor
   a server, so the command only leaves a request in the app's directory; the
   pass, on the host, plays the saved script and answers there while the worker
   runs (:func:`serve_checks`);
4. a second read-only code review lists what now stops the app from loading or
   its basic use from working (a fix that rewrites a file can break what
   worked), and the script is played and looked at once more. This look also
   opens the first play's screenshots and lists what looks worse than in them:
   fixes that rewrote a file had dropped the arrow keys, or flattened a scene
   the first play showed, and a look at the new screenshots alone found neither.
   For the same reason the script uses every control the code takes for one
   action, and the fix is told to keep what a file it rewrites had;
5. a worker repairs what step 4 found, once, and the script is played again;
6. the versions played are compared to keep the best
   (:mod:`ouroboros.orchestrator.app_versions` keeps their files): starting from
   the version played last, each version played before it, the one just before
   it first, is compared with the best so far. A read-only reviewer sees both
   plays' screenshots and says which a first-time user would rather get, then
   again with the two named the other way round; when both answers prefer the
   earlier version, it becomes the best so far. The best is written back at the
   end. A fix had broken the shaders of a galaxy the first play showed, and a
   repair raised another's particle count until it was one white slab: both
   stayed, as nothing compared them with what came before. Later a fix that
   rewrote a kart game's renderer drew less than the first build, and it stayed
   when the repair after it looked worse again, as the first build was compared
   only with the repair.

A parent session that delegated the run (``_ooo_parent_*`` arguments) bounds
both sessions as it bounded the run: its permission mode is the worker's, and
neither session gets a tool outside its effective tool set. When that set has
no tool that writes, nothing runs.

The pass is best effort. A Seed without expected experience, a run that did not
complete, a backend that dispatches through a host plugin, or a session that
fails or hangs leaves the product as the run left it. A session that ends in a
provider error (a model at capacity, say) is tried once more after a short wait;
a hung session is not. The pass never changes
the run verdict; it adds a ``completion_pass`` record to the run result.

The pass runs after ``execution.terminal``, while the run job is still open. A
waiter that trusts the durable terminal over an open job after a short window
(direct ``ooo auto``, #1593) would end the process and the pass with it, so each
stage is announced in the execution stream as ``execution.completion_pass.updated``
with the most it can take (``bound_seconds``); stage ``done`` ends the pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import contextlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import sys
import threading
from typing import Any

import structlog
import yaml

from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import app_versions
from ouroboros.orchestrator import playtest as playtest_module
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
)
from ouroboros.orchestrator.playtest import (
    MAX_SHOTS,
    REQUEST_PREFIX,
    PlayReport,
    PlayScript,
    open_dir_inside,
    parse_play_script,
    save_in,
    write_answer,
)

log = structlog.get_logger(__name__)

REVIEWER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
REVIEWER_PERMISSION_MODE = "default"
WORKER_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
WRITING_TOOLS = frozenset({"Write", "Edit", "Bash"})
# Hang guard for one session: a runtime that never returns. Not a time budget.
SESSION_TIMEOUT_SECONDS = 1800.0
# One more try for a session that ended in an error, after this wait.
SESSION_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 30.0
MAX_DEFECTS = 40
MAX_DEFECT_CHARS = 400
# How a look marks what makes a working screen look unfinished, and how many of
# those lines reach a worker: they are presentation, not breakage.
UNFINISHED_MARK = "unfinished:"
MAX_UNFINISHED = 3
NO_DEFECTS = "NONE"
STAGE_EVENT = "execution.completion_pass.updated"
# The play script itself: at most 60 s of waits and holds, plus page loads and
# browser start-up. Its own protocol calls time out well inside this.
PLAY_BOUND_SECONDS = 180.0
# After the bound, the play is told to stop: its browser is killed and it cleans
# up within seconds. The pass waits at most this long for that before going on.
PLAY_STOP_GRACE_SECONDS = 30.0
PLAYTEST_DIR = Path(".ouroboros") / "playtest"
CHECK_DIR = PLAYTEST_DIR / "check"
# Plays one fix or repair session may ask for; each is a bounded browser run on the host.
MAX_CHECKS = 8
CHECK_POLL_SECONDS = 0.5
_NONCE = re.compile(r"[0-9a-f]{32}")

REVIEW_PROMPT = """You review an app an AI built for this request: {goal}

Research on current products of this kind found that users typically expect:
{expected}

Read the code paths that run, as a strict reviewer. Do not modify any file. List concrete defects, one per line starting with "- ", naming the file:
1. First, anything in the request's basic use that does not work for a first-time user (for example, something they add that they then cannot see, input that should be rejected but is not, an action that does nothing, an error on load).
2. Then each expected line above that the code does not do yet, or does only in part.
Reply with the list only, or the single word {none}."""

FIX_PROMPT = """This project was built for this request: {goal}

A reviewer read the code and found these defects:
{defects}

Fix every defect above, the ones about the request's basic use first.{seen} Keep everything that already works: before you rewrite a whole file, note every control, screen and feature it has, and check that the new file still has each. Do not add a server, network service, account, payment or personal-data feature unless the request asks for it. When you are done, check that every element, file, function and asset the running code uses exists, and run the project's tests if it has any."""

PLAY_PLAN_PROMPT = """An AI built an app for this request: {goal}

Read the code paths that run. Do not modify any file. If the app runs in a web browser from a static page in this directory, with no build step and no server code of its own, write a short script that plays it the way a first-time user would for about half a minute: open it, start it, and use its main features with the controls the code really listens to. When the code takes more than one control for the same action (arrow keys and letter keys, say), use each of them in some step: a later change can drop one, and users press either. Act as that user does: click a text field before typing into it, with click_text naming its placeholder or label, since a click by position guessed from the code can miss the field and send the typing nowhere; after typing into a one-line field, submit it by pressing Enter, as most people do, not by clicking a button. If the app keeps what the user makes or changes, reload the page once near the end and take a screenshot after it, as a user who comes back later would. Take a screenshot after each meaningful moment, at most {max_shots}, and name each for what the user has just done (for example "after-typing-a-task" or "after-holding-up-5s"), not for what the app should show by then: half a minute rarely reaches the end of anything that takes a real user longer.

Reply with one JSON object only: {{"page": "<page path in this directory>", "steps": [...]}}. Each step is one of: {{"wait": ms}}, {{"shot": "name"}}, {{"key": "ArrowUp", "hold": ms}}, {{"keys": ["ArrowUp", "ArrowLeft"], "hold": ms}}, {{"press": "Enter"}}, {{"type": "text"}}, {{"click_text": "visible label or a field's placeholder"}}, {{"click": [x, y]}}, {{"drag": [x0, y0, x1, y1, ms]}}, {{"wheel": [x, y, dy]}}, {{"reload": true}}. The window is 1280x800. A step waits or holds at most 5000 ms. Keys: the arrow keys, Enter, Escape, Space, Shift, Tab, Backspace or one character.
If the app does not run that way, reply with the single word {none}."""

PLAY_LOOK_PROMPT = """An AI built an app for this request: {goal}

The app was opened in a 1280x800 browser and played the way a first-time user would, with these steps:
{steps}
The screenshots taken at the screenshot steps, in order: {shots}
{errors}
Open and look at every screenshot. Do not modify any file. List what that user sees that is broken, wrong or confusing, most serious first, one per line starting with "- ", naming the file to change when you can: a main view that does not show what the user needs to act, something that does not respond, text or overlays that stay, overlap or are cut off, text too small or too faint to read at a glance, a control the user needs that shows only while the pointer is over it, text a drag or click left selected, what the user made that a reload lost, blank or garbled areas, error messages. Do not list missing features. The play lasted about half a minute, and a screenshot's name says what the user had just done, not what the app must show: that something a real user needs longer to reach did not happen in it is not a defect, and nothing should happen faster than it would for a real user. A step the notes say missed its target tells nothing about the app.{finish}{before}
Reply with the list only, or the single word {none}."""

# The second look only: the same steps were played before the fix, and a fix that
# rewrote a file had dropped arrow keys or flattened a scene that the first play showed.
PLAY_LOOK_BEFORE = """ The same steps were played before the last change, with these screenshots, in the same order: {shots}. Open them too, and also list what looks worse now than in them: a control that responded and no longer does, a view or scene drawn less fully, something that showed and is gone. Compare what is drawn, not counts or claims the page writes about itself."""

# The first look only: what judges held against Auto products that worked,
# where the fix that follows can still be played and looked at again.
PLAY_LOOK_FINISH = """ Then, at most three, what makes the screen look unfinished next to a released product of its kind, the most noticeable first, each on a line starting with "- unfinished: ": a main view that fills only part of the window and leaves the rest empty, a scene or page that looks sparse or like a placeholder, panels or text that crowd what the user watches."""

RECHECK_PROMPT = """A worker just changed an app an AI built for this request: {goal}

Read the code paths that run, as a strict reviewer. Do not modify any file. List only what stops the app from loading or its basic use from working for a first-time user, one per line starting with "- ", naming the file: an error on load or in the main loop, an element, file, function, asset or module the running code uses that does not exist or is not loaded, a control that does nothing. Do not list missing features.
Reply with the list only, or the single word {none}."""


# Which of two played versions a user would rather get. Asked in both orders;
# an earlier version is put back only when both answers prefer it.
PICK_PROMPT = """An AI built an app for this request: {goal}

Two versions of the app were each opened in a 1280x800 browser and played with the same steps:
{steps}
Version 1, its screenshots in order: {first}
{first_notes}
Version 2, its screenshots in order: {second}
{second_notes}
Open every screenshot. Do not modify any file. Which version would a first-time user rather get? Judge what the screenshots show: the main view drawn fully and clearly, controls that visibly respond, nothing blank, washed out, broken, overlapping or cut off. Compare what is drawn, not counts or claims the page writes about itself.
Reply with 1 or 2 only, or 0 when neither is clearly better."""


def _expected_seed(seed_content: str) -> Seed | None:
    """The Seed when it carries expected experience, else ``None``."""
    try:
        parsed = yaml.safe_load(seed_content)
        seed = Seed.from_dict(parsed) if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - an unreadable Seed has nothing to complete against
        return None
    scope = seed.sourced_scope if seed is not None else None
    if scope is None or not scope.expected_experience:
        return None
    return seed


def _delegated_bounds(
    run_arguments: Mapping[str, Any] | None,
) -> tuple[str | None, frozenset[str] | None]:
    """The permission mode and tool set a delegating parent session allowed the run."""
    arguments = run_arguments or {}
    mode = arguments.get(DELEGATED_PARENT_PERMISSION_MODE_ARG)
    tools = arguments.get(DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG)
    allowed = (
        frozenset(t for t in tools if isinstance(t, str) and t) if isinstance(tools, list) else None
    )
    return (mode if isinstance(mode, str) and mode else None), (allowed or None)


def parse_defects(reply: str) -> tuple[str, ...]:
    """The reviewer's ``- `` lines, bounded; none for ``NONE`` or no list."""
    defects = [
        line.strip()[2:].strip()[:MAX_DEFECT_CHARS]
        for line in reply.splitlines()
        if line.strip().startswith("- ") and line.strip()[2:].strip()
    ]
    return tuple(defects[:MAX_DEFECTS])


def bound_unfinished(defects: tuple[str, ...]) -> tuple[str, ...]:
    """``defects`` with only the first :data:`MAX_UNFINISHED` lines marked unfinished."""
    kept: list[str] = []
    marked = 0
    for defect in defects:
        if defect.lower().startswith(UNFINISHED_MARK):
            marked += 1
            if marked > MAX_UNFINISHED:
                continue
        kept.append(defect)
    return tuple(kept)


def build_review_prompt(seed: Seed) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    expected = "\n".join(f"- {line}" for line in scope.expected_experience)
    return REVIEW_PROMPT.format(goal=scope.goal or seed.goal, expected=expected, none=NO_DEFECTS)


def build_recheck_prompt(seed: Seed) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    return RECHECK_PROMPT.format(goal=scope.goal or seed.goal, none=NO_DEFECTS)


def build_play_plan_prompt(seed: Seed) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    return PLAY_PLAN_PROMPT.format(
        goal=scope.goal or seed.goal, max_shots=MAX_SHOTS, none=NO_DEFECTS
    )


def build_play_look_prompt(
    seed: Seed,
    script: PlayScript,
    report: PlayReport,
    working_dir: Path,
    round_no: int = 1,
    before: tuple[str, ...] = (),
) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    shots = ", ".join(path.relative_to(working_dir).as_posix() for path in report.shots)
    lines = [*report.errors, *report.notes]
    errors = (
        "The page logged or met these problems:\n" + "\n".join(f"- {line}" for line in lines)
        if lines
        else "The page logged no errors."
    )
    return PLAY_LOOK_PROMPT.format(
        goal=scope.goal or seed.goal,
        steps=script.describe(),
        shots=shots,
        errors=errors,
        finish=PLAY_LOOK_FINISH if round_no == 1 else "",
        before=PLAY_LOOK_BEFORE.format(shots=", ".join(before)) if before else "",
        none=NO_DEFECTS,
    )


def _play_notes(report: PlayReport) -> str:
    lines = [*report.errors, *report.notes]
    if not lines:
        return "Its page logged no errors."
    return "Its page logged or met these problems:\n" + "\n".join(f"- {line}" for line in lines)


def build_pick_prompt(
    seed: Seed,
    script: PlayScript,
    first: tuple[tuple[str, ...], PlayReport],
    second: tuple[tuple[str, ...], PlayReport],
) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    return PICK_PROMPT.format(
        goal=scope.goal or seed.goal,
        steps=script.describe(),
        first=", ".join(first[0]),
        first_notes=_play_notes(first[1]),
        second=", ".join(second[0]),
        second_notes=_play_notes(second[1]),
    )


def parse_pick(reply: str | None) -> int:
    """The version a pick preferred, 1 or 2; 0 for neither or no answer."""
    found = re.search(r"\b([012])\b", reply or "")
    return int(found.group(1)) if found else 0


def build_fix_prompt(
    seed: Seed, defects: tuple[str, ...], shots: tuple[str, ...] = (), check: str | None = None
) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    listed = "\n".join(f"- {defect}" for defect in defects)
    seen = ""
    if shots:
        seen = (
            " Some defects were seen on the screen while the app was played in a browser;"
            f" the screenshots are {', '.join(shots)}. Open them to see what the user saw."
        )
    if check:
        seen += (
            f" To see the page after your changes, run `{check}` in this directory: it has the"
            " same steps played in a headless browser, waits for them (up to three minutes) and"
            " prints the screenshots that play saved. Open them, and keep changing and checking"
            " until they no longer show those defects."
        )
    if seen:
        seen += (
            " That browser is an ordinary Chrome with WebGL, and what it shows is what users see:"
            " fix the app itself, and never make the page detect that browser or behave"
            " differently in it."
        )
    return FIX_PROMPT.format(goal=scope.goal or seed.goal, defects=listed, seen=seen)


def check_command(check_dir: Path = CHECK_DIR) -> str:
    """The command a worker runs in the app's directory to have the play script replayed."""
    return (
        f"{shlex.quote(sys.executable)} -m ouroboros.orchestrator.playtest {check_dir.as_posix()}"
    )


Player = Callable[[Path, PlayScript, Path, str, threading.Event], PlayReport]


async def _bounded_play(
    player: Player, working_dir: Path, script: PlayScript, out: Path, browser: str
) -> PlayReport:
    """One play, for at most :data:`PLAY_BOUND_SECONDS`.

    At the bound, or when the caller is cancelled, the play is told to stop, and
    this returns or raises only once it has: its browser is gone and it writes
    nothing more.
    """
    stop = threading.Event()
    running = asyncio.ensure_future(
        asyncio.to_thread(player, working_dir, script, out, browser, stop)
    )
    try:
        return await asyncio.wait_for(asyncio.shield(running), timeout=PLAY_BOUND_SECONDS)
    except BaseException:
        stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(running), timeout=PLAY_STOP_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - the play's own outcome no longer matters
            if not running.done():
                log.warning("execution.completion_pass.play_not_stopped", out=str(out))
        raise


def _make_dir(working_dir: Path, sub: Path) -> None:
    """Make ``sub`` under ``working_dir``; a symlink on the way raises :class:`OSError`."""
    os.close(open_dir_inside(working_dir, sub, create=True))


async def serve_checks(
    working_dir: Path,
    script: PlayScript,
    browser: str,
    player: Player,
    served: list[str],
    *,
    limit: int = MAX_CHECKS,
    poll_seconds: float = CHECK_POLL_SECONDS,
) -> None:
    """Answer the replay requests a worker leaves in :data:`CHECK_DIR`, until cancelled.

    Only ``script``, the one the pass saved, is played; a request carries nothing
    but its name. Each play saves its shots in a new directory named after the
    request. The check directory is reached from ``working_dir`` without
    following a symlink, and every read, removal and write in it goes through that
    one descriptor, so a link the worker plants never leads a host write outside
    the app. After ``limit`` plays a request is answered with an error.
    """
    played = 0
    while True:
        try:
            check_fd: int | None = open_dir_inside(working_dir, CHECK_DIR)
        except OSError:
            check_fd = None
        try:
            names = sorted(os.listdir(check_fd)) if check_fd is not None else []
            for name in names:
                if check_fd is None or not name.startswith(REQUEST_PREFIX):
                    continue
                nonce = name[len(REQUEST_PREFIX) :]
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=check_fd)
                if not _NONCE.fullmatch(nonce):
                    continue
                answer: dict[str, Any]
                if played >= limit:
                    answer = {"error": f"the page was already played {limit} times in this session"}
                else:
                    played += 1
                    served.append(nonce)
                    try:
                        os.mkdir(nonce, 0o755, dir_fd=check_fd)
                        report = await _bounded_play(
                            player, working_dir, script, working_dir / CHECK_DIR / nonce, browser
                        )
                        answer = {
                            "shots": [p.relative_to(working_dir).as_posix() for p in report.shots],
                            "errors": list(report.errors),
                            "notes": list(report.notes),
                        }
                    except Exception as exc:  # noqa: BLE001 - reported to the worker that asked
                        answer = {"error": f"the play did not finish: {exc or type(exc).__name__}"}
                try:
                    write_answer(check_fd, nonce, answer)
                except OSError as exc:
                    log.warning("execution.completion_pass.check_not_answered", error=str(exc))
        finally:
            if check_fd is not None:
                os.close(check_fd)
        await asyncio.sleep(poll_seconds)


async def _close(runtime: Any) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None and inspect.iscoroutinefunction(closer):
        await closer()


async def _session(
    factory: Callable[..., Any],
    prompt: str,
    *,
    tools: tuple[str, ...],
    timeout_seconds: float,
    **runtime_kwargs: Any,
) -> tuple[str | None, bool]:
    """Run one session: its final message (``None`` when it did not finish) and
    whether the failure was an error worth one more try."""
    from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

    try:
        runtime = await asyncio.to_thread(factory, **runtime_kwargs)
    except Exception as exc:  # noqa: BLE001 - best effort: no runtime, no pass
        log.warning("execution.completion_pass.runtime_failed", error=str(exc))
        return None, False
    try:
        if preflight_agent_runtime(runtime) is not None:
            return None, False
        result = await asyncio.wait_for(
            runtime.execute_task_to_result(prompt, tools=list(tools)),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        log.warning("execution.completion_pass.session_hung", timeout_seconds=timeout_seconds)
        return None, False
    except Exception as exc:  # noqa: BLE001 - a failed session ends the pass
        log.warning("execution.completion_pass.session_failed", error=str(exc))
        return None, True
    finally:
        await _close(runtime)
    if result.is_err:
        log.warning("execution.completion_pass.session_failed", error=str(result.error))
        return None, True
    return result.value.final_message or "", False


async def _stage(
    event_store: Any,
    execution_id: str | None,
    stage: str,
    factory: Callable[..., Any],
    prompt: str,
    *,
    tools: tuple[str, ...],
    timeout_seconds: float,
    data: Mapping[str, Any],
    **runtime_kwargs: Any,
) -> str | None:
    """Announce and run one stage's session, once more after an error."""
    for attempt in range(1, SESSION_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        await _announce(event_store, execution_id, stage, timeout_seconds, attempt=attempt, **data)
        reply, retryable = await _session(
            factory, prompt, tools=tools, timeout_seconds=timeout_seconds, **runtime_kwargs
        )
        if reply is not None or not retryable:
            return reply
    return None


async def _announce(
    event_store: Any, execution_id: str | None, stage: str, bound_seconds: float, **data: Any
) -> None:
    """Record a stage and the most it can take; best effort."""
    if event_store is None or not execution_id:
        return
    event = BaseEvent(
        type=STAGE_EVENT,
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={"stage": stage, "bound_seconds": bound_seconds, **data},
    )
    try:
        await event_store.append(event)
    except Exception as exc:  # noqa: BLE001 - the pass itself does not depend on the record
        log.warning("execution.completion_pass.announce_failed", stage=stage, error=str(exc))


_PLAYTEST_TEXT = {
    "clean": " Playing it in a browser showed nothing broken.",
    "found": " Playing it in a browser showed {n} visible problem(s).",
    "no_browser": " It was not played: no Chromium-family browser was found.",
    "not_planned": " It was not played: it does not run from a static page.",
    "plan_failed": " It was not played: the play script was not written.",
    "play_failed": " Playing it in a browser did not finish.",
    "look_failed": " The review of the screenshots did not finish.",
}
_RECHECK_TEXT = {
    "clean": " A second code review found nothing broken.",
    "found": " A second code review found {n} broken part(s).",
    "recheck_failed": " The second code review did not finish.",
}
_REPLAY_TEXT = {
    "clean": " Played again, it showed nothing broken.",
    "found": " Played again, it still showed {n} problem(s).",
    "play_failed": " Playing it again did not finish.",
    "look_failed": " The review of the second play did not finish.",
}
_REPAIR_TEXT = {
    "repaired": " A worker repaired them.",
    "repair_failed": " The repair did not finish.",
}


def _with_record(run_result: MCPToolResult, record: dict[str, Any]) -> MCPToolResult:
    status = record["status"]
    if status == "review_failed":
        text = "review_failed; the product is as the run left it."
    else:
        n = record["defects"]
        text = f"a code review found {n} defect(s)." if n else "a code review found no defects."
        if "playtest" in record:
            visible = len(record.get("visible_defects", ()))
            text += _PLAYTEST_TEXT[record["playtest"]].format(n=visible)
        if status == "fixed":
            text += " A worker fixed them."
        elif status == "fix_failed":
            text += " The fix did not finish."
        if "recheck" in record:
            text += _RECHECK_TEXT[record["recheck"]].format(n=len(record.get("regressions", ())))
        if "replay_2" in record:
            still = len(record.get("replay_2_defects", ()))
            text += _REPLAY_TEXT[record["replay_2"]].format(n=still)
        if "repair" in record:
            text += _REPAIR_TEXT[record["repair"]]
        if record.get("replay_3") == "played":
            text += " It was played once more."
        elif record.get("replay_3") == "play_failed":
            text += " Playing it once more did not finish."
        if record.get("put_back") == "failed":
            text += " An earlier version looked better, but it could not be put back."
        elif record.get("kept_round", 0) < record.get("last_round", 0):
            text += (
                f" The version played in round {record['kept_round']} looked better than the"
                " last one, so it was put back."
            )
        if record.get("checks"):
            text += f" While they worked, the page was played {record['checks']} more time(s)."
    text = f"\nCompletion pass: {text}\n"
    return MCPToolResult(
        content=(*run_result.content, MCPContentItem(type=ContentType.TEXT, text=text)),
        is_error=run_result.is_error,
        meta={**run_result.meta, "completion_pass": record},
        structured_content=run_result.structured_content,
    )


async def complete_after_run(
    run_result: MCPToolResult,
    *,
    seed_content: str,
    working_dir: Path,
    runtime_backend: str | None,
    opencode_mode: str | None = None,
    llm_backend: str | None = None,
    event_store: Any = None,
    execution_id: str | None = None,
    run_arguments: Mapping[str, Any] | None = None,
    runtime_factory: Callable[..., Any] | None = None,
    timeout_seconds: float = SESSION_TIMEOUT_SECONDS,
    browser_finder: Callable[[], str | None] | None = None,
    player: Player | None = None,
) -> MCPToolResult:
    """Review the finished product against its expected experience and fix what is missing."""
    if run_result.is_error or run_result.meta.get("status") != "completed":
        return run_result
    seed = _expected_seed(seed_content)
    if seed is None or not runtime_backend:
        return run_result
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if should_dispatch_via_plugin(runtime_backend, opencode_mode):
        return run_result
    parent_mode, parent_tools = _delegated_bounds(run_arguments)
    reviewer_tools = tuple(t for t in REVIEWER_TOOLS if parent_tools is None or t in parent_tools)
    worker_tools = tuple(t for t in WORKER_TOOLS if parent_tools is None or t in parent_tools)
    if not WRITING_TOOLS.intersection(worker_tools):
        return run_result
    from ouroboros.config.loader import resolve_execution_model
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    factory = runtime_factory or create_agent_runtime

    async def review(stage: str, prompt: str) -> str | None:
        return await _stage(
            event_store,
            execution_id,
            stage,
            factory,
            prompt,
            tools=reviewer_tools,
            timeout_seconds=timeout_seconds,
            data={},
            backend=runtime_backend,
            model=None,
            permission_mode=REVIEWER_PERMISSION_MODE,
            cwd=working_dir,
        )

    async def fix(stage: str, defects: tuple[str, ...], shots: tuple[str, ...] = ()) -> str | None:
        server = None
        if check is not None and script is not None and browser is not None:
            if "Bash" in worker_tools:
                server = asyncio.create_task(
                    serve_checks(
                        working_dir, script, browser, player or playtest_module.play, checks
                    )
                )
        try:
            return await _stage(
                event_store,
                execution_id,
                stage,
                factory,
                build_fix_prompt(seed, defects, shots, check if server is not None else None),
                tools=worker_tools,
                timeout_seconds=timeout_seconds,
                data={"defects": len(defects)},
                backend=runtime_backend,
                model=resolve_execution_model(runtime_backend),
                cwd=working_dir,
                llm_backend=llm_backend,
                startup_output_timeout_seconds=0,
                stdout_idle_timeout_seconds=0,
                **({"permission_mode": parent_mode} if parent_mode else {}),
            )
        finally:
            if server is not None:
                server.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server

    # Each round's shots and report, by round; a round that did not play is absent.
    played: dict[int, tuple[tuple[str, ...], PlayReport]] = {}

    async def play_round(script: PlayScript, browser: str, round_no: int) -> PlayReport | None:
        """One play of ``script``, its shots in ``round-<round_no>``; ``None`` if it failed."""
        stage = "play" if round_no == 1 else f"play_{round_no}"
        await _announce(event_store, execution_id, stage, PLAY_BOUND_SECONDS, attempt=1)
        out = working_dir / PLAYTEST_DIR / f"round-{round_no}"
        try:
            _make_dir(working_dir, out)
            report = await _bounded_play(
                player or playtest_module.play, working_dir, script, out, browser
            )
        except Exception as exc:  # noqa: BLE001 - a play that fails leaves the product as it is
            log.warning(
                "execution.completion_pass.play_failed", error=str(exc) or type(exc).__name__
            )
            return None
        if not report.shots:
            return None
        played[round_no] = (
            tuple(path.relative_to(working_dir).as_posix() for path in report.shots),
            report,
        )
        return report

    async def play_and_look(
        script: PlayScript, browser: str, round_no: int, before: tuple[str, ...] = ()
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """One play of ``script``: its status, the visible defects and the shots."""
        report = await play_round(script, browser, round_no)
        if report is None:
            return "play_failed", (), ()
        look = await review(
            "play_review" if round_no == 1 else f"play_review_{round_no}",
            build_play_look_prompt(seed, script, report, working_dir, round_no, before),
        )
        if look is None:
            return "look_failed", (), ()
        visible = bound_unfinished(parse_defects(look))
        return ("found" if visible else "clean"), visible, played[round_no][0]

    async def prefers_earlier(script: PlayScript, earlier: int, latest: int) -> bool:
        """Whether a pick in each order prefers round ``earlier``'s version to ``latest``'s."""
        first = await review(
            f"pick_{earlier}", build_pick_prompt(seed, script, played[latest], played[earlier])
        )
        if parse_pick(first) != 2:
            return False
        second = await review(
            f"pick_{earlier}_swapped",
            build_pick_prompt(seed, script, played[earlier], played[latest]),
        )
        return parse_pick(second) == 1

    async def keep_version() -> app_versions.AppVersion | None:
        try:
            return await asyncio.to_thread(app_versions.keep, working_dir)
        except OSError as exc:
            log.warning("execution.completion_pass.version_not_kept", error=str(exc))
            return None

    check: str | None = None
    checks: list[str] = []
    script: PlayScript | None = None
    browser: str | None = None
    reply = await review("review", build_review_prompt(seed))
    defects = parse_defects(reply) if reply is not None else ()
    record: dict[str, Any] = {"status": "review_failed", "defects": 0}
    if reply is not None:
        log.info("execution.completion_pass.reviewed", defects=len(defects), cwd=str(working_dir))
        record = {"status": "clean", "defects": 0}
        if defects:
            record["defect_list"] = list(defects)
            record["defects"] = len(defects)
        # Play the app before the fix, so the fix also gets what the screen shows.
        visible: tuple[str, ...] = ()
        shots: tuple[str, ...] = ()
        browser = (browser_finder or playtest_module.find_browser)()
        if browser is None:
            record["playtest"] = "no_browser"
        else:
            plan = await review("play_plan", build_play_plan_prompt(seed))
            script = parse_play_script(plan, working_dir) if plan is not None else None
            if script is None:
                record["playtest"] = "plan_failed" if plan is None else "not_planned"
            else:
                try:
                    playtest_fd = open_dir_inside(working_dir, PLAYTEST_DIR, create=True)
                    try:
                        data = json.dumps({"page": script.page, "steps": list(script.steps)})
                        save_in(playtest_fd, "script.json", data.encode())
                    finally:
                        os.close(playtest_fd)
                    check = check_command()
                except OSError as exc:
                    log.warning("execution.completion_pass.script_not_saved", error=str(exc))
                record["playtest"], visible, shots = await play_and_look(script, browser, 1)
        record["visible_defects"] = list(visible)
        found = (*defects, *visible)[:MAX_DEFECTS]
        if found:
            # Each played version is kept, so a later one that looks worse can be undone.
            versions: dict[int, app_versions.AppVersion | None] = {}
            if 1 in played:
                versions[1] = await keep_version()
            fixed = await fix("fix", found, shots)
            record["status"] = "fixed" if fixed is not None else "fix_failed"
            # A worker that rewrote a file, or stopped part-way, can break what worked,
            # and a worker cannot be sure what the page shows now.
            recheck = await review("recheck", build_recheck_prompt(seed))
            regressions = parse_defects(recheck) if recheck is not None else ()
            record["recheck"] = (
                "recheck_failed" if recheck is None else ("found" if regressions else "clean")
            )
            record["regressions"] = list(regressions)
            still: tuple[str, ...] = ()
            if script is not None and browser is not None:
                status, still, shots = await play_and_look(script, browser, 2, shots)
                record["replay_2"] = status
                record["replay_2_defects"] = list(still)
            broken = (*regressions, *still)[:MAX_DEFECTS]
            latest = 2 if 2 in played else None
            if broken:
                if latest is not None:
                    versions[2] = await keep_version()
                repaired = await fix("repair", broken, shots if still else ())
                record["repair"] = "repaired" if repaired is not None else "repair_failed"
                if latest is not None and script is not None and browser is not None:
                    # Nothing looked at the app after the repair; a repair that raised a
                    # galaxy's particle count until it was one white slab stayed.
                    latest = 3 if await play_round(script, browser, 3) is not None else None
                    record["replay_3"] = "played" if latest is not None else "play_failed"
            if latest is not None and script is not None:
                # Each earlier version is compared with the best one so far, not only with
                # the last: a fix that drew less than the first build stayed when the repair
                # after it looked worse again, as nothing compared the fix with the first build.
                best, chosen = latest, None
                for earlier in sorted(versions, reverse=True):
                    version = versions[earlier]
                    if earlier >= best or earlier not in played or version is None:
                        continue
                    if await prefers_earlier(script, earlier, best):
                        best, chosen = earlier, version
                kept = latest
                if chosen is not None:
                    try:
                        await asyncio.to_thread(app_versions.put_back, working_dir, chosen)
                    except OSError as exc:
                        log.warning(
                            "execution.completion_pass.version_not_put_back", error=str(exc)
                        )
                        record["put_back"] = "failed"
                    else:
                        kept = best
                record["kept_round"] = kept
                record["last_round"] = latest
            if checks:
                record["checks"] = len(checks)
        log.info(
            "execution.completion_pass.completed",
            status=record["status"],
            defects=len(defects),
            playtest=record["playtest"],
            visible=len(visible),
            recheck=record.get("recheck"),
            regressions=len(record.get("regressions", ())),
            replay=record.get("replay_2"),
            still_visible=len(record.get("replay_2_defects", ())),
            repair=record.get("repair"),
            kept_round=record.get("kept_round"),
            checks=len(checks),
        )
    await _announce(event_store, execution_id, "done", 0.0, status=record["status"])
    return _with_record(run_result, record)


__all__ = [
    "CHECK_DIR",
    "FIX_PROMPT",
    "MAX_CHECKS",
    "MAX_UNFINISHED",
    "NO_DEFECTS",
    "PLAY_BOUND_SECONDS",
    "PLAY_STOP_GRACE_SECONDS",
    "PLAY_LOOK_BEFORE",
    "PLAY_LOOK_FINISH",
    "PLAY_LOOK_PROMPT",
    "PICK_PROMPT",
    "PLAY_PLAN_PROMPT",
    "PLAYTEST_DIR",
    "RECHECK_PROMPT",
    "REVIEW_PROMPT",
    "REVIEWER_PERMISSION_MODE",
    "REVIEWER_TOOLS",
    "STAGE_EVENT",
    "UNFINISHED_MARK",
    "WORKER_TOOLS",
    "bound_unfinished",
    "build_fix_prompt",
    "build_pick_prompt",
    "check_command",
    "build_play_look_prompt",
    "build_play_plan_prompt",
    "build_recheck_prompt",
    "build_review_prompt",
    "complete_after_run",
    "parse_defects",
    "parse_pick",
    "serve_checks",
]
