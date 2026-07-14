---
doc_kind: project-work
status: working
version: 2026-07-14_v1
canonical_path: self
---

# 1 ChatGPT Host Bridge 작업 계약

이 문서는 Q00/ouroboros Full host bridge 구현의 기준선과 소유 경계를 기록한다.

## 1.1 기준선

| 항목 | 값 |
|---|---|
| Canonical remote | https://github.com/Q00/ouroboros.git |
| Audited tag | v0.50.3 |
| Audited commit | cf15274b6a2b992a40feb3573e353e6cfd94ceae |
| Implementation branch | feat/chatgpt-host-bridge |
| Starting HEAD | c4beb2276b125b52852816aa185da26f6cc549c5 |

## 1.2 소유 경계

- Q00/ouroboros Full은 Interview, Seed, Auto, Evaluate, Ralph, Evolve, Resume와 EventStore를 계속 소유한다.
- Host bridge는 기존 Full dispatch를 typed work order로 변환하고 completion receipt를 기존 EventStore에 반영한다.
- Host bridge는 모델, agent CLI 또는 별도 workflow engine을 실행하지 않는다.

## 1.3 Gate A

Task 2부터 Task 4까지 typed dispatch, idempotent completion, terminal semantics와 하나의 Full lineage를 검증하기 전에는 GTM packaging production code를 작성하지 않는다.

## 1.4 변경 이력

- 2026-07-14 v1 — 감사 기준선과 Full-only 소유 계약을 기록했다.
