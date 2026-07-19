---
name: ooo
description: "Start Ouroboros onboarding. Use when the user sends bare `ooo`, asks to start Ouroboros, or is using Ouroboros for the first time."
---

# Start Ouroboros

Read `../welcome/SKILL.md` and follow its instructions exactly. This is the
explicit Marketplace-plugin entry point for a bare `ooo` message before any
runtime-specific setup has run.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status`
when that MCP projection is available; otherwise derive it from this skill's
actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an
evolutionary loop. The breadcrumb line must be the last line of the response.
