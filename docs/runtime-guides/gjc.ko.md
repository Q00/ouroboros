# GJC 런타임

로컬에 설치한 `gjc` CLI를 기반으로 Ouroboros 워크플로를 실행합니다.

GJC 런타임은 SDK 기반 어댑터입니다. Ouroboros가 워크플로 엔진, Seed 분해, 체크포인트, 평가 전달, `ooo` 스킬 디스패치를 소유합니다. 각 런타임 작업에서 GJC Coordinator MCP 서버를 시작하고 Broker가 관리하는 SDK 세션을 생성한 뒤, 영구 보존되는 GJC 턴 상태를 Ouroboros의 `AgentMessage` 값으로 변환합니다.

## 개념 모델

```text
User / CLI / MCP
      |
      | 1. Selects runtime_backend: gjc, or sends an ooo shortcut
      v
Ouroboros runtime adapter
      |
      | 2a. ooo shortcut? handle inside Ouroboros before GJC starts
      | 2b. normal task? create a GJC SDK session
      v
gjc mcp-serve coordinator
      |
      | 3. Broker -> SessionRouter -> AgentSession
      v
GJC durable turns and questions
```

따라서 “GJC가 Ouroboros 런타임이다”라는 말은 2b 단계가 있고 선택할 수 있다는 뜻입니다. GJC 내부 모듈을 Ouroboros에 가져왔다는 뜻도 아니고, setup이 관리하는 GJC 측 `ooo` 브리지 확장을 설치하지 않았다면 GJC의 대화형 명령 UI가 Ouroboros 명령 라우터에 포함된다는 뜻도 아닙니다.

## 사전 요구 사항

| 요구 사항 | 이유 |
|---|---|
| `gjc` CLI | 공급자 런타임입니다. `gjc`를 PATH에 두거나 명시적 경로를 설정합니다. |
| GJC 인증 | 처음 사용하기 전에 GJC 공급자 로그인/설정 절차를 완료합니다. |
| Ouroboros MCP v2 프로필 | `pip install 'ouroboros-ai[mcp]'`를 실행합니다. MCP 1.x `[claude]` / `[claude-sdk]` 환경과 분리해 둡니다. |

## 빠른 시작

```bash
# 1. Install the MCP v2 Ouroboros profile and GJC, then authenticate GJC
pip install 'ouroboros-ai[mcp]'
gjc

# 2. Point Ouroboros at GJC and install the GJC-side ooo bridge
ouroboros setup --runtime gjc

# 3. Run a workflow through the configured runtime
ouroboros run workflow seed.yaml

# 4. In GJC, restart or reload extensions if needed, then:
ooo auto build a small CLI
```

GJC가 PATH 밖에 설치된 경우 다음을 설정합니다.

```bash
export OUROBOROS_GJC_CLI_PATH=/absolute/path/to/gjc
```

또는 다음을 설정합니다.

```yaml
orchestrator:
  runtime_backend: gjc
  gjc_cli_path: /absolute/path/to/gjc
```

명령 하나에만 백엔드를 선택할 수도 있습니다.

```bash
ouroboros run workflow --runtime gjc seed.yaml
```

## 런타임 계약

일반 실행 작업에서 Ouroboros는 다음 명령을 시작합니다.

```text
gjc mcp-serve coordinator
```

그런 다음 GJC의 지원되는 Coordinator MCP 계약을 사용합니다.

1. `gjc_coordinator_start_session`은 Broker가 관리하는 SDK 세션을 만들고 호출자가 제공한 멱등성 키로 첫 프롬프트를 제출합니다.
2. `gjc_coordinator_await_turn`은 완료, 실패, 취소 또는 구조화된 질문이 나올 때까지 영구 보존되는 턴 상태를 읽습니다.
3. 최종 턴에 최종 응답이 인라인으로 포함되지 않으면 `gjc_coordinator_read_tail`이 제한된 마지막 assistant 출력을 복구합니다.
4. `gjc_coordinator_list_questions`와 `gjc_coordinator_submit_question_answer`는 Ouroboros `RuntimeHandle` 재개 과정에서 질문 상관관계를 유지합니다.
5. `gjc_coordinator_stop_session`은 SDK 생명주기 권한을 사용해 완료된 임시 세션을 닫습니다.

