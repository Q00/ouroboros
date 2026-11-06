# Troubleshoot Ouroboros with an AI assistant

Use this guide when installation, an MCP connection, an execution, or an update does not behave as expected. Identify the affected environment, inspect the symptom, preserve existing work, choose a targeted action, and verify the outcome. A host plugin can work without a standalone `ouroboros` command in your shell.

**Version boundary:** This guide describes source revision [`592c667f076c3dad7ef68d7844e91480d80135d0`](https://github.com/Q00/ouroboros/tree/592c667f076c3dad7ef68d7844e91480d80135d0), checked on 2026-11-07. A published package, a host plugin, and the MCP server launched by that host can be different versions. Check the help and behavior of the installation involved before applying these instructions. Source review and package inclusion do not establish that every host, authentication method, model, or recovery path has been tested.

## Start here

Tell the assistant which machine and operating system, host application, project directory, command or request, symptom, and last error are involved. Preserve the actual job, session, execution, and auto-session IDs, if any. Ask the assistant to distinguish observations from inferences, and commands it actually ran from commands it recommends.

If the affected environment has a standalone CLI, run these in its shell:

```bash
command -v ouroboros
ouroboros --version
ouroboros --help
```

In PowerShell, use `Get-Command ouroboros` to locate the command. The result identifies that shell's CLI, not necessarily the MCP server launched by a host. If the command is missing, check the host's own plugin and MCP status; do not install another CLI solely to infer the host server's version. A different machine, user, home directory, virtual environment, or host session can select a different installation. For a source checkout, `git rev-parse HEAD` identifies that checkout, not a separately installed package.

**Review output before sharing it.** Paths, project names, prompts, IDs, configuration errors, and launcher arguments can contain private information or secrets. Redact them as appropriate. Do not send credentials, tokens, a complete environment dump, or an entire configuration file. In particular, `doctor install` can display configured launcher arguments.

## Choose the symptom

| Symptom | First step | If that does not resolve it |
| --- | --- | --- |
| The shell cannot find `ouroboros`, or the host does not recognize `ooo` | Separate the standalone CLI from the host plugin; check the affected session and installed version. | Inspect the host's plugin status and installation instructions for that version. Do not assume one installation proves the other. |
| MCP tools are unavailable or connection fails | Check whether the host reports its server enabled and connected; preserve the redacted error. | Follow **MCP connection** below. |
| Authentication fails or health reports a credential warning | Identify the selected backend and the environment that actually launches it. Check its supported login or credential setup without sharing the credential. | A credential-presence check does not prove validity; verify an authorized connection separately. |
| A job appears stuck or a client disconnected | Preserve the original IDs, workspace and owner; query the existing work. | Follow **Status and recovery** below before retrying. |
| An update reports partial failure | Identify which package and refresh step changed. | Follow **Update and partial failure** below. |
| A job completed but the result seems wrong | Compare the Seed and actual files, tests and evaluation evidence. | Follow **Verify and report** below. |

## Diagnose without assuming a pass proves success

Run the following in the affected CLI environment only if that installation provides them. Consult the relevant `--help` first and inspect the individual errors and warnings, not just the exit code. These checks are diagnostic, not repair commands; nevertheless, logging, initialization, and dependency imports can create local traces or make network requests. They are not a promise of offline or OS-isolated operation.

| Command | What it inspects | What a pass does not establish |
| --- | --- | --- |
| `ouroboros doctor install` | Package, plugin and MCP installation surfaces and launcher information. | A live host connection or successful task. |
| `ouroboros status health` | Configuration, database path, runtime availability and credential presence. | Valid authentication, a model call, or successful execution. |
| `ouroboros mcp doctor` | MCP prerequisites; the default does not start a server. | The actual host connection or a model call. |
| `ouroboros mcp doctor-runtime` | Bounded executable provenance, ephemeral loopback bindability and Ouroboros server registry metadata. | A live connection between the host and server. |
| `ouroboros codex doctor` | Codex `ooo auto` dispatch artifacts and MCP configuration. | A live host connection or model call. |

`doctor install`, `mcp doctor`, and `mcp doctor-runtime` support `--json` in the source revision above. For `mcp doctor`, `--machine-snapshot --json` returns an object containing `checks` and `machine_snapshot` rather than the default list. Redact either form before sharing. A configuration error in `status health` remains important even if other rows report `ok`. See the [installation doctor](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/doctor.py), [MCP doctor](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/mcp_doctor.py), and [health implementation](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/status.py).

## MCP connection or authentication fails

1. In the original host session, check whether the plugin and Ouroboros server are enabled and whether the error concerns server startup, connection, tool discovery, or authentication. Inspect the server's command or URL and environment without exposing secrets. A CLI health pass does not prove that this host uses that CLI or server.
2. If the relevant standalone CLI is available, read the failing rows of `ouroboros mcp doctor`. For Codex, `ouroboros codex doctor` checks the `ooo auto` dispatch setup. A failure gives a starting point; inspect the suggested changes and target before applying them. A pass still does not prove the host connection.
3. If an additional process is appropriate, `ouroboros mcp doctor --probe-local-stdio` starts a temporary local child and checks startup and tool discovery without invoking a tool. It does not test the host's configured launcher, authentication, or model. Its Python-level restrictions are not OS confinement; interpreter startup can precede them. Ordinary doctor checks may also attempt network access.
4. `ouroboros codex doctor --live-mcp` instead launches the **configured stdio server** and checks discovery. Inspect that launcher and its potential network and other effects first. This option does not verify a URL transport or a model call. See the [Codex doctor implementation](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/codex.py).
5. If setup reports `No runtimes found.`, inspect the runtime executable, configuration and help in the setup process's environment. Detection can involve more than the current shell's PATH. Setup and refresh can change configuration and artifacts; a failed setup does not prove that no local state was created. Preserve existing files and inspect the failed step before retrying. See [setup](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/setup.py).

After an authorized correction, repeat the relevant check in the same host and environment. If a real connection or model call was not tested, report that limit.

## Status and recovery after a disconnect

A closed client or missing response does not establish that a worker stopped. Find the original start response and distinguish `job_id`, `session_id`, `execution_id`, and `auto_session_id`. Check the workspace, branch, process owner, and any external effects before taking action. In the original host, use the status tools it actually exposes. For a standalone CLI, examples are below; replace each placeholder with the **matching real ID**, and check the installed help first:

```bash
ouroboros job status JOB_ID
ouroboros job result JOB_ID
ouroboros status execution EXECUTION_ID --events
ouroboros status run --session-id SESSION_ID
ouroboros status auto AUTO_SESSION_ID
ouroboros resume --list
```

`job result` reads the terminal output for the same job ID, including a `failed`, `cancelled`, or `interrupted` job; it is not limited to successful work. Read its status/error fields and returned failure text or payload as evidence about the outcome. A successful query only means the record was read; it does not mean the work passed its criteria. In the cited main source, `worker_blocked` is a closed failure category derived from a worker judgement class. It does not identify the underlying reason by itself. Inspect the actual result, any linked session or execution ID, and the worker's relevant output before attributing the cause. If available through the owning host, inspect only the relevant stdout/stderr and permission, backend/runtime, and environment error details; redact tokens, keys, credentials, and unrelated environment values before sharing. The source does not guarantee that raw stdout/stderr is persisted in the job result. Preserve the existing workspace, branch, uncommitted files, and external effects; do not rerun, resume, cancel, or start a duplicate while the owner or current state is uncertain. `resume --list` lists candidates; it does not resume them. A list can be limited or omit sessions whose records could not be reconstructed, so an empty list is not proof that no work exists. A database open error is not an empty history. See [job result and status semantics](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/mcp/tools/job_handlers.py), [worker failure categories](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/orchestrator/run_failure_cause.py), [status](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/status.py), and [resume listing](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/resume.py). The cited source is main at `592c667`; installed releases and host integrations may differ.

Before resuming, cancelling, or starting a new run, establish the original owner and terminal state, Seed **file path**, target workspace and branch, uncommitted files, and effects already sent to external systems. Do not start a duplicate because a receipt was lost. In the cited revision, a particular `run workflow --resume` failure caused by an unavailable process-local check package can record the original session as failed and start a new attempt. Inspect the installed behavior and target before choosing a recovery action. Cancellation does not undo completed external effects. See [run recovery](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/run.py).

If the owner, state, target, or external effects remain unknown, preserve the records and ask the person responsible before guessing or starting another attempt.

## Update and partial failure

`ouroboros update --check` queries remote release information; it is not an offline check. Before updating, identify the package manager and executable, affected hosts and running work. An update or refresh may change installed files and settings.

If output says `Ouroboros partially updated`, the package may already have changed even though a later refresh failed. Check `ouroboros --version` in the same environment, preserve the error, and identify the failed step. Review the suggested recovery command and its target before running it. In the cited revision, rerunning `ouroboros update` when the package is already current does not automatically retry failed runtime refreshes. Do not delete or overwrite existing configuration merely to silence an error. See the [update implementation](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/cli/commands/update.py).

## Verify and report

After a targeted, authorized change, repeat the original check in the same machine, host and installation. For a task result, compare the Seed's goal and acceptance criteria with the actual changed files, test and evaluation output, and unresolved failures. A job ID, health `ok`, terminal job state, successful query, or evaluation request alone does not prove the requested result was achieved.

Report the redacted symptom, identified installation and version, relevant ID types, commands actually run and their results, changes, and remaining uncertainty. State explicitly if real host connection, authentication, model use, external effects, or recovery were not tested. Do not claim that an action succeeded solely because it was suggested.
