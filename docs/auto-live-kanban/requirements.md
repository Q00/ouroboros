# Auto Live Kanban Requirements

> Generated: 2026-08-06
> Status: Clarified from the implementation request

## Original Request

Auto 라이브 칸반은 동시에 여러 실행이 가능하므로 실행 목록을 제공하고, `run`과 `auto`의 안내가 실제 화면 흐름과 맞는지 확인한다. query parameter로 특정 실행 상세에 바로 들어갈 수 있게 하며, 목록에서는 goal 기반 제목을 잘라내거나 `strip()`하지 않는다.

## Clarified Specification

- 하나의 dashboard daemon이 여러 실행을 제공한다.
- 기본 dashboard URL(`/`)은 최신 실행으로 자동 이동하지 않고 최근 실행 목록을 보여준다.
- `/?run=<execution_id>`는 해당 실행의 live Kanban 상세 화면을 직접 연다.
- 목록에는 실행 ID, goal 원문, 상태, 단계/activity, AC 진행률, provider를 표시한다.
- 목록은 주기적으로 갱신되어 동시 실행을 함께 관찰할 수 있다.
- `ooo run`은 실행 시작 전에 pinned detail URL을 안내한다.
- `ooo auto`는 실행 ID가 생기기 전에도 목록 URL을 안내한다.

## Success Criteria

- 두 개 이상의 실행이 `/api/runs`와 목록 화면에 동시에 나타난다.
- goal의 앞뒤 공백과 줄바꿈이 reader/UI 경계에서 제거되지 않는다.
- 목록 항목을 클릭하면 `?run=` 상세 화면으로 이동한다.
- 기존 SSE 상세 화면과 snapshot 렌더링이 유지된다.
- 관련 단위 테스트와 정적 분석이 통과한다.
