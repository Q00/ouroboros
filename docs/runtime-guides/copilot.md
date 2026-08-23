# GitHub Copilot CLI Runtime

> 한국어: [copilot.ko.md](./copilot.ko.md)

Run Ouroboros workflows on top of the locally installed
[GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli).

The Copilot runtime is a sibling of the Codex / Gemini / Hermes / OpenCode /
Kiro runtimes: Ouroboros owns the orchestration loop and shells out to
`copilot -p` per task instead of talking to a hosted SDK. Authentication
flows through your existing `gh auth` session, so there is no separate API
key to manage.

> **What makes this runtime different**: Copilot is the only Ouroboros
> backend that **live-discovers its model catalog**. `ouroboros setup
> --runtime copilot` queries the GitHub Copilot models API at setup time
> and lets you pick a default from whatever your subscription currently
> grants, instead of asking you to remember a hardcoded model ID. New
> models become available the moment GitHub publishes them; rerun setup to
> refresh.

## Prerequisites

| Requirement      | Why                                                                |
|------------------|--------------------------------------------------------------------|
| `copilot` CLI    | Provider — install per the [Copilot CLI install guide](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) |
| `gh` CLI         | Used to discover the live Copilot model catalog (`gh auth token`)  |
| GitHub auth      | `gh auth login` once before first use                              |
| Ouroboros (mcp)  | `pipx install 'ouroboros-ai[mcp]'` or `uv tool install 'ouroboros-ai[mcp]'` |

> Copilot runs on the **base** Ouroboros package plus the `[mcp]` extra. It
> does not require the `[claude]` extra; the MCP entry is registered with
> `ouroboros-ai[mcp]`. Host registration requires the package-isolated `uvx`
> or `pipx run` launcher. A plain `pip install` is suitable for embedding in
> an already isolated environment, but it does not satisfy this host-launcher
> requirement by itself; setup fails closed if neither launcher is available.

## Quick start

```bash
# 1. Install Copilot CLI and authenticate (once)
gh auth login                            # gives gh auth token access

# 2. Install Ouroboros with the MCP extra
pipx install 'ouroboros-ai[mcp]'         # or: uv tool install 'ouroboros-ai[mcp]'

# 3. Wire Ouroboros to Copilot
ouroboros setup --runtime copilot
#   - auto-detects copilot on PATH (or honours OUROBOROS_COPILOT_CLI_PATH)
#   - calls https://api.githubcopilot.com/models with your gh token
#   - prints the live model list and lets you pick a default
#   - writes ~/.ouroboros/config.yaml + ~/.copilot/mcp-config.json
#   - installs ~/.copilot/ouroboros-instructions/AGENTS.md for Ouroboros runs

# 4. Restart your Copilot session, then use ooo skills
copilot
> ooo interview Add a CLI flag to skip eval
```

## CLI path resolution

The runtime looks for the binary in this order:

1. Constructor argument `cli_path=...`
2. `OUROBOROS_COPILOT_CLI_PATH` environment variable
3. `orchestrator.copilot_cli_path` in `~/.ouroboros/config.yaml`
4. `copilot` on `$PATH`

This means non-PATH installs (for example, a winget or scoop install on
Windows that lands the binary outside `$PATH`) work without modifying
shell init.

Ouroboros-launched Copilot child sessions append the setup-owned
`~/.copilot/ouroboros-instructions` directory to
`COPILOT_CUSTOM_INSTRUCTIONS_DIRS` using Copilot CLI's comma-separated list
format. Existing custom instruction directories are preserved.

## Live model discovery

`ouroboros setup --runtime copilot` always queries the live model catalog
at the start of the wizard. The flow:

1. Resolve a token from `GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_TOKEN`, or
   `gh auth token` (in that order).
2. `GET https://api.githubcopilot.com/models` with that token.
3. Parse `data[].id` and `capabilities.family` into a typed list.
4. Cache the result in process for the rest of the setup run.
5. If any of the above fails (no `gh`, network down, rate limited, parse
   error), print a warning and fall back to a bundled snapshot of well-known
   IDs so setup still completes.

