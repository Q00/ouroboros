#!/bin/bash
# Ouroboros installer — auto-detects runtime and installs accordingly.
# Usage: curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
#
# Runtime selection (first match wins):
#   1. OUROBOROS_INSTALL_RUNTIME env var
#      (claude|codex|opencode|hermes|gemini|kiro|copilot|all)
#   2. Existing ~/.ouroboros/config.yaml runtime — preserved on upgrade
#      unless OUROBOROS_INSTALL_RECONFIGURE=1 (or --reconfigure flag) is set.
#   3. Interactive prompt when stdin is a TTY.
#   4. Auto-detect single CLI on PATH; default to claude in pipe mode.
set -euo pipefail

PACKAGE_NAME="ouroboros-ai"
MIN_PYTHON="3.12"
IS_LOCAL=false
RECONFIGURE="${OUROBOROS_INSTALL_RECONFIGURE:-}"
EXPLICIT_RUNTIME="${OUROBOROS_INSTALL_RUNTIME:-}"

# Parse simple flags: --reconfigure, --runtime <name>
while [ $# -gt 0 ]; do
  case "$1" in
    --reconfigure)
      RECONFIGURE="1"
      shift
      ;;
    --runtime)
      EXPLICIT_RUNTIME="${2:-}"
      shift 2
      ;;
    --runtime=*)
      EXPLICIT_RUNTIME="${1#--runtime=}"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# Override PACKAGE_NAME if running inside the repository clone
if [ -f "pyproject.toml" ] && grep -q "name = \"ouroboros-ai\"" pyproject.toml; then
  PACKAGE_NAME="."
  IS_LOCAL=true
elif [ -f "$(dirname "$0")/../pyproject.toml" ] && grep -q "name = \"ouroboros-ai\"" "$(dirname "$0")/../pyproject.toml"; then
  PACKAGE_NAME="$(dirname "$0")/.."
  IS_LOCAL=true
fi

# Auto-detect: if PyPI's latest published version is a pre-release, allow
# pre-releases. On lookup/parsing failure, stay stable-only.
PRE_FLAG=""
if [ "$IS_LOCAL" = false ] && command -v curl &>/dev/null; then
  LATEST=$(curl -fsSL "https://pypi.org/pypi/${PACKAGE_NAME}/json" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || true)
  if [ -n "$LATEST" ]; then
    if echo "$LATEST" | grep -qE '(a|b|rc|dev)'; then
      PRE_FLAG="yes"
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

# NOTE: Interpreter selection branches (uv, pipx, pip) are not covered
# by automated tests. When modifying this logic, manually verify:
#   1. `uv` available → uses `uv tool install --python ">=3.12"` (uv manages Python)
#   2. `pipx` available, no `uv` → probes python3.{14,13,12}/python3/python,
#      picks first >= 3.12, passes --python to pipx; exits if none found
#   3. Neither available → falls back to `python3 -m pip install --user`;
#      exits if python3/python < 3.12
#   4. Python < 3.12 with no uv/pipx → prints error and exits
# See bot review on PR #432 for context.

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
      echo "Or install uv (recommended). Pick whichever fits your environment:"
      echo "  pipx install uv"
      echo "  pip install --user uv"
      echo "  brew install uv          # macOS / Linuxbrew"
      echo "  curl -LsSf https://astral.sh/uv/install.sh | sh   # vendor one-liner"
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
      echo "  • uv (recommended). Pick whichever fits your environment:"
      echo "      pipx install uv"
      echo "      pip install --user uv"
      echo "      brew install uv          # macOS / Linuxbrew"
      echo "      curl -LsSf https://astral.sh/uv/install.sh | sh   # vendor one-liner"
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
HAS_HERMES=false
HAS_OPENCODE=false
HAS_GEMINI=false
HAS_KIRO=false
HAS_COPILOT=false
if command -v codex &>/dev/null; then
  echo "  Codex:  $(which codex)"
  HAS_CODEX=true
fi
if command -v claude &>/dev/null; then
  echo "  Claude: $(which claude)"
  HAS_CLAUDE=true
fi
if command -v hermes &>/dev/null; then
  echo "  Hermes: $(which hermes)"
  HAS_HERMES=true
fi
if command -v opencode &>/dev/null; then
  echo "  OpenCode: $(which opencode)"
  HAS_OPENCODE=true
