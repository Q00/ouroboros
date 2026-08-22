# Ouroboros Improvement Backlog

> Generated: 2026-08-23
> Branch: `fix/docs-code-alignment-improvements`

## Summary

코드와 문서 간의 불일치, 코드 품질 이슈, 설정 문제를 분석하여 아래 5개 작업 항목을 도출했습니다.

---

## Task 1: docs/architecture.md 수치 불일치 수정

**Priority**: High  
**Type**: Documentation Fix

### Problem
- Line ~30 다이어그램: `Skills (9)` → 실제 22개
- Line ~63 본문: "14 core workflow skills" → 실제 22개
- Line ~64 본문: "9 specialized agents" → 실제 20개 agent .md 파일 존재
- Line ~47 다이어그램: "7 Execution Modes" → `src/ouroboros/execution/`에 소스 파일 없음 (빈 디렉토리)

### Fix
- 다이어그램과 본문의 수치를 실제 코드에 맞게 업데이트
- execution 관련 설명을 현 상태에 맞게 수정 (실행 로직은 mcp/tools/와 orchestrator에 분산)
- agent 수 및 skill 수를 정확히 반영

---

## Task 2: AGENTS.md 명령어 테이블 및 에이전트 목록 업데이트

**Priority**: High  
**Type**: Documentation Fix

### Problem
- 명령어 테이블에 `ooo config`, `ooo ooo` 누락
- "Core" 에이전트 6개 + "Support" 4개 = 10개만 나열, 실제 20개 존재
- 미등록 에이전트: advocate, analysis-agent, breadth-keeper, code-executor, codebase-explorer, consensus-reviewer, judge, ontology-analyst, research-agent, seed-closer, semantic-evaluator

### Fix
- 에이전트 목록에 누락된 11개 에이전트 추가 (카테고리별 분류)
- `ooo:END` 블록의 Agents 섹션 업데이트

---

## Task 3: docs/cli-reference.md에 미문서화 명령어 추가

**Priority**: Medium  
**Type**: Documentation Fix

### Problem
문서화되지 않은 CLI 명령어들:
- `plugin` - 플러그인 관리
- `pm` - PM 인터뷰
- `zcode` - Zcode 런타임 연동
- `harness` - 하네스 관리
- `doctor` - 설정 진단
- `detect` - 런타임 감지
- `artifacts` - 아티팩트 관리
- `codex` - Codex 설정
- `workflow-ir` - 워크플로우 IR

### Fix
- 각 미문서화 명령어에 대해 기본 설명과 사용법 섹션 추가

---

## Task 4: pyproject.toml 설정 개선

**Priority**: Medium  
**Type**: Configuration Fix

### Problem
1. Python 3.14 classifier가 아직 미출시 버전인데 포함됨
2. 일부 의존성 범위가 과도하게 넓음:
   - `typer>=0.12.0,<0.28.0` (16 마이너 버전)
   - `rich>=13.0.0,<16.0.0` (3 메이저 버전)
   - `structlog>=24.0.0,<27.0.0` (3 메이저 버전)

### Fix
- Python 3.14 classifier 제거 (출시 시 재추가)
- 의존성 범위를 합리적으로 조정 (메이저 버전 하나로 제한 검토)

---

## Task 5: src/ouroboros/execution/ 빈 디렉토리 정리

**Priority**: Low  
**Type**: Code Cleanup

### Problem
- `src/ouroboros/execution/` 디렉토리에 `__pycache__`만 존재하고 소스 파일 없음
- 아키텍처 문서에 따르면 이전에 `double_diamond.py`, `decomposition.py`, `atomicity.py`가 있었으나 "no live caller"로 삭제됨
- 빈 디렉토리가 남아있어 혼란 유발

### Fix
- 빈 execution/ 디렉토리 제거 (또는 __init__.py를 남기고 deprecation notice)
- 관련 import 참조가 없는지 확인 후 삭제

---

## Execution Plan

5개 sub-agent를 병렬 실행하여 각 Task를 동시에 처리합니다.
완료 후 단일 PR로 통합 커밋합니다.



---

## Wave 2 — Additional Improvements (found during wave 1)

### Task 6: README.md 아키텍처 섹션 코드-현실 불일치

**Priority**: Medium  
**Type**: Documentation Fix

#### Problem
- 'Under the Hood' 섹션의 디렉토리 구조가 outdated:
  - `execution/` 설명이 'Double Diamond, hierarchical AC decomposition'으로 되어 있으나 실제로 빈 디렉토리
  - `orchestrator/` 라인에 최신 런타임(GJC, Goose, Antigravity, Grok, Zcode) 누락
- 'The Nine Minds' 섹션이 9개만 표시하나 실제 21개 에이전트 존재

#### Fix
- execution/ 라인에 deprecated 표시
- orchestrator/ 라인에 전체 런타임 추가
- Nine Minds 설명에 총 21개 에이전트 언급 추가

---

### Task 7: `ouroboros zcode` CLI 문서화 개선

**Priority**: Medium  
**Type**: Documentation Fix

#### Problem
- `ouroboros zcode`가 독립 명령어 그룹이 아닌, `ouroboros setup --runtime zcode`의 편의 래퍼임을 명시하지 않음
- `ozo` entry point가 pyproject.toml에 정의되어 있으나 어디에도 문서화되지 않음

#### Fix
- cli-reference.md의 zcode 설명 개선
- `ozo` entry point 문서화 추가

---

### Task 8: README.ko.md 동기화

**Priority**: Low  
**Type**: Documentation Fix

#### Problem
- 한국어 README의 아키텍처 섹션도 영문과 동일한 outdated 정보 포함

#### Fix
- 영문 README와 동일한 수정 적용

---

## Status

- [x] Task 1: docs/architecture.md 수치 수정 ✅
- [x] Task 2: AGENTS.md 에이전트 목록 업데이트 ✅
- [x] Task 3: docs/cli-reference.md 미문서화 명령어 추가 ✅
- [x] Task 4: pyproject.toml 설정 개선 ✅
- [x] Task 5: execution/ 빈 디렉토리 정리 ✅
- [x] Task 6: README.md 아키텍처 섹션 수정 ✅
- [x] Task 7: zcode CLI 문서화 개선 ✅
- [x] Task 8: README.ko.md 동기화 ✅
