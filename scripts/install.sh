#!/bin/bash
# Ouroboros installer — auto-detects runtime and installs accordingly.
# Usage: curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
#
# Runtime selection (first match wins):
#   1. OUROBOROS_INSTALL_RUNTIME env var
#      (claude|codex|opencode|hermes|gemini|kiro|copilot|pi|all)
#   2. Existing ~/.ouroboros/config.yaml runtime — preserved on upgrade
#      unless OUROBOROS_INSTALL_RECONFIGURE=1 (or --reconfigure flag) is set.
#   3. Interactive prompt when stdin is a TTY.
#   4. Auto-detect single CLI on PATH; default to claude in pipe mode.
set -euo pipefail

PACKAGE_NAME="ouroboros-ai"
CLICK_SPEC="click>=8.1.0,<9.0.0"
MIN_PYTHON="3.12"
IS_LOCAL=false
RECONFIGURE="${OUROBOROS_INSTALL_RECONFIGURE:-}"
EXPLICIT_RUNTIME="${OUROBOROS_INSTALL_RUNTIME:-}"

if [ -z "${NO_COLOR:-}" ] && { [ -t 1 ] || [ -n "${FORCE_COLOR:-}" ]; } && { [ "${TERM:-}" != "dumb" ] || [ -n "${FORCE_COLOR:-}" ]; }; then
  BOLD="$(printf '\033[1m')"
  DIM="$(printf '\033[2m')"
  GREEN="$(printf '\033[1;32m')"
  YELLOW="$(printf '\033[1;33m')"
  BLUE="$(printf '\033[1;34m')"
  CYAN="$(printf '\033[1;36m')"
  MAGENTA="$(printf '\033[1;35m')"
  RED="$(printf '\033[1;31m')"
  RESET="$(printf '\033[0m')"
else
  BOLD=""
  DIM=""
  GREEN=""
  YELLOW=""
  BLUE=""
  CYAN=""
  MAGENTA=""
  RED=""
  RESET=""
fi

_say() {
  printf '%s\n' "$*"
}

_blank() {
  printf '\n'
}

_banner() {
  _say "${MAGENTA}╭────────────────────────────────────────────╮${RESET}"
  _say "${MAGENTA}│${RESET} ${BOLD}${CYAN}Ouroboros installer${RESET}                         ${MAGENTA}│${RESET}"
  _say "${MAGENTA}│${RESET} ${DIM}Specification-first AI development${RESET}          ${MAGENTA}│${RESET}"
  _say "${MAGENTA}╰────────────────────────────────────────────╯${RESET}"
  _say "${DIM}Installs the CLI, chooses an agent backend, and wires up local skills.${RESET}"
}

_step() {
  _blank
  _say "${BLUE}◆${RESET} ${BOLD}$1${RESET}"
  if [ "${2:-}" != "" ]; then
    _say "  ${DIM}$2${RESET}"
  fi
}

_ok() {
  _say "  ${GREEN}✓${RESET} $1"
}

_warn() {
  _say "  ${YELLOW}!${RESET} $1"
}

_err() {
  _say "  ${RED}✗${RESET} $1"
}

_info() {
  _say "  ${DIM}•${RESET} $1"
}

_choice() {
  printf '  %b[%s]%b %-8s %s\n' "$BOLD" "$1" "$RESET" "$2" "$3"
}

_prompt() {
  printf '%b' "${BOLD}$1${RESET}"
}

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

# Auto-detect: if PyPI's info.version response is a pre-release, allow
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

_banner

# 1. Detect installer: uv > pipx > pip (determines Python requirement)
HAS_UV=false
HAS_PIPX=false
PYTHON=""

_step "1/4  Checking Python tooling" "Prefer uv, then pipx, then pip."

if command -v uv &>/dev/null; then
  HAS_UV=true
  _ok "uv found: $(uv --version)"
