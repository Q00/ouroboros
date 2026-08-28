# Handoff — MCP 활성 사용자 → Ouroboros 제품 사용 전환

> Last Updated: 2026-08-27
> Status: 구현 진행 중 (instructions → tool descriptions → host routing)

## Goal

MCP가 연결된 사용자에게 `ooo` 문법을 먼저 학습시키는 것이 아니라, 사용자의
자연어 요청이 요구사항 명확화·명세화·검증 실행에 적합할 때 호스트 모델이
Ouroboros 진입 툴을 선제적으로 선택하도록 만든다.

## Evidence

최근 30일 PostHog 실측:

- MCP service 사용자: 2,086
- 이후 `command_run` 도달 사용자: 807
- service → any command 전환율: 38.7%
- 첫 명령 상위: interview 303, brownfield 75, setup 64, qa 52,
  config 50, update 47, auto 43, run 39, pm 38

운영 명령도 섞여 있으므로 실제 product activation은 38.7%보다 낮다.
툴 설명만 개선하는 것으로는 부족하다. 툴 설명은 모델이 이미 툴을 발견한 뒤에만
작동한다. 가장 넓은 공통 개입 지점은 모든 MCP 호스트가 받는 server
`instructions`다.

## Decision

세 층을 함께 수정한다.

1. **Server instructions**: `render_mcp_server_instructions()` 첫 문단에
   `WHEN TO USE OUROBOROS` 라우팅 규칙을 추가한다.
2. **Entry-tool descriptions**: interview, start_auto, start_execute_seed를
   `Use when / Result / Do not use when` 구조로 고친다.
3. **Host natural-language routing**: 명시적 `ooo` 문자열 없이도 모호한 요청,
   다단계 기능, 마이그레이션, 고위험·검증 요청을 적절한 진입 툴에 연결한다.

기본 라우팅:

```text
요구사항이 모호하거나 AC가 없음      → ouroboros_interview
큰 작업을 명확화부터 실행까지 요청    → ouroboros_start_auto
이미 Seed가 있음                     → ouroboros_start_execute_seed
```

## Implementation Plan

### 1. MCP 공통 instructions를 outcome-first로 재배치

대상: `src/ouroboros/backends/capabilities.py::render_mcp_server_instructions`

추가할 핵심 의미:

```text
WHEN TO USE OUROBOROS

Use proactively before implementation when a request is ambiguous, lacks
acceptance criteria, spans multiple implementation steps, is a migration, or
requires high-confidence execution and verification.

Default entry points:
- unclear request → ouroboros_interview
- substantial end-to-end task → ouroboros_start_auto
- existing Seed → ouroboros_start_execute_seed

Do not wait for the literal word "ooo" when the natural-language request
clearly matches these cases.
```

제약:

- 전체 UTF-8 크기 2,048 bytes 미만 유지
- `WHEN TO USE`를 tool discovery와 fan-out보다 앞에 둔다.
- 런타임별 구체 툴명(`ToolSearch`, `Task/Agent`)은 넣지 않는다.
- 범용 단일 파일 수정·단순 질문에는 Ouroboros를 강제하지 않는다.

### 2. 진입 툴 설명 세 개 개선

대상:

- `src/ouroboros/mcp/tools/authoring_handlers.py`
  - `ouroboros_interview`
- `src/ouroboros/mcp/tools/auto_handler.py`
  - `ouroboros_start_auto`
- `src/ouroboros/mcp/tools/execution_handlers.py`
  - `ouroboros_start_execute_seed`

설명 계약:

- interview: 구현 전, 모호함·범위·AC 부족 시 사용. 닫힌 요구사항 상태를 만든다.
- start_auto: 다단계·마이그레이션·고위험 end-to-end 작업의 기본 진입점.
  interview → A-grade Seed → execution handoff를 수행한다.
- start_execute_seed: 기존 Seed가 있을 때만 사용. raw/모호 요청에는 사용하지 않는다.

내부 아키텍처, polling 방법, 장황한 파라미터 설명은 진입 설명에서 제외하고 기존
파라미터 schema와 응답 계약에 남긴다.

### 3. Host routing 문구 정렬

대상:

