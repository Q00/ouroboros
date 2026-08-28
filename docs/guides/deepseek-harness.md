# DeepSeek Harness integration

Ouroboros and [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(`dsh`) connect in two independent directions. Pick the one that matches where
you want to sit:

| You want to… | Direction | Set up |
|---|---|---|
| Work in a **dsh chat** and call Ouroboros from there | dsh → Ouroboros | [Install the plugin](#dsh--ouroboros-the-plugin) |
| Work in **Ouroboros** and have DeepSeek's models answer the interview/Seed/QA calls | Ouroboros → dsh | [Configure the `dsh` LLM backend](#ouroboros--dsh-the-llm-backend) |

They are unrelated at runtime. Installing the plugin does not change which LLM
Ouroboros uses, and selecting the `dsh` backend does not install anything into
your dsh profile.

---

## dsh → Ouroboros: the plugin

One command, and every Ouroboros MCP tool appears in your dsh profile:

```sh
dsh plugin --profile <your-profile> add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"
```

`--profile` is required by `dsh plugin`; it names the profile to install into.
Boot as usual afterwards (`dsh --profile <your-profile>`), and
`dsh --profile <your-profile> --dump-config` will show a `# == dsh-ouroboros`
layer.

Then just type what you want in the chat:

```
ooo interview I want a CLI that keeps my dotfiles in sync
ooo auto add retry-with-backoff to the fetch layer
```

The model finds the matching `mcp__ouroboros__*` tool on its own — each of the
36 tools carries its own description, so no extra prompting is needed.

### What it needs

- [`uv`](https://astral.sh/uv) on `PATH`. That's the only prerequisite: the
  plugin spawns `uvx --from 'ouroboros-ai[mcp]' ouroboros mcp serve`, which
  fetches Ouroboros into an isolated environment on first launch.
- An executable agent runtime (`claude-cli`, `codex`, `opencode`, …) if you
  want `ooo auto` to run its execution step. Either run `ouroboros setup` once
  or export `OUROBOROS_AGENT_RUNTIME`. An MCP subprocess cannot host the
  in-process `claude` SDK runtime, so this has to be an executable one.

### Credentials do not travel implicitly

dsh scrubs every credential-shaped variable — anything matching
`/KEY|PASSWORD|SECRET|TOKEN/i` — out of a child process by design, so that
harness credentials never leak into a spawned program. A plugin's explicit
`env` layer merges *after* that scrub, which is the only way a credential
reaches `ouroboros mcp serve`.

The bundle therefore forwards a short, deliberate allowlist rather than
everything:

- `ANTHROPIC_API_KEY` — Ouroboros' default LLM backend
- `DEEPSEEK_API_KEY` — the `dsh` backend loopback below

To forward one more (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, …), override the
`mcp-ouroboros` row in your own profile's `cordis.patch.yml` with that extra
name in `env`. A later patch layer **replaces** a row's entire `config` rather
than deep-merging it, so copy the bundle's `config` block and add your line to
it. Everything non-credential-shaped — `PATH`, `HOME`, the `OUROBOROS_*`
selectors — passes through untouched and needs no entry at all.

### When it fails to start

Startup failures are non-fatal (`failOnStartupError: false`): a machine without
`uv` still boots dsh with every other plugin working, just without the
Ouroboros tools. Recovery is not automatic in general — whether `mcp-client`
retries depends on your dsh build, and where a reconnect loop exists it gives
up after a bounded number of attempts. After fixing the cause, reload the
plugin or restart dsh.

Full bundle reference: [`integrations/dsh-plugin/README.md`](../../integrations/dsh-plugin/README.md).

---

## Ouroboros → dsh: the LLM backend

`dsh` is an **LLM-only** backend: it answers interview, Seed authoring, and QA
calls. It is not valid for `orchestrator.runtime_backend`, because dsh's ACP
surface is deliberately text-only and fresh-session — right for completions,
wrong for a tool-using execution runtime.

Selecting it is not a one-variable switch. Ouroboros spawns **its own**
`dsh-acp-demo` child process, and that child fails closed with
`invalid_config` unless you give it a composition to load.

### 1. Get the ACP server binary

Build DeepSeek Harness from source (`pnpm install && pnpm run build`, Node.js
>= 22) and make its `dsh-acp-demo` bin reachable — on `PATH`, or named by
`OUROBOROS_DSH_CLI_PATH` / `orchestrator.dsh_cli_path`. Installing the
published `@deepseek-ai/dsh-acp-demo` package still fails on a peer-dependency
conflict inside its own `dsh-tool-bash` chain, so a source build is the working
path today.

### 2. Point at a composition

```sh
export OUROBOROS_DSH_CONFIG_PATH=/absolute/path/to/cordis.yml
```

Two rules the client enforces or inherits:

- **Absolute only.** A relative path is rejected on purpose — it would resolve
  against the untrusted project cwd, and the composition selects which plugins
  the Node process loads, which is code execution.
- **It must live where dsh's `node_modules` (or workspace) is reachable.**
  Plugin package names inside a composition resolve relative to the composition
  file's own directory.

Both `OUROBOROS_DSH_CLI_PATH` and `OUROBOROS_DSH_CONFIG_PATH` are on the
project `.env` denylist for the same reason: a checked-out repository must not
be able to redirect which executable or plugin tree Ouroboros loads. Set them
in your shell or in `~/.ouroboros/config.yaml`.

### 3. Supply the credential the composition names

Usually `DEEPSEEK_API_KEY`. The spawned child inherits your environment, minus
the Ouroboros selector variables (so a nested `ooo` inside dsh is not confused
about which backend it is).

### 4. Select the backend

```sh
ouroboros mcp serve --runtime claude-cli --llm-backend dsh
# or
export OUROBOROS_LLM_BACKEND=dsh
```

`--runtime` is not optional here. `dsh` answers only the LLM-only calls, so the
execution runtime is still a separate choice — and an MCP 2 server rejects the
SDK-backed `claude` / `claude-sdk` default, so name an executable one
(`claude-cli`, `codex`, `opencode`, …).

Or persist it in `~/.ouroboros/config.yaml`:

```yaml
llm:
  backend: dsh
orchestrator:
  dsh_cli_path: /absolute/path/to/dsh-acp-demo
  dsh_config_path: /absolute/path/to/cordis.yml
```

`deepseek_harness` is accepted as an alias for `dsh` only where backend names
are resolved at runtime — `OUROBOROS_LLM_BACKEND=deepseek_harness` and
programmatic `create_llm_adapter(backend="deepseek_harness")`. The typed
surfaces reject it: `--llm-backend deepseek_harness` fails argument validation,
and `llm.backend: deepseek_harness` fails config validation. Write `dsh` in the
CLI and in `config.yaml`.

### Model selection

The ACP wire carries no model parameter — the Cordis composition owns the
provider and model. Ouroboros reports the honest `dsh-composition` sentinel
instead of inventing a model name, and reports zero token usage because the
protocol does not return counts. To change models, edit the composition.

---

## Using both at once

They compose: install the plugin so a dsh chat can drive Ouroboros, and set
`OUROBOROS_LLM_BACKEND=dsh` so the interview questions that come back are
written by DeepSeek's own models. That loopback is exactly why the bundle
forwards `DEEPSEEK_API_KEY` explicitly — without it the tools list fine and the
first call fails.

Note that the loopback spawns a second `dsh-acp-demo` process; it does not
reuse the dsh you are chatting in, so steps 1–3 above still apply.

For source-backed runtime diagnostics beyond this integration path—especially
plugin/MCP composition, Session recovery, sandbox boundaries, and cross-platform
failures—see the independent [DeepSeek Harness Handbook](https://github.com/sandbaseai/deepseek-harness-handbook).
