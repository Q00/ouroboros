"""Deterministic E2E entrypoint for the test fixture github-pr-ops.

This is the local stand-in the firewall subprocess-launches during the
contract proof in tests/integration/plugin/test_e2e.py. It deliberately
does NOT hit the GitHub API.

Behavior:
  - argv = ["review", "<url>"]:
      url contains "fail" → exit 2 with stderr "synthetic failure"
      url contains "ok"   → exit 0 with stdout {"status": "ok", "url": ...}
      else                → exit 0 with stdout {"status": "ok", "url": ...}
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github-pr-ops")
    parser.add_argument("command", choices=["review"])
    parser.add_argument("pull_request_url")
    args = parser.parse_args(argv)

    if "fail" in args.pull_request_url:
        sys.stderr.write("synthetic failure\n")
        return 2

    sys.stdout.write(
        json.dumps(
            {
                "plugin": "github-pr-ops",
                "command": args.command,
                "pull_request_url": args.pull_request_url,
                "status": "ok",
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
