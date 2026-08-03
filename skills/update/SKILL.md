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

1. **Prefer the native CLI command** (single source of truth for the update flow):

   ```bash
   ouroboros update --check
   ```

   If the command succeeds, show its output to the user.

   - If it reports **up to date**, you are done — skip to step 4.
   - If it reports an **update available**, ask the user with AskUserQuestion:
     - **"Update now"** — run the update
     - **"Skip"** — do nothing

   If the user chose to update, run:

   ```bash
   ouroboros update --yes
   ```

   The CLI performs the full flow natively: PyPI version check, package
   upgrade with the original installer (uv tool > pipx > pip, preserving the
   `[claude]` extra), Claude Code plugin refresh, and
   `ouroboros setup --non-interactive` for the detected runtime. When it
   finishes, continue at step 3.

   If `ouroboros update` is **not available** (the installed binary predates
   the command), fall back to the manual procedure in step 2.

2. **Manual fallback (older installs only)**:

   a. **Check current version**:

   ```bash
   ouroboros --version 2>/dev/null
   ```

   If that fails, try the plugin version:

   ```bash
   cat .claude-plugin/plugin.json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null
   ```

   b. **Check latest version on PyPI**:

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

   c. **Compare and report**. If already on the latest version:

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

   d. **Update PyPI package** (if user chose to update) — detect the original
   install method and preserve the standalone `[claude]` profile:

   Check which installer was used:

   ```bash
   uv tool list 2>/dev/null | grep -q ouroboros && echo "uv"
   pipx list 2>/dev/null | grep -q ouroboros && echo "pipx"
   ```

   > This skill runs inside Claude Code, so use `ouroboros-ai[claude]`.
   > Never combine it with `[mcp]`: the current Claude Agent SDK embeds MCP 1.x,
   > while the protocol server requires MCP 2. Supported MCP hosts launch their
   > own isolated `ouroboros-ai[mcp]` process through `uvx` or `pipx run`.

   - If installed via **uv tool** (most common with install.sh):
     ```bash
     # For pre-release targets:
     uv tool install --upgrade --prerelease=allow 'ouroboros-ai[claude]'
     # For stable targets:
     uv tool install --upgrade 'ouroboros-ai[claude]'
     ```

   - If installed via **pipx**:
     > `pipx upgrade` cannot add extras to an existing venv — use `install --force` to reinstall with extras.
     ```bash
     # For pre-release targets:
     pipx install --force --pip-args='--pre' 'ouroboros-ai[claude]'
     # For stable targets:
     pipx install --force 'ouroboros-ai[claude]'
     ```

   - If installed via **pip** (fallback):
     ```bash
     # For pre-release targets:
     python3 -m pip install --upgrade --pre 'ouroboros-ai[claude]'
     # For stable targets:
     python3 -m pip install --upgrade 'ouroboros-ai[claude]'
     ```

   e. **Update runtime integration**:

   For Claude Code:

   ```bash
   claude plugin marketplace update ouroboros 2>/dev/null || true
   claude plugin install ouroboros@ouroboros
   ```

   For Codex CLI (re-install skills/rules to ~/.codex/):

   ```bash
   ouroboros setup --runtime codex --non-interactive
   ```

   f. **Refresh runtime config** (Claude Code only — Codex is already handled
   by step e):

   ```bash
   ouroboros setup --runtime claude --non-interactive
   ```

   Standalone Claude setup leaves `~/.claude/mcp.json` untouched. It refreshes
   the Claude runtime/LLM settings and preserves the MCP 1.x / MCP 2 boundary.

3. **Verify and update CLAUDE.md version marker** (the CLI does not touch
   project files):

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

4. **Post-update guidance**:

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
- `ouroboros update` supports `--check`, `--yes`, `--dry-run`, `--prerelease`,
  and `--runtime` — see `ouroboros update --help`.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
