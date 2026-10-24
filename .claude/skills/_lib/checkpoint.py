#!/usr/bin/env python3
# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint helper for the runbook skills (vuln-scan, triage,
threat-model, dnr-hunt, dnr-respond).

Called via Bash from within a skill so phase state and final output land on disk
in small, atomic chunks instead of one large Write tool call.

    save   <state_dir> <N> [<name>] --from F [--key K]  → <K><N>.json + progress.json
    shard  <state_dir> <shard_id> --from F              → shard_<id>.json; shards_done += id
    done   <state_dir> <N> [--key K]                    → progress.json status=complete
    load   <state_dir>                                  → progress.json to stdout
    append <output_file> --from F [--replace]           → append or replace atomically
    reset  <state_dir>                                  → rm -rf state dir
    rundir <results_parent> --state D [--scope P] [--fresh]  → print run dir

rundir gives every skill run a harness-style results/<target>/<timestamp>/
directory (same strftime as harness/cli.py, so lexical sort == chronological).
The newest timestamp-format dir under <results_parent> (other dirs — user
backups like latest/, <ts>.bak, symlinks, or future-dated plants — are
ignored) is printed when this skill can still use it: its <D>/progress.json
is absent (another skill started the dir; the lifecycle accretes there) or
has status "running" (resume own run). A dir where this skill already
completed — or a corrupt progress.json — mints a new UTC-stamped dir.
--fresh always mints. --scope <target-dir> stamps a .rundir-scope marker at
mint and refuses to share/resume a dir whose marker names a different
resolved path, so two targets sharing a basename can't cross-contaminate.

Payload always comes from --from <file> (written via the Write tool), never
stdin or heredoc: target-derived strings in a heredoc could collide with the
delimiter and break out to shell. With --from, no repo-derived bytes touch
the Bash argv.

--key defaults to "phase" (vuln-scan, triage). Pass --key stage for
threat-model bootstrap. progress.json schema:
    {"status": "running"|"complete", "<key>_done": N, "shards_done": [...], "updated": iso}

All writes are atomic (tmp + os.replace) so a kill mid-write never leaves a
partial file that breaks resume.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys

_ROOT = Path(os.environ.get("CHECKPOINT_ROOT", ".")).resolve()

# Run dirs minted by rundir: harness timestamp + optional same-second suffix.
_RUN_DIR_RE = re.compile(r"\d{8}T\d{6}Z(_\d+)?")


