# OMP CLI Runtime

Run Ouroboros workflow execution on top of the locally installed OMP ("Oh My
Pi", the `omp` binary) CLI.

OMP is a Pi-family coding agent: it speaks the same JSON event protocol as Pi,
so the OMP runtime is a subprocess adapter that mirrors the Pi runtime.
Ouroboros owns the workflow engine, Seed decomposition, checkpointing,
evaluation handoff, and `ooo` skill dispatch. For each runtime task it shells
out to OMP JSON mode and normalizes OMP's JSONL events into Ouroboros
`AgentMessage` values.

## Mental Model

There are three separate layers:

```text
User / CLI / MCP
      |
      | 1. Selects runtime_backend: omp, or sends an ooo shortcut
      v
Ouroboros runtime adapter
      |
      | 2a. ooo shortcut? handle inside Ouroboros before OMP starts
      | 2b. normal task? spawn OMP JSON mode
      v
omp --mode json <prompt>
      |
      | 3. OMP loads its own settings, extensions, tools, and model config
      v
OMP model turn and JSONL events
```

So "OMP is an Ouroboros runtime" means step 2b exists and is selectable. It
does not mean OMP extensions are imported into Ouroboros, and it does not mean
OMP's interactive command surface becomes part of the Ouroboros command router
unless the managed OMP bridge extension is installed by setup.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| `omp` CLI | Provider runtime; install Oh My Pi and keep `omp` on `PATH`, or configure an explicit path |
| OMP model auth | OMP selects its own model via its configured model roles (`smol`, `slow`, `plan`); complete OMP's own model setup before first use |
| Ouroboros base package | `pip install ouroboros-ai` |

OMP has no provider-login surface inside the Ouroboros adapter: model choice
and credentials live in OMP's own configuration. Ouroboros only passes an
explicit `--model` override when the execution path supplies one.

## Quick Start

```bash
# 1. Install Oh My Pi so `omp` is on PATH
#    (or set OUROBOROS_OMP_CLI_PATH / orchestrator.omp_cli_path below)

# 2. Point Ouroboros at OMP and install the OMP-side ooo bridge
ouroboros setup --runtime omp

# 3. Run a workflow through the configured runtime
ouroboros run workflow seed.yaml

# 4. In an interactive OMP session, restart OMP so the bridge extension loads, then:
ooo auto build a small CLI
```

The installer and `ouroboros update` also raise OMP's MCP tool-call timeout to
60 seconds via `omp config set extensionHandlers.toolCallTimeoutMs 60000` when
`omp` is on `PATH`.

If OMP is installed outside `PATH`, set:

```bash
export OUROBOROS_OMP_CLI_PATH=/absolute/path/to/omp
```

or configure:

```yaml
orchestrator:
  runtime_backend: omp
  omp_cli_path: /absolute/path/to/omp
```

Resolution precedence is `OUROBOROS_OMP_CLI_PATH`, then
`orchestrator.omp_cli_path`, then `PATH`.

## Runtime Contract

For a normal execution task, Ouroboros launches:

```text
omp --mode json [--model <MODEL>] [--resume <SESSION_ID>]
  [--append-system-prompt <SYSTEM>] [--tools <TOOLS>] [--no-tools] <PROMPT>
```

| Argument | Why |
|----------|-----|
| `--mode json` | Requests OMP's headless JSONL event stream |
| `--model` | Optional model override passed by the caller. OMP's `--model` does fuzzy model matching and has no `default` id, so Ouroboros omits the generic `default` sentinel and lets OMP use its own configured model |
| `--resume` | Optional native OMP session id for targeted resume. Pi uses `--session` for the same purpose; OMP's flag is `--resume` |
| `--append-system-prompt` | Native delivery of Ouroboros' `system_prompt` parameter (appended to OMP's base coding prompt) |
| `--tools` | Native tool allow-list: OMP's own flag enables only the listed tools. Claude-style names are mapped to OMP's lowercase built-ins (`Read`→`read`, `Bash`→`bash`, `Glob`→`glob`, …). OMP's directory-enumeration tool is `glob` (Pi's `find` is only a Pi-compat alias there), and OMP has no `ls` built-in, so both `Glob` and `LS` map to `glob`. Unknown names pass through unchanged so extension tools (`mcp__*`, `task`, `todo`, `web_search`, `eval`, `hub`, …) keep working |
| `--no-tools` | Explicit tool-free mode: emitted when Ouroboros requests `tools=[]` (no tools allowed). Distinguishes "use defaults" (`tools=None`, flag omitted) from "disable all tools" |
| `<PROMPT>` | The composed task prompt from Ouroboros, passed as a positional message argument. OMP's JSON mode accepts the task positionally; piping it on stdin as well would duplicate it, so stdin stays unused |

