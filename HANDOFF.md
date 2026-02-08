# Handoff Document

> Last Updated: 2026-02-06
> Session: Parallel DX — Phase A/B Complete + Coordinator Agent Design

---

## Goal

Ouroboros parallel execution 파이프라인 강화:
1. **Phase A**: Inter-level context passing (레벨 간 컨텍스트 전달) ✅
2. **Phase B**: ExecutionStrategy pattern (task_type별 전략 분화) ✅
3. **Review 이슈 수정**: Dead code wiring, 버그 수정, 미구현 피쳐 ✅
4. **Coordinator Agent**: 레벨 간 지능형 검토 게이트 ← **다음 세션에서 구현**

---

## Current Progress

### ✅ Phase A: Inter-level Context Passing — COMPLETE

| File | Status |
|------|--------|
| `src/ouroboros/orchestrator/level_context.py` | Created (ACContextSummary, LevelContext, extract_level_context, build_context_prompt) |
| `src/ouroboros/orchestrator/parallel_executor.py` | Modified (level_contexts 축적 + prompt 주입) |
| `tests/unit/orchestrator/test_level_context.py` | Created (18 tests) |

### ✅ Phase B: ExecutionStrategy Pattern — COMPLETE

| File | Status |
|------|--------|
| `src/ouroboros/orchestrator/execution_strategy.py` | Created (Protocol + Code/Research/Analysis strategies) |
| `src/ouroboros/core/seed.py` | Modified (task_type field added) |
| `src/ouroboros/orchestrator/runner.py` | Modified (strategy-based prompt/tools) |
| `src/ouroboros/orchestrator/workflow_state.py` | Modified (activity_map parameterization) |
| `tests/unit/orchestrator/test_execution_strategy.py` | Created (28 tests) |

### ✅ Review Issue Fixes — COMPLETE (8 items)