elif command -v pipx &>/dev/null; then
  HAS_PIPX=true
  _ok "pipx found: $(pipx --version)"
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
      _blank
      _err "pipx requires Python >=${MIN_PYTHON}, but none was found."
      _blank
      _say "${BOLD}Install one of these, then run the installer again:${RESET}"
      _info "uv: pipx install uv"
      _info "uv: pip install --user uv"
      _info "uv: brew install uv"
      _info "uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
      _info "Python ${MIN_PYTHON}+: https://www.python.org/downloads/"
      exit 1
    fi
    _ok "Python found: $($PYTHON --version)"
  else
    # pip fallback: any matching python3/python will do.
    for cmd in python3 python; do
      if command -v "$cmd" &>/dev/null && _python_ok "$cmd"; then
        PYTHON="$cmd"
        break
      fi
    done
    if [ -z "$PYTHON" ]; then
      _blank
      _err "No installer found: uv, pipx, or Python >=${MIN_PYTHON}."
      _blank
      _say "${BOLD}Install one of these, then run the installer again:${RESET}"
      _info "uv: pipx install uv"
      _info "uv: pip install --user uv"
      _info "uv: brew install uv"
      _info "uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
      _info "Python ${MIN_PYTHON}+: https://www.python.org/downloads/"
      exit 1
    fi
    _ok "Python found: $($PYTHON --version)"
  fi
fi

# 2. Detect runtimes
_step "2/4  Choosing an agent backend" "Codex, Claude, Hermes, OpenCode, Gemini, Kiro, Copilot, and Pi are supported."
EXTRAS=""
RUNTIME=""
HAS_CODEX=false
HAS_CLAUDE=false
HAS_HERMES=false
HAS_OPENCODE=false
HAS_GEMINI=false
HAS_KIRO=false
HAS_COPILOT=false
HAS_PI=false
if command -v codex &>/dev/null; then
  _ok "Codex found: $(which codex)"
  HAS_CODEX=true
fi
if command -v claude &>/dev/null; then
  _ok "Claude found: $(which claude)"
  HAS_CLAUDE=true
fi
if command -v hermes &>/dev/null; then
  _ok "Hermes found: $(which hermes)"
  HAS_HERMES=true
fi
if command -v opencode &>/dev/null; then
  _ok "OpenCode found: $(which opencode)"
  HAS_OPENCODE=true
fi
if command -v gemini &>/dev/null; then
  _ok "Gemini found: $(which gemini)"
  HAS_GEMINI=true
fi
if command -v kiro-cli &>/dev/null; then
  _ok "Kiro found: $(which kiro-cli)"
  HAS_KIRO=true
fi
if command -v copilot &>/dev/null; then
  _ok "Copilot found: $(which copilot)"
  HAS_COPILOT=true
fi
if command -v pi &>/dev/null; then
  _ok "Pi: $(which pi)"
  HAS_PI=true
fi

RUNTIME_COUNT=0
[ "$HAS_CLAUDE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_CODEX" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_HERMES" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_OPENCODE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_GEMINI" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_KIRO" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_COPILOT" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_PI" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))

# Map a runtime name to (EXTRAS, RUNTIME) pair.
# Used after explicit/preserved runtime resolution to derive install extras.
# Keep this table boring and explicit: agents reading this installer should be
# able to see why Claude/Hermes pull MCP dependencies while file-based runtimes
# only need the base CLI.
_runtime_to_extras() {
  # `tui` ships with every selection so the settings GUI
  # (`ouroboros config`) works out of the box (#1414).
  case "$1" in
    claude)  EXTRAS="[mcp,claude,tui]"; RUNTIME="claude" ;;
    codex)   EXTRAS="[tui]"; RUNTIME="codex" ;;
    opencode) EXTRAS="[tui]"; RUNTIME="opencode" ;;
    hermes)  EXTRAS="[mcp,tui]"; RUNTIME="hermes" ;;
    gemini)  EXTRAS="[tui]"; RUNTIME="gemini" ;;
    kiro)    EXTRAS="[tui]"; RUNTIME="kiro" ;;
    copilot) EXTRAS="[tui]"; RUNTIME="copilot" ;;
    pi)      EXTRAS="[tui]"; RUNTIME="pi" ;;
    all)     EXTRAS="[all]"; RUNTIME="" ;;
    "")      EXTRAS="[tui]"; RUNTIME="" ;;
    *)
      _err "unsupported runtime '$1'"
      _info "Expected one of: claude, codex, opencode, hermes, gemini, kiro, copilot, pi, all"
      exit 1
      ;;
  esac
}