`--append-system-prompt`, `--tools`, and `--no-tools` are documented flags of
the `omp` CLI, so unlike Pi no `--help` capability probe is needed and those
parameters are always delivered natively. If OMP rejects a genuinely unknown
tool name at startup, the CLI usage error surfaces as a normal runtime error
result.

Ouroboros parses the initial `session` event into a `RuntimeHandle`, streams
`message_update` `text_delta` events as assistant output, and reads terminal
assistant text from `message_end`, `turn_end`, or `agent_end` events — the
same event lifecycle as Pi JSON mode.

## What `ooo` Means With OMP

There are two supported entry paths.

### Ouroboros Launches OMP

When Ouroboros is already in control and `runtime_backend: omp` is selected,
`ooo <skill>` is handled by Ouroboros before the OMP subprocess starts.

The OMP runtime calls the shared `SkillInterceptor` at the top of
`OmpRuntime.execute_task()`. If the prompt is an Ouroboros skill shortcut such
as `ooo interview`, the interceptor resolves the skill and invokes the
matching Ouroboros MCP handler. OMP does not receive that prompt as ordinary
chat input.

This means:

- `ooo interview` in an Ouroboros-controlled OMP runtime means "Ouroboros
  handles the interview command, using the configured LLM backend for
  authoring."
- OMP only runs normal Seed execution prompts after the command dispatch path
  has decided the input is not an `ooo` shortcut.

### OMP Launches Ouroboros

`ouroboros setup --runtime omp` also installs a managed global OMP extension:

```text
~/.omp/agent/extensions/ouroboros-ooo-bridge.ts
```

OMP auto-discovers extensions in that directory (the same layout as Pi's
`~/.pi/agent/extensions`). After restarting OMP, interactive OMP sessions can
type:

```text
ooo auto build a small CLI
ooo interview clarify this feature
/ooo status auto --resume auto_...
```

The extension intercepts exact-prefix `ooo ...` input and runs:

```text
ouroboros dispatch --runtime omp --cwd <omp-session-cwd> "ooo ..."
```

That hidden `dispatch` entrypoint uses the same shared skill resolver and MCP
handler composition as the runtime adapters. The registered `/ooo` command
also provides TAB argument completion, and the dispatch timeout is tunable
through `OUROBOROS_OMP_BRIDGE_TIMEOUT_MS`.

Like the Pi bridge, the extension only consumes commands the hidden dispatcher
can execute; first-party shortcuts without an MCP dispatch target are returned
to the OMP session with a deterministic unsupported-dispatch exit code so the
session can continue handling the input.

## `ooo auto --runtime omp`

`ooo auto` has two different completion levels:

| Command shape | What completes | OMP involvement |
|---------------|----------------|-----------------|
| `ouroboros auto --runtime omp ...` | Interview, Seed generation, Seed QA, and run handoff | Starts an execution handoff for the OMP runtime; the final product may still be pending |

OMP model selection comes from OMP's own configured model roles unless the
execution path passes a model override. With an override, the OMP runtime
launch includes:

```text
omp --mode json --model <MODEL> <PROMPT>
```

The generic `default` model sentinel is never forwarded to OMP: its `--model`
fuzzy matcher has no `default` id, so the sentinel is omitted and OMP picks
its own model.

Auto usually runs in Ouroboros-managed task worktrees. A successful
file-writing smoke test may therefore create files under
`~/.ouroboros/worktrees/...` rather than in the shell's original checkout.

## OMP Extensions And Security

