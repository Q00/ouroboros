"""Run an inner check and write its status as a bounded wrapper receipt."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import shlex
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.core.filesystem_capability import NoFollowDirectoryChain

if "_WRAPPER_DIRECTORY_OPENER" not in globals():
    from ouroboros.core.filesystem_capability import (
        open_nofollow_directory_chain as _WRAPPER_DIRECTORY_OPENER,
    )


def _output_parent(path: str) -> NoFollowDirectoryChain:
    target = Path(path)
    if (
        target.is_absolute()
        or not target.parts
        or any(part in {"", ".", ".."} for part in target.parts)
    ):
        raise ValueError("wrapper output must be a contained relative path")
    return _WRAPPER_DIRECTORY_OPENER(Path.cwd(), relative_components=target.parts[:-1])


def _new_output(path: str, data: bytes, parent: NoFollowDirectoryChain) -> None:
    if not parent.create_exclusive(Path(path).name, data):
        raise FileExistsError(path)


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
    with ExitStack() as stack:
        parents = {}
        for path in (args.receipt, args.stdout, args.stderr):
            parent = _output_parent(path)
            stack.callback(parent.close)
            parents[path] = parent
        try:
            completed = subprocess.run(command, capture_output=True, check=False)
            inner_status = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except OSError as exc:
            inner_status = 127
            stdout = b""
            stderr = str(exc).encode("utf-8", errors="replace")
        _new_output(args.stdout, stdout, parents[args.stdout])
        _new_output(args.stderr, stderr, parents[args.stderr])
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
        _new_output(
            args.receipt, json.dumps(receipt, sort_keys=True).encode("utf-8"), parents[args.receipt]
        )
        return inner_status


if __name__ == "__main__":
    raise SystemExit(main())
