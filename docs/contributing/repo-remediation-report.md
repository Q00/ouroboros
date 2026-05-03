# Repository Remediation Report

## 조사 범위

- 작업 디렉터리: `/Users/jaegyu.lee/Project/ouroboros-gemini3`
- AGENTS.md 적용 범위: 사용자 제공 컨텍스트 기준 저장소 내부 및 상위 경로에서 발견되지 않음.
- 조사 대상:
  - 버전 관리 대상 Python 패키지와 테스트 구성: `src/`, `tests/`, `pyproject.toml`
  - 주요 프로젝트 문서: `README.md`, `docs/contributing/`
  - 명시적 미완성 신호 검색: `TODO`, `FIXME`, `NotImplemented`, `placeholder`, `stub`, `pass`
  - 정적 검사와 테스트: ruff, MCP 단위 테스트, 전체 단위/통합/e2e 테스트
- 제외/주의 범위:
  - 기존에 있던 다수의 untracked 파일과 디렉터리는 사용자 또는 다른 에이전트 작업일 수 있어 무관한 파일은 수정하지 않음.
  - `apps/kart/node_modules/` 같은 생성/의존성 산출물은 코드 품질 조사 범위에서 제외함.

## 발견한 불완전/위험 코드

- `src/ouroboros/mcp/resources/handlers.py`의 MCP 리소스 핸들러가 실제 저장소 데이터를 읽지 않고 하드코딩된 예제 JSON을 반환하고 있었음.
- 후속 리뷰에서 `MCPServerAdapter`가 등록된 exact URI만 조회해 `ouroboros://seeds/{id}`, `ouroboros://sessions/{id}`, `ouroboros://events/{session_id}` 요청이 리소스 핸들러의 동적 URI 분기까지 도달하지 못하는 P1 문제가 확인됨.
- 후속 리뷰에서 `ouroboros://sessions/current`가 재구성된 `SessionTracker.last_message_time == None` 값만 기준으로 삼아 running session이 여러 개일 때 최신 activity 세션을 안정적으로 고르지 못하는 P1 문제가 확인됨.
- 재리뷰에서 `sessions/current` activity 계산이 `aggregate_type == "session"` snapshot만 반영해, `workflow.progress.updated`처럼 `data.session_id`로 연결되는 execution-scoped event가 최신 activity인 running session을 놓칠 수 있는 문제가 확인됨.
- 재리뷰에서 `sessions/current` 최종 선택이 offset이 섞인 ISO timestamp 문자열을 lexicographic 비교해 UTC 기준 최신 세션을 잘못 고를 수 있는 문제가 확인됨.
- 영향:
  - `ouroboros://seeds`, `ouroboros://sessions`, `ouroboros://events` 리소스가 실제 seed/session/event 상태와 불일치함.
  - 특정 seed/session/event 조회도 존재 여부를 검증하지 않고 예제 데이터를 조립해 반환할 수 있었음.
  - 같은 영역을 겨냥한 `tests/unit/mcp/resources/test_handlers.py`가 untracked 상태로 존재했고, 현재 구현과 생성자/응답 계약이 맞지 않는 상태였음.
  - MCP 서버 어댑터 경유 호출에서는 특정 seed/session/event URI가 not found로 반환될 수 있었음.
  - 여러 active session이 있을 때 `sessions/current`가 최신 activity가 아닌 오래된 세션을 반환할 수 있었음.
  - 오래된 running session에 더 최신 execution-scoped progress가 있어도, 더 늦게 시작한 session aggregate activity가 우선될 수 있었음.

## 수정한 파일 목록

- `src/ouroboros/mcp/resources/handlers.py`
- `src/ouroboros/mcp/server/adapter.py`
- `tests/unit/mcp/resources/test_handlers.py`
- `tests/unit/mcp/server/test_adapter.py`
- `docs/contributing/repo-remediation-report.md`

## 수정 요약

- `SeedsResourceHandler`
  - `seed_dir`를 주입 가능하게 추가하고 기본값을 `~/.ouroboros/seeds`로 설정함.
  - `*.yaml`, `*.yml` seed 파일을 실제로 로드해 목록과 특정 seed 상세를 JSON으로 반환하게 변경함.
  - 존재하지 않는 seed는 `MCPResourceNotFoundError`를 반환하게 변경함.
- `SessionsResourceHandler`
  - `EventStore` 주입을 지원하도록 변경함.
  - `SessionRepository`로 세션을 재구성해 목록, 현재 세션, 특정 세션을 반환하게 변경함.
  - 저장소가 주입되지 않은 기본 핸들러는 예제 데이터 대신 빈 상태를 반환함.
- `EventsResourceHandler`
  - `EventStore` 주입을 지원하도록 변경함.
  - 최근 이벤트와 세션 관련 이벤트를 실제 EventStore 쿼리 결과로 반환하게 변경함.
