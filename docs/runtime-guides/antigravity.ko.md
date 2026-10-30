# Antigravity CLI 런타임

로컬에 설치한 **Antigravity CLI**(`agy` 실행 파일)에서 Ouroboros 워크플로를 실행합니다. Antigravity는 Google Gemini CLI의 후속 도구입니다.

**2026-06-18**부터 Gemini CLI는 Google AI Pro / Ultra / 무료 소비자 요금제 지원을 중단하고 해당 사용자를 Antigravity CLI로 이전합니다(기업용 및 API 키 Gemini 이용에는 영향이 없습니다). Pro/Ultra 구독으로 Gemini 런타임에 인증했다면 해당 단계를 `antigravity` 런타임으로 바꾸세요.

Antigravity 런타임은 Gemini 런타임을 확장합니다(Gemini CLI의 후속 도구). Ouroboros가 오케스트레이션 루프를 소유하고 작업마다 `agy -p --output-format stream-json`을 실행합니다. 네이티브 assistant, 도구, 세션, 종료 이벤트를 Ouroboros 메시지로 정규화하며 워크플로 체크포인트는 Ouroboros 계층(이벤트 저장소와 계보)에 기록합니다. 설치된 CLI는 `--output-format stream-json`을 지원해야 합니다. CLI에 따라 달라지는 동작은 [Antigravity 스트림 계약](../antigravity-stream-contract.md)을 참고하세요.

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

Antigravity는 **런타임 전용**입니다. 에이전트 오케스트레이터를 구동하지만 LLM 완성 백엔드로 등록되지 않으므로 `llm.backend` 값으로 사용할 수 없습니다. 구조화된 런타임 이벤트 스트림이 있다는 사실만으로 완성 어댑터나 공급자 수준 JSON/스키마 강제가 제공되는 것은 아닙니다. `orchestrator.runtime_backend`, 단계별 라우팅 또는 `--runtime antigravity`로 런타임을 선택하세요. 완성 단계에서 다양한 모델(합의 또는 역할 모델)이 필요하면 `litellm`/OpenRouter를 통해 Gemini나 다른 공급자를 연결합니다.

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
    --output-format stream-json \
    [--model <MODEL>] \
    [--effort <low|medium|high|xhigh|max>] \
    [--conversation <CONVERSATION_ID>]