# Try to read the previously-configured runtime from ~/.ouroboros/config.yaml.
# Preserves user choice across upgrades unless --reconfigure / --runtime is set.
EXISTING_RUNTIME=""
EXISTING_CONFIG="$HOME/.ouroboros/config.yaml"
# Fresh install (no config yet) → the post-install settings-GUI offer
# defaults to yes; upgrades default to no.
FRESH_CONFIG=true
[ -f "$EXISTING_CONFIG" ] && FRESH_CONFIG=false
if [ -z "$EXPLICIT_RUNTIME" ] && [ -z "$RECONFIGURE" ] && [ -f "$EXISTING_CONFIG" ] && command -v python3 &>/dev/null; then
  EXISTING_RUNTIME=$(EXISTING_CONFIG="$EXISTING_CONFIG" python3 -c "
import os, re
supported = {'claude', 'codex', 'opencode', 'hermes', 'gemini', 'kiro', 'copilot', 'pi'}
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
  _blank
  _ok "Runtime: $EXPLICIT_RUNTIME (from --runtime / OUROBOROS_INSTALL_RUNTIME)"
  _runtime_to_extras "$EXPLICIT_RUNTIME"
elif [ -n "$EXISTING_RUNTIME" ]; then
  _blank
  _ok "Runtime: $EXISTING_RUNTIME (preserved from $EXISTING_CONFIG)"
  _info "Re-run with --reconfigure to choose again."
  _runtime_to_extras "$EXISTING_RUNTIME"
elif [ "$RUNTIME_COUNT" -gt 1 ]; then
  if [ -t 0 ]; then
    _blank
    _say "${BOLD}Multiple runtimes detected. Pick where Ouroboros should appear first:${RESET}"
    _choice 1 "Claude" "Claude Code plugin + MCP server (${PACKAGE_NAME}[mcp,claude,tui])"
    _choice 2 "Codex" "Codex plugin artifacts (${PACKAGE_NAME}[tui])"
    _choice 3 "Hermes" "Hermes agent guides + MCP server (${PACKAGE_NAME}[mcp,tui])"
    _choice 4 "OpenCode" "OpenCode commands and agent files (${PACKAGE_NAME}[tui])"
    _choice 5 "Gemini" "Gemini CLI integration (${PACKAGE_NAME}[tui])"
    _choice 6 "Kiro" "Kiro CLI integration (${PACKAGE_NAME}[tui])"
    _choice 7 "Copilot" "GitHub Copilot integration (${PACKAGE_NAME}[tui])"
    _choice 8 "Pi" "Pi CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 9 "All" "Install every optional integration (${PACKAGE_NAME}[all])"
    _prompt "Select [1]: "
    read -r choice
    case "${choice:-1}" in
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "kiro" ;;
      7) _runtime_to_extras "copilot" ;;
      8) _runtime_to_extras "pi" ;;
      9) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode: default to claude when multiple runtimes exist
    _warn "Multiple runtimes detected in non-interactive mode; defaulting to Claude."
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
elif [ "$HAS_PI" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "pi"
else
  # No runtime CLI on PATH yet — first install. Always prompt when interactive
  # so the user picks deliberately rather than silently defaulting to claude.
  if [ -t 0 ]; then
    _blank
    _say "${BOLD}No runtime CLI detected yet. Choose the agent you plan to use:${RESET}"
    _choice 1 "Claude" "Recommended: plugin + MCP server (${PACKAGE_NAME}[mcp,claude,tui])"
    _choice 2 "Codex" "Codex plugin artifacts (${PACKAGE_NAME}[tui])"
    _choice 3 "Hermes" "Hermes agent guides + MCP server (${PACKAGE_NAME}[mcp,tui])"
    _choice 4 "OpenCode" "OpenCode commands and agent files (${PACKAGE_NAME}[tui])"
    _choice 5 "Gemini" "Gemini CLI integration (${PACKAGE_NAME}[tui])"
    _choice 6 "Kiro" "Kiro CLI integration (${PACKAGE_NAME}[tui])"
    _choice 7 "Copilot" "GitHub Copilot integration (${PACKAGE_NAME}[tui])"
    _choice 8 "Pi" "Pi CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 9 "All" "Install every optional integration (${PACKAGE_NAME}[all])"
    _choice 0 "None" "Base CLI only; choose a backend later"
    _prompt "Select [1]: "
    read -r choice
    case "${choice:-1}" in
      0) _runtime_to_extras "" ;;
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "kiro" ;;
      7) _runtime_to_extras "copilot" ;;
      8) _runtime_to_extras "pi" ;;
      9) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode (curl | bash): install base package, skip runtime-specific setup.
    _blank
    _warn "No runtime detected in non-interactive mode; installing the base package."
    _info "Pick a backend afterwards with: ouroboros setup --runtime <claude|codex|opencode|hermes|gemini|kiro|copilot|pi>"
    _runtime_to_extras ""
  fi
