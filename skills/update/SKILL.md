---
name: update
description: "Check for updates and upgrade Ouroboros to the latest version"
---

# /ouroboros:update

Check for updates and upgrade Ouroboros (PyPI package + Claude Code plugin).

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

   b. **Update Claude Code plugin**:
   ```bash
   claude plugin update ouroboros@ouroboros
   ```

   c. **Verify**:
   ```bash
   python3 -c "import ouroboros; print(ouroboros.__version__)"
   ```

   d. **Update CLAUDE.md version marker** (if present):

   Check if the current working directory has a `CLAUDE.md` with an Ouroboros block:
   ```bash
   grep -q "ooo:VERSION" CLAUDE.md 2>/dev/null && echo "HAS_BLOCK" || echo "NO_BLOCK"
   ```

   If `HAS_BLOCK`:
   1. Extract the old version from the marker:
      ```bash
      grep "ooo:VERSION" CLAUDE.md | sed 's/.*ooo:VERSION:\(.*\) -->/\1/'
      ```
   2. If the old version differs from the newly installed version, replace the marker:
      ```bash
      sed -i.bak "s/<!-- ooo:VERSION:.*-->/<!-- ooo:VERSION:$NEW_VERSION -->/" CLAUDE.md
      ```
   3. Report: `CLAUDE.md version marker updated: v{old} → v{new}`

   If `NO_BLOCK`, skip silently.

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
- Plugin update pulls the latest from the Claude Code marketplace.
- No data is lost during updates — event stores and session data are preserved.
