"""Run an inner check and write its status as a bounded wrapper receipt."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess


def _new_output(path: str, data: bytes) -> None:
    target = Path(path)
    if (
        target.is_absolute()
        or not target.parts
        or any(part in {"", ".", ".."} for part in target.parts)
    ):
        raise ValueError("wrapper output must be a contained relative path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or len({args.receipt, args.stdout, args.stderr}) != 3:
        parser.error("provide a command and distinct output paths")
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
        inner_status = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except OSError as exc:
        inner_status = 127
        stdout = b""
        stderr = str(exc).encode("utf-8", errors="replace")
    _new_output(args.stdout, stdout)
    _new_output(args.stderr, stderr)
    receipt = {
        "schema_version": 1,
        "kind": "ouroboros.test-wrapper-receipt",
        "inner_command": shlex.join(command),
        "inner_exit_status": inner_status,
        "outer_exit_status": inner_status,
        "stdout_ref": args.stdout,
        "stderr_ref": args.stderr,
        "test_result": "passed" if inner_status == 0 else "failed",
    }
    _new_output(args.receipt, json.dumps(receipt, sort_keys=True).encode("utf-8"))
    return inner_status


if __name__ == "__main__":
    raise SystemExit(main())
