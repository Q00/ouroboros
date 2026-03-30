#!/usr/bin/env bash
# dev-sync.sh — sync workspace skills/agents to installed plugin cache
#
# Usage:
#   ./scripts/dev-sync.sh          # sync to all cached versions
#   ./scripts/dev-sync.sh 0.25.1   # sync to a specific version only
#
# Run this after editing skills/ or agents/ to pick up changes immediately
# in Claude Code without bumping the plugin version.

set -euo pipefail

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
CACHE_BASE="$HOME/.claude/plugins/cache/ouroboros/ouroboros"

if [ ! -d "$CACHE_BASE" ]; then
  echo "No plugin cache found at $CACHE_BASE — is ouroboros installed?"
  exit 1
fi

TARGET_VERSION="${1:-}"

sync_to_version() {
  local version="$1"
  local dest="$CACHE_BASE/$version"

  if [ ! -d "$dest" ]; then
    echo "  SKIP  $version (cache dir not found)"
    return
  fi

  for dir in skills agents hooks; do
    src="$WORKSPACE/$dir"
    if [ ! -d "$src" ]; then
      continue
    fi
    rsync -a --delete "$src/" "$dest/$dir/"
    echo "  SYNC  $version/$dir"
  done
}

if [ -n "$TARGET_VERSION" ]; then
  sync_to_version "$TARGET_VERSION"
else
  for version_dir in "$CACHE_BASE"/*/; do
    version="$(basename "$version_dir")"
    # skip .bak dirs
    if [[ "$version" == *.bak ]]; then
      continue
    fi
    sync_to_version "$version"
  done
fi

echo ""
echo "Done. Restart the MCP server in Claude Code to pick up changes."
