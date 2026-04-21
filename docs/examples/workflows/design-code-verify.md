# Workflow Example: Design -> Code -> Verify

This workflow is for UI work where implementation should follow a design source
and then be verified through local and browser-based checks.

## MCP Servers

Recommended upstream servers:

- `figma` for design inspection.
- `context7` for framework or component-library documentation.
- `opencron` for synthetic browser or endpoint QA when a deployed/staging URL
  is available.

Example config shape:

```yaml
mcp_servers:
  - name: figma
    transport: stdio
    command: "<figma-mcp-command>"
    args: []
    env:
      FIGMA_TOKEN: "${FIGMA_TOKEN}"

  - name: context7
    transport: stdio
    command: "<context7-mcp-command>"
    args: []

  - name: opencron
    transport: stdio
    command: "<opencron-mcp-command>"
    args: []
    env:
      OPENCRON_API_KEY: "${OPENCRON_API_KEY}"

connection:
  timeout_seconds: 45
  retry_attempts: 2
```

## Seed Shape

```yaml
goal: "Implement the settings panel from the referenced Figma frame."
constraints:
  - "Use the existing component system."
  - "Do not add new styling primitives unless required by the design."
  - "Keep keyboard and responsive behavior intact."
acceptance_criteria:
  - "Implemented UI matches the referenced layout, spacing, and states."
  - "Local component tests or app tests pass."
  - "Browser QA confirms the panel renders at desktop and mobile sizes."
```

## Execution Notes

1. Pull only the relevant Figma frame or component data. Avoid asking for the
   entire file unless the design depends on global tokens.
2. Use Context7 when framework or library behavior is uncertain.
3. Implement against existing local components first.
4. Run local tests before browser/synthetic QA.
5. Use OpenCron or browser checks for deployed/staging verification when the
   change cannot be fully validated locally.

## QA

For external QA, request concrete evidence:

```text
Run the settings-panel smoke check. Return viewport, URL, status, duration,
and any visual or interaction failure.
```

If no deployed URL exists, keep QA local and state that external verification
was not applicable for the branch.
