"""Untrusted-source environment policy — the ``.env`` trust boundary.

Extracted from :mod:`ouroboros.config.loader` so this policy is one auditable
unit rather than a block buried in a 2.4k-line module. It answers exactly one
question: may a key coming from an untrusted source — the project-directory
``.env`` that travels with whatever repository the user cloned — be written to
``os.environ``?

The loader owns *when* the policy applies; this module owns *what* it covers.
"""

from __future__ import annotations

# Environment variables that determine HOW Ouroboros executes work or install
# its own persistent package. This
# is the single authoritative trust boundary: a cloned repository's `.env`
# must not be able to change which binary runs or whether the user's
# approval gate applies. Five classes, all remote-code-execution sinks
# when sourced from an untrusted location:
#   1. Explicit CLI path overrides fed straight into a subprocess.
#   2. Runtime/backend selectors that pick which adapter (and therefore
#      which executable) is spawned — a selector can route to a backend
#      whose CLI then resolves via a weak shutil.which / bare-name lookup.
#   3. Permission-mode overrides — setting acceptEdits/bypassPermissions
#      silently removes the human approval gate, letting a malicious repo
#      auto-approve arbitrary tool calls (effectively RCE).
#   4. Runtime-native preload hooks such as NODE_OPTIONS, which can execute
#      attacker-controlled code before a spawned JavaScript CLI starts.
#   5. Python package-manager controls. `ouroboros update` deliberately
#      inherits trusted process/home configuration so corporate indexes keep
#      working, but a cloned repository must not redirect uv/pipx resolution to
#      its own index or config file through the project `.env`.
# These keys are only honored from trusted sources (the real process
# environment, ~/.ouroboros/.env, ~/.ouroboros/config.yaml), never from
# the project-directory .env that travels with a cloned repo. Trusted .env
# files still follow the loader's normal "do not override an already-set
# real process environment value" precedence. Enforcing this here — at the
# .env load — keeps the policy in one place rather than split across
# downstream sinks.
UNTRUSTED_ENV_DENYLIST = frozenset(
    {
        # Search PATH used by shutil.which()/bare executable spawning.
        "PATH",
        # Node/Electron preload hook. A project .env could otherwise inject a
        # --require/--import payload before a spawned JavaScript CLI starts.
        "NODE_OPTIONS",
        # Non-LD_/DYLD_ dynamic-loader controls used by supported or adjacent
        # Unix platforms. Prefix families are rejected below.
        "LDR_PRELOAD",
        "LIBPATH",
        "SHLIB_PATH",
        # Explicit executable-path overrides.
        "OUROBOROS_CLI_PATH",
        "OUROBOROS_CODEX_CLI_PATH",
        "OUROBOROS_COPILOT_CLI_PATH",
        "OUROBOROS_KIRO_CLI_PATH",
        "OUROBOROS_OPENCODE_CLI_PATH",
        "OUROBOROS_HERMES_CLI_PATH",
        "OUROBOROS_GOOSE_CLI_PATH",
        "OUROBOROS_GEMINI_CLI_PATH",
        "OUROBOROS_PI_CLI_PATH",
        "OUROBOROS_GJC_CLI_PATH",
        "OUROBOROS_ANTIGRAVITY_CLI_PATH",
        "OUROBOROS_GROK_CLI_PATH",
        "OUROBOROS_OUROCODE_CLI_PATH",
        "OUROBOROS_ZCODE_CLI_PATH",
        "OUROBOROS_DSH_CLI_PATH",
        # POSIX shell the orchestrator runs every AC verify_command through.
        # A repo .env pointing this at its own binary would execute arbitrary
        # code inside the verification gate — the one place that must stay
        # untamperable.
        "OUROBOROS_VERIFY_BASH",
        # Not an executable path, but it selects the Cordis composition the
        # spawned Node process loads — plugin rows in that file execute
        # arbitrary code inside `dsh-acp-demo`, so an untrusted repo .env must
        # not be able to choose it.
        "OUROBOROS_DSH_CONFIG_PATH",
        # Bare provider aliases (no OUROBOROS_ prefix) that adapters also
        # honor and then execute. Any new such alias MUST be added here:
        # `opencode_config._configured_opencode_cli_path` reads
        # OPENCODE_CLI_PATH and runs it via subprocess.run.
        "OPENCODE_CLI_PATH",
        # The Pi frontdoor bridge reads this suffix-less alias — it is not a
        # _CLI_PATH variant, which is exactly why it is listed separately. The
        # bridge executes the selected command inside the spawned vendor CLI,
        # so an untrusted repo .env must not choose it. Ouroboros never sets
        # this key itself; it exists only as an operator escape hatch.
        "OUROBOROS_CLI",
        # Spawned-CLI discovery roots. The gjc CLI resolves its agent dir
        # (rules/skills/extensions it loads into every session) from these
        # vars; an untrusted repo .env must not be able to point a spawned
        # gjc at attacker-controlled instruction/extension directories.
        "GJC_CODING_AGENT_DIR",
        "GJC_CONFIG_DIR",
        "PI_CONFIG_DIR",
        # Copilot custom-instruction roots — same instruction-injection class
        # as GJC_CODING_AGENT_DIR. `copilot/cli_policy.py` derives the child
        # env from os.environ and only *appends* the setup-owned dir, so an
        # untrusted .env entry survives and a spawned Copilot loads attacker
        # AGENTS.md from it.
        "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
        # Ouroboros agent-definition root. `agents/loader.py` resolves every
        # agent's role/persona markdown (socratic-interviewer, evaluator, …)
        # from this dir first; an untrusted .env pointing it at a committed
        # repo dir lets a cloned repo replace the system prompt of every
        # spawned sub-agent — instruction injection, same class as above.
        "OUROBOROS_AGENTS_DIR",
        # Backend config-home roots. The spawned vendor CLI resolves its own
        # config file — which can name MCP servers to launch, disable the
        # approval gate, and widen the sandbox — from these vars, and the var
        # passes through the child env untouched (it is not in any backend's
        # strip_keys). An untrusted repo .env must not redirect a nested agent
        # at attacker-controlled config. Codex honors $CODEX_HOME/config.toml
        # (mcp_servers.<name>.command/args -> arbitrary command execution;
        # approval_policy="never" + sandbox_mode="danger-full-access" ->
        # silent removal of the human approval gate). OpenCode resolves its
        # config from OPENCODE_CONFIG / OPENCODE_CONFIG_DIR and otherwise
        # falls back to $XDG_CONFIG_HOME/opencode. Completes CVE-2026-47211.
        "CODEX_HOME",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        # Platform home selectors also choose Ouroboros' trusted config root.
        # A project .env must not turn a repository directory into ~/.ouroboros.
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        # Ouroboros' own MCP-bridge / plugin execution roster roots. Each
        # selects a file whose contents name an external command that the
        # bridge or plugin dispatcher then spawns verbatim — direct RCE, the
        # same threat model as the backend config-home roots above:
        #   - OUROBOROS_MCP_CONFIG -> mcp/bridge/config.py:discover_config
        #     returns the path; the YAML's server `command`/`args` are spawned
        #     via stdio_client (loader -> discover_config -> MCPClientAdapter
        #     -> stdio_client).
        #   - OUROBOROS_PLUGIN_LOCKFILE / OUROBOROS_PLUGIN_TRUST_ROOT ->
        #     plugin_dispatch resolves the installed-plugin roster and trust
        #     root from these; redirecting them lets a cloned repo register an
        #     attacker manifest / mark a malicious plugin as trusted, so
        #     `ooo <name>` dispatches into attacker code.
        "OUROBOROS_MCP_CONFIG",
        "OUROBOROS_PLUGIN_LOCKFILE",
        "OUROBOROS_PLUGIN_TRUST_ROOT",
        # SSRF guard toggle. `mcp/types.py` blocks loopback/private/link-local
        # MCP transport targets unless this is "1"; an untrusted .env must not
        # be able to re-enable connections to internal addresses.
        "OUROBOROS_ALLOW_LOCAL_TRANSPORT",
        # Telemetry is an operator-owned privacy boundary. A project `.env`
        # must not be able to re-enable collection, replace the public ingest
        # key, or redirect the stable anonymous identifier to an arbitrary
        # endpoint. These remain available from the real process environment
        # and the trusted ~/.ouroboros/.env file. DO_NOT_TRACK belongs to the
        # same boundary from the opposite direction: the loader applies the
        # untrusted project `.env` before the trusted `~/.ouroboros/.env` and
        # never overrides an already-set value, so an unset DO_NOT_TRACK here
        # would let a cloned repo's `.env` (e.g. `DO_NOT_TRACK=0`) win the
        # race and silently suppress a user's persisted opt-out before the
        # trusted file ever gets a chance to set it. CI/GITHUB_ACTIONS are
        # the same boundary again: telemetry.py stamps `ci=true` on events
        # from these two vars, and the published counting rule excludes
        # `ci=true` from the weekly-active metric -- an untrusted project
        # `.env` shipping `CI=1` could silently deregister genuine local
        # workflow_outcome events from that metric. Real CI runners set
        # these in the actual process environment, which the loader never
        # overrides regardless of this denylist, so denying project-`.env`
        # input here does not affect real CI detection -- only a cloned
        # repository's own `.env` file loses the ability to set them. The
        # trusted `~/.ouroboros/.env` is unaffected either way: this
        # denylist only gates the untrusted project file.
        "OUROBOROS_TELEMETRY",
        "OUROBOROS_POSTHOG_API_KEY",
        "OUROBOROS_POSTHOG_HOST",
        # Onboarding attribution is an analytics boundary. A cloned repo must
        # not rewrite the surface label used to compare activation cohorts.
        "OUROBOROS_FIRST_COMMAND_SURFACE",
        "DO_NOT_TRACK",
        "CI",
        "GITHUB_ACTIONS",
        # Runtime/backend selectors — choose which adapter is spawned.
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        # Backend profile selector (get_runtime_profile): chooses the
        # orchestrator backend profile and therefore which backend behavior /
        # executable is used — same routing class as the selectors above.
        "OUROBOROS_RUNTIME_PROFILE",
        # Shared-workspace provider switching is execution authority, not a
        # repository preference. Only the real process/trusted home may opt in.
        "OUROBOROS_CROSS_HARNESS_REDISPATCH",
        # Permission-mode overrides — must not silently disable the
        # user's approval gate from an untrusted repo.
        "OUROBOROS_AGENT_PERMISSION_MODE",
        "OUROBOROS_LLM_PERMISSION_MODE",
        "OUROBOROS_OPENCODE_PERMISSION_MODE",
        # Tool-capability override file. The override YAML can lower a tool's
        # approval_class (e.g. ELEVATED -> DEFAULT), weakening the human
        # approval gate for non-built-in tools. External control of this path
        # is therefore an approval-gate-bypass sink — same class as the
        # permission-mode overrides above.
        "OUROBOROS_TOOL_CAPABILITIES",
        # Execution-cost/behavior dial — an untrusted repo .env must not be able
        # to force a higher (or invalid) reasoning-effort level for every AC,
        # which changes runtime cost and behavior. Follows the same trusted-source
        # policy as the runtime/permission overrides above (RFC #1405).
        "OUROBOROS_AGENT_REASONING_EFFORT",
        # Explicit constructor model pin. Besides changing cost/capability, this
        # disables tier routing by design, so an untrusted project .env must not
        # be able to force an arbitrary model or bypass the frugality policy.
        "OUROBOROS_EXECUTION_MODEL",
        # Model-tier experiment controls are the same trust class: a cloned repo
        # must not disable routing or opt the user into shadow replay, which
        # re-executes successful children and can double token spend.
        "OUROBOROS_MODEL_TIER_ROUTING",
        "OUROBOROS_SHADOW_REPLAY",
        # Shell startup files, read before the first command of *any* shell
        # this process spawns — including the verify gate's `bash -c`. A repo
        # `.env` pointing `BASH_ENV` at a file containing `exit 0` turns
        # `bash -c 'exit 23'` into a pass, which is arbitrary code execution
        # inside the one place that must stay untamperable. `ENV` is the POSIX
        # spelling of the same hook.
        "BASH_ENV",
        "ENV",
        # Shell option state carried into that child: `xtrace` writes into the
        # output an assertion is checked against, `errexit` changes which leg
        # of a chain decides the status, `xpg_echo` changes what `echo` prints.
        "SHELLOPTS",
        "BASHOPTS",
        "BASH_XTRACEFD",
        "BASH_COMPAT",
    }
)
UNTRUSTED_ENV_DENIED_PREFIXES = (
    "DYLD_",
    "LD_",
    # Exported shell functions. A `-c` command resolves a function before any
    # executable of the same name, so `BASH_FUNC_pytest%%` replaces the tool a
    # contract meant to run — the shellshock-era shape of the same hole.
    "BASH_FUNC_",
    # Package-manager source/configuration families. Prefixes are intentional:
    # uv supports dynamically named index credentials (UV_INDEX_<NAME>_*), and
    # both uv and pip/pipx may add new controls. Trusted real-process and home
    # `.env` values remain allowed because this policy applies only to the
    # project-directory `.env`.
    "PIP_",
    "PIPX_",
    "UV_",
)


def is_untrusted_env_denied_key(key: str) -> bool:
    """Return whether an untrusted .env key may alter execution routing."""
    normalized = key.upper()
    return normalized in UNTRUSTED_ENV_DENYLIST or normalized.startswith(
        UNTRUSTED_ENV_DENIED_PREFIXES
    )