fi

if [ -n "$RUNTIME" ]; then
  _ok "Selected backend: $RUNTIME"
elif [ "$EXTRAS" = "[all]" ]; then
  _ok "Selected backend: all"
else
  _info "Selected backend: none yet"
fi

INSTALL_SPEC="${PACKAGE_NAME}${EXTRAS}"

_step "3/4  Installing Ouroboros" "Package: ${INSTALL_SPEC}"
_say "Installing ${INSTALL_SPEC} ..."

# 3. Install (or upgrade if already installed)
# uv tool install has issues with [extras] syntax — use --with for reliability.
INSTALL_METHOD=""
if [ "$HAS_UV" = true ]; then
  INSTALL_METHOD="uv"
  _info "Install method: uv tool install"
  # `click` is also declared in pyproject, but keep this explicit so the
  # installer can repair already-published wheels whose metadata missed it.
  UV_ARGS=(tool install --upgrade --python ">=3.12" "$PACKAGE_NAME" --with "$CLICK_SPEC")
  if [ -n "$PRE_FLAG" ]; then
    UV_ARGS+=(--prerelease=allow)
  fi
  # Map extras to explicit --with flags for uv.
  # NOTE: Pin specs MUST mirror [project.optional-dependencies] in
  # pyproject.toml. tests/unit/scripts/test_install_runtime_selection.py
  # asserts the `[all]` set covers every declared extra so silent drift
  # (e.g. forgetting `tui`) is caught in CI rather than discovered by a
  # user with a half-installed `[all]` tree.
  case "$EXTRAS" in
    "[mcp,claude,tui]")
      UV_ARGS+=(
        --with "mcp==1.27.2"
        --with "claude-agent-sdk==0.2.101"
        --with "anthropic==0.109.1"
      )
      ;;
    "[mcp,tui]")
      UV_ARGS+=(--with "mcp==1.27.2")
      ;;
    "[all]")
      UV_ARGS+=(
        --with "mcp==1.27.2"
        --with "claude-agent-sdk==0.2.101"
        --with "anthropic==0.109.1"
        --with "litellm==1.89.0"
      )
      ;;
  esac
  # Every install ships the settings GUI (`ouroboros config`).
  UV_ARGS+=(
    --with "textual==8.2.7"
    --with "textual-serve==1.1.3"
  )
  uv "${UV_ARGS[@]}"
