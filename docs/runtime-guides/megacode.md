# Megacode Harness Runtime

Run Ouroboros inside the [megacode harness](https://megacode-harness.vercel.app/docs/)
(the `harness` binary), and run Ouroboros's own model calls and workers on the
harness too, including its hosted `mega:*` models.

There are two halves, and setup wires both:

- **Harness → Ouroboros.** A harness session gets the Ouroboros skills, the
  Ouroboros MCP server, and an `ooo` routing table in its `AGENTS.md`. The
  harness has no hooks, so the routing table stands in for the keyword hook.
- **Ouroboros → harness.** The `megacode` backend runs each worker task and each
  LLM completion as one headless `harness -p` turn, so authoring, evaluation and
  execution use the model and login of your harness sessions.

## Prerequisites

| Requirement      | Why                                                            |
|------------------|----------------------------------------------------------------|
| `harness` CLI    | Install from the harness docs; keep `harness` on `PATH`        |
| Harness login    | `harness login` for hosted `mega:*` models, or a provider key  |
| `uv`             | Launches the Ouroboros MCP server (`uvx`)                      |

## Quick start

```bash
ouroboros setup --runtime megacode
harness            # then type: ooo interview build me a small CLI
```

Setup only writes inside the harness config folder (`harness paths` →
`CONFIG`); `~/.ouroboros/config.yaml` is unchanged, so other hosts keep their
runtime. To use the backend from the Ouroboros CLI as well, select it per run:

```bash
ouroboros run workflow seed.yaml --runtime megacode
OUROBOROS_LLM_BACKEND=megacode ouroboros run workflow seed.yaml --runtime megacode
```

## What setup writes

| Path (under `CONFIG`) | Content |
|---|---|
| `config.toml` | One managed block: `[skills] extra_roots`, `[modes.ouroboros-worker]`, `[modes.ouroboros-llm]`, `[mcp.servers.ouroboros]` |
| `ouroboros/skills/` | A copy of the packaged skills, each in a folder named by its frontmatter `name` (the harness skips a skill whose folder and `name` differ) |
| `AGENTS.md` | One managed block routing `ooo <command>` to a skill |

Text outside the managed blocks is never touched, and rerunning setup replaces
the blocks in place. If your own `config.toml` already defines `[skills]`, one
of the two modes, or `[mcp.servers.ouroboros]`, setup stops and names the table
instead of writing a second copy. Rerun setup after upgrading Ouroboros to
refresh the copied skills.

The MCP server runs with `sandbox = false`: it installs itself with `uvx`,
writes its state under `~/.ouroboros`, and launches `harness -p` for its own
model calls.

## Headless contract

| | Worker (`MegacodeRuntime`) | LLM completion (`MegacodeLLMAdapter`) |
|---|---|---|
| Command | `harness -p --output-format stream-json --mode ouroboros-worker` | `harness -p --output-format stream-json --mode ouroboros-llm --max-iterations N` |
| Prompt | stdin | stdin |
| Tools | read, write, edit, bash, glob, search, exec | read, glob, search |
| Answer | the `result` record | the `result` record |

- Both modes set `ask = []`: a headless turn cannot ask anyone, and neither mode
  offers `mcp` or `skill`, so a worker cannot re-enter Ouroboros.
- `--model` is forwarded only for an explicit id such as `mega:full`; otherwise
  the harness uses its own default.
- `permission_mode: bypassPermissions` adds `--sandbox off`; anything else keeps
  the harness's configured sandbox (on by default, no network).
- A command that runs and exits non-zero is reported by the harness as
  `status: ok` with `output.code`; the runtime treats a non-zero `code` as a
  failed tool call.

## Limits

- **No resume.** A headless harness run cannot continue a session (`resume -p`
  is a documented harness limit), so the backend declares no targeted resume and
  every retry starts a fresh session.
- **MCP startup per nested call.** Every `harness -p` starts the MCP servers in
  your harness config, including Ouroboros's own, even in a mode without the
  `mcp` tool. That adds the server's startup time to each worker and LLM call.
- **No executable attestation.** The megacode CLI is not yet part of the CLI
  executable-identity attestation the Codex-family runtimes use.

## CLI path resolution

1. `OUROBOROS_MEGACODE_CLI_PATH` (ignored when it comes from a project `.env`)
2. `orchestrator.megacode_cli_path` in `~/.ouroboros/config.yaml`
3. `harness` on `PATH`
