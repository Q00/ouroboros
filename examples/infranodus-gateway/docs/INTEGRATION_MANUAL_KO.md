# Ouroboros × InfraNodus Gateway 통합 매뉴얼

이 문서는 `ouroboros-infranodus-gateway`를 설치하고 MCP 호스트에 연결한 뒤, Ouroboros의 seed·정체·delivery 단계에서 안전하게 사용하는 전체 운영 절차입니다.

## 1. 통합 원칙

이 Gateway는 Ouroboros를 대체하거나 자동 제어하지 않습니다. InfraNodus가 제공하는 그래프 분석은 사람에게 참고 신호를 주는 advisory 계층이며, 실행·승인·QA의 최종 권한은 계속 Ouroboros와 운영자에게 있습니다.

```text
Ouroboros 작업 흐름
       |
       | 운영자가 안전한 요약문만 전달
       v
Codex 또는 MCP 호스트
       |
       | stdio / 정확히 3개 도구
       v
Ouroboros × InfraNodus Gateway
       |
       | 고정 API 주소 + doNotSave=true
       v
InfraNodus 분석 API
```

v1에서 제공하는 도구는 다음 세 개뿐입니다.

| 도구 | 호출 시점 | 역할 | 최종 결정권 |
|---|---|---|---|
| `graph_review_seed` | seed 생성 후, `ooo run` 전 | 요구사항과 seed의 개념적 누락 비교 | 운영자 |
| `graph_diagnose_stagnation` | 동일 가정이 반복될 때 | 새로운 관점 후보 탐색 | Ouroboros 실행 흐름과 운영자 |
| `graph_compare_delivery` | 산출물 완성 후, 최종 수락 전 | 수락 기준과 검증 증거의 누락 비교 | `ooo qa`와 운영자 |

## 2. 사전 조건

- macOS 또는 Node.js를 실행할 수 있는 POSIX 환경
- Node.js 22.x
- npm
- Ouroboros CLI 0.50.5 이상
- 유효한 `INFRANODUS_API_KEY`
- stdio MCP를 지원하는 호스트. 이 매뉴얼에서는 Codex CLI/App을 기준으로 설명합니다.

현재 환경을 확인합니다.

```bash
node --version
npm --version
ooo --version
codex --version
```

Node.js는 `v22.x`여야 합니다. 이 저장소의 `package.json`은 Node 22만 허용합니다.

## 3. 설치와 빌드

```bash
cd /Users/heomin/Projects/ouroboros-infranodus-gateway
npm install
npm run typecheck
npm test
npm run build
```

정상 기준:

- `typecheck` 종료 코드 0
- 전체 테스트 PASS
- `dist/stdio.js` 생성
- API 키가 저장소 파일이나 로그에 출력되지 않음

프로덕션 의존성도 확인합니다.

```bash
npm audit --omit=dev
```

High 또는 Critical 취약점이 있으면 MCP 등록 전에 중단합니다.

## 4. API 키 준비

API 키는 저장소, `config.toml`, 명령 인자, 문서에 직접 기록하지 않습니다. MCP 호스트를 실행한 부모 환경에만 제공합니다.

현재 셸에서 키의 존재 여부만 확인합니다. 값은 출력하지 않습니다.

```bash
test -n "$INFRANODUS_API_KEY" && echo "INFRANODUS_API_KEY is set" || echo "INFRANODUS_API_KEY is missing"
```

키를 새로 설정해야 한다면 사용하는 비밀관리 도구나 셸의 비공개 세션에서 `INFRANODUS_API_KEY` 환경변수로 주입합니다. 셸 히스토리에 실제 값을 남기지 마십시오.

## 5. Codex MCP 등록

먼저 API 키를 macOS Keychain의 `ouroboros-infranodus-gateway` 서비스 항목에 저장합니다. Gateway의 [Keychain launcher](../scripts/start-codex-mcp.zsh)가 키를 읽어 자식 MCP 프로세스에만 주입합니다. 키 값은 TOML·저장소·셸 히스토리에 기록하지 않습니다.

Codex 설정 파일 `~/.codex/config.toml`에는 다음 항목을 추가합니다.

