# Workflow Example: Research -> Deliverable

This workflow is for tasks where the agent must gather current external
information and turn it into a concrete artifact such as a report, plan, spec,
or comparison matrix.

## MCP Servers

Recommended upstream servers:

- `tavily` for web/source discovery.
- `context7` for library and framework documentation.

Example config shape:

```yaml
mcp_servers:
  - name: tavily
    transport: stdio
    command: "<tavily-mcp-command>"
    args: []
    env:
      TAVILY_API_KEY: "${TAVILY_API_KEY}"

  - name: context7
    transport: stdio
    command: "<context7-mcp-command>"
    args: []

connection:
  timeout_seconds: 30
  retry_attempts: 3
```

## Seed Shape

```yaml
goal: "Create a sourced implementation brief for adding streaming responses."
task_type: research
constraints:
  - "Use official documentation for API behavior."
  - "Separate facts from recommendations."
  - "Include links or source identifiers for non-obvious claims."
acceptance_criteria:
  - "Brief explains the recommended implementation path."
  - "Brief lists risks, unknowns, and verification steps."
  - "All current API claims are attributed to fetched sources."
ontology_schema:
  name: "implementation_brief"
  description: "Structured sourced brief for an engineering decision."
  fields:
    - name: "recommendation"
      field_type: "markdown"
      description: "Recommended implementation path and rationale."
    - name: "sources"
      field_type: "list"
      description: "Source identifiers or links for current external claims."
    - name: "risks"
      field_type: "list"
      description: "Known risks, unknowns, and verification steps."
metadata:
  ambiguity_score: 0.15
```

## Execution Notes

1. Start with Tavily only when the topic requires broad discovery.
2. Use Context7 for exact library/API behavior before writing implementation
   guidance.
3. Ask the agent to preserve citations in the deliverable rather than only in
   intermediate notes.
4. Keep the final artifact narrow enough to act on: decisions, tradeoffs,
   implementation steps, and verification.

## QA

Run `ouroboros_qa` on the final deliverable with a quality bar such as:

```text
The brief must distinguish sourced facts from recommendations, cite external
claims, and include enough implementation detail for an engineer to start.
```
