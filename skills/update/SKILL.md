---
name: update
description: "Check for updates and upgrade Ouroboros to the latest version"
---

# /ouroboros:update

Check for updates and upgrade Ouroboros without changing the installation's
manager, environment, or optional-dependency profile.

## Usage

```
ooo update
/ouroboros:update
```

**Trigger keywords:** "ooo update", "update ouroboros", "upgrade ouroboros"

## Instructions

When the user invokes this skill:

1. Use the native command as the single source of truth:

   ```bash
   ouroboros update --check
   ```

   - If it reports **up to date**, report that result and stop.
   - If it reports an **update available**, ask the user to choose **Update
     now** or **Skip**.
   - If the user chooses **Update now**, run:

     ```bash
     ouroboros update --yes
     ```

     When the user actively uses both Claude Code and Codex, refresh both host
     integrations without changing the configured execution backend:

     ```bash
     ouroboros update --yes --runtime all
     ```

   Show the command's result. The native updater binds the package upgrade and
   post-update setup to one receipt-backed installation identity: manager,
   environment, recorded profile, and environment-local console script.

2. If the check exits unsuccessfully but `ouroboros update --help` works,
   report the native error and stop. **Do not** replace it with a manual
   updater. In particular, do not infer ownership from global `uv tool list`,
   `pipx list`, PATH order, directory names, or the active agent runtime.

3. If `ouroboros update --help` does not work, this is a legacy installation
   that predates the receipt-bound updater. Fail closed:

   - A read-only `ouroboros --version` check is allowed.
   - Explain that the old skill cannot prove the original manager,
     environment, and extras/profile, so it will not mutate the installation.
   - Ask the user to rerun the exact original install command (including its
     `ouroboros-ai[...]` extras and custom manager root) to reach a version
     with the native updater.
   - If the user does not know that identity, recommend a fresh isolated
     install rather than guessing.
   - Do not run package upgrades, plugin refreshes, or setup commands from this
     legacy path.

4. After a successful update, relay the runtime-specific restart guidance from
   the native command. If project instruction content also needs regeneration,
   suggest `ooo setup`; do not edit project instruction files as part of the
   package update.

## Safety contract

- PyPI is the version source of truth; fully yanked releases are excluded.
- `uv` and `pipx` upgrades replay the running environment's local receipt,
  preserving base, `[tui]`, `[mcp,tui]`, `[claude,tui]`, `[all]`, and
  other recorded profiles.
- The Claude SDK and MCP 2 profiles are never combined or substituted.
- Missing or ambiguous installation identity is a non-mutating error.
- Automatic runtime refresh preserves the configured backend and the existing
  OpenCode `plugin`/`subprocess` topology instead of inferring a replacement
  from PATH.
- Runtime executable identity follows the supported environment override,
  then the persisted `orchestrator.*_cli_path`, then PATH. The chosen exact
  executable is validated and reused for plugin/setup refresh so a stale PATH
  binary cannot replace an operator-selected runtime.
- `--runtime all` refreshes the Claude and Codex plugin integrations plus
  installed runtime artifacts without changing the configured execution
  backend. Active Codex sessions still need a restart because Codex does not
  currently retain an in-use plugin generation; Claude may use
  `/reload-plugins` or restart.
- `ouroboros update` supports `--check`, `--yes`, `--dry-run`,
  `--prerelease`, and `--runtime`; see `ouroboros update --help`.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status`
when that MCP projection is available; otherwise
derive it from this skill's actual outcome. Never use a linear `Step N of M` footer
because Ouroboros is an evolutionary loop. When the next action is
genuinely a choice, list 2-3 honest options in the `next:` clause. The
breadcrumb line must be the last line of the response.