Ouroboros는 GJC 엔드포인트 기록이나 인증 정보를 읽지 않으며 비공개 SDK WebSocket도 열지 않습니다. 엔드포인트 발견, 인증, 세대 펜싱, 턴 전달은 GJC Broker와 `SessionRouter`만 관리합니다.

GJC 어댑터는 MCP v2 통합입니다. setup은 `runtime_backend: gjc` 또는 `llm.backend: gjc`를 기록하기 전에 현재 Ouroboros 인터프리터에 고정 버전 `mcp==2.0.0` 클라이언트가 있는지 확인합니다. 이 프로필은 MCP 1.x 의존성을 유지하는 Claude Agent SDK 프로필과 분리해야 합니다.

## GJC에서 `ooo`의 동작

지원되는 진입 경로는 두 가지입니다.

### Ouroboros가 GJC를 시작하는 경우

Ouroboros가 이미 제어 중이고 `runtime_backend: gjc`를 선택했다면 GJC 하위 프로세스를 시작하기 전에 Ouroboros가 `ooo <skill>`을 처리합니다.

GJC 런타임은 작업 실행을 시작할 때 공유 `SkillInterceptor`를 호출합니다. 프롬프트가 `ooo interview` 또는 `/ouroboros:ouroboros-run` 같은 Ouroboros 스킬 바로가기라면 interceptor가 스킬을 찾아 해당 Ouroboros MCP 처리기를 호출합니다. GJC는 이 프롬프트를 일반 채팅 입력으로 받지 않습니다.

따라서 다음과 같이 동작합니다.

- Ouroboros가 제어하는 GJC 런타임에서 `ooo interview`는 설정된 LLM 백엔드를 사용해 작성 작업을 Ouroboros가 처리하라는 뜻입니다.
- 명령 디스패치가 입력을 `ooo` 바로가기가 아니라고 판별한 뒤에야 GJC가 일반 Seed 실행 프롬프트를 처리합니다.

### GJC가 Ouroboros를 시작하는 경우

`ouroboros setup --runtime gjc`는 관리형 GJC 브리지 확장도 설치합니다.

```text
<agent-dir>/extensions/ouroboros-ooo-bridge/index.ts
```

GJC가 확장을 불러오면 대화형 GJC 세션에서 다음 명령을 입력할 수 있습니다.

```text
ooo auto build a small CLI
ooo interview clarify this feature
/ooo status auto --resume auto_...
```

확장은 접두사가 정확히 일치하는 `ooo ...` 입력을 가로채고 다음을 실행합니다.

```text
ouroboros dispatch --runtime gjc --cwd <gjc-session-cwd> "ooo ..."
```

숨겨진 `dispatch` 진입점은 런타임 어댑터와 같은 공유 스킬 리졸버 및 MCP 처리기 구성 방식을 사용합니다. 이 브리지는 양방향으로 동작합니다. Ouroboros가 실행을 위해 GJC를 시작할 수 있고, GJC 측 확장은 대화형 `ooo` 명령을 다시 Ouroboros로 보낼 수 있습니다.

브리지는 MCP 기반 스킬 frontmatter를 통해 숨겨진 디스패처가 실행할 수 있는 명령만 처리합니다. MCP 디스패치 대상을 선언하지 않은 Ouroboros 자체 바로가기 명령은 결정적인 미지원 디스패치 종료 코드와 함께 GJC로 돌아갑니다. 그러면 일반 GJC 세션이 입력을 계속 처리하며 브리지 오류로 중단되지 않습니다.

브리지는 종료 코드 `78`을 미지원 디스패치 결과로 그대로 전달합니다. 또한 브리지가 생성한 `ooo` 명령이 다시 가로채져 Ouroboros로 재전달되는 것을 막는 재귀 방지 장치도 둡니다.