```toml
[mcp_servers.ouroboros_infranodus]
command = "/Users/heomin/Projects/ouroboros-infranodus-gateway/scripts/start-codex-mcp.zsh"
args = []
cwd = "/Users/heomin/Projects/ouroboros-infranodus-gateway"
startup_timeout_sec = 10
tool_timeout_sec = 35
```

이 설정은 현재 Codex의 실제 stdio MCP 형식인 `command`, `args`, `cwd`를 사용합니다. `env_vars`는 이 환경의 `mcp_servers` 설정 필드가 아니므로 사용하지 않습니다.

Keychain launcher가 올바른 Node 22 runtime을 사용하도록 다음을 확인합니다.

```bash
/Users/heomin/.hermes/node/bin/node --version
```

`codex mcp add --env INFRANODUS_API_KEY=...`, `[mcp_servers.ouroboros_infranodus.env]`, `.zshrc`에 실제 키를 쓰는 방식은 설정 파일·셸 히스토리에 값이 남을 수 있으므로 사용하지 않습니다.

설정 후 Codex를 다시 시작하거나 새 세션을 열고 등록 상태를 확인합니다.

```bash
codex mcp get ouroboros_infranodus
```

정상 상태:

- `enabled: true`
- `transport: stdio`
- command가 Node 실행 파일
- args가 이 저장소의 `dist/stdio.js`
- API 키 값 자체는 출력되지 않음

## 6. MCP 프로토콜 사전 검증

실제 InfraNodus를 호출하기 전에 로컬 가짜 upstream을 사용하는 stdio 종단간 검증을 실행합니다.

```bash
cd /Users/heomin/Projects/ouroboros-infranodus-gateway
npm run test:stdio
```

이 검증은 실제 자식 stdio MCP 프로세스를 시작하고 다음을 확인합니다.

- 도구가 정확히 세 개만 노출됨
- 모든 도구가 `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`
- 세 도구 모두 MCP 호출에 응답함
- 테스트용 URL 재작성은 `tests/rewrite-fetch.mjs` preload 내부에만 존재함
- 프로덕션 진입점은 InfraNodus의 고정 HTTPS API 주소만 사용함

## 7. 안전한 입력 작성법

모든 도구는 동일한 입력 필드를 받습니다.

```json
{
  "objective": "달성해야 할 목표나 수락 기준을 안전한 산문으로 요약",
  "candidate": "검토할 seed, 현재 접근법 또는 delivery 증거를 안전한 산문으로 요약"
}
```

좋은 입력 예시:

```json
{
  "objective": "로그인 실패 시 사용자가 복구 경로를 확인할 수 있어야 한다.",
  "candidate": "현재 산출물은 로그인 성공 흐름과 오류 메시지를 검증했지만 복구 흐름 증거는 포함하지 않는다."
}
```

보내면 안 되는 내용:

- API 키, Bearer/JWT, GitHub·OpenAI·AWS 형태 토큰
- PEM private key
- 이메일, 전화번호, 주민번호형 식별자, 카드번호
- `https://...` 또는 `www...` URL
- 소스코드, 코드 블록, raw stack trace
- 원본 인터뷰 전문, 사용자 개인정보, 저장된 그래프 이름
- 20,000자를 넘는 분석문 또는 64 KiB를 넘는 요청

정책에 거부되면 원문을 억지로 분할하거나 필터를 우회하지 말고, 민감정보를 제거한 짧은 산문 요약으로 다시 작성합니다.

## 8. Gate 1: seed 검토

### 8.1 Ouroboros seed 생성

```bash
ooo interview start
ooo interview list
ooo seed SESSION_ID
```

인터뷰가 완료된 후 생성된 seed를 그대로 보내지 말고 목표·범위·검증 기준만 요약합니다.

### 8.2 `graph_review_seed` 호출

- `objective`: 인터뷰에서 확정한 요구사항과 성공 조건 요약
- `candidate`: 생성된 seed의 목표·범위·검증 절차 요약

판단 규칙:

1. `status=OK`이면 `observations`를 검토합니다.
2. 실제 누락으로 판단되는 항목만 seed에 반영합니다.
3. 관련 없거나 중복인 신호는 이유를 남기고 기각합니다.
4. 사람이 seed를 승인한 후에만 `ooo run`을 시작합니다.

InfraNodus 응답은 seed를 자동 수정하거나 실행을 시작하지 않습니다.

## 9. Gate 2: 정체 진단

