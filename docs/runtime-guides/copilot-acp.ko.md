# GitHub Copilot ACP 런타임 (실험적 기능)

Copilot의 Agent Client Protocol을 사용하면 `copilot -p`의 최종 응답을 기다리지 않고 **작업 실행 중에** 활동을 볼 수 있습니다.

> GitHub는 ACP를 **공개 미리 보기**로 설명합니다. Ouroboros는 기존 `copilot` 백엔드 뒤에 이 기능을 격리합니다. [CLI 전송](copilot.ko.md)은 계속 기본값이며 제거되지 않습니다. 이 구현은 **stdio만** 사용합니다.

## ACP 활성화

[Copilot CLI 사전 조건](copilot.ko.md#사전-조건)에 더해 `copilot --acp --stdio`, 프로토콜 버전 1, `session/new`, `session/prompt`를 지원하는 Copilot 바이너리가 필요합니다. Copilot CLI 1.0.83은 Linux에서 스모크 테스트했습니다. 추정한 최소 버전이 아니라 핸드셰이크로 기능 호환성을 확인합니다.

Ouroboros를 시작하기 전에 조직에서 승인한 Copilot 계정으로 인증하세요. 새 API 키, TCP 리스너, 디스플레이, 작업 실행 중 브라우저 세션, Node 패키지 또는 ACP SDK 의존성은 필요하지 않습니다.

```bash
ouroboros setup --runtime copilot
ouroboros config set orchestrator.copilot_transport acp
```

동등한 `~/.ouroboros/config.yaml` 설정:

```yaml
orchestrator:
  runtime_backend: copilot
  copilot_transport: acp       # cli (default) | acp
  copilot_acp_fallback: true   # guarded fallback before prompt submission only
llm:
  backend: copilot
```

현재 프로세스에만 적용하려면:

```bash
OUROBOROS_AGENT_RUNTIME=copilot \
OUROBOROS_COPILOT_TRANSPORT=acp \
ouroboros run workflow seed.yaml
```

백엔드 이름은 `copilot-acp`가 아니라 여전히 **`copilot`**입니다. 기존 CLI 경로, 모델 검색, 역할/프로필 선택, 사용자 지침, setup/MCP 등록을 재사용합니다. `CopilotCliLLMAdapter`는 interview, QA 등의 LLM 전용 호출에 계속 `-p`를 사용하며, ACP가 바꾸는 것은 **에이전트 실행 전송 방식**입니다. 전송 방식을 바꾼 후 이미 실행 중인 Ouroboros MCP 서버를 재시작하세요.

| 설정 | 기본값 | 의미 |
|---------|---------|---------|
| `orchestrator.copilot_transport` / `OUROBOROS_COPILOT_TRANSPORT` | `cli` | `cli` 또는 `acp` 선택 |
| `orchestrator.copilot_acp_fallback` / `OUROBOROS_COPILOT_ACP_FALLBACK` | `true` | 아래 설명한 제한적 폴백 허용 |
| `orchestrator.copilot_cli_path` / `OUROBOROS_COPILOT_CLI_PATH` | PATH 검색 | 두 전송 방식에서 같은 실행 파일 사용 |

신뢰된 환경 변수 재정의가 YAML보다 우선합니다. 비어 있지 않은 잘못된 전송 방식이나 불리언 재정의는 명시적으로 실패합니다. 신뢰할 수 없는 프로젝트 `.env` 파일은 전송 방식이나 폴백 정책을 바꿀 수 없습니다.

## 스트리밍 내용

Copilot ACP는 JSON-RPC `session/update` 알림을 내보냅니다. 이는 Copilot SDK의 `assistant.message_delta` / `tool.execution_start` 와이어 이벤트와 **다릅니다**. 변환기는 실제 ACP의 `params.update.sessionUpdate` 구분자를 사용합니다.

| ACP 입력 | 정규화된 `AgentMessage` | `runtime_event_type` |
|-----------|---------------------------|----------------------|
| 텍스트가 있는 `agent_message_chunk` | assistant, 정확한 `content_delta` | `assistant.message_delta` |
| `tool_call` | assistant 도구 envelope | `tool.started` |
| 종료 상태가 아닌 `tool_call_update` | system, `subtype=tool_progress` | `tool.progress` |
| `completed` 또는 `failed`인 `tool_call_update` | tool, `subtype=tool_result` | `tool.result` |
| `plan` | system 상태 | `agent.plan` |
| `session/request_permission`과 클라이언트 결정 | system 감사 기록 | `acp.permission_resolved` |
| `stopReason=end_turn`인 `session/prompt` 응답 | 최종 결과 | `turn.completed` |
| 기타 종료 이유 / 프로토콜 실패 | 오류 결과 | `turn.failed` |

실제로 관측한 페이로드의 세션/도구 ID를 줄인 예시:

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call","toolCallId":"toolu_…","title":"Run two-stage python print command","kind":"execute","status":"pending","rawInput":{"command":"python3 -c \"…\""}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","content":[{"type":"content","content":{"type":"text","text":"ACP_STAGE_1\n"}}]}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","content":[{"type":"content","content":{"type":"text","text":"ACP_STAGE_1\nACP_STAGE_2\n"}}]}}}
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn","usage":{"inputTokens":12502,"outputTokens":196,"totalTokens":12698,"thoughtTokens":0,"cachedReadTokens":6163,"cachedWriteTokens":6335}}}
```

두 셸 진행 스냅샷은 prompt 응답 전에 4초 간격으로 도착했습니다. 셸 진행은 **누적 출력**이므로 독립된 출력 델타라고 잘못 표시하지 않고 `tool_output`으로 저장합니다.

`status=completed`는 **도구가 끝났음**을 뜻하며 셸 명령의 성공을 뜻하지 않습니다. 관측된 종료 출력에는 구조화된 명령 상태가 포함됩니다.

```json
{"rawOutput":{"content":"…\n<shellId: 0 completed with exit code 1>","contents":[{"type":"shell_exit","shellId":"0","exitCode":1,"outputTruncated":false,"cwd":"…","outputPreview":"…"}]}}
```

Ouroboros는 이를 `exit_code=1`과 오류 도구 결과로 보존합니다. 구조화된 종료 상태가 없을 때 끝의 Copilot 셸 종료 footer를 호환성 폴백으로 사용합니다. 따라서 에이전트가 나중에 오류를 고치고 전체 턴이 성공해도 테스트 실패를 볼 수 있습니다.

관측된 다른 업데이트는 `agent_thought_chunk`, `usage_update`, `config_option_update`, `available_commands_update`입니다. 비공개 사고 청크는 작업 활동으로 노출하거나 저장하지 않습니다. 설정과 명령 검색은 작업 텍스트가 아닙니다. `usage_update.used/size`는 과금 토큰이 아닌 컨텍스트 창을 나타내며, 검증된 prompt 결과 카운터만 기존 usage 필드에 매핑합니다.

## 아키텍처와 저장

```text
Herdr / SSH / Coder VM (optional environment management)
  → Ouroboros AgentRuntime
    → CopilotAcpRuntime
      → CopilotAcpClient → copilot --acp --stdio
      → CopilotAcpEventTranslator → AgentMessage
    → existing runtime projection + ExecutionEventEmitter
    → existing EventStore → live consumers / history / replay
