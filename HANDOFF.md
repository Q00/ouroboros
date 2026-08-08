# Handoff Document

> Last Updated: 2026-08-06
> Session: Hidden-Checklist Convergence Loop — run → eval → evolve를 `ooo run` 한 번으로

---

## Goal

`ooo run`을 "한 번 실행하고 끝"에서 **"숨은 체크리스트가 전부 PASS할 때까지 수렴"**으로 바꾼다. 오너가 확정한 3원칙:

1. **답안지 은닉**: AC는 한 문장(description) + 검증요소(체크리스트)로 구성된다. 워커에게는 한 문장과 산출물 목록만 보여주고, `verify_command`·`output_assertion`은 **절대 보여주지 않는다**. 공개수위 설정(config knob)은 만들지 않는다 — 무조건 은닉 (오너 명시 결정).
2. **힌트 루프**: 검증 실패 시 체크리스트를 공개하는 대신, **그 세션이 실제로 한 툴콜과 산출물 기록을 근거로 지시사항을 고쳐서** 다음 라운드에 넘긴다 (assertion 문자열은 힌트에도 누출 금지).
3. **run → eval → evolve 단일 체인**: 평가에서 실패한 AC만 evolve(focused 재실행)로 넘겨 세대를 돌리고, 이 전체가 `ooo run` 한 번의 호출로 완결된다. blocked는 예산 소진 시 최후 수단.

배경: 이 설계는 blocked 최소화 논의에서 나왔다. 시드 게이트(preflight)는 "사실 부족"을 걸러 사람에게 묻고, 이 수렴 루프는 "구현 품질"을 소화한다 — 상호 보완이며 대체가 아니다.

---

## Current Progress

이번 세션(2026-08-06)까지 완료된 기반 — **전부 미커밋 워킹트리 상태**:

- **Seed Preflight 게이트** (`src/ouroboros/auto/seed_preflight.py` 신규): 허구 스크립트·미바인딩 `$VAR`·개념 경로를 RUN 전에 결정론 차단, 열린 질문 산출. 실코퍼스 190개로 오탐 검증 완료.
- **점수 게이밍 제거**: seed QA의 `ambiguity_score := 0.19` 강제 하향 삭제, ambiguity 요구는 `seed_qa_ambiguity_unrepairable` 블록으로.
- **조용한 블록 수정**: `auto.session.blocked` 이벤트 신설 + CLI/MCP 양쪽 driver `event_store` 배선(이전엔 모든 auto.* 이벤트가 무음 폐기) + attention relay 등록. 라이브 검증됨.
- **`wait_for_pending_emits` 라이브락 수정** (`interview_driver.py`): done 태스크가 discard 안 되면 무한회전하던 사전 결함.
- 테스트: auto 스위트 1,370개 + relay/start_auto 등 전체 그린, ruff/mypy 클린.

이번 계획(아래 Next Steps)은 **아직 구현 시작 전**이다.

---

## What Worked (이미 존재하는 레버리지 — 새로 만들지 말 것)

