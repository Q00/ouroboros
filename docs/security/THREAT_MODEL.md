# Threat Model: Ouroboros

## 1. System context

Ouroboros (`ouroboros-ai` on PyPI, `src/ouroboros/`, ~634 Python modules plus
TypeScript bridges) is a specification-first AI workflow engine. It runs as a
CLI (`ouroboros` / `ooo`), as an MCP server (`ouroboros mcp serve`, stdio by
default, optionally SSE / streamable-http), as a Textual TUI, and as a
localhost web dashboard daemon. Its job is to interview a user, crystallize a
Seed (goal + acceptance criteria), then orchestrate one or more **vendor
coding-agent CLIs** (Claude Code, Codex, Copilot, Gemini, OpenCode, Hermes,
Kiro, Goose, Pi, OMP, gjc, ourocode, zcode, dsh, Grok) as child processes that
edit a user's repository, and finally judge each acceptance criterion by
running a `verify_command` through a resolved Bash inside that repository.

It is installed per developer machine (`uv tool` / `pipx` / `uvx --from`),
runs with the developer's full privileges and credentials, and is typically
launched by an agent host (Claude Code, Codex, an IDE) **with the current
working directory set to whatever repository the developer has open** —
frequently one they just cloned. Every process boundary Ouroboros crosses is
therefore a boundary between "the operator's machine" and "a repository the
operator did not write".

## 2. Assets

