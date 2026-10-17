# Antigravity CLI Runtime

Run Ouroboros workflows on top of the locally installed **Antigravity CLI**
(the `agy` binary) — Google's successor to the Gemini CLI.

On **2026-06-18** the Gemini CLI stops serving the Google AI Pro / Ultra / free
consumer tiers and migrates those users to the Antigravity CLI (enterprise and
API-key Gemini access is unaffected). If you authenticated the Gemini runtime
through a Pro/Ultra subscription, switch the affected stages to the
`antigravity` runtime.

The Antigravity runtime extends the Gemini runtime (Antigravity is Google's Gemini-CLI successor): Ouroboros owns the orchestration loop and invokes `agy -p --output-format stream-json` for each task. It normalizes native assistant, tool, session, and terminal events into Ouroboros messages. Workflow checkpoints remain at the Ouroboros layer (event store + lineage). The installed CLI must support `--output-format stream-json`; CLI-dependent behavior is documented in `docs/antigravity-stream-contract.md`.

## Prerequisites

| Requirement       | Why                                                              |
|-------------------|------------------------------------------------------------------|
| `agy` CLI         | Provider — the Antigravity CLI binary, installed in `~/.local/bin`|
| Google auth       | Run `agy` once (browser OAuth) — Google AI Pro/Ultra/free, or set `ANTIGRAVITY_API_KEY` |
| Ouroboros (base)  | `pip install ouroboros-ai` — no provider-specific extras         |

> Antigravity runs on the **base** Ouroboros package. It does **not** require
> the `[claude]` extra.

## Quick start

```bash
# 1. Authenticate the Antigravity CLI (one-time browser OAuth)
agy            # first launch opens the sign-in flow

# 2. Point Ouroboros at Antigravity (config.yaml or the settings GUI)
ouroboros config           # GUI: set Execute/Evaluate/... runtime to "antigravity"
# or set OUROBOROS_ANTIGRAVITY_CLI_PATH / orchestrator.antigravity_cli_path

# 3. Run a workflow
ouroboros run workflow seed.yaml --runtime antigravity
```

## CLI path resolution

The runtime looks for the binary in this order:

1. Constructor argument `cli_path=...`
2. `OUROBOROS_ANTIGRAVITY_CLI_PATH` environment variable
3. `orchestrator.antigravity_cli_path` in `~/.ouroboros/config.yaml`
4. `agy` on `$PATH`

## Configuration

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: antigravity
  antigravity_cli_path: ~/.local/bin/agy   # optional; auto-detected
```

Antigravity is **runtime-only** — it drives the agentic orchestrator but is not
registered as an LLM-completion backend (`agy -p` returns plain text, not a
structured JSON/schema payload), so it is not a valid `llm.backend` value. Use
the runtime via `orchestrator.runtime_backend`, per-stage routing, or
`--runtime antigravity`. For completion-level diversity (consensus / role
models) route Gemini/other vendors through `litellm`/OpenRouter.

### Per-stage routing

Antigravity shines as a cross-vendor stage in a multi-LLM pipeline — for
example, generate with one vendor and verify with another:

```yaml
orchestrator:
  runtime_backend: claude
  runtime_profile:
    stages:
      execute: claude          # generate
      evaluate: antigravity    # cross-vendor verification (Gemini 3.x via agy)
```

## Headless contract

Each task spawns:

```text
agy -p <PROMPT> \
    --dangerously-skip-permissions \
    [--model <MODEL>]
```

| Flag                             | Why                                                       |
|----------------------------------|-----------------------------------------------------------|
| `-p` (`--print` / `--prompt`)    | Runs a single prompt non-interactively, prints the answer |
| `--dangerously-skip-permissions` | Auto-approves tool requests so the subprocess never blocks|
| `--model`                        | Forwarded only for an explicit, non-sentinel model id     |

`agy` has a native `--mode accept-edits` option. In headless use, however,
approval-required calls may be soft-denied without an interactive prompt; the
native mode therefore does not establish an edit-only permission guarantee for
Ouroboros. To preserve its existing behavior, both Ouroboros non-blocking
permission modes continue to map to the skip flag:

| Ouroboros permission mode | `agy` behavior                  | Notes                                         |
|---------------------------|---------------------------------|-----------------------------------------------|
| `acceptEdits` (default)   | `--dangerously-skip-permissions`| Over-approximated to full skip (no narrower mode) |
| `bypassPermissions`       | `--dangerously-skip-permissions`| Explicit full bypass                          |

The interactive Ouroboros `default` mode is normalized to `acceptEdits` (with an
audit log), and consequently keeps the same full-bypass mapping in headless
execution.

> ⚠️ **Security note — `acceptEdits` is full auto-approval here.** Unlike the
> Gemini parent runtime (where `acceptEdits` maps to the narrower edits-only
> `--approval-mode auto_edit`), Antigravity's native `--mode accept-edits` does
> not change Ouroboros's established mapping: **both** `acceptEdits` (the
> default) and `bypassPermissions` resolve to
> `--dangerously-skip-permissions` — every tool request is auto-approved, not
> just file edits. This mapping applies only when you explicitly select the
> `antigravity` backend. Run Antigravity stages in a sandbox/worktree
> (Ouroboros uses managed git worktrees by default) and do not select it for
> untrusted inputs if you need edits-only containment — use the Claude, Codex,
> or Gemini runtime there instead.

Use [`agy models`](https://www.antigravity.google/docs/models/) for the
installed CLI/account catalog. On `agy 1.4.0` (checked 2026-10-18), it lists
`gemini-3.8-flash-{low,medium,high}`, `gemini-4-argon-{low,medium,high}`,
`claude-sonnet-5-5-{low,medium,high}`, `claude-opus-5-5-{low,medium,high}`, and
`gpt-oss-120b-medium`. These are native CLI IDs, not Google or Anthropic API
model IDs; availability varies by version, account, and plan. Antigravity is
a **sentinel-model backend** — by default Ouroboros lets `agy` pick its own
configured model and only forwards `--model` when you set an explicit id.

## Capabilities

| Capability               | Status                                                |
|--------------------------|-------------------------------------------------------|
| Headless execution       | ✅ `agy -p`                                           |
| Tool calls               | ✅ (Antigravity-managed via `--dangerously-skip-permissions`) |
| Structured event stream  | ❌ plain-text stdout (no `--output-format`)           |
| Session resumption       | ❌ not surfaced in v1 (recovery at the Ouroboros lineage layer) |
| LLM-completion backend   | ❌ runtime-only                                       |

If you need a structured event stream or resumable sessions, use the Claude,
Codex, or Grok runtime.

## Troubleshooting

**`agy` not found.** Install the Antigravity CLI, then set
`OUROBOROS_ANTIGRAVITY_CLI_PATH=/abs/path/to/agy` or
`orchestrator.antigravity_cli_path`.

**A headless call needs approval.** Ouroboros passes
`--dangerously-skip-permissions`, which auto-approves tool requests. Direct
headless use of `agy` with a mode that requires approval may soft-deny the call
without an interactive prompt; it does not necessarily hang.

**Gemini stopped working on 2026-06-18.** That is the consumer-tier Gemini CLI
cutoff. Switch the affected stages to `antigravity`, or keep Gemini via an
enterprise/API-key credential (unaffected).
