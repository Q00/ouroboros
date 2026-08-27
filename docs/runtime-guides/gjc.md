# GJC Runtime

Run Ouroboros workflow execution on top of the locally installed `gjc` CLI.

The GJC runtime is a subprocess adapter. Ouroboros owns the workflow engine,
Seed decomposition, checkpointing, evaluation handoff, and `ooo` skill
dispatch. For each runtime task it starts a GJC RPC session, sends the
normalized agent-runtime frames, and converts recognized GJC agent events into
Ouroboros `AgentMessage` values.

## Mental Model

There are three separate layers:

```text
User / CLI / MCP
      |
      | 1. Selects runtime_backend: gjc, or sends an ooo shortcut
      v
Ouroboros runtime adapter
      |
      | 2a. ooo shortcut? handle inside Ouroboros before GJC starts
      | 2b. normal task? spawn GJC RPC mode
      v
gjc --mode rpc
      |
      | 3. GJC loads its own settings, extensions, tools, model auth
      v
GJC agent events
```

So "GJC is an Ouroboros runtime" means step 2b exists and is selectable. It
does not mean GJC internals are imported into Ouroboros. Interactive GJC gains
the Ouroboros command surface after setup activates either the native skill/MCP
projection or, on older storage-only GJC releases, the setup-owned compatibility
input bridge.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| `gjc` CLI | Provider runtime; keep `gjc` on `PATH`, or configure an explicit path |
| GJC auth | Run the GJC provider login/configuration flow before first use |
| Ouroboros base package | `pip install ouroboros-ai` |

## Quick Start

```bash
# 1. Install and authenticate GJC, then confirm gjc is on PATH
gjc

# 2. Point Ouroboros at GJC and activate its supported interactive route
ouroboros setup --runtime gjc

# 3. Restart GJC so it loads the native projection or compatibility bridge
gjc

# 4. Use Ouroboros commands in the GJC session
ooo auto build a small CLI
```

If GJC is installed outside `PATH`, set:

```bash
export OUROBOROS_GJC_CLI_PATH=/absolute/path/to/gjc
```

or configure:

```yaml
orchestrator:
  runtime_backend: gjc
  gjc_cli_path: /absolute/path/to/gjc
```

You can also select the backend for one command with:

```bash
ouroboros run workflow --runtime gjc seed.yaml
```

## Runtime Contract

For a normal execution task, Ouroboros launches:

```text
gjc --mode rpc
```

and then speaks the GJC RPC protocol for the task:

1. Wait for the initial `ready` frame.
2. Optionally send `set_model(provider/modelId)` when the caller provided a
   model override.
3. Send the composed task `prompt`.
4. Treat the prompt acknowledgement as delivery confirmation only. A prompt ack
   is **not** task completion.
5. Stream recognized agent events until `agent_end`.

Ouroboros recognizes GJC agent events that map to `AgentMessage` output,
including assistant text deltas/final text, runtime handles, and terminal agent
state. The adapter fails closed on frames that would require host-side UI or
capabilities Ouroboros does not provide. Unsupported `workflow_gate`,
`host_tool`, `host_uri`, and `extension_ui` frames are surfaced as runtime
errors instead of being ignored or treated as model text.

GJC may report provider/model failures as assistant messages with
`stopReason: "error"` while the process still exits with status `0`.
Ouroboros treats those assistant stop reasons as runtime errors instead of
relying only on the process return code.

## What `ooo` Means With GJC

There are two supported entry paths.

### Ouroboros Launches GJC

When Ouroboros is already in control and `runtime_backend: gjc` is selected,
`ooo <skill>` is handled by Ouroboros before the GJC subprocess starts.

The GJC runtime calls the shared `SkillInterceptor` at the top of task
execution. If the prompt is an Ouroboros skill shortcut such as `ooo interview`
or `/ouroboros:ouroboros-run`, the interceptor resolves the skill and invokes the matching
Ouroboros MCP handler. GJC does not receive that prompt as ordinary chat input.

This means:

- `ooo interview` in an Ouroboros-controlled GJC runtime means "Ouroboros
  handles the interview command, using the configured LLM backend for
  authoring."
- GJC only runs normal Seed execution prompts after the command dispatch path
  has decided the input is not an `ooo` shortcut.

### GJC Launches Ouroboros

`ouroboros setup --runtime gjc` first probes the installed GJC MCP contract. A
host whose `gjc mcp` surface declares conventional standalone autoload receives
the shared native projection in the active agent profile:

```text
<agent-dir>/skills/ouroboros-*/SKILL.md
<agent-dir>/rules/ouroboros-skill-capability-guide.md
<agent-dir>/ouroboros/mcp-bridge.yaml
<agent-dir>/mcp.json                    # written through `gjc mcp add`
```