| Issue | Fix |
|-------|-----|
| Sub-AC level_contexts 미전달 버그 | `_execute_sub_acs_parallel()`에 `level_contexts` param 추가 |
| `strategy.get_tools()` dead code | `_get_merged_tools()`에 strategy param 추가, strategy.get_tools()를 base로 사용 |
| `strategy.get_task_prompt_suffix()` dead code | `build_task_prompt()`가 strategy suffix 사용하도록 변경 |
| `strategy.get_activity_map()` dead code | `WorkflowStateTracker`에 `activity_map` param 추가 |
| `__init__.py` export 누락 | level_context + execution_strategy 심볼 export 추가 |
| Sub-AC awareness (Section 7 #1) | `_execute_atomic_ac` 프롬프트에 Parallel Execution Notice 주입 |
| File boundary hints (Section 7 #2) | `_try_decompose_ac` 프롬프트에 파일 경계 분리 가이드 추가 |
| 전체 테스트 | 194/194 passed |

### 🔲 Coordinator Agent (#4) — DESIGN COMPLETE, IMPLEMENTATION PENDING

**설계 문서**: `docs/api/coordinator-agent-design.md`

**확정된 스펙**:
- **충돌 처리**: Auto-resolve via Claude (Edit/Bash 도구 사용)
- **에이전트 수준**: Full agent (Read, Bash, Edit, Grep, Glob)
- **기존 코드 관계**: Enhance (extract_level_context 유지, 위에 enriched context 추가)
- **#5 File Impact Prediction**: 드롭 (bias 문제)
- **#6 Inter-AC Messaging**: #4에 흡수 (인메모리 ACExecutionResult 데이터 활용)

**아키텍처 선택 필요**:
- **A: Pragmatic (권장)** — Python으로 충돌 감지 → 충돌 있을 때만 Claude 세션
- **B: Full Agent** — 매 레벨마다 Claude 세션 (품질 검증까지)
- 권장: A로 시작, B는 `--coordinator-mode=always` 플래그로 후속 추가

---

## What Worked

1. **Agent team 리뷰**: 3명의 code-reviewer agent를 병렬로 실행해 dead code, 버그, 미구현 피쳐를 정확하게 발견
2. **인메모리 데이터 활용**: EventStore 쿼리 불필요 — `ACExecutionResult.messages`에 이미 모든 tool event 정보 있음
3. **단기 개선 3개 시너지**: #3(분해 품질) → #2(실행 충돌 방지) → #1(다음 레벨 컨텍스트) 파이프라인 강화

## What Didn't Work / Dropped

1. **#5 File Impact Prediction 드롭**: LLM이 다른 LLM의 파일 접근을 예측하는 건 bias 유발. 예측 오류 시 불필요한 직렬화 또는 해결 공간 축소
2. **EventStore 직접 쿼리 방식**: 탐색 결과 불필요 판명. 인메모리 데이터가 이미 충분

---

## Next Steps

### 1. Coordinator Agent 구현 (아키텍처 A 기준)

```
1. CREATE  src/ouroboros/orchestrator/coordinator.py
   - FileConflict, CoordinatorReview dataclasses
   - LevelCoordinator class
     - detect_file_conflicts(level_results) → list[FileConflict]  # Pure Python
     - run_review(conflicts, adapter, ...) → CoordinatorReview     # Claude session

2. MODIFY  src/ouroboros/orchestrator/level_context.py
   - LevelContext에 coordinator_review: CoordinatorReview | None 필드 추가
   - build_context_prompt()에서 coordinator_review 있으면 경고/권고 섹션 추가

3. MODIFY  src/ouroboros/orchestrator/parallel_executor.py
   - execute_parallel() 레벨 루프 lines 416-423에 coordinator 삽입
   - _detect_file_conflicts() 호출 → 충돌 시 coordinator.run_review()
   - CoordinatorReview를 LevelContext에 attach

4. MODIFY  src/ouroboros/orchestrator/__init__.py
   - 새 심볼 export

5. CREATE  tests/unit/orchestrator/test_coordinator.py
   - detect_file_conflicts() 단위 테스트
   - CoordinatorReview 데이터 모델 테스트
   - build_context_prompt()에 review 포함 시 프롬프트 생성 테스트
```

### 2. 삽입 지점 코드 (현재 상태)

```python
# parallel_executor.py, execute_parallel() 내부, lines ~416-423:
# Extract context from this level for next level's ACs
if level_success > 0:
    level_ac_data = [
        (r.ac_index, r.ac_content, r.success, r.messages, r.final_message)
        for r in all_results
        if isinstance(r, ACExecutionResult) and r.ac_index in executable
    ]
    level_ctx = extract_level_context(level_ac_data, level_num)
    level_contexts.append(level_ctx)
```

→ 여기에 coordinator 호출을 삽입해야 함.

---

## Important Files

### 핵심 구현 (이번 세션에서 수정/생성)
```
src/ouroboros/orchestrator/level_context.py        # Phase A: context extraction + prompt injection
src/ouroboros/orchestrator/execution_strategy.py   # Phase B: strategy protocol + 3 strategies
src/ouroboros/orchestrator/parallel_executor.py    # 주요 수정: context, awareness, boundary hints
src/ouroboros/orchestrator/runner.py               # strategy wiring (tools, prompt, activity_map)
src/ouroboros/orchestrator/workflow_state.py        # activity_map parameterization
src/ouroboros/core/seed.py                         # task_type field
src/ouroboros/orchestrator/__init__.py             # exports
```

### 설계 문서
```
docs/api/coordinator-agent-design.md               # Coordinator Agent 아키텍처 설계
docs/responses/parallel-execution-comparison-en-20260206.md  # 원본 비교 분석 문서
```

### 테스트
```
tests/unit/orchestrator/test_level_context.py      # 18 tests
tests/unit/orchestrator/test_execution_strategy.py # 28 tests
tests/unit/orchestrator/test_runner.py             # 기존 (수정 없이 통과)
tests/unit/orchestrator/test_workflow_state.py     # 기존 (수정 없이 통과)
```

### Coordinator 구현 시 참고할 파일
```
src/ouroboros/orchestrator/adapter.py              # ClaudeAgentAdapter.execute_task() 인터페이스
src/ouroboros/events/base.py                       # BaseEvent 스키마
src/ouroboros/persistence/event_store.py           # EventStore.replay() 쿼리 메서드
```

---

## Notes

### 검증 명령어
```bash
# 전체 orchestrator 테스트 (194 tests)
uv run pytest tests/unit/orchestrator/ -v

# level_context + execution_strategy 테스트만
uv run pytest tests/unit/orchestrator/test_level_context.py tests/unit/orchestrator/test_execution_strategy.py -v
```

### Section 7 개선사항 현황

| # | Item | Status |
|---|------|--------|
| 1 | Sub-AC awareness | ✅ 구현 |
| 2 | File boundary hints | ✅ 구현 |
| 3 | Context injection | ✅ 구현 (+ Sub-AC 버그 수정) |
| 4 | Coordinator agent | 🔲 설계 완료, 구현 대기 |
| 5 | File impact prediction | ❌ 드롭 (bias 문제) |
| 6 | Inter-AC messaging | → #4에 흡수 |

---

*Phase A/B 완료. Coordinator Agent (#4) 구현으로 진행 가능.*
