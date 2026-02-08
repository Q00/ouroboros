# 병렬 실행 비교: Claude Code Teammates vs Ouroboros Sub-AC

> 생성일: 2026-02-06
> 맥락: P0-P3 Parallel DX 구현을 Claude Code teammate 팀으로 실행

---

## 요약

Claude Code의 teammate 기반 병렬 실행과 Ouroboros의 Sub-AC 병렬 실행은 **동일한 Claude SDK session**을 사용하지만, **오케스트레이션의 지능 배치**에서 근본적으로 다르다.

> **중요**: Claude Code teammate 모델에서 "Team Lead"는 메인 CLI의 Claude 세션이지, **사람이 아니다**. 두 시스템 모두 완전한 AI 기반이며, 차이는 아키텍처적인 것이다.

### Claude Code Teammate 아키텍처

각 teammate는 **독립된 Claude Code 세션**으로 다음을 받는다:

| 받는 것 | 상세 |
|---------|------|
| `CLAUDE.md` | 프로젝트 레벨 지시 (페르소나, 규칙, 워크플로우) |
| 프로젝트 메모리 | `~/.claude/projects/.../memory/MEMORY.md` |
| MCP 서버 | 연결된 모든 MCP 도구 (Tavily, context7 등) |
| Skills | 설치된 모든 스킬 |
| Spawn prompt | 리드 세션이 작성한 태스크 지시 |
| 팀 도구 | SendMessage, TaskList, TaskUpdate (협업용) |

teammate가 **받지 않는 것**:
- 리드 세션의 대화 히스토리
- 리드 세션의 시스템 프롬프트 (CLAUDE.md에서 자체 로드)
- 다른 teammate의 컨텍스트나 대화 상태
- 커스텀 시스템 프롬프트 (spawn 시 설정 불가)

---

## 1. Ouroboros 아키텍처 (실제)

```
Seed Goal
  |
  +-- DependencyAnalyzer (Claude, temp=0.0)
  |   "AC간 의존성 분석"
  |   -> DAG: ((0,2), (1,), (3))   <- Kahn's algorithm
  |
  +-- Level 0: AC-0, AC-2 병렬 실행
  |   |
  |   +-- AC-0 -> _try_decompose_ac() (Claude)
  |   |         "atomic인지 쪼갤지?"
  |   |         -> "ATOMIC" -> 바로 실행
  |   |
  |   +-- AC-2 -> _try_decompose_ac() (Claude)
  |             -> ["Sub1: login endpoint", "Sub2: signup endpoint"]
  |             -> Sub-ACs 병렬 실행 (anyio task group)
  |
  +-- Level 1: AC-1 실행 (AC-0 완료 후)
  |
  +-- Level 2: AC-3 실행 (AC-1, AC-2 완료 후)
```

### AC당 3회 AI 호출 (모두 Claude SDK)

병렬 실행 파이프라인 (`runner.py` → `ParallelACExecutor`)은 **3회의 AI 호출**을 하며, 모두 `ClaudeCodeAdapter` (Claude SDK) 경유:

| AI 호출 | LLM | 도구 | 역할 |
|---------|-----|------|------|
| 1. 의존성 분석 | ClaudeCodeAdapter (Sonnet, temp=0.0) | 없음 (분석만) | `DependencyAnalyzer`로 AC간 의존성 DAG 생성 |
| 2. AC별 분해 | ClaudeCodeAdapter (어댑터 기본값) | 없음 (tools=[]) | 각 AC: "ATOMIC" 또는 2-5개 Sub-AC로 분해 (`_try_decompose_ac()`) |
| 3. AC/Sub-AC 실행 | ClaudeCodeAdapter | Read, Edit, Bash, Glob, Grep | 실제 태스크 실행 — 코드 탐색, 파일 수정 |

```
AI 호출 1 (1회):     DependencyAnalyzer  → 실행 레벨이 포함된 DAG
AI 호출 2 (AC당):    _try_decompose_ac() → "ATOMIC" 또는 [Sub-AC 목록]
AI 호출 3 (AC당):    _execute_atomic_ac() → 실제 코드 작업
```

