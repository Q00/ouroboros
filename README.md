<!-- mcp-name: io.github.Q00/ouroboros -->
<p align="right">
  <strong>English</strong> | <a href="./README.ko.md">한국어</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <br/>
  ◯ ─────────── ◯
  <br/><br/>
  <img src="./docs/images/ouroboros.png" width="420" alt="Ouroboros">
  <br/><br/>
  <strong>O U R O B O R O S</strong>
  <br/><br/>
  ◯ ─────────── ◯
  <br/>
</p>


<p align="center">
  <strong>It gets smarter on its own. We just hold the line.</strong>
  <br/>
  <sub>Skip the prompt engineering. The agent runs, fails, and gets smarter every generation. The grading command and expected result never make it into the success contract we hand it.</sub>
  <br/>
  <sub>The <strong>Agent OS</strong> for replayable AI coding workflows</sub>
</p>

<p align="center">
  <a href="https://github.com/Q00/ouroboros"><img src="https://img.shields.io/github/stars/Q00/ouroboros?color=yellow&logo=github&label=stars" alt="GitHub stars"></a>
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://github.com/sponsors/Q00"><img src="https://img.shields.io/github/sponsors/Q00?logo=githubsponsors&color=EA4AAA&label=sponsors" alt="GitHub Sponsors"></a>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/26008?utm_source=repository-badge&utm_medium=badge&utm_campaign=badge-repository-26008" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/repositories/26008" alt="Q00%2Fouroboros | Trendshift" width="250" height="55"/></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-ouroboros">Why</a> ·
  <a href="#what-you-get">Results</a> ·
  <a href="#the-loop">How It Works</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#from-wonder-to-ontology">Philosophy</a> ·
  <a href="https://ouroboros.page/learn/en/">Guide</a>
</p>

```bash
# macOS / Linux / WSL 2
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | OUROBOROS_INSTALL_REF=readme-hero bash
```

```powershell
# Windows (PowerShell) — no Python needed; installs Git and uv for you
irm https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.ps1 | iex
```

<p align="center"><sub>One command installs it. Then run <code>ooo setup</code> once inside your coding agent — details in <a href="#quick-start">Quick Start</a>.</sub></p>

<p align="center"><sub><b>Separate runs, separate hosts. Different tasks on purpose — the engine is what is shared, not the prompt</b></sub></p>

<table align="center">
<tr>
<td align="center" width="33%"><img src="./docs/images/ooo-interview.gif" width="300" alt="Terminal recording of the ouroboros CLI interview reporting an ambiguity score"><br><sub><b>Terminal CLI</b> — a task-management CLI: <code>ouroboros init start</code> asking about ordering and scope, then reporting an ambiguity score</sub></td>
<td align="center" width="33%"><img src="./docs/images/host-codex.gif" width="300" alt="Screen recording of the ChatGPT app calling Ouroboros as an integration"><br><sub><b>ChatGPT (Codex)</b> — called as an integration, on a video-publishing harness: the interview, its advisory lanes, and the ambiguity ledger</sub></td>
<td align="center" width="33%"><img src="./docs/images/host-claude.gif" width="300" alt="Screen recording of Claude Code running six Ouroboros interview advisory lanes in parallel"><br><sub><b>Claude Code</b> — a YouTube automation task, with the six advisory lanes running in parallel before the interview submits</sub></td>
</tr>
<tr>
<td align="center" width="33%"><img src="./docs/images/host-hermes.gif" width="300" alt="Screen recording of a Discord bot running the Ouroboros interview and reporting a final ambiguity of 0.15"><br><sub><b>Hermes (Discord)</b> — a kart-racing game, run as a chat bot, ending at <code>Final ambiguity: 0.15</code></sub></td>
<td align="center" width="33%"><img src="./docs/images/host-dsh.gif" width="300" alt="Screen recording of DeepSeek Harness calling the Ouroboros interview tool and submitting advisory fan-out results"><br><sub><b>DeepSeek Harness</b> — an OSS-trend outreach script, driven from a dsh chat: <code>mcp__ouroboros__ouroboros_interview</code> turn by turn, fan-out results submitted between rounds</sub></td>
<td align="center" width="33%"><img src="./docs/images/host-kiro.gif" width="300" alt="10x screen recording of Kiro CLI running an Ouroboros interview"><br><sub><b>Kiro</b> — the Kiro CLI running the Ouroboros interview flow, turning a vague request into a structured, testable Seed</sub></td>
</tr>
</table>

