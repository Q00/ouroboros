# Gemini CLI 런타임

로컬에 설치한 [`gemini`](https://github.com/google-gemini/gemini-cli) CLI에서 Ouroboros 워크플로를 실행합니다.

Gemini 런타임은 Codex, Hermes, OpenCode 런타임과 같은 계열입니다. Ouroboros가 오케스트레이션 루프를 소유하고 호스팅 SDK에 직접 연결하는 대신 작업마다 `gemini`를 하위 프로세스로 실행합니다. 이 런타임은 **상태를 유지하지 않습니다**. Gemini는 현재 세션 재개 API를 제공하지 않으므로 체크포인트는 하위 프로세스 내부가 아니라 Ouroboros 계층(이벤트 저장소와 계보)에 기록됩니다.

## 사전 요구 사항

| 요구 사항 | 이유 |
|---|---|
| `gemini` CLI | 공급자 도구. `npm install -g @google/gemini-cli`로 설치합니다. |
| Google 인증 | 처음 사용하기 전에 `gemini auth`를 실행하거나 `GOOGLE_API_KEY`를 설정합니다. |
| Ouroboros 기본 패키지 | `pip install ouroboros-ai`. 공급자 전용 추가 패키지는 필요하지 않습니다. |

> Gemini는 Ouroboros **기본 패키지**에서 실행됩니다. `[claude]` 추가 패키지는 필요하지 않습니다. MCP 진입점은 기존에 설정한 추가 패키지를 계속 사용해도 되고, 기본 `ouroboros-ai[mcp]`를 사용해도 됩니다.

## 빠른 시작

```bash
# 1. Install Gemini CLI (if not already on PATH)
npm install -g @google/gemini-cli
gemini auth                       # one-time auth

# 2. Point Ouroboros at Gemini
ouroboros setup --runtime gemini  # auto-detects PATH or OUROBOROS_GEMINI_CLI_PATH
# or, switch later:
ouroboros config backend gemini

# 3. Run a workflow
ouroboros init "Add a CLI flag to skip eval"
ouroboros run workflow seed.yaml
```

## CLI 경로 확인 순서

런타임은 다음 순서로 실행 파일을 찾습니다.

1. 생성자 인수 `cli_path=...`
2. `OUROBOROS_GEMINI_CLI_PATH` 환경 변수
3. `~/.ouroboros/config.yaml`의 `orchestrator.gemini_cli_path`
4. `$PATH`의 `gemini`

따라서 `~/.local/share/gemini-cli/bin/gemini`처럼 PATH에 없는 곳에 설치해도 셸 초기화 파일을 수정하지 않고 사용할 수 있습니다.

## 설정

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: gemini
  gemini_cli_path: /opt/homebrew/bin/gemini   # optional; auto-detected
llm:
  backend: gemini                             # for interview / seed / eval
```

### 완료 요청의 모델 선택

`llm.backend: gemini`를 사용하는 인터뷰·Seed·평가의 완료 요청에서 자동 역할 모델은
Ouroboros 내부의 `default` 값으로 해석됩니다. 명시한 모델이 없으면 완료 어댑터는
`--model`을 생략하므로 설치된 Gemini CLI의 설정과 기본 선택을 따릅니다.
Ouroboros가 특정 Gemini 버전을 대신 지정하지 않습니다.

명시한 요청 모델과 역할·공급자 프로필의 모델 지정은 기존 우선순위를 유지합니다.
어댑터를 직접 사용하는 호출자는 생성자에 모델을 지정할 수도 있습니다. 이 선택은
`--model`로 전달되며 `flash`, `pro`, `auto` 같은 별칭과 구체적인 모델 ID를
그대로 사용할 수 있습니다. 기존 `gemini/`, `google/` 접두사는 실행 전에 제거됩니다.
Ouroboros 호출 단계에서 고른 `auto`는 역할 해석기가 `default`로 바꾸므로
CLI에 직접 전달하는 `auto` 별칭과 구분됩니다.

완료 응답에는 CLI의 `init` 이벤트가 보고한 모델을 사용합니다. 모델을 명시하지
않았고 해당 이벤트에도 모델이 없다면 구체적인 ID를 추측하지 않고
`default`를 보고합니다. 이 완료 요청의 동작이 워크플로 런타임의 작업별 적응형
모델 배치를 보장하는 것은 아닙니다.

백엔드 이름을 받는 모든 CLI에서 같은 `gemini` 값을 사용할 수 있습니다.

- `ouroboros setup --runtime gemini`
- `ouroboros config backend gemini`
- `ouroboros mcp serve --runtime gemini --llm-backend gemini`
- `ouroboros init --llm-backend gemini`

## 헤드리스 실행 계약

작업마다 다음 명령을 실행합니다.

```text
gemini --prompt <PROMPT> \
       --non-interactive \
       --output-format stream-json \
       --approval-mode yolo \
       --skip-trust \
       [--model gemini-2.5-pro]
```

| 플래그 | 이유 |
|---|---|
| `--prompt` | 요청을 전달합니다(Gemini가 문서화한 헤드리스 API). |
| `--non-interactive` | TTY 프롬프트를 비활성화해 하위 프로세스가 대기하지 않게 합니다. |
| `--output-format` | `GeminiEventNormalizer`가 파싱하는 NDJSON 이벤트 스트림입니다. |
| `--approval-mode` | 기본값은 `auto_edit`(`acceptEdits`)입니다. `yolo`는 운영자가 `bypassPermissions`를 명시적으로 설정했을 때만 사용합니다. |
| `--skip-trust` | Gemini의 작업공간 신뢰 목록에 아직 없는 새 작업 worktree에서도 `yolo` 모드를 유지합니다. |
| `--model` | 선택적 모델 재정의입니다(`gemini-2.5-pro`, `flash`). |

Ouroboros는 권한 모드를 Gemini의 비대화형 승인 모드에 대응시킵니다.

| Ouroboros 권한 모드 | Gemini 플래그 값 | 사용 시점 |
|---|---|---|
| `acceptEdits` | `auto_edit` | 기본값. TTY 프롬프트 없이 편집을 적용합니다. |
| `bypassPermissions` | `yolo` | 운영자가 전체 권한 우회를 명시적으로 요청한 경우입니다. |

Runner가 실행하는 Seed 작업은 `bypassPermissions`를 강제하므로 새 실행과 논리적 재개 실행에 `--approval-mode yolo --skip-trust`를 사용합니다. `acceptEdits`를 명시적으로 선택한 하위 수준 호출자는 `--skip-trust` 없이 `auto_edit`를 계속 사용합니다.

Gemini에서는 대화형 Ouroboros의 `default` 권한 모드를 `acceptEdits`로 정규화합니다. `--non-interactive` 실행은 TTY 승인 프롬프트를 처리할 수 없기 때문입니다.

## 이벤트 변환

런타임은 `GeminiEventNormalizer`를 통해 Gemini의 `stream-json` 이벤트를 파싱하고 Ouroboros의 `AgentMessage`로 변환합니다.

| Gemini 이벤트 | Ouroboros 메시지 |
|---|---|
| `init` | 세션 메타데이터만 기록하며 메시지는 생성하지 않습니다. |
| `message` / `text` | `assistant` 메시지 |
| `thinking` | `data.thinking`이 포함된 `assistant` |
| `tool_use` | `tool_name`과 `data.tool_input`이 포함된 `assistant` |
| `tool_result` | `data.is_error`가 포함된 `tool` 메시지 |
| `error` | `data.is_error=True`인 `system` 메시지 |
| `result` | **최종** `assistant` 메시지. 중간 `message` 이벤트가 없을 때 Gemini는 이 이벤트에 최종 응답을 담습니다. |

최종 `result` 이벤트는 중요합니다. Gemini가 스트림 중간에 `message`를 내보내지 않으면 최종 답변이 오케스트레이터에 도달하는 유일한 경로입니다. 이전 프로토타입은 이 이벤트를 버려 최종 답변이 유실됐습니다. 현재 런타임은 `data.terminal=True`와 함께 명시적으로 전달합니다.

## 기능

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | ✅ |
| 도구 호출 | ✅ (Gemini가 관리하며 Codex 권한 플래그를 사용하지 않음) |
| 재귀 방지 | ✅ `_OUROBOROS_DEPTH` (Claude/Codex와 동일) |
| 응답 길이 제한 | ✅ `InputValidator` 사용 (#315와 동일) |
| 세션 재개 | ❌ Gemini CLI에서 지원하지 않음 |

세션 재개가 필요하면 Claude 또는 Codex 런타임을 사용하세요. Gemini는 Ouroboros 체크포인트 계층에서 복구합니다.

## 문제 해결

**`gemini CLI not found.`**
`@google/gemini-cli`를 설치한 뒤 `setup`이 자동 탐지하게 하거나 `OUROBOROS_GEMINI_CLI_PATH=/abs/path/to/gemini`를 설정합니다.

**최종 응답이 없습니다.**
`result` 이벤트를 무시하던 이전 빌드를 사용 중일 수 있습니다. 업그레이드한 뒤 다시 실행하세요. 현재 런타임은 `result.response`를 최종 assistant 메시지로 전달합니다.

**CLI가 입력을 기다리며 멈춥니다.**
런타임은 항상 `--non-interactive`를 전달합니다. 멈춘다면 해당 플래그 없이 `gemini`를 직접 실행한 것이 아니라 `ouroboros run`(또는 MCP 서버)을 통해 런타임을 호출했는지 확인하세요.
