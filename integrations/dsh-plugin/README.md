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
- Nothing else for execution: this bundle defaults `OUROBOROS_AGENT_RUNTIME`
  to `host`, so dsh's own model does `ooo run`/`ooo auto`'s execution step —
  no CLI to install. Set `OUROBOROS_AGENT_RUNTIME` (see below) to an
  executable runtime (`claude-cli`, `codex`, `opencode`, ...) if you'd rather
  have that CLI do the work instead.

## Install

This bundle lives inside the main [Ouroboros](https://github.com/Q00/ouroboros)
repository as a subdirectory (it has no independent release cadence or code of
its own), so install it straight from GitHub with pnpm's subdirectory syntax.
`--profile` is required by `dsh plugin` and names the profile to install into:

```sh
dsh plugin --profile <your-profile> add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"
```

Then boot as usual (`dsh --profile <your-profile>`, or `dsh web` if your
profile is named `web`). `dsh --profile <your-profile> --dump-config` shows
the `# == dsh-ouroboros` layer once it's composed.

## Configuration

Set these as environment variables before launching `dsh`. They are read from
the dsh host environment and passed through to the spawned
`ouroboros mcp serve` process:

| Variable | Purpose |
|---|---|
| `OUROBOROS_AGENT_RUNTIME` | Agent runtime for `ooo run`/`ooo auto`'s execution step. Defaults to `host` (dsh's own model does the work — see below); set to `claude-cli`, `codex`, `opencode`, ... to use that CLI instead. |
| `OUROBOROS_LLM_BACKEND` | LLM backend for interview/Seed/QA. Leave unset to keep your `ouroboros setup` default. `dsh` routes those calls through DeepSeek Harness — read the next section first, it needs two more variables. |
| `OUROBOROS_DSH_CONFIG_PATH` | Absolute path to the trusted Cordis composition the `dsh` LLM backend loads. Required whenever `OUROBOROS_LLM_BACKEND=dsh`. |
| `OUROBOROS_DSH_CLI_PATH` | Path to the `dsh-acp-demo` bin, when it isn't on `PATH`. |

Override any field — timeout, args, a pinned Ouroboros version — from your own
profile's `cordis.patch.yml` by targeting the `mcp-ouroboros` row id; see
["Package and install a plugin"](https://github.com/deepseek-ai/deepseek-harness/blob/main/docs/user/develop/basic/publish.md)
for the override mechanics.

### Routing interview/Seed/QA back through DeepSeek Harness

`OUROBOROS_LLM_BACKEND=dsh` is not a one-variable switch, and it does not reuse
the dsh process you're already running. Ouroboros spawns **its own**
`dsh-acp-demo` ACP child, and that child fails closed with `invalid_config`
unless it is given a composition to load. To use it:

1. Build DeepSeek Harness from source (`pnpm install && pnpm run build`,
   Node.js >= 22) and make its `dsh-acp-demo` bin reachable — on `PATH`, or
   named by `OUROBOROS_DSH_CLI_PATH`. A source build is the working path today:
   installing the published `@deepseek-ai/dsh-acp-demo` still fails on a
   peer-dependency conflict inside its own `dsh-tool-bash` chain.
2. Point `OUROBOROS_DSH_CONFIG_PATH` at an **absolute** composition file.
   Relative paths are rejected on purpose (they would resolve against the
   untrusted project cwd). The file must sit where dsh's `node_modules` (or
   workspace) is reachable: plugin package names in a composition resolve
   relative to the composition file's own directory.
3. Provide whatever credential that composition names — `DEEPSEEK_API_KEY` for
   DeepSeek's own models. See the credentials note below: it must be present in
   the dsh host environment, and this bundle forwards it explicitly.

Without steps 1–2 the Ouroboros tools still register and list fine; the first
interview/Seed/QA call is what fails. Everything except the `dsh` backend works
without them.

### Credentials

dsh does not hand child processes the harness environment wholesale. Its
subprocess seam scrubs every credential-shaped name — anything matching
`/KEY|PASSWORD|SECRET|TOKEN/i` — plus every `DSH_*` name, so harness
credentials never leak into a spawned process implicitly. A plugin's explicit
`env` layer merges after that scrub.

So this bundle names a short allowlist rather than passing everything through:

- `ANTHROPIC_API_KEY` — Ouroboros' default LLM backend.
- `DEEPSEEK_API_KEY` — the `dsh` backend loopback above.

To forward another (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`,
...), override the `mcp-ouroboros` row in your own profile's
`cordis.patch.yml` with that one extra name in `env`. A later layer replaces a
row's entire `config` rather than deep-merging it, so copy this bundle's
`config` block and add your line to it. Everything non-credential-shaped —
`PATH`, `HOME`, the `OUROBOROS_*` selectors — passes through untouched and
needs no row at all.

## Startup failures and recovery

Connection failures are non-fatal (`failOnStartupError: false`): a machine
without `uv` on `PATH` yet still boots dsh normally, just without the Ouroboros
tools, and every other plugin keeps working.

Recovery is not automatic in general. Whether `mcp-client` retries at all
depends on your dsh build — the published `0.0.1-rc.1` has no reconnect loop,
current `main` has one with bounded exponential backoff that gives up after a
capped number of consecutive failures. Either way, after fixing the cause
(installing `uv`, setting a runtime), reload the plugin or restart dsh rather
than waiting for a reconnect.

## What you get

36 tools under the `ouroboros` namespace — `mcp__ouroboros__ouroboros_interview`,
`mcp__ouroboros__ouroboros_auto`, `mcp__ouroboros__ouroboros_evaluate`,
`mcp__ouroboros__ouroboros_ralph`, and more. Each carries its own description,
so a plain `ooo interview: <vague idea>` or `ooo auto: <goal>` in chat is
enough — no extra prompting required.

## License

MIT
