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
does not mean GJC internals are imported into Ouroboros, and it does not mean
GJC's interactive command UI becomes part of the Ouroboros command router unless
the managed GJC-side `ooo` bridge extension is installed by setup.

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

# 2. Point Ouroboros at GJC and install the GJC-side ooo bridge
ouroboros setup --runtime gjc

# 3. Run a workflow through the configured runtime
ouroboros run workflow seed.yaml

# 4. In GJC, restart or reload extensions if needed, then:
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
or `/ouroboros:run`, the interceptor resolves the skill and invokes the matching
Ouroboros MCP handler. GJC does not receive that prompt as ordinary chat input.

This means:

- `ooo interview` in an Ouroboros-controlled GJC runtime means "Ouroboros
  handles the interview command, using the configured LLM backend for
  authoring."
- GJC only runs normal Seed execution prompts after the command dispatch path
  has decided the input is not an `ooo` shortcut.

### GJC Launches Ouroboros

`ouroboros setup --runtime gjc` also installs a managed GJC bridge extension:

```text
<agent-dir>/extensions/ouroboros-ooo-bridge/index.ts
```

After GJC loads that extension, interactive GJC sessions can type:

```text
ooo auto build a small CLI
ooo interview clarify this feature
/ooo status auto --resume auto_...
```

The extension intercepts exact-prefix `ooo ...` input and runs:

```text
ouroboros dispatch --runtime gjc --cwd <gjc-session-cwd> "ooo ..."
```

That hidden `dispatch` entrypoint uses the same shared skill resolver and MCP
handler composition as the runtime adapters. It is a bidirectional bridge:
Ouroboros can launch GJC for execution, and a GJC-side extension can route
interactive `ooo` commands back into Ouroboros.

The bridge only consumes commands that the hidden dispatcher can execute through
MCP-backed skill frontmatter. Commands that are first-party shortcuts but do not
declare an MCP dispatch target are returned to GJC with a deterministic
unsupported-dispatch exit code so the normal GJC session can continue handling
the input instead of receiving a hard bridge failure.

The bridge passes exit code `78` through as an unsupported-dispatch result. It
also includes a recursion guard so an `ooo` command produced by the bridge is not
intercepted and re-dispatched into Ouroboros again.

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

## Live QA

Live GJC QA is opt-in and is skipped by default. The tests require all gates:
`OUROBOROS_LIVE_GJC=1`, a resolvable `gjc` binary via `OUROBOROS_GJC_CLI_PATH`
or `PATH`, and a cheap `gjc --mode rpc` readiness/auth probe. Missing gates are
reported as skips with actionable reasons; a skipped live run is not an exercised
pass badge.

Hermetic runs should continue excluding the marker:

```bash
uv run python -m pytest -m "not live_gjc" --co -q
uv run python -m pytest -m "not live_gjc" tests/unit/orchestrator/test_gjc_runtime.py -q
```

To run the live QA when GJC is installed and authenticated:

```bash
OUROBOROS_LIVE_GJC=1 uv run python -m pytest -m live_gjc tests/live/test_gjc_agentos_live.py -vv
OUROBOROS_LIVE_GJC=1 uv run python tools/live_gjc_agentos_qa.py
```

The wrapper prints a QA receipt with `EXERCISED` versus `SKIPPED` counts plus the
resolved GJC binary path and version. The `ooo` bridge test is deterministic: it
checks the installed extension source dispatches `ouroboros dispatch --runtime
gjc`. Interactive GJC PTY round-trip confirmation remains a manual receipt step.

## Capabilities

| Capability | Status |
|------------|--------|
| Headless execution | Yes, through `gjc --mode rpc` |
| Skill shortcut dispatch | Yes, before spawning GJC |
| Native targeted resume | No in v1; `targeted_resume=False` and checkpointing stays at the Ouroboros lineage layer |
| Structured event stream | Yes, RPC agent events parsed by the GJC runtime |
| Structured schema responses as LLM backend | Soft-enforced and validated |
| Hard tool/schema envelope | No in v1 |
| GJC extension loading | GJC-owned; setup installs the bridge into `<agent-dir>/extensions` |
| Interactive GJC `ooo` frontdoor | Yes, via managed setup-installed extension |

## v1 Limitations

- No native session continuity or targeted resume is declared in v1. Ouroboros
  can checkpoint at the workflow/event-store layer, but the GJC runtime does not
  advertise native targeted resume.
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

**`ooo ...` is sent to the model as ordinary chat inside GJC**
Run `ouroboros setup --runtime gjc`, then restart or reload GJC. Confirm that
`<agent-dir>/extensions/ouroboros-ooo-bridge/index.ts` exists.