Setup prints the chosen default model and persists it through supported
model fields in `~/.ouroboros/config.yaml` — for example
`clarification.default_model`, `llm.qa_model`, evaluation/resilience model
fields, and consensus model defaults when those fields are absent or still
on Ouroboros' shipped defaults. There is no `llm.default_model` key in the
config contract. Re-run `ouroboros setup --runtime copilot` any time to pick
a new default after GitHub ships new models.

### Hyphen versus dotted model IDs

Ouroboros' defaults use the Anthropic SDK form
(`claude-opus-5`, `claude-sonnet-4-6`). Copilot CLI expects its catalog form
(`claude-opus-5`, `claude-sonnet-4.6`). The adapter resolves these forms
against the discovered Copilot catalog rather than rewriting arbitrary model
names.

`map_to_copilot_model()` ([`copilot/model_discovery.py`](../../src/ouroboros/copilot/model_discovery.py))
passes through an explicit dotted Copilot ID, then derives candidates by
removing the known `openrouter/anthropic/` prefix, preserving exact legacy
aliases, or changing only the trailing numeric version separator. For example,
`claude-opus-4-8` becomes the candidate `claude-opus-4.8`; the hyphens in
`claude-opus` are never touched. Every transformed candidate, including a
prefix-stripped or statically mapped one, is returned only when that exact ID
is in the discovered or bundled catalog.

The current `DEFAULT_OPUS_MODEL` therefore resolves to the published
`claude-opus-5`, as does `openrouter/anthropic/claude-opus-5`. Future
Anthropic versions use the same catalog-gated trailing-version rule without
another static-map entry. An unknown model, or any derived candidate that the
active catalog does not publish, remains unchanged—including its OpenRouter
prefix—so the existing Copilot unavailable-model error stays explicit rather
than silently selecting a different model.

If you set a model that Copilot does not recognise, the subprocess will
fail with `Model "<id>" from --model flag is not available.` Pass a model
from the discovered list (or rerun setup to refresh).

## Configuration

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: copilot
  copilot_cli_path: C:\Users\you\AppData\Local\Programs\copilot\copilot.exe   # optional
llm:
  backend: copilot
  qa_model: claude-opus-4.6                     # written by setup
clarification:
  default_model: claude-opus-4.6                # written by setup
```

> `llm` has no `default_model` field (`config/models.py`, `LLMConfig`). Setup
> writes the model it discovered into the fields that do exist — for example
> `clarification.default_model`, `llm.qa_model`, and the evaluation/resilience
> model fields.

The same `copilot` value is accepted by every CLI surface that takes a
backend name:

- `ouroboros setup --runtime copilot`
- `ouroboros config backend copilot`
- `ouroboros mcp serve --runtime copilot --llm-backend copilot`
- `ouroboros init --llm-backend copilot`

## Headless contract

Each task spawns a single non-interactive Copilot prompt:

```text
copilot --no-color --log-level none \
        --add-dir <CWD> \
        --available-tools=<TOOLS> --allow-tool=<TOOLS> \
        [--model <DOTTED_ID> | --agent <NAME>] \
        -p <PROMPT>
```

| Flag                | Why                                                          |
|---------------------|--------------------------------------------------------------|
| `--no-color`        | Stable JSONL parsing                                         |
| `--log-level none`  | Suppress non-event log lines                                 |
| `--add-dir`         | Sandbox-write boundary; pinned to the CWD Ouroboros passed   |
| `--available-tools` | Hard tool envelope (allowlist) — anything outside is invisible to the model |
| `--allow-tool`      | Skip per-call confirmation prompts (required for `-p`)       |
| `--model`           | Per-task model override (auto-mapped from hyphen form)       |
| `--agent`           | Custom agent profile; takes precedence over `--model`        |
| `-p`                | One-shot prompt (no interactive REPL)                        |

Before launching that command, the runtime applies the same executable
version-attestation policy as Codex: initialization-time probe failure blocks
the runtime because no positive baseline exists; a later timeout or execution
failure blocks only the current attempt and is not reported as executable
drift; only a version-output change requires two successful, different
attestations.
The runtime rejects non-executing path/content/device/inode/symlink evidence
that differs from initialization before running `copilot --version`, and it
post-samples every started probe so mutation takes precedence over a concurrent
timeout or execution failure.
If only a containing-directory generation changed, the evidence cannot
distinguish unrelated sibling churn from an executable-entry swap-and-restore.
That attempt fails closed as retryable, indeterminate authority without
claiming confirmed executable drift.
Path, content, symlink, device/inode, or probe-window generation drift can fail
closed before a second successful version probe. This ensures that two missing
`copilot --version` results never authorize a launch under load.

### MCP registration

`ouroboros setup --runtime copilot` writes
`~/.copilot/mcp-config.json` with an entry that points at whichever
install method the wizard detected:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
      "env": {
        "OUROBOROS_AGENT_RUNTIME": "copilot",
        "OUROBOROS_LLM_BACKEND": "copilot"
      }
    }
  }
}
```

