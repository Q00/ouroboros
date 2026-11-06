# Ouroboros troubleshooting for assistants

Use this guide to investigate an Ouroboros problem with the user. It describes
the commands shipped with this version; installations, hosts and releases can
differ. Check the installed command's `--help` before using an option. If the
host skill is available but no standalone `ouroboros` command exists, do not
assume the host's MCP server uses a terminal installation or install one merely
to diagnose it.

## 1. Identify the environment

Ask for the symptom, exact error, host and operating system, working directory,
and the command or skill used. Identify whether the problem concerns a host
plugin/MCP server or a standalone CLI, and preserve any actual job, session,
auto-session or execution IDs. On a shell with a standalone CLI, use
`command -v ouroboros` and `ouroboros --version` (on PowerShell use
`Get-Command ouroboros`). These identify that CLI, not a separately launched
host server. Inspect the host's own plugin and MCP status through its supported
interface; record which server and version are actually known. Do not infer
model authentication from a version or an installed plugin.

Outputs can contain private paths, project names, prompts, identifiers, server
launch arguments and even secrets embedded in arguments or errors. Ask the user
to review and redact output before sharing; never request credentials or dump
entire configuration files. Report what was observed separately from inference.

## 2. Diagnose before changing anything

For this version the following CLI diagnostics inspect local state without
repairing it. They may be unavailable on older installations. Run only those
relevant to the problem, and inspect each check and warning, not just exit code:

- `ouroboros doctor install` identifies installation surfaces and configured
  launchers. Its output can expose launcher arguments.
- `ouroboros status health` checks configuration, database path, runtime
  availability and credential presence. An `ok` is not proof of authentication,
  successful model calls, or a live host connection.
- `ouroboros mcp doctor` checks MCP prerequisites; its default does not start a
  server. `--machine-snapshot` adds static machine metadata. The ordinary
  checks may import dependencies that attempt network requests; the command is
  not an offline or OS-sandbox guarantee.
- `ouroboros mcp doctor-runtime` checks bounded executable provenance, loopback
  bindability and own-server registry metadata. It does not test the actual
  host connection or model.

`--json` is available for the doctor commands; with `mcp doctor`, adding
`--machine-snapshot` changes the top-level JSON from a list to an object with
`checks` and `machine_snapshot`. Redact either form before sharing. An optional
`ouroboros mcp doctor --probe-local-stdio` starts a temporary local child and
checks discovery without calling tools. It does not test the host's configured
launcher, authentication or model. Its Python-level restrictions are not OS
confinement, and interpreter startup can precede those restrictions. Use it only
when the extra process and temporary files are appropriate.

## 3. Inspect work and recover carefully

A receipt is not a completed result. In the original host, use the available
job/session status tools for the actual ID. For a standalone CLI, consult
`ouroboros job --help`, `ouroboros status --help`, and `ouroboros resume --help`;
`ouroboros job status JOB_ID`, `ouroboros status execution EXECUTION_ID`,
`ouroboros status auto AUTO_SESSION_ID`, and `ouroboros resume --list` are
examples for their respective ID types. Replace placeholders with real IDs.
Lists may be limited and may exclude completed sessions. A status command's
success indicates a successful query, not necessarily a successful run; stored
status alone does not prove a process is alive.

Before resuming, restarting, cancelling, updating, or changing settings, inspect
the existing owner, state, workspace and possible external effects. Do not
start a duplicate run just because a response was lost. Keep records and user
files; explain the proposed action and its consequences. Follow the user's
instructions and permissions for any change.

## 4. Verify the outcome

Check the Seed's intended target and acceptance criteria, changed files,
actual test or evaluation output, and unresolved failures. An ID, a successful
health check, a terminal job state, or an evaluation request alone is not proof
that the user's goal was met. Report checks performed, results and gaps; do not
claim real-host, authentication, model or recovery verification unless observed.
