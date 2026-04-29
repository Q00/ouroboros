# MCP Best Practices

Use this guide when adding upstream MCP servers to Ouroboros through
`~/.ouroboros/mcp_servers.yaml` or `{cwd}/.ouroboros/mcp_servers.yaml`.

## Goals

External MCP servers should make the child agent more capable without making
execution brittle, slow, or unsafe. Prefer a small, named set of tools for one
workflow over a large always-on catalog.

## Recommended Server Roles

| Server | Use when | Typical scope |
|--------|----------|---------------|
| OpenCron | Scheduled browser or synthetic checks are part of verification | QA and monitoring tasks only |
| Figma | Design artifacts must guide implementation | Read-only design inspection |
| Context7 | Current library/framework documentation is needed | Documentation lookup during planning or implementation |
| Tavily | External web research is required | Research and source discovery |

## Quick Setup

For a user-wide setup, create the MCP config file under `~/.ouroboros`:

```bash
mkdir -p ~/.ouroboros
$EDITOR ~/.ouroboros/mcp_servers.yaml
chmod 600 ~/.ouroboros/mcp_servers.yaml
```

Paste the YAML shape from the next section, then replace each placeholder
command with the MCP server command used in your environment. Keep API keys in
environment variables and reference them as `${VAR_NAME}` in the YAML.

Ouroboros discovers upstream MCP config in this order:

1. `$OUROBOROS_MCP_CONFIG`
2. `~/.ouroboros/mcp_servers.yaml`
3. `{cwd}/.ouroboros/mcp_servers.yaml`

Use the home config for servers you want available across projects. Use the
project-local config when the server list or credentials are specific to one
repository. To bypass discovery for one run, pass an explicit path:

```bash
ouroboros run seed.yaml --mcp-config .ouroboros/mcp_servers.yaml
```

For the bridge lifecycle quick start, see
[`docs/guides/mcp-bridge.md`](./mcp-bridge.md).

## Configuration Pattern

```yaml
mcp_servers:
  - name: context7
    transport: stdio
    command: "<context7-mcp-command>"
    args: []
    timeout: 30

  - name: tavily
    transport: stdio
    command: "<tavily-mcp-command>"
    args: []
    env:
      TAVILY_API_KEY: "${TAVILY_API_KEY}"
    timeout: 45

  - name: figma
    transport: stdio
    command: "<figma-mcp-command>"
    args: []
    env:
      FIGMA_TOKEN: "${FIGMA_TOKEN}"
    timeout: 30

  - name: opencron
    transport: stdio
    command: "<opencron-mcp-command>"
    args: []
    timeout: 60

connection:
  timeout_seconds: 30
  retry_attempts: 3
  health_check_interval: 60
```

Replace the placeholder commands with the server package or wrapper used by
your environment. Keep credentials in environment variables rather than in the
YAML file.

## Naming

Use stable, domain-oriented server names: `context7`, `tavily`, `figma`,
`opencron`. Avoid names such as `tools` or `research` because they make logs and
tool provenance harder to audit.

If a server exports generic tool names, add `tool_prefix` in the MCP config or
wrap the server with prefixed tool names. Built-in agent tools take precedence;
colliding MCP tools may be skipped.

## Security

- Use read-only tokens for Figma and documentation servers when possible.
- Do not grant browser or filesystem tools to research-only workflows.
- Store API keys in the environment and reference them as `${VAR_NAME}`.
- Keep `mcp_servers.yaml` out of shared logs and examples when it contains
  private URLs, headers, or workspace IDs.
- Prefer separate server entries per trust boundary. For example, do not put a
  public web-research tool and an internal data tool behind the same wrapper.

## Reliability

- Set per-server `timeout` based on expected latency. Browser/synthetic QA
  servers usually need more time than documentation lookup servers.
- Keep `connection.retry_attempts` at 2 or 3. Higher values can hide broken
  credentials and slow every execution.
- Use health checks for long-running sessions, but do not rely on them as a
  substitute for per-call timeouts.
- If a workflow only needs one external server, configure only that server for
  the run. Smaller catalogs reduce startup and tool-selection noise.

## Workflow Mapping

| Workflow | Suggested servers |
|----------|-------------------|
| Research -> Deliverable | Tavily, Context7 |
| Design -> Code -> Verify | Figma, Context7, OpenCron |
| Library upgrade | Context7, optional Tavily |
| Launch QA | OpenCron, optional Context7 |

See `docs/examples/workflows/` for concrete workflow examples.