먼저 Ouroboros의 읽기 전용 trace surface로 정체가 실제인지 확인합니다.

```bash
ooo harness list
ooo harness show RUN_ID
ooo harness frontier --metric METRIC
```

`frontier`는 특정 run ID를 받지 않고, 이미 export된 run들을 `outcome.json`의 지정 metric으로 순위화합니다. 단일 run을 확인할 때는 `show RUN_ID`를 사용합니다.

그다음 `graph_diagnose_stagnation`에 전달합니다.

- `objective`: 원래 달성하려던 결과
- `candidate`: 반복 중인 가정, 시도한 접근, 관찰된 실패를 코드 없이 요약

`observations` 중 한 항목을 새로운 가설로 선택해 Ouroboros 흐름에서 검증합니다. 응답을 자동 명령, 자동 lateral event 또는 성공 판정으로 취급하지 않습니다.

Ouroboros 0.50.5에는 top-level `unstuck` 명령이 없습니다. 따라서 이 단계는 운영자가 명시적으로 호출해야 합니다.

## 10. Gate 3: delivery 비교

산출물 검증 후 `graph_compare_delivery`를 호출합니다.

- `objective`: 수락 기준과 보안·동작·관찰 가능성 요구사항
- `candidate`: 실제 실행한 테스트, runtime 관찰, 증거 파일을 산문으로 요약

그래프 신호를 검토한 다음 Ouroboros의 권위 있는 QA를 실행합니다.

```bash
ooo qa ARTIFACT
```

`graph_compare_delivery`가 `OK`여도 `ooo qa`를 생략할 수 없습니다. InfraNodus는 보조 검토자이며 최종 품질 게이트가 아닙니다.

## 11. 응답 해석

정상 응답은 다음 경계를 가집니다.

```json
{
  "status": "OK",
  "operation": "graph_review_seed",
  "summary": "...",
  "observations": ["..."],
  "nextActions": ["..."],
  "provenance": {
    "provider": "infranodus",
    "mode": "no-save",
    "endpoint": "/graphsAndStatements",
    "cache": "miss"
  }
}
```

출력 제한:

- observation 최대 8개
- next action 최대 5개
- 각 항목 최대 280자
- raw graph, statement, 요청 원문, upstream 오류 본문, 숫자형 confidence 없음

`status=DEGRADED_NO_GRAPH`이면 InfraNodus 장애나 timeout입니다. 실패를 승인으로 해석하지 말고, 로컬 Ouroboros gate를 계속 진행하면서 그래프 조언을 사용할 수 없었다고 기록합니다.

## 12. 실 API 무저장 검증

배포 전이나 Gateway 업데이트 후 다음 검증을 실행합니다.

```bash
cd /Users/heomin/Projects/ouroboros-infranodus-gateway
GATEWAY_STATE_DIR="$PWD/.gateway-state" \
LIVE_EVIDENCE_PATH="$PWD/evidence/live-verification.json" \
npm run verify:live
```

검증기는 다음 순서로 동작합니다.

1. `/listGraphs`로 호출 전 inventory count와 semantic digest를 계산합니다.
2. 개인정보가 없는 고정 fixture로 세 작업을 각각 한 번 호출합니다.
3. 모든 응답을 `GraphAdvice` schema로 검증합니다.
4. 호출 후 inventory를 다시 계산합니다.
5. count 또는 digest가 달라지면 실패합니다.

성공 증거에는 그래프 이름, API 키, 입력 원문, observation 내용이 포함되지 않습니다.

## 13. 로컬 상태와 권한

`GATEWAY_STATE_DIR`을 설정하면 Gateway가 metadata ledger를 생성합니다.

```text
.gateway-state/             mode 0700
└── ledger.jsonl            mode 0600
```

ledger에 저장되는 값:

- timestamp
- operation
- 정규화된 요청의 SHA-256 hash
- `OK` 또는 `DEGRADED_NO_GRAPH`
- `hit`, `miss`, `bypass`

요청 원문, API 키, observation, 그래프 이름은 저장하지 않습니다. 분석 결과 cache는 프로세스 메모리에만 존재하며 재시작하면 사라집니다.

권한을 확인합니다.

```bash
stat -f '%N mode=%Lp' .gateway-state .gateway-state/ledger.jsonl
```

## 14. 장애 대응

