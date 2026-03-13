#!/usr/bin/env bash
# Fast MCP server launcher — avoids uv cold-start latency.
# Activates the venv directly and runs the module entry point.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${SCRIPT_DIR}/.venv/bin/python"

if [ ! -x "$VENV" ]; then
  exec uv run --directory "$SCRIPT_DIR" ouroboros mcp serve "$@"
fi

exec "$VENV" -m ouroboros.cli mcp serve "$@"
