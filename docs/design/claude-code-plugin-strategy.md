# Ouroboros Dual Identity Strategy

> Ouroboros는 하나의 코어 위에 두 개의 인터페이스를 제공합니다.
> Python이 있으면 풀파워 모드, 없으면 프롬프트 기반 모드.
> MCP Server가 두 세계를 연결하는 다리입니다.

**Date**: 2026-02-09
**Status**: Draft (Revised)
**Version**: 0.2.0

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Dual Identity Architecture](#2-dual-identity-architecture)
3. [Shared vs Mode-Specific Components](#3-shared-vs-mode-specific-components)
4. [Data Flow](#4-data-flow)
5. [Graceful Degradation Strategy](#5-graceful-degradation-strategy)
6. [MCP Bridge Design](#6-mcp-bridge-design)
7. [Star Solicitation Strategy](#7-star-solicitation-strategy)
8. [Plugin Architecture](#8-plugin-architecture)
9. [Implementation Roadmap](#9-implementation-roadmap)
10. [Risk Analysis](#10-risk-analysis)
11. [Technical References](#11-technical-references)

---

## 1. Executive Summary

Ouroboros는 **Dual Identity**를 가진 프로젝트입니다.

- **Mode A: Standalone Python Framework** -- CLI (Typer, 6 commands), TUI (Textual, 4 screens), SQLite event store, 직접 Python API 호출
- **Mode B: Claude Code Plugin** -- Skills (8 SKILL.md), Agents (7 agent .md), Hooks (keyword detection, drift monitoring)

이 둘은 별개의 제품이 **아닙니다**. 플러그인은 프레임워크 위의 얇은 프레젠테이션 레이어입니다. MCP Server(FastMCP)가 두 모드를 연결하는 브릿지 역할을 합니다. Standalone 모드에서는 Python이 직접 코어를 호출하고, Plugin 모드에서는 MCP 프로토콜(stdio)을 통해 동일한 코어에 접근합니다.

### One-Liner

> "하나의 코어, 두 개의 인터페이스. Python이 있으면 풀파워, 없으면 프롬프트 모드."

### 핵심 가치

| 관점 | Ouroboros가 제공하는 것 |
|---|---|
| Requirement Crystallization | 모호한 요구사항을 Socratic interview로 정제 (ambiguity score <= 0.2 gate) |
| Cost-Optimized Routing | PAL Router -- 복잡도 기반 자동 모델 에스컬레이션/다운그레이드 |
| Stagnation Detection | 4가지 정체 패턴 감지 + 5가지 persona 자동 전환 (lateral thinking) |
| 3-Stage Verification | Mechanical -> Semantic -> Consensus 파이프라인 |
| Drift Measurement | 실시간 goal deviation 측정 (`observability/drift.py`) |
| Event Sourcing | SQLite immutable event store, session recovery 지원 |

---

## 2. Dual Identity Architecture

아래 다이어그램은 Ouroboros의 두 가지 접근 경로가 하나의 Shared Core로 수렴하는 구조를 보여줍니다.

```
+============================================================================+
|                        OUROBOROS DUAL IDENTITY                              |
|                                                                            |
|  +---------------------------+       +---------------------------+         |
|  | MODE A: Standalone        |       | MODE B: Claude Code Plugin|         |
|  |                           |       |                           |         |
|  | CLI (Typer, 6 cmds)       |       | Skills (8 SKILL.md)       |         |
|  | TUI (Textual, 4 screens)  |       | Agents (7 agent .md)      |         |
|  | Direct Python API         |       | Hooks (keyword, drift)    |         |
|  +-------------+-------------+       +-----------+---------------+         |
|                |                                  |                         |
|                v                                  v                         |
|     Direct Python calls               MCP Protocol (stdio)                  |
|                |                                  |                         |
|     +----------+----------------------------------+---------+              |
|     |              MCP SERVER (FastMCP)                      |              |
|     |  uvx ouroboros-ai mcp serve                            |              |
|     |                                                        |              |
|     |  Tools (8):                                            |              |
|     |    ouroboros_interview, ouroboros_generate_seed,        |              |
|     |    ouroboros_execute_seed, ouroboros_evaluate,          |              |
|     |    ouroboros_lateral_think, ouroboros_measure_drift,    |              |
|     |    ouroboros_session_status, ouroboros_query_events     |              |
|     +-------------------------+--------------------------+   |              |
|     |              SHARED CORE (Python 3.14+)            |   |              |
|     |                                                    |   |              |
|     |  bigbang/ | routing/ | execution/ | resilience/    |   |              |
|     |  evaluat/ | orchestr/ | persistence/ | observ/     |   |              |
|     |  providers/ | config/ | core/                      |   |              |
|     +====================================================+   |              |
+============================================================================+
```

### 구조적 핵심

- **Mode A**는 Python을 직접 호출합니다. CLI 명령어(`ouroboros init`, `ouroboros run` 등)가 Typer를 통해 코어 모듈을 실행합니다. TUI는 Textual 프레임워크로 4개 화면(dashboard, execution, logs, debug)을 제공합니다.
- **Mode B**는 Claude Code 환경 안에서 동작합니다. 사용자가 `/ouroboros:interview` 같은 skill을 호출하면, MCP 프로토콜을 통해 동일한 Python 코어에 접근합니다. Python이 없으면 agent markdown의 프롬프트 기반으로 기능이 제한됩니다.
- **MCP Server**는 `create_ouroboros_server()` (FastMCP 기반)를 composition root로 사용하며, 8개 tool handler를 등록합니다.

---

## 3. Shared vs Mode-Specific Components

| Component | Shared | Mode A Only | Mode B Only |
|---|---|---|---|
| **Interview Engine** (`bigbang/interview.py`) | Core logic | CLI `ouroboros init start` command | `/ouroboros:interview` skill |
| **Seed Generator** (`bigbang/seed_generator.py`) | Core logic | CLI `ouroboros init` flow (seed auto-gen) | `/ouroboros:seed` skill |
| **Orchestrator Runner** (`orchestrator/runner.py`) | Core logic | CLI `ouroboros run workflow` command | `/ouroboros:run` skill via MCP |
| **Evaluation Pipeline** (`evaluation/pipeline.py`) | Core logic | Planned (`ouroboros run --evaluate`) | `/ouroboros:evaluate` skill |
| **Lateral Thinking** (`resilience/lateral.py`) | Core logic | Auto-activated during execution | `/ouroboros:unstuck` skill |
| **Drift Measurement** (`observability/drift.py`) | Core logic | TUI dashboard widget | `drift-monitor.mjs` hook |
| **PAL Router** (`routing/`) | Core logic | Direct Python call | MCP tool parameter (`model_tier`) |
| **Event Store** (`persistence/`) | SQLite engine | Direct query | `ouroboros_query_events` MCP tool |
| **Session Management** (`orchestrator/session.py`) | Core logic | Direct access | `ouroboros_session_status` MCP tool |
| **TUI** (`tui/`) | -- | 4 Textual screens | -- (not applicable) |
| **CLI** (`cli/main.py`) | -- | Typer 6 commands | -- (not applicable) |
| **Skills** (`skills/*.md`) | -- | -- | 8 SKILL.md files |
| **Agents** (`agents/*.md`) | -- | -- | 7 agent definitions |
| **Hooks** (`hooks/hooks.json`) | -- | -- | keyword-detector, drift-monitor |
| **Plugin Manifest** (`plugin.json`) | -- | -- | Claude Code plugin metadata |

핵심 원칙: 모든 비즈니스 로직은 Shared Core에 있습니다. Mode A와 Mode B는 각각 Python 직접 호출과 MCP 프로토콜이라는 서로 다른 인터페이스를 통해 동일한 코어에 접근합니다.

---

## 4. Data Flow

### Mode A: Standalone (Full Python)

```
User
  |
  v
CLI (Typer)
  |
  v
InterviewEngine (bigbang/interview.py)
  |  start_interview() → ask_next_question() → record_response()
  |  ambiguity scoring <= 0.2
  v
Seed (YAML specification)
  |
  v
OrchestratorRunner (orchestrator/runner.py)
  |  execute_seed()
  |  PAL Router (model escalation/downgrade)
  |  Double Diamond (Discover -> Define -> Design -> Deliver)
  v
EventStore (SQLite)
  |  immutable event sourcing
  |  session recovery
  v
TUI (Textual)
  |  dashboard_v3: AC tree visualization
  |  execution: real-time progress
  |  logs: structured event log
  |  debug: internal state inspection
  v
Evaluation Pipeline
  |  Mechanical -> Semantic -> Consensus
  v
Result
```

이 흐름에서 모든 단계는 Python 내부에서 직접 실행됩니다. EventStore는 SQLite에 모든 이벤트를 immutable하게 기록하고, TUI는 0.5초 폴링으로 실시간 상태를 표시합니다.

### Mode B: Claude Code Plugin

```
User
  |
  v
Skill (/ouroboros:interview) or Hook (keyword detection)
  |
  v
MCP Tool Call (stdio protocol)
  |  ouroboros_interview / ouroboros_generate_seed / ...
  v
Shared Core (Python 3.14+)
  |  same InterviewEngine, same OrchestratorRunner
  v
MCP Response (JSON)
  |  ContentType.TEXT results
  |  meta: session_id, status, progress
  v
Claude Code (renders response to user)
```

**Fallback (Python 미설치 시)**:

```
User
  |
  v
Skill (/ouroboros:interview)
  |
  v
Agent (socratic-interviewer.md)
  |  prompt-only Socratic questioning
  |  no ambiguity scoring (numerical)
  |  no persistence (no SQLite)
  v
Claude Code (markdown-based interview output)
  |
  v
Agent (seed-architect.md)
  |  prompt-only seed generation
  v
Seed (markdown, not YAML — no validation)
```

Python이 없는 환경에서는 agent markdown의 프롬프트만으로 동작합니다. 인터뷰는 가능하지만 ambiguity scoring, event persistence, drift measurement 같은 수치 기반 기능은 사용할 수 없습니다.

---

## 5. Graceful Degradation Strategy

### Feature Matrix by Availability

| Feature | Full Mode (Python + MCP) | Plugin-Only (No Python) |
|---|---|---|
| Socratic Interview | InterviewEngine with `start_interview()` / `ask_next_question()` | Agent prompt-based Q&A |
| Ambiguity Scoring | Numerical score (0.0-1.0), 0.2 threshold gate | Not available (qualitative only) |
| Seed Generation | Validated YAML with schema enforcement | Markdown-based seed (no validation) |
| Seed Execution | `OrchestratorRunner.execute_seed()` via MCP | Not available |
| PAL Router | Automatic model tier selection (small/medium/large) | Manual model hint in skill |
| Lateral Thinking | 5 personas with stagnation pattern detection | Agent personas (prompt-only) |
| 3-Stage Evaluation | Mechanical -> Semantic -> Consensus pipeline | Prompt-based evaluation checklist |
| Drift Measurement | Real-time numerical drift score | Not available |
| Event Persistence | SQLite immutable event store | Not available |
| Session Recovery | Full session resume from event store | Not available |
| TUI Dashboard | 4 Textual screens | Not applicable |
| Magic Keywords | Hook-based detection -> MCP tool call | Hook-based detection -> agent call |

### Detection Logic

Setup wizard (`/ouroboros:setup`)가 환경을 자동 감지합니다.

```
Step 1: Check Python availability
  |
  +-- python3 --version >= 3.14?
  |     |
  |     +-- YES: Check uvx availability
  |     |    |
  |     |    +-- uvx available?
  |     |    |    YES -> Full Mode (MCP server enabled)
  |     |    |    NO  -> Install uvx prompt, fallback to Plugin-Only
  |     |    |
  |     +-- NO: Check python3 --version >= 3.12?
  |          |
  |          +-- YES: "Python 3.12+ detected. Some features require 3.14+.
  |          |         Consider upgrading for full support."
  |          |         -> Plugin-Only mode with future upgrade path
  |          |
  |          +-- NO: Plugin-Only mode
  |
  +-- No python3 found -> Plugin-Only mode
```

감지 결과에 따라 `.mcp.json` 등록 여부가 결정됩니다. Full Mode에서는 MCP server가 등록되어 8개 tool을 모두 사용할 수 있고, Plugin-Only에서는 agent markdown과 skill만으로 동작합니다.

---

## 6. MCP Bridge Design

MCP Server는 Dual Identity의 핵심 브릿지입니다. Mode A에서는 Python이 직접 코어를 호출하지만, Mode B에서는 MCP 프로토콜을 거쳐 동일한 코어에 접근합니다.

### Current State (v0.8.0)

현재 `src/ouroboros/mcp/tools/definitions.py`에 3개의 tool handler가 정의되어 있지만, 모두 placeholder 상태입니다.

| Tool | Handler Class | Status |
|---|---|---|
| `ouroboros_execute_seed` | `ExecuteSeedHandler` | Placeholder (`# TODO: Integrate with actual execution engine`) |
| `ouroboros_session_status` | `SessionStatusHandler` | Placeholder (`# TODO: Integrate with actual session management`) |
| `ouroboros_query_events` | `QueryEventsHandler` | Placeholder (`# TODO: Integrate with actual event store`) |

### Target State (v0.9.0)

8개의 실제 tool handler를 구현하여, Plugin 모드에서도 전체 파이프라인에 접근 가능하게 합니다.

| Tool | Maps To | Description |
|---|---|---|
| `ouroboros_interview` | `bigbang/interview.py` | Socratic interview 시작/진행, `InterviewState` persistence |
| `ouroboros_generate_seed` | `bigbang/seed_generator.py` | Interview 결과에서 Seed YAML 생성 |
| `ouroboros_execute_seed` | `orchestrator/runner.py` | Seed 기반 task 실행 (existing, needs wiring) |
| `ouroboros_evaluate` | `evaluation/pipeline.py` | 3-stage evaluation 실행 |
| `ouroboros_lateral_think` | `resilience/lateral.py` | Stagnation 감지 시 persona 전환 |
| `ouroboros_measure_drift` | `observability/drift.py` | 현재 실행의 goal deviation 측정 |
| `ouroboros_session_status` | `orchestrator/session.py` | Session 상태 조회 (existing, needs wiring) |
| `ouroboros_query_events` | `persistence/` EventStore | Event history 조회 (existing, needs wiring) |

### Composition Root

`create_ouroboros_server()`가 모든 의존성을 조립하는 단일 진입점입니다.

```python
# src/ouroboros/mcp/server/adapter.py:377 (CURRENT STATE)
def create_ouroboros_server(
    *,
    name: str = "ouroboros-mcp",
    version: str = "1.0.0",
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
) -> MCPServerAdapter:
    """Create an Ouroboros MCP server with default handlers."""
    server = MCPServerAdapter(
        name=name,
        version=version,
        auth_config=auth_config,
        rate_limit_config=rate_limit_config,
    )
    # Tools and resources will be registered separately
    # to avoid circular imports
    return server
```

**Target state** (Phase 2에서 구현): 이 factory가 composition root가 되어 모든 shared service를 인스턴스화하고, 각 tool handler에 DI를 수행합니다.

Target 구현에서는 각 handler가 생성자를 통해 실제 코어 모듈 인스턴스를 주입받습니다. 예를 들어 `ExecuteSeedHandler`는 `OrchestratorRunner` 인스턴스를, `QueryEventsHandler`는 `EventStore` 인스턴스를 받게 됩니다.

### Stateful Interview via MCP

Interview는 다회 왕복이 필요한 stateful 프로세스입니다. MCP tool은 기본적으로 stateless이므로, 기존 `InterviewState` persistence를 활용합니다.

```
Call 1: ouroboros_interview(topic="Build a REST API")
  -> Returns: { question: "...", session_id: "abc-123", progress: 1/5 }

Call 2: ouroboros_interview(session_id="abc-123", answer="It should handle...")
  -> Returns: { question: "...", session_id: "abc-123", progress: 2/5 }

...

Call N: ouroboros_interview(session_id="abc-123", answer="...")
  -> Returns: { complete: true, ambiguity_score: 0.15, seed_ready: true }
```

`session_id`가 상태의 키 역할을 하며, `InterviewState`가 SQLite에 중간 상태를 저장합니다. Claude Code가 여러 번 MCP tool을 호출하면서 인터뷰를 진행합니다.

---

## 7. Star Solicitation Strategy

### 기존 문서의 오류 정정

v0.1.0 문서에서는 OMC의 star solicitation 접근을 "Manipulative Star Solicitation"이라고 표현했습니다. 이는 분석 결과 **부정확한 평가**였습니다.

### 분석 결과

OMC의 접근은 다크패턴이 **아닙니다** (Brignull/Gray 분류법 기준).

다크패턴의 핵심 조건은 다음과 같습니다:
- 사용자의 의도와 반대되는 행동을 유도하는가?
- "No" 선택이 "Yes"보다 어려운가?
- 기능이 blocking되는가?

OMC의 실제 동작:
- Setup 마지막 단계에서 `AskUserQuestion`으로 묻습니다
- "No thanks"가 "Yes"와 동일한 노력(한 번 클릭)입니다
- Star 여부와 관계없이 모든 기능이 동일하게 동작합니다
- Blocking이 없습니다

### 스펙트럼 분석

```
DARK PATTERNS                                              ACCEPTABLE PATTERNS
     |                                                              |
Cookie walls   EU consent   Homebrew    OMC setup    npm post    Apple
(forced)       dark patterns opt-out    star ask     install     SKStore
                                            ^
                                            |
                                    Brignull 기준 acceptable
                                    "No thanks" = same effort as "Yes"
                                    No functionality gating
```

OMC는 스펙트럼에서 acceptable 영역에 위치합니다. 다만 Ouroboros는 더 세련된 접근을 취합니다.

### Ouroboros 추천 접근: Hybrid (Option D)

사용자 경험을 우선하면서도 프로젝트 성장을 지원하는 하이브리드 접근입니다.

**Phase A: Setup 완료 시점 (최초 1회)**

Setup wizard의 마지막 단계에서 `AskUserQuestion`으로 3가지 옵션을 제시합니다.

```
"Ouroboros 설치가 완료되었습니다!

이 프로젝트가 도움이 된다면, GitHub에서 Star를 눌러주시면
개발 지속에 큰 힘이 됩니다.

  [1] Yes, star the project
  [2] No thanks
  [3] Remind me after my first interview"
```

- Option 1: `gh api -X PUT /user/starred/Q00/ouroboros` 실행, `~/.ouroboros/prefs.json`에 `star_asked: true` 기록
- Option 2: `star_asked: true` 기록, 다시 묻지 않음 (영구 opt-out)
- Option 3: `star_remind_after_interview: true` 기록

**Phase B: 첫 인터뷰 완료 후 (최대 1회)**

Phase A에서 Option 3("Remind me")을 선택한 사용자에게만 적용됩니다.

```
"인터뷰가 완료되었습니다! Ambiguity score: 0.15 (ready for execution)

이전에 '첫 인터뷰 후 다시 알려달라'고 하셨습니다.
Ouroboros가 도움이 되셨다면:

  [1] Yes, star the project
  [2] No thanks (won't ask again)"
```

- Option 1: `gh api -X PUT /user/starred/Q00/ouroboros` 실행
- Option 2: 영구 opt-out

**제약 조건:**

| Rule | Description |
|---|---|
| 최대 횟수 | 사용자당 최대 2번 (Phase A 1회 + Phase B 1회) |
| 영구 opt-out | "No thanks" 선택 시 다시 묻지 않음 |
| 중립적 언어 | 감정적 압박 없음 ("도움이 된다면" 수준) |
| 기능 gating 없음 | Star 여부와 무관하게 모든 기능 동일 |
| Preference storage | `~/.ouroboros/prefs.json` (plugin scope 내) |

### 구현 흐름

```python
# Pseudocode for star solicitation logic
def should_ask_star() -> tuple[bool, str]:
    prefs = load_prefs()  # ~/.ouroboros/prefs.json

    if prefs.get("star_asked"):
        return (False, "")

    if prefs.get("star_remind_after_interview"):
        if prefs.get("first_interview_completed"):
            return (True, "phase_b")
        return (False, "")

    if not prefs.get("setup_completed"):
        return (False, "")

    return (True, "phase_a")


def handle_star_response(phase: str, choice: int) -> None:
    if choice == 1:  # Yes
        execute("gh api -X PUT /user/starred/Q00/ouroboros")
        save_pref("star_asked", True)
    elif choice == 2:  # No thanks
        save_pref("star_asked", True)  # permanent opt-out
    elif choice == 3 and phase == "phase_a":  # Remind me later
        save_pref("star_remind_after_interview", True)
```

---

## 8. Plugin Architecture

### Directory Structure

```
ouroboros-plugin/
├── .claude-plugin/
│   └── plugin.json                 # Required plugin manifest
├── agents/
│   ├── socratic-interviewer.md     # Big Bang interview agent
│   ├── ontologist.md               # Ontological analysis agent
│   ├── seed-architect.md           # Seed generation agent
│   ├── evaluator.md                # 3-stage evaluation agent
│   ├── contrarian.md               # "Wrong problem?" persona
│   ├── hacker.md                   # "Make it work" persona
│   └── simplifier.md              # "Cut scope, return to MVP" persona
├── skills/
│   ├── interview/SKILL.md          # /ouroboros:interview
│   ├── seed/SKILL.md               # /ouroboros:seed
│   ├── run/SKILL.md                # /ouroboros:run
│   ├── evaluate/SKILL.md           # /ouroboros:evaluate
│   ├── unstuck/SKILL.md            # /ouroboros:unstuck (5 personas)
│   ├── status/SKILL.md             # /ouroboros:status
│   ├── setup/SKILL.md              # /ouroboros:setup (wizard)
│   └── help/SKILL.md               # /ouroboros:help
├── hooks/
│   └── hooks.json                  # Hook definitions
├── scripts/
│   ├── keyword-detector.mjs        # Magic keyword detection
│   └── drift-monitor.mjs           # Drift measurement on file changes
├── .mcp.json                       # MCP server configuration
└── README.md
```

### Plugin Manifest (`plugin.json`)

OMC의 실제 manifest 형식을 참조하여 설계합니다. 필수 필드: `name`, `version`, `description`, `author`, `repository`, `homepage`, `license`, `keywords`, `skills`, `mcpServers`.

```json
{
  "name": "ouroboros",
  "version": "0.8.0",
  "description": "Self-improving AI workflow system. Crystallize requirements before execution with Socratic interview, ambiguity scoring, and 3-stage evaluation.",
  "author": {
    "name": "Q00",
    "email": "jqyu.lee@gmail.com"
  },
  "repository": "https://github.com/Q00/ouroboros",
  "homepage": "https://github.com/Q00/ouroboros",
  "license": "MIT",
  "keywords": [
    "workflow",
    "requirements",
    "socratic",
    "interview",
    "seed",
    "evaluation",
    "self-improving",
    "drift-detection"
  ],
  "skills": "./skills/",
  "agents": "./agents/",
  "hooks": "./hooks/hooks.json",
  "mcpServers": "./.mcp.json"
}
```

### Skills (Slash Commands)

| Skill | Trigger Keywords | Maps To | MCP Tool |
|---|---|---|---|
| `/ouroboros:interview` | "interview me", "clarify requirements" | `bigbang/interview.py` | `ouroboros_interview` |
| `/ouroboros:seed` | "generate seed", "crystallize" | `bigbang/seed_generator.py` | `ouroboros_generate_seed` |
| `/ouroboros:run` | "ouroboros run", "execute seed" | `orchestrator/runner.py` | `ouroboros_execute_seed` |
| `/ouroboros:evaluate` | "evaluate this", "3-stage check" | `evaluation/pipeline.py` | `ouroboros_evaluate` |
| `/ouroboros:unstuck` | "I'm stuck", "think sideways" | `resilience/lateral.py` | `ouroboros_lateral_think` |
| `/ouroboros:status` | "session status", "drift check" | `orchestrator/session.py` | `ouroboros_session_status` |
| `/ouroboros:setup` | "setup ouroboros" | Setup wizard (new) | N/A |
| `/ouroboros:help` | "ouroboros help" | Help display (new) | N/A |

### Agents (Subagent Types)

| Agent | Role | Source |
|---|---|---|
| `ouroboros:socratic-interviewer` | 숨겨진 가정을 노출하는 Socratic questioning | Big Bang interview prompts |
| `ouroboros:ontologist` | "이것의 본질이 무엇인가?" -- 존재론적 분석 | Seed architecture patterns |
| `ouroboros:seed-architect` | Interview 결과를 immutable Seed spec으로 결정화 | `bigbang/seed_generator.py` |
| `ouroboros:evaluator` | 3-stage evaluation (mechanical, semantic, consensus) | `evaluation/pipeline.py` |
| `ouroboros:contrarian` | "우리가 잘못된 문제를 풀고 있는 건 아닌가?" | CONTRARIAN persona |
| `ouroboros:hacker` | "우아함은 나중에. 일단 동작하게 만들자." | HACKER persona |
| `ouroboros:simplifier` | "스코프를 반으로 줄여라. MVP로 돌아가라." | SIMPLIFIER persona |

### Hooks

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "node \"${CLAUDE_PLUGIN_ROOT}/scripts/keyword-detector.mjs\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "node \"${CLAUDE_PLUGIN_ROOT}/scripts/drift-monitor.mjs\"",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

Hook 설계 원칙:
- `UserPromptSubmit`: Magic keyword 감지만 수행 (경량 node script, 5초 timeout)
- `PostToolUse`: Write/Edit 시에만 drift 측정 (파일 변경 추적)
- 모든 이벤트를 hook하지 않음 -- 실행 품질에 집중

### Magic Keywords

| Keyword | Trigger Skill | OMC 키워드와 충돌 여부 |
|---|---|---|
| `"interview me"` | `/ouroboros:interview` | 충돌 없음 |
| `"crystallize"` | `/ouroboros:seed` | 충돌 없음 |
| `"am I drifting?"` | `/ouroboros:status` | 충돌 없음 |
| `"think sideways"` | `/ouroboros:unstuck` | 충돌 없음 |
| `"evaluate this"` | `/ouroboros:evaluate` | 충돌 없음 |

OMC 키워드(`ralph`, `ulw`, `eco`, `plan`, `autopilot`, `ultrapilot`)와 **완전히 분리**되어 있어 동시 설치 시에도 충돌이 없습니다.

### MCP Server Configuration (`.mcp.json`)

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["ouroboros-ai", "mcp", "serve"]
    }
  }
}
```

`uvx`가 Python 버전 격리를 자동으로 처리합니다. 사용자 시스템의 Python 버전과 관계없이 올바른 환경에서 MCP server가 실행됩니다.

### CLAUDE.md Injection

Setup wizard가 사용자 동의 하에 CLAUDE.md에 Ouroboros 설정을 추가합니다.

```markdown
<!-- OUROBOROS:START -->
<!-- OUROBOROS:VERSION:0.8.0 -->
# Ouroboros -- Requirement Crystallization Engine

## When to Use Ouroboros
- "interview me" -> /ouroboros:interview (Socratic requirement clarification)
- "crystallize" -> /ouroboros:seed (generate validated seed spec)
- "think sideways" -> /ouroboros:unstuck (5 lateral thinking personas)
- "evaluate this" -> /ouroboros:evaluate (3-stage verification)
- "am I drifting?" -> /ouroboros:status (drift measurement)

## Agents
- ouroboros:socratic-interviewer -- Exposes hidden assumptions
- ouroboros:ontologist -- Finds root problems, not symptoms
- ouroboros:seed-architect -- Crystallizes requirements into seed spec
- ouroboros:evaluator -- 3-stage verification (mechanical/semantic/consensus)
- ouroboros:contrarian -- "What if we're solving the wrong problem?"
<!-- OUROBOROS:END -->
```

Injection 규칙:
- 약 40줄 (최소한의 footprint)
- 항상 **APPEND** (기존 내용의 위치를 변경하지 않음)
- 수정 전 자동 백업 (`CLAUDE.md.bak`)
- `<!-- OUROBOROS:START -->` / `<!-- OUROBOROS:END -->` 마커 사용
- 제거: `/ouroboros:setup --uninstall` 한 번으로 완전 제거
- Injection 없이도 동작: `/ouroboros:*` skill 직접 호출 가능

---

## 9. Implementation Roadmap

### Phase 1: MVP (1 week)

**목표**: Plugin 설치 -> interview -> seed generation이 동작하는 최소 viable 상태.

| # | Task | Deliverable |
|---|---|---|
| 1 | `.claude-plugin/plugin.json` 생성 | Plugin installable via Claude Code |
| 2 | `agents/socratic-interviewer.md` -- Big Bang interview prompts 추출 | Socratic interview agent 사용 가능 |
| 3 | `agents/ontologist.md` -- 존재론적 분석 agent 정의 | Ontological analysis agent 사용 가능 |
| 4 | `skills/interview/SKILL.md` 작성 | `/ouroboros:interview` 동작 |
| 5 | `skills/seed/SKILL.md` 작성 | `/ouroboros:seed` 동작 (Seed YAML 생성) |
| 6 | `skills/help/SKILL.md` 작성 | `/ouroboros:help` 사용 가이드 |
| 7 | `README.md` 작성 | Plugin 설치/사용 문서 |

**핵심 검증**: `/ouroboros:interview "Build a REST API"` 실행 시 Socratic Q&A가 진행되고, `/ouroboros:seed`로 Seed 스펙이 생성되는 것.

### Phase 2: MCP Bridge (2 weeks)

**목표**: MCP server의 placeholder tool handler들을 실제 코어 모듈과 연결.

| # | Task | Deliverable |
|---|---|---|
| 8 | `ouroboros_interview` tool handler 구현 | Stateful interview via MCP |
| 9 | `ouroboros_generate_seed` tool handler 구현 | Seed generation via MCP |
| 10 | `ouroboros_execute_seed` handler wiring (`orchestrator/runner.py` 연결) | Seed execution via MCP |
| 11 | `ouroboros_evaluate` tool handler 구현 | 3-stage evaluation via MCP |
| 12 | `ouroboros_lateral_think` tool handler 구현 | Lateral thinking via MCP |
| 13 | `ouroboros_measure_drift` tool handler 구현 | Drift measurement via MCP |
| 14 | `ouroboros_session_status` handler wiring (`orchestrator/session.py` 연결) | Session status via MCP |
| 15 | `ouroboros_query_events` handler wiring (`persistence/` EventStore 연결) | Event query via MCP |
| 16 | `create_ouroboros_server()` composition root에 DI wiring 완성 | 모든 handler에 실제 의존성 주입 |

**핵심 검증**: Claude Code에서 MCP tool 호출 시 실제 Python 코어가 실행되고 결과가 반환되는 것.

### Phase 3: Growth (1 week)

**목표**: Setup wizard, star solicitation, 나머지 skill/agent 완성.

| # | Task | Deliverable |
|---|---|---|
| 17 | `skills/setup/SKILL.md` (5-step wizard) 작성 | `/ouroboros:setup` 동작 |
| 18 | Star solicitation 구현 (Hybrid Option D) | Phase A/B 로직, prefs.json |
| 19 | CLAUDE.md injection 구현 | 동의 기반 append, backup, uninstall |
| 20 | `scripts/keyword-detector.mjs` 구현 | Magic keyword hook 동작 |
| 21 | `hooks/hooks.json` 작성 | Hook 등록 완료 |
| 22 | 나머지 agent definitions 완성 (`seed-architect`, `evaluator`, `contrarian`, `hacker`, `simplifier`) | 7개 agent 전체 |
| 23 | 나머지 skills 완성 (`run`, `evaluate`, `unstuck`, `status`) | 8개 skill 전체 |

**핵심 검증**: `/ouroboros:setup` 실행 시 환경 감지, CLAUDE.md injection, MCP 등록이 순서대로 진행되는 것.

### Phase 4: Polish (Ongoing)

| # | Task | Description |
|---|---|---|
| 24 | `scripts/drift-monitor.mjs` 구현 | PostToolUse hook으로 Write/Edit 시 drift 측정 |
| 25 | Cross-plugin workflow 가이드 | Ouroboros interview -> OMC autopilot 워크플로우 문서 |
| 26 | Integration test suite | Plugin 설치/실행/제거 자동 테스트 |
| 27 | Multilingual README | ko, en 지원 |
| 28 | Community seed template library | GitHub Discussions 기반 seed 템플릿 공유 |

---

## 10. Risk Analysis

### Risk 1: Python 3.14+ Requirement -- HIGH

**문제**: `pyproject.toml:9`에 `requires-python = ">=3.14"`가 명시되어 있습니다. 대부분의 사용자 환경은 Python 3.12-3.13입니다.

**영향**: MCP server 모드 사용 불가 -> 코어 기능(`execute_seed`, `query_events`, ambiguity scoring 등) 차단.

**대응**:
1. Plugin-Only 모드가 Python 없이 동작합니다 (agents + skills = pure markdown)
2. `uvx`가 Python 버전 격리를 자동 처리합니다
3. 장기적으로 3.14 전용 기능(`type` 문법 등) 사용 부분을 감사하여 3.12+로 하한선 낮추는 것을 검토합니다
4. Setup wizard가 Python 버전을 감지하고 적절한 모드를 자동 선택합니다

### Risk 2: Plugin System Maturity -- MEDIUM

**문제**: Claude Code의 plugin 시스템은 아직 진화 중입니다. API 변경 가능성이 있습니다.

**영향**: Plugin manifest 형식이나 hook API 변경 시 plugin이 깨질 수 있습니다.

**대응**:
1. Agents와 Skills는 pure markdown이므로 API 변경에 탄력적입니다
2. Hooks는 최소한의 JavaScript (2개 script)로 surface area가 작습니다
3. OMC v4.1.4가 안정적으로 동작 중이므로, 현재 API는 충분히 안정적입니다
4. Plugin manifest는 OMC의 실제 동작 형식을 참조하여 호환성을 확보합니다

### Risk 3: Star Solicitation -- LOW (Revised)

**문제**: v0.1.0 문서에서 "Manipulative Star Solicitation"이라 표현한 접근이 분석 결과 acceptable 범위였습니다.

**영향**: 지나치게 보수적인 접근(star 요청 전면 금지)은 프로젝트 성장을 불필요하게 제한합니다.

**대응**:
1. Hybrid 접근(Option D) 채택: setup 시 1회 + 첫 인터뷰 후 조건부 1회
2. 최대 2번, 영구 opt-out, 중립적 언어
3. 기능 gating 없음 -- star 여부와 무관하게 모든 기능 동일
4. `~/.ouroboros/prefs.json`에 preference 저장

### Risk 4: CLAUDE.md Injection Trust -- MEDIUM

**문제**: 사용자는 도구가 CLAUDE.md를 수정하는 것에 경계심을 가집니다.

**영향**: 사용자가 setup을 거부하거나 즉시 제거할 수 있습니다.

**대응**:
1. 최소한의 injection (~40줄)
2. `AskUserQuestion`으로 명시적 동의 + injection 내용 미리보기 제공
3. `/ouroboros:setup --uninstall` 한 번으로 완전 제거
4. 수정 전 자동 백업 (`CLAUDE.md.bak`)
5. Injection 없이도 동작 -- `/ouroboros:*` skill 직접 호출 가능
6. 항상 APPEND, 기존 내용의 위치나 순서를 변경하지 않음

### Risk 5: Complexity Barrier -- MEDIUM

**문제**: 6-phase pipeline 용어(Big Bang, PAL Router, Double Diamond)가 신규 사용자에게 부담이 될 수 있습니다.

**대응**:
1. Progressive disclosure: 처음에는 "interview", "run", "evaluate"만 노출
2. 자연어 키워드: "interview me" (Big Bang 아님), "think sideways" (Resilience 아님)
3. Auto-suggestion: hook이 모호한 요청을 감지하면 "먼저 요구사항을 명확히 하시겠습니까?" 제안

---

## 11. Technical References

### Ouroboros Source Files

| File | Line | Relevance |
|---|---|---|
| `pyproject.toml` | 9 | `requires-python = ">=3.14"` -- Python version constraint |
| `src/ouroboros/mcp/tools/definitions.py` | 1-351 | 3 tool handlers (ExecuteSeedHandler, SessionStatusHandler, QueryEventsHandler) -- all placeholder |
| `src/ouroboros/mcp/tools/definitions.py` | 112 | `# TODO: Integrate with actual execution engine` |
| `src/ouroboros/mcp/tools/definitions.py` | 190 | `# TODO: Integrate with actual session management` |
| `src/ouroboros/mcp/tools/definitions.py` | 295 | `# TODO: Integrate with actual event store` |
| `src/ouroboros/mcp/server/adapter.py` | 295-369 | `serve()` method with FastMCP tool/resource registration |
| `src/ouroboros/mcp/server/adapter.py` | 377-408 | `create_ouroboros_server()` composition root |
| `src/ouroboros/bigbang/interview.py` | 182 | `start_interview()` -- interview session entry point |
| `src/ouroboros/bigbang/seed_generator.py` | -- | Seed YAML generation from interview results |
| `src/ouroboros/orchestrator/runner.py` | 324 | `execute_seed()` -- main execution entry point |
| `src/ouroboros/evaluation/pipeline.py` | -- | 3-stage evaluation (mechanical, semantic, consensus) |
| `src/ouroboros/resilience/lateral.py` | -- | 5 personas (HACKER, RESEARCHER, SIMPLIFIER, ARCHITECT, CONTRARIAN) |
| `src/ouroboros/observability/drift.py` | -- | Real-time goal deviation measurement |
| `src/ouroboros/cli/main.py` | 21-34 | CLI app structure with 6 command groups |
| `src/ouroboros/tui/app.py` | -- | TUI application entry, TUIState SSOT |

### Ouroboros Source Modules

| Module | Path | Description |
|---|---|---|
| Big Bang | `src/ouroboros/bigbang/` | Interview engine, seed generator, ambiguity scoring |
| Routing | `src/ouroboros/routing/` | PAL Router, complexity-based model selection |
| Execution | `src/ouroboros/execution/` | Double Diamond process, task execution |
| Resilience | `src/ouroboros/resilience/` | Stagnation detection, lateral thinking personas |
| Evaluation | `src/ouroboros/evaluation/` | 3-stage verification pipeline |
| Orchestrator | `src/ouroboros/orchestrator/` | Session management, runner, adapter |
| Persistence | `src/ouroboros/persistence/` | SQLite event store, session recovery |
| Observability | `src/ouroboros/observability/` | Drift measurement, metrics |
| Providers | `src/ouroboros/providers/` | LLM provider abstractions |
| Config | `src/ouroboros/config/` | Configuration management |
| Core | `src/ouroboros/core/` | Shared types, Result type, base abstractions |
| MCP | `src/ouroboros/mcp/` | MCP server/client, tool definitions, protocol |
| CLI | `src/ouroboros/cli/` | Typer-based CLI (6 command groups) |
| TUI | `src/ouroboros/tui/` | Textual-based TUI (4 screens) |

### Claude Code Plugin Documentation

| Resource | URL |
|---|---|
| Plugin Creation | https://code.claude.com/docs/en/plugins |
| Plugin Reference | https://code.claude.com/docs/en/plugins-reference |
| Marketplace | https://code.claude.com/docs/en/plugin-marketplaces |
| Skills Development | https://code.claude.com/docs/en/skills |
| Hooks Reference | https://code.claude.com/docs/en/hooks |