```

| 플래그 | 이유 |
|---|---|
| `-p` (`--print` / `--prompt`) | 단일 프롬프트를 비대화형으로 실행하고 응답을 출력합니다. |
| `--dangerously-skip-permissions` | 도구 요청을 자동 승인해 하위 프로세스가 대기하지 않게 합니다. |
| `--output-format stream-json` | 네이티브 assistant, 도구, 세션, 종료 이벤트를 출력합니다. |
| `--model` | 명시적이며 센티널이 아닌 모델 ID를 지정했을 때만 전달합니다. |
| `--effort` | 선택적 네이티브 추론 강도입니다. 지원하지 않는 값은 실행 전에 거부합니다. |
| `--conversation` | 지정한 대화 ID 또는 런타임 핸들의 정확한 세션을 재개합니다. |

런타임은 구조화된 이벤트를 정규화하고 도구 출력을 최종 assistant 응답과 분리합니다. 일반 텍스트로 감싼 출력은 호환성 폴백으로 남지만, 어떤 도구가 실행됐는지는 입증하지 못합니다. 재개에는 가장 최근 대화를 고르는 `--continue`가 아니라 `--conversation`을 사용합니다. 지원되지 않는 재개 요청은 명시적으로 실패하며 새 세션으로 재시도하지 않습니다. 스트림에 다른 네이티브 대화 ID가 나오면 해당 이벤트의 내용을 전달하기 전에 실행을 종료합니다.

`agy`에는 네이티브 `--mode accept-edits` 옵션이 있습니다. 다만 헤드리스 실행에서는 승인이 필요한 호출이 대화형 프롬프트 없이 거절될 수 있으므로, 이 모드가 Ouroboros의 편집 전용 권한을 보장하지는 않습니다. 기존 동작을 유지하기 위해 Ouroboros의 두 비차단 권한 모드는 계속 전체 우회 플래그로 매핑됩니다.

| Ouroboros 권한 모드 | `agy` 동작 | 참고 |
|---|---|---|
| `acceptEdits`(기본값) | `--dangerously-skip-permissions` | 기존 전체 우회 매핑을 유지하며 편집 전용 권한을 보장하지 않습니다. |
| `bypassPermissions` | `--dangerously-skip-permissions` | 전체 권한 우회를 명시합니다. |

대화형 Ouroboros의 `default` 모드는 감사 로그와 함께 `acceptEdits`로 정규화되므로 헤드리스 실행에서도 같은 전체 우회 매핑을 유지합니다.

> ⚠️ **보안 참고 — 여기서 `acceptEdits`는 모든 요청을 자동 승인합니다.** Gemini 상위 런타임에서는 `acceptEdits`가 편집만 허용하는 `--approval-mode auto_edit`로 연결되지만, Antigravity의 네이티브 `--mode accept-edits`가 Ouroboros의 기존 매핑을 바꾸지는 않습니다. 기본값인 `acceptEdits`와 `bypassPermissions`는 모두 `--dangerously-skip-permissions`로 처리되어 파일 편집뿐 아니라 모든 도구 요청을 자동 승인합니다. 이 매핑은 `antigravity` 백엔드를 명시적으로 선택했을 때만 적용됩니다. Antigravity 단계는 샌드박스/worktree에서 실행하세요(Ouroboros는 기본적으로 관리형 git worktree를 사용합니다). 편집만 허용하는 격리가 필요하다면 신뢰하지 않는 입력에 Antigravity를 선택하지 말고 Claude, Codex 또는 Gemini 런타임을 사용하세요.

설치된 CLI와 계정의 목록은 [`agy models`](https://www.antigravity.google/docs/models/)로 확인합니다. `agy 1.4.0`에서 2026-10-18에 확인한 ID는 `gemini-3.8-flash-{low,medium,high}`, `gemini-4-argon-{low,medium,high}`, `claude-sonnet-5-5-{low,medium,high}`, `claude-opus-5-5-{low,medium,high}`, `gpt-oss-120b-medium`입니다. Google·Anthropic API 모델명과 다른 CLI 전용 ID이며, 버전·계정·요금제에 따라 사용 가능 목록이 달라집니다. Antigravity는 **센티널 모델 백엔드**입니다. 기본적으로 Ouroboros가 `agy`의 설정 모델 선택에 맡기며 명시적 ID를 지정한 경우에만 `--model`을 전달합니다.

## 기능

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | ✅ `agy -p` |
| 도구 호출 | ✅ (`--dangerously-skip-permissions`를 통해 Antigravity가 관리) |
| 구조화된 이벤트 스트림 | ✅ `--output-format stream-json`. 일반 텍스트 호환성 폴백도 유지 |
| 세션 재개 | ✅ `--conversation`으로 정확한 네이티브 대화 재개. 새 세션으로 폴백하지 않음 |
| 추론 강도 | ✅ 네이티브 `--effort`: `low`, `medium`, `high`, `xhigh`, `max` |
| LLM 완성 백엔드 | ❌ 런타임 전용 |

이 기능들은 설치된 CLI가 문서에 나온 플래그를 지원해야 동작합니다. 실시간 턴 중단이나 강제 도구/샌드박스 경계를 의미하지는 않습니다.

## 문제 해결

**`agy`를 찾을 수 없습니다.** Antigravity CLI를 설치한 뒤 `OUROBOROS_ANTIGRAVITY_CLI_PATH=/abs/path/to/agy` 또는 `orchestrator.antigravity_cli_path`를 설정합니다.

**헤드리스 호출에 승인이 필요합니다.** Ouroboros는 도구 요청을 자동 승인하는 `--dangerously-skip-permissions`를 전달합니다. 승인이 필요한 모드로 `agy`를 직접 실행하면 대화형 프롬프트 없이 호출이 거절될 수 있으며, 반드시 입력을 기다리며 멈추는 것은 아닙니다.

**2026-06-18에 Gemini가 작동하지 않습니다.** 이는 소비자 요금제 Gemini CLI 지원 중단입니다. 해당 단계를 `antigravity`로 전환하거나 기업용/API 키 인증으로 Gemini를 계속 사용하세요(이용에 영향 없음).