elif [ "$HAS_PIPX" = true ]; then
  INSTALL_METHOD="pipx"
  _info "Install method: pipx"
  if [ -n "$PRE_FLAG" ]; then
    pipx install --force --python "$PYTHON" --pip-args='--pre' "$INSTALL_SPEC"
  else
    pipx install --force --python "$PYTHON" "$INSTALL_SPEC"
  fi
  # The venv name is the distribution name even when installing from a local path.
  pipx inject "ouroboros-ai" "$CLICK_SPEC"
else
  INSTALL_METHOD="pip"
  _info "Install method: pip --user"
  if [ -n "$PRE_FLAG" ]; then
    $PYTHON -m pip install --user --upgrade --pre "$INSTALL_SPEC" "$CLICK_SPEC"
  else
    $PYTHON -m pip install --user --upgrade "$INSTALL_SPEC" "$CLICK_SPEC"
  fi
fi

# Ensure setup runs the freshly-installed uv tool binary rather than a stale
# command already on PATH. uv can install into UV_TOOL_BIN_DIR or its configured
# tool bin directory without updating the current shell's PATH, so remember the
# fresh executable and invoke that exact path below. For pipx/pip installs,
# preserve the existing PATH command unless no ouroboros command is visible.
OUROBOROS_BIN=""
_prepend_path_if_ouroboros() {
  local candidate="$1"
  if [ -n "$candidate" ] && [ -x "$candidate/ouroboros" ]; then
    export PATH="$candidate:$PATH"
    if [ -z "$OUROBOROS_BIN" ]; then
      OUROBOROS_BIN="$candidate/ouroboros"
    fi
    return 0
  fi
  return 1
}

if [ "$INSTALL_METHOD" = "uv" ]; then
  _prepend_path_if_ouroboros "${UV_TOOL_BIN_DIR:-}" || true
  if command -v uv &>/dev/null; then
    UV_TOOL_BIN="$(uv tool dir --bin 2>/dev/null || true)"
    _prepend_path_if_ouroboros "$UV_TOOL_BIN" || true
  fi
fi

if [ -z "$OUROBOROS_BIN" ] && ! command -v ouroboros &>/dev/null; then
  for p in "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/bin"; do
    _prepend_path_if_ouroboros "$p" || true
  done
fi

# 4. Setup (ouroboros CLI configures runtime-specific integration)
_step "4/4  Wiring local integrations" "Creates config and runtime-specific files when a backend was selected."
if [ -n "$RUNTIME" ] && { [ -n "$OUROBOROS_BIN" ] || command -v ouroboros &>/dev/null; }; then
  OUROBOROS_SETUP_CMD="${OUROBOROS_BIN:-ouroboros}"
  _info "Running: $OUROBOROS_SETUP_CMD setup --runtime $RUNTIME --non-interactive"
  "$OUROBOROS_SETUP_CMD" setup --runtime "$RUNTIME" --non-interactive || true
elif [ -n "$RUNTIME" ]; then
  _warn "ouroboros command is not on PATH yet; run setup after your shell sees the installed binary."
else
  _info "No backend selected; skipping runtime setup."
fi