When `uvx` is unavailable, setup writes the equivalent `pipx` entry:

```json
{
  "command": "pipx",
  "args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
}
```

It never registers a direct global binary or `python -m` fallback because
those environments cannot guarantee MCP 2. The wizard is idempotent and
updates setup-managed entries to the current isolated launcher.

> **Restart required**: Copilot CLI binds MCP children at session start.
> After the first registration (or any change to the entry), close and
> reopen your `copilot` session so the new MCP server is spawned.

## Capabilities

| Capability              | Status                                                 |
|-------------------------|--------------------------------------------------------|
| Headless execution      | Yes                                                    |
| Tool envelope           | Yes (`--available-tools` allowlist + `--allow-tool`)   |
| Sandbox boundary        | Yes (`--add-dir <CWD>`)                                |
| Live model discovery    | Yes (only runtime that does this)                      |
| Agent profile selection | Yes (`--agent` from `runtime_profile` mapping)         |
| Recursion guard         | Yes (`_OUROBOROS_DEPTH`, matches Claude/Codex)         |
| Response truncation     | Yes (via `InputValidator`)                             |
| Structured output flag  | No (`--output-schema` not supported; uses prompt directive + post-hoc JSON extraction, same workaround as Gemini) |
| Session resumption      | No (Copilot CLI does not expose a resume API; checkpointing happens at the Ouroboros lineage layer) |

## Troubleshooting

**`Model "claude-opus-4-6" from --model flag is not available.`**
Old Ouroboros build that did not yet auto-map hyphen IDs to the dotted
Copilot form. Upgrade to a release that includes the model-discovery
module, or override your default to the dotted form
by rerunning `ouroboros setup --runtime copilot` and picking a
dotted ID from the live catalog. There is no `OUROBOROS_DEFAULT_MODEL`
environment variable; per-role overrides use their own variables, such as
`OUROBOROS_CLARIFICATION_MODEL`.

**`copilot CLI not found.`**
Install Copilot CLI per the GitHub docs, then either let setup auto-detect
it or set `OUROBOROS_COPILOT_CLI_PATH=/abs/path/to/copilot`.

**`MCP dependencies not installed: mcp package not installed.`**
The isolated MCP launcher is unavailable or could not load the `[mcp]` extra.
Install with `pipx install 'ouroboros-ai[mcp]'` or
`uv tool install 'ouroboros-ai[mcp]'`. For local dev installs use
`uv tool install --with mcp --from . ouroboros-ai`.

**`ouroboros-ouroboros_*` tools return `Error: Not connected`.**
The MCP child crashed or was killed. Check
`~/.copilot/logs/<session>/...` for the spawn error, fix it (usually the
missing `[mcp]` extra above), then **restart your Copilot session** — the
CLI does not auto-reconnect dead MCP children mid-session.

**`Could not reach the GitHub Copilot models API` during setup.**
Setup falls back to a bundled model snapshot so you can finish the wizard.
Run `gh auth login` (or set `GH_TOKEN` / `GITHUB_TOKEN`), then re-run
`ouroboros setup --runtime copilot` to refresh from the live catalog.

**Final response missing.**
The Copilot adapter reconstructs the assistant reply from the JSONL event
stream. If a tool call exhausts the allowed turn budget, the reply may be
empty — raise `--max-turns` (or the equivalent config field) and rerun.
