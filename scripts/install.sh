#!/bin/bash
# Ouroboros installer — auto-detects runtime and installs accordingly.
# Usage: curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
set -euo pipefail

PACKAGE_NAME="ouroboros-ai"
MIN_PYTHON="3.12"

# Auto-detect: if a stable release exists on PyPI, use it. Otherwise allow pre-release.
# PyPI /json info.version returns latest stable only.
# If python3 is unavailable for JSON parsing, PRE_FLAG stays "yes" which is safe:
# --pre/--prerelease=allow still installs stable versions when they're the latest.
PRE_FLAG="yes"
if command -v curl &>/dev/null; then
  STABLE=$(curl -fsSL "https://pypi.org/pypi/${PACKAGE_NAME}/json" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || true)
  if [ -n "$STABLE" ]; then
    if ! echo "$STABLE" | grep -qE '(a|b|rc|dev)'; then
      PRE_FLAG=""
    fi
  fi
fi

echo "╭──────────────────────────────────────╮"
echo "│     Ouroboros Installer              │"
echo "╰──────────────────────────────────────╯"
echo

# 1. Detect installer: uv > pipx > pip (determines Python requirement)
HAS_UV=false
HAS_PIPX=false
PYTHON=""

if command -v uv &>/dev/null; then
  HAS_UV=true
  echo "  uv:     $(uv --version)"
elif command -v pipx &>/dev/null; then
  HAS_PIPX=true
  echo "  pipx:   $(pipx --version)"
fi

# Helper: check whether a Python executable meets MIN_PYTHON
_python_ok() {
  local cmd="$1"
  local ver
  ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
  [ -n "$ver" ] && [ "$(printf '%s\n' "$MIN_PYTHON" "$ver" | sort -V | head -n1)" = "$MIN_PYTHON" ]
}

# Python check: always required for pip; also needed by pipx to pick the right interpreter.
if [ "$HAS_UV" = false ]; then
  if [ "$HAS_PIPX" = true ]; then
    # For pipx: probe versioned candidates first, then fall back to generic names.
    for cmd in python3.14 python3.13 python3.12 python3 python; do
      if command -v "$cmd" &>/dev/null && _python_ok "$cmd"; then
        PYTHON="$(command -v "$cmd")"
        break
      fi
    done
    if [ -z "$PYTHON" ]; then
      echo "Error: pipx requires Python >=${MIN_PYTHON} but none was found."
      echo ""
      echo "Install Python ${MIN_PYTHON}+: https://www.python.org/downloads/"
      echo "Or switch to uv (recommended): curl -LsSf https://astral.sh/uv/install.sh | sh"
      exit 1
    fi
    echo "  Python: $($PYTHON --version)"
  else
    # pip fallback: any matching python3/python will do.
    for cmd in python3 python; do
      if command -v "$cmd" &>/dev/null && _python_ok "$cmd"; then
        PYTHON="$cmd"
        break
      fi
    done
    if [ -z "$PYTHON" ]; then
      echo "Error: No installer found (uv, pipx) and Python >=${MIN_PYTHON} not available."
      echo ""
      echo "Install one of:"
      echo "  • uv (recommended): curl -LsSf https://astral.sh/uv/install.sh | sh"
      echo "  • Python ${MIN_PYTHON}+: https://www.python.org/downloads/"
      exit 1
    fi
    echo "  Python: $($PYTHON --version)"
  fi
fi

# 2. Detect runtimes
EXTRAS=""
RUNTIME=""
HAS_CODEX=false
HAS_CLAUDE=false
if command -v codex &>/dev/null; then
  echo "  Codex:  $(which codex)"
  HAS_CODEX=true
fi
if command -v claude &>/dev/null; then
  echo "  Claude: $(which claude)"
  HAS_CLAUDE=true
fi

