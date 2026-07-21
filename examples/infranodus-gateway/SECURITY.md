# Security policy and trust boundary

## Protected assets

- `INFRANODUS_API_KEY` and its bearer header.
- Ouroboros requirements, seeds, traces, delivery evidence, and local state.
- InfraNodus saved graph inventory and account metadata.
- MCP protocol integrity on stdout.

## Enforced controls

- The API key is accepted only from the environment and is never logged, persisted, or returned.
- Input is normalized and screened before the upstream client is invoked.
- The upstream endpoint and query set are constructed internally; callers cannot provide an endpoint or persistence flag.
- `doNotSave=true` is present on every graph-analysis request.
- Response projection reads only selected extended-summary fields, returns at most eight observations/five actions, and excludes raw graph, statements, request content, and numeric confidence.
- Upstream error bodies are discarded. Failures become `DEGRADED_NO_GRAPH` at the gateway boundary.
- The cache is process memory only. The optional state directory is 0700 and `ledger.jsonl` is 0600 with hashed metadata only.
- The server is stdio-only and registers exactly three closed-world tools.

## Reporting

Do not paste credentials, raw sensitive inputs, saved graph names, or upstream error bodies into an issue. Report the operation name, gateway version, bounded status, and a redacted reproduction.
