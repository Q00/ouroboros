<!--
doc_metadata:
  runtime_scope: [codex]
-->

# Codex CLI로 Ouroboros 실행하기

> English: [codex.md](./codex.md)
>
> 영문 원문 전체를 옮긴 완역본입니다. 원문이 갱신되면 이 문서도 함께 갱신해야 합니다.

> 설치와 첫 실행 흐름 전체는 [Getting Started](../getting-started.md)(영문)를 보세요.

Ouroboros는 **OpenAI Codex**를 런타임 백엔드로 쓸 수 있습니다. [Codex CLI](https://github.com/openai/codex)가 어댑터와 실제로 대화하는 로컬 실행 표면입니다. macOS에서는 `PATH`에 없을 때 ChatGPT 앱에 딸려 오는 실행 파일도 setup이 찾아냅니다. Ouroboros 안에서 이 백엔드는 **세션 지향 런타임**으로 다뤄지며, 규약 우선 워크플로우 하네스(검수 기준, 평가 원칙, 결정적 종료 조건)는 동일하게 적용됩니다. 어댑터 자체는 로컬 `codex` 실행 파일과 통신할 뿐입니다. 기본값으로 Ouroboros는 **Codex가 지금 선택하고 있는 모델**을 그대로 쓰고, 역할별 추론 강도(reasoning effort)만 넘깁니다.

기본 `ouroboros-ai` 패키지 외에 추가 Python SDK는 필요 없습니다.

> **모델 권장**: Codex의 현재 기본 모델로 시작하세요. Ouroboros는 호출마다 역할별 추론 강도를 적용합니다. 특정 단계에 모델을 의도적으로 고정해야 할 때만 Ouroboros 설정에서 모델을 지정하세요.

## 시작하기 (권장 경로)

Codex 플러그인으로 시작하는 게 가장 짧습니다.

시작하기 전에 두 가지가 필요합니다. **`codex`가 `PATH`에 있어야 하고**(아래 명령을 셸에서 치기 때문입니다), 호스트에 **`uvx`가 있어야 합니다**(플러그인의 MCP 서술자가 `uvx`로 서버를 띄웁니다 — [`.mcp.codex.json`](../../.mcp.codex.json)). `uvx`가 없으면 다음 중 하나로 uv를 설치하세요:

```bash
pipx install uv
pip install --user uv
brew install uv          # macOS / Linuxbrew
```

**Python은 직접 깔지 않아도 됩니다.** `uvx`가 패키지의 `requires-python = ">=3.12"`를 보고 맞는 인터프리터를 확보합니다.

**터미널:**

```bash
codex plugin marketplace add Q00/ouroboros
codex plugin add ouroboros@ouroboros
```

새 Codex 세션을 열고 `ooo`를 입력하세요. setup이 아직 안 돌았다면, Ouroboros가 **무언가를 바꾸기 전에 먼저 런타임을 준비할지 물어봅니다.** 준비가 끝나면 Codex의 현재 기본 모델을 자동으로 씁니다. **직접 모델 설정하기**(Directly configure models)는 특정 파이프라인 단계의 모델을 고르거나 고정하고 싶을 때만 선택하세요 — Codex에서는 임시 `localhost` 주소로 브라우저에 로컬 설정 화면이 열립니다.

### 사전 조건 (권장 경로)

- **Codex CLI**가 설치돼 있고 **`PATH`에 있을 것.** 위 `codex plugin ...` 명령을 셸에서 실행하므로 이 경로에서는 `PATH` 등록이 필수입니다. macOS ChatGPT 앱에 딸린 실행 파일만 있고 `PATH`에는 없다면, 그 실행 파일을 `PATH`에 올리거나([설치 절차](#codex-cli-설치) 참고) 아래 [독립 설치](#독립-설치-플러그인-없이) 경로를 쓰세요. Ouroboros setup은 앱 번들 실행 파일을 찾아낼 수 있지만, **셸은 `codex plugin` 명령을 해석하지 못합니다.**
- 로그인된 **Codex CLI** 계정. API 키 인증도 됩니다: `printenv OPENAI_API_KEY | codex login --with-api-key`. 파일 기반 키 관리는 [`credentials.yaml`](../config-reference.md#credentialsyaml) 참고
- **`uvx`** (uv에 포함) — 위에서 설명한 대로 플러그인 MCP 서술자가 이걸로 서버를 띄웁니다

아래 독립 설치 경로의 `Python >= 3.12` 요구사항은 **이 경로에는 해당하지 않습니다.**

## 독립 설치 (플러그인 없이)

플러그인 없이 Codex CLI를 직접 붙이는 경로입니다. `codex`가 `PATH`에 없고 macOS ChatGPT 앱 번들만 있는 경우에도 이 경로를 씁니다 — setup은 번들 실행 파일을 찾아낼 수 있습니다.

### 사전 조건 (독립 설치 경로)

**setup을 돌리기 전에** 다음이 필요합니다:

- 로그인된 **Codex CLI** 계정 (위와 동일). `codex`가 `PATH`에 있으면 좋지만, 이 경로에서는 필수가 아닙니다
- **Python >= 3.12**
- **Ouroboros 설치**
- **MCP launcher** — 아래 셋 중 **하나**. `_codex_release_mcp_launcher()`가 이 순서로 찾고, 셋 다 없으면 `_register_codex_mcp_server()`가 중단합니다. 메시지는 `Could not find a launchable Ouroboros MCP command. Install uv, or install Ouroboros with the [mcp] extra, then rerun setup.` 인데, **화면에서는 `[mcp]`가 Rich 마크업으로 소비되어 `with the extra`로 보입니다** — 터미널 출력과 소스 문자열이 다릅니다:
  1. `uvx` (uv에 포함) ← 가장 간단
  2. `ouroboros mcp serve --help`가 성공하는 설치, 즉 **`[mcp]` extra 포함**
  3. `mcp` 패키지가 들어 있는 Python 환경

**터미널:**

```bash
# 1. Ouroboros 설치 (launcher 선택과 무관하게 항상 필요)
pip install ouroboros-ai

# 2. MCP launcher 확보 — 둘 중 하나
pipx install uv                          # uvx. brew install uv도 가능
pip install 'ouroboros-ai[mcp]'          # 또는 extra로 갈아끼우기 (1단계를 대체)

# 3. 연결
ouroboros setup --runtime codex
```

> 1단계와 2단계는 별개입니다. `pipx install uv`는 `uvx`만 설치하고 **`ouroboros` 실행 파일은 설치하지 않습니다.** `pip install 'ouroboros-ai[mcp]'`만이 우연히 둘 다 해결합니다. 다른 설치 방법(한 줄 설치, 소스)은 [Getting Started](../getting-started.md)(영문) 참고

> **`[mcp]` extra는 "필요 없음"이 아닙니다.** 기본 `ouroboros-ai` 패키지에 Codex CLI 런타임 어댑터는 들어 있지만, **MCP 등록에는 위 launcher 중 하나가 반드시 있어야 합니다.** `uvx`가 있으면 extra 없이도 되고, `uvx`가 없으면 `[mcp]` extra가 사실상 필수입니다. 설치 방법 전체(pip, 한 줄 설치, 소스)는 [Getting Started](../getting-started.md)(영문) 참고

## Codex CLI 설치

Codex CLI 자체는 npm 패키지로 배포됩니다. 전역으로 설치하세요.

```bash
npm install -g @openai/codex
```

설치를 확인합니다.

```bash
codex --version
```

다른 설치 방법과 셸 자동완성은 [Codex CLI README](https://github.com/openai/codex#readme)를 보세요.

## 플랫폼

| 플랫폼 | 상태 | 비고 |
|----------|--------|-------|
| macOS (ARM/Intel) | 지원 | 주 개발 플랫폼 |
| Linux (x86_64/ARM64) | 지원 | Ubuntu 22.04+, Debian 12+, Fedora 38+ 에서 검증 |
| Windows (WSL 2) | 지원 | Windows 사용자에게 권장하는 경로 |
| Windows (네이티브) | 실험적 | WSL 2를 강력히 권장합니다. 네이티브 Windows에서는 경로 처리와 프로세스 관리에 문제가 있을 수 있습니다. **Codex CLI 자체가 네이티브 Windows를 지원하지 않습니다.** |

## 설정

런타임 백엔드로 Codex CLI를 고르려면 설정에 이렇게 씁니다.

```yaml
orchestrator:
  runtime_backend: codex
```

명령줄에서 넘길 수도 있습니다.

```bash
uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

### Codex 사용자는 무엇을 어디에 설정하나

**Ouroboros 런타임 설정은 `~/.ouroboros/config.yaml`입니다.** 평소 모델 선택은 `ouroboros config` 또는 `ouroboros config --web`으로 여세요. 둘 다 같은 설정 화면입니다.

**Use Codex default model**을 고르면 Codex의 현재 기본 모델을 그대로 씁니다. 이게 권장 설정입니다 — Ouroboros가 Codex 호출마다 역할별 추론 강도만 넘기므로, Codex App이나 CLI에서 새 모델로 바꾸면 자동으로 그게 쓰입니다. 목록에서 모델을 고르거나 **Enter another model ID…**를 쓰는 건, 특정 단계(Execute 포함)에 모델을 의도적으로 고정하고 싶을 때만 하세요.

**Codex의 MCP/env 연결과 사용자가 직접 관리하는 네이티브 Codex 프로필은 `$CODEX_HOME/config.toml`입니다.** `CODEX_HOME`이 설정돼 있지 않으면 Codex는 `~/.codex/config.toml`을 씁니다.

Codex 기반 Ouroboros 역할들이 Codex CLI의 활성 기본값/프로필을 물려받는 대신 명시적 모델을 쓰게 하려면, `config.yaml` 키를 직접 지정하세요.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex   # codex가 이미 PATH에 있으면 생략

llm:
  backend: codex
  qa_model: gpt-5.4

clarification:
  default_model: gpt-5.4

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
  # 선택: 단순 투표 로스터도 여기에 `consensus.models`로 둡니다
```

이 키들을 기본값 그대로 두면, Codex setup이 프로바이더 중립적인 `llm_profiles`와 `llm_role_profiles` 매핑을 추가합니다. 그 Codex 매핑은 **모델이나 생성된 Codex 프로필을 고르지 않고** 호출별 추론 강도만 정합니다(fast: low, standard: medium, deep: high, frontier: xhigh). `config.yaml`에 명시한 모델 값이 있으면 그쪽이 이깁니다.

> **주의**: `~/.codex/config.toml`은 Ouroboros 단계별 모델을 고정하는 자리가 **아닙니다.** 설정 화면이나 `~/.ouroboros/config.yaml`의 해당 값을 쓰세요. 명시적 `--profile`이 필요하면 사용자가 관리하는 네이티브 Codex 프로필을 그대로 두면 됩니다.
>
> 오래 떠 있는 URL 기반 Ouroboros MCP 서버를 운영한다면 그 URL 항목을 `~/.codex/config.toml`에 두세요. `ouroboros setup --runtime codex`는 **기본적으로 그 항목을 보존합니다.** setup이 그 항목을 관리형 command-spawned 서버로 **교체하기를 의도적으로 원할 때만** `--mcp-mode stdio`를 쓰세요.

## 빠른 시작

> 첫 실행 흐름 전체(인터뷰 → seed → 실행)는 **[Getting Started](../getting-started.md)**(영문)를 보세요.

### 설치 확인

```bash
ouroboros --help
codex --version
```

> `codex --version`이 `command not found`로 나와도 **독립 설치 경로에서는 고장이 아닙니다.** 이 경로는 macOS ChatGPT 앱 번들 실행 파일만 있고 `PATH`에는 없는 사용자를 지원하며, setup은 그 번들을 찾아냅니다. 그 경우 setup이 기록한 경로로 확인하세요:
>
> ```bash
> ouroboros config show
> ```
>
> 출력의 **`CLI path:`** 줄이 setup이 실제로 해석한 실행 파일입니다(`cli/commands/config.py:696-701`). `codex_cli_path`라는 문자열은 출력에 나오지 않으니 그걸로 grep하지 마세요.
>
> `codex`를 `PATH`에 올린 경우와 권장(플러그인) 경로에서는 `codex --version`이 정상 확인 방법입니다.

---

## 명령 표면 (Command Surface)

사용자 눈에 Codex 연동은 **세션 지향 Ouroboros 런타임**으로 보입니다. Claude 런타임을 굴리는 것과 같은, 명세 우선(specification-first) 워크플로 하네스입니다.

내부적으로 `CodexCliRuntime`은 여전히 로컬 `codex` 실행 파일과 대화하지만, Codex 고유의 세션 ID와 resume 핸들을 보존하고, Codex 명령 디스패처가 `ooo` 스타일 스킬 명령을 인프로세스 Ouroboros MCP 서버로 넘길 수 있습니다.

`ouroboros setup --runtime codex`가 현재 하는 일:

- `PATH`에서 `codex` 바이너리를 찾습니다
- `~/.ouroboros/config.yaml`에 `orchestrator.runtime_backend: codex`와 `llm.backend: codex`를 씁니다
- Codex LLM 호출과 에이전트 런타임 세션용으로, 빠져 있는 공급자 중립 `llm_profiles`·`llm_role_profiles` 기본값을 채웁니다. 호출마다 reasoning effort가 적용되고 모델 핀은 걸지 않습니다
- 가능하면 `orchestrator.codex_cli_path`를 기록합니다
- 관리되는 Ouroboros 규칙을 `~/.codex/rules/`에 설치합니다
- 관리되는 Ouroboros 스킬을 `~/.codex/skills/`에 설치합니다
- `~/.codex/config.toml`에 Ouroboros MCP/env 연결이 없으면 등록하고, setup이 관리하는 stdio 블록은 갱신하되, 사용자가 관리하는 URL·커스텀 항목은 기본적으로 보존합니다
- 손대지 않은 레거시 자동생성 `ouroboros-*.config.toml` 태스크 프로파일 앵커만 정리합니다. 사용자가 만든 Codex 프로파일은 보존됩니다
- 관리되는 `ouroboros-worker.config.toml`을 등록해서, Agent OS 워커 서브프로세스가 MCP/env 연결을 잃지 않고도 대화형 Codex 기본값에서 빠질 수 있게 합니다

`~/.codex/` 밖의 전역 산출물도 함께 생깁니다. `ensure_config_dir()`가 `~/.ouroboros/data/`와 `~/.ouroboros/logs/`를 만들고([`setup.py:2632`](../../src/ouroboros/cli/commands/setup.py)), 설정이 처음이면 `~/.ouroboros/credentials.yaml`을 `0600` 권한으로 새로 씁니다([`setup.py:2771`](../../src/ouroboros/cli/commands/setup.py)).

`~/.codex/config.toml`은 **Ouroboros 스테이지 모델 핀을 둘 자리가 아닙니다.** 설정 UI나 그에 대응하는 `~/.ouroboros/config.yaml` 값을 쓰고, 명시적인 `--profile`이 필요할 때만 사용자가 관리하는 네이티브 Codex 프로파일을 유지하세요. 장기 실행 URL 기반 Ouroboros MCP 서버를 직접 운영한다면 그 URL 항목을 `~/.codex/config.toml`에 그대로 두면 됩니다. `ouroboros setup --runtime codex`가 기본적으로 보존합니다. setup이 그 항목을 관리형 command-spawn 서버로 **바꾸길 원할 때만** `--mcp-mode stdio`를 쓰세요.

### 워커 서브프로세스 격리 (Agent OS `runtime_profile`)

대화형 `codex` 세션과 Ouroboros가 띄우는 워커 서브프로세스는 서로 다른 기본값을 원할 때가 있습니다. 모델, 샌드박스, notify 훅 같은 것들이요. 오케스트레이터 수준의 런타임 프로파일을 `worker`로 두면, Ouroboros가 띄우는 모든 `codex exec` 호출이 관리형 `~/.codex/ouroboros-worker.config.toml` 프로파일을 쓰게 됩니다.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  runtime_profile:
    backend_profile: worker   # 선택. 기본값(미설정)은 지금 동작을 그대로 유지
```

한 번만 다르게 돌리고 싶으면 환경변수로:

```bash
OUROBOROS_RUNTIME_PROFILE=worker ouroboros run workflow --runtime codex seed.yaml
```

워커 오버라이드는 `~/.codex/ouroboros-worker.config.toml`에서 직접 고칩니다:

```toml
model = "o3-mini"
notify = []
sandbox = "workspace-write"
```

`runtime_profile`이 미설정이면(기본) Ouroboros는 예전과 똑같이 `codex exec`를 내보냅니다. 프로파일 플래그 없이, 사용자 설정을 전부 상속합니다. 이건 런타임 공통 Agent OS 프로파일 계약의 Codex 쪽 매핑이고, OpenCode·Hermes·Claude Code·LiteLLM은 각자의 백엔드 로컬 매핑을 따로 추가할 수 있습니다.

### Codex에서 쓸 수 있는 `ooo` 스킬

`ouroboros setup --runtime codex`를 돌리고 나면 번들 `ooo` 스킬이 `~/.codex/skills/ouroboros-*`에, 라우팅 규칙이 `~/.codex/rules/`에 설치됩니다. Ouroboros를 올린 뒤 **그 산출물만** 갱신하려면 `ouroboros codex refresh`를 쓰세요. `~/.codex/config.toml`도 `~/.ouroboros/config.yaml`도 건드리지 않습니다.

런타임 식별 지문은 사용자가 관리하는 규칙과 스킬을 계속 추적합니다. 다만 Codex Desktop이 자체적으로 갱신하는 예약 경로 `~/.codex/skills/.system`은 제외하며, 같은 `skills` 디렉터리의 사용자 스킬 변경은 이전처럼 런타임 식별 지문을 무효화합니다.

현재 스냅숏 기준 `resolve_packaged_codex_assets()`는 `skills/*/SKILL.md` 번들 **22개**를 해석해 설치합니다(맨 `ooo`와 `welcome` 포함). 아래 표는 그 전부와, 터미널만 쓰는 사람을 위한 CLI 대응입니다.

| `ooo` 스킬 | Codex 세션 | CLI 대응 (터미널) |
|-------------|---------------|--------------------------|
| `ooo` (인자 없이) | O | *(디스패처가 라우팅. 세션에서 시작점으로 씀)* |
| `ooo auto` | O | `ouroboros auto "goal"` (관리 규칙이 `ouroboros_start_auto`로 라우팅) |
| `ooo brownfield` | O | `ouroboros setup scan` / `setup list` / `setup default` |
| `ooo config` | O | `ouroboros config` (설정 화면. `config show`는 읽기 전용 출력이라 다릅니다) |
| `ooo pm` | O | `ouroboros pm` |
| `ooo resume-session` | O | `ouroboros resume` (진행 중 세션을 나열합니다. `run workflow --resume`는 그 탐색 단계를 건너뜁니다) |
| `ooo interview` | O | `ouroboros init start --llm-backend codex "your idea"` |
| `ooo seed` | O | *(`ouroboros init start`에 포함됨)* |
| `ooo run` | O | `ouroboros run workflow --runtime codex seed.yaml` |
| `ooo status` | O | `ouroboros status execution <execution_id>` |
| `ooo evaluate` | O | *(MCP 전용)* |
| `ooo evolve` | O | *(MCP 전용)* |
| `ooo ralph` | O | MCP가 소유하는 `ouroboros_ralph` 백그라운드 작업. job 도구로 모니터링 |
| `ooo cancel` | O | `ouroboros cancel execution <execution_id>` |
| `ooo unstuck` | O | *(MCP 전용)* |
| `ooo tutorial` | O | *(MCP 전용)* |
| `ooo welcome` | O | *(MCP 전용)* |
| `ooo update` | O | `ouroboros update` |
| `ooo help` | O | `ouroboros --help` |
| `ooo qa` | O | `ouroboros qa` |
| `ooo setup` | O | `ouroboros setup --runtime codex` |
| `ooo publish` | O | *(`ouroboros publish` 서브커맨드는 없음. 스킬/런타임 흐름이 `gh` CLI를 씀)* |

> **Ralph 주의 (#528):** `ooo ralph`는 이제 MCP가 소유하는 `ouroboros_ralph` 백그라운드 작업 하나를 시작하고, 표준 job 도구로 감시합니다. 스킬이 클라이언트 쪽 `evolve_step` 폴링으로 다세대 루프를 다시 구현하지 않습니다. 돌고 있는 Ralph를 멈추려면 MCP 작업 취소 도구 `ouroboros_cancel_job(job_id)`를 쓰세요. **`ouroboros cancel execution <execution_id>`는 실행 세션 전용이라 Ralph의 job ID를 취소하지 못합니다.**

> **`ooo seed`와 `ooo interview`의 차이:** 별개의 스킬이고 역할이 다릅니다. `ooo interview`는 소크라테스식 문답 세션을 돌리고 `session_id`를 돌려줍니다. `ooo seed`는 그 `session_id`를 받아 구조화된 Seed YAML을 만듭니다(모호도 채점 포함). 터미널에서는 두 단계가 `ouroboros init start` 한 번에 함께 수행됩니다.

> **`ooo publish` 주의:** Codex 세션에서 `ooo publish`는 setup이 관리형 규칙과 스킬을 설치한 뒤 스킬/런타임 표면으로 제공됩니다. 지금은 전용 `ouroboros publish` 셸 서브커맨드가 아니라, 외부 `gh` CLI와 GitHub 인증에 의존합니다.

Codex는 정확한 `ooo` 및 `/ouroboros:` 스킬 디스패치에 공통 무상태 `ouroboros.router` 리졸버를 씁니다. 명령을 추가하거나 바꾸려면 해당 `SKILL.md`의 frontmatter만 고치면 되고, 런타임은 로깅·메시지 조립·MCP 호출을 로컬에 유지합니다. [Shared `ooo` Skill Dispatch Router](../guides/ooo-skill-dispatch-router.md)(영문)를 보세요.

## 동작 방식

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |   Codex CLI     |
|  (your task)    |     | (runtime_factory)|     |   (runtime)     |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Codex executes  |
                        |  with its own    |
                        |  tool set and    |
                        |  sandbox model   |
                        +------------------+
```

`CodexCliRuntime` 어댑터는 `codex`(또는 `codex-cli`)를 전송 계층으로 띄우되, 세션 핸들·resume 지원·결정적 스킬/MCP 디스패치로 감싸서 런타임이 지속적인 Ouroboros 세션처럼 동작하게 합니다.

### 실행 파일 버전 증명 (attestation)

어댑터는 런타임을 만들 때 성공한 `codex --version` 증거를 선택된 경로, 실제 대상의 device/inode 쌍, 콘텐츠 다이제스트, 심링크 동일성과 함께 기록해 두고, 실행할 때마다 그 증거를 검증합니다. 정책은 fail-closed지만, **증거를 못 얻은 것과 변조가 확인된 것을 혼동하지 않습니다.**

- 초기화 중 타임아웃이나 실행 실패가 나면 긍정적 기준선이 남지 않으므로 실행이 차단되고, 새 런타임 세션이 필요합니다.
- 이후 검사에서 타임아웃이나 실행 실패가 나면 그 시도는 차단되지만 **증명 증거 없음**으로 보고됩니다. 같은 런타임을 다시 시도할 수 있습니다.
- 선택된 실행 파일을 `--version`으로 돌리기 **전에**, 어댑터는 실행하지 않는 방식으로 경로·콘텐츠·device/inode·완전한 의미론적 심링크 증거를 검증된 초기화 기준선과 비교합니다. 알려진 변조는 바뀐 후보를 실행하지 않고 거부합니다.
- 시작된 프로브는 타임아웃이나 실패로 끝나도 전부 사후 샘플링됩니다. 그 증거가 프로브 구간 중 변조를 입증하면, 변조가 일시적 프로브 결과보다 우선합니다.
- 상위 디렉터리의 generation 변화만으로는 실행 파일 동일성보다 범위가 넓습니다. 무관한 형제 항목이나 항목 교체 후 복원에서도 생길 수 있습니다. 그래서 이 경우는 실행 파일 변조가 확인됐다고 주장하지 않고, **재시도 가능한 불확정 권위**로 fail-closed 처리합니다.
- 버전 변조는 **성공한 버전 증명 두 개가 서로 다를 때만** 보고됩니다. 경로·콘텐츠·심링크·device/inode·프로브 구간 generation 변조는 두 번째 성공 프로브 전에 fail-closed될 수 있습니다. 증명이 두 번 없었다는 사실은 실행 파일이 안 바뀌었다는 증거가 되지 않습니다.

Copilot·Gemini·Goose·Grok 런타임도 같은 증명·비교 정책을 상속합니다.

> 전체 런타임 백엔드 비교는 [runtime capability matrix](../runtime-capability-matrix.md)(영문)를 보세요.

## Codex CLI의 강점

- **세션을 아는 Codex 런타임** — Ouroboros가 워크플로 단계 전반에서 Codex 세션 핸들과 resume 상태를 보존합니다
- **강한 코딩·추론** — Codex가 현재 선택한 모델을 쓰고, Ouroboros는 작업에 맞는 reasoning effort를 적용합니다
- **에이전틱 작업 실행** — 복잡한 작업을 순차 단계로 쪼개고 자율적으로 반복하는 데 강합니다
- **오픈소스** — Codex CLI는 오픈소스(Apache 2.0)라 열어보고 기여할 수 있습니다
- **Ouroboros 하네스** — 명세 우선 워크플로 엔진이 Codex CLI 위에 구조화된 검수 기준, 평가 원칙, 결정적 종료 조건을 얹습니다

## 런타임 차이

Codex CLI와 Claude Code는 **서로 독립적인 런타임 백엔드**이고, 도구 집합·권한 모델·샌드박싱 동작이 다릅니다. 같은 Seed 파일이 양쪽에서 동작하지만, 실행 경로는 달라질 수 있습니다.

| 항목 | Codex CLI | Claude Code |
|--------|-----------|-------------|
| 정체 | Codex CLI 전송을 쓰는 Ouroboros 세션 런타임 | Anthropic의 에이전틱 코딩 도구 |
| 인증 | Codex 계정 로그인 또는 OpenAI API 키 | Max Plan 구독 |
| 모델 | Codex의 현재 기본 모델 (권장) | Claude (claude-agent-sdk 경유) |
| 샌드박스 | Codex CLI 자체 샌드박스 모델 | Claude Code의 권한 시스템 |
| 도구 표면 | Codex 네이티브 도구 (파일 I/O, 셸) | Read, Write, Edit, Bash, Glob, Grep |
| 세션 모델 | 런타임 핸들·resume ID·스킬 디스패치로 세션 인지 | 네이티브 Claude 세션 컨텍스트 |
| 비용 모델 | Codex CLI에 설정된 경로를 따름 — Codex OAuth 또는 OpenAI API 키 | Max Plan 구독에 포함 |
| Windows (네이티브) | 미지원 | 실험적 |

> **참고:** Ouroboros의 워크플로 모델(Seed 파일, 검수 기준, 평가 원칙)은 런타임과 무관하게 동일합니다. 다만 Codex CLI와 Claude Code는 바탕이 되는 에이전트 능력·도구 접근·샌드박싱이 다르기 때문에, **같은 Seed 파일에서도 실행 경로와 결과가 달라질 수 있습니다.**

## CLI 옵션

### 워크플로 명령

```bash
# 워크플로 실행 (Codex 런타임)
# ouroboros init이 만든 seed는 ~/.ouroboros/seeds/seed_{id}.yaml에 저장됩니다
uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# 드라이런 (실행하지 않고 seed만 검증)
uv run ouroboros run workflow --dry-run ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# 디버그 출력 (로그와 에이전트 출력 표시)
uv run ouroboros run workflow --runtime codex --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# 이전 세션 이어서 실행
uv run ouroboros run workflow --runtime codex --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Seed 파일 레퍼런스

| 필드 | 필수 | 설명 |
|-------|----------|-------------|
| `goal` | O | 주 목표. 빈 문자열 불가 |
| `task_type` | X | 실행 전략. `code`(기본), `research`, `analysis`, `artifact`, `document`, `documentation`, `presentation` |
| `brownfield_context` | X | 기존 코드베이스 컨텍스트. 비어 있으면 greenfield로 취급 |
| `constraints` | X | 반드시 만족해야 하는 제약 |
| `acceptance_criteria` | X | 성공 판정 기준 |
| `ontology_schema` | O | 출력 구조 정의 |
| `evaluation_principles` | X | 평가 원칙 |
| `exit_conditions` | X | 종료 조건 |
| `metadata` | O | 생성 메타데이터 |
| `metadata.ambiguity_score` | X | 생성 시점 모호도. 기본 `0.15`, 허용 범위 `0.0`~`1.0` |

> **`ambiguity_score`의 0.2 임계값이 실제로 걸리는 지점:** 이 필드 자체는 `0.0`~`1.0`을 허용합니다([`core/seed.py:409`](../../src/ouroboros/core/seed.py)). 0.2 게이트는 **seed 생성 시점**에 걸립니다. 인터뷰가 0.2 아래로 못 내려가면 seed를 안 만들어 주는 것이고, 여기에는 명시적 우회 선택지가 있습니다. CLI의 "Generate Seed anyway", MCP의 `force` 파라미터입니다. 우회해도 **실제 점수는 그대로 메타데이터에 기록되고 감사 로그에 남습니다.**
>
> `ouroboros auto`는 실행 중에 readiness를 다시 보긴 하지만 **조건부이고, 억제되는 두 경우의 결과가 정반대입니다** ([`auto/grading.py:225-226`](../../src/ouroboros/auto/grading.py)):
>
> - **ledger 닫힘**(`closure_mode`가 `ledger_only`·`safe_default`이고 degraded가 아닐 때): **ledger의 구조적 완결성이 수용 신호이고 LLM이 매긴 점수는 설계상 낡은 값**이라, 0.2를 크게 웃도는 Seed도 A 등급을 받고 **실행됩니다.** 나머지 채점 축은 그대로 적용됩니다.
> - **degraded Seed**: 블로커가 억제되는 건 **부분 산출물을 내보내기 위해서일 뿐입니다.** 블로커가 없는 degraded Seed는 **등급이나 `may_run`과 무관하게** 곧바로 partial-product 종단(`AutoPhase.COMPLETE`)으로 갑니다([`auto/pipeline.py:1286`](../../src/ouroboros/auto/pipeline.py)). **RUN에는 절대 도달하지 않습니다.** 남아 있는 블로커는 하드 안전 블로커라 그대로 실행을 종료시킵니다.
>
> 실무적으로: 손으로 쓴 seed에 높은 `ambiguity_score`를 박아도 `ouroboros run workflow`가 막지는 않습니다. 이 값은 **강제 차단 장치가 아니라 출처 기록(provenance)**입니다.

## 문제 해결

### Codex CLI를 못 찾을 때

`codex` 또는 `codex-cli`가 설치돼 있고 `PATH`에서 보이는지 확인하세요:

```bash
which codex || which codex-cli
```

설치돼 있지 않다면 npm으로:

```bash
npm install -g @openai/codex
```

다른 설치 방법은 [Codex CLI README](https://github.com/openai/codex#readme)를 보세요.

### 인증 오류

Codex CLI는 두 가지 방식으로 인증할 수 있고, 어느 쪽인지는 Codex CLI 설정에 달려 있습니다. `$CODEX_HOME/auth.json`(`CODEX_HOME`이 없으면 `~/.codex/auth.json`)에 저장된 Codex 로그인을 쓰거나, OpenAI API 키를 씁니다.

OAuth 기반 Codex CLI라면:

```bash
codex login
```

API 키 기반 Codex CLI라면, 키가 설정돼 있고 선택된 모델에 접근 권한이 있는지 확인하세요:

```bash
echo $OPENAI_API_KEY  # 값이 있어야 합니다
```

### 헬스체크의 "Providers: warning"

**오케스트레이터 런타임 백엔드를 쓸 때는 정상입니다.** 이 경고는 LiteLLM 공급자를 가리키는데, 오케스트레이터 모드에서는 쓰지 않습니다.

### "EventStore not initialized"

데이터베이스는 `ouroboros config show`가 보여주는 활성 경로에 자동으로 생성됩니다.

## 비용

Codex CLI를 런타임 백엔드로 쓰면, 결제는 **Codex CLI에 설정된 인증·과금 경로를 따릅니다.** 설정에 따라 Codex OAuth일 수도, OpenAI API 키 직접 사용일 수도 있습니다. 비용을 좌우하는 것:

- Codex가 선택한 모델 (**Codex 기본 모델 사용**을 권장합니다)
- 작업 복잡도와 토큰 사용량
- 도구 호출 및 반복 횟수

현재 요율은 [OpenAI 가격 페이지](https://openai.com/pricing)를 확인하세요.

## Active Conductor와 Synapse

Codex CLI는 검증된 Synapse `inform`·`after_turn` 백엔드입니다. Ouroboros가 현재 턴이 끝난 뒤 같은 영속 Codex 스레드를 재개하고, 재개된 공급자 턴이 확인 응답을 낸 뒤에야 `applied`로 보고합니다. **라이브 체크포인트 `redirect`나 하드 `replace`는 광고하지 않습니다.**

구체적으로, Codex 런타임이 선언하는 세션 시그널 능력은 6개 중 3개입니다 ([`orchestrator/codex_cli_runtime.py:453`](../../src/ouroboros/orchestrator/codex_cli_runtime.py)):

| 능력 | Codex | 의미 |
|---|:---:|---|
| `inform_delivery` | O | 실행 중인 세션에 정보를 전달할 수 있음 |
| `background_reply` | O | 백그라운드에서 응답을 받을 수 있음 |
| `after_turn_delivery` | O | **현재 턴이 끝난 뒤에** 전달됨 |
| `checkpoint_redirect` | X | 턴 중간에 방향을 틀 수 없음 |
| `owned_turn_abort` | X | 진행 중인 턴을 중단시킬 수 없음 |
| `replacement_resume` | X | 세션을 교체해 재개할 수 없음 |

`codex exec resume <thread-id>`는 정확히 하나의 영속 스레드로 다시 들어갑니다. Synapse는 현재 턴이 **완료된 뒤에만** 시그널을 배출하므로, 이 능력 선언은 라이브 인터럽션을 주장하지 않습니다. **긴 턴 도중에 방향을 바꾸고 싶다면 그 턴이 끝날 때까지 기다려야 한다는 뜻입니다.**

> **참고 — 서브에이전트 팬아웃:** Codex는 세션 안에서 스스로 병렬화할 수 있지만, `codex mcp-server`는 `codex`와 `codex-reply`만 노출합니다. Codex의 네이티브 멀티에이전트 팀 도구는 외부 드라이버가 닿을 수 없습니다. 그래서 Ouroboros는 Codex 스레드를 재사용·연장할 수는 있어도 **Codex 자식들을 오케스트레이션하지는 못하고**, 서브에이전트 팬아웃은 인프로세스로 남습니다 (`subagent_orchestration=INTERNAL`, [`codex_cli_runtime.py:448`](../../src/ouroboros/orchestrator/codex_cli_runtime.py)).

이 경로가 **공개적으로 호출 가능해지는 것은 완전한 MCP 호스트 계층에서뿐입니다.** 그 계층이 탐색/전달 도구를 등록하고, run 및 Auto 실행과 하나의 Synapse 허브를 공유합니다. 계약만 있는 스택 계층이나 런타임만 있는 계층은 테스트와 수동 스모크 커버리지를 제공할 뿐, 그 자체로 공개 제어 경로를 노출하지는 않습니다.

`ooo run`/`ooo auto`가 도는 동안 메인 호스트는 **읽기 전용 관찰자 하나를 배타적으로** 유지하면서 런타임/모델 라우팅, 효율·절약 정책, 제한된 Discover 요약, 전체 의존/병렬 수준, 처음 스케줄된 AC들, 경로나 하네스 변경, 주의 사항, 최종 보증을 보고합니다. 사용자는 메인 세션에서 계속 대화할 수 있습니다. 호스트는 영향받는 AC를 **의미로 골라내며, 내부 ID를 묻지 않습니다.** 지침의 정본 언어는 영어지만, 호스트는 이 사실들을 사용자가 지금 쓰고 있는 대화 언어로 자연스럽게 표현합니다.

---

이 가이드는 여기서 끝납니다. 영문 원문은 [codex.md](./codex.md)입니다.