The skills are namespaced so they cannot collide with GJC's bundled workflow
skills. The always-applied rule maps exact `ooo <command>` prefixes to the
matching `/skill:ouroboros-<command>` entry before generic planning, search, or
GJC's own `deep-interview` routing. Arguments after the prefix are preserved
verbatim.

Native-capable GJC releases autoload the isolated Ouroboros MCP server from
their own config. The GJC-specific child uses an empty setup-owned upstream
bridge config so the user's separate `~/.ouroboros/mcp_servers.yaml` fan-in is
not recursively started during session startup. That user file is not modified.

If the installed GJC exposes only storage-only registration, setup does not
pretend that a persisted server is active. It installs the owned compatibility
input bridge instead and keeps the existing `ooo` dispatch path working. A
later refresh migrates that bridge only after native activation validates.

Interactive GJC sessions can then type:

```text
ooo auto build a small CLI
ooo interview clarify this feature
ooo status
```

Setup removes an obsolete setup-owned input bridge only after native activation
validates. Each skill, guide, bridge config, and compatibility route is published
independently through the shared single-entry ownership primitive. Setup does not
journal or roll back those host artifacts as one transaction; on failure it leaves
complete setup-owned generations for `ouroboros setup refresh` and never replaces
an operator generation.

## GJC As LLM Backend

GJC can also be selected as an LLM backend for authoring, scoring, extraction,
and other completion flows:

```yaml
llm:
  backend: gjc
```

This is separate from `orchestrator.runtime_backend`.

The GJC LLM adapter supports structured `response_format` requests through soft
enforcement: Ouroboros injects a strict JSON/schema instruction, extracts the
JSON payload from GJC's response, and validates `json_schema` payloads before
returning them. GJC RPC mode does not currently provide a hard tool-envelope or
provider-native schema enforcement flag, so malformed structured responses are
retried and then surfaced as provider errors.

Use GJC as the runtime backend when you want GJC to execute Seed tasks; use
`llm.backend: gjc` when the authoring/evaluation flow can accept adapter-level
JSON extraction and validation rather than provider-native schema enforcement.

## Capabilities

| Capability | Status |
|------------|--------|
| Headless execution | Yes, through `gjc --mode rpc` |
| Skill shortcut dispatch | Yes, before spawning GJC |
| Native targeted resume | No in v1; `targeted_resume=False` and checkpointing stays at the Ouroboros lineage layer |
| Structured event stream | Yes, RPC agent events parsed by the GJC runtime |
| Native permission override | No; RPC mode is headless but exposes no per-invocation approval flag, so `permission_mode_support=ignored` |
| Structured schema responses as LLM backend | Soft-enforced and validated |
| Hard tool/schema envelope | No in v1 |
| GJC extension loading | GJC-owned; Ouroboros uses an input bridge only as the compatibility route for storage-only hosts |
| Interactive GJC `ooo` frontdoor | Yes, via native skills/MCP autoload or the compatibility bridge selected during setup |

## v1 Limitations

- No native session continuity or targeted resume is declared in v1. Ouroboros
  can checkpoint at the workflow/event-store layer, but the GJC runtime does not
  advertise native targeted resume.
- No native approval-mode switch is exposed by GJC RPC mode. Runner-driven
  execution records the forced permission request for audit continuity, while
  the runtime capability truthfully reports that the CLI does not enforce it.
- No hard tool envelope or provider-native JSON schema enforcement is exposed to
  the LLM adapter. Structured output is soft-enforced by prompt instruction,
  extraction, validation, and retry.
- Unsupported host-interaction frames fail closed. `workflow_gate`, `host_tool`,
  `host_uri`, and `extension_ui` frames are errors until Ouroboros implements an
  explicit host contract for them.

## Troubleshooting

**`GJC not found`**
Install GJC, put `gjc` on `PATH`, or set `OUROBOROS_GJC_CLI_PATH`.

**A structured-output request fails after retries**
The GJC LLM backend uses soft JSON/schema enforcement. Inspect the surfaced
provider error and prompt output; malformed JSON or schema-invalid payloads are
rejected by Ouroboros after extraction and validation.

**`ooo ...` is sent to the wrong GJC workflow**
Run `ouroboros setup --runtime gjc` and restart GJC. On native-capable releases,
`gjc skills discover --source user --json` lists `ouroboros-interview` and
`gjc mcp list --json` reports `ouroboros` with `runtimeStatus: "autoload"`.
Storage-only releases instead keep `<agent-dir>/extensions/ouroboros-ooo-bridge/index.ts`.
