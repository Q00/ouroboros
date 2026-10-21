# Antigravity CLI 런타임

로컬에 설치한 **Antigravity CLI**(`agy` 실행 파일)에서 Ouroboros 워크플로를 실행합니다. Antigravity는 Google Gemini CLI의 후속 도구입니다.

**2026-06-18**부터 Gemini CLI는 Google AI Pro / Ultra / 무료 소비자 요금제 지원을 중단하고 해당 사용자를 Antigravity CLI로 이전합니다(기업용 및 API 키 Gemini 이용에는 영향이 없습니다). Pro/Ultra 구독으로 Gemini 런타임에 인증했다면 해당 단계를 `antigravity` 런타임으로 바꾸세요.

Antigravity 런타임은 Gemini 런타임을 확장합니다(Gemini CLI의 후속 도구). Ouroboros가 오케스트레이션 루프를 소유하고 작업마다 `agy`를 하위 프로세스로 실행합니다. `agy -p`는 **일반 텍스트** 응답을 출력하며 `--output-format` 플래그는 없습니다. 따라서 런타임은 하위 프로세스 내부가 아니라 Ouroboros 계층(이벤트 저장소와 계보)에 체크포인트를 기록하고 최종 답변을 assistant 메시지로 전달합니다.

## 사전 요구 사항

| 요구 사항 | 이유 |
|---|---|
| `agy` CLI | 공급자 도구인 Antigravity CLI 실행 파일. `~/.local/bin`에 설치됩니다. |
| Google 인증 | 처음에 `agy`를 한 번 실행해 브라우저 OAuth를 완료하거나 `ANTIGRAVITY_API_KEY`를 설정합니다. Google AI Pro/Ultra/무료 요금제를 사용할 수 있습니다. |
| Ouroboros 기본 패키지 | `pip install ouroboros-ai`. 공급자 전용 추가 패키지는 필요하지 않습니다. |

> Antigravity는 Ouroboros **기본 패키지**에서 실행됩니다. `[claude]` 추가 패키지는 필요하지 않습니다.

## 빠른 시작

```bash
# 1. Authenticate the Antigravity CLI (one-time browser OAuth)
agy            # first launch opens the sign-in flow

# 2. Point Ouroboros at Antigravity (config.yaml or the settings GUI)
ouroboros config           # GUI: set Execute/Evaluate/... runtime to "antigravity"
# or set OUROBOROS_ANTIGRAVITY_CLI_PATH / orchestrator.antigravity_cli_path

# 3. Run a workflow
ouroboros run workflow seed.yaml --runtime antigravity
```

## CLI 경로 확인 순서

런타임은 다음 순서로 실행 파일을 찾습니다.

1. 생성자 인수 `cli_path=...`
2. `OUROBOROS_ANTIGRAVITY_CLI_PATH` 환경 변수
3. `~/.ouroboros/config.yaml`의 `orchestrator.antigravity_cli_path`
4. `$PATH`의 `agy`

## 설정

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: antigravity
  antigravity_cli_path: ~/.local/bin/agy   # optional; auto-detected
```

Antigravity는 **런타임 전용**입니다. 에이전트 오케스트레이터를 구동하지만 `agy -p`가 일반 텍스트를 반환하고 구조화된 JSON/스키마 페이로드를 제공하지 않으므로 LLM 완성 백엔드로 등록되지 않습니다. 따라서 `llm.backend` 값으로 사용할 수 없습니다. `orchestrator.runtime_backend`, 단계별 라우팅 또는 `--runtime antigravity`로 런타임을 선택하세요. 완성 단계에서 다양한 모델(합의 또는 역할 모델)이 필요하면 `litellm`/OpenRouter를 통해 Gemini나 다른 공급자를 연결합니다.

### 단계별 라우팅

Antigravity는 여러 LLM을 쓰는 파이프라인에서 공급자를 교차하는 단계에 유용합니다. 예를 들어 한 공급자로 생성하고 다른 공급자로 검증할 수 있습니다.

```yaml
orchestrator:
  runtime_backend: claude
  runtime_profile:
    stages:
      execute: claude          # generate
      evaluate: antigravity    # cross-vendor verification (Gemini 3.x via agy)
```

## 헤드리스 실행 계약

작업마다 다음 명령을 실행합니다.

```text
agy -p <PROMPT> \
    --dangerously-skip-permissions \
    [--model <MODEL>]
