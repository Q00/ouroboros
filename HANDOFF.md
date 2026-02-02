# Handoff Document

> Last Updated: 2026-02-03
> Session: Ontological Framework Implementation - Phase 6 Complete

---

## Goal

Ouroboros v0.4.0에 **Ontological Framework** 추가:
1. **AOP 기반 분석 프레임워크** - 횡단 관심사를 모듈화하여 재사용 가능한 분석 전략 구현
2. **Deliberative Consensus** - Advocate/Devil/Judge 역할 기반 2라운드 토론 시스템
3. **Devil's Advocate Strategy** - 온톨로지 질문으로 "근본 해결책인가?" 검증

---

## Current Progress

### ✅ Phase 6: Quality Review - COMPLETE

**Code Review 이슈 수정 완료:**

| 이슈 | 위치 | 해결 |
|------|------|------|
| Exception Handling (ProviderError 중복 래핑) | `consensus.py:731-737` | try/except 제거 - Strategy가 내부에서 에러 처리 |
| Unused import | `consensus.py:26` | `build_devil_advocate_prompt` 제거 |
| Import ordering | 3개 파일 | `ruff --fix`로 자동 정렬 |
| Missing `__all__` | `ontology_aspect.py` | 이미 존재 (line 443-454) |

**테스트 결과:** 73개 테스트 통과 (consensus + ontology 관련)

### ✅ 완료된 구현

| 파일 | 설명 | 테스트 |
|------|------|--------|
| `src/ouroboros/core/ontology_questions.py` | 온톨로지 질문 정의 | ✅ |
| `src/ouroboros/core/ontology_aspect.py` | AOP 분석 프레임워크 | ✅ |
| `src/ouroboros/evaluation/models.py` | VoterRole, FinalVerdict, DeliberationResult | ✅ |
| `src/ouroboros/evaluation/consensus.py` | DeliberativeConsensus 클래스 | ✅ |
| `src/ouroboros/strategies/devil_advocate.py` | DevilAdvocateStrategy | ✅ |
| `tests/unit/evaluation/test_consensus.py` | Deliberative 테스트 | ✅ |
| `tests/unit/core/test_ontology_aspect.py` | AOP 테스트 | ✅ |
| `tests/unit/core/test_ontology_questions.py` | 온톨로지 질문 테스트 | ✅ |

---

## Next Steps

### Phase 7: Summary

1. **전체 테스트 실행**
   ```bash
   uv run pytest tests/unit/evaluation/ tests/unit/core/ -v
   ```

2. **변경 사항 커밋** (선택적)
   ```bash
   git add -p  # 변경 검토
   git commit -m "feat(evaluation): add deliberative consensus with AOP-based devil's advocate"
   ```

### 대기 중 (낮은 우선순위)

| 파일 | 설명 |
|------|------|
| `src/ouroboros/bigbang/ontology.py` | Interview Phase 통합 |
| `src/ouroboros/bigbang/ambiguity.py` | Ontology Score 가중치 추가 |

---

## Important Files

### 핵심 구현
```
src/ouroboros/core/ontology_questions.py    # 온톨로지 질문 정의
src/ouroboros/core/ontology_aspect.py       # AOP 프레임워크 (BaseAnalyzer, AnalysisResult)
src/ouroboros/evaluation/consensus.py       # DeliberativeConsensus (lines 500-830)
src/ouroboros/strategies/devil_advocate.py  # Strategy 패턴 구현
```

### 테스트
```
tests/unit/evaluation/test_consensus.py     # 32개 테스트
tests/unit/core/test_ontology_aspect.py     # 18개 테스트
tests/unit/core/test_ontology_questions.py  # 23개 테스트
```

---

## Notes

### 아키텍처 결정

1. **Devil's Advocate는 Strategy 객체**: LLM 호출 대신 `DevilAdvocateStrategy.analyze()` 사용
2. **Strategy가 에러 처리**: `analyze()` 메서드가 LLM 에러를 내부에서 처리하여 `AnalysisResult.invalid()` 반환
3. **AnalysisResult.is_valid**: `True` = 근본 해결책, `False` = 증상 치료

### 검증 명령어

```bash
# 테스트
uv run pytest tests/unit/evaluation/test_consensus.py -v
uv run pytest tests/unit/core/ -v

# 린트
uv run ruff check src/ouroboros/evaluation/ src/ouroboros/core/ src/ouroboros/strategies/
```

---

*Phase 6 완료. Phase 7 (Summary)로 진행 가능.*
