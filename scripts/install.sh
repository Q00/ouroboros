#!/bin/bash
# Ouroboros installer — auto-detects runtime and installs accordingly.
# Usage: curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/release/0.26.0-beta/scripts/install.sh | bash
# TODO: Change URL back to main branch when 0.26.0 is officially released
set -euo pipefail

# TODO: Remove version pin when 0.26.0 stable is released
PACKAGE_NAME="ouroboros-ai"
VERSION="==0.26.0b1"
MIN_PYTHON="3.12"

echo "╭──────────────────────────────────────╮"
echo "│     Ouroboros Installer              │"
echo "╰──────────────────────────────────────╯"
echo

# 1. Check Python
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
    if [ -n "$ver" ] && [ "$(printf '%s\n' "$MIN_PYTHON" "$ver" | sort -V | head -n1)" = "$MIN_PYTHON" ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python >=${MIN_PYTHON} is required but not found."
  echo "Install it from https://www.python.org/downloads/"
  exit 1
fi
echo "  Python: $($PYTHON --version)"

# 2. Detect runtimes
EXTRAS=""
RUNTIME=""
if command -v codex &>/dev/null; then
  echo "  Codex:  $(which codex)"
  RUNTIME="codex"
fi
if command -v claude &>/dev/null; then
  echo "  Claude: $(which claude)"
  EXTRAS="[claude]"
  RUNTIME="${RUNTIME:-claude}"
fi

if [ -z "$RUNTIME" ]; then
  echo
  echo "No runtime CLI detected. Which runtime will you use?"
  echo "  [1] Codex   (pip install ${PACKAGE_NAME}${VERSION})"
  echo "  [2] Claude  (pip install ${PACKAGE_NAME}[claude]${VERSION})"
  echo "  [3] All     (pip install ${PACKAGE_NAME}[all]${VERSION})"
  read -rp "Select [1]: " choice
  case "${choice:-1}" in
    2) EXTRAS="[claude]"; RUNTIME="claude" ;;
    3) EXTRAS="[all]"; RUNTIME="" ;;
    *) EXTRAS=""; RUNTIME="codex" ;;
  esac
fi

# Build PEP 508 install specifier: name[extras]==version
INSTALL_SPEC="${PACKAGE_NAME}${EXTRAS}${VERSION}"

echo
echo "Installing ${INSTALL_SPEC} ..."

# 3. Install (or upgrade if already installed)
if command -v pipx &>/dev/null; then
  pipx install "$INSTALL_SPEC" 2>/dev/null \
    || pipx upgrade "$INSTALL_SPEC"
elif command -v uv &>/dev/null; then
  uv tool install --upgrade "$INSTALL_SPEC"
else
  $PYTHON -m pip install --user --upgrade "$INSTALL_SPEC"
fi

# 4. Setup
if [ -n "$RUNTIME" ]; then
  echo
  echo "Running setup..."
  ouroboros setup --runtime "$RUNTIME" --non-interactive
fi

echo
echo "Done! Get started:"
echo '  ouroboros init start "your idea here"'