def _confine(p: str | Path, *, must_end: str | None = None) -> Path:
    """Normalize p lexically under CHECKPOINT_ROOT (default: cwd).

    The Bash permission is a prefix wildcard, so a prompt-injected agent could
    otherwise point append/reset at ~/.ssh, ~/.bashrc, etc. Confining to cwd
    keeps the blast radius at the repo being scanned."""
    r = Path(os.path.abspath(p))
    if not r.is_relative_to(_ROOT):
        print(f"checkpoint: refusing path outside {_ROOT}: {p}", file=sys.stderr)
        raise SystemExit(2)
    if must_end and not r.name.endswith(must_end):
        print(
            f"checkpoint: refusing {p} (name must end with {must_end!r})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return r


@contextmanager
def _parent_fd(path: Path, *, create: bool = False):
    relative = _confine(path).relative_to(_ROOT)
    if not relative.parts:
        raise ValueError("checkpoint root is not an output file")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("checkpoint requires no-follow directory descriptors")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(_ROOT, flags)
    try:
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def _read_text(path: Path) -> str:
    with _parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("checkpoint payload is not a regular file")
            return stream.read()


def _exists(path: Path) -> bool:
    try:
        with _parent_fd(path) as (parent, name):
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _mkdir(path: Path) -> None:
    with _parent_fd(path, create=True) as (parent, name):
        os.mkdir(name, mode=0o700, dir_fd=parent)


def _safe_token(s: str, what: str) -> str:
    if "/" in s or os.sep in s or ".." in s:
        print(f"checkpoint: refusing {what} with path separators: {s!r}", file=sys.stderr)
        raise SystemExit(2)
    return s


def _load_progress(p: Path) -> dict | None:
    """progress.json as a dict; None when missing, corrupt, or non-dict JSON.

    ValueError covers JSONDecodeError and UnicodeDecodeError; OSError covers
    missing/unreadable. None is never a crash, but callers split it by cause:
    rundir mints a fresh run dir either way (non-destructive), while
    save/shard use _progress_corrupt to refuse overwriting an existing file
    (silently restarting from {} would erase shards_done and re-run
    finished work)."""
    try:
        loaded = json.loads(_read_text(p))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _progress_corrupt(p: Path) -> bool:
    """True when progress.json exists but holds no usable progress dict."""
    return _exists(p) and _load_progress(p) is None


def _refuse_corrupt_progress(p: Path) -> int:
    print(
        f"error: {p} exists but holds no valid progress object — "
        "inspect or delete it before recording more progress "
        "(the payload file was still written)",
        file=sys.stderr,
    )
    return 2


def _atomic_write(path: Path, data: str) -> None:
    with _parent_fd(path, create=True) as (parent, name):
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError("checkpoint refuses non-regular output")
        temporary = f".{name}.{secrets.token_hex(16)}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            with _parent_fd(path) as (current, _):
                held = os.fstat(parent)
                observed = os.fstat(current)
                if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
                    raise OSError("checkpoint output parent changed during publication")
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _pop_opt(
    argv: list[str], flag: str, default: str | None = None
) -> tuple[list[str], str | None]:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 >= len(argv):
            print(f"checkpoint: {flag} requires a value", file=sys.stderr)
            raise SystemExit(2)
        return argv[:i] + argv[i + 2 :], argv[i + 1]
    return argv, default


def _read_payload(src: str | None) -> str:
    """Payload comes from --from <file>, never stdin/heredoc.

    Target-derived strings in a heredoc can terminate the delimiter early and
    break out to shell. Requiring --from keeps all repo-derived bytes out of
    the Bash argv; the file is written via the Write tool (no shell)."""
    if src is None:
        print(
            "checkpoint: payload must be passed via --from <file> "
            "(stdin/heredoc disabled to prevent shell injection)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return _read_text(_confine(src))


def _read_json(src: str | None) -> str:
    raw = _read_payload(src)
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"checkpoint: --from {src} is not valid JSON: {e}", file=sys.stderr)
        raise SystemExit(1)
    return raw


def _write_progress(state_dir: Path, *, status: str, key: str, n: int, shards: list[str]) -> None:
    _atomic_write(
        state_dir / "progress.json",
        json.dumps(
            {
                "status": status,
                f"{key}_done": n,
                "shards_done": shards,
                "updated": datetime.now(UTC).isoformat(),
            }
        ),
    )


def cmd_save(argv: list[str]) -> int:
    argv, key = _pop_opt(argv, "--key", "phase")
    assert key
    argv, src = _pop_opt(argv, "--from")
    if len(argv) < 2:
        print(
            "usage: checkpoint.py save <state_dir> <N> [<name>] --from <file> [--key K]",
            file=sys.stderr,
        )
        return 2
    state_dir = _confine(argv[0], must_end="-state")
    n = int(argv[1])
    key = _safe_token(key, "--key")
    name = argv[2] if len(argv) > 2 else f"{key}{n}"
    raw = _read_json(src)
    _atomic_write(state_dir / f"{key}{n}.json", raw)
    # same refusal as cmd_shard: overwriting a corrupt progress.json with
    # shards=[] would destroy the shards_done evidence through the save door
    p = state_dir / "progress.json"
    if _progress_corrupt(p):
        return _refuse_corrupt_progress(p)
    _write_progress(state_dir, status="running", key=key, n=n, shards=[])
    print(f"checkpoint: {key} {n} ({name}) saved → {state_dir}/")
    return 0


def cmd_shard(argv: list[str]) -> int:
    argv, src = _pop_opt(argv, "--from")
    if len(argv) != 2:
        print(
            "usage: checkpoint.py shard <state_dir> <shard_id> --from <file>",
            file=sys.stderr,
        )
        return 2
    state_dir = _confine(argv[0], must_end="-state")
    shard_id = _safe_token(argv[1], "shard_id")
    # payload first: the shard output is the expensive part (a subagent
    # run) and must land on disk even when the bookkeeping below refuses
    raw = _read_json(src)
    _atomic_write(state_dir / f"shard_{shard_id}.json", raw)
    p = state_dir / "progress.json"
    if _progress_corrupt(p):
        return _refuse_corrupt_progress(p)
    prog: dict | None = _load_progress(p)
    if prog is None:
        prog = {"status": "running"}
    shards: list = prog.get("shards_done", [])
    if shard_id not in shards:
        shards.append(shard_id)
    prog["shards_done"] = shards
    prog["updated"] = datetime.now(UTC).isoformat()
    _atomic_write(p, json.dumps(prog))
    print(f"checkpoint: shard {shard_id} saved ({len(shards)} done)")
    return 0


def cmd_done(argv: list[str]) -> int:
    argv, key = _pop_opt(argv, "--key", "phase")
    assert key
    if len(argv) != 2:
        print("usage: checkpoint.py done <state_dir> <N> [--key K]", file=sys.stderr)
        return 2
    _write_progress(
        _confine(argv[0], must_end="-state"),
        status="complete",
        key=_safe_token(key, "--key"),
        n=int(argv[1]),
        shards=[],
    )
    print("checkpoint: complete")
    return 0


def cmd_load(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: checkpoint.py load <state_dir>", file=sys.stderr)
        return 2
    p = _confine(argv[0], must_end="-state") / "progress.json"
    sys.stdout.write(_read_text(p) if _exists(p) else '{"status": "absent"}')
    return 0


def cmd_append(argv: list[str]) -> int:
    replace = "--replace" in argv
    argv = [arg for arg in argv if arg != "--replace"]
    argv, src = _pop_opt(argv, "--from")
    if len(argv) != 1:
        print(
            "usage: checkpoint.py append <output_file> --from <file> [--replace]",
            file=sys.stderr,
        )
        return 2
    out = _confine(argv[0])
    chunk = _read_payload(src)
    previous = "" if replace or not _exists(out) else _read_text(out)
    _atomic_write(out, previous + chunk + ("" if chunk.endswith("\n") else "\n"))
    print(f"checkpoint: appended {len(chunk)} bytes → {out}")
    return 0


def _rundir_reusable(run: Path, state: str, scope: str | None) -> bool:
    """True when this skill may print `run` instead of minting a new dir."""
    marker = run / ".rundir-scope"
    if scope:
        # rundir stamps .rundir-scope on every dir it mints, so a marker-less
        # dir under results/ came from another tool — e.g. the autonomous
        # dnr-pipeline writes results/<target>/<ts>/ (with INCIDENTS.json +
        # result.json, no marker) — and must never be adopted or overwritten.
        if not _exists(marker):
            return False
        # Same basename, different codebase (clients/acme/api vs
        # clients/globex/api) must not share or resume each other's runs.
        try:
            if _read_text(marker).strip() != scope:
                return False
        except OSError:
            return False
    prog = run / state / "progress.json"
    if not _exists(prog):
        # Another skill started this run dir; share it so one dir accretes
        # the target's whole lifecycle (threat model → scan → triage → patch).
        return True
    # Corrupt progress.json (any shape) means "mint new", never a crash.
    data = _load_progress(prog)
    return data is not None and data.get("status") == "running"


def _minted_run_dir(d: Path, now_ts: str) -> bool:
    """Could rundir itself have minted d? Gates the reuse candidates.

    The name must be a harness timestamp (+ optional _NN suffix) no later
    than now — rundir never mints ahead of the clock, so a future-dated name
    is a plant that would shadow every later run of this target. Symlinks
    are never minted either, and printing one as {RUN} would route every
    downstream skill write through it — a pre-positioned link under
    results/ could escape CHECKPOINT_ROOT. The [:16] slice is the timestamp
    prefix _RUN_DIR_RE pins."""
    return bool(
        _RUN_DIR_RE.fullmatch(d.name)
        and d.name[:16] <= now_ts
        and d.is_dir()
        and not d.is_symlink()
    )


def cmd_rundir(argv: list[str]) -> int:
    argv, state = _pop_opt(argv, "--state")
    argv, scope = _pop_opt(argv, "--scope")
    fresh = "--fresh" in argv
    argv = [a for a in argv if a != "--fresh"]
    if len(argv) != 1 or not state:
        print(
            "usage: checkpoint.py rundir <results_parent> --state <dir> "
            "[--scope <target-dir>] [--fresh]",
            file=sys.stderr,
        )
        return 2
    state = _safe_token(state, "--state")
    parent = _confine(argv[0])
    _confine(parent / state, must_end="-state")
    scope = str(Path(scope).resolve()) if scope else None
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not fresh and parent.is_dir():
        newest = max((d for d in parent.iterdir() if _minted_run_dir(d, ts)), default=None)
        if newest and _rundir_reusable(newest, state, scope):
            print(newest)
            return 0
    # exist_ok=False + suffix loop: timestamps are second-resolution, so a
    # re-mint within the same second must not silently reuse the old dir.
    # Zero-padded suffix keeps lexical sort == chronological past _09.
    run, n = parent / ts, 1
    while True:
        try:
            _mkdir(run)
            break
        except FileExistsError:
            n += 1
            run = parent / f"{ts}_{n:02d}"
    if scope:
        _atomic_write(run / ".rundir-scope", scope + "\n")
    print(run)
    return 0


def cmd_reset(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: checkpoint.py reset <state_dir>", file=sys.stderr)
        return 2
    d = _confine(argv[0], must_end="-state")
    if _exists(d):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError("checkpoint reset requires descriptor-safe rmtree")
        with _parent_fd(d) as (parent, name):
            shutil.rmtree(name, dir_fd=parent)
            print(f"checkpoint: removed {d}/")
    return 0


CMDS = {
    "save": cmd_save,
    "shard": cmd_shard,
    "done": cmd_done,
    "load": cmd_load,
    "append": cmd_append,
    "reset": cmd_reset,
    "rundir": cmd_rundir,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in CMDS:
        print(f"usage: checkpoint.py {{{'|'.join(CMDS)}}} ...", file=sys.stderr)
        return 2
    try:
        return CMDS[argv[0]](argv[1:])
    except (OSError, ValueError) as exc:
        print(f"checkpoint: refused filesystem operation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
