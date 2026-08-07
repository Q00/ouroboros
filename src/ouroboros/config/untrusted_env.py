"""Untrusted-source environment policy — the ``.env`` trust boundary.

Extracted from :mod:`ouroboros.config.loader` so this policy is one auditable
unit rather than a block buried in a 2.4k-line module. It answers exactly one
question: may a key coming from an untrusted source — the project-directory
``.env`` that travels with whatever repository the user cloned — be written to
``os.environ``?

The loader owns *when* the policy applies; this module owns *what* it covers.
"""

from __future__ import annotations

# Environment variables that determine HOW Ouroboros executes work. This
# is the single authoritative trust boundary: a cloned repository's `.env`
# must not be able to change which binary runs or whether the user's
# approval gate applies. Four classes, all remote-code-execution sinks
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
        # Bare provider aliases (no OUROBOROS_ prefix) that adapters also
        # honor and then execute. Any new such alias MUST be added here:
        # `opencode_config._configured_opencode_cli_path` reads
        # OPENCODE_CLI_PATH and runs it via subprocess.run.
        "OPENCODE_CLI_PATH",
        # The `ooo` frontdoor bridges read this *suffix-less* alias — it is not
        # a _CLI_PATH variant, which is exactly why it was missed. Both the gjc
        # extension (`gjc_bridge/index.ts`) and the Pi extension emitted by
        # `ouroboros setup --runtime pi` do:
        #     if (process.env.OUROBOROS_CLI) return { command: ..., args: [] }
        # and then execFile/pi.exec that command. The bridges run inside the
        # spawned vendor CLI, which inherits this process' environment, so an
        # untrusted repo .env would choose the binary that every `ooo ...`
        # input executes. Ouroboros never sets this key itself; it exists only
        # as an operator escape hatch, so denying it from an untrusted source
        # costs nothing. Any new bridge/adapter key that names an executable
        # belongs here regardless of whether it ends in _CLI_PATH.
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
        # Runtime/backend selectors — choose which adapter is spawned.
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        # Backend profile selector (get_runtime_profile): chooses the
        # orchestrator backend profile and therefore which backend behavior /
        # executable is used — same routing class as the selectors above.
        "OUROBOROS_RUNTIME_PROFILE",
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
    }
)
UNTRUSTED_ENV_DENIED_PREFIXES = ("DYLD_", "LD_")


def is_untrusted_env_denied_key(key: str) -> bool:
    """Return whether an untrusted .env key may alter execution routing."""
    normalized = key.upper()
    return normalized in UNTRUSTED_ENV_DENYLIST or normalized.startswith(
        UNTRUSTED_ENV_DENIED_PREFIXES
    )
