# Ouroboros Runtime Guide: Hermes Agent

This guide covers how to use Ouroboros with the [Hermes Agent](https://github.com/NousResearch/hermes-agent) as an execution runtime.

## Installation

To use Hermes with Ouroboros, ensure you have the Hermes CLI installed (v0.8.0 or higher):

```bash
# Verify installation
hermes version
```

## Setup

Run the Ouroboros setup command and select the `hermes` runtime:

```bash
ouroboros setup --runtime hermes
```

This will:
1.  Configure `~/.ouroboros/config.yaml` to use the `hermes` backend.
2.  Install Ouroboros skills into `~/.hermes/skills/autonomous-ai-agents/ouroboros/`.
3.  Register the Ouroboros MCP server in `~/.hermes/config.yaml`.

## Usage

Once configured, Ouroboros will use Hermes as the orchestrator runtime backend. This does not rewrite `llm.backend`; interview, ambiguity scoring, and other LLM-only flows continue to use the configured LLM adapter.

### Executing Workflows

```bash
ouroboros run seed.yaml --runtime hermes
```

### Scripting with Hermes

You can use the `ooo` command prefix inside a Hermes session to trigger Ouroboros skills:

```bash
hermes chat -q "ooo interview 'Build a new CLI tool'"
hermes chat -q "ooo run seed.yaml"
```

Hermes uses the shared stateless `ouroboros.router` resolver for exact `ooo`
and `/ouroboros:` skill dispatch. Adding or changing a command only requires
updating the relevant `SKILL.md` frontmatter; the runtime keeps logging,
message assembly, and MCP invocation local. See
[Shared `ooo` Skill Dispatch Router](../guides/ooo-skill-dispatch-router.md).

## Configuration

You can customize the Hermes CLI path in `~/.ouroboros/config.yaml`:

```yaml
orchestrator:
  runtime_backend: hermes
  hermes_cli_path: ~/.local/bin/hermes
```

## Technical Details

### Session Management

Ouroboros tracks Hermes sessions using the `session_id` emitted by the Hermes CLI in quiet mode (`-Q`). This allows Ouroboros to resume conversations using the `--resume` flag.

### Permission mode

Seed execution forces `bypassPermissions` on both fresh and resumed Hermes turns. The runtime translates that contract to Hermes' native `--yolo --accept-hooks` flags so dangerous-command approvals and previously unseen shell-hook approvals cannot block a headless run.

### Output Parsing

Ouroboros parses the Hermes CLI output to extract the final response and session metadata. It automatically strips reasoning blocks and banners when running in programmatic mode.

## Active Conductor and Synapse

The installed Hermes probe emitted a session marker but could not resume that
session from its store. Ouroboros therefore advertises no Synapse delivery for
Hermes: `inform`, `after_turn`, checkpoint `redirect`, and hard `replace` fail
closed instead of pretending that a CLI flag proves continuity. Ordinary Hermes
execution and observer progress still work, and the main host explains the
unsupported delivery honestly in the user's conversation language.
