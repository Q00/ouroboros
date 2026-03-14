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
   ```bash
   python3 -c "import ouroboros; print(ouroboros.__version__)"
   ```
   If import fails, the package is not installed — skip to step 3.

2. **Check latest version on PyPI**:
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

   a. **Update PyPI package**:
   ```bash
   pip install --upgrade ouroboros-ai
   ```
   Or if `uv` is available:
   ```bash
   uv pip install --upgrade ouroboros-ai
   ```

   b. **Update runtime integration** (Claude Code only):
   ```bash
   claude plugin update ouroboros@ouroboros
   ```

   c. **Verify and update CLAUDE.md version marker**:
   ```bash
   NEW_VERSION=$(python3 -c "import ouroboros; print(ouroboros.__version__)" 2>/dev/null)
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

   If you have an MCP server running, restart it:
     ouroboros mcp serve --transport stdio

   If CLAUDE.md block content changed, regenerate it:
     ooo setup

   📍 Run `ooo help` to see what's new.
   ```

## Notes

- The update check uses PyPI as the source of truth for the latest version.
- Plugin update (Claude Code) pulls the latest from the marketplace.
- No data is lost during updates — event stores and session data are preserved.
