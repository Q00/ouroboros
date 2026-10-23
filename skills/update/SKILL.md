---
name: update
description: "Check for updates and upgrade Ouroboros to the latest version"
---

# /ouroboros:update

Upgrade Ouroboros without changing the installation's manager, environment, or optional-dependency profile.

```
ooo update
/ouroboros:update
```

## Instructions

The native command is the single source of truth. Run it with the active runtime's `run_shell` capability.

1. Run `ouroboros update --check`.
   - **Up to date**: report the package result together with any Codex plugin version mismatch or unknown-status warning, then stop. Do not infer that every integration is current from the package version.
   - **Update available**: ask the user (active runtime's `ask_user` capability) to choose **Update now** or **Skip**. On **Update now**, run `ouroboros update --yes`. When the user actively uses both Claude Code and Codex, run `ouroboros update --yes --runtime all` instead; it refreshes both host integrations without changing the configured execution backend. Show the command's result.

2. If the check fails but `ouroboros update --help` works, report the native error and stop. Never substitute a manual upgrade, and never infer ownership from `uv tool list`, `pipx list`, PATH order, directory names, or the active agent runtime.

3. If `ouroboros update --help` does not work, this is a legacy install that predates the receipt-bound updater. Fail closed: only a read-only `ouroboros --version` is allowed. Explain that the original manager, environment, and extras cannot be proven, and ask the user to rerun their exact original install command (including its `ouroboros-ai[...]` extras and custom manager root), or to do a fresh isolated install if they do not know it. Run no package upgrade, plugin refresh, or setup command on this path.

4. After a successful update, relay the command's restart guidance (Claude may use `/reload-plugins` or restart; active Codex sessions need a restart) and any Codex plugin version diagnostic. A mismatch is advisory; preserve existing marketplace refs rather than automatically removing or advancing a pin. If project instruction content needs regeneration, suggest `ooo setup`; never edit project instruction files as part of the update.

The updater replays the environment's install receipt, so the Claude SDK and MCP 2 profiles are never combined or substituted. Other options: `--dry-run`, `--prerelease`; see `ouroboros update --help`.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
