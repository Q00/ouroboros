# GJC Runtime

Run Ouroboros workflow execution on top of the locally installed `gjc` CLI.

The GJC runtime is an SDK-backed adapter. Ouroboros owns the workflow engine,
Seed decomposition, checkpointing, evaluation handoff, and `ooo` skill
dispatch. For each runtime task it starts GJC's Coordinator MCP server, creates
a Broker-managed SDK session, and maps durable GJC turn state into Ouroboros
`AgentMessage` values.

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
      | 2b. normal task? create a GJC SDK session
      v
gjc mcp-serve coordinator
      |
      | 3. Broker -> SessionRouter -> AgentSession
      v
GJC durable turns and questions
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
gjc mcp-serve coordinator
```

It then uses GJC's supported Coordinator MCP contract:

1. `gjc_coordinator_start_session` creates a Broker-managed SDK session and
   submits the initial prompt with a caller-owned idempotency key.
2. `gjc_coordinator_await_turn` reads durable turn state until completion,
   failure, cancellation, or a structured question.
3. `gjc_coordinator_read_tail` recovers bounded last-assistant output when the
   terminal turn does not inline its final response.
4. `gjc_coordinator_list_questions` and
   `gjc_coordinator_submit_question_answer` preserve question correlation across
   an Ouroboros `RuntimeHandle` resume.
5. `gjc_coordinator_stop_session` closes completed ephemeral sessions through
   SDK lifecycle authority.

Ouroboros never reads GJC endpoint records or credentials and never opens a
private SDK WebSocket. GJC's Broker and `SessionRouter` remain the sole owners
of endpoint discovery, authentication, generation fencing, and turn delivery.

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
returning them. The GJC SDK surface does not currently provide a hard
provider-native schema envelope, so malformed structured responses are retried
and then surfaced as provider errors.

Use GJC as the runtime backend when you want GJC to execute Seed tasks; use
`llm.backend: gjc` when the authoring/evaluation flow can accept adapter-level
JSON extraction and validation rather than provider-native schema enforcement.

## Capabilities

| Capability | Status |
|------------|--------|
| Headless execution | Yes, through Coordinator MCP and Broker-managed SDK sessions |
| Skill shortcut dispatch | Yes, before starting a GJC session |
| Native targeted resume | Yes, through SDK session IDs and question-bound runtime handles |
| Structured event/state | Yes, durable Coordinator turn and question projections |
| Native permission override | No; the SDK session keeps GJC's configured permission policy, so `permission_mode_support=ignored` |
| Structured schema responses as LLM backend | Soft-enforced and validated |
| Hard tool/schema envelope | No |
| GJC extension loading | GJC-owned; Ouroboros setup installs the bridge artifact, but activation depends on the GJC extension policy |
| Interactive GJC `ooo` frontdoor | Via GJC's configured MCP/skill path; the optional filesystem extension is not part of the SDK backend contract |

## Limitations

- GJC owns the active tool and permission policy. Ouroboros tool allow-lists and
  permission-mode overrides are reported as ignored rather than silently
  claiming enforcement.
- LLM structured output remains prompt-enforced and validated after the turn;
  GJC does not expose a provider-native output-schema envelope through this SDK
  route.
- Coordinator-created sessions and turn journals are durable. Cleanup failures
  do not replace a completed result; the next Coordinator startup can reconcile
  retained state through GJC's own lifecycle authority.

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