if [ "$HAS_CODEX" = true ] && [ "$HAS_CLAUDE" = true ]; then
  if [ -t 0 ]; then
    echo
    echo "Both Codex and Claude detected. Which runtime do you want to use?"
    echo "  [1] Claude  (pip install ${PACKAGE_NAME}[mcp,claude])  ← recommended"
    echo "  [2] Codex   (pip install ${PACKAGE_NAME})"
    echo "  [3] All     (pip install ${PACKAGE_NAME}[all])"
    read -rp "Select [1]: " choice
    case "${choice:-1}" in
      2) EXTRAS=""; RUNTIME="codex" ;;
      3) EXTRAS="[all]"; RUNTIME="" ;;
      *) EXTRAS="[claude]"; RUNTIME="claude" ;;
    esac
  else
    # Pipe mode: default to claude when both exist
    EXTRAS="[claude]"
    RUNTIME="claude"
  fi
elif [ "$HAS_CLAUDE" = true ]; then
  EXTRAS="[claude]"
  RUNTIME="claude"
elif [ "$HAS_CODEX" = true ]; then
  EXTRAS=""
  RUNTIME="codex"
else
  if [ -t 0 ]; then
    # Interactive mode: ask the user
    echo
    echo "No runtime CLI detected. Which runtime will you use?"
    echo "  [1] Claude  (pip install ${PACKAGE_NAME}[claude])  ← recommended"
    echo "  [2] Codex   (pip install ${PACKAGE_NAME})"
    echo "  [3] All     (pip install ${PACKAGE_NAME}[all])"
    read -rp "Select [1]: " choice
  else
    # Pipe mode (curl | bash): install base package, skip runtime-specific setup
    echo
    echo "  No runtime detected (non-interactive: installing base package)"
    choice="0"
  fi
  case "${choice:-1}" in
    0) EXTRAS=""; RUNTIME="" ;;
    2) EXTRAS="[mcp]"; RUNTIME="codex" ;;
    3) EXTRAS="[all]"; RUNTIME="" ;;
    *) EXTRAS="[mcp,claude]"; RUNTIME="claude" ;;
  esac
fi

INSTALL_SPEC="${PACKAGE_NAME}${EXTRAS}"

echo
echo "Installing ${INSTALL_SPEC} ..."

# 3. Install (or upgrade if already installed)
# uv tool install has issues with [extras] syntax — use --with for reliability.
INSTALL_METHOD=""
if [ "$HAS_UV" = true ]; then
  INSTALL_METHOD="uv"
  UV_ARGS=(tool install --upgrade --python ">=3.12" "$PACKAGE_NAME")
  if [ -n "$PRE_FLAG" ]; then
    UV_ARGS+=(--prerelease=allow)
  fi
  # Map extras to explicit --with flags for uv
  case "$EXTRAS" in
    "[claude]")
      UV_ARGS+=(--with "claude-agent-sdk>=0.1.0" --with "anthropic>=0.52.0")
      ;;
    "[all]")
      UV_ARGS+=(--with "claude-agent-sdk>=0.1.0" --with "anthropic>=0.52.0" --with "litellm>=1.80.0,<=1.82.6")
      ;;
  esac
  uv "${UV_ARGS[@]}"
elif [ "$HAS_PIPX" = true ]; then
  INSTALL_METHOD="pipx"
  if [ -n "$PRE_FLAG" ]; then
    pipx install --force --python "$PYTHON" --pip-args='--pre' "$INSTALL_SPEC"
  else
    pipx install --force --python "$PYTHON" "$INSTALL_SPEC"
  fi
else
  INSTALL_METHOD="pip"
  if [ -n "$PRE_FLAG" ]; then
    $PYTHON -m pip install --user --upgrade --pre "$INSTALL_SPEC"
  else
    $PYTHON -m pip install --user --upgrade "$INSTALL_SPEC"
  fi
fi

# Ensure ouroboros binary is in PATH (uv tool install may add to ~/.local/bin)
if ! command -v ouroboros &>/dev/null; then
  for p in "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/bin"; do
    if [ -x "$p/ouroboros" ]; then
      export PATH="$p:$PATH"
      break
    fi
  done
fi