- `src/ouroboros/codex/ouroboros.md`
- 필요 시 런타임별 생성 가이드/패키지 산출물

현재 자연어 매핑은 "clarify requirements", "generate seed", "run seed"처럼
Ouroboros 용어를 이미 아는 사용자 표현에 치우쳐 있다. 다음 일반 표현을 추가한다.

- "이 기능 만들어줘", "끝까지 검증해서 구현해줘" → start_auto 후보
- "요구사항이 애매해", "범위부터 잡자" → interview
- "이 Seed 실행해" → start_execute_seed

호스트는 사용자의 의도와 현재 artifact 유무를 확인해 라우팅하며, 자연어 요청을
무조건 auto로 보내지 않는다.

### 4. Activation analytics 추가

PostHog 대시보드 `1985514`에 다음 Insight를 추가한다.

- Service → product command conversion
- First product command distribution
- Interview → Seed conversion
- Auto/Run → workflow_outcome conversion
- app_version별 product activation
- runtime_backend별 product activation

Product command 집합:

```text
interview, auto, run, pm, qa, seed, evaluate
```

`setup`, `config`, `update`, `status`, `brownfield`는 사용량에는 포함할 수 있지만
product activation 성공으로 계산하지 않는다.

## Acceptance Criteria

1. `render_mcp_server_instructions()` 결과가 2,048 bytes 미만이다.
2. instructions 첫 섹션이 `WHEN TO USE OUROBOROS`이며 interview/auto/run의
   구분과 literal `ooo`를 기다리지 말라는 규칙을 포함한다.
3. instructions는 provider-neutral하며 `ToolSearch`, `Task/Agent`를 포함하지 않는다.
4. 세 진입 툴 설명이 각각 Use when, Result, Do not use when 의미를 포함한다.
5. 모호한 자연어 요청은 interview, 큰 end-to-end 요청은 start_auto,
   Seed 제공 요청은 start_execute_seed로 매핑되는 계약 테스트가 있다.
6. 단순 질문·작은 명확한 수정은 자동으로 Ouroboros에 라우팅하지 않는다.
7. PostHog에 service → product command 및 첫 product command Insight가 생성되고
   쿼리가 정상 실행된다.
8. 릴리즈 전 7일과 릴리즈 후 7일의 전환율을 app_version 기준으로 비교할 수 있다.

## Verification

```bash
uv run pytest tests/unit/backends/test_capabilities.py -q
uv run pytest \
  tests/unit/mcp/tools/test_interview.py \
  tests/unit/mcp/tools/test_auto_handler.py \
  tests/unit/mcp/tools/test_execution_handlers.py -q
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
```

실제 테스트 경로가 다르면 기존 handler별 테스트 파일을 사용한다. 새 테스트 전용
파일을 만들기 전에 현재 description/definition 테스트 위치를 먼저 찾는다.

## Important Files

```text
src/ouroboros/backends/capabilities.py
tests/unit/backends/test_capabilities.py
src/ouroboros/mcp/tools/authoring_handlers.py
src/ouroboros/mcp/tools/auto_handler.py
src/ouroboros/mcp/tools/execution_handlers.py
src/ouroboros/codex/ouroboros.md
src/ouroboros/mcp/server/adapter.py
```

## Risks / Non-goals

- MCP 연결만으로 매 세션 팝업·홍보 문구를 띄우지 않는다.
- 모든 사용자 요청을 auto로 보내지 않는다.
- 툴 설명을 길게 만들어 context budget을 소비하지 않는다.
- activation 개선을 증명하기 전에 전환율 상승을 주장하지 않는다.
- 기존 텔레메트리 이벤트 계약을 다시 넓히지 않는다. 필요한 분석은 현재
  `service_active`, `command_run`, `workflow_outcome`, app_version,
  runtime_backend를 사용한다.

## Next Session Start

1. 최신 `main`에서 새 브랜치 생성.
2. 관련 GitHub issue 생성 후 PR에 연결.
3. server instructions byte budget 테스트를 먼저 강화.
4. instructions → tool descriptions → host routing 순서로 구현.
5. 코드 배포 후 PostHog Insight 생성·검증.
6. 7일 뒤 app_version cohort 전환율 비교.
