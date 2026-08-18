# Ouroboros Runtime Guide: Kiro CLI

> 한국어: [kiro.ko.md](./kiro.ko.md)

This guide covers how to use Ouroboros with the [Kiro CLI](https://kiro.dev/docs/cli/)
as an execution runtime. Kiro is run in its **headless mode**
(`kiro-cli chat --no-interactive`, documented at
<https://kiro.dev/docs/cli/headless/>).

## Installation

Kiro CLI must be installed and authenticated before Ouroboros can use it:

```bash
# Verify installation — 2.2.0 or higher is required for --resume-id support
kiro-cli --version
kiro-cli chat --help | grep -- --resume-id
```

Sign in to Kiro once using whatever flow your Kiro distribution provides
(AWS Builder / IAM sign-in, etc.) so headless invocations work without
prompting.

## Setup

```bash
pipx install 'ouroboros-ai[mcp]'         # or: uv tool install 'ouroboros-ai[mcp]'
ouroboros setup --runtime kiro
```

Setup requires `uvx` or `pipx` so the MCP 2 server cannot inherit an
incompatible host Python environment. If neither launcher is available, setup
exits non-zero before changing `~/.ouroboros/config.yaml`.

This will:

1. Confirm `kiro-cli` is on `PATH` (or honour `OUROBOROS_KIRO_CLI_PATH` /
   `orchestrator.kiro_cli_path` from your config).
2. Write to `~/.ouroboros/config.yaml`:
   ```yaml
   orchestrator:
     runtime_backend: kiro
     kiro_cli_path: /usr/local/bin/kiro-cli  # whatever was detected
   llm:
     backend: kiro
   ```
3. Register the Ouroboros MCP server in `~/.kiro/settings/mcp.json` with
   the `env` block pre-seeded so `ooo <skill>` shortcuts dispatch to the
   Kiro adapter automatically:
   ```json
   {
     "mcpServers": {
       "ouroboros": {
         "command": "uvx",
         "args": ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
         "disabled": false,
         "env": {
           "OUROBOROS_RUNTIME": "kiro",
           "OUROBOROS_LLM_BACKEND": "kiro"
         }
       }
     }
   }
   ```

   If `uvx` is unavailable but `pipx` exists, setup writes the equivalent
   isolated launcher instead:
   ```json
   {
     "command": "pipx",
     "args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
   }
   ```

Setup is idempotent — re-running preserves any peer MCP entries and
custom `env` keys. The entry always uses `uvx --isolated` or `pipx run` so the
server receives the MCP 2 dependency profile in an isolated package
environment. Kiro may need a longer timeout on the first `uvx` launch;
setup never substitutes a faster global binary with an unknown MCP major.

### Model discovery

The settings UI queries the models available to the currently authenticated
Kiro account instead of shipping a global Kiro model list. It runs
`kiro-cli chat --listmodels -f json` and retries the newer
`--list-models --format json` spelling for compatible CLI releases. The
returned ids are merged into the picker without replacing Kiro's `default`
sentinel, so account tier, region, and enterprise model allow-list changes are
reflected locally. If discovery fails or Kiro is offline, the picker keeps
`default` and the custom-model entry rather than guessing from a third-party
catalog.

## Usage

Open a Kiro session from the directory you want to work in:

```bash
cd ~/projects/my-new-idea
kiro-cli chat
```

Inside the session, the skill shortcuts behave the same way they do in
Claude Code or Codex:

```
> ooo interview "I want to build a todo list CLI"
```

Kiro will invoke the `ouroboros_interview` MCP tool, stream a Socratic
question, and hand control back to you for the answer. Continue the
interview turn-by-turn until the ambiguity score drops to `≤ 0.2` and
Ouroboros declares the session READY; then run `ooo seed` (or call
`ouroboros_generate_seed` directly) to crystallise the Seed YAML.

### Executing Workflows

After a Seed exists, either stay inside Kiro and call
`ouroboros_execute_seed`, or drive it from the terminal with the Kiro
runtime selected:

```bash
ouroboros run ~/.ouroboros/seeds/seed_<id>.yaml --runtime kiro
```

### Skill dispatch layering

Kiro uses the shared stateless `ouroboros.router` resolver plus the new
`SkillInterceptor`: `ooo <skill>` and `/ouroboros:<skill>` prefixes are
matched **before** the Kiro subprocess spawns. Skill dispatch therefore
runs identically across Kiro / Codex / Claude. See
[Shared `ooo` Skill Dispatch Router](../guides/ooo-skill-dispatch-router.md).

### Permission mode

Runner-driven seed execution forces `bypassPermissions` for both fresh and
resumed Kiro dispatches. Kiro translates that contract to
`--trust-all-tools`. If the same call carries a tool envelope, the envelope is
still included as prompt guidance, but it cannot downgrade the native approval
boundary to `--trust-tools`.

### Targeted resume (caller-supplied session id)

When a caller passes a known session id, the adapter forwards it to Kiro's native `--resume-id` flag (invalid or shell-unsafe ids are rejected at argv-build time):

```bash
kiro-cli chat --no-interactive --resume-id 6f8a3c21-... "next turn"
```

Unlike bare `--resume` (which resumes the **most recent** session in the directory), `--resume-id` honours the requested id exactly.

Ouroboros does **not** currently capture Kiro session ids from a normal `execute_task` run — headless mode does not surface them on stdout. That limits the built-in checkpoint/resume story to callers who retrieve ids out-of-band via `kiro-cli chat --list-sessions -f json`. See *Declared capabilities* below for the honest flag; this is future-work territory.

## Declared capabilities

Kiro's `KiroAgentAdapter.capabilities` evaluates to:

```python
RuntimeCapabilities(
    skill_dispatch=True,
    targeted_resume=False,
    structured_output=False,
)
```

`targeted_resume=False` reflects a concrete limitation of Kiro headless mode: `kiro-cli chat --no-interactive` does not surface the session id on stdout or stderr during the run, so the adapter cannot capture a resumable handle from normal execution. Session ids are only visible afterwards via `kiro-cli chat --list-sessions`. The adapter still understands `--resume-id <session_id>` when a caller provides an externally-sourced id, but does not advertise native resume capability it cannot honor end-to-end. Wiring `--list-sessions -f json` into completion (or adopting `kiro-cli acp`) would flip this flag in a future PR.

`structured_output=False` reflects that Kiro headless emits plain-text stdout (with ANSI prompt markers stripped by the adapter) rather than the JSONL event streams Claude / Codex produce. Callers that depend on structured events should branch on `capabilities.structured_output` instead of backend names so that a future ACP-based Kiro adapter can flip this flag without breaking consumers.

## Future work: ACP

Kiro also exposes an [Agent Client Protocol](https://kiro.dev/docs/cli/acp/)
surface (`kiro-cli acp`) that offers structured JSON-RPC events and
richer session management. This adapter intentionally does not use it
yet — the `RuntimeCapabilities` + `SkillInterceptor` abstraction
introduced in this PR was written so a future `KiroACPAdapter` can
simply be added and flip `structured_output=True` without changing
callers.

## Troubleshooting

### `connection closed: initialize response` in Kiro logs

Kiro's MCP init timed out before the isolated Ouroboros server responded.
Check `~/.kiro/settings/mcp.json`: the `command` must be `uvx` or `pipx`,
with the matching `ouroboros-ai[mcp]` arguments shown in Setup above. A first
`uvx` launch may need a longer Kiro startup timeout while it resolves the
package. Never replace the entry with a direct `ouroboros` or `python -m`
command; those environments cannot guarantee MCP 2.

### `I don't have a tool called ouroboros_*`

MCP server loaded with a different name or did not load at all. Ensure `uvx`
or `pipx` is on `PATH`, re-run `ouroboros setup --runtime kiro` from any
terminal, then restart the Kiro session. Setup replaces legacy direct-binary
entries with the supported isolated launcher and exits non-zero without
persisting runtime changes if no launcher is available.

### Responses start with `> ` or contain escape sequences

Kiro's headless stdout still carries terminal prompt markers; the
adapter strips SGR/CSI escapes and the leading `> ` marker before
surfacing content. If you see them leaking through, you are probably
running an older Ouroboros wheel that predates the fix — reinstall
from a version that includes commit `9d0db8a`
(`fix(kiro): strip ANSI prompt marker + color escapes from stdout`).

## Further reading

- [Kiro CLI headless mode](https://kiro.dev/docs/cli/headless/) — upstream docs
- [Runtime capability matrix](../runtime-capability-matrix.md) — cross-runtime comparison
- [Skill dispatch router](../guides/ooo-skill-dispatch-router.md) — how `ooo` shortcuts route
- [`kiro-cli acp` docs](https://kiro.dev/docs/cli/acp/) — upstream ACP surface, not consumed by this adapter