**Turn a vague idea into a verified, working codebase -- across Claude Code, Codex CLI, OpenCode, Hermes, Gemini, Kiro, Copilot, Pi, Zcode, Goose, GJC, Antigravity, and Grok.**

Ouroboros is an **Agent OS** for AI coding: a local-first runtime layer that
turns non-deterministic agent work into a replayable, observable, policy-bound
execution contract. It replaces ad-hoc prompting with a structured
specification-first workflow: interview, crystallize, execute, evaluate,
evolve.

---

## The Ouroboros Agent OS Stack

Like any OS, Ouroboros is split into a stable **OS layer** of primitives, an
**application layer** of domain workflows, and a **shell** that humans actually
sit in front of. Three repos, one stack:

| Layer | Repo | Role | What it gives you |
| :--- | :--- | :--- | :--- |
| **Shell** (terminal client) | [`Ouro-labs/ourocode`](https://github.com/Ouro-labs/ourocode) | Native terminal UI for running `ooo` workflows across Claude / Codex / Gemini CLIs in one session | TUI, wonderTool decision pickers, MCP pane state, command discovery |
| **Apps** (domain workflows) | [`Ouro-labs/ouroboros-plugins`](https://github.com/Ouro-labs/ouroboros-plugins) | UserLevel plugin contract — composes core primitives into installable domain programs (PR ops, Jira sync, incidents, releases) | Plugin manifest, scoped permissions, audit/provenance, reference plugins |
| **OS** (this repo) | [`Q00/ouroboros`](https://github.com/Q00/ouroboros) | Agent OS core — Seed, Ledger, Runtime, MCP, safety boundaries | `ooo` commands, spec-first workflow engine, multi-runtime adapter |

**How they connect:**

```
  ourocode  ──►  ooo / ouroboros-plugins  ──►  ouroboros core (Seed · Ledger · MCP · Runtime)
   shell             user-level apps                        kernel
```

- The **kernel** (`ouroboros`) owns the contract: every action becomes a
  Seed-bound, ledger-recorded, replayable event — regardless of which LLM
  executes it.
- **Plugins** (`ouroboros-plugins`) declare scoped capabilities against that
  contract, so domain workflows (review a PR, triage a Linear ticket, run a
  release) stay auditable and policy-bound instead of being one-off prompts.
- **Ourocode** is the terminal shell: it surfaces MCP state, interview
  questions, and wonderTool decisions as first-class TUI elements, so you can
  drive the OS without leaving the keyboard or switching between CLIs.

Use `ouroboros` alone with any supported CLI, layer plugins on for domain
workflows, or install `ourocode` when you want a unified terminal cockpit.

> **Disclaimer.** The Ouroboros project and community are **not affiliated with
> any cryptocurrency, token, memecoin, or trading community** — including, but
> not limited to, any "ouroboros" tickers on pump.fun or other launchpads. This
> is an open-source developer tool. We do not issue, endorse, or hold any
> coins. Any token claiming association with this project is unauthorized.

> **Naming note.** A separate, unaffiliated open-source project also uses the
> name "Ouroboros" — Anton Razzhigaev's self-modifying, autonomous-memory agent
> at `github.com/razzant/ouroboros`. No shared code, no relationship. This
> project locks a specification before executing rather than rewriting its own
> architecture; if you're looking for the latter, that's the other one.

---

## Why Ouroboros?

Most AI coding fails at the **input**, not the output. The bottleneck is not AI capability -- it is human clarity.

| Problem       | What Happens                     | Ouroboros Fix                                 |
| :------------ | :------------------------------- | :-------------------------------------------- |
| Vague prompts | AI guesses, you rework           | Socratic interview exposes hidden assumptions |
| No spec       | Architecture drifts mid-build    | Immutable seed spec locks intent before code  |
| Manual QA     | "Looks good" is not verification | 3-stage automated evaluation gate             |

---

## Quick Start

**Install** — one command, everything auto-detected:

```bash
# macOS / Linux / WSL 2
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | OUROBOROS_INSTALL_REF=readme bash
```

```powershell
# Windows (PowerShell 5.1+ or pwsh 7+) — nothing to install first
irm https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.ps1 | iex
```

The Windows installer installs Git and uv through winget when they are missing,
lets uv download its own Python, then installs `ouroboros-ai` and wires the
host it finds. Native Windows is experimental and Codex CLI needs WSL 2; see
[platform support](./docs/platform-support.md).

**First command** — open your AI coding agent and run these in order:

```
> ooo setup
> ooo interview "I want to build a task management CLI"
```

`ooo setup` is a one-time configuration step. `ooo interview` is the first
workflow command and starts the Socratic interview. After setup, Codex follows
its currently selected model and Claude Code starts with its recommended model
settings. Choose **Directly configure models** only when you want to pin a
stage to a specific model; it opens the local settings screen in your browser.
You can return to those settings any time with `ooo config`.

Or from a plain terminal, without an agent host:

```
$ ouroboros init start --orchestrator "I want to build a task management CLI tool"
```

<p align="center">
  <sub>That recording is this exact command. It is at the top of this page so you can see the tool before installing it.</sub>
</p>

<p align="center">
  <img src="./docs/images/ooo-setup-refresh.gif" width="760" alt="Terminal recording of ouroboros setup refresh installing Codex rules and skills, Hermes skills, the OpenCode plugin and instruction guide, and the Pi and GJC bridges, ending with the line Refreshed runtime artifacts: codex, hermes, opencode, pi, gjc">
</p>

<p align="center">
  <sub><code>ouroboros setup refresh</code> on one machine. It installs into the hosts that machine actually has, each in the shape that host expects: rules and skills for Codex, skills for Hermes, a plugin and an <code>AGENTS.md</code> for OpenCode, bridges for Pi and GJC. Your machine will show whichever of the thirteen you have installed.</sub>
</p>

> Works with Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode, Hermes, Gemini, Kiro CLI, Pi CLI, Zcode, Goose, GJC, Antigravity CLI, and Grok Build CLI. The installer detects available runtimes and registers the MCP server where the host supports it. For explicit selection, run `ouroboros setup --runtime <opencode|kiro|copilot|gemini|pi|zcode|goose|gjc|antigravity|grok>` after installation. Copilot live-discovers its subscription catalog via the GitHub Copilot models API; Kiro's settings picker queries the authenticated CLI with `kiro-cli chat --listmodels -f json`, so account and enterprise allow-list changes appear without a hardcoded model table.

> **DeepSeek support.** Ouroboros speaks DeepSeek two ways. Point the interview/Seed/QA pipeline at DeepSeek's own models with `--llm-backend dsh` (`ouroboros mcp serve --runtime claude-cli --llm-backend dsh`, or `OUROBOROS_LLM_BACKEND=dsh`) — this drives [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)'s ACP server under the hood. Or go the other way: install the [`dsh-ouroboros` plugin](./integrations/dsh-plugin) (`dsh plugin --profile <your-profile> add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"`) and type `ooo interview` / `ooo auto` directly in the DeepSeek Harness chat — the same `ouroboros_interview` / `ouroboros_auto` tools run natively inside it, Socratic questions and all. Both directions, including what the `dsh` backend needs beyond the one variable, are in the [DeepSeek Harness guide](./docs/guides/deepseek-harness.md).

<details>
<summary><strong>Codex plugin quick start</strong></summary>

Needs `codex` on your `PATH` and `uvx` on the host (the plugin's MCP descriptor
launches the server with it). Install uv with `pipx install uv`,
`pip install --user uv`, or `brew install uv`.

```bash
codex plugin marketplace add Q00/ouroboros
codex plugin add ouroboros@ouroboros
```

Start a new Codex session, then run these commands in order:

```
ooo setup
ooo interview "Build a task management CLI"
```

`ooo setup` is the one-time runtime preparation. Once ready, Ouroboros follows
Codex's current default model; choose **Directly configure models** only when
you want to pin a specific model for a pipeline stage.

</details>

<details>
<summary><strong>Kiro CLI quick start</strong></summary>

```bash
pipx install 'ouroboros-ai[mcp]'       # or: uv tool install 'ouroboros-ai[mcp]'
ouroboros setup --runtime kiro         # detects Kiro CLI, registers MCP server, and
                                        # writes OUROBOROS_RUNTIME=kiro into
                                        # ~/.kiro/settings/mcp.json (the trusted,
                                        # setup-managed location -- a project .env
                                        # is untrusted input and this key is ignored there)
```

Then use `ooo` commands inside a Kiro CLI session.

</details>

<details>
<summary><strong>GitHub Copilot CLI quick start</strong></summary>

```bash
gh auth login                                # one-time GitHub auth (used for live model discovery)
pipx install 'ouroboros-ai[mcp]'             # or: uv tool install 'ouroboros-ai[mcp]'
ouroboros setup --runtime copilot            # discovers models live, picks a default,
                                             # registers MCP server in ~/.copilot/mcp-config.json
```

Restart your Copilot CLI session, then use `ooo` commands inside it. Model-ID mapping is catalog-gated: the current direct and OpenRouter Opus defaults resolve to Copilot's published `claude-opus-5`, while legacy Anthropic versions convert only their trailing numeric separator and only when the discovered catalog contains the exact candidate. Unknown IDs remain unchanged so Copilot reports an explicit unavailable-model error instead of silently selecting a different model. Leave role models unset so setup writes a discovered ID, or set a Copilot-valid ID explicitly. See the [Copilot runtime guide](./docs/runtime-guides/copilot.md).

See the [GitHub Copilot CLI runtime guide](./docs/runtime-guides/copilot.md) for full details.

</details>

<details>
<summary><strong>Other install methods</strong></summary>

**Claude Code plugin only** (no Python package or global Python to install; the
host needs uv, which provides both `uvx` for the MCP server and the skills'
Python >= 3.12 fallback):
```bash
claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros
```
Then run `ooo setup` inside a Claude Code session.

**pip / uv / pipx**:
```bash
pip install 'ouroboros-ai[mcp,tui]' && ouroboros setup --runtime claude-cli  # recommended MCP v2 default
pip install 'ouroboros-ai[claude]'      # Claude Agent SDK profile (MCP 1.x, isolated)
pip install 'ouroboros-ai[claude-cli]'  # dependency-free Claude CLI worker
pip install 'ouroboros-ai[claude-sdk]'  # explicit alias for the Claude SDK profile
pip install 'ouroboros-ai[litellm]'     # + LiteLLM multi-provider; Python 3.12-3.13
pip install 'ouroboros-ai[mcp]'         # MCP v2 server/client without the GUI
pip install 'ouroboros-ai[tui]'         # settings GUI only
pip install 'ouroboros-ai[all]'         # MCP 1.x app bundle; excludes MCP 2 by design
ouroboros setup                         # configure runtime
```

Core and non-LiteLLM installs support Python 3.12-3.14. LiteLLM-bearing installs (`[litellm]`, `[all]`, and source `--extra all`) support Python 3.12-3.13; use Python 3.13 for current examples. See [Platform Support](./docs/platform-support.md#python-profile-matrix).

The recommended standalone installation is `ouroboros-ai[mcp,tui]` followed by
an explicit MCP v2-compatible runtime selection. The example uses
`--runtime claude-cli`; substitute another compatible runtime such as `codex`,
`opencode`, `hermes`, `gemini`, `goose`, `kiro`, `copilot`, `pi`, or `gjc`.
Use `[claude]` and `[claude-sdk]` only in isolated MCP 1.x environments.

`pip install 'ouroboros-ai[mcp]'` is valid for embedding the MCP client/server library in an already isolated Python environment, but host registration requires `uvx --isolated --python '>=3.12'` or `pipx`. Use `pipx install 'ouroboros-ai[mcp]'` or `uv tool install 'ouroboros-ai[mcp]'` before `ouroboros setup --runtime <claude-cli|codex|opencode|hermes|gemini|goose|kiro|copilot|pi|gjc>`; setup exits without changing runtime configuration when neither isolated launcher is available.

Legacy compatibility: `ouroboros-ai[dashboard]` is still accepted as a compatibility alias/no-op; it does not install dashboard runtime payload. `ouroboros-ai[all]` includes that no-op alias only for compatibility.

**Homebrew (macOS/Linux)**:
```bash
brew tap q00/tap
brew install ouroboros-ai
ouroboros setup                         # configure runtime
```
Self-hosted tap, not yet in homebrew-core. Installs the same package published to PyPI.

See runtime guides: [Claude Code](./docs/runtime-guides/claude-code.md) · [Codex CLI](./docs/runtime-guides/codex.md) · [Hermes](./docs/runtime-guides/hermes.md) · [OpenCode](./docs/runtime-guides/opencode.md) · [Kiro CLI](./docs/runtime-guides/kiro.md) · [Gemini CLI](./docs/runtime-guides/gemini.md) · [GitHub Copilot CLI](./docs/runtime-guides/copilot.md) · [Zcode](./docs/runtime-guides/zcode.md) · [Pi JSON mode](https://pi.dev/docs/latest/json) · [Goose](./docs/runtime-guides/goose.md) · [GJC](./docs/runtime-guides/gjc.md) · [Antigravity CLI](./docs/runtime-guides/antigravity.md) · [Grok Build CLI](./docs/runtime-guides/grok.md)

</details>

<details>
<summary><strong>Uninstall</strong></summary>

```bash
ouroboros uninstall
```

Removes all configuration, MCP registration, and data. See [UNINSTALL.md](./UNINSTALL.md) for details.

</details>

> **Python >= 3.12 required.** LiteLLM-bearing profiles support Python 3.12-3.13. See [Platform Support](./docs/platform-support.md#python-profile-matrix) and [pyproject.toml](./pyproject.toml).
>
> **Installing as an MCP server: use 0.51.1 or later.** Earlier versions can fail at startup with `Failed to reconnect to plugin:ouroboros:ouroboros: -32000` when an existing environment shadows the `[mcp]` profile ([#2012](https://github.com/Q00/ouroboros/issues/2012)). This matters if you install through a downstream package rather than PyPI, since those can lag.

<p align="center">
  <sub>Most people find out they were unclear about three files into the review.<br/>
  If that feels familiar, star <a href="https://github.com/Q00/ouroboros"><strong>Q00/ouroboros on GitHub</strong></a> so the next person it could save can find it.</sub>
</p>

---

## What You Get

After one loop of the Ouroboros cycle, a vague idea becomes a verified codebase:

| Step          | Before                  | After                                                                   |
| :------------ | :---------------------- | :---------------------------------------------------------------------- |
| **Interview** | *"Build me a task CLI"* | 12 hidden assumptions exposed, ambiguity scored to 0.19                 |
| **Seed**      | No spec                 | Immutable specification with acceptance criteria, ontology, constraints |
| **Evaluate**  | Manual review           | 3-stage gate: Mechanical (free) -> Semantic -> Multi-Model Consensus    |

<details>
<summary><strong>What just happened?</strong></summary>

```
interview  ->  Socratic questioning exposed 12 hidden assumptions
seed       ->  Crystallized answers into an immutable spec (Ambiguity: 0.15)
run        ->  Executed via Double Diamond decomposition
evaluate   ->  3-stage verification: Mechanical -> Semantic -> Consensus
```

> Use `ooo <cmd>` inside your AI coding agent session, or `ouroboros init start`, `ouroboros run seed.yaml`, etc. from the terminal.

The serpent completed one loop. Each loop, it knows more than the last.

</details>

---

## How It Compares

AI coding tools are powerful -- but they solve the **wrong problem** when the input is unclear.

|                     | Vanilla AI Coding                        | Ouroboros                                                                       |
| :------------------ | :--------------------------------------- | :------------------------------------------------------------------------------ |
| **Vague prompt**    | AI guesses intent, builds on assumptions | Socratic interview forces clarity *before* code                                 |
| **Spec validation** | No spec -- architecture drifts mid-build | Immutable seed spec locks intent; ambiguity gate (<= 0.2) blocks premature code without explicit `force` |
| **Evaluation**      | "Looks good" / manual QA                 | 3-stage automated gate: Mechanical -> Semantic -> Multi-Model Consensus         |
| **Rework rate**     | High -- wrong assumptions surface late   | Low -- assumptions surface in the interview, not in the PR review               |

---

## The Loop

The ouroboros -- a serpent devouring its own tail -- is not decoration. It IS the architecture:

```
    Interview -> Seed -> Execute -> Evaluate
        ^                           |
        +---- Evolutionary Loop ----+
```

Each cycle does not repeat -- it **evolves**. The output of evaluation feeds back as input for the next generation, until the system truly knows what it is building.

| Phase         | What Happens                                                          |
| :------------ | :-------------------------------------------------------------------- |
| **Interview** | Socratic questioning exposes hidden assumptions                       |
| **Seed**      | Answers crystallize into an immutable specification                   |
| **Execute**   | Double Diamond: Discover -> Define -> Design -> Deliver               |
| **Evaluate**  | 3-stage gate: Mechanical ($0) -> Semantic -> Multi-Model Consensus    |
| **Evolve**    | Wonder *("What do we still not know?")* -> Reflect -> next generation |

> *"This is where the Ouroboros eats its tail: the output of evaluation*
> *becomes the input for the next generation's seed specification."*
> -- `reflect.py`

Convergence is reached when ontology similarity >= 0.95 -- when the system has questioned itself into clarity.

### Ralph: The Loop That Never Stops

`ooo ralph` runs the evolutionary loop persistently -- across session boundaries -- until convergence is reached. Each step is **stateless**: the EventStore reconstructs the full lineage, so even if your machine restarts, the serpent picks up where it left off.

```
Ralph Cycle 1: evolve_step(lineage, seed) -> Gen 1 -> action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       -> Gen 2 -> action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       -> Gen 3 -> action=CONVERGED
                                                +-- Ralph stops.
                                                    The ontology has stabilized.
```

---

## Commands

Inside AI coding agent sessions, use `ooo <cmd>` skills. From the terminal, use the `ouroboros` CLI.

| Skill (`ooo`)    | CLI equivalent                                                    | What It Does                                                 |
| :--------------- | :---------------------------------------------------------------- | :----------------------------------------------------------- |
| `ooo setup`      | `ouroboros setup`                                                 | Register runtime and configure project (one-time)            |
| `ooo interview`  | `ouroboros init start`                                            | Socratic questioning -- expose hidden assumptions            |
| `ooo auto`       | `ouroboros auto`                                                  | Goal → A-grade Seed → execution handoff with bounded loops   |
| `ooo seed`       | *(generated by interview)*                                        | Crystallize into immutable spec                              |
| `ooo run`        | `ouroboros run seed.yaml`                                         | Execute via Double Diamond decomposition                     |
| `ooo evaluate`   | *(via MCP)*                                                       | 3-stage verification gate                                    |
| `ooo evolve`     | *(via MCP)*                                                       | Evolutionary loop until ontology converges                   |
| `ooo unstuck`    | *(via MCP)*                                                       | 5 lateral thinking personas when you are stuck               |
| `ooo status`     | `ouroboros status executions` / `ouroboros status execution <id>` | Session tracking + (MCP-only) drift detection                |
| `ooo resume-session` | `ouroboros resume`                                           | List in-flight sessions and re-attach commands              |
| `ooo cancel`     | `ouroboros cancel execution [<id>\|--all]`                        | Cancel stuck or orphaned executions                          |
| `ooo ralph`      | *(via MCP)*                                                       | Persistent loop until verified                               |
| `ooo tutorial`   | *(interactive)*                                                   | Interactive hands-on learning                                |
| `ooo help`       | `ouroboros --help`                                                | Full reference                                               |
| `ooo pm`         | *(via MCP)*                                                       | PM-focused interview + PRD generation                        |
| `ooo qa`         | *(via skill)*                                                     | General-purpose QA verdict for any artifact                  |
| `ooo update`     | `ouroboros update`                                                | Check for updates + upgrade to latest                        |
| `ooo brownfield` | *(via skill)*                                                     | Scan and manage brownfield repo/worktree defaults            |
| `ooo publish`    | *(skill/runtime surface; uses `gh` CLI)*                          | Publish a Seed as GitHub Epic/Task issues for team workflows |

> Not all skills have direct CLI equivalents. Some (`evaluate`, `evolve`, `unstuck`, `ralph`, `publish`) are available through agent skills, runtime rules, or MCP tools rather than a direct `ouroboros <subcommand>` shell command.
> `/resume` is reserved for Claude Code's built-in session picker; use `ooo resume-session` for Ouroboros in-flight sessions.
> Claude Code also reserves `/run`, `/status`, `/help`, and `/config`. The safe
> direct skill forms are `/ouroboros:ouroboros-run`,
> `/ouroboros:ouroboros-status`, `/ouroboros:ouroboros-help`, and
> `/ouroboros:ouroboros-config`; the familiar `ooo run`, `ooo status`,
> `ooo help`, and `ooo config` phrases remain supported.

See the [CLI reference](./docs/cli-reference.md) for full details.

---

## The Nine Minds

Nine highlighted agents below, with 12 more specialized agents available (21 total). All loaded on-demand, never preloaded:

| Agent                    | Role                               | Core Question                                       |
| :----------------------- | :--------------------------------- | :-------------------------------------------------- |
| **Socratic Interviewer** | Questions-only. Never builds.      | *"What are you assuming?"*                          |
| **Ontologist**           | Finds essence, not symptoms        | *"What IS this, really?"*                           |
| **Seed Architect**       | Crystallizes specs from dialogue   | *"Is this complete and unambiguous?"*               |
| **Evaluator**            | 3-stage verification               | *"Did we build the right thing?"*                   |
| **Contrarian**           | Challenges every assumption        | *"What if the opposite were true?"*                 |
| **Hacker**               | Finds unconventional paths         | *"What constraints are actually real?"*             |
| **Simplifier**           | Removes complexity                 | *"What's the simplest thing that could work?"*      |
| **Researcher**           | Stops coding, starts investigating | *"What evidence do we actually have?"*              |
| **Architect**            | Identifies structural causes       | *"If we started over, would we build it this way?"* |

---

## Under the Hood

<details>
<summary><strong>Architecture overview -- Python >= 3.12</strong></summary>

```
src/ouroboros/
+-- bigbang/        Interview, ambiguity scoring, brownfield explorer
+-- routing/        PAL Router -- 3-tier cost optimization (1x / 10x / 30x)
+-- execution/      (deprecated — logic moved to orchestrator/ and mcp/tools/)
+-- evaluation/     Mechanical -> Semantic -> Multi-Model Consensus
+-- evolution/      Wonder / Reflect cycle, convergence detection
+-- resilience/     4-pattern stagnation detection, 5 lateral personas
+-- observability/  3-component drift measurement, auto-retrospective
+-- persistence/    Event sourcing (SQLAlchemy + aiosqlite), checkpoints
+-- orchestrator/   Runtime abstraction layer (Claude Code, Codex CLI, OpenCode, Hermes, Gemini, Kiro, Copilot, Pi, GJC, Goose, Antigravity, Grok, Zcode)
+-- core/           Types, errors, seed, ontology, security
+-- providers/      LiteLLM adapter (100+ models)
+-- mcp/            MCP client/server integration
+-- plugin/         Plugin system (skill/agent auto-discovery)
+-- tui/            Terminal UI dashboard
+-- cli/            Typer-based CLI
```

**Key internals:**
- **PAL Router** -- Frugal (1x) -> Standard (10x) -> Frontier (30x) with auto-escalation on failure, auto-downgrade on success
- **Drift** -- Goal (50%) + Constraint (30%) + Ontology (20%) weighted measurement, threshold <= 0.3
- **Brownfield** -- Auto-detects config files across multiple language ecosystems
- **Evolution** -- Up to 30 generations, convergence at ontology similarity >= 0.95
- **Stagnation** -- Detects spinning, oscillation, no-drift, and diminishing returns patterns
- **Agent OS runtime** -- Replayable execution contract across capability discovery, policy, directives, event journal, and agent processes
- **Runtime backends** -- Pluggable abstraction layer (`orchestrator.runtime_backend` config) with first-class support for Claude Code, Codex CLI, OpenCode, Hermes, Gemini, Goose, Kiro, Copilot, and Pi; same workflow spec, different execution engines

See [Architecture](./docs/architecture.md) for the full design document.

</details>

---

## From Wonder to Ontology

<details>
<summary><strong>The philosophical engine behind Ouroboros</strong></summary>

> *Wonder -> "How should I live?" -> "What IS 'live'?" -> Ontology*
> -- Socrates

Every great question leads to a deeper question -- and that deeper question is always **ontological**: not *"how do I do this?"* but *"what IS this, really?"*

```
   Wonder                          Ontology
"What do I want?"    ->    "What IS the thing I want?"
"Build a task CLI"   ->    "What IS a task? What IS priority?"
"Fix the auth bug"   ->    "Is this the root cause, or a symptom?"
```

This is not abstraction for its own sake. When you answer *"What IS a task?"* -- deletable or archivable? solo or team? -- you eliminate an entire class of rework. **The ontological question is the most practical question.**

Ouroboros embeds this into its architecture through the **Double Diamond**:

```
    * Wonder          * Design
   /  (diverge)      /  (diverge)
  /    explore      /    create
 /                 /
* ------------ * ------------ *
 \                 \
  \    define       \    deliver
   \  (converge)     \  (converge)
    * Ontology        * Evaluation
```

The first diamond is **Socratic**: diverge into questions, converge into ontological clarity. The second diamond is **pragmatic**: diverge into design options, converge into verified delivery. Each diamond requires the one before it -- you cannot design what you have not understood.

</details>

<details>
<summary><strong>Ambiguity Score: The Gate Between Wonder and Code</strong></summary>

The Interview does not end when you feel ready -- it ends when the **math** says you are ready. Ouroboros quantifies ambiguity as the inverse of weighted clarity:

```
Ambiguity = 1 - Sum(clarity_i * weight_i)
```

Each dimension is scored 0.0-1.0 by the LLM (temperature 0.1 for reproducibility), then weighted:

| Dimension                                                     | Greenfield | Brownfield |
| :------------------------------------------------------------ | :--------: | :--------: |
| **Goal Clarity** -- *Is the goal specific?*                   |    40%     |    35%     |
| **Constraint Clarity** -- *Are limitations defined?*          |    30%     |    25%     |
| **Success Criteria** -- *Are outcomes measurable?*            |    30%     |    25%     |
| **Context Clarity** -- *Is the existing codebase understood?* |     --     |    15%     |

**Threshold: Ambiguity <= 0.2.** A score above that blocks Seed generation. Passing `force` explicitly is what gets past it, and the CLI puts that choice on screen next to continue and cancel. The gate is a default worth arguing with, not a lock.

```
Example (Greenfield):

  Goal: 0.9 * 0.4  = 0.36
  Constraint: 0.8 * 0.3  = 0.24
  Success: 0.7 * 0.3  = 0.21
                        ------
  Clarity             = 0.81
  Ambiguity = 1 - 0.81 = 0.19  <= 0.2 -> Ready for Seed
```

Why 0.2? Because at 80% weighted clarity, the remaining unknowns are small enough that code-level decisions can resolve them. Above that threshold, you are still guessing at architecture.

</details>

<details>
<summary><strong>Ontology Convergence: When the Serpent Stops</strong></summary>

The evolutionary loop does not run forever. It stops when consecutive generations produce ontologically identical schemas. Similarity is measured as a weighted comparison of schema fields:

```
Similarity = 0.5 * name_overlap + 0.3 * type_match + 0.2 * exact_match
```

| Component        | Weight | What It Measures                                   |
| :--------------- | :----: | :------------------------------------------------- |
| **Name overlap** |  50%   | Do the same field names exist in both generations? |
| **Type match**   |  30%   | Do shared fields have the same types?              |
| **Exact match**  |  20%   | Are name, type, AND description all identical?     |

**Threshold: Similarity >= 0.95** -- the loop converges and stops evolving.

But raw similarity is not the only signal. The system also detects pathological patterns:

| Signal                  | Condition                                        | What It Means                      |
| :---------------------- | :----------------------------------------------- | :--------------------------------- |
| **Stagnation**          | Similarity >= 0.95 for 3 consecutive generations | Ontology has stabilized            |
| **Oscillation**         | Gen N ~ Gen N-2 (period-2 cycle)                 | Stuck bouncing between two designs |
| **Repetitive feedback** | >= 70% question overlap across 3 generations     | Wonder is asking the same things   |
| **Hard cap**            | 30 generations reached                           | Safety valve                       |

```
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     -> similarity 0.78 -> CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     -> similarity 1.00 -> CONVERGED
```

Two mathematical gates, one philosophy: **do not build until you are clear (Ambiguity <= 0.2), do not stop evolving until you are stable (Similarity >= 0.95).**

</details>

---

## Contributing

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --python 3.13 --all-groups
uv run --python 3.13 --no-sync pytest
```

[Issues](https://github.com/Q00/ouroboros/issues) · [Discussions](https://github.com/Q00/ouroboros/discussions) · [Contributing Guide](./CONTRIBUTING.md)

---

## Sponsors

Ouroboros is MIT-licensed and built in the open. If it saves you rework — or you want the loop to keep evolving — consider sponsoring. Sponsorship directly funds maintenance, new runtime integrations, and sponsor-only deep-dive content.

<p align="center">
  <a href="https://github.com/sponsors/Q00"><img src="https://img.shields.io/badge/%E2%9D%A4%EF%B8%8E%20Sponsor%20on%20GitHub-EA4AAA?style=for-the-badge&logo=githubsponsors&logoColor=white" alt="Sponsor Q00 on GitHub"></a>
</p>

Every sponsor keeps the serpent evolving. Thank you.

---

## Activity

These numbers are generated from GitHub data and refreshed automatically; caching may delay updates.

<p align="center">
  <a href="https://github.com/Q00/ouroboros/graphs/contributors"><img src="https://img.shields.io/github/contributors/Q00/ouroboros?color=orange" alt="Contributors"></a>
  <a href="https://github.com/Q00/ouroboros/commits/main"><img src="https://img.shields.io/github/commit-activity/m/Q00/ouroboros?color=orange" alt="Commit activity"></a>
  <a href="https://github.com/Q00/ouroboros/pulls?q=is%3Apr+is%3Aclosed"><img src="https://img.shields.io/github/issues-pr-closed/Q00/ouroboros?color=orange" alt="Closed pull requests"></a>
  <a href="https://github.com/Q00/ouroboros/commits/main"><img src="https://img.shields.io/github/last-commit/Q00/ouroboros?color=orange" alt="Last commit"></a>
</p>

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <strong>The serpent does not repeat -- it evolves.</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
