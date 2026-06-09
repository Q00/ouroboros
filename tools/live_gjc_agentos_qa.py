#!/usr/bin/env python3
"""Thin opt-in QA wrapper for live GJC AgentOS tests."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

from ouroboros.config import get_gjc_cli_path

_TIMEOUT_SECONDS = 900


def _resolve_gjc_cli() -> str | None:
    configured = get_gjc_cli_path()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        return None
    return shutil.which("gjc")


def _gjc_version(cli_path: str | None) -> str:
    if not cli_path:
        return "unresolved"
    for args in ([cli_path, "--version"], [cli_path, "version"]):
        try:
            completed = subprocess.run(
                args, text=True, capture_output=True, timeout=10, check=False
            )
        except Exception as exc:  # pragma: no cover - host dependent
            return f"version unavailable: {type(exc).__name__}: {exc}"
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0 and output:
            return output.splitlines()[0]
    return "version unavailable"


def _count(pattern: str, text: str) -> int:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    return int(matches[-1]) if matches else 0


def main() -> int:
    artifacts_dir = Path(
        os.environ.get("OUROBOROS_LIVE_GJC_ARTIFACTS", "artifacts/live-gjc-agentos")
    )
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = artifacts_dir / "pytest-live-gjc-agentos.log"

    cli_path = _resolve_gjc_cli()
    skip_reasons: list[str] = []
    if os.environ.get("OUROBOROS_LIVE_GJC") != "1":
        skip_reasons.append("Set OUROBOROS_LIVE_GJC=1 to opt in to live GJC QA.")
    if not cli_path:
        skip_reasons.append(
            "GJC CLI is not resolvable; install gjc, put it on PATH, or set OUROBOROS_GJC_CLI_PATH."
        )

    print("GJC Live AgentOS QA")
    print(f"artifacts_dir={artifacts_dir}")
    print(f"gjc_binary={cli_path or 'unresolved'}")
    print(f"gjc_version={_gjc_version(cli_path)}")
    for reason in skip_reasons:
        print(f"SKIP_GATE: {reason}")

    command = [
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "-m",
        "live_gjc",
        "tests/live/test_gjc_agentos_live.py",
        "-vv",
    ]
    print("command=" + " ".join(command))

    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        receipt_path.write_text(output, encoding="utf-8")
        print(
            f"QA_RECEIPT status=FAILED exercised=0 skipped=0 reason=timeout artifact={receipt_path}"
        )
        return 124

    output = completed.stdout + completed.stderr
    receipt_path.write_text(output, encoding="utf-8")
    if output:
        print(output, end="" if output.endswith("\n") else "\n")

    skipped = _count(r"(\d+)\s+skipped", output)
    failed = _count(r"(\d+)\s+failed", output)
    errors = _count(r"(\d+)\s+errors?", output)
    passed = _count(r"(\d+)\s+passed", output)
    exercised = passed + failed + errors
    status = "SKIPPED" if exercised == 0 and skipped > 0 else "EXERCISED"
    if failed or errors:
        status = "FAILED"

    print(
        "QA_RECEIPT "
        f"status={status} exercised={exercised} skipped={skipped} "
        f"failed={failed} errors={errors} gjc_binary={cli_path or 'unresolved'} "
        f"artifact={receipt_path}"
    )

    if status == "SKIPPED":
        return 0
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
