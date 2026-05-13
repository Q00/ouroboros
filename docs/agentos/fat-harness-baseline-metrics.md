# #961 fat-harness baseline metrics capture

This is the recorded fixture baseline for the `agentos-substrate-wiring` gate.
It captures the five #830/#961 hard-gate metrics without live model calls,
without `parallel_executor` wiring, and without changing `ooo run` defaults.

```text
Fat-harness baseline report — profile=fat_harness_fixture_baseline · acs=8 · K=2
--------------------------------------------------------------------------------
  [CAPT] one_shot_pass_rate                  : 0.5000 (target baseline + post-change measurement; target >= +10pp improvement)
  [PASS] k_recovery_rate                     : 0.7500 (target >= 70% of initially failed ACs recover within K=2)
  [PASS] fabrication_incidents_per_100_acs   : 0.0000 (target 0 verifier-detected fabrication incidents per 100 ACs)
  [CAPT] median_chars_per_ac                 : 1820.0000 (target capture baseline median chars per AC)
  [PASS] new_domain_cost                     : 42 (target <= 50 LOC and <= 1 YAML for one new profile/domain)
--------------------------------------------------------------------------------
  one_shot_pass_rate                     : 0.5000
  k_recovery_rate                        : 0.7500
  fabrication_incidents_per_100_acs      : 0.0000
  median_chars_per_ac                    : 1820.0000
  new_domain_loc_delta                   : 42
  new_domain_yaml_delta                  : 1
```

## Source sample rows

| AC | source | accepted | attempts | fabrication | chars | note |
|---|---|---:|---:|---:|---:|---|
| FH-AC-001 | `fixture:thin-skill/decompose/accepted-first-try` | yes | 1 | 0 | 1600 | Verifier accepted the first atomic AC attempt. |
| FH-AC-002 | `fixture:thin-skill/evidence/accepted-first-try` | yes | 1 | 0 | 1620 | Evidence manifest matched the expected file claim. |
| FH-AC-003 | `fixture:profile/code/accepted-first-try` | yes | 1 | 0 | 1500 | Profile-aware prompt stayed inside the existing wrapper contract. |
| FH-AC-004 | `fixture:verifier/pass/accepted-first-try` | yes | 1 | 0 | 1760 | Verifier accepted without retry or redispatch. |
| FH-AC-005 | `fixture:retry/recovered-on-second-attempt` | yes | 2 | 0 | 1930 | Initial evidence miss recovered within K=2. |
| FH-AC-006 | `fixture:retry/recovered-on-third-attempt` | yes | 3 | 0 | 2060 | Second retry produced accepted evidence within K=2. |
| FH-AC-007 | `fixture:blocked/recovered-on-second-attempt` | yes | 2 | 0 | 1880 | Typed blocked evidence was resolved by a retry inside budget. |
| FH-AC-008 | `fixture:retry/unrecovered-after-budget` | no | 3 | 0 | 2200 | Retry budget exhausted; counted in recovery denominator only. |

## New-domain cost source

- Source: `docs/rfc/contract-ledger.md profile fixture adapter sketch`
- LOC delta: 42
- YAML delta: 1

## Gate conclusion

- 1-shot AC pass rate is captured as the baseline for later post-change comparison.
- K=2 recovery rate is measured against the >= 70% gate.
- Fabrication incidents are measured as verifier-detected incidents per 100 ACs.
- Median chars per AC is captured as the token-budget proxy baseline.
- New-domain cost is measured against <= 50 LOC + <= 1 YAML.