```

UI 전용 이벤트 경로나 두 번째 데이터베이스 스키마는 없습니다. 순차 실행기와 병렬 실행기 모두 일반 진행 emitter를 통해 각 assistant 델타와 도구/진행/결과 이벤트를 저장합니다. 공백, 도구 ID, 전송 메타데이터와 사용 가능한 부모/에이전트 상관관계는 projection 이후에도 유지됩니다. 도구 진행은 추가 도구 호출로 계산하지 않습니다.

각 호출은 프로세스 하나, 네이티브 세션 하나, prompt 하나를 소유합니다. ACP는 `session/new.cwd`로 작업공간을 지정하며 클라이언트 파일시스템/터미널 기능을 제공하거나 이 전송 방식에서 추가 MCP 서버를 공급하지 않습니다. 기존 `ooo` skill 가로채기는 Ouroboros에서 유지됩니다.

프레임은 8 MiB, 도구 출력 미리보기는 64 KiB, 최종 답변 조합은 2 MiB로 제한하며 초과 시 `response_truncated` 메타데이터가 표시됩니다. 큰 assistant 청크는 재생 텍스트 손실 없이 제한된 델타로 나눕니다. 클라이언트는 stderr를 별도로 비우고 시작/유휴 대기를 제한합니다. 런타임 생성자 시간 제한은 재정의할 수 있으며 기본값은 시작 60초, prompt 중 stdout 비활동 300초입니다. 이는 작업 전체를 5분으로 제한한다는 뜻이 아닙니다.

취소, 반복자 종료, live handle 종료 시 작업이 남아 있으면 `session/cancel`을 보내고 stdin을 닫습니다. 정상 종료가 실패하면 소유한 자식/프로세스 그룹을 회수합니다. 제출 여부가 불확실한 prompt는 재실행하지 않습니다.

## 권한과 인증

- 네이티브 `--available-tools=...` 필터는 **`tools=[]`**를 포함해 작업의 도구 envelope를 적용합니다. 예를 들어 Ouroboros의 `Read → view`, `Glob → glob`, `Grep → grep`, `Bash → bash` 및 셸 세션 제어 도구로 변환합니다. Copilot 네이티브 검색 도구는 `rg`가 아닌 `grep`입니다. **관측된 미리 보기 동작:** CLI 1.0.83은 `--available-tools=`를 무제한으로 해석했고 중첩 exclusion도 명시적으로 허용한 도구를 제거하지 못했습니다. 따라서 ACP는 호출마다 일치하지 않는 nonce allow-list 항목으로 도구 없음을 적용합니다. 텍스트 답변은 유지하면서 네이티브 도구가 비활성화되는 것을 확인했습니다. 초기 비활성/알 수 없는 도구 알림은 최종 답변이 아닌 상태로 정규화합니다. 이 우회는 ACP와 그 제한적 폴백에만 적용하며 기존 CLI 전송은 바꾸지 않습니다.
- `default`는 기존 Copilot 정책처럼 도구를 노출하지 않습니다. `acceptEdits`는 요청되고 이해된 로컬 작업만 허용합니다. 이 모드에서 envelope를 생략하면 Read/Glob/Grep/Edit/Write/Bash가 기본입니다.
- 실행기가 요청한 `bypassPermissions`는 의도적으로 **`acceptEdits`로 제한**합니다. 매개변수 협상은 이를 `translated`로 보고합니다. ACP는 `--yolo`, `--allow-all`, `--allow-all-tools`를 추가하지 않습니다.
- 서버의 권한 요청에는 **`allow_once`** 또는 취소/거부로만 응답하고 영구 승인하지 않습니다. 알 수 없는 권한 유형, 다른 세션, 심볼릭 링크 탈출을 포함해 정규 작업공간 밖의 요청 경로는 거부합니다. 쓰기에는 식별 가능한 대상이 필요합니다.
- 이는 **OS 샌드박스가 아닌** 도구/승인 정책입니다. 특히 Bash를 명시적으로 허용하면 로컬 사용자 권한으로 셸 명령이 실행됩니다. `--add-dir`은 일반적인 파일시스템 또는 네트워크 격리 수단이 아닙니다. 신뢰할 수 없는 작업은 적절히 격리된 작업공간에서 수행하세요.

Ouroboros는 기존 Copilot 인증 환경을 유지하며 토큰을 YAML에 복사하거나 계정을 몰래 바꾸지 않습니다. 오래된 `GITHUB_TOKEN`은 정상 CLI 로그인을 덮어쓸 수 있습니다. 이것이 진단된 원인이라면 자식 명령에서 그 유효하지 않은 변수만 제외하거나 raw probe의 명시적 `--ignore-github-token` 옵션을 사용하세요. ACP 핸드셰이크나 무관한 GitHub Enterprise 호스트에 대한 `gh` 로그인만으로 Copilot 승인이 증명되지는 않습니다. 세션과 prompt를 테스트하세요.

Copilot Business 사용 가능 여부는 조직의 Copilot CLI 정책에 따릅니다. 인증/인가 실패는 표시되며 **자동 폴백하지 않습니다**. Ouroboros는 조직 정책을 변경하지 않습니다.

## 폴백과 제한

기존 `cli` 전송이 기본값입니다. ACP를 명시적으로 켜고 폴백을 허용한 경우 지원되지 않는 플래그, 호환되지 않는 프로토콜 버전, 잘못된 시작 응답 또는 세션 생성 실패는 **prompt 전송 전**이며 **도구가 없거나 읽기 전용 Read/Glob/Grep envelope일 때만** 기존 실행 방식을 선택할 수 있습니다. `runtime.fallback` 이벤트가 전환 이유를 설명합니다.

변경을 수행하는 envelope는 ACP 권한 콜백을 잃는 대신 실패합니다. 인증 오류, 실행 파일 attestation 실패, 취소, prompt 제출 이후 실패는 다른 전송 방식으로 다시 실행하지 않습니다. 스모크 테스트나 스트리밍이 필수일 때 폴백을 끄세요.

```bash
ouroboros config set orchestrator.copilot_acp_fallback false
```

기존 런타임으로 명시적으로 돌아가려면:

```bash
ouroboros config set orchestrator.copilot_transport cli
```

남은 제한:

- Copilot이 해당 기능을 광고해도 이 구현에는 **네이티브 세션 resume/load가 없습니다**. 네이티브 세션 ID는 재개 handle 선택자가 아닌 진단용 `acp_session_id` 메타데이터로 기록하며 `targeted_resume=False`, 반환 handle은 `can_resume=False`입니다. 이후 Ouroboros dispatch는 범위/프로필 메타데이터를 유지할 수 있지만 새 네이티브 세션을 시작하며 Copilot 대화 상태를 복구하지 않습니다. Ouroboros 기록/재생은 계속 동작합니다.
- 선택적 `agentId`, `parentToolCallId`, `parentId`, 소스 ID/타임스탬프와 MCP 상관 필드는 있을 때 유지합니다. 도구 상관관계는 순차 실행을 가정하지 않습니다. 실시간 foreground `explore` probe에서 Task 호출(`kind=other`, `rawInput.agent_type=explore`)과 내부 Read/Glob 세 호출이 완료 전에 나왔지만 해당 업데이트에는 **agent 또는 parent ID가 없었습니다**. 각 도구 ID는 보존하되 시간이나 ID 접두사로 계보를 추정하지 않습니다. Copilot이 상관관계를 생략하면 부모/자식 텍스트 귀속과 병렬 subagent 시각화는 불완전합니다.
- `structured_output=True`는 구조화된 **런타임 이벤트**를 뜻하며 JSON schema로 제한된 네이티브 모델 응답을 뜻하지 않습니다.
- Herdr, Coder, desktop/web/TUI 레이아웃 또는 원격 세션 저장을 변경하지 않습니다. 스모크 테스트는 헤드리스 Linux 실행이며 Herdr/SSH 전체 배포 테스트가 아닙니다.

## Probe 재현

기존 개발 의존성이 설치된 checkout의 환경을 사용하세요. 기존 setup이 소유한 MCP launcher는 로컬 변경이 아니라 게시된 PyPI 패키지를 실행할 수 있습니다. Copilot 또는 Herdr 같은 호스트에서 테스트하기 전에 [개발 흐름](../contributing/developing.md)을 확인하세요.

```bash
# Raw wire protocol; read-only by default, independent of Ouroboros imports.
python3 scripts/probe-copilot-acp.py \
  --workspace /path/to/safe-repo \
  --log .acp-artifacts/probe.ndjson

