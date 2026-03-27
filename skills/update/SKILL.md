---
name: update
description: "Check for updates and upgrade Ouroboros to the latest version"
---

# /ouroboros:update

Check for updates and upgrade Ouroboros (PyPI package + runtime integration).

## Usage

```
ooo update
/ouroboros:update
```

**Trigger keywords:** "ooo update", "update ouroboros", "upgrade ouroboros"

## Instructions

When the user invokes this skill:

1. **Check current version**:

   First, try reading the version from the CLI binary (works for all install methods):
   ```bash
   ouroboros --version 2>/dev/null
   ```

   If that fails, try the plugin version:
   ```bash
   cat .claude-plugin/plugin.json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null
   ```

   If both fail, the package is not installed — skip to step 3.

2. **Check latest version on PyPI**:

   First, determine if the current installed version is a pre-release (contains `a`, `b`, `rc`, or `dev`).

   If the current version **is a pre-release**, scan all PyPI releases to find the latest (including betas):
   ```bash
   python3 -c "
   import json, ssl, urllib.request
   from packaging.version import Version
   ctx = ssl.create_default_context()
   data = json.loads(urllib.request.urlopen('https://pypi.org/pypi/ouroboros-ai/json', timeout=5, context=ctx).read())
   versions = [Version(v) for v in data.get('releases', {}) if data['releases'][v]]
   print(str(max(versions)) if versions else data['info']['version'])
   "
   ```

   If the current version **is stable**, use the standard latest:
   ```bash
   python3 -c "
   import json, ssl, urllib.request
   ctx = ssl.create_default_context()
   data = json.loads(urllib.request.urlopen('https://pypi.org/pypi/ouroboros-ai/json', timeout=5, context=ctx).read())
   print(data['info']['version'])
   "
   ```

3. **Compare and report**:

   If already on the latest version:
   ```
   Ouroboros is up to date (v0.X.Y)
   ```

   If a newer version is available, show:
   ```
   Update available: v0.X.Y → v0.X.Z

   Changes: https://github.com/Q00/ouroboros/releases/tag/v0.X.Z
   ```

   Then ask the user with AskUserQuestion:
   - **"Update now"** — Proceed with update
   - **"Skip"** — Do nothing

4. **Run update** (if user chose to update):

   a. **Update PyPI package** — detect the original install method and preserve `[claude]` extras:

   Check which installer was used:
   ```bash
   uv tool list 2>/dev/null | grep -q ouroboros && echo "uv"
   pipx list 2>/dev/null | grep -q ouroboros && echo "pipx"
   ```

   > This skill runs inside Claude Code, so always use `ouroboros-ai[claude]`
   > (includes `claude-agent-sdk` and `anthropic` required for MCP tools).

   - If installed via **uv tool** (most common with install.sh):
     ```bash
     # For pre-release targets:
     uv tool install --upgrade --prerelease=allow ouroboros-ai[claude]
     # For stable targets:
     uv tool install --upgrade ouroboros-ai[claude]
     ```

   - If installed via **pipx**:
     > `pipx upgrade` cannot add extras to an existing venv — use `install --force` to reinstall with extras.
     ```bash
     # For pre-release targets:
     pipx install --force --pip-args='--pre' ouroboros-ai[claude]
     # For stable targets:
     pipx install --force ouroboros-ai[claude]
     ```

   - If installed via **pip** (fallback):
     ```bash
     # For pre-release targets:
     python3 -m pip install --upgrade --pre ouroboros-ai[claude]
     # For stable targets:
     python3 -m pip install --upgrade ouroboros-ai[claude]
     ```

   > **Note**: The `[claude]` extra is critical — it installs `claude-agent-sdk` and
   > `anthropic` which are required for MCP tool execution. Omitting it causes MCP
   > tools to fail silently at call time.

   b. **Update runtime integration**:

   For Claude Code:
   ```bash
   claude plugin marketplace update ouroboros 2>/dev/null || true
   claude plugin install ouroboros@ouroboros
   ```

   For Codex CLI (re-install skills/rules to ~/.codex/):
   ```bash
   ouroboros setup --runtime codex --non-interactive
   ```

   c. **Refresh MCP server config** (fixes stale args from older versions):

   Run the same setup command used in step b to ensure MCP config is current:

   For Claude Code:
   ```bash
   ouroboros setup --runtime claude --non-interactive
   ```

   For Codex CLI (already handled by step b above — skip this step).

   This ensures `~/.claude/mcp.json` has the latest MCP command and args
   (e.g., `ouroboros-ai[claude]` extras). Skips if already up to date.

   d. **Verify and update CLAUDE.md version marker**:
   ```bash
   NEW_VERSION=$(ouroboros --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.]*')
   echo "Installed: v$NEW_VERSION"

   if [ -n "$NEW_VERSION" ] && grep -q "ooo:VERSION" CLAUDE.md 2>/dev/null; then
     OLD_VERSION=$(grep "ooo:VERSION" CLAUDE.md | sed 's/.*ooo:VERSION:\(.*\) -->/\1/' | tr -d ' ')
     if [ "$OLD_VERSION" != "$NEW_VERSION" ]; then
       sed -i.bak "s/<!-- ooo:VERSION:.*-->/<!-- ooo:VERSION:$NEW_VERSION -->/" CLAUDE.md && rm -f CLAUDE.md.bak
       echo "CLAUDE.md version marker updated: v$OLD_VERSION → v$NEW_VERSION"
     else
       echo "CLAUDE.md version marker already up to date (v$NEW_VERSION)"
     fi
   fi
   ```

   > **Note**: This only updates the version marker. If the block content itself
   > changed between versions, the user should run `ooo setup` to regenerate it.

5. **Post-update guidance**:
   ```
   Updated to v0.X.Z

   Restart your Claude Code session to apply the update.
   (Close this session and start a new one with `claude`)

   If CLAUDE.md block content changed, regenerate it:
     ooo setup

   Run `ooo help` to see what's new.
   ```

## Notes

- The update check uses PyPI as the source of truth for the latest version.
- Plugin update (Claude Code) pulls the latest from the marketplace.
- No data is lost during updates — event stores and session data are preserved.
- **Always use the same installer** that was used for the original installation (uv tool > pipx > pip).
