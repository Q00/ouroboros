# Cursor Support — TODO / Known Issues

## ACP 기반 아키텍처 (v0.26.0)

Cursor 백엔드는 `cursor-agent acp` (Agent Client Protocol) 기반으로 동작합니다.
하나의 `cursor-agent` 프로세스를 유지하며, LLM 호출과 에이전트 실행 모두 같은 세션에서 처리합니다.

```
CursorACPClient (공유 프로세스)
├── CursorACPAdapter  — LLM 호출 (interview, scoring, seed 생성)
└── CursorACPRuntime  — 에이전트 실행 (ooo run)
```

## 해결된 이슈

- ✅ 서브에이전트 runtime backend 자동 감지 (ACP 전환으로 해결)
- ✅ Cursor 단독 설치 흐름 (`ouroboros setup --runtime cursor`)
- ✅ LLM 호출 속도 (프로세스 재생성 → ACP 세션 재사용으로 4-8배 개선)
- ✅ ooo run 실행 시 런타임 정보 표시

## 추후 개선 필요

### 모델 선택
- ACP `session/set_config_option`으로 모델 변경 가능 (해결됨)
- IDE의 최근 사용 모델을 `state.vscdb`에서 읽어 자동 매핑
- IDE 모델명(예: `gpt-5.4-medium`)과 ACP 모델 ID(예: `gpt-5.4[reasoning=medium,...]`)가 달라 매핑 필요
- 매핑 실패 시 `auto` 폴백

### MCP 인터뷰 첫 턴 속도
- brownfield 스캔 + LLM 요약 + ambiguity scoring + 질문 생성이 동기 묶음
- ACP 세션 재사용으로 개선되었으나, brownfield 스캔 자체가 1분+ 소요 가능
- 장기: lazy evaluation (첫 턴은 빠른 질문만, scoring은 지연) 또는 ACP 세션 기반 인터뷰 재설계

### ooo run 중 간헐적 에러
- `invalid_request_error` — reasoning 블록 관련 간헐적 에러
- ouroboros의 서브에이전트 retry 로직 확인 필요
