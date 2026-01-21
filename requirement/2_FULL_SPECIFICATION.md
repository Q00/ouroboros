# Ouroboros: Self-Improving AI Workflow System

## Full Specification Document v0.4

**Date**: 2026-01-12  
**Status**: Implementation Ready  
**Philosophy**: Frugal by Default, Rigorous in Verification

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Unknown Unknowns & Solutions](#2-unknown-unknowns--solutions)
3. [System Architecture](#3-system-architecture)
4. [Phase 0: Big Bang (Clarification)](#4-phase-0-big-bang-clarification)
5. [Phase 1: Tiered Routing (PAL)](#5-phase-1-tiered-routing-pal)
6. [Phase 2: Execution Loop](#6-phase-2-execution-loop)
7. [Phase 3: Resilience System](#7-phase-3-resilience-system)
8. [Phase 4: Evaluation Pipeline](#8-phase-4-evaluation-pipeline)
9. [Phase 5: Consensus Protocol](#9-phase-5-consensus-protocol)
10. [Phase 6: Secondary Loop](#10-phase-6-secondary-loop)
11. [Infrastructure: Persistence](#11-infrastructure-persistence)
12. [Interface Specifications](#12-interface-specifications)
13. [Core Algorithms](#13-core-algorithms)
14. [Future Enhancements](#14-future-enhancements)
15. [Open Questions](#15-open-questions)

---

## 1. Design Philosophy

### 1.1 Core Principle

> **"Frugal by Default, Rigorous in Verification"**

| 원칙 | 의미 | 적용 |
|------|------|------|
| **Frugal by Default** | 기본값은 항상 비용 효율적 선택 | 모든 작업은 1x Cost에서 시작 |
| **Rigorous in Verification** | 검증에서는 엄격함 타협 안 함 | 최종 Gate에서 30x Cost 투입 |
| **Efficiency in Execution** | 실행은 빠르고 가볍게 | 단일 모델로 충분한 곳에 Quorum 금지 |
| **Resilience over Perfection** | 완벽보다 회복력 | 실패해도 멈추지 않고 우회 |

### 1.2 Anti-Patterns (명시적 금지)

| Anti-Pattern | 문제점 | 대안 |
|--------------|--------|------|
| Consensus Everywhere | 비용 폭발 | Gate에서만 Consensus |
| Frontier First | 비용 폭발 | Frugal First, 에스컬레이션 |
| Infinite Retry | 정체 | Lateral Thinking 전환 |
| Immediate Optimization | 야크 털 깎기 | TODO Registry로 Defer |
| Vague Seed | GIGO | Ambiguity Threshold 강제 |

### 1.3 Decision Framework

```python
def select_approach(task):
    # 1. Frugal로 충분한가?
    if task.complexity < 0.4:
        return Tier.FRUGAL  # 1x cost
    
    # 2. 되돌릴 수 있는 결정인가?
    if task.reversible:
        return Tier.STANDARD  # 10x cost, 빠르게
    
    # 3. Seed 방향에 영향을 주는가?
    if task.affects_seed_direction:
        return Tier.FRONTIER  # 30x cost, Consensus
    
    return Tier.STANDARD
```

---

## 2. Unknown Unknowns & Solutions

v0.1~v0.4 연구를 통해 해결된 미지수들:

| # | Unknown | Solution | Version |
|---|---------|----------|---------|
| 1 | 경계 문제 | 시스템 = 인프라 + 파이프라인 전체 | v0.1 |
| 2 | 기초 문제 | Big Bang (Seed) 필요성 인정 | v0.1 |
| 3 | 종결 문제 | 한 단계 위에서만 평가 | v0.1 |
| 4 | 불변량 문제 | Seed 방향 = 불변량 | v0.1 |
| 5 | 수렴 문제 | Double Diamond + Retrospective | v0.1 |
| 6 | 실패 모드 | Stagnation Detection + Lateral | v0.2 |
| 7 | 비용 효율성 | Tiered Routing (1x/10x/30x) | v0.2 |
| 8 | 정체 탈출 | Lateral Thinking Personas | v0.2 |
| 9 | 상태 영속성 | Checkpoint Persistence | v0.2 |
| 10 | 모호성 정량화 | Ambiguity Threshold ≤ 0.2 | v0.3 |
| 11 | Consensus 남용 | Trigger Matrix | v0.3 |
| 12 | 비용 가시성 | Cost Factor 명시 | v0.4 |

---

## 3. System Architecture

### 3.1 Layer Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        OUROBOROS SYSTEM v0.4                                │
│                                                                             │
│              "Frugal by Default, Rigorous in Verification"                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  PHASE 0: BIG BANG                                                  │   │
│  │  User Input → Scoping → Interview Loop → Ambiguity Check (≤ 0.2)   │   │
│  │  Output: Immutable Seed JSON                                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼────────────────────────────────────┐  │
│  │  PAL ROUTER (Provider Abstraction Layer)                             │  │
│  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐            │  │
│  │  │    FRUGAL     │  │   STANDARD    │  │   FRONTIER    │            │  │
│  │  │   1x Cost     │  │   10x Cost    │  │   30x Cost    │            │  │
│  │  │   DEFAULT     │  │   On-Demand   │  │   Gate Only   │            │  │
│  │  └───────────────┘  └───────────────┘  └───────────────┘            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                    │                                        │
│  ╔═════════════════════════════════▼═══════════════════════════════════╗   │
│  ║  EXECUTION LOOP                                                     ║   │
│  ║  ┌────────────────────────────────────────────────────────────────┐ ║   │
│  ║  │  DOUBLE DIAMOND: Discover → Define → Design → Deliver          │ ║   │
│  ║  └────────────────────────────────────────────────────────────────┘ ║   │
│  ║                                 │                                    ║   │
│  ║  ┌──────────────────────────────▼─────────────────────────────────┐ ║   │
│  ║  │  STAGNATION DETECTOR                                           │ ║   │
│  ║  │  Spinning | Oscillation | No Drift | Diminishing Returns       │ ║   │
│  ║  └──────────────────────────────┬─────────────────────────────────┘ ║   │
│  ║                                 │                                    ║   │
│  ║              ┌──────────────────┼──────────────────┐                ║   │
│  ║              ▼                  ▼                  ▼                 ║   │
│  ║        [NORMAL]           [ESCALATE]         [STAGNATION]           ║   │
│  ║            │                   │                   │                 ║   │
│  ║            │                   ▼                   ▼                 ║   │
│  ║            │            Upgrade Tier        LATERAL THINKING         ║   │
│  ║            │                                Persona Switch           ║   │
│  ║            └───────────────────┬───────────────────┘                 ║   │
│  ╚════════════════════════════════╪════════════════════════════════════╝   │
│                                   │                                         │
│  ┌────────────────────────────────▼─────────────────────────────────────┐  │
│  │  3-STAGE EVALUATION PIPELINE                                         │  │
│  │                                                                       │  │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │  │
│  │  │  STAGE 1    │    │  STAGE 2    │    │  STAGE 3    │               │  │
│  │  │ Mechanical  │───►│  Semantic   │───►│ Consensus   │               │  │
│  │  │   $0        │    │   $$        │    │   $$$$      │               │  │
│  │  │  (Always)   │    │  (Always)   │    │ (Trigger)   │               │  │
│  │  └─────────────┘    └─────────────┘    └─────────────┘               │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                   │                                         │
│  ┌────────────────────────────────▼─────────────────────────────────────┐  │
│  │  SECONDARY LOOP: TODO Registry → Prioritize → Batch Execute         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ═══════════════════════════════════════════════════════════════════════   │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │  PERSISTENCE LAYER: Checkpoint → State DB → Recovery                  │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │  RETROSPECTIVE: Every 3 iterations → Drift Analysis → Direction Fix  │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Phase 0: Big Bang (Clarification)

### 4.1 Objective

GIGO(Garbage In, Garbage Out) 방지. 모호한 의도를 기계적으로 검증 가능한 Immutable Seed로 변환.

### 4.2 Ambiguity Threshold

```yaml
ambiguity:
  threshold: 0.2  # 이 값 이하여야 실행 허용
  
  scale:
    0.0: "완전히 명확 - 기계 검증 가능"
    0.2: "충분히 명확 - 실행 허용"  # ← GATE
    0.5: "보통 - 추가 질문 필요"
    0.8: "매우 모호 - 상당한 명확화 필요"
    1.0: "완전히 모호 - 해석 불가"
```

### 4.3 Ambiguity Measurement Algorithm

```python
async def calculate_ambiguity(requirements: str, model: Model) -> float:
    """
    모호성 점수 (0.0 ~ 1.0) 계산
    0.2 이하일 때만 실행 루프 진입 허용
    """
    scores = await model.evaluate(
        requirements,
        criteria=[
            ("measurability", 0.30),   # 성공 기준 측정 가능성
            ("constraints", 0.30),     # 제약 조건 명확성
            ("scope", 0.20),           # 범위의 경계
            ("technical", 0.20)        # 기술적 구체성
        ]
    )
    
    # 명확성 점수의 가중 평균
    clarity = sum(score * weight for score, weight in scores)
    
    # 모호성 = 1 - 명확성
    return 1.0 - clarity
```

### 4.4 Interview Protocol

```typescript
interface InterviewProtocol {
  config: {
    maxRounds: 10;
    ambiguityThreshold: 0.2;
    modelTier: 'standard';  // 10x cost
  };
  
  stages: {
    scoping: {
      purpose: "범위와 경계 파악";
      questions: [
        "최종 산출물은 무엇인가요?",
        "성공을 어떻게 측정할 수 있나요?",
        "반드시 포함/제외할 것은?"
      ];
    };
    
    constraints: {
      purpose: "제약 파악";
      questions: [
        "기술 스택 제약이 있나요?",
        "절대로 하면 안 되는 것은?",
        "시간/비용/품질 우선순위는?"
      ];
    };
    
    clarification: {
      purpose: "모호한 부분 명확화";
      dynamicQuestions: true;
    };
  };
  
  exitCondition: "ambiguity <= 0.2 OR rounds >= maxRounds";
}
```

### 4.5 Seed Interface

```typescript
interface Seed {
  readonly seedId: string;
  readonly createdAt: DateTime;
  readonly ambiguityScore: number;  // Must be ≤ 0.2
  readonly version: "0.4";
  readonly immutable: true;
  
  goal: {
    statement: string;
    successMetrics: SuccessMetric[];
    scope: {
      includes: string[];
      excludes: string[];
    };
  };
  
  constraints: {
    hard: Constraint[];   // 절대 위반 불가
    soft: Constraint[];   // 선호하지만 협상 가능
  };
  
  acceptanceCriteria: AcceptanceCriteria[];
  
  ontologySchema: {
    concepts: Concept[];
    relationships: Relationship[];
    rules: Rule[];
  };
  
  evaluationPrinciples: {
    criteria: EvaluationCriterion[];
    priorities: PriorityOrder;
  };
  
  exitConditions: {
    success: ExitCondition[];
    failure: ExitCondition[];
    timeout: TimeoutConfig;
  };
}

interface SuccessMetric {
  name: string;
  measurement: string;
  target: string | number;
  verificationMethod: 'automated' | 'manual' | 'llm';
}
```

---

## 5. Phase 1: Tiered Routing (PAL)

### 5.1 Economic Model

| Tier | Cost Factor | Intelligence | Models | Use Cases |
|------|-------------|--------------|--------|-----------|
| **Frugal** | **1x** | 9~11 | gpt-4o-mini, gemini-flash, haiku | 루틴 코딩, 로그 분석, Stage 1 수정 |
| **Standard** | **10x** | 14~16 | gpt-4o, claude-sonnet, gemini-pro | 논리 설계, Stage 2 평가, 인터뷰 |
| **Frontier** | **30x+** | 18~20 | o3, claude-opus | Consensus, Lateral, Big Bang |

### 5.2 Routing Logic

```typescript
interface RoutingContext {
  taskComplexity: number;        // 0.0 - 1.0
  previousFailures: number;
  currentTier: Tier;
  consecutiveSuccesses: number;
  consensusRequired: boolean;
}

function routeToTier(context: RoutingContext): Tier {
  // Critical path: Consensus 필요 시 Frontier
  if (context.consensusRequired) {
    return Tier.FRONTIER;  // 30x
  }
  
  // Escalation: 2회 연속 실패
  if (context.previousFailures >= 2) {
    return upgradeTier(context.currentTier);
  }
  
  // Downgrade: 5회 연속 성공
  if (context.consecutiveSuccesses >= 5) {
    return downgradeTier(context.currentTier);
  }
  
  // Complexity-based
  if (context.taskComplexity > 0.7) {
    return Tier.FRONTIER;
  } else if (context.taskComplexity > 0.4) {
    return Tier.STANDARD;
  }
  
  // Default: Frugal
  return Tier.FRUGAL;  // 1x
}
```

### 5.3 Fallback & Recovery

```yaml
fallback:
  on_model_failure:
    action: "switch_to_same_tier_alternative"
    max_retries: 2
    
  on_tier_exhausted:
    action: "escalate_to_higher_tier"
    
  on_all_failed:
    action: "pause_and_notify"
    
  context_revival:
    trigger: "model_switch OR context_overflow"
    strategy: "summarize_core_and_inject"
    max_tokens: 2000
```

---

## 6. Phase 2: Execution Loop

### 6.1 Double Diamond Cycle

```
Discover (Diverge) → Define (Converge) → Design (Diverge) → Deliver (Converge)
```

각 단계에서 Ontology Reflection 수행:
- 우리가 정의한 온톨로지가 문제 본질을 포착하는가?
- 누락된 개념이나 관계는 없는가?
- 다른 프레이밍이 더 적절할 수 있는가?

### 6.2 Acceptance Criteria Structure

```typescript
interface AcceptanceCriteria {
  id: string;              // e.g., "AC-1.2.3"
  level: number;
  parentId: string | null;
  
  inheritedContext: {
    goalFragment: string;
    constraints: Constraint[];
    ontologySubset: OntologySchema;
  };
  
  scope: {
    includes: string[];
    excludes: string[];
    assumptions: string[];
  };
  
  status: 'pending' | 'in-progress' | 'completed' | 'failed' | 'blocked';
  iterationCount: number;
  
  children: AcceptanceCriteria[];
  result: Result | null;
}
```

### 6.3 Atomic Judgment

```python
def is_atomic(ac: AcceptanceCriteria) -> bool:
    """AC가 더 이상 분해 불필요한 Atomic 단위인지 판정"""
    
    if estimate_complexity(ac) > COMPLEXITY_THRESHOLD:
        return False
        
    if has_unresolved_dependencies(ac):
        return False
        
    if not can_verify_completion(ac):
        return False
        
    if estimate_duration(ac) > MAX_ATOMIC_DURATION:
        return False
        
    return True
```

---

## 7. Phase 3: Resilience System

### 7.1 Stagnation Detection

```typescript
interface StagnationPatterns {
  spinning: {
    description: "동일한 에러 시그니처 반복";
    condition: "error_signature_match(current, previous)";
    threshold: 2;
  };
  
  oscillation: {
    description: "상태가 A→B→A→B로 진동";
    condition: "state_cycle_detected";
    threshold: 2;
  };
  
  noDrift: {
    description: "출력은 생성되지만 목표 접근 없음";
    condition: "drift_delta < epsilon";
    threshold: 3;
  };
  
  diminishingReturns: {
    description: "개선폭이 점점 감소";
    condition: "improvement_rate < 0.01";
    threshold: 3;
  };
}
```

### 7.2 Stagnation Response

```python
def respond_to_stagnation(
    pattern: StagnationPattern,
    context: StagnationContext
) -> StagnationResponse:
    
    # 1차: 모델 에스컬레이션
    if not context.already_escalated:
        return EscalateModel(target=Tier.FRONTIER)
    
    # 2차: Lateral Thinking
    if not context.lateral_attempted:
        persona = select_persona(pattern)
        return ActivateLateral(persona)
    
    # 3차: 문제 재프레이밍
    if pattern in ['oscillation', 'spinning']:
        return ReframeProblem()
    
    # 4차: 인간 개입 (최후 수단)
    return RequestHumanInput(compile_context(context))
```

### 7.3 Lateral Thinking Personas

| Persona | Trigger | Strategy | Old Name |
|---------|---------|----------|----------|
| **The Hacker** | 반복 구현 실패 | "우아함은 버려라. 하드코딩이라도 작동하게 만들어라." | workaround_finder |
| **The Researcher** | 원인 불명 에러 | "코딩을 멈춰라. 문서와 외부 검색에만 집중하라." | researcher |
| **The Simplifier** | 복잡도 초과 | "기능을 절반으로 줄여라. MVP로 돌아가라." | simplifier |
| **The Architect** | 구조적 막힘 | "아키텍처를 의심하라. 기본 원리부터 다시 생각하라." | first_principles |
| **The Contrarian** | 모든 시도 실패 | "현재 가정의 반대로 생각하라. 문제 정의 자체에 의문을 제기하라." | devils_advocate |

### 7.4 SubAgent Isolation

Lateral Thinking 실행 시 메인 컨텍스트 오염 방지:

```typescript
interface SubAgentConfig {
  spawnMode: 'isolated';
  
  contextFilter: {
    include: ['task_definition', 'relevant_constraints', 'what_tried_summary'];
    exclude: ['failed_attempts_detail', 'debug_logs', 'tangential_discussions'];
  };
  
  resources: {
    maxTokens: number;
    timeout: Duration;
    modelTier: 'frontier';  // 30x
  };
  
  returnFormat: {
    summaryOnly: true;
    fields: ['alternative_approach', 'rationale', 'risks', 'quick_win_potential'];
  };
  
  lifecycle: {
    onComplete: 'return_result_and_terminate';
    onFailure: 'return_error_and_terminate';
    cleanup: { releaseMemory: true, archiveLogs: true };
  };
}
```

---

## 8. Phase 4: Evaluation Pipeline

### 8.1 Overview

```
Stage 1 (Mechanical) → Stage 2 (Semantic) → Stage 3 (Consensus)
       $0                    $$                   $$$$
    Always Run            Always Run          Trigger-Based
```

### 8.2 Stage 1: Mechanical Verification

```yaml
stage_1:
  purpose: "기계적 검증 - LLM 비용 $0"
  always_run: true
  
  checks:
    lint:
      tools: ["eslint", "pylint", "rustfmt"]
      fail_on: "error"
      
    build:
      command: "project_specific"
      timeout: "5m"
      
    test:
      command: "project_specific"
      coverage_threshold: 0.7
      
    static_analysis:
      tools: ["sonarqube", "semgrep"]
      severity_threshold: "high"
      
  on_fail:
    action: "return_to_execution"
    model_tier: "frugal"  # 1x로 수정
```

### 8.3 Stage 2: Semantic Evaluation

```yaml
stage_2:
  purpose: "의미적 품질 평가"
  cost: "$$"
  model_tier: "standard"  # 10x
  always_run: true  # Stage 1 통과 후
  
  evaluations:
    ac_compliance:
      threshold: 0.8
      
    goal_alignment:
      threshold: 0.8
      
    constraint_check:
      hard_tolerance: 0
      soft_tolerance: 2
      
    drift_measurement:
      dimensions: ["goal", "constraints", "ontology"]
      
  output:
    satisfaction_score: number
    uncertainty_score: number
    drift_metrics: DriftMetrics
```

### 8.4 Stage 3: Consensus (Conditional)

```yaml
stage_3:
  purpose: "다중 모델 합의"
  cost: "$$$$"
  model_tier: "frontier"  # 30x × 3
  conditional: true
  
  config:
    min_models: 3
    diversity: "different_providers"
    threshold: 0.67  # 2/3 majority
    
  output:
    decision: "approved | rejected | no_consensus"
    votes: Vote[]
    synthesis: string
    dissent: string | null
```

---

## 9. Phase 5: Consensus Protocol

### 9.1 Consensus Trigger Matrix

| Scenario | Consensus? | Reason |
|----------|------------|--------|
| Routine AC 완료 | ❌ NO | Stage 2로 충분 |
| Atomic Task 완료 | ❌ NO | 단순 작업 |
| Stage 1 실패 | ❌ NO | 기계적 오류 |
| **Final Delivery** | ✅ YES | 되돌릴 수 없는 결정 |
| **Ontology Change** | ✅ YES | 시스템 세계관 영향 |
| **Lateral Adoption** | ✅ YES | 전략 변경 승인 |
| **Seed Drift Alert** (>0.3) | ✅ YES | 방향 이탈 판정 |
| **Stage 2 Uncertainty** (>0.3) | ✅ YES | 평가 불확실 |

### 9.2 Trigger Decision Logic

```typescript
function shouldTriggerConsensus(
  stage2: Stage2Result,
  context: EvaluationContext
): ConsensusTriggerResult {
  
  // Mandatory triggers
  if (context.isFinalDelivery) {
    return { trigger: true, reason: 'final_delivery' };
  }
  
  if (context.ontologyChanged) {
    return { trigger: true, reason: 'ontology_change' };
  }
  
  if (context.isLateralProposal) {
    return { trigger: true, reason: 'lateral_adoption' };
  }
  
  if (stage2.driftMetrics.overall > 0.3) {
    return { trigger: true, reason: 'seed_drift_alert' };
  }
  
  // Conditional triggers
  if (stage2.uncertaintyScore > 0.3) {
    return { trigger: true, reason: 'high_uncertainty' };
  }
  
  // Never triggers
  if (context.isRoutineAC || context.isAtomicTask) {
    return { trigger: false, reason: null };
  }
  
  return { trigger: false, reason: null };
}
```

---

## 10. Phase 6: Secondary Loop

### 10.1 Discover and Defer

주 목표 달성에 치명적이지 않은 개선점은 TODO Registry로 미룸.

### 10.2 TODO Registry

```typescript
interface TodoItem {
  id: string;
  createdAt: DateTime;
  
  discoveredAt: {
    phase: 'discover' | 'define' | 'design' | 'deliver';
    acId: string;
    context: string;
  };
  
  category: 'optimization' | 'security' | 'refactoring' | 
            'documentation' | 'technical_debt' | 'test_coverage';
            
  priority: 'low' | 'medium';  // 'high'는 즉시 처리됨
  estimatedEffort: Duration;
  suggestedAction: string;
  
  relatedToAC: string | null;
  blocksCurrentAC: boolean;  // true면 즉시 처리
  
  status: 'pending' | 'in_progress' | 'completed' | 'deferred';
}
```

### 10.3 Triage Logic

```typescript
function triageTodoItem(item: TodoItem, context: Context): TriageDecision {
  // 현재 AC 차단 → 즉시 실행
  if (item.blocksCurrentAC) {
    return { action: 'execute_now', reason: 'blocks_ac' };
  }
  
  // 보안 Critical → 즉시 실행
  if (item.category === 'security' && item.priority === 'high') {
    return { action: 'execute_now', reason: 'critical_security' };
  }
  
  // Seed 범위 벗어남 → 버림
  if (!isWithinScope(item, context.seed)) {
    return { action: 'discard', reason: 'out_of_scope' };
  }
  
  // 그 외 → 레지스트리에 등록
  return { action: 'defer', reason: 'not_blocking' };
}
```

### 10.4 Async Execution

```yaml
secondary_loop:
  trigger: "primary_goal_satisfied"
  
  process:
    1: "aggregate_pending_todos"
    2: "prioritize_by_impact_effort"
    3: "present_to_user_or_auto_decide"
    4: "execute_approved_items"
    
  execution:
    model_tier: "standard"  # 10x
    max_items_per_session: 10
    mode: "async_batch"
```

---

## 11. Infrastructure: Persistence

### 11.1 Checkpointing

```yaml
persistence:
  storage: "postgresql"
  
  checkpointing:
    strategy: "after_each_node"
    periodic: "5m"
    
  state_schema:
    immutable:
      - seed
    mutable:
      - ac_tree
      - iteration_state
      - routing_state
      - todo_registry
      - drift_metrics
      - stagnation_state
      - lateral_history
      
  recovery:
    on_restart: "load_latest_checkpoint"
    on_corruption: "rollback_to_previous"
    max_rollback_depth: 3
```

### 11.2 Context Compression

```yaml
context_compression:
  max_age: "6h"
  max_tokens: 100000
  
  strategy: "summarize_and_archive"
  
  preserve_always:
    - seed
    - current_ac
    - recent_failures
    - active_constraints
    
  compress:
    - older_iteration_details
    - resolved_ac_details
    - debug_logs
```

### 11.3 Database Schema

```sql
CREATE TABLE ouroboros_sessions (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    goal_summary TEXT,
    status VARCHAR(20) NOT NULL,
    total_iterations INT DEFAULT 0,
    total_cost_units INT DEFAULT 0  -- 1x = 1 unit
);

CREATE TABLE ouroboros_checkpoints (
    id UUID PRIMARY KEY,
    session_id UUID REFERENCES ouroboros_sessions(id),
    created_at TIMESTAMP NOT NULL,
    
    seed JSONB NOT NULL,
    ac_tree JSONB NOT NULL,
    iteration_state JSONB NOT NULL,
    routing_state JSONB NOT NULL,
    
    version VARCHAR(20) NOT NULL,
    is_valid BOOLEAN DEFAULT TRUE
);

CREATE TABLE ouroboros_todos (
    id UUID PRIMARY KEY,
    session_id UUID REFERENCES ouroboros_sessions(id),
    category VARCHAR(30) NOT NULL,
    priority VARCHAR(10) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending'
);
```

---

## 12. Interface Specifications

### 12.1 Complete Interface Index

```typescript
// Phase 0
interface Seed { ... }
interface InterviewProtocol { ... }
interface AmbiguityResult { score: number; breakdown: Record<string, number>; }

// Phase 1
interface RoutingContext { ... }
interface RoutingDecision { tier: Tier; reason: string; }

// Phase 2
interface AcceptanceCriteria { ... }
interface Result { acId: string; output: any; metadata: Metadata; }

// Phase 3
interface StagnationPatterns { ... }
interface LateralPersona { id: string; prompt: string; }
interface SubAgentConfig { ... }

// Phase 4
interface EvaluationResult {
  mechanical: { passed: boolean; logs: string[]; };
  semantic: { 
    satisfactionScore: number; 
    uncertaintyScore: number;
    driftMetrics: DriftMetrics;
  };
  consensus?: {
    triggered: boolean;
    decision: 'approved' | 'rejected' | 'no_consensus';
    votes: Vote[];
  };
  finalVerdict: 'pass' | 'fail' | 'iterate';
}

// Phase 5
interface ConsensusTriggerResult {
  trigger: boolean;
  reason: ConsensusTrigger | null;
}

// Phase 6
interface TodoItem { ... }
interface TriageDecision { action: string; reason: string; }

// Infrastructure
interface Checkpoint { ... }
interface RecoveryProtocol { ... }
```

---

## 13. Core Algorithms

### 13.1 Drift Measurement

```python
def calculate_drift(current_state: State, seed: Seed) -> DriftMetrics:
    """현재 상태가 Seed 목표에서 얼마나 벗어났는지 측정"""
    
    # 1. Goal Alignment (Vector Similarity)
    goal_drift = 1.0 - cosine_similarity(
        embed(current_state.output),
        embed(seed.goal.statement)
    )
    
    # 2. Constraint Violations
    violations = count_violations(current_state, seed.constraints)
    constraint_drift = min(violations * 0.1, 1.0)
    
    # 3. Ontology Deviation
    ontology_drift = ontology_distance(
        current_state.effective_ontology,
        seed.ontologySchema
    )
    
    # Weighted Sum
    overall = (
        goal_drift * 0.5 +
        constraint_drift * 0.3 +
        ontology_drift * 0.2
    )
    
    return DriftMetrics(
        goal=goal_drift,
        constraints=constraint_drift,
        ontology=ontology_drift,
        overall=overall
    )
```

### 13.2 Integrated Execution Loop

```python
async def main_execution_loop(seed: Seed, persistence: Persistence):
    state = await persistence.load_or_create(seed)
    router = PALRouter(config)
    detector = StagnationDetector(config)
    
    while not should_exit(state, seed.exitConditions):
        try:
            # 1. Select current AC
            current_ac = select_next_ac(state.ac_tree)
            
            # 2. Route to model (1x / 10x / 30x)
            tier = router.route(current_ac, state.routing_state)
            
            # 3. Execute Double Diamond
            result = await execute_double_diamond(current_ac, tier)
            
            # 4. Check stagnation
            stagnation = detector.detect(result, state)
            if stagnation:
                result = await handle_stagnation(stagnation, state)
            
            # 5. Evaluate (3-stage pipeline)
            evaluation = await evaluate_pipeline(result, current_ac)
            
            # 6. Update state
            state = update_state(state, current_ac, result, evaluation)
            
            # 7. Checkpoint
            await persistence.checkpoint(state)
            
            # 8. Retrospective check
            if state.iteration_count % 3 == 0:
                await run_retrospective(state, seed)
                
        except RecoverableError as e:
            state = await handle_error(e, state, persistence)
        except CriticalError as e:
            await notify_and_pause(e, state)
            break
    
    # Secondary Loop
    if state.status == 'completed' and state.todo_registry.has_pending():
        await run_secondary_loop(state)
    
    return state
```

---

## 14. Future Enhancements

### 14.1 Self-Tooling (v1.0+)

```yaml
description: |
  에이전트가 필요한 도구를 코드로 작성하고
  런타임에 MCP 서버에 등록하여 사용.
  
deferred_reason:
  - "v0.x 안정화 우선"
  - "보안 검증 메커니즘 필요"
  - "생성된 도구 품질 보증"
  
prerequisites:
  - "안전한 샌드박스"
  - "도구 검증 파이프라인"
  - "Skill Library 저장소"
```

### 14.2 Multi-Instance Hive Mind (v1.0+)

```yaml
description: |
  여러 Ouroboros 인스턴스 간 학습 공유.
  성공한 Lateral 전략을 "면역 항체"처럼 공유.
  
deferred_reason:
  - "단일 인스턴스 안정화 우선"
  - "프라이버시/보안 고려"
```

---

## 15. Open Questions

| # | Question | Priority | Component |
|---|----------|----------|-----------|
| 1 | Ambiguity 측정의 모델 간 일관성은? | High | Big Bang |
| 2 | Cost Factor 실제 측정값과 일치하는가? | High | PAL |
| 3 | Stagnation false positive 감소 방법은? | Medium | Resilience |
| 4 | Consensus 타임아웃 전략은? | Medium | Consensus |
| 5 | Retrospective 자동화 범위는? | Low | Retrospective |

---

## Appendix: Glossary

| Term | Definition |
|------|------------|
| **Seed** | 불변의 시작점. Ambiguity ≤ 0.2 필수. |
| **Frugal** | 1x Cost Tier. 기본값. |
| **Standard** | 10x Cost Tier. 복잡한 작업용. |
| **Frontier** | 30x Cost Tier. Consensus/Lateral용. |
| **Stagnation** | 오류 없이 진행되지 않는 상태. |
| **Lateral Thinking** | 페르소나 전환으로 정체 돌파. |
| **Consensus Trigger** | Stage 3 사용 여부 결정 조건. |

---

*End of Full Specification*