> **참고:** 별도의 `double_diamond.py` 파이프라인 (순차 실행용)은 호출 2를 정식 **Atomicity Check** (Gemini Flash, temp=0.3) + **Decomposition** (Claude Sonnet, temp=0.5) 두 개의 호출로 대체한다. 병렬 실행기는 이를 단일 프롬프트로 통합.

---

## 2. 동일한 점

| | Claude Code Teammate | Ouroboros Sub-AC |
|---|---|---|
| 실행 단위 | Claude SDK session | Claude SDK session |
| 도구 | Read, Edit, Bash, Glob, Grep | Read, Edit, Bash, Glob, Grep |
| 코드 이해 | 직접 읽고 수정 | 직접 읽고 수정 |
| 파일시스템 | 공유 (같은 cwd) | 공유 (같은 cwd) |
| 병렬화 | Task tool (background) | anyio task group |

---

## 3. 핵심 차이: 오케스트레이션의 지능 배치

```
                    Claude Code (teammate)      Ouroboros
                    ---------------------       ---------

  계획              리드 Claude 세션이             3회 AI 호출 (모두 Claude SDK):
                    코드를 읽고 분석                1. DependencyAnalyzer (DAG)
                    -> 4개 태스크 생성              2. AC별 분해
                    -> 의존성 선언                  3. AC별 실행

  지시 정밀도        "이 파일 350번줄의             "CLI 콘솔에 tool detail
                    break 제거하고                 표시하도록 개선해"
                    이 코드로 교체해"

  에이전트 자율성    낮음 (타이핑 수준)             높음 (탐색->판단->구현)

  에이전트간 인식    리드 세션이 중계               완전 격리
                    "P0a 끝났으니 P0b 시작"        서로의 존재를 모름

  실패 전파          수동 (리드 세션이 판단)        자동 (Cascade Failure)

  리드 정체          Claude 세션 (메인 CLI)        ParallelACExecutor (코드)
```

---

## 4. Ouroboros가 우월한 지점

### 4.1 분해 시 독립성 원칙

병렬 실행기의 `_try_decompose_ac()` 프롬프트:

```
"Each Sub-AC should be:
 - Independently executable
 - Specific and focused
 - Part of achieving the parent AC"
```

분해 시점에 AI가 **독립적으로 실행 가능한** Sub-AC를 만들도록 지시 -> 파일 기반 락 불필요.

> **참고:** 별도의 `double_diamond.py` 파이프라인은 더 강력한 **MECE 원칙** ("Mutually Exclusive, Collectively Exhaustive — children should not overlap and should cover the full scope")을 사용. 병렬 실행기의 프롬프트는 더 간결하지만 "independently executable"로 유사한 분리를 달성.

### 4.2 자동 Atomicity 판단

병렬 실행기에서 Claude가 인라인으로 판단:

```
"If the AC is simple/atomic (can be done in one focused task), respond with: ATOMIC
 If this AC is complex (requires multiple distinct steps that could run independently),
 decompose it into 2-5 smaller Sub-ACs."
```

Claude Code에서는 리드 세션이 코드를 분석해서 판단 ("dashboard_v3.py가 복잡하니까 두 agent로 나누자"). Ouroboros는 이를 자동화 — 각 AC를 실행 전에 Claude가 개별 평가.

### 4.3 의존성 Cascade Failure

```
AC-0 실패 -> AC-1 (depends on 0) 자동 스킵 -> AC-3 (depends on 1) 자동 스킵
```

Teammate 모델에는 이런 자동 전파가 없음. 리드 세션이 수동으로 "P0a 실패했으니 P0b 취소"를 결정해야 함.

### 4.4 반복 가능성 & 확장성

