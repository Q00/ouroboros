# Incident Postmortem: MCP Empty Response — Gen 2/3 Skipped in HOTL Evolution

**Date**: 2026-02-28
**Severity**: SEV2 (Partial Degradation)
**Status**: Active (Recurring pattern)
**Session**: `houseops_new`
**Analysis**: 5-agent multi-perspective (Timeline, Root Cause, Systems, Impact, Devil's Advocate)

---

## Executive Summary

HOTL(Human On The Loop) 모드에서 `ouroboros_evolve_step` MCP 도구 호출 시, Gen 2와 Gen 3이 빈 응답으로 인해 실패했으나 **실패 이벤트가 기록되지 않아** Gen 4에서 ontology 변경 없이 similarity 1.000으로 조기 수렴했다.

**근본 원인**: 두 가지 버그의 조합
1. **Event emission gap**: `evolve_step()`이 `_run_generation()` 에러 시 `generation.failed` 이벤트를 emit하지 않음 (`loop.py:493-514`)
2. **Nested session concurrency**: ClaudeCodeAdapter가 Max Plan 쿼터를 공유하는 중첩 Claude Code 세션을 제한 없이 생성하여 빈 응답 유발

---

## Timeline

| Time | Event | Duration | Status |
|------|-------|----------|--------|
| 12:58:21 | Lineage created + Gen 1 started | — | Executing |
| 15:46:54 | Gen 1 completed | **2h 48m** | seed_068a5d9f1a8d |
| 15:46 → 18:10 | *Idle gap (Ralph scheduling)* | **2h 23m** | — |
| 18:10:01 | Gen 2 started (wondering) | — | **NO completion** |
| 18:12:18 | Gen 3 started (wondering) | **~2m 17s** | **NO completion** |
| 18:12:58 | Gen 4 started (wondering) | **~40s** | — |
| 18:29:41 | Gen 4 completed + CONVERGED | **16m 43s** | similarity 1.000 |

**Critical**: Gen 2, 3에는 `lineage.generation.started` 이벤트만 있고 `completed`도 `failed`도 없음.

---

## Root Cause Analysis

### Primary Bug: Missing `generation.failed` event in `evolve_step()`

**File**: `loop.py:493-514`

```python
# BUG: gen_result.is_err 시 이벤트 미발행
if gen_result.is_err:
    failed_gen = GenerationResult(...)  # StepResult 생성
    signal = ConvergenceSignal(...)
    return Result.ok(StepResult(       # FAILED action 반환
        ..., action=StepAction.FAILED,
    ))
    # ← generation.failed 이벤트 emit 누락!
```

비교: Timeout 경로(`loop.py:461-469`)는 `lineage_generation_failed()`를 정상 emit함.

### Secondary: CancelledError로 인한 이벤트 손실

MCP transport timeout이나 client disconnect로 `asyncio.CancelledError` 발생 시, `_run_generation()` 내부의 어떤 failure event emission point도 실행되지 않고 전체 task가 취소됨. `generation.started` 이벤트만 남고 `failed`/`completed`가 영원히 기록되지 않음.

### Tertiary: Nested Claude Code Session 경합

```
Ralph (Claude Code Max) → MCP ouroboros → ClaudeCodeAdapter → Claude Agent SDK → subprocess
```

- Wonder 단계: `ClaudeCodeAdapter.complete()` → SDK `query()` → 새 Claude Code 프로세스 생성
- Reflect 단계: 동일하게 새 프로세스 생성
- Execute 단계: `ClaudeAgentAdapter` → 또 다른 프로세스 생성
- 모두 **동일한 Max Plan 쿼터**를 공유

ClaudeCodeAdapter의 retry 로직(5회, 지수 백오프 2→4→8→16→32초 ≈ 62초)이 Gen 2의 ~2분 실패 시간과 정확히 일치.

---

## Failure Chain (Confirmed)

```
1. evolve_step() 호출
2. _run_generation() 시작
3. lineage.generation.started 이벤트 EMIT (loop.py:631)
4. Wonder 단계: ClaudeCodeAdapter.complete() 호출
   → SDK query() 빈 응답 → 5회 retry 실패 → ProviderError 반환
   → WonderEngine 내부 degraded fallback (wonder.py:97-102)
   → wonder_output = WonderOutput(should_continue=True, ...)
5. Reflect 단계: ClaudeCodeAdapter.complete() 호출
   → 동일한 빈 응답/경합 → retry 실패 → ProviderError 반환
   → ReflectEngine은 fallback 없음 → Result.err() 반환 (reflect.py:119-123)
   → generation.failed 이벤트 EMIT (loop.py:681)
   → _run_generation() returns Result.err()
6. [또는] MCP transport가 timeout으로 task 취소
   → CancelledError 전파 → 모든 이벤트 emission 건너뜀
7. evolve_step() line 493: gen_result.is_err → FAILED StepResult 반환
   → generation.failed 이벤트 미발행 ← BUG
8. MCP 도구가 FAILED 결과 또는 빈 응답 반환
9. Ralph가 다음 evolve_step 호출 → Gen 3 시작 (동일 패턴 반복)
```

---

## Impact Assessment

| Metric | Value |
|--------|-------|
| 손실된 진화 사이클 | 2회 (Gen 2 + Gen 3) |
| 손실된 ontology mutation | ~4-10개 추정 |
| 총 세션 시간 | ~5h 31m |
| 유용한 작업 시간 | ~2h 49m (Gen 1만) |
| 낭비 비율 | **~49%** |
| 수렴 유효성 | **FALSE** — 1.000 = 진화 없음, 안정성 아님 |

### False Convergence 메커니즘

Gen 4는 Gen 1과 **동일한 seed**를 사용하여 실행 (Wonder/Reflect 실패로 새 seed 미생성). `OntologyDelta.compute(Gen1_ontology, Gen4_ontology) = 1.000` → `ConvergenceCriteria`가 `min_generations=2` (Gen 1 + Gen 4 = 2 completed) 충족으로 수렴 선언.

이것은 "진화를 통한 수렴"이 아닌 **"실패를 통한 수렴"**.

---

## Architectural Weaknesses (Systems Analysis)

### Critical

1. **stdout SPOF**: MCP stdio 채널에 어떤 비프로토콜 출력이라도 전체 채널 파괴. 복구 불가.
2. **무제한 동시 세션**: `ParallelACExecutor`가 AC당 1개 세션 생성, 동시성 제한 없음. 6 ACs × 5 Sub-ACs = 최대 30개 동시 세션 가능.
3. **글로벌 rate limiter 부재**: 각 어댑터가 독립적으로 retry → thundering herd 발생.

### High

4. **이벤트 원자성 부재**: `generation.completed` + `ontology_evolved` + convergence 이벤트가 순차 emit. 중간 crash 시 비일관 상태.
5. **서브프로세스 추적/정리 없음**: CLI 서브프로세스 PID 미추적, 좀비 프로세스 가능.
6. **evaluation 실패 무시**: `loop.py:792-803`에서 평가 실패 시 warning만 기록하고 세대를 "성공"으로 처리.

---

## Devil's Advocate Findings (Caveats)

DA가 제기한 유효한 지적사항:

1. **`eval_gate_enabled=False`** (기본값): 평가를 통과하지 못해도 수렴 가능. 이 설정이 `True`였다면 false convergence를 방지했을 수 있음.
2. **`min_generations=2`가 너무 낮음**: 2세대만으로 수렴을 허용하면 탐색 공간이 부족.
3. **`_safe_query()` 예외 처리 범위**: `MessageParseError`만 catch하고 다른 예외는 전파. 예상치 못한 SDK 예외가 빈 응답을 유발할 수 있음.
4. **`OntologyDelta` 유사도 편향**: 필드 이름에 50% 가중치 → 설명만 변경하는 진화를 과소평가.

**기각된 DA 가설**: "Wonder가 `should_continue=false`를 반환"
→ 이 경로는 `Result.ok(GenerationResult(phase=COMPLETED))`를 반환하고 `generation.completed` 이벤트가 emit됨. Gen 2/3에 `completed` 이벤트가 없으므로 이 가설은 DB 증거와 모순.

---

## Recommendations

### P0: 즉시 수정 (이 인시던트의 직접적 원인)

| # | 항목 | 파일 | 라인 |
|---|------|------|------|
| 1 | `evolve_step()` 에러 경로에 `generation.failed` 이벤트 추가 | `loop.py` | 493 |
| 2 | `_run_generation()`에 `try/finally` 추가하여 CancelledError 시에도 failed 이벤트 보장 | `loop.py` | 609 |
| 3 | `_run_generation()` 내 execution 에러 경로(`is_ok` False)에 failed 이벤트 추가 | `loop.py` | 750-751 |

### P1: 재발 방지 (아키텍처 개선)

| # | 항목 | 설명 |
|---|------|------|
| 4 | 글로벌 concurrency semaphore | `create_ouroboros_server`에서 생성, 모든 어댑터에 주입. `asyncio.Semaphore(N)` |
| 5 | False convergence 감지 | similarity=1.000 + Wonder/Reflect 미성공 시 "false convergence"로 표시 |
| 6 | `eval_gate_enabled=True` 기본값 변경 | 평가 미통과 시 수렴 차단 |
| 7 | `min_generations` 상향 | 최소 3세대 (성공적 Wonder→Reflect 포함) |
| 8 | `append_batch()` 사용 | generation completion 이벤트를 원자적으로 기록 |

### P2: 장기 안정성

| # | 항목 | 설명 |
|---|------|------|
| 9 | Circuit breaker 패턴 | N회 연속 실패 시 새 세션 생성 중단 |
| 10 | 서브프로세스 추적/정리 | PID 추적 + atexit/signal handler |
| 11 | SSE transport 옵션 | 장시간 작업에 stdio 대신 SSE 사용 |
| 12 | Health check endpoint | MCP 서버 생존 감지용 ping/pong |

---

## Action Items

- [ ] P0-1: `evolve_step()` line 493에 `generation.failed` 이벤트 emit 추가
- [ ] P0-2: `_run_generation()`에 `try/finally` guard 추가
- [ ] P0-3: execution 에러 경로 이벤트 emit 추가
- [ ] P1-4: 글로벌 concurrency semaphore 구현
- [ ] P1-5: False convergence 감지 로직 추가
- [ ] `houseops_new` lineage를 Gen 1으로 rewind하여 정상 진화 재시도

---

*Analyzed by 5-agent incident team (Timeline, Root Cause, Systems, Impact, Devil's Advocate)*
*Co-Authored-By: Claude Opus 4.6*