## LLM 백엔드로서의 GJC

GJC는 작성, 점수화, 추출 및 기타 완성 요청의 LLM 백엔드로도 선택할 수 있습니다.

```yaml
llm:
  backend: gjc
```

이는 `orchestrator.runtime_backend`와 별개의 설정입니다.

GJC LLM 어댑터는 완화된 강제 방식을 통해 구조화된 `response_format` 요청을 지원합니다. Ouroboros가 엄격한 JSON/스키마 지시문을 추가하고 GJC 응답에서 JSON 페이로드를 추출하며, 반환하기 전에 `json_schema` 페이로드를 검증합니다. GJC SDK에는 공급자 자체의 강제 스키마 봉투가 없으므로 잘못된 구조화 응답은 재시도한 뒤 공급자 오류로 전달됩니다.

GJC가 Seed 작업을 실행하게 하려면 GJC를 런타임 백엔드로 사용합니다. 작성/평가 흐름에서 공급자 수준 스키마 강제 대신 어댑터 수준 JSON 추출과 검증을 허용할 수 있다면 `llm.backend: gjc`를 사용합니다.

## 기능

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | 가능. Coordinator MCP 및 Broker 관리 SDK 세션 사용 |
| 스킬 바로가기 디스패치 | 가능. GJC 세션 시작 전에 처리 |
| 지정 세션 재개 | 가능. SDK 세션 ID 및 질문에 연결된 런타임 핸들 사용 |
| 구조화된 이벤트/상태 | 가능. 영구 보존되는 Coordinator 턴 및 질문 프로젝션 |
| 기본 권한 재정의 | 불가. SDK 세션은 GJC에 설정한 권한 정책을 유지하므로 `permission_mode_support=ignored` |
| LLM 백엔드의 구조화 스키마 응답 | 완화된 방식으로 강제하고 검증 |
| 강제 도구/스키마 봉투 | 없음 |
| GJC 확장 로드 | GJC가 소유. Ouroboros setup이 브리지 산출물을 설치하지만 활성화 여부는 GJC 확장 정책에 달림 |
| 대화형 GJC `ooo` 진입점 | GJC에 설정한 MCP/스킬 경로를 사용. 선택적 파일 시스템 확장은 SDK 백엔드 계약에 포함되지 않음 |

## 한계

- 활성 도구와 권한 정책은 GJC가 소유합니다. Ouroboros 도구 허용 목록과 권한 모드 재정의는 적용된 것처럼 표시하지 않고 무시된 것으로 보고합니다.
- LLM 구조화 출력은 여전히 프롬프트로 요청한 뒤 턴이 끝난 후 검증합니다. 이 SDK 경로에서 GJC는 공급자 자체 출력 스키마 봉투를 제공하지 않습니다.
- Coordinator가 만든 세션과 턴 저널은 영구 보존됩니다. 정리 작업이 실패해도 완료된 결과를 대체하지 않습니다. 다음 Coordinator 시작 시 GJC 자체 생명주기 권한에 따라 남은 상태를 조정할 수 있습니다.

## 문제 해결

**`GJC not found`**
GJC를 설치해 `gjc`를 PATH에 두거나 `OUROBOROS_GJC_CLI_PATH`를 설정합니다.

**구조화된 출력 요청이 재시도 후에도 실패합니다.**
GJC LLM 백엔드는 완화된 JSON/스키마 강제를 사용합니다. 표시된 공급자 오류와 프롬프트 출력을 확인하세요. 잘못된 JSON 또는 스키마에 맞지 않는 페이로드는 추출과 검증 과정에서 Ouroboros가 거부합니다.

**GJC에서 `ooo ...`가 일반 채팅으로 모델에 전달됩니다.**
`ouroboros setup --runtime gjc`를 실행한 뒤 GJC를 다시 시작하거나 확장을 다시 불러옵니다. `<agent-dir>/extensions/ouroboros-ooo-bridge/index.ts` 파일이 있는지 확인하세요.