fi
if command -v gemini &>/dev/null; then
  echo "  Gemini: $(which gemini)"
  HAS_GEMINI=true
fi
if command -v kiro-cli &>/dev/null; then
  echo "  Kiro:   $(which kiro-cli)"
  HAS_KIRO=true
fi
if command -v copilot &>/dev/null; then
  echo "  Copilot: $(which copilot)"
  HAS_COPILOT=true
fi

RUNTIME_COUNT=0
[ "$HAS_CLAUDE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_CODEX" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_HERMES" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_OPENCODE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_GEMINI" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_KIRO" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_COPILOT" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))

# Map a runtime name to (EXTRAS, RUNTIME) pair.
# Used after explicit/preserved runtime resolution to derive install extras.
_runtime_to_extras() {
  case "$1" in
    claude)  EXTRAS="[mcp,claude]"; RUNTIME="claude" ;;
    codex)   EXTRAS=""; RUNTIME="codex" ;;
    opencode) EXTRAS=""; RUNTIME="opencode" ;;
    hermes)  EXTRAS="[mcp]"; RUNTIME="hermes" ;;
    gemini)  EXTRAS=""; RUNTIME="gemini" ;;
    kiro)    EXTRAS=""; RUNTIME="kiro" ;;
    copilot) EXTRAS=""; RUNTIME="copilot" ;;
    all)     EXTRAS="[all]"; RUNTIME="" ;;
    "")      EXTRAS=""; RUNTIME="" ;;
    *)
      echo "Error: unsupported runtime '$1' (expected: claude, codex, opencode, hermes, gemini, kiro, copilot, all)"
      exit 1
      ;;
  esac
}

# Try to read the previously-configured runtime from ~/.ouroboros/config.yaml.
# Preserves user choice across upgrades unless --reconfigure / --runtime is set.
EXISTING_RUNTIME=""
EXISTING_CONFIG="$HOME/.ouroboros/config.yaml"
if [ -z "$EXPLICIT_RUNTIME" ] && [ -z "$RECONFIGURE" ] && [ -f "$EXISTING_CONFIG" ] && command -v python3 &>/dev/null; then
  EXISTING_RUNTIME=$(EXISTING_CONFIG="$EXISTING_CONFIG" python3 -c "
import os, re
supported = {'claude', 'codex', 'opencode', 'hermes', 'gemini', 'kiro', 'copilot'}
try:
    lines = open(os.environ['EXISTING_CONFIG']).read().splitlines()
    in_orchestrator = False
    for line in lines:
        if re.match(r'^orchestrator:\s*(?:#.*)?$', line):
            in_orchestrator = True
            continue
        if in_orchestrator and line and not line[0].isspace():
            break
        if in_orchestrator:
            match = re.match(r'\s+runtime_backend:\s*[\"\']?([^\"\'\s#]+)', line)
            if match and match.group(1) in supported:
                print(match.group(1))
                break
except Exception:
    pass
" 2>/dev/null || true)
fi

if [ -n "$EXPLICIT_RUNTIME" ]; then
  echo
  echo "  Runtime: $EXPLICIT_RUNTIME (from --runtime / OUROBOROS_INSTALL_RUNTIME)"
  _runtime_to_extras "$EXPLICIT_RUNTIME"
elif [ -n "$EXISTING_RUNTIME" ]; then
  echo
  echo "  Runtime: $EXISTING_RUNTIME (preserved from $EXISTING_CONFIG)"
  echo "           [dim]Re-run with --reconfigure to choose again.[/dim]"
  _runtime_to_extras "$EXISTING_RUNTIME"
elif [ "$RUNTIME_COUNT" -gt 1 ]; then
  if [ -t 0 ]; then
    echo
    echo "Multiple runtimes detected. Which runtime do you want to use?"
    echo "  [1] Claude   (pip install ${PACKAGE_NAME}[mcp,claude])"
    echo "  [2] Codex    (pip install ${PACKAGE_NAME})"
    echo "  [3] Hermes   (pip install ${PACKAGE_NAME}[mcp])"
    echo "  [4] OpenCode (pip install ${PACKAGE_NAME})"
    echo "  [5] Gemini   (pip install ${PACKAGE_NAME})"
    echo "  [6] Kiro     (pip install ${PACKAGE_NAME})"
    echo "  [7] Copilot  (pip install ${PACKAGE_NAME})"
    echo "  [8] All      (pip install ${PACKAGE_NAME}[all])"
    read -rp "Select [1]: " choice
    case "${choice:-1}" in
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "kiro" ;;
      7) _runtime_to_extras "copilot" ;;
      8) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode: default to claude when multiple runtimes exist
    _runtime_to_extras "claude"
  fi