```

| 플래그 | 이유 |
|---|---|
| `-p` (`--print` / `--prompt`) | 단일 프롬프트를 비대화형으로 실행하고 응답을 출력합니다. |
| `--dangerously-skip-permissions` | 도구 요청을 자동 승인해 하위 프로세스가 대기하지 않게 합니다. |
| `--model` | 명시적이며 센티널이 아닌 모델 ID를 지정했을 때만 전달합니다. |

`agy`에는 편집만 허용하는 세밀한 모드가 없습니다. 따라서 Ouroboros의 두 가지 비차단 권한 모드가 모두 하나의 우회 플래그로 매핑됩니다.

| Ouroboros 권한 모드 | `agy` 동작 | 참고 |
|---|---|---|
| `acceptEdits`(기본값) | `--dangerously-skip-permissions` | 더 좁은 모드가 없어 전체 권한 우회로 확대됩니다. |
| `bypassPermissions` | `--dangerously-skip-permissions` | 전체 권한 우회를 명시합니다. |

헤드리스 `agy -p` 호출은 TTY 승인 프롬프트를 처리할 수 없으므로 대화형 Ouroboros의 `default` 모드를 감사 로그와 함께 `acceptEdits`로 정규화합니다.

> ⚠️ **보안 참고 — 여기서 `acceptEdits`는 모든 요청을 자동 승인합니다.** Gemini 상위 런타임에서는 `acceptEdits`가 편집만 허용하는 `--approval-mode auto_edit`로 연결되지만, `agy`에는 편집 전용 모드가 없습니다. 따라서 Antigravity에서는 기본값인 `acceptEdits`와 `bypassPermissions`가 모두 `--dangerously-skip-permissions`로 처리됩니다. 파일 편집뿐 아니라 모든 도구 요청을 자동 승인합니다. 이는 `agy`의 전부 아니면 전무인 권한 모델이며 `antigravity` 백엔드를 명시적으로 선택했을 때만 적용됩니다. Antigravity 단계는 샌드박스/worktree에서 실행하세요(Ouroboros는 기본적으로 관리형 git worktree를 사용합니다). 편집만 허용하는 격리가 필요하다면 신뢰하지 않는 입력에 Antigravity를 선택하지 말고 Claude, Codex 또는 Gemini 런타임을 사용하세요.

설치된 CLI와 계정의 목록은 [`agy models`](https://www.antigravity.google/docs/models/)로 확인합니다. `agy 1.4.0`에서 2026-10-18에 확인한 ID는 `gemini-3.8-flash-{low,medium,high}`, `gemini-4-argon-{low,medium,high}`, `claude-sonnet-5-5-{low,medium,high}`, `claude-opus-5-5-{low,medium,high}`, `gpt-oss-120b-medium`입니다. Google·Anthropic API 모델명과 다른 CLI 전용 ID이며, 버전·계정·요금제에 따라 사용 가능 목록이 달라집니다. Antigravity는 **센티널 모델 백엔드**입니다. 기본적으로 Ouroboros가 `agy`의 설정 모델 선택에 맡기며 명시적 ID를 지정한 경우에만 `--model`을 전달합니다.

## 기능

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | ✅ `agy -p` |
| 도구 호출 | ✅ (`--dangerously-skip-permissions`를 통해 Antigravity가 관리) |
| 구조화된 이벤트 스트림 | ❌ 일반 텍스트 stdout(`--output-format` 없음) |
| 세션 재개 | ❌ v1에서 제공하지 않음(복구는 Ouroboros 계보 계층에서 처리) |
| LLM 완성 백엔드 | ❌ 런타임 전용 |

구조화된 이벤트 스트림 또는 세션 재개가 필요하면 Claude, Codex 또는 Grok 런타임을 사용하세요.

## 문제 해결

**`agy`를 찾을 수 없습니다.** Antigravity CLI를 설치한 뒤 `OUROBOROS_ANTIGRAVITY_CLI_PATH=/abs/path/to/agy` 또는 `orchestrator.antigravity_cli_path`를 설정합니다.

**CLI가 입력을 기다리며 멈춥니다.** 런타임은 항상 `--dangerously-skip-permissions`와 `-p`를 전달합니다. 멈춘다면 `agy`를 직접 구동한 것이 아니라 `ouroboros run`(또는 MCP 서버)을 통해 런타임을 호출했는지 확인하세요.

**2026-06-18에 Gemini가 작동하지 않습니다.** 이는 소비자 요금제 Gemini CLI 지원 중단입니다. 해당 단계를 `antigravity`로 전환하거나 기업용/API 키 인증으로 Gemini를 계속 사용하세요(이용에 영향 없음).