# 5. Claude Code integration (MCP + skills)
# Only apply Claude-specific integration when Claude was the selected runtime,
# or when the user explicitly asked for the all-runtimes install.
if command -v claude &>/dev/null && { [ "$RUNTIME" = "claude" ] || [ "$EXTRAS" = "[all]" ]; }; then
  _blank
  _say "${BLUE}◆${RESET} ${BOLD}Claude Code extras${RESET}"

  # 5a. Register MCP server in ~/.claude/mcp.json
  # (ouroboros setup may have done this already, but we ensure it with timeout)
  MCP_FILE="$HOME/.claude/mcp.json"
  mkdir -p "$HOME/.claude"

  # MCP command matches the installer that actually ran in step 3
  if [ "$INSTALL_METHOD" = "uv" ]; then
    case "$EXTRAS" in
      "[mcp,claude]" | "[mcp,claude,tui]")
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
        _ok "MCP merged into existing $MCP_FILE"
      else
        _warn "MCP could not merge; check $MCP_FILE manually."
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
        _ok "MCP created at $MCP_FILE"
      else
        _warn "MCP could not create; check $MCP_FILE manually."
      fi
    fi
  else
    _warn "MCP skipped: no python3 found. Add the entry manually to $MCP_FILE."
  fi

  # 5b. Install/update Ouroboros skills (claude plugin)
  _info "Installing Ouroboros skills via Claude plugin marketplace..."
  claude plugin marketplace add Q00/ouroboros 2>/dev/null || true
  claude plugin marketplace update ouroboros 2>/dev/null || true
  if claude plugin install ouroboros@ouroboros 2>/dev/null; then
    _ok "Skills installed"
  else
    _warn "Skills skipped. Manual install: claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros"
  fi
fi

_blank
_say "${GREEN}${BOLD}Done! Ouroboros is ready.${RESET}"
_blank
_say "${BOLD}Get started${RESET}"
_info 'Open your AI coding agent and run: > ooo interview "your idea here"'
_info 'Or from the terminal: ouroboros init start "your idea here"'
if [ -n "$RUNTIME" ]; then
  _info "Current backend: $RUNTIME"
fi
_info "Switch backend later: ouroboros setup --runtime <claude|codex|opencode|hermes|gemini|kiro|copilot|pi>"
_say "${BOLD}Settings GUI — pick per-stage agents & models${RESET}"
_info 'Inside your AI agent: > ooo config   (opens in your browser)'
_info 'From this terminal:  ouroboros config   (full-screen TUI)'

# 6. Optional first-run settings GUI (interactive installs only).
# install.sh always runs in a real terminal when interactive, so the
# full-screen Textual settings app can open right here. curl|bash pipe
# installs skip this automatically ([ -t 0 ] is false).
if [ -t 0 ] && [ -z "${OUROBOROS_INSTALL_SKIP_CONFIG_GUI:-}" ]; then
  if [ "$FRESH_CONFIG" = true ]; then
    GUI_DEFAULT="y"
    GUI_HINT="[Y/n]"
  else
    GUI_DEFAULT="n"
    GUI_HINT="[y/N]"
  fi
  _blank
  _say "${BOLD}First-time setup: pick per-stage agents & models in the settings GUI?${RESET}"
  _prompt "Open settings GUI now $GUI_HINT: "
  read -r gui_choice
  case "${gui_choice:-$GUI_DEFAULT}" in
    y | Y | yes | YES)
      GUI_OK=false
      if [ -n "${OUROBOROS_SETUP_CMD:-}" ] && "$OUROBOROS_SETUP_CMD" config; then
        GUI_OK=true
      elif command -v uvx &>/dev/null; then
        # The selected extras may not include [tui]; run the GUI from an
        # ephemeral env with the tui extra instead of fattening the install.
        _info "Settings GUI needs the tui extra; running it via uvx..."
        if uvx --from "${PACKAGE_NAME}[tui]" ouroboros config; then
          GUI_OK=true
        fi
      fi
      if [ "$GUI_OK" = false ]; then
        _warn "Could not open the settings GUI."
        _info "Run it later with: uvx --from '${PACKAGE_NAME}[tui]' ouroboros config"
      fi
      ;;
    *)
      _info "Skipped. Open it anytime:"
      _info '  in your AI agent: > ooo config'
      _info '  in a terminal:    ouroboros config'
      ;;
  esac
fi