### MCP 서버가 시작되지 않음

```bash
codex mcp get ouroboros_infranodus
command -v node
test -f /Users/heomin/Projects/ouroboros-infranodus-gateway/dist/stdio.js
test -n "$INFRANODUS_API_KEY"
```

빌드 파일이 없으면 `npm run build`를 실행합니다. API 키 값은 진단 출력에 넣지 않습니다.

### `INPUT_REJECTED`

민감정보, URL, 코드, 크기 제한을 확인하고 안전한 산문 요약으로 다시 작성합니다. 정책 정규식을 임시로 약화하지 않습니다.

### `DEGRADED_NO_GRAPH`

- Ouroboros 로컬 작업은 계속할 수 있습니다.
- 그래프 분석을 성공으로 간주하지 않습니다.
- 키 존재 여부와 InfraNodus 가용성을 확인합니다.
- 같은 요청을 무한 재시도하지 않습니다.

### inventory가 변경됨

1. Gateway 사용을 즉시 중단합니다.
2. MCP 등록을 비활성화합니다.
3. `evidence/live-verification.json`을 보존합니다.
4. InfraNodus 계정의 실제 graph inventory를 사람이 검토합니다.
5. 원인이 규명되기 전에는 재활성화하지 않습니다.

### stdout 프로토콜 오류

stdio 서버 stdout은 JSON-RPC 전용입니다. `console.log`나 일반 로그를 추가하지 마십시오. 운영 메시지는 stderr 또는 bounded MCP result로만 처리합니다.

## 15. 업데이트 절차

Gateway, Node, MCP SDK 또는 InfraNodus 계약을 업데이트할 때:

```bash
git status --short
npm install
npm run typecheck
npm test
npm run test:stdio
npm run build
npm audit
```

그다음 실 API 무저장 검증을 다시 실행합니다. 다음 변경은 단순 업데이트가 아니라 별도 보안 단계로 취급합니다.

- 저장된 graph 읽기
- GraphRAG
- URL ingestion
- HTTP/SSE transport
- persistent advice cache
- Ouroboros 자동 lifecycle hook
- InfraNodus write operation
- 프로덕션 API base override

이 항목에는 새 threat model, 사용자 승인, before/after inventory 증거, 롤백 검증이 필요합니다.

## 16. 롤백과 제거

Codex에서 MCP 등록을 제거합니다.

```bash
codex mcp remove ouroboros_infranodus
```

실행 중인 stdio 프로세스가 있다면 종료합니다. Gateway는 Ouroboros DB나 InfraNodus graph를 migration하지 않으므로 별도 데이터 롤백은 없습니다.

`.gateway-state`는 hash metadata만 포함하지만 삭제가 필요하면 먼저 증거 보존 정책을 확인하고 명시적으로 승인받습니다. 자동 삭제하지 않습니다.

## 17. 운영 체크리스트

### 최초 설치

- [ ] Node.js 22 확인
- [ ] `npm install`, typecheck, test, build PASS
- [ ] `npm audit --omit=dev`에서 High/Critical 없음
- [ ] API 키가 환경변수로만 전달됨
- [ ] Codex MCP 등록에서 정확한 Node·dist 경로 확인
- [ ] `npm run test:stdio` PASS
- [ ] `npm run verify:live`에서 inventory 불변 확인

### 매 호출

- [ ] 원문이 아니라 안전한 산문 요약만 사용
- [ ] 비밀정보·PII·URL·코드 제거
- [ ] GraphAdvice를 advisory로만 사용
- [ ] `DEGRADED_NO_GRAPH`를 성공으로 해석하지 않음
- [ ] 최종 결정은 Ouroboros gate와 사람이 수행

### 업데이트 후

- [ ] 전체 테스트와 stdio E2E 재실행
- [ ] dependency audit 재실행
- [ ] 실 API inventory 전후 digest 확인
- [ ] 도구 목록이 정확히 세 개인지 확인
- [ ] 새로운 write·GraphRAG·자동 hook이 추가되지 않았는지 확인

## 관련 문서

- [프로젝트 개요](../README.md)
- [Ouroboros 단계별 런북](OUROBOROS_RUNBOOK.md)
- [보안 경계](../SECURITY.md)
- [최근 실 API 검증 증거](../evidence/live-verification.json)
