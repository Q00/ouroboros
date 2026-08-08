# Handoff Document

> Last Updated: 2026-08-09
> Session: auto blocked 최소화 오버홀 — transient 재시도·체인 패리티·blocked UX

---

## Status of the previous plan (2026-08-06 세션)

이전 HANDOFF의 hidden-checklist convergence loop 계획(답안지 은닉 + 트레이스 힌트 루프 + run→eval→evolve 단일 체인)은 **PR #1916으로 머지 완료**. 후속: #1921(detached evolve 신호 릴레이), #1913(lifecycle 간 verified evidence 보존). 구현 산출물: `orchestrator/retry_hints.py`, `orchestrator/contract_redaction.py`, `mcp/tools/run_evaluate_chain.py`, `mcp/tools/evaluate_ralph_chain.py`, docs/hidden-checklist-convergence/.

이전 세션의 별도 미커밋 작업(seed preflight 게이트 + blocked 이벤트 관측성)은 stash pop 충돌 해소 후 `feat/auto-seed-preflight-blocked-obs` 브랜치의 `2fe806a61`로 커밋됨. 충돌 해소 핵심: watchdog이 새 EventStore 대신 `runtime_event_store`를 공유해야 blocked 이벤트가 attention relay에 도달한다 (upstream의 dashboard URL 픽커는 유지).

---

## Goal (이번 세션)

`ooo auto`의 빈발 blocked를 구조적으로 줄인다. Vision #1157 계약: "복구 가능한 프로세스/인터뷰 비수렴은 BLOCKED로 끝나면 안 된다. blocked는 예산 소진·안전·권한·명시적 인간 확인의 최후 수단."

## Root causes (전수 조사 결과 — file:line 증거 확보)

pipeline.py에만 mark_blocked 76곳, interview_driver.py 13곳. 분류 결과의 핵심 비대칭: **예산(BUDGET) 클래스만 실제 bound가 있고, HEALABLE/INFRA 클래스는 카운터도 재시도도 없이 첫 실패에 즉사한다** — 시스템이 이미 깊게 고민한 곳(repair 5회, evaluate 3라운드, persona 5종)에서는 재시도하면서, 고민 안 한 곳(단발 LLM 호출)에서는 즉시 포기하는 구조.

1. **체인 패리티 부재 (최대 발견)**: `auto/adapters.py:270-271`(HandlerRunStarter)과 `:334-335`(HandlerSynchronousRunStarter)가 `auto_evaluate/auto_evolve: False` 하드코딩. `snapshot_run_successor_policy`(run_evaluate_chain.py:28-33)는 명시적 bool을 config(기본 True)보다 우선하므로 **기본 모드 auto의 run은 #1916 수렴 체인에서 완전히 배제**된다. 의도 주석("auto가 자체 평가 경로 소유")은 complete_product=True에만 참 — 기본 모드는 run handoff 후 auto가 끝나므로 아무도 평가하지 않았다. 계약 테스트 test_adapters_client_gates.py:38-39,100-101이 이 게이트를 고정하고 있었음.
2. **transient 즉사 3연대 (pipeline.py)**: seed QA(2909/2920/2932), evaluator(3372/3381/3388), lateral(3792/3801/3808) — 타임아웃/예외/`.error` 모두 재시도 0회로 mark_blocked. RFC #809 주석 스스로 "transient는 예산을 소모하면 안 된다"고 명시하면서 재시도는 미구현.
3. **인터뷰 한 라운드 즉사**: interview_driver.py 1278/1291 — 한 라운드의 60s 타임아웃/예외가 max_rounds=50 예산을 통째로 버림. backend.resume 브랜치(702-706)는 start 브랜치의 closure 폴백(1350)을 시도하지 않음.
4. **resume dead end 버그**: `_recoverable_phase_for_tool`의 REVIEW 집합에 `seed_reviewer` 누락 — pre-run 리뷰 타임아웃(순수 transient)이 영구 dead end. seed_repairer는 같은 이유로 이미 추가돼 있었음(주석 4867).
5. **blocked UX**: `_print_result`는 prose blocker만 출력, resume_capability NONE + goal 부재 시 다음 단계 안내가 아예 없음.

## What was done (이번 세션, Sonnet 5 서브에이전트 4개 분업)

