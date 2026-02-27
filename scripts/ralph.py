#!/usr/bin/env python3
"""ralph.py — Standalone MCP client that calls evolve_step once.

Connects to the Ouroboros MCP server via stdio, invokes
``ouroboros_evolve_step`` (and optionally ``ouroboros_lateral_think``
on stagnation), then prints a single JSON line to stdout.

Exit codes:
    0  — success
    1  — MCP connection failure
    2  — argument / usage error
    3  — tool-level error (evolve_step or lateral_think returned an error)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Text parser — FastMCP drops meta, so we regex the text block
# ---------------------------------------------------------------------------

# Expected format from EvolveStepHandler:
#   ## Generation 2
#   **Action**: continue
#   **Phase**: reflect
#   **Convergence similarity**: 85.00%
#   **Reason**: ...
#   **Lineage**: lin_xxx (2 generations)
#   **Next generation**: 3

_GENERATION_RE = re.compile(r"^##\s+Generation\s+(\d+)", re.MULTILINE)
_ACTION_RE = re.compile(r"^\*\*Action\*\*:\s*(\w+)", re.MULTILINE)
_SIMILARITY_RE = re.compile(r"^\*\*Convergence similarity\*\*:\s*([\d.]+)%", re.MULTILINE)
_NEXT_GEN_RE = re.compile(r"^\*\*Next generation\*\*:\s*(\d+)", re.MULTILINE)
_LINEAGE_RE = re.compile(r"^\*\*Lineage\*\*:\s*(\S+)", re.MULTILINE)


def parse_evolve_text(text: str) -> dict[str, Any]:
    """Parse the markdown text returned by ouroboros_evolve_step.

    Returns a dict with keys: generation, action, similarity,
    next_generation, lineage_id.  Missing fields are None.
    """
    def _first(pattern: re.Pattern[str]) -> str | None:
        m = pattern.search(text)
        return m.group(1) if m else None

    gen_str = _first(_GENERATION_RE)
    similarity_str = _first(_SIMILARITY_RE)
    next_gen_str = _first(_NEXT_GEN_RE)

    return {
        "generation": int(gen_str) if gen_str else None,
        "action": _first(_ACTION_RE),
        "similarity": round(float(similarity_str) / 100, 4) if similarity_str else None,
        "next_generation": int(next_gen_str) if next_gen_str else None,
        "lineage_id": _first(_LINEAGE_RE),
    }


# ---------------------------------------------------------------------------
# MCP session helpers
# ---------------------------------------------------------------------------

async def connect_and_run(args: argparse.Namespace) -> dict[str, Any]:
    """Open an MCP stdio session and call evolve_step once."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=args.server_command,
        args=args.server_args,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await _call_evolve(session, args)


async def _call_evolve(session: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Invoke evolve_step, handle stagnation with lateral_think retries."""
    # Build arguments for evolve_step
    tool_args: dict[str, Any] = {"lineage_id": args.lineage_id}
    if args.seed_file:
        seed_path = Path(args.seed_file)
        if not seed_path.exists():
            return _error_result(f"Seed file not found: {seed_path}")
        tool_args["seed_content"] = seed_path.read_text(encoding="utf-8")
    if args.no_execute:
        tool_args["execute"] = False

    result = await session.call_tool("ouroboros_evolve_step", tool_args)

    # Extract text from response
    text = _extract_text(result)
    if text is None:
        return _error_result("No text content in evolve_step response")

    parsed = parse_evolve_text(text)
    lineage_id = parsed.get("lineage_id") or args.lineage_id

    # Check for tool-level errors
    is_error = getattr(result, "isError", False) or getattr(result, "is_error", False)
    if is_error:
        return {
            "action": "failed",
            "generation": parsed.get("generation"),
            "lineage_id": lineage_id,
            "similarity": parsed.get("similarity"),
            "next_generation": parsed.get("next_generation"),
            "lateral_think_applied": False,
            "error": text,
        }

    action = parsed.get("action")

    # Handle stagnation — retry with lateral_think
    lateral_applied = False
    if action == "stagnated":
        for retry in range(args.max_retries):
            lateral_result = await session.call_tool(
                "ouroboros_lateral_think",
                {
                    "problem_context": f"Evolutionary lineage {lineage_id} stagnated at generation {parsed.get('generation')}",
                    "current_approach": f"Ontology similarity stuck at {parsed.get('similarity')}",
                    "persona": "contrarian",
                },
            )
            lateral_applied = True

            # Re-run evolve_step after lateral thinking
            retry_args: dict[str, Any] = {"lineage_id": lineage_id}
            if args.no_execute:
                retry_args["execute"] = False
            result = await session.call_tool("ouroboros_evolve_step", retry_args)
            text = _extract_text(result)
            if text is None:
                return _error_result("No text in retry evolve_step response")
            parsed = parse_evolve_text(text)
            action = parsed.get("action")
            if action != "stagnated":
                break

    return {
        "action": action,
        "generation": parsed.get("generation"),
        "lineage_id": lineage_id,
        "similarity": parsed.get("similarity"),
        "next_generation": parsed.get("next_generation"),
        "lateral_think_applied": lateral_applied,
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
        "action": "failed",
        "generation": None,
        "lineage_id": None,
        "similarity": None,
        "next_generation": None,
        "lateral_think_applied": False,
        "error": msg,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ralph",
        description="Call ouroboros_evolve_step once via MCP stdio.",
    )
    p.add_argument("--lineage-id", required=True, help="Lineage ID to evolve")
    p.add_argument("--seed-file", default=None, help="Path to seed YAML (Gen 1 only)")
    p.add_argument("--no-execute", action="store_true", help="Ontology-only evolution (skip execution)")
    p.add_argument("--max-retries", type=int, default=2, help="Lateral-think retries on stagnation (default: 2)")
    p.add_argument(
        "--server-command",
        default="ouroboros",
        help="MCP server executable (default: ouroboros)",
    )
    p.add_argument(
        "--server-args",
        nargs="*",
        default=["mcp"],
        help="Arguments for the MCP server command (default: mcp)",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.lineage_id:
        parser.error("--lineage-id is required")

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

    # Map action to exit code
    action = output.get("action")
    if action == "failed":
        sys.exit(3)
    elif output.get("error"):
        sys.exit(3)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
