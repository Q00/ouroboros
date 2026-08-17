# dsh-ouroboros

Mount [Ouroboros](https://github.com/Q00/ouroboros) — a spec-first AI dev
workflow engine (Socratic interview → Seed spec → execute → evaluate → evolve)
— into [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) as
native tools. Once installed, type `ooo interview <goal>` or `ooo auto <goal>`
directly in the dsh chat — the model finds and calls the matching
`mcp__ouroboros__*` tool on its own.

This is a **config-only bundle**: it contains no custom plugin code, just one
row that mounts dsh's existing `@deepseek-ai/dsh-mcp-client` against
`ouroboros mcp serve`.

## Requirements

- [`uv`](https://astral.sh/uv) on `PATH`. Nothing else — `uvx` fetches and
  runs Ouroboros in an isolated environment on first launch, no `pip install`
  step.
- Python >= 3.12 (whatever `uv` resolves).
- An Ouroboros-supported agent runtime available (Claude Code, Codex CLI,
  OpenCode, ...) for `ooo auto`'s execution step. Run `ouroboros setup` once
  yourself, or set `OUROBOROS_AGENT_RUNTIME` (see below) — an MCP subprocess
  can't host the in-process `claude` SDK runtime, so this needs an executable
  one (`claude-cli`, `codex`, `opencode`, ...).

## Install

This bundle lives inside the main [Ouroboros](https://github.com/Q00/ouroboros)
repository as a subdirectory (it has no independent release cadence or code of
its own), so install it straight from GitHub with pnpm's subdirectory syntax:

```sh
dsh plugin --profile <your-profile> add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"
```

Then boot as usual (`dsh --profile <your-profile>`, or `dsh web` if your
profile is named `web`). `dsh --profile <your-profile> --dump-config` shows
the `# == dsh-ouroboros` layer once it's composed.

## Configuration

Set these as environment variables before launching `dsh` (they pass straight
through to the spawned `ouroboros mcp serve` process):

| Variable | Purpose |
|---|---|
| `OUROBOROS_AGENT_RUNTIME` | Agent runtime for `ooo auto`'s execution step (`claude-cli`, `codex`, `opencode`, ...). Leave unset if you've already run `ouroboros setup` and picked a default. |
| `OUROBOROS_LLM_BACKEND` | LLM backend for interview/Seed/QA. Set to `dsh` to route those calls back through this same DeepSeek Harness install instead of whatever Ouroboros defaults to. |

Override any field — timeout, args, a pinned Ouroboros version — from your own
profile's `cordis.patch.yml` by targeting the `mcp-ouroboros` row id; see
["Package and install a plugin"](https://github.com/deepseek-ai/deepseek-harness/blob/main/docs/user/develop/basic/publish.md)
for the override mechanics.

By default, connection failures are non-fatal (`failOnStartupError: false`):
a machine without `uv` on `PATH` yet still boots dsh normally, just without
the Ouroboros tools, and retries on reconnect per dsh's usual mcp-client
backoff.

## What you get

36 tools under the `ouroboros` namespace — `mcp__ouroboros__ouroboros_interview`,
`mcp__ouroboros__ouroboros_auto`, `mcp__ouroboros__ouroboros_evaluate`,
`mcp__ouroboros__ouroboros_ralph`, and more. Each carries its own description,
so a plain `ooo interview: <vague idea>` or `ooo auto: <goal>` in chat is
enough — no extra prompting required.

## License

MIT