- 공통 직렬화 헬퍼를 추가해 session/event 응답 구조를 안정화함.
- `MCPServerAdapter`
  - exact URI 조회가 실패하면 등록된 base URI 중 가장 긴 prefix를 찾아 child URI를 해당 핸들러로 라우팅하게 변경함.
  - FastMCP 등록 경로에서도 base resource에 `{resource_id}` 템플릿을 함께 등록해 실제 MCP transport에서 child URI가 도달할 수 있게 함.
- `SessionsResourceHandler`
  - `EventStore.get_session_activity_snapshots()`의 `last_activity`/`start_time`을 세션 응답에 병합하고, `sessions/current` 선택 기준을 해당 activity로 변경함.
  - `last_message_time`이 비어 있는 재구성 세션이 여러 개여도 최신 이벤트 activity를 가진 running/paused session을 선택하도록 회귀 테스트를 추가함.
  - 각 session snapshot에 `query_session_related_events(session_id=..., execution_id=..., limit=1)`의 최신 timestamp를 병합해 `SessionRepository.reconstruct_session()`과 같은 related-event 범위의 execution-scoped activity까지 `sessions/current` 선택 기준에 반영함.
  - 오래된 running session의 `workflow.progress.updated` execution event가 더 최신 activity인 경우 해당 session을 current로 고르는 회귀 테스트를 추가함.
  - `sessions/current` 최종 선택 키가 `last_activity`/`last_message_time`/`start_time`을 timezone-aware `datetime`으로 파싱해 비교하도록 변경하고, offset-equivalent 및 lexicographic 순서가 다른 timestamp 회귀 테스트를 추가함.

## 수정 의도

- MCP 리소스 API가 데모 데이터가 아니라 Ouroboros의 실제 영속 계층을 반영하도록 만들어 사용자와 에이전트가 잘못된 상태를 기반으로 판단하지 않게 하는 것이 목적임.
- 기본 생성 경로는 기존 등록 코드와 호환되도록 유지하되, 실제 저장소가 없는 경우 허위 데이터 대신 빈 결과를 반환하도록 해 오진 위험을 줄였음.
- seed 파일과 EventStore를 테스트에서 직접 주입할 수 있게 만들어 리소스 핸들러의 동작을 독립적으로 검증 가능하게 함.

## 검증 결과

- `uv run ruff check src/ouroboros/mcp/server/adapter.py src/ouroboros/mcp/resources/handlers.py tests/unit/mcp/server/test_adapter.py tests/unit/mcp/resources/test_handlers.py`
  - 결과: 통과
- `uv run mypy src/ouroboros/mcp/server/adapter.py src/ouroboros/mcp/resources/handlers.py`
  - 결과: Success: no issues found in 2 source files
- `uv run mypy src`
  - 결과: Success: no issues found in 272 source files
- `uv run pytest tests/unit/mcp/server/test_adapter.py::TestMCPServerAdapterResources tests/unit/mcp/server/test_adapter.py::TestServeTransport::test_fastmcp_registers_base_resource_uri_template tests/unit/mcp/resources/test_handlers.py`
  - 결과: 15 passed
- `uv run pytest tests/integration/mcp/test_server_adapter.py::TestMCPServerAdapterResourceReading`
  - 결과: 4 passed
- `uv run pytest tests/unit/mcp tests/integration/mcp/test_server_adapter.py::TestMCPServerAdapterResourceReading`
  - 결과: 749 passed
- `uv run pytest tests/unit/mcp/resources/test_handlers.py`
  - 결과: 9 passed
- `uv run ruff check src/ouroboros/mcp/resources/handlers.py tests/unit/mcp/resources/test_handlers.py`
  - 결과: All checks passed!
- `uv run mypy src/ouroboros/mcp/resources/handlers.py`
  - 결과: Success: no issues found in 1 source file
- `uv run pytest tests/unit/mcp`
  - 결과: 745 passed

## 남은 리스크

- 저장소에는 기존 untracked 파일이 많이 남아 있으며, 그중 일부는 별도 기능 개발 또는 생성 산출물로 보인다. 이번 작업에서는 사용자/다른 에이전트 변경을 보존하기 위해 무관한 untracked 파일을 수정하지 않았다.
- `tests/unit/mcp/resources/test_handlers.py`는 작업 시작 시점부터 untracked 상태였으나, 이번 후속 수정에서 회귀 테스트를 추가/정리했다. 해당 테스트를 정식으로 포함할지는 별도 스테이징 판단이 필요하다.
- 기본 `OUROBOROS_RESOURCES`는 EventStore를 주입하지 않은 상태로 생성되므로 기본 등록만으로는 세션/이벤트가 빈 결과를 반환한다. 실제 MCP 서버 등록 경로에서 런타임 EventStore를 주입하는 후속 연결이 필요할 수 있다.
