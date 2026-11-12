# 호스트 주도 실행 런타임

`host` 에이전트 런타임은 외부 CLI를 실행하는 대신 호출한 MCP 호스트에 실행을 위임합니다. DeepSeek Harness처럼 자체 서브에이전트를 생성할 수 있지만 설치 가능한 실행 런타임을 제공하지 않는 호스트를 위한 것입니다.

## 런타임 활성화

```bash
ouroboros setup --runtime host --non-interactive
```

또는 MCP 서버 프로세스에 사용할 런타임을 설정합니다.

```bash
export OUROBOROS_AGENT_RUNTIME=host
```

DSH 플러그인 번들의 이 변수 기본값은 `host`입니다. `orchestrator.runtime_profile.stages.execute` 값이 설정되어 있으면 그 값이 우선하므로, 예상과 다른 백엔드가 실행될 때는 런타임 프로필을 확인하세요.

설정 후에는 MCP 호스트 채팅을 유지한 채 다음을 입력합니다.

```text
ooo run
```

호스트 명령은 작업 추적 MCP 실행 경로를 호출합니다. 이 런타임에서는 터미널에서 `ouroboros run`을 실행하지 마세요. 터미널에는 디스패치를 받아 제출할 호스트 루프가 없습니다.

## 실행 흐름

```text
ouroboros_start_execute_seed
  -> HostDispatchRuntime이 실행 요청 하나를 대기 상태로 둠
  -> ouroboros_job_wait이 meta.pending_host_dispatches를 반환
  -> MCP 호스트가 실행 가능한 디스패치마다 서브에이전트 하나를 생성
  -> ouroboros_submit_fanout_results가 작업자 결과를 전달
  -> 서버 측 검증 게이트가 작업을 평가
  -> ouroboros_job_wait / ouroboros_job_result가 작업의 최종 상태를 반환
```

`ouroboros_job_status`는 관찰용으로 대기 중인 디스패치 식별자를 보여주지만, 실행 가능한 알림을 가져가지는 않습니다. 실행 호스트는 작업을 받기 위해 `ouroboros_job_wait`을 사용해야 합니다.

## 제출 계약

각 대기 항목은 다음을 제공합니다.

- `dispatch_id` / `fanout_id`
- `session_id`
- `result_correlation_key`
- `subagents[0].prompt`
- `subagents[0].context.working_directory`
- `submit_tool`

성공한 작업자 결과는 다음과 같이 제출합니다.

```json
{
  "fanout_id": "fanout_...",
  "session_id": "orch_...",
  "correlation_key": "result",
  "results": [
    {
      "key": "result",
      "content": "Worker final output"
    }
  ]
}
```

작업자가 생성되지 않았다면 명시적인 종료 실패를 제출합니다.

```json
{
  "key": "result",
  "undispatched": true
}
```

`null`, 빈 문자열, 빈 객체, 배열, 불리언이 아닌 `success` 표시, 실질적인 출력 없이 성공 표시만 있는 객체는 거부되며 해당 활성 디스패치를 소비하지 않습니다. 호스트는 같은 `fanout_id`를 수정해 재시도할 수 있습니다.

## 수명 주기 보장

- 모든 디스패치는 비어 있지 않은 세션 ID에 속합니다.
- 상관관계 또는 세션이 일치하지 않으면 안전하게 거부합니다.
- 중복되었거나 취소·대체·완료된 제출은 더 이상 유효하지 않습니다.
- 활성 디스패치는 한 번만 알립니다. 알림이나 작업자가 사라지면 시도 기한에 만료되며, 재시도는 아직 실행 중일 수 있는 작업을 다시 실행하지 않고 새 디스패치 ID를 만듭니다.
- 디스패치의 절대 기한 이후 도착한 결과는 전달되지 않습니다.
- 대기 중인 작업은 heartbeat를 내보내므로 실행기의 정체 감지기가 사람이 응답하는 속도의 호스트를 성급히 취소하지 않습니다.
- 호스트 실패 텍스트는 구조화된 메타데이터로 저장되며 런타임 할당량 분류를 조작할 수 없습니다.
- 작업자 프롬프트는 반드시 사용해야 할 작업용 worktree를 지정하며, 검증도 같은 작업 디렉터리에서 실행됩니다.
- 숨겨진 검증 assertion은 호스트에 전달하는 페이로드에 직렬화하지 않습니다.

## 지원하지 않는 경로

- 터미널 `ouroboros run`은 `host`를 거부합니다. MCP 호스트 채팅에서 `ooo run`을 사용하세요.
- 직접 호출하는 `ouroboros_execute_seed`는 `host`를 거부합니다. 호스트용 `ooo run` 명령은 작업 추적 `ouroboros_start_execute_seed` 경로를 사용합니다.
- Evolve와 Ralph는 작업의 전체 경로에서 세션 범위를 찾아낼 수 있게 되기 전까지 `host`를 거부합니다.

## 프로세스 소유권

Fan-out 상관관계 레코드는 파일에 저장되지만, 활성 대기자는 서비스를 제공하는 MCP 프로세스에 속합니다. 따라서 호스트 기반 작업은 분리된 worker로 옮기지 않고 해당 프로세스에 남습니다. 여러 단계를 혼합한 `ooo auto`는 분리가 안전한지 결정하기 전에 실제 실행 단계 런타임을 확인합니다.

## 수동 DSH 스모크 테스트

플러그인을 활성화한 상태로 DSH를 시작한 뒤 다음을 실행합니다.

```bash
node scripts/live-dsh-interview.cjs \
  "ooo interview deepseek 하네스를 이용해서 마케팅 에이전트를 만들고싶어"
```

환경 변수 재정의:

- `DSH_URL`: DSH 웹 URL, 기본값 `http://127.0.0.1:3081`
- `PLAYWRIGHT_MODULE`: 모듈 이름 또는 Playwright 패키지 절대 경로
- `LIVE_INTERVIEW_OUT`: 출력 디렉터리, 기본값은 임시 디렉터리

스크립트는 최종 페이지 텍스트와 전체 페이지 스크린샷을 기록한 다음, Ouroboros 인터뷰 도구와 모호성 점수가 관찰됐는지 출력합니다.
