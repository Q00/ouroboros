# Ouroboros × InfraNodus Gateway

A standalone, minimum-privilege MCP adapter that gives an Ouroboros operator three bounded InfraNodus graph-analysis gates without registering the upstream InfraNodus tool catalog or modifying either project.

## v1 contract

The server exposes exactly:

- `graph_review_seed`: compare requirements with a proposed seed before `ooo run`.
- `graph_diagnose_stagnation`: request a lateral graph perspective when a run is repeating one assumption.
- `graph_compare_delivery`: compare acceptance criteria with delivery evidence before accepting `ooo qa` results.

Every operation is read-only, non-destructive, idempotent, and closed-world. The adapter calls only `/graphsAndStatements` or `/graphAndStatements`, always with `doNotSave=true`. It has no InfraNodus write operation, GraphRAG path, URL ingestion, saved-graph lookup, HTTP/SSE listener, Ouroboros database access, or dynamic tool discovery.

## Architecture

```text
Ouroboros operator / MCP host
             |
             | stdio; exactly 3 private tools
             v
 input policy -> Gateway -> allowlisted Infra client -> InfraNodus hosted API
      |            |
      | reject     +-> memory-only idempotency cache
      |                metadata-only 0600 ledger
      v
 fail closed     bounded GraphAdvice / DEGRADED_NO_GRAPH
```

Raw inputs are normalized in memory and rejected if they contain secret-like values, email addresses, URLs, raw-code indicators, more than 20,000 characters, or more than 64 KiB. Neither raw input nor graph advice is persisted. The optional ledger stores only a timestamp, operation name, SHA-256 request key, status, and cache disposition.

## Install and run

Requirements: Node.js 22 and an `INFRANODUS_API_KEY` supplied through the parent process environment.

```bash
npm install
npm run build
INFRANODUS_API_KEY="..." GATEWAY_STATE_DIR="$PWD/.gateway-state" node dist/stdio.js
```

Do not put the API key in repository files or command arguments. Configure the MCP host to inherit `INFRANODUS_API_KEY`, run `node`, and pass the absolute `dist/stdio.js` path as its sole argument. Stdout is reserved for MCP protocol traffic; operational failures are returned as bounded tool results or written to stderr.

## Verification

```bash
npm run typecheck
npm test
npm run test:stdio
npm audit --omit=dev
INFRANODUS_API_KEY="..." npm run verify:live
```

`verify:live` snapshots the private graph inventory before and after three sanitized real API calls. It prints only inventory counts/digests and bounded result metadata. It fails if any operation degrades, a response violates `GraphAdvice`, or the inventory changes.

Start with the [Korean integration manual](docs/INTEGRATION_MANUAL_KO.md) for installation, Codex MCP registration, operations, troubleshooting, updates, and rollback. See [docs/OUROBOROS_RUNBOOK.md](docs/OUROBOROS_RUNBOOK.md) for the concise phase gates and [SECURITY.md](SECURITY.md) for the trust boundary.

## Explicit non-goals

- Automatic interception of Ouroboros lifecycle events.
- Registration inside the Ouroboros agent tool catalog.
- InfraNodus graph creation, updates, deletion, retrieval, GraphRAG, or full MCP proxying.
- Numeric confidence claims or autonomous go/no-go decisions.

Ouroboros 0.50.5 does not expose top-level `evaluate` or `unstuck` CLI commands. Its lateral/evaluation handlers are MCP-internal, so v1 intentionally uses operator-invoked advisory gates rather than claiming lifecycle hooks that do not exist.