OMP extensions are loaded by OMP itself from its global extension directory.
Setup writes only one managed file there — the `ooo` bridge — so an OMP
session can keep its own extensions alongside it.

Because OMP resolves its agent directory (sessions, extensions, and rules
loaded into every spawned session) from the spawned-CLI discovery environment,
that discovery variable (`PI_CODING_AGENT_DIR`, shared with Pi) is denied from
untrusted repo `.env` files. An untrusted repository cannot point a spawned
`omp` at attacker-supplied extensions.

## OMP As LLM Backend

OMP can also be selected as an LLM backend for authoring, scoring, extraction,
and other completion flows:

```yaml
llm:
  backend: omp
```

This is separate from `orchestrator.runtime_backend`. The same selection is
available on the CLI as `ouroboros init --llm-backend omp` and
`ouroboros mcp serve --llm-backend omp`, and the `omp_cli` alias is accepted
everywhere `omp` is.

The `OmpLLMAdapter` drives the same `omp --mode json` stream for LLM-only
flows. It supports structured `response_format` requests through soft
enforcement: Ouroboros injects a strict JSON/schema instruction, extracts the
JSON payload from OMP's response, and validates `json_schema` payloads before
returning them. OMP JSON mode does not expose a hard `--output-schema` flag,
so malformed structured responses are retried and then surfaced as provider
errors — the same contract as the Pi LLM adapter. Generic cross-provider
default models normalize to the backend-safe `default` sentinel, which OMP
resolves to its own configured model.

Use OMP as the runtime backend when you want OMP to execute Seed tasks; use
`llm.backend: omp` when the authoring/evaluation flow can accept
adapter-level JSON extraction and validation rather than provider-native
schema enforcement.

## Capabilities

| Capability | Status |
|------------|--------|
| Headless execution | Yes, through `omp --mode json` |
| Skill shortcut dispatch | Yes, before spawning OMP |
| Native targeted resume | Yes, through `--resume <id>` |
| Structured event stream | Yes, JSONL parsed by `OmpRuntime` |
| Native system prompt and tool allow-list | Yes, through documented `--append-system-prompt` / `--tools` / `--no-tools` flags |
| Structured schema responses as LLM backend | Soft-enforced and validated |
| OMP extension loading | OMP-owned; the managed bridge installs into the global extension directory |
| Interactive OMP `ooo` frontdoor | Yes, via managed setup-installed extension |

## Troubleshooting

**`OMP not found`**
Install Oh My Pi so `omp` is on `PATH`, or set `OUROBOROS_OMP_CLI_PATH` /
`orchestrator.omp_cli_path`.

**`--tools` reports a CLI usage error**
OMP rejects genuinely unknown tool names at startup. Check that requested
tool names are either OMP built-ins (`read`, `write`, `edit`, `bash`, `grep`,
`glob`, `find`, `lsp`), extension tools such as `mcp__*`, or Claude-style
names Ouroboros maps (`Read`, `Glob`, `LS`, …). Note there is no `ls`
built-in: directory enumeration is `glob`.

**Model seems wrong or ignored**
OMP selects its model through its own configured model roles
(`smol`/`slow`/`plan`) and fuzzy-matches `--model`. Ouroboros omits the
generic `default` sentinel; pass an explicit model only when you want to pin
one.

**`ooo ...` is sent to the model as ordinary chat inside OMP**
Run `ouroboros setup --runtime omp`, then restart OMP. Confirm that
`~/.omp/agent/extensions/ouroboros-ooo-bridge.ts` exists, and that the
dispatch timeout env `OUROBOROS_OMP_BRIDGE_TIMEOUT_MS` is not set to an
unusable value.

## Active Conductor and Synapse

OMP declares Synapse `inform`/`after_turn` signal capabilities (including
background reply) on the same runtime session handle. It does not claim live
checkpoint `redirect` or hard `replace`.

During a run, one exclusive read-only observer relays the current runtime/model,
efficiency assurance, bounded Discover targets, dependency/parallel levels,
first scheduled ACs, attention, and terminal assurance. The main session stays
available and selects the relevant AC semantically rather than asking the user
for IDs. English is the canonical instruction language; the host renders the UX
naturally in the current conversation language.