# 4. Setup (ouroboros CLI configures runtime-specific integration)
if [ -n "$RUNTIME" ] && command -v ouroboros &>/dev/null; then
  echo
  echo "Running setup..."
  ouroboros setup --runtime "$RUNTIME" --non-interactive || true
fi

# 5. Claude Code integration (MCP + skills)
if command -v claude &>/dev/null; then
  echo
  echo "Setting up Claude Code integration..."

  # 5a. Register MCP server in ~/.claude/mcp.json
  # (ouroboros setup may have done this already, but we ensure it with timeout)
  MCP_FILE="$HOME/.claude/mcp.json"
  mkdir -p "$HOME/.claude"

  # MCP command matches the installer that actually ran in step 3
  if [ "$INSTALL_METHOD" = "uv" ]; then
    case "$EXTRAS" in
      "[mcp,claude]")
        OUROBOROS_ENTRY='{"command":"uvx","args":["--from","ouroboros-ai[mcp,claude]","ouroboros","mcp","serve"]}'
        ;;
      "[all]")
        OUROBOROS_ENTRY='{"command":"uvx","args":["--from","ouroboros-ai[all]","ouroboros","mcp","serve"]}'
        ;;
      *)
        OUROBOROS_ENTRY='{"command":"uvx","args":["--from","ouroboros-ai[mcp]","ouroboros","mcp","serve"]}'
        ;;
    esac
  elif [ "$INSTALL_METHOD" = "pipx" ]; then
    OUROBOROS_ENTRY='{"command":"ouroboros","args":["mcp","serve"]}'
  else
    OUROBOROS_ENTRY='{"command":"'"${PYTHON:-python3}"'","args":["-m","ouroboros","mcp","serve"]}'
  fi

  # Find a working Python: system python3, or uv-managed python
  MCP_PYTHON=""
  if command -v python3 &>/dev/null; then
    MCP_PYTHON="python3"
  elif command -v uv &>/dev/null; then
    MCP_PYTHON="uv run python3"
  fi

  if [ -n "$MCP_PYTHON" ]; then
    if [ -f "$MCP_FILE" ]; then
      if MCP_FILE="$MCP_FILE" OUROBOROS_ENTRY="$OUROBOROS_ENTRY" $MCP_PYTHON -c "
import json, os
mcp_file = os.environ['MCP_FILE']
entry = json.loads(os.environ['OUROBOROS_ENTRY'])
with open(mcp_file) as f:
    data = json.load(f)
servers = data.setdefault('mcpServers', {})
servers['ouroboros'] = entry
with open(mcp_file, 'w') as f:
    json.dump(data, f, indent=2)
print('merged')
" 2>/dev/null; then
        echo "  MCP: merged into existing $MCP_FILE"
      else
        echo "  MCP: could not merge — check $MCP_FILE manually"
      fi
    else
      if MCP_FILE="$MCP_FILE" OUROBOROS_ENTRY="$OUROBOROS_ENTRY" $MCP_PYTHON -c "
import json, os
mcp_file = os.environ['MCP_FILE']
entry = json.loads(os.environ['OUROBOROS_ENTRY'])
data = {'mcpServers': {'ouroboros': entry}}
with open(mcp_file, 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null; then
        echo "  MCP: created $MCP_FILE"
      else
        echo "  MCP: could not create — check $MCP_FILE manually"
      fi
    fi
  else
    echo "  MCP: skipped (no python3 found — add manually to $MCP_FILE)"
  fi

  # 5b. Install/update Ouroboros skills (claude plugin)
  echo "  Installing Ouroboros skills..."
  claude plugin marketplace add Q00/ouroboros 2>/dev/null || true
  claude plugin marketplace update ouroboros 2>/dev/null || true
  if claude plugin install ouroboros@ouroboros 2>/dev/null; then
    echo "  Skills: installed"
  else
    echo "  Skills: skipped (install manually: claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros)"
  fi
fi

echo
echo "Done! Get started:"
echo
echo "  Open your AI coding agent and run:"
echo '    > ooo interview "your idea here"'
echo
echo "  Or from the terminal:"
echo '    ouroboros init start "your idea here"'