- **WS-A** (`auto/pipeline.py`): 사이트 2의 transient 3연대에 bounded 재시도(`_TRANSIENT_TOOL_ATTEMPTS=3`, 백오프 (1,5)s, deadline 체크 유지). 소진 시에만 기존 문구로 블록 + 신설 error_code `seed_qa_transient_exhausted`/`evaluator_transient_exhausted`/`lateral_transient_exhausted` + `auto.seed_qa.blocked` 이벤트. `seed_reviewer`를 REVIEW resume 집합에 추가. 예산 카운터 불소모 보장.
- **WS-B** (`cli/commands/auto.py`, `skills/auto/SKILL.md`): blocked 터미널에 구조화 패널(stage/reason code/open questions/정확한 resume 명령), non-resumable 시 명시 안내. SKILL.md에 신설 코드 문서화 + 호스트 플레이북(transient_exhausted → 자동 resume 1회 후에만 유저 에스컬레이션; fact-gap → open questions를 답변형 질문으로 제시, YAML 수동 편집 요구 금지).
- **WS-C** (`auto/adapters.py`): HandlerRunStarter의 하드코딩 제거 → 기본 모드 auto run이 config(`execution.auto_evaluate/auto_evolve`, 기본 ON)를 따라 #1916 체인 편입. HandlerSynchronousRunStarter(complete_product)는 자체 EVALUATE/RALPH 소유라 False 유지. **오너 확인 필요한 행동 변경.**
- **WS-D** (`auto/interview_driver.py`): 라운드 호출에 bounded 재시도(라운드 예산 불소모), backend start/resume 재시도 + resume 브랜치에도 closure 폴백 확장. error_code `interview_round_transient_exhausted`/`interview_backend_transient_exhausted`.

(최종 테스트/머지 상태는 세션 마지막 커밋 메시지 참조.)

## What was deliberately NOT touched

- run/ralph starter의 2회 제한(1707/1753/1775/1893) — 중복 enqueue 방지 설계. resume 시 같은 blocker 재출력되는 자기루프는 알려진 트레이드오프.
- seed_preflight 게이트(2871) — 오너 결정: 사실 부족은 사람에게 묻는다. 수리는 UX(질문 제시)로.
- `seed_qa_ambiguity_unrepairable` — 점수 게이밍 제거의 짝. 블록이 정답.
- Ralph stop_reason 4종(oscillation 제외) — 예산 소진, 직접 BLOCKED 유지 (`_maybe_route_ralph_oscillation_to_lateral` 문서화된 결정).

## Known remaining gaps (다음 세션 후보)

1. **stagnation×safe-default persona 고갈**: stagnation 개입 2회가 CONTRARIAN·ARCHITECT를 소모하면 safe-default lateral이 첫 호출에 `unstuck_exhausted`(시도 0회) — persona 풀 분리 필요 (interview_driver.py 1746-1748 × 1893-1896).
2. **unmapped tool_name dead end 잔여**: `auto_pipeline`(6곳)·`probe_runner`·`intent_guard`·`runtime_watchdog`·`reference_candidate_bridge` 등은 여전히 resume 불가. 특히 804(ledger open gaps)는 Vision #1157이 명시한 "인터뷰 비수렴 BLOCKED 금지" 위반.
3. **`recovery_guard_tripped` sticky 재블록**(3239/3679): 새 artifact hash 없이는 resume이 verbatim 재출력.
4. #1650 인터뷰 UX 역전(시스템이 생각하고 유저는 고르기) — 별도 트랙, 자식 이슈 #1638→#1652→#1651→#1653→#1654/5→#1656.

## Important Files

```
src/ouroboros/auto/pipeline.py          # transient 재시도 3사이트 + resume 매핑 (5,404줄)
src/ouroboros/auto/adapters.py          # 체인 패리티 (270, 334)
src/ouroboros/auto/interview_driver.py  # 라운드/백엔드 재시도
src/ouroboros/auto/resume_render.py     # resume 안내 렌더러 (WS-B가 CLI에서 소비)
src/ouroboros/cli/commands/auto.py      # blocked 패널 + _print_result(1406-1497)
src/ouroboros/mcp/tools/run_evaluate_chain.py    # snapshot_run_successor_policy(28-33)
src/ouroboros/mcp/tools/execution_handlers.py    # 체인 트리거(2644-2656)
tests/unit/auto/test_adapters_client_gates.py    # 체인 게이트 계약 테스트
```

## Notes

- 검증: `uv run pytest tests/unit/auto/ tests/unit/cli/ -q` + relay/start_auto 단일 파일. **tests/unit/mcp 풀런 금지**(실서버 누수 사고 전력).
- blocked 관측성 배선(auto.session.blocked/preflight/qa → attention relay)은 `2fe806a61`에 포함 — emitter와 relay가 같은 EventStore를 공유해야 동작.
- 관련 메모리: seed-preflight-gate-and-qa-escalation.md, hidden-checklist-convergence-plan.md(완료 처리 필요), auto-freedom-and-convergence-overhaul.md.