# Actual runtime → projection → EventStore, with live timestamps and no fallback.
.venv/bin/python scripts/smoke-copilot-acp.py \
  --workspace /path/to/safe-repo \
  --output-dir .acp-artifacts/runtime-smoke
```

구독에서 사용 가능한 모델은 `--model`로 선택합니다. raw 셸 진행 probe는 `--tools bash --allow-command '<exact command>'`를 전달하고 `--prompt`에 정확히 그 명령을 요청하세요. 다른 모든 실행 요청은 거부됩니다. 각 런타임 스모크는 새 출력 디렉터리에서 실행합니다. `messages.ndjson`, `events.db`, `summary.json`을 기록합니다.

장시간 수용 테스트는 검토하지 않은 실제 작업 저장소 대신 **일회용 로컬 fixture**를 사용하세요. 의도적으로 실패하는 테스트를 만든 뒤 `--tools Read,Edit,Bash`를 명시적으로 허용하고 에이전트에게 fixture 구현만 실행·검사·수정하고 재실행하도록 요청하세요. 테스트의 유용한 지연은 보존합니다. 마지막으로 범위를 정한 세 테스트 fixture에서는 7.333초에 첫 도구가 스트리밍됐고 실행 중 초기 실패와 재테스트 출력이 나왔으며 53.354초에 최종 답변이 도착했습니다. 정규화 이벤트 37개가 저장됐습니다. 이후 fixture 테스트를 독립적으로 다시 실행하세요.

Raw probe 로그에는 비공개 사고, prompt, 도구 출력이 들어갈 수 있습니다. 정규화 런타임 로그는 사고를 제외하지만 민감한 작업공간 데이터가 포함될 수 있습니다. 둘 다 비공개로 유지하고 커밋하지 마세요. 스크립트는 로그를 업로드하지 않습니다.

오프라인 프로토콜, 변환기, 권한, 선택, 취소, 폴백 및 지속 스트리밍 테스트에는 Copilot 계정이 필요하지 않습니다.

```bash
.venv/bin/python -m pytest \
  tests/unit/copilot/test_acp_client.py \
  tests/unit/copilot/test_acp_events.py \
  tests/unit/copilot/test_acp_permissions.py \
  tests/unit/orchestrator/test_copilot_acp_runtime.py \
  tests/unit/config/test_copilot_transport.py \
  tests/integration/test_copilot_acp_runtime.py -q
```

가짜 서버는 consumer가 도구 시작을 저장할 때까지 prompt를 끝내지 않습니다. 닫은 뒤 다시 열어 재생하는 검사에서 같은 델타와 도구 결과가 SQLite에 보존됐는지 확인합니다.

## 참고 자료

- [GitHub Copilot ACP 서버](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server)
- [Copilot CLI와 보안 모델](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-copilot-cli)
- [Agent Client Protocol](https://agentclientprotocol.com/)
- [Ouroboros 설정 참조](../config-reference.md)
- [런타임 기능 매트릭스](../runtime-capability-matrix.md)
