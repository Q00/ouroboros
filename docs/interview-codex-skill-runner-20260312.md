# Interview: Codex CLI Skill Runner

> Session ID: `interview_20260311_165459`
> Date: 2026-03-12
> Backend: Codex (OUROBOROS_LLM_BACKEND=codex)

---

## Context

Codex CLI를 메인 호스트로 사용할 때, Claude Code 플러그인 생태계의 스킬(skills/)을 실행할 수 있게 만들고 싶다. 현재 Claude Code에서는 `.claude/commands/`와 `skills/` 디렉토리의 SKILL.md를 읽어 실행하는 구조인데, Codex CLI에는 이 메커니즘이 없다. 훅(hooks)은 없어도 괜찮지만, 스킬만이라도 Codex에서 돌아갈 수 있는 방법을 찾고 싶다.

참고: [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) — tmux 기반 codex 세션 관리 프로젝트

---

## Architecture

```
Codex CLI (메인 호스트)
  ├── ~/.codex/rules/ouroboros.md      ← 자연어 가이드 (ooo setup이 설치)
  ├── ~/.codex/skills/ouroboros-*/     ← 스킬 self-contained 복사 (ooo setup이 설치)
  └── MCP: ouroboros                   ← MCP 도구 (interview/execute_seed/evaluate...)
       │
       └── codex_cli_runtime.py
            ├── exact prefix 감지 (ooo interview, ooo run 등)
            │   ├── → SKILL.md frontmatter(mcp_tool/mcp_args)로 dispatch
            │   ├── → 기본 파싱 (prefix + 첫 인자 분리)
            │   └── MCP 실패 시 → Codex pass-through (경고 로그)
            └── prefix 미매치 → Codex에 그대로 넘김
```

---

## Decisions

### Approach

| # | Question | Decision |
|---|----------|----------|
| Q1 | 구현 방식 | 3가지 병행: Ouroboros 내부 해결 + Codex CLI 확장 + MCP 도구 노출 |
| Q2 | 라우터 source of truth | 기존 keywords.py/registry.py를 단일 라우터로 유지. Codex에 seamless하게 맞춤 |
| Q3 | 호환 범위 | 단계적. 1단계: 트리거 인식 + MCP 위임, 2단계: SKILL.md 전체 실행 의미론 호환 |

### Interception

| # | Question | Decision |
|---|----------|----------|
| Q4 | 가로채기 방식 | 둘 다 지원. Codex rules로 안내 + 실패 시 Ouroboros fallback |
| Q6 | 타이밍 | 즉시/결정적. ooo 트리거 감지 시 Codex 모델 거치지 않고 Ouroboros가 즉시 처리 |
| Q7 | 가로채기 대상 | exact prefix만 (ooo run, ooo interview, /ouroboros:...). 자연어 변형은 rules 가이드에 위임 |

### Dispatch

| # | Question | Decision |
|---|----------|----------|
| Q8 | 실행 경로 | 내부 로직은 기존 경로 그대로, 출력/UX는 Codex 환경에 맞게 조정 가능 |
| Q9 | UX 방식 | MCP 도구로 라운드별 UX. TTY takeover 안 함 |
| Q10 | 상태 관리 | 이미 ouroboros_interview가 session_id 기반 stateful 프로토콜로 동작 중 |
| Q11 | MCP 도구 매핑 | 이미 대부분 MCP 도구 존재 (interview, execute_seed, evaluate, evolve_step, session_status, lateral_think, generate_seed, qa) |
| Q12 | dispatch table | SKILL.md에 이미 있지만, 효율적이면 별도 dispatch table 생성 OK |

### SKILL.md Frontmatter

| # | Question | Decision |
|---|----------|----------|
| Q14 | 인자 문법 | 좋은 서브셋으로 축소 OK. 핵심 인자만 지원, 점진적 확장 |
| Q16 | 인자 전달 | 기본 파싱 해줌. prefix + 첫 번째 인자 분리하여 MCP 파라미터에 매핑 |
| Q15 | 스키마 검증 | 안 함. prefix 매치 시 무조건 MCP 호출, 인자 검증은 MCP 도구 책임 |
| Q17 | 매핑 소스 | SKILL.md frontmatter에서 동적 파싱. 스킬 추가 시 dispatch table 자동 확장 |
| Q18 | 매핑 구조 | 1:1. 하나의 prefix에 하나의 MCP 도구 |
| Q19 | frontmatter 필드 | `mcp_tool`, `mcp_args` 필드를 SKILL.md frontmatter에 추가 |

### Frontmatter Example

```yaml
---
name: interview
description: "Socratic interview to crystallize vague requirements"
mcp_tool: ouroboros_interview
mcp_args:
  initial_context: "$1"
  cwd: "$CWD"
---
```

### Error Handling

| # | Question | Decision |
|---|----------|----------|
| Q13 | 매핑 미등록 시 | 경고 로그 + Codex pass-through |
| Q27 | MCP 호출 실패 시 | Codex pass-through |

### Installation & Lifecycle

| # | Question | Decision |
|---|----------|----------|
| Q21 | 설치 위치 | `~/.codex/skills/ouroboros-*`에 self-contained 복사 |
| Q22 | 설치 소스 | PyPI 패키지 안의 skills/ 디렉토리에서 복사 |
| Q23 | 네임스페이스 | `ouroboros-` prefix로 충돌 방지 |
| Q24 | 설치 형태 | Self-contained 복사. 프로젝트 없어도 동작 |
| Q25 | rules 설치 | `ooo setup/update`가 `~/.codex/rules/`에도 설치/갱신/prune |
| Q26 | 업데이트 흐름 | `ooo interview`: 버전 체크 + 알림만. `ooo update`: 실제 업그레이드 + skills/rules refresh + prune |
| Q27 | prune | `ooo update` 시 패키지에서 사라진 `ouroboros-*` 스킬 삭제 |

---

## Phase 1 Acceptance Criteria (Smoke Test)

1. `ooo interview "topic"` → `ouroboros_interview` MCP dispatch 성공
2. `ooo run seed.yaml` → `ouroboros_execute_seed` MCP dispatch 성공
3. frontmatter 누락 스킬 → 경고 + Codex pass-through
4. MCP 실패 → Codex pass-through
5. `ooo setup` → `~/.codex/skills/ouroboros-*` + `~/.codex/rules/` 설치
6. `ooo update` → refresh + prune

## Phase 2 (Future)

- SKILL.md 전체 실행 의미론 호환
- 에이전트 역할 주입
- 상대경로 자산/스크립트 참조 해석
- 자연어 트리거 감지 강화

---

## Next

`ooo seed` to crystallize these requirements into a specification