elif [ "$HAS_CLAUDE" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "claude"
elif [ "$HAS_CODEX" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "codex"
elif [ "$HAS_HERMES" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "hermes"
elif [ "$HAS_OPENCODE" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "opencode"
elif [ "$HAS_GEMINI" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "gemini"
elif [ "$HAS_KIRO" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "kiro"
elif [ "$HAS_COPILOT" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "copilot"
else
  # No runtime CLI on PATH yet — first install. Always prompt when interactive
  # so the user picks deliberately rather than silently defaulting to claude.
  if [ -t 0 ]; then
    echo
    echo "No runtime CLI detected. Which runtime will you use?"
    echo "  [1] Claude   (pip install ${PACKAGE_NAME}[mcp,claude])  ← recommended"
    echo "  [2] Codex    (pip install ${PACKAGE_NAME})"
    echo "  [3] Hermes   (pip install ${PACKAGE_NAME}[mcp])"
    echo "  [4] OpenCode (pip install ${PACKAGE_NAME})"
    echo "  [5] Gemini   (pip install ${PACKAGE_NAME})"
    echo "  [6] Kiro     (pip install ${PACKAGE_NAME})"
    echo "  [7] Copilot  (pip install ${PACKAGE_NAME})"
    echo "  [8] All      (pip install ${PACKAGE_NAME}[all])"
    echo "  [0] None     (install base package only — pick a backend later)"
    read -rp "Select [1]: " choice
    case "${choice:-1}" in
      0) _runtime_to_extras "" ;;
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "kiro" ;;
      7) _runtime_to_extras "copilot" ;;
      8) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode (curl | bash): install base package, skip runtime-specific setup.
    echo
    echo "  No runtime detected (non-interactive: installing base package)"
    echo "  Pick a backend afterwards with: ouroboros setup --runtime <claude|codex|opencode|hermes|gemini|kiro|copilot>"
    _runtime_to_extras ""
  fi
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
  # Map extras to explicit --with flags for uv.
  # NOTE: Pin specs MUST mirror [project.optional-dependencies] in
  # pyproject.toml. tests/unit/scripts/test_install_runtime_selection.py
  # asserts the `[all]` set covers every declared extra so silent drift
  # (e.g. forgetting `tui` / `dashboard`) is caught in CI rather than
  # discovered by a user with a half-installed `[all]` tree.
  case "$EXTRAS" in
    "[mcp,claude]")
      UV_ARGS+=(
        --with "mcp>=1.26.0,<2.0.0"
        --with "claude-agent-sdk>=0.1.0,<1.0.0"
        --with "anthropic>=0.52.0,<1.0.0"
      )
      ;;
    "[mcp]")
      UV_ARGS+=(--with "mcp>=1.26.0,<2.0.0")
      ;;
    "[all]")
      UV_ARGS+=(
        --with "mcp>=1.26.0,<2.0.0"
        --with "claude-agent-sdk>=0.1.0,<1.0.0"
        --with "anthropic>=0.52.0,<1.0.0"
        --with "litellm>=1.80.0,<=1.82.6"
        --with "textual>=1.0.0,<9.0.0"
        --with "streamlit>=1.40.0,<2.0.0"
        --with "plotly>=5.24.0,<7.0.0"
        --with "pandas>=2.2.0,<3.0.0"
      )
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
# Only apply Claude-specific integration when Claude was the selected runtime,
# or when the user explicitly asked for the all-runtimes install.
if command -v claude &>/dev/null && { [ "$RUNTIME" = "claude" ] || [ "$EXTRAS" = "[all]" ]; }; then
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
echo
if [ -n "$RUNTIME" ]; then
  echo "  Current backend: $RUNTIME"
fi
echo "  Switch backend later: ouroboros setup --runtime <claude|codex|opencode|hermes|gemini|kiro|copilot>"
