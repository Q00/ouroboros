#!/usr/bin/env python3
"""ralph-rewind.py — Standalone MCP client that rewinds a lineage.

Connects to the Ouroboros MCP server via stdio, invokes
``ouroboros_evolve_rewind``, optionally checks out the git tag,
then prints a single JSON line to stdout.

Exit codes:
    0  — success
    1  — MCP connection failure
    2  — argument / usage error
    3  — tool-level error (evolve_rewind returned an error)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any


# ---------------------------------------------------------------------------
# MCP session helpers
# ---------------------------------------------------------------------------

async def connect_and_run(args: argparse.Namespace) -> dict[str, Any]:
    """Open an MCP stdio session and call evolve_rewind once."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=args.server_command,
        args=args.server_args,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await _call_rewind(session, args)


async def _call_rewind(session: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Invoke evolve_rewind and return result dict."""
    tool_args: dict[str, Any] = {
        "lineage_id": args.lineage_id,
        "to_generation": args.to_generation,
    }

    result = await session.call_tool("ouroboros_evolve_rewind", tool_args)

    # Extract text from response
    text = _extract_text(result)
    if text is None:
        return _error_result("No text content in evolve_rewind response")

    # Check for tool-level errors
    is_error = getattr(result, "isError", False) or getattr(result, "is_error", False)
    if is_error:
        return {
            "lineage_id": args.lineage_id,
            "from_generation": None,
            "to_generation": args.to_generation,
            "git_checkout": False,
            "error": text,
        }

    # Attempt git checkout if requested
    git_checkout = False
    if args.git_checkout:
        tag = f"ooo/{args.lineage_id}/gen_{args.to_generation}"
        try:
            subprocess.run(
                ["git", "checkout", tag],
                check=True,
                capture_output=True,
                text=True,
            )
            git_checkout = True
        except subprocess.CalledProcessError as e:
            return {
                "lineage_id": args.lineage_id,
                "from_generation": None,
                "to_generation": args.to_generation,
                "git_checkout": False,
                "error": f"git checkout failed: {e.stderr.strip()}",
            }

    return {
        "lineage_id": args.lineage_id,
        "from_generation": None,  # Extracted from text if needed
        "to_generation": args.to_generation,
        "git_checkout": git_checkout,
        "error": None,
    }


def _extract_text(result: Any) -> str | None:
    """Pull text out of an MCP CallToolResult."""
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text":
            return item.text  # type: ignore[return-value]
        # Some SDK versions use a string enum
        t = getattr(item, "type", None)
        if hasattr(t, "value") and t.value == "text":
            return item.text  # type: ignore[return-value]
    return None


def _error_result(msg: str) -> dict[str, Any]:
    return {
        "lineage_id": None,
        "from_generation": None,
        "to_generation": None,
        "git_checkout": False,
        "error": msg,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ralph-rewind",
        description="Rewind an evolutionary lineage via MCP stdio.",
    )
    p.add_argument("--lineage-id", required=True, help="Lineage ID to rewind")
    p.add_argument(
        "--to-generation",
        required=True,
        type=int,
        help="Generation number to rewind to (inclusive)",
    )
    p.add_argument(
        "--git-checkout",
        action="store_true",
        help="Check out the git tag after rewind",
    )
    p.add_argument(
        "--server-command",
        default="ouroboros",
        help="MCP server executable (default: ouroboros)",
    )
    p.add_argument(
        "--server-args",
        nargs=argparse.REMAINDER,
        default=["mcp"],
        help="Arguments for the MCP server command (default: mcp). "
        "Must be the LAST option — all remaining tokens are captured.",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.lineage_id:
        parser.error("--lineage-id is required")

    if args.to_generation < 1:
        json.dump(
            _error_result("to_generation must be >= 1"),
            sys.stdout,
        )
        print()
        sys.exit(2)

    try:
        output = asyncio.run(connect_and_run(args))
    except (ConnectionError, OSError, TimeoutError) as exc:
        json.dump(
            _error_result(f"MCP connection failed: {exc}"),
            sys.stdout,
        )
        print()
        sys.exit(1)
    except Exception as exc:
        json.dump(
            _error_result(f"Unexpected error: {exc}"),
            sys.stdout,
        )
        print()
        sys.exit(3)

    json.dump(output, sys.stdout)
    print()

    # Map result to exit code
    if output.get("error"):
        sys.exit(3)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
