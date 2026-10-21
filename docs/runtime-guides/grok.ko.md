# Grok Build CLI 런타임

로컬에 설치한 xAI **Grok Build** CLI(`grok` 실행 파일)에서 Ouroboros 워크플로를 실행합니다.

Grok 런타임은 Codex, Gemini, Hermes 런타임과 같은 계열입니다. Ouroboros가 오케스트레이션 루프를 소유하고 작업마다 `grok`을 하위 프로세스로 실행합니다. Grok은 구조화된 `streaming-json` 이벤트 스트림을 출력하므로 중간 추론과 결과를 실시간으로 볼 수 있습니다. v1은 Grok의 기본 세션 재개 기능 대신 Ouroboros 계층(이벤트 저장소와 계보)에 체크포인트를 기록합니다.

## 사전 요구 사항

| 요구 사항 | 이유 |
|---|---|
| `grok` CLI | 공급자 도구. `curl -fsSL https://x.ai/cli/install.sh \| bash`로 설치합니다. |
| xAI 인증 | `grok login`(브라우저 OAuth, SuperGrok / X Premium+) 또는 `XAI_API_KEY`를 사용합니다. |
| Ouroboros 기본 패키지 | `pip install ouroboros-ai`. 공급자 전용 추가 패키지는 필요하지 않습니다. |

> Grok Build는 Ouroboros **기본 패키지**에서 실행되며 `[claude]` 추가 패키지는 필요하지 않습니다.

## 빠른 시작

```bash
# 1. Install + authenticate the Grok Build CLI
curl -fsSL https://x.ai/cli/install.sh | bash
grok login                           # browser OAuth (SuperGrok / X Premium+)

# 2. Point Ouroboros at Grok (config.yaml or the settings GUI)
ouroboros config                     # GUI: set a stage's runtime to "grok"

# 3. Run a workflow
ouroboros run workflow seed.yaml --runtime grok
```

## CLI 경로 확인 순서

런타임은 다음 순서로 실행 파일을 찾습니다.

1. 생성자 인수 `cli_path=...`
2. `OUROBOROS_GROK_CLI_PATH` 환경 변수
3. `~/.ouroboros/config.yaml`의 `orchestrator.grok_cli_path`
4. `$PATH`의 `grok`

## 설정

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: grok
  grok_cli_path: ~/.local/bin/grok    # optional; auto-detected
```

Grok은 **런타임 전용**입니다. 에이전트 오케스트레이터를 구동하지만 LLM 완성 백엔드로 등록되지 않으므로 `llm.backend` 값으로 사용할 수 없습니다. 완성 단계에서 다양한 모델(합의 또는 역할 모델)이 필요하면 `litellm`/OpenRouter를 통해 xAI Grok을 연결하세요.

### 단계별 라우팅

Grok은 여러 LLM을 사용하는 파이프라인에서 공급자를 교차하는 단계에 적합합니다. 예를 들어 실행 단계의 공급자와 다른 성찰/막힘 해소 단계로 지정할 수 있습니다.

```yaml
orchestrator:
  runtime_backend: claude
  runtime_profile:
    stages:
      execute: claude      # generate
      evaluate: gemini     # cross-vendor verify
      reflect: grok        # divergent lateral thinking
```

## 헤드리스 실행 계약

작업마다 다음 명령을 실행합니다.

```text
grok -p <PROMPT> \
     --output-format streaming-json \
     --permission-mode acceptEdits \
     [-m grok-build]
```

| 플래그 | 이유 |
|---|---|
| `-p` | 단일 턴 프롬프트를 전달하고 응답을 stdout에 출력한 뒤 종료합니다. |
| `--output-format` | 정규화기가 파싱하는 `streaming-json` NDJSON 이벤트를 출력합니다. |
| `--permission-mode` | 기본 비대화형 모드(`acceptEdits` / `bypassPermissions`)를 지정합니다. |
| `-m` | 선택적 모델 재정의입니다(`grok-build`, `grok-composer-2.5-fast`). |

Ouroboros는 권한 모드를 Grok 기본 `--permission-mode` 값에 대응시킵니다.

| Ouroboros 권한 모드 | `grok` 값 | 사용 시점 |
|---|---|---|
| `acceptEdits`(기본값) | `acceptEdits` | TTY 프롬프트 없이 편집을 적용합니다. |
| `bypassPermissions` | `bypassPermissions` | 전체 권한 우회를 명시적으로 요청한 경우입니다. |

헤드리스 `grok -p` 실행은 TTY 승인 프롬프트를 처리할 수 없으므로 대화형 Ouroboros의 `default` 모드를 감사 로그와 함께 `acceptEdits`로 정규화합니다.

Grok이 자체 모델 목록을 관리하므로 **센티널 모델 백엔드**입니다. Ouroboros는 Grok이 설정된 모델을 고르게 하고, 명시적 ID를 지정한 경우에만 `-m`을 전달합니다. 설정 GUI는 `grok models`를 실행해 출력된 글머리 목록에서 호출 가능한 모델을 동적으로 읽습니다. 따라서 선택 목록은 현재 Grok CLI 또는 구독에서 사용할 수 있는 모델을 그대로 반영합니다. 정적 대체 목록은 `grok-build`와 `grok-composer-2.5-fast`입니다.

## 이벤트 변환

런타임은 공유 NDJSON 정규화기를 통해 Grok의 `streaming-json` 이벤트를 파싱하고 Ouroboros의 `AgentMessage`로 변환합니다.

| Grok 이벤트 | Ouroboros 메시지 |
|---|---|
| `{"type":"thought","data":...}` | `data.thinking`이 포함된 `assistant` |
| `{"type":"text","data":...}` | `assistant` 메시지 |
| `{"type":"end","stopReason":...}` | **최종** `assistant` 표시(`data.terminal=True`) |
| `error` | `data.is_error=True`인 `system` 메시지 |

## 기능

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | ✅ `grok -p` |
| 구조화된 이벤트 스트림 | ✅ `--output-format streaming-json` |
| 도구 호출 | ✅ (`--permission-mode`에 따라 Grok이 관리) |
| 추론 강도 | `grok --reasoning-effort`를 지원하지만 기본 추론 강도 라우팅은 후속 작업으로 계획됨 |
| 세션 재개 | ❌ v1에서는 연결하지 않음 (`grok -r`는 존재하며 복구는 Ouroboros 계보 계층에서 처리) |
| LLM 완성 백엔드 | ❌ 런타임 전용 |

## 문제 해결

**`grok`을 찾을 수 없습니다.** Grok Build CLI를 설치한 뒤 `OUROBOROS_GROK_CLI_PATH=/abs/path/to/grok` 또는 `orchestrator.grok_cli_path`를 설정합니다.

**인증 오류가 발생합니다.** `grok login`(SuperGrok / X Premium+)을 한 번 실행하거나 헤드리스/API 키 사용을 위해 `XAI_API_KEY`를 내보냅니다.

**CLI가 입력을 기다리며 멈춥니다.** 런타임은 항상 `-p`와 비대화형 `--permission-mode`를 전달합니다. 계속 멈춘다면 `grok`을 직접 구동한 것이 아니라 `ouroboros run`(또는 MCP 서버)을 통해 런타임을 호출했는지 확인하세요.