- **채점 권한은 이미 오케스트레이터 소유** (#1591): verify gate가 `expected_artifacts` 존재·`verify_command` exit 0·`output_assertion` substring·워크스페이스 다이제스트를 직접 판정 — `parallel_executor.py:9631-9768` (`_run_ac_verify_gate`), 적용 `:9770-9902`.
- **run→eval 서버 체인 존재**: `execution_handlers.py:2774`가 유일한 트리거. 단 **성공한 run + `ouroboros_start_execute_seed`(백그라운드) 경로에만**. 체인 평가는 항상 `acceptance_criteria`를 실어 **multi-AC checklist 경로**를 타므로 AC별 pass/fail이 구조적으로 나온다 (`evaluation_handlers.py:999-1019` — `checklist:[{ac_text, passed, reasoning, evidence, failure_reason}]`).
- **evolve_step 내부에 루프 완결**: execute→validate→evaluate→wonder→reflect→새 Seed가 이벤트 소싱으로 이미 들어 있다 (`evolution/loop.py:1485-1940`). ralph는 그걸 N회 도는 얇은 드라이버 (`ralph_loop.py:155`, `:214`).
- **평가→다음 세대 번역기 존재**: `focus.select_evolution_focus()` (`evolution/focus.py:147`)가 `evaluation.ac_results`에서 통과 AC frozen / 실패 AC active를 계산하고, 통과 AC는 `externally_satisfied_acs`로 실행 스킵 (`focus.py:120`). 증거 없으면 전체 그래프 = fail-closed.
- **워커 툴콜 트레이스 전량 기록됨**: `execution.tool.started/completed`에 `tool_input`·`tool_result_text` 무절단 (`execution_event_emitter.py:965-995`). 이를 구조화 매니페스트로 투영하는 완성 함수가 이미 있다: `deliver_gate.load_ac_evidence_manifest()` (`harness/deliver_gate.py:172`) — 현재 관찰 전용으로 묶여 있을 뿐.
- ralph 정지 조건 세트(QA pass 0.80, max_generations 10, oscillation window 3, grade regressing, wall clock) — `ralph_loop.py:297-334`, `:523-542`.

## What Didn't Work / 현재 결함 (계획이 고칠 것)

- **답안지 verbatim 노출**: `atomic_prompt_builder.py:47-60` `_build_success_contract_block`이 워커 프롬프트에 verify_command와 `Expected output: <assertion>`을 그대로 넣는다. reward-hacking 초대장 (seed_2be2907edc07 포스트모템 참조).
- **재시도 힌트 빈약**: `_build_ac_retry_prompt()` (`parallel_executor.py:10565-10599`) = 실패 분류 라벨 1개 + error **마지막 500자** + (마지막 시도만) lateral 지시. 직전 시도의 툴콜·typed evidence·검증기 사유는 전달 안 됨.
- **실패 run은 eval 체인 안 됨**: `_run_succeeded()` 게이트 (`execution_handlers.py:582-586`) — 실패한 run이야말로 평가가 필요한데 그대로 반환.
- **eval→evolve 체인 부재**: 각 조각은 완성돼 있는데 연결이 없다.
- **LLM 평가 경로에서 `ac_results` 빔** (`mcp/server/adapter.py:2112-2119`) — 이러면 focus가 fail-closed로 전체 그래프 재실행. spec-verifier 경로만 채움.
- **evolve의 평가 호출이 bare except** (`evolution/loop.py:1946-1951`) — 실패 시 `evaluation_summary=None`으로 피드백 통째 소실.
- **TIMEOUT=0 함정**: `StartEvaluateHandler.deadline_seconds`에서 0은 "무한 대기"다 (`evaluation_handlers.py:2195-2201`). 체인 호출은 deadline을 안 넘겨 항상 1800s 고정 (`execution_handlers.py:2437-2443`).
- 평가 파이프라인 이벤트는 EventStore에 append되지 않음 — 소비 가능 표면은 잡 `result_meta`와 `lineage.generation.completed`의 `evaluation_summary`뿐.

---

## Next Steps

### Phase 1 — per-AC 은닉 + 트레이스 기반 힌트 루프 (run 내부)

1. `atomic_prompt_builder.py:34-61` `_build_success_contract_block` 수정: **description + expected_artifacts만 노출** (산출물 목록은 "뭘 만들 것인가"의 계약이므로 유지), `verify_command` 라인과 `Expected output:` 라인 삭제. "Run locally before completion" 문구도 삭제 — 대체 문구: "검증은 하네스가 독립 수행한다; 산출물 계약을 충족하라". 설정 분기 없음(무조건).
2. **힌트 빌더 신설** (예: `orchestrator/retry_hints.py`): 입력 = verify gate 실패 사유·명령 출력 tail(`:9860-9863`) + `deliver_gate.load_ac_evidence_manifest()`의 툴콜 매니페스트. 출력 = assertion-safe 힌트 — 누락 artifact 이름, exit code, stderr 발췌, 워커가 실제 실행한 명령 요약, "산출물 X는 만들었으나 검증 명령이 Y에서 실패" 수준. **output_assertion 문자열 누출 금지 필터 필수** (auto/pipeline.py의 `_SEED_QA_SENSITIVE_RE` 위생 패턴 참조). `_build_ac_retry_prompt`의 500자 tail을 이것으로 대체.
3. (2단계 레이어) **코치 리라이트**: 싼 모델 1콜로 {이전 지시문 + 매니페스트 + 힌트} → 수정된 지시문. 기존 모델/effort 에스컬레이션(`model_routing.py:486`, `effort_routing.py:273`)과 공존. 결정론 힌트(2번)가 기본이고 이건 선택.
4. 회귀 테스트: assertion 문자열이 초기·재시도 워커 프롬프트에 **절대 등장하지 않음**을 고정; 힌트에 매니페스트 유래 사실 포함 검증.
5. 주의: 답안지 은닉으로 수렴 라운드 증가 가능 → `ac_retry_attempts`(기본 2, `config/models.py:265`) 상향 검토와 세트로.

### Phase 2 — 실패 run도 eval 체인

1. `execution_handlers.py:2774`의 `_run_succeeded` 조건 완화 — 실패 run도 `_enqueue_chained_evaluation`. enqueue 실패가 run 결과를 뒤집지 않는 기존 fail-open 계약(`:651-668`) 유지.
2. `_enqueue_chained_evaluation`에 `deadline_seconds` 명시 전달(기본 1800) + "0=무한" 함정 주석.
3. 체인 eval의 multi-AC `checklist` meta가 Phase 3 evolve 입력이 되므로 계약 필드(특히 `failure_reason`) 스냅샷 테스트로 고정.

### Phase 3 — eval 실패 → evolve 자동 체인 (`ooo run` 한 번으로 수렴)

1. **원칙: 루프 재구현 금지** (`skills/ralph/SKILL.md:38`). run은 Gen1이고, 수렴은 ralph(=evolve_step 드라이버)에 위임한다. 형태: run 잡 → 체인 eval 잡 → (미승인 시) 체인 ralph 잡. `follow_result_job_keys` 체인을 eval 잡에 `chained_ralph_job_id`로 확장 — 호스트/옵저버는 기존 계약(`skills/run/SKILL.md:446-463`)대로 따라간다.
2. 훅: `StartEvaluateHandler` 러너 터미널에서 `final_approved=False`(또는 checklist 실패 존재) 시 ralph enqueue. 트리거 지점은 run 쪽이 아니라 **eval 잡의 완료 지점** (run 러너를 늘리지 않기 위해).
3. **Gen1 브리지 (최대 신규 작업)**: run의 seed + 체인 eval의 AC별 결과를 lineage 이벤트로 투영해 `evolve_step`이 replay로 Gen2를 시작할 수 있게 한다. **핵심 계약: `evaluation_summary.ac_results`를 반드시 채울 것** — LLM 경로에서 비는 문제(`adapter.py:2112-2119`)를 체인 eval의 checklist 결과로 메꾼다. 이게 채워져야 `focus.select_evolution_focus`가 실패 AC만 active로 만들고 통과 AC를 스킵한다.
4. `evolution/loop.py:1946-1951` bare except 수정 — eval 예외 시 피드백 소실 → 최소한 실패 사실과 사유를 `evaluation_summary`에 남기고 fail-closed 사유를 이벤트로.
5. 종료·예산: ralph 기존 정지 조건 재사용. **blocked는 이 예산 소진 시에만** — 그 시점의 blocked 이벤트는 이번 세션에 배선한 `auto.session.blocked`/attention relay 경로로 시끄럽게 표면화된다.
6. CLI `ooo run` 직접 실행 패리티는 후순위 — 서버 잡 경로(`ouroboros_start_execute_seed`)를 정본으로.

### Scope Out (하지 말 것)

- 공개수위 설정(config) — 오너가 명시적으로 거부. 무조건 은닉.
- single-AC 평가 meta 보강 — 체인 경로는 항상 multi-AC라 불필요.
- maestro-agent-sdk 백엔드 통합 — 별도 트랙. 이 루프의 라운드 단가를 낮추는 시너지(DeepSeek/Kimi 저가 프로바이더)만 기록해 둠: https://github.com/maestrojeong/maestro-agent-sdk

---

## Important Files

```
# Phase 1
src/ouroboros/orchestrator/atomic_prompt_builder.py:34-61   # 답안지 노출 지점 (수정 대상)
src/ouroboros/orchestrator/parallel_executor.py:10565-10599 # _build_ac_retry_prompt (대체 대상)
src/ouroboros/orchestrator/parallel_executor.py:9631-9902   # verify gate (실패 증거 소스)
src/ouroboros/harness/deliver_gate.py:172                   # 툴콜 매니페스트 투영 (재사용)
src/ouroboros/config/models.py:265                          # ac_retry_attempts

# Phase 2
src/ouroboros/mcp/tools/execution_handlers.py:2774,2390,582 # 체인 트리거/enqueue/_run_succeeded
src/ouroboros/mcp/tools/evaluation_handlers.py:999-1019     # multi-AC checklist meta
src/ouroboros/mcp/tools/evaluation_handlers.py:2059,2195    # deadline_seconds (0=무한 함정)

# Phase 3
src/ouroboros/evolution/loop.py:571,1436,1946               # evolve_step / 세대선택 / bare except
src/ouroboros/evolution/focus.py:147,120                    # 평가→실행범위 번역 / 통과 AC 스킵
src/ouroboros/ralph_loop.py:155,214,297-334                 # 루프 드라이버 / 정지 조건
src/ouroboros/mcp/server/adapter.py:2112-2119               # ac_results 빔 (메꿀 곳)
skills/run/SKILL.md:446-463                                 # follow_result_job_keys 호스트 계약
```

이번 세션 미커밋 변경(이 계획과 별개, 커밋 필요):
```
M skills/auto/SKILL.md                          M src/ouroboros/mcp/tools/attention_relay.py
M src/ouroboros/agents/socratic-interviewer.md  M src/ouroboros/mcp/tools/auto_handler.py
M src/ouroboros/auto/interview_driver.py        M tests/unit/auto/test_pipeline_lateral.py
M src/ouroboros/auto/pipeline.py                M tests/unit/mcp/tools/test_attention_relay.py
M src/ouroboros/cli/commands/auto.py            ?? src/ouroboros/auto/seed_preflight.py
?? tests/unit/auto/test_pipeline_seed_preflight.py  ?? tests/unit/auto/test_seed_preflight.py
```

---

## Notes

- **과거 결정과의 정합**: PR #174은 "정보 비대칭보다 평가 깊이"라고 결론냈지만 그것은 **평가자 층** 결정이다. 이 계획은 **실행 워커 층**의 답안지 노출 제거 — 층이 달라 충돌하지 않으며, 보류됐던 OctopusGarden #91 holdout의 경량 실현에 해당한다.
- **attempt_judged는 텔레메트리 전용** (`parallel_executor.py:9915-9921`) — 재시도 분기 권한은 `ACExecutionResult`에 있다. 힌트 배선 시 이벤트가 아니라 반환값 경로를 써라.
- **deliver gate는 "AC 성공/실패·재시도·라우팅을 절대 바꾸지 않는다"** 계약 (`parallel_executor.py:9508-9511`) — 매니페스트 **읽기**는 재사용하되 게이트의 판정 권한은 건드리지 말 것.
- 평가 파이프라인 자체는 EventStore에 안 남는다 — 프로그램 소비는 잡 `result_meta`(`ouroboros_job_result`)와 `lineage.generation.completed`로.
- 관련 메모리: `seed-preflight-gate-and-qa-escalation.md` (이번 세션 기반 작업), `fat-harness-verify-gate-authority.md` (#1591), `frugality-model-tier-routing-direction.md` (라운드 단가), `reflect-scoped-reexecution.md` (externally_satisfied_acs).
- 검증 명령: `uv run pytest tests/unit/auto/ tests/unit/orchestrator/ -q`, 오케스트레이터 쪽은 `tests/unit/mcp` 풀런 금지(실서버 누수 사고 전력 — 메모리 참조), 대상 파일 단위로.