| asset | description | sensitivity |
|---|---|---|
| operator code execution | The developer's user account: shell, filesystem, credentials, SSH agent. Any RCE in Ouroboros is RCE as the developer. | critical |
| provider credentials | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, OpenRouter, vendor CLI auth stores (`CODEX_HOME`, `~/.claude`), PostHog key. Read from env / `~/.ouroboros/.env` / login-shell import. | critical |
| approval gate | The human-in-the-loop permission mode of every spawned vendor CLI (`bypassPermissions`, `--trust-all-tools`, codex `approval_policy`, sandbox mode). Removing it silently converts an LLM worker into unattended arbitrary execution. | critical |
| AC verify verdict | The exit status + output of `verify_command` that decides whether an acceptance criterion PASSED. Everything downstream (evaluate, evolve, telemetry, the paper's benchmark) trusts it. | high |
| trusted config root | `~/.ouroboros/` (`config.yaml`, `.env`, `mcp_servers.yaml`, `backend_limits.yaml`, plugin lockfile / trust root, agent definitions, event DB, worktrees). Selecting a different root = choosing what Ouroboros trusts. | critical |
| workspace integrity | The user's repository / worktree that workers edit and the gate verifies in. Bounded by design (workers may edit anything inside it). | medium |
| event store / telemetry privacy | Local SQLite event DB with prompts and previews; optional PostHog telemetry with opt-out. A cloned repo must not flip privacy toggles. | medium |
| internal network reachability | The MCP client can dial HTTP/SSE transports; the dashboard binds localhost. SSRF = reach cloud metadata / loopback services from a config value. | medium |
| service availability | Startup of `ouroboros` / `mcp serve` from any cwd; one hostile `.env` must not make the tool unstartable. | low |

## 3. Entry points & trust boundaries

| entry_point | description | trust_boundary | reachable_assets |
|---|---|---|---|
| project .env | `config/loader.py::_load_env_file(Path(".env"), trusted=False)` runs **at import** and writes every key not rejected by `untrusted_env.is_untrusted_env_denied_key` into `os.environ`. cwd is the cloned repo. | untrusted repo file → process environment of Ouroboros and (by inheritance) every child it spawns | operator code execution, approval gate, trusted config root, AC verify verdict, event store / telemetry privacy, provider credentials |
| child-process env inheritance | Every `subprocess.*` / `asyncio.create_subprocess_*` / `os.execv*` site is enumerated by `tests/unit/security/test_trust_boundary_invariants.py`; roughly a third pass an env from a named builder (`runtime/child_env.build_child_env`, a per-backend `_build_child_env`, `verify_shell.sanitized_verify_environment`, git-specific envs), the rest inherit `os.environ` and are individually allowlisted there. None strips loader-class keys except the verify gate. | process environment → spawned interpreter / vendor CLI / shell / git | operator code execution, approval gate, AC verify verdict |
| bare-name executable resolution | `shutil.which("codex"/"opencode"/"uvx"/"ouroboros-tui"…)`, `["git", …]`, `os.execvpe(uvx, …)`; `codex/cli_policy.find_real_cli` walks `PATH` manually; Windows `PATHEXT`. | `PATH` (denied from `.env`) and non-denied aliases → argv[0] | operator code execution |
| config-file roots that name commands | `OUROBOROS_MCP_CONFIG` → `mcp/bridge/config.py::discover_config` → YAML `command/args/env` spawned by `stdio_client` (`${VAR}` substituted from `os.environ`); `OUROBOROS_PLUGIN_LOCKFILE` / `_TRUST_ROOT` → `plugin_dispatch`; `OUROBOROS_BACKEND_LIMITS`; `OUROBOROS_TOOL_CAPABILITIES`; `OUROBOROS_AGENTS_DIR`; `OUROBOROS_DSH_CONFIG_PATH`; vendor roots `CODEX_HOME`, `OPENCODE_CONFIG(_DIR)`, `XDG_CONFIG_HOME`, `GJC_*`, `PI_*`, `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, `HOME`. | env key → file path → argv / instruction text / approval policy | operator code execution, approval gate, trusted config root |
| AC verify gate | `orchestrator/verify_shell.py` resolves an absolute Bash (`OUROBOROS_VERIFY_BASH` → config → well-known → PATH), proves `-c` semantics with a probe, runs `bash -c '<verify_command>'` with `sanitized_verify_environment()` (strips `PYTHON*`, `PYTEST_*`, `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `SHELLOPTS`…, `LD_PRELOAD`/`DYLD_INSERT_LIBRARIES`/`BASH_FUNC_*`). Callers: `verify_shell:232`, `leaf_dispatcher:966`, `parallel_executor:9780` (+ `verify_command_runner._run_process`). | Seed/AC text (untrusted, by design executes in-workspace) and process env → verdict | AC verify verdict, operator code execution |
| MCP tool surface | `ouroboros_*` tools over stdio (trusted host) or network (`sse`/`streamable-http`, Host/Origin allowlists, no authentication). Arguments (seed content, session_context, cwd, verify commands) are untrusted. | MCP client → orchestrator with operator privileges | workspace integrity, AC verify verdict, operator code execution (via workers) |
| MCP transport URLs | `mcp/types.py::_validate_transport_url` guards SSE/HTTP/streamable-http URLs from `mcp_servers.yaml`; `OUROBOROS_ALLOW_LOCAL_TRANSPORT=1` disables the guard. | config value → outbound connection | internal network reachability |
| login-shell env import | `cli/commands/mcp.py::_ensure_shell_env` runs `$SHELL -l -c 'python3 … json.dump(os.environ)'` when launched by an agent host with a minimal env, merging a whitelisted subset + `PATH` into `os.environ` (cached 0600). | `SHELL` (not denied) → argv[0] of a spawned login shell | operator code execution, provider credentials |
| Python worker interpreters | `sys.executable -m ouroboros.mcp.detached_worker` (`mcp/detached_jobs.py:296`), `-m ouroboros.dashboard_web --serve-daemon` (`dashboard_web/daemon.py:175`), `-m ouroboros.providers.litellm_proof_worker` (`litellm_adapter.py:984`, `-I`). | inherited env → interpreter startup (`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `.pth`) | operator code execution |
| vendor CLI config homes | Spawned CLIs read their own config: `$CODEX_HOME/config.toml` (`mcp_servers.*.command`, `approval_policy`, `sandbox_mode`), OpenCode config, gjc/pi agent dirs, Copilot instruction dirs, `~/.claude`. | Ouroboros env → nested agent's trust decisions | approval gate, operator code execution |
| telemetry / privacy toggles | `OUROBOROS_TELEMETRY`, `OUROBOROS_POSTHOG_*`, `DO_NOT_TRACK`, `CI`, `GITHUB_ACTIONS`, `OUROBOROS_FIRST_COMMAND_SURFACE` (all denied); `OUROBOROS_IO_JOURNAL_PREVIEWS` (`events/io.py`, not denied). | env → what is recorded / sent | event store / telemetry privacy |
| supply chain / self-update | `ooo update` runs `uv tool` / `pipx` with `UV_TOOL_DIR` / `PIPX_HOME` overrides; `uvx --isolated --from ouroboros-ai[...]` relaunches; Homebrew formula; `.mcp.json` unpinned `uvx`. | package index / installer env → installed code | operator code execution |
| localhost web dashboard | `dashboard_web` daemon, SSE over EventStore, binds `127.0.0.1` by default; `--host 0.0.0.0` allowed. | browser (same machine) → read-only event view | event store / telemetry privacy |

## 4. Threats

| id | threat | actor | surface | asset | impact | likelihood | status | controls | evidence |
|---|---|---|---|---|---|---|---|---|---|
| T1 | Remote code execution as the operator: a cloned repository's `.env` sets an environment key that a process Ouroboros spawns honours as an executable path, config/home root, dynamic or module loader control, shell startup hook, or package-manager configuration | remote_unauth (author of any repo the operator opens) | project .env; child-process env inheritance; Python worker interpreters; vendor CLI config homes; login-shell env import; config-file roots that name commands | operator code execution, approval gate, trusted config root | critical | almost_certain | partially_mitigated | `UNTRUSTED_ENV_DENYLIST` / `UNTRUSTED_ENV_DENIED_PREFIXES` applied at `.env` load; loader never overrides an already-set real-env value; `interpolate=False`; verify-gate strip list; `-I` on the litellm worker; `tests/unit/config/test_loader_env.py` + `tests/unit/security/` invariants | CVE-2026-47211 (GHSA-c4m7-2gwp-vw76), CVE-2026-66065 (GHSA-jv2h-4p9v-wf5w), GHSA-wvgf-hr9x-v3g6, GHSA-7j6g-gw2r-mw48 (draft, PYTHONPATH), commits 7c04e283d 5855e8d44 bca0c1a86 c2f8e0a7a |
| T2 | Silent removal of the human approval gate: an untrusted source (repo `.env`, redirected vendor config home, tool-capability override file) makes a spawned worker run with bypass permissions / `approval_policy=never` / lowered `approval_class` | remote_unauth | project .env; vendor CLI config homes; config-file roots that name commands | approval gate | critical | likely | partially_mitigated | `OUROBOROS_*_PERMISSION_MODE`, `OUROBOROS_TOOL_CAPABILITIES`, `CODEX_HOME`, `OPENCODE_CONFIG*`, `XDG_CONFIG_HOME`, `HOME` denied from `.env`; bypass is an explicit contract at the runner | CVE-2026-66065 (permission-mode class) |
| T3 | Verify-gate tampering: something outside the `verify_command` text turns a failing acceptance criterion into a PASS (shell startup file, exported function, `PYTEST_ADDOPTS`, import-path hijack, substituted shell, non-sanitized spawn path, worker-writable env) | remote_unauth via repo contents, or insider (LLM worker) | AC verify gate; child-process env inheritance | AC verify verdict | high | possible | partially_mitigated | absolute Bash + `-c` capability probe + sha256 identity; `sanitized_verify_environment` on all three gate spawn paths; `OUROBOROS_VERIFY_BASH` denied from `.env`; workspace-bound cwd | commits bca0c1a86, 46dcf2062, 7cf56b3e2 |
| T4 | Arbitrary command execution through a config file that names commands: a bridge YAML, plugin manifest/lockfile, or vendor config picked up from the repository or from a root an untrusted key selects | remote_unauth | config-file roots that name commands; project .env | operator code execution, trusted config root | critical | possible | partially_mitigated | project `.ouroboros/mcp_servers.yaml` deliberately not auto-discovered; roster/root env keys denied; `${VAR}` substitution errors on unset vars | CVE-2026-47211 (companion `OUROBOROS_MCP_CONFIG` / `CODEX_HOME` keys) |
| T5 | Executable substitution via bare-name lookup: `shutil.which` / manual `PATH` walk / `os.execvpe` resolves a vendor CLI, `git`, `uvx`, or the login shell to an attacker-placed binary | remote_unauth | bare-name executable resolution; login-shell env import; project .env | operator code execution | critical | possible | partially_mitigated | `PATH`, `SHELL` and `ZDOTDIR` denied from `.env`; explicit `*_CLI_PATH` overrides denied; codex wrapper detection; verify shell absolute-path rule | GHSA-wvgf-hr9x-v3g6 (bare `OUROBOROS_CLI` alias) |
| T6 | Server-side request forgery from MCP transport configuration: an SSE/HTTP URL reaches loopback, link-local (cloud metadata), or RFC1918 targets, or the guard is disabled from an untrusted source | remote_auth (whoever can edit `~/.ouroboros/mcp_servers.yaml` or set env) | MCP transport URLs; project .env | internal network reachability | medium | rare | partially_mitigated | `_validate_transport_url` (scheme, userinfo, loopback names, IP ranges, DNS resolution); `OUROBOROS_ALLOW_LOCAL_TRANSPORT` denied from `.env` | |
| T7 | Privacy / telemetry override from a cloned repository: collection re-enabled, ingest endpoint redirected, CI classification forged, or journal previews un-redacted | remote_unauth | telemetry / privacy toggles; project .env | event store / telemetry privacy | medium | possible | partially_mitigated | telemetry keys, `DO_NOT_TRACK`, `CI`, `GITHUB_ACTIONS` denied; installer parity tests | commits f52836fd4, f3cf009ee, 4c7a28c42 |
| T8 | Unauthenticated network MCP: `mcp serve --transport sse/streamable-http` exposes every orchestration tool (spawning workers with operator credentials) to anyone who can reach the socket | adjacent_network | MCP tool surface | operator code execution, provider credentials | critical | rare | partially_mitigated | stdio default; non-loopback binds refused without `OUROBOROS_MCP_AUTH_TOKEN` / `--auth-token` (`mcp/server/auth.py`); Host/Origin allowlists with DNS-rebinding protection; idle shutdown; loopback binds stay credential-free by policy | |
| T9 | Supply-chain compromise of the installed package or its launcher: unpinned `uvx --from ouroboros-ai`, index redirection through `PIP_*`/`UV_*`/`PIPX_*`, Homebrew formula automation | supply_chain | supply chain / self-update | operator code execution | critical | rare | partially_mitigated | `PIP_`/`PIPX_`/`UV_` prefixes denied from `.env`; AI-stack exact pins; release via protected `main` | |
| T10 | Denial of service at startup: a hostile `.env` (malformed bytes, illegal keys, huge file, or a forged internal control marker) makes every Ouroboros command unusable in that directory | remote_unauth | project .env | service availability | low | possible | partially_mitigated | broad exception isolation in `_load_env_file` covers parser failures (`test_import_survives_a_hostile_project_env`); a semantically valid `_OUROBOROS_NESTED=1` still makes `mcp serve` exit 0 before hydration | commits 720a7c91c, aab288446 |
| T11 | Untrusted MCP / Seed input executes in the workspace: a hostile Seed or session_context runs shell in the operator's repository | remote_auth (whoever drives the MCP client) | MCP tool surface; AC verify gate | workspace integrity | medium | likely | risk_accepted | workspace is the sandbox boundary by design; approval gate on workers; workspace-bound cwd | commits cd13da16f, 22ca95fb6 |

## 5. Deprioritized

| threat | reason |
|---|---|
| Memory-safety corruption | Pure Python + TypeScript; no FFI / `unsafe`. |
| XSS / CSRF in the web dashboard | Localhost, read-only SSE view of the operator's own event store; no auth state to steal. Re-evaluate if `--host 0.0.0.0` becomes a documented mode. |
| Prompt injection into LLM workers | Not a code vulnerability in Ouroboros (generic rule 6); the approval gate and workspace boundary are the controls. |
| Repudiation | Single-operator tool; the event store is the audit log and is local. |
| Volumetric DoS of the MCP server | Local / operator-driven; idle timeouts exist. |
| Seed / AC text executing shell inside the workspace | Intended design (T11 risk_accepted); the gate only has to be un-tamperable from outside the command text. |

## 6. Open questions

- Is `--transport sse` / `streamable-http` used by anyone outside a loopback-only dev setup? If so T8 needs an authentication story, not just allowlists.
- Should the per-backend `_build_child_env` implementations that are `os.environ.copy()` + pop (omp, pi, goose, dsh, ourocode) migrate to `runtime/child_env.build_child_env` so one helper is the single spawn chokepoint?
- Is the 0600 login-shell env cache (`_store_shell_env_cache`) acceptable at rest, given it holds provider keys?
- Does any supported vendor CLI honour additional loader-class keys not yet enumerated (`RUBYOPT`, `PERL5OPT`, `JAVA_TOOL_OPTIONS`, `DENO_*`, `BUN_*`)?
- Owner decision needed: after the sibling PR lands, should the loader move from a denylist to an allowlist for the project `.env` (only `*_API_KEY`-shaped and documented keys pass)? That is the only change that closes T1 as a class rather than per key.

## 7. Provenance

- mode: bootstrap
- date: 2026-09-15
- target: src/ouroboros @ e4defa1bb (branch feat/defending-code-harness, based on origin/main)
- inputs: git-log + GitHub security advisories (`gh api /repos/Q00/ouroboros/security-advisories`) + `tests/unit/config/test_loader_env.py` + SECURITY.md; swarm briefs run inline by the orchestrating agent (docs reader, surface mapper, asset finder, history miner, advisory fetcher)
- owner: unset

## 8. Recommended mitigations

| mitigation | threat_ids | closes_class | effort |
|---|---|---|---|
| CI-enforced invariants: every spawn site must use a named env builder or be explicitly allowlisted; a catalog of known loader / executable-selector keys must all be denied (`tests/unit/security/test_trust_boundary_invariants.py`) | T1,T2,T4,T5,T7 | partial | S |
| Switch the project `.env` from a denylist to an allowlist (documented credential-shaped keys only), keeping the denylist as defence in depth | T1,T2,T4,T5,T7 | yes | M |
| Start every `sys.executable -m ouroboros.*` worker with `-I` (or `-E -s`) so interpreter-startup keys are inert regardless of the env | T1 | partial | S |
| Route the five hand-rolled `_build_child_env` copies through `runtime/child_env.build_child_env` so one helper is the spawn chokepoint | T1 | partial | M |
| Add an authentication token to network MCP transports or refuse non-loopback binds without one | T8 | yes | M |
| Pin `uvx --from ouroboros-ai==<version>` in `.mcp.json` and generated launchers | T9 | partial | S |
