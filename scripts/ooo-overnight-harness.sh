#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

HARNESS="${REPO_ROOT}/scripts/ooo-env-harness.py"
DEFAULT_ARGS=(--repo "${REPO_ROOT}" --include-run-smoke)

if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -dimsu "${PYTHON_BIN}" "${HARNESS}" "${DEFAULT_ARGS[@]}" "$@"
fi

exec "${PYTHON_BIN}" "${HARNESS}" "${DEFAULT_ARGS[@]}" "$@"