- 사용자는 Seed Goal만 제공; 전체 파이프라인이 자동 실행
- 리드 세션 병목 없음 (Ouroboros 오케스트레이터는 결정적 코드, LLM 세션이 아님)
- 같은 seed로 일관된 실행 계획 생성 (의존성 분석 temp=0.0)

---

## 5. Claude Code Teammate가 우월한 지점

### 5.1 실시간 에이전트간 통신

Ouroboros Sub-AC들은 **서로의 존재조차 모름**. 시스템 프롬프트에 "다른 agent가 병렬로 돌고 있다"는 말도 없음. Claude Code에서는 리드 세션이 teammate에게 DM을 보내고, 중간 결과를 전달하고, 실행 순서를 동적으로 조정 가능. Teammate끼리도 직접 메시지 가능.

### 5.2 같은 파일 내 영역 분할

p2-tree와 p2-detail이 같은 `dashboard_v3.py`를 동시에 수정할 수 있었던 건, 리드 세션이 spawn prompt에서 각 agent의 경계를 명확히 했기 때문: "너는 NodeDetailPanel 클래스만 건드려". Ouroboros는 이런 세밀한 파일 내 영역 분할을 못함.

### 5.3 정밀 지시로 인한 탐색 비용 절감

```
Claude Code Teammate: 정확한 코드 제공 -> 즉시 편집 (토큰 최소)
Ouroboros Sub-AC:     추상적 목표 -> 코드 탐색 -> 판단 -> 구현 (토큰 소모)
```

---

## 6. 결론

| 차원 | Claude Code Teammate | Ouroboros |
|------|---------------------|-----------|
| 단일 실행 정확도 | 높음 (정밀 지시) | 중간 (AI 판단 의존) |
| 확장성 | 낮음 (리드 세션 병목) | 높음 (완전 자동화) |
| 반복 가능성 | 낮음 (리드가 매번 재분석) | 높음 (Seed만 필요) |
| 파일 충돌 위험 | 낮음 (수동 회피) | 낮음 (독립성 원칙 분해) |
| 비용 효율 | 높음 (탐색 최소) | 중간 (탐색 오버헤드) |
| 에이전트 자율성 | 낮음 | 높음 |

**Ouroboros의 자동화된 파이프라인이 반복 가능하고 확장 가능한 해답.** Claude Code teammate 방식은 일회성 정밀 구현에 적합하지만, 리드 Claude 세션이 병목 — 모든 코드를 읽고, 모든 태스크를 설계하고, 각 teammate에 정확한 지시를 작성해야 함. 두 시스템 모두 완전한 AI 기반이며, 차이는 Ouroboros가 계획 지능을 3회의 특화된 AI 호출 (의존성 분석 → 분해 → 실행)로 분산하는 반면, Claude Code는 단일 리드 세션에 집중한다는 것.

---

## 7. Ouroboros 개선 가능 방향

### 단기 (Prompt Engineering)

- **Sub-AC 인식**: Sub-agent 시스템 프롬프트에 병렬 실행 맥락 추가 ("다른 agent가 형제 AC를 동시에 작업 중")
- **파일 경계 힌트**: 분해 프롬프트에 "각 자식은 별도 파일 또는 공유 파일 내 별도 섹션을 대상으로" 추가
- **컨텍스트 주입**: 완료된 AC 결과를 다음 레벨 AC에 컨텍스트로 전달

### 중기 (Architecture)

- **Coordinator agent**: 레벨 간 경량 에이전트가 Level N 결과를 리뷰한 후 Level N+1 디스패치
- **파일 영향 예측**: 각 AC가 어떤 파일을 건드릴지 사전 분석, 겹치면 직렬화
- **AC간 메시징**: Sub-AC가 발견 사항을 브로드캐스트 (예: "auth/models.py 생성함")

### 장기 (수렴)

두 접근법이 수렴할 수 있다: Ouroboros가 **적응형 지시 정밀도**를 채택 — 코드베이스를 잘 파악했을 때는 더 구체적인 지시를 생성하고, 새로운 도메인에서는 자율 탐색으로 폴백.
