#!/usr/bin/env bash
# Bump the Homebrew formula to a released PyPI version.
#
# The formula vendors its full dependency graph as `resource` blocks, so a
# version bump is not just a url/sha256 swap: the resources must be regenerated
# against the same extras the installer selects. `scripts/install.sh` defaults
# MCP v2-compatible runtimes to `ouroboros-ai[mcp,tui]`; the formula tracks that
# same profile so a `brew install` and an `install.sh` install expose the same
# feature set. `[claude]`/`[claude-sdk]` stay out on purpose — they pin the
# isolated MCP 1.x graph that must never share an interpreter with `[mcp]`.
#
# `brew update-python-resources` only accepts a formula brew can resolve, so
# the formula is addressed by its tap-qualified name and the file path is
# derived from it rather than passed in.
#
# Usage: bump-homebrew-formula.sh <version> <tap-qualified-formula>

set -euo pipefail

VERSION="${1:?usage: bump-homebrew-formula.sh <version> <tap-qualified-formula>}"
FORMULA_REF="${2:?usage: bump-homebrew-formula.sh <version> <tap-qualified-formula>}"

PACKAGE_NAME="ouroboros-ai"
# Keep in sync with the installer's default profile (scripts/install.sh).
FORMULA_EXTRAS="${FORMULA_EXTRAS:-mcp,tui}"

if ! command -v brew >/dev/null 2>&1; then
  echo "brew is required to resolve and regenerate the formula" >&2
  exit 1
fi

FORMULA="$(brew formula "$FORMULA_REF")"
if [ ! -f "$FORMULA" ]; then
  echo "formula not found for $FORMULA_REF: $FORMULA" >&2
  exit 1
fi
echo "==> Formula $FORMULA_REF -> $FORMULA"

# Resolve the sdist that Homebrew must build from. Waits for PyPI to serve the
# freshly published release: `uv publish` returns before the file is visible on
# the JSON API, and a bump that races it would pin the previous version.
echo "==> Resolving ${PACKAGE_NAME} ${VERSION} sdist on PyPI"
# Network goes through curl, not Python: a bare `python3` is not guaranteed to
# carry a usable CA bundle (macOS system Python does not), and this must work on
# a runner as well as a maintainer's laptop.
fetch_sdist() {
  curl --fail --silent --show-error --location --max-time 30 \
    "https://pypi.org/pypi/${PACKAGE_NAME}/${VERSION}/json" |
    python3 -c '
import json, sys
payload = json.load(sys.stdin)
sdists = [u for u in payload["urls"] if u["packagetype"] == "sdist"]
if len(sdists) != 1:
    raise SystemExit(f"expected exactly one sdist, found {len(sdists)}")
print(json.dumps({"url": sdists[0]["url"], "sha256": sdists[0]["digests"]["sha256"]}))
'
}

SDIST_JSON=""
DEADLINE=$(( $(date +%s) + 600 ))
while :; do
  if SDIST_JSON="$(fetch_sdist 2>/dev/null)" && [ -n "$SDIST_JSON" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "${PACKAGE_NAME} ${VERSION} not resolvable on PyPI after 600s" >&2
    fetch_sdist >/dev/null || true
    exit 1
  fi
  echo "    waiting for PyPI to serve ${VERSION}"
  sleep 10
done

SDIST_URL="$(printf '%s' "$SDIST_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
SDIST_SHA="$(printf '%s' "$SDIST_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])')"
echo "    url    $SDIST_URL"
echo "    sha256 $SDIST_SHA"

# Rewrite only the package's own url/sha256. Every later pair belongs to a
# vendored `resource`, so the edit stops at the first `resource` block.
echo "==> Updating formula stable URL"
FORMULA="$FORMULA" SDIST_URL="$SDIST_URL" SDIST_SHA="$SDIST_SHA" python3 - <<'PY'
import os
import re
from pathlib import Path

formula = Path(os.environ["FORMULA"])
text = formula.read_text(encoding="utf-8")

head, marker, tail = text.partition("  resource ")
if not marker:
    raise SystemExit("formula has no resource blocks; refusing to guess its layout")

head, url_count = re.subn(
    r'^(\s*)url "[^"]*"',
    lambda m: f'{m.group(1)}url "{os.environ["SDIST_URL"]}"',
    head,
    count=1,
    flags=re.MULTILINE,
)
head, sha_count = re.subn(
    r'^(\s*)sha256 "[^"]*"',
    lambda m: f'{m.group(1)}sha256 "{os.environ["SDIST_SHA"]}"',
    head,
    count=1,
    flags=re.MULTILINE,
)
if url_count != 1 or sha_count != 1:
    raise SystemExit(f"expected one url and one sha256 before resources, got {url_count}/{sha_count}")

formula.write_text(head + marker + tail, encoding="utf-8")
PY

# Regenerate the vendored graph for the tracked extras. Without
# --ignore-main-package-cooldown this fails on a release published minutes ago.
echo "==> Regenerating resource blocks for ${PACKAGE_NAME}[${FORMULA_EXTRAS}]"
brew update-python-resources \
  --version "$VERSION" \
  --package-name "${PACKAGE_NAME}[${FORMULA_EXTRAS}]" \
  --ignore-main-package-cooldown \
  "$FORMULA_REF"

echo "==> Done: $(grep -c '^  resource' "$FORMULA") resources"
