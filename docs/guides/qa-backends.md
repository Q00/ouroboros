# QA Backends

Ouroboros QA has two layers:

- In-process evaluation tools such as `ouroboros_qa`, `ouroboros_evaluate`,
  and the mechanical verification pipeline.
- Upstream MCP servers that provide external checks, browsers, scheduled
  probes, or domain-specific validators.

Use an external QA backend when correctness depends on behavior outside the
local repository: browser rendering, deployed endpoints, cron behavior, API
latency, or third-party integrations.

## OpenCron Backend

OpenCron is useful when QA needs synthetic checks or scheduled verification.
Keep it scoped to verification tasks and avoid exposing broad filesystem or
credential access through the same server.

Example upstream entry:

```yaml
mcp_servers:
  - name: opencron
    transport: stdio
    command: "<opencron-mcp-command>"
    args: []
    env:
      OPENCRON_BASE_URL: "${OPENCRON_BASE_URL}"
      OPENCRON_API_KEY: "${OPENCRON_API_KEY}"
    timeout: 60

connection:
  timeout_seconds: 45
  retry_attempts: 2
  health_check_interval: 60
```

Use a dedicated API key for QA. The key should only be able to read or run the
checks required by the workflow.

## When To Use

Use an external QA backend for:

- Verifying a deployed URL after a code change.
- Running browser or endpoint checks that cannot be represented by local unit
  tests.
- Confirming scheduled jobs or webhook-like behavior.
- Producing QA evidence for a PR when local tests are not enough.

Avoid it for:

- Pure unit-test coverage.
- Static code review.
- Checks that require production write access.

## Prompting Pattern

When a seed relies on an external QA backend, make the verification surface
explicit:

```yaml
acceptance_criteria:
  - "Local tests pass for the changed module."
  - "OpenCron synthetic check for the target workflow passes."
  - "QA output includes the checked URL, check name, and timestamp."
```

For manual tool calls, include the target, environment, and success threshold:

```text
Use the OpenCron MCP tools to run the checkout smoke check against staging.
Return the check name, target URL, status, duration, and any failing step.
```

## Result Handling

External QA output should be copied into the final execution summary or PR body
only as evidence, not as the sole source of truth. Keep local mechanical tests
in the pipeline wherever possible.

If the external QA backend is unavailable, mark the result as blocked or
inconclusive. Do not silently treat a transport failure as a passed QA check.
