<!--
doc_metadata:
  runtime_scope: [opencode]
-->

# OpenCode에서 Ouroboros 실행하기

> 설치 및 첫 실행 안내는 [시작하기](../getting-started.md)를 참고하세요.

Ouroboros는 오픈소스 멀티 공급자 AI 코딩 에이전트인 **OpenCode**([opencode.ai](https://opencode.ai))와 두 가지 경로로 통합됩니다.

1. **서브에이전트 브리지 플러그인(주 경로, 권장):** 대화형 OpenCode 세션 안에서 실행됩니다. `_subagent` 봉투를 반환하는 Ouroboros MCP 도구(예: ouroboros_qa, ouroboros_lateral_think persona="all")는 OpenCode 기본 **Task 패널**로 분기합니다. 서브에이전트마다 자식 세션 하나를 만들고 도구 호출 아래에 인라인으로 표시합니다. 세션 선택기에 불필요한 세션이 쌓이지 않고, 여러 페르소나에 병렬 전달하며 각 자식에 새 LLM 문맥을 제공합니다.
2. **하위 프로세스 런타임(대체 경로, 헤드리스/CI):** Ouroboros가 작업마다 opencode run --format json을 비대화형 하위 프로세스로 시작합니다. CLI 워크플로, 일괄 실행, 연결된 OpenCode 세션이 없는 환경에 적합합니다.

두 경로는 명세 우선 하네스(Seed, 완료 기준, 평가 원칙, 결정론적 종료 조건)를 공유합니다. 일상적인 대화형 작업에는 플러그인을, 자동화에는 하위 프로세스 런타임을 선택하세요. 기본 ouroboros-ai 패키지 외에 Python SDK는 필요하지 않습니다.

> **모델 권장 사항:** OpenCode는 설정한 공급자가 지원하는 모든 모델을 사용할 수 있습니다. 여러 단계의 에이전트 코딩 작업을 잘 처리하는 최상위 모델(Claude Opus, GPT-5.4 또는 동급 모델)을 권장합니다.

## 사전 요구 사항

- **OpenCode**를 설치·설정하고 PATH에서 실행 가능하게 둡니다(아래 [OpenCode 설치](#opencode-설치) 참고).
- **OpenCode에 공급자를 설정**합니다. opencode를 실행해 최초 설정을 완료하거나 opencode providers auth <provider>를 사용합니다.
- **Python >= 3.12**

> **참고:** OpenCode가 공급자 인증을 자체 관리하므로 Ouroboros용 ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 환경 변수를 설정할 필요가 없습니다. 인증 정보는 OpenCode의 ~/.config/opencode/opencode.jsonc(또는 opencode.json)에 저장됩니다.

## OpenCode 설치

OpenCode는 독립 실행 바이너리로 배포됩니다. 공식 설치 스크립트 또는 npm으로 설치합니다.

```bash
# Recommended: official installer
curl -fsSL https://opencode.ai/install | bash

# Alternative: npm
npm i -g opencode-ai@latest
```

설치를 확인합니다.

```bash
opencode --version
```

설치 후 opencode를 한 번 실행해 공급자를 선택하고 최초 인증을 완료합니다. 다른 설치 방법은 [OpenCode 문서](https://opencode.ai/docs)를 참고하세요.

## Ouroboros 설치

> 모든 설치 방법(pip, 한 줄 설치, 소스 설치)과 첫 실행 안내는 **[시작하기](../getting-started.md)**를 참고하세요. 기본 ouroboros-ai 패키지에 OpenCode 런타임 어댑터가 포함되어 있으므로 추가 패키지는 필요하지 않습니다.

## 플랫폼 참고 사항

OpenCode 런타임 어댑터는 Linux, macOS, Windows(WSL 2)를 대상으로 합니다. OpenCode 자체는 macOS와 네이티브 Windows를 지원하며 Ouroboros의 경로 처리와 하위 프로세스 전달은 이식 가능합니다.

| 플랫폼 | 상태 |
|---|---|
| Linux (x86_64 / ARM64) | 지원 |
| macOS (Apple Silicon / Intel) | 지원 |
| Windows (WSL 2) | 지원 |
| Windows (네이티브) | 최선 노력 지원 — 하위 프로세스 대체 경로는 WSL 2에서 실행 권장 |

## 설정

ouroboros setup --runtime opencode로 통합을 설정합니다. 함께 사용할 수 없는 다음 두 모드 중 하나를 선택합니다.

| 모드 | 동작 | 사용 시점 |
|---|---|---|
| plugin(기본값) | 브리지 플러그인을 설치하고 opencode.jsonc에 MCP 등록 | OpenCode 안에서 작업하며 _subagents 전달로 인라인 Task 패널을 쓸 때 |
| subprocess | ~/.ouroboros/config.yaml에 하위 프로세스 런타임 기록 | 헤드리스 실행, CI, 스크립트 파이프라인, 대화형 세션이 없을 때 |

opencode run 하위 프로세스에서 Ouroboros MCP 도구를 호출하면 전역 등록 플러그인도 실행되어 서브에이전트가 중복 전달되고 토큰을 낭비합니다. 따라서 두 모드는 상호 배타적입니다. 하나를 선택하세요. 같은 컴퓨터에서 두 모드를 의도적으로 연결하려면 --opencode-mode를 달리해 setup을 두 번 실행하고 토큰 비용을 감수합니다.

```bash
ouroboros setup --runtime opencode                              # interactive picker
ouroboros setup --runtime opencode --opencode-mode plugin       # inside-OpenCode default
ouroboros setup --runtime opencode --opencode-mode subprocess   # headless CI
ouroboros setup --runtime opencode --non-interactive            # accepts default (plugin)
```

설치 항목은 다음과 같습니다.

**plugin**
- <opencode_config_dir>/plugins/ouroboros-bridge/ouroboros-bridge.ts의 브리지 플러그인(원자적 기록, 콘텐츠 해시 사용, 변경 없으면 쓰지 않음)
- ~/.config/opencode/opencode.jsonc 또는 opencode.json의 플러그인 항목(오래된 중복 항목 제거)
- 같은 파일의 Ouroboros MCP 서버
- Claude SDK MCP 보조 설정은 건드리지 않아 MCP 1.x 프로필을 분리해 둠

**subprocess**
- ~/.ouroboros/config.yaml에 orchestrator.runtime_backend: opencode
- 같은 파일에 orchestrator.opencode_cli_path: <auto-detected path>
- 같은 파일에 llm.backend: opencode

> 호환성을 위해 .jsonc는 주석을 제거한 일반 JSON으로 다시 기록됩니다.

### 설정 파일 위치

| 설정 | 파일 |
|---|---|
| Ouroboros 런타임 설정(백엔드, CLI 경로) | ~/.ouroboros/config.yaml |
| OpenCode 공급자 / 모델 / MCP / 플러그인 | ~/.config/opencode/opencode.jsonc(또는 .json) |
| 브리지 플러그인 소스 | <opencode_config_dir>/plugins/ouroboros-bridge/ouroboros-bridge.ts |

OpenCode 기반 워크플로의 모델은 config.yaml이 아니라 OpenCode 자체에서 선택합니다.

## 경로 1 — 서브에이전트 브리지 플러그인 [권장]

플러그인은 OpenCode의 tool.execute.after 이벤트에 연결됩니다. Ouroboros MCP 도구가 _subagent / _subagents 봉투를 반환하면 다음과 같이 처리합니다.

1. 서브에이전트마다 독립적인 **자식 세션**을 만듭니다(client.session.create와 client.session.prompt 사용).
2. 상위 메시지에 subtask 부분을 넣어 자식이 원래 도구 호출 아래 기본 **Task 패널**로 인라인 표시되게 합니다.
3. 최대 MAX_FANOUT = 10개의 자식을 동시에 분기합니다. 각 자식은 페르소나 간 앵커링 편향이 없는 새로운 LLM 문맥을 씁니다.

여러 페르소나 예:

```
ouroboros_lateral_think persona="all"
  → hacker     (child session, Task pane)
  → researcher (child session, Task pane)
  → simplifier (child session, Task pane)
  → architect  (child session, Task pane)
  → contrarian (child session, Task pane)
```

### 환경 변수 조정

| 변수 | 기본값 | 용도 |
|---|---|---|
| OUROBOROS_CHILD_TIMEOUT_MS | 1200000 (20분) | 자식별 벽시계 시간 제한 |

> 브리지가 읽는 환경 설정은 OUROBOROS_CHILD_TIMEOUT_MS뿐입니다(opencode/plugin/ouroboros-bridge.ts:38). 재시도 횟수는 컴파일 시 고정되는 PATCH_RETRIES = 3, RESOLVE_RETRIES = 5(:40-41)이며 둘 다 재정의할 수 없습니다.

전체 안내는 **[OpenCode 서브에이전트 브리지](../guides/opencode-subagent-bridge.md)**를 참고하세요.

## 경로 2 — 하위 프로세스 런타임(대체 경로)

대화형 OpenCode 세션이 없는 헤드리스, CI 또는 스크립트 워크플로에서는 하위 프로세스 런타임을 명시적으로 선택합니다.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: opencode
  opencode_cli_path: /usr/local/bin/opencode   # omit if on PATH

llm:
  backend: opencode
```

명령별로 지정할 수도 있습니다.

```bash
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

OpenCodeRuntime 어댑터는 opencode run --format json --dangerously-skip-permissions를 하위 프로세스로 실행하고 stdin으로 프롬프트를 전달한 뒤 stdout의 구조화 JSON 이벤트 스트림을 파싱합니다. orchestrator.opencode_permission_mode의 기본값은 bypassPermissions이며 Seed 실행은 새 실행과 재개 실행 모두에서 이 모드를 강제합니다.

### 플러그인 대신 하위 프로세스를 선택할 상황

| 상황 | 경로 |
|---|---|
| 대화형 OpenCode에서 Task 패널 사용 | 플러그인 |
| 여러 페르소나 병렬 전달(lateral_think, qa) | 플러그인 |
| 연결된 세션이 없는 CI / 헤드리스 자동화 | 하위 프로세스 |
| 스크립트에서 워크플로 명령 호출 | 하위 프로세스 |
| 터미널 단발성 실행의 디버그/재현 | 하위 프로세스 |

### 플러그인 없이도 병렬 서브에이전트 분기가 가능한가요?

이론상 가능합니다. 오케스트레이터가 _subagents 봉투 항목마다 opencode run --format json 프로세스 N개를 병렬로 시작하고, 각 페르소나 프롬프트를 stdin으로 전달해 stdout JSON 이벤트 스트림을 모은 뒤 결과를 하나의 봉투로 합칠 수 있습니다.

구상:

```
parent = subprocess(opencode run --format json) ← seed prompt
         └─ hits MCP tool returning _subagents=[hacker, researcher, ...]
orchestrator
  ├─ subprocess(opencode run --format json) ← hacker prompt
  ├─ subprocess(opencode run --format json) ← researcher prompt
  └─ subprocess(opencode run --format json) ← simplifier prompt
         ↓ stdout JSON per child
      merge → parent envelope
```

이 방식을 제공하지 않는 이유:

| 고려 사항 | 플러그인 | 하위 프로세스 분기 |
|---|---|---|
| 상위 메시지 아래 인라인 Task 패널 표시 | 가능(상위 메시지에 subtask 부분 PATCH) | 불가 — 각 자식이 최상위 세션 |
| 세션 선택기 혼잡 | 상위 1개, 숨겨진 자식 N개 | 호출마다 보이는 세션 N+1개 |
| 상위 메시지 ID 아래 자식 배치 | 가능(session._client 직접 PATCH) | 플러그인 훅 없이 불가능 |
| 자식별 초기화 지연 | 프로세스 안에서 client.session.create 한 번 | 자식마다 CLI 부팅 및 TUI 초기화 수행 |
| 실행 중 진행 표시 | 가능(OpenCode 기본 렌더링) | 불가 — 병합 후에야 자식 출력 표시 |
| MCP/공급자 설정 상속 | 자동(같은 프로세스) | 프로세스마다 다시 해석 |
| 헤드리스/연결 세션 없음 | 불가(실행 세션 필요) | 가능 |

하위 프로세스 런타임은 단일 헤드리스 실행에 집중합니다. 병렬 분기는 플러그인 전용 기능입니다. 하위 프로세스로 흉내 낼 수는 있지만 OpenCode 세션이 연결된 경우 사용 경험이 더 나빠집니다.

## OpenCode에서 ooo 스킬 사용

setup을 실행하면 Ouroboros MCP 서버가 OpenCode 설정에 등록되고, OpenCode 세션에서 MCP 도구 전달로 ooo 스킬을 쓸 수 있습니다.

| 스킬 | OpenCode 세션 | CLI 대응 명령(터미널) |
|---|---|---|
| ooo interview | 가능 | ouroboros init start --llm-backend opencode "your idea" |
| ooo seed | 가능 | init start에 포함 |
| ooo run | 가능 | ouroboros run workflow --runtime opencode seed.yaml |
| ooo status | 가능 | ouroboros status execution <execution_id> |
| ooo evaluate / evolve | 가능 | MCP 전용 |
| ooo ralph | 가능 | MCP 소유 ouroboros_ralph. 하위 프로세스 모드는 작업 반환, 플러그인 모드는 자식 Task에 위임 |
| ooo cancel | 가능 | ouroboros cancel execution <execution_id> |
| ooo unstuck / tutorial / welcome | 가능 | MCP 전용 |
| ooo update | 가능 | pip install --upgrade ouroboros-ai |
| ooo help | 가능 | ouroboros --help |
| ooo qa | 가능 | ouroboros qa |
| ooo setup | 가능 | ouroboros setup --runtime opencode |
| ooo publish | 가능 | 직접 대응하는 하위 명령 없음. 스킬/런타임 흐름에서 gh CLI 사용 |

> **Ralph 참고 (#528):** ooo ralph는 이제 클라이언트가 evolve_step을 반복 조회해 다세대 루프를 재구현하지 않고 MCP 소유 ouroboros_ralph를 호출합니다. 하위 프로세스/플러그인 외 모드에서는 표준 백그라운드 job_id를 반환합니다. 작업 도구로 상태를 확인하고 ouroboros_cancel_job(job_id)로 취소합니다. 플러그인 모드에서는 job_id=None, status=delegated_to_plugin을 반환합니다. 브리지가 로컬 JobManager 작업을 만들지 않고 자식 Task 세션을 전달하므로 그 실행에는 로컬 Ralph 조회/취소 도구를 쓸 수 없습니다. ouroboros cancel execution <execution_id>은 실행 세션만 취소하며 Ralph 작업 ID에는 적용되지 않습니다.

> **ooo seed와 ooo interview 참고:** 역할이 다른 두 스킬입니다. ooo interview는 소크라테스식 질문을 진행하고 session_id를 반환합니다. ooo seed는 그 ID를 받아 모호도 점수가 포함된 구조화 Seed YAML을 생성합니다. 터미널에서는 ouroboros init start 한 번으로 두 단계를 실행합니다.

OpenCode는 정확한 ooo 및 /ouroboros: 스킬 전달에 공유 무상태 ouroboros.router 리졸버를 씁니다. 명령을 추가하거나 바꿀 때는 해당 SKILL.md frontmatter를 갱신하면 됩니다. 로깅, 메시지 조립, MCP 호출은 런타임이 로컬에서 처리합니다. [공유 스킬 전달 라우터](../guides/ooo-skill-dispatch-router.md)를 참고하세요.

## 빠른 시작

> 첫 실행 전체 흐름(interview -> seed -> execute)은 **[시작하기](../getting-started.md)**를 참고하세요.

### 설치 확인

```bash
opencode --version
ouroboros --help
```

### 첫 명령

첫 워크플로를 열기 전에 OpenCode 통합을 한 번 설정합니다.

```bash
ouroboros setup --runtime opencode
```

새 OpenCode 세션에서 다음을 실행합니다.

```
ooo interview "Build a task management CLI"
```

setup이 Ouroboros MCP 서버를 등록하면 ooo 명령을 쓸 수 있습니다.

## OpenCode의 장점

- **다중 공급자 지원** — 한 런타임에서 Anthropic, OpenAI, Google 등 사용
- **내장 공급자 관리** — 자체 인증/설정 관리, 환경 변수 설정 불필요
- **다양한 도구 접근** — 파일, 셸, 검색 도구 전체 사용(Claude Code와 같은 범위)
- **기본 MCP 통합** — MCP 서버를 기본 지원
- **오픈소스** — 전체 소스를 살펴보고 기여 가능
- **세션 인식 런타임** — 워크플로 단계 사이의 OpenCode 세션 핸들과 재개 상태 유지

모든 백엔드 비교는 [런타임 기능 매트릭스](../runtime-capability-matrix.md)를 참고하세요.

## 런타임 차이

OpenCode, Claude Code, Codex CLI는 도구, 권한 모델, 공급자 생태계가 다른 독립 백엔드입니다. 같은 Seed 파일을 모두에서 쓸 수 있지만 실행 경로는 달라질 수 있습니다.

| 항목 | OpenCode | Claude Code | Codex CLI |
|---|---|---|---|
| 소개 | OpenCode 하위 프로세스 기반 Ouroboros 세션 런타임 | Anthropic 에이전트형 코딩 도구 | Codex CLI 전송 기반 Ouroboros 세션 런타임 |
| 인증 | OpenCode가 관리 | Max Plan 구독 | OpenAI API 키 |
| 모델 | 설정 공급자가 지원하는 모델 | Claude (claude-agent-sdk) | 중간 추론 강도의 GPT-5.4 권장 |
| 도구 범위 | Read, Write, Edit, Bash, Glob, Grep | Read, Write, Edit, Bash, Glob, Grep | Codex 기본 파일/셸 도구 |
| 세션 | --session 및 런타임 핸들 | Claude 기본 세션 문맥 | 핸들, 재개 ID, 스킬 전달 |
| 전송 | 하위 프로세스, stdin 프롬프트 | Claude Agent SDK 직접 API | codex 실행 파일 하위 프로세스 |
| 비용 | 공급자 API 요금 | Max Plan에 포함 | OpenAI API 요금 |
| 테스트 플랫폼 | Linux | Linux, macOS | Linux, macOS |

> **참고:** Seed, 완료 기준, 평가 원칙 등 Ouroboros 워크플로 모델은 런타임마다 같습니다. 다만 기반 에이전트 기능, 도구, 공급자 생태계가 달라 같은 Seed에서도 경로와 결과가 달라질 수 있습니다.

## CLI 옵션

### 워크플로 명령

```bash
# Execute workflow (OpenCode runtime)
# Seeds generated by ouroboros init are saved to ~/.ouroboros/seeds/seed_{id}.yaml
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime opencode --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime opencode --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Seed 파일 참조

| 필드 | 필수 | 설명 |
|---|---|---|
| goal | 예 | 주요 목표 |
| task_type | 아니요 | 실행 전략: code(기본값), research, analysis |
| constraints | 아니요 | 충족해야 할 강제 제약 |
| acceptance_criteria | 아니요 | 구체적인 성공 기준 |
| ontology_schema | 예 | 출력 구조 정의 |
| evaluation_principles | 아니요 | 평가 원칙 |
| exit_conditions | 아니요 | 종료 조건 |
| metadata.ambiguity_score | 예 | 0.2 이하여야 함 |

## 알려진 한계

### 세션 누적(하위 프로세스 런타임만)

opencode run으로 작업을 실행할 때마다 OpenCode 세션 기록에 표시되는 세션이 생깁니다. 오케스트레이터 단계가 많은 장기 워크플로에서는 세션이 누적됩니다. 플러그인 경로에는 영향이 없습니다. 브리지가 만든 자식 세션은 Task 패널에 인라인 재배치되므로 선택기에 쌓이지 않습니다. 하위 프로세스 재배치는 [#331](https://github.com/Q00/ouroboros/issues/331)을 참고하세요.

### 플러그인 모드의 백그라운드 작업 도구는 실행 후 반환

하위 프로세스 모드에서 ouroboros_start_execute_seed와 ouroboros_start_evolve_step은 백그라운드 작업 API입니다. job_id를 반환하고 호출자는 ouroboros_job_status / ouroboros_job_result로 조회합니다.

**플러그인 모드**에서는 이 도구가 실행을 브리지에 위임하고 브리지가 호스트 안에 자식 세션을 엽니다. MCP 서버가 자식의 생명주기를 볼 수 없으므로:

- job_id는 None입니다(JobManager 기록을 만들지 않음).
- status는 running 또는 queued가 아니라 delegated_to_plugin입니다.
- None을 전달한 ouroboros_job_status / ouroboros_job_result는 쓸 수 있는 핸들이 아닙니다.

브리지는 자식 생성, Task 패널 진행 표시, 완료 신호 등 자체 생명주기를 관리합니다. 호출자는 status가 delegated_to_plugin인지 확인하고 폴링 대신 인라인 표시를 따르면 됩니다.

### 대화형 모드 없음

어댑터는 opencode run --format json 비대화형 실행을 사용합니다. 수동 승인 프롬프트 등 대화형 OpenCode 세션이 필요한 기능은 Ouroboros 실행 중 사용할 수 없습니다.

### 권한 모드

OpenCode에는 여러 값을 갖는 --permission-mode가 없지만 현재 릴리스는 --dangerously-skip-permissions를 제공합니다. Ouroboros는 새 실행과 --session 재개 모두에서 bypassPermissions를 해당 플래그로 변환합니다. 더 제한적인 저장 모드는 전체 우회 플래그를 추가하지 않습니다.

플러그인 모드에서 브리지는 위임하는 모든 자식 세션에 permission="*", pattern="*", action="allow" 권한 규칙을 명시합니다. 이는 하위 프로세스 우회 플래그에 해당하는 세션 API 설정입니다. 이미 전달한 자식에 영구 재접속할 수 없어 플러그인 재개는 지원하지 않습니다.

## 문제 해결

### OpenCode를 찾을 수 없음

opencode가 설치되어 PATH에서 실행되는지 확인합니다.

```bash
which opencode
```

설치되지 않았다면:

```bash
curl -fsSL https://opencode.ai/install | bash
```

### 공급자가 설정되지 않음

OpenCode에서 공급자 오류가 나면 최초 설정을 완료했는지 확인합니다.

```bash
opencode                        # interactive first-run setup
# or
opencode providers auth anthropic   # configure a specific provider
```

OpenCode가 인증 정보를 관리하므로 Ouroboros 통합에 ANTHROPIC_API_KEY 같은 환경 변수를 설정할 필요가 없습니다.

### 상태 검사에 "Providers: warning" 표시

오케스트레이터 런타임 백엔드 사용 시 정상입니다. 이 경고는 오케스트레이터 모드에서 사용하지 않는 LiteLLM 공급자를 가리킵니다.

### "EventStore not initialized"

데이터베이스는 ouroboros config show가 표시하는 활성 경로에 자동 생성됩니다.

## 비용

OpenCode를 런타임 백엔드로 쓰면 설정한 공급자의 API 요금이 발생합니다. 비용은 다음에 따라 달라집니다.

- OpenCode 설정에서 선택한 공급자와 모델
- 작업 복잡도 및 토큰 사용량
- 도구 호출과 반복 횟수

현재 요금은 공급자의 가격 페이지에서 확인하세요.

## 활성 Conductor 및 Synapse

OpenCode CLI 하위 프로세스 세션은 같은 OpenCode 세션 ID를 사용하는 검증된 Synapse inform / after_turn 전송입니다. 실시간 체크포인트 redirect 또는 강제 replace를 뜻하지 않습니다. OpenCode 플러그인의 Task 전달은 호스트가 별도로 소유하는 생명주기이며 런타임 중단으로 해석되지 않습니다.

조회 가능한 실행에서는 읽기 전용 observer 하나가 현재 모델/하네스, 효율 보증, 범위가 제한된 Discover 대상, 의존성/병렬 단계, 먼저 예약된 AC, 주의 사항, 종료 보증을 전달합니다. 주 세션은 계속 사용할 수 있습니다. 주 호스트는 사용자가 준 내부 ID가 아니라 의미를 기준으로 영향받는 AC를 선택하고 정본 영어 지침에 따라 사용자 대화 언어로 자연스럽게 응답합니다.
