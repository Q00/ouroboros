# #978 P4 TraceGuard vs legacy baseline benchmark

Fixture-only A/B benchmark. No live model calls, no default flip,
and no legacy self-report removal.

## Legacy self-report

```text
Fat-harness baseline report — profile=legacy_self_report_fixture · acs=8 · K=2
------------------------------------------------------------------------------
  [CAPT] one_shot_pass_rate                  : 0.8750 (target baseline + post-change measurement; target >= +10pp improvement)
  [FAIL] k_recovery_rate                     : 0.0000 (target >= 70% of initially failed ACs recover within K=2)
  [FAIL] fabrication_incidents_per_100_acs   : 25.0000 (target 0 verifier-detected fabrication incidents per 100 ACs)
  [CAPT] semantic_miss_incidents_per_100_acs : 25.0000 (target sample and report evidence-backed-but-semantically-wrong incidents per 100 ACs)
  [CAPT] median_chars_per_ac                 : 1295.0000 (target capture baseline median chars per AC)
  [PASS] new_domain_cost                     : 42 (target <= 50 LOC and <= 1 YAML for one new profile/domain)
------------------------------------------------------------------------------
  one_shot_pass_rate                     : 0.8750
  k_recovery_rate                        : 0.0000
  fabrication_incidents_per_100_acs      : 25.0000
  semantic_miss_incidents_per_100_acs    : 25.0000
  median_chars_per_ac                    : 1295.0000
  new_domain_loc_delta                   : 42
  new_domain_yaml_delta                  : 1
```

## TraceGuard deliver gate

```text
Fat-harness baseline report — profile=traceguard_deliver_gate_fixture · acs=8 · K=2
-----------------------------------------------------------------------------------
  [CAPT] one_shot_pass_rate                  : 0.5000 (target baseline + post-change measurement; target >= +10pp improvement)
  [PASS] k_recovery_rate                     : 0.7500 (target >= 70% of initially failed ACs recover within K=2)
  [PASS] fabrication_incidents_per_100_acs   : 0.0000 (target 0 verifier-detected fabrication incidents per 100 ACs)
  [CAPT] semantic_miss_incidents_per_100_acs : 12.5000 (target sample and report evidence-backed-but-semantically-wrong incidents per 100 ACs)
  [CAPT] median_chars_per_ac                 : 1820.0000 (target capture baseline median chars per AC)
  [PASS] new_domain_cost                     : 42 (target <= 50 LOC and <= 1 YAML for one new profile/domain)
-----------------------------------------------------------------------------------
  one_shot_pass_rate                     : 0.5000
  k_recovery_rate                        : 0.7500
  fabrication_incidents_per_100_acs      : 0.0000
  semantic_miss_incidents_per_100_acs    : 12.5000
  median_chars_per_ac                    : 1820.0000
  new_domain_loc_delta                   : 42
  new_domain_yaml_delta                  : 1
```

## TraceGuard + claim-term guard

```text
Fat-harness baseline report — profile=traceguard_plus_claim_term_guard_fixture · acs=8 · K=2
--------------------------------------------------------------------------------------------
  [CAPT] one_shot_pass_rate                  : 0.5000 (target baseline + post-change measurement; target >= +10pp improvement)
  [PASS] k_recovery_rate                     : 0.7500 (target >= 70% of initially failed ACs recover within K=2)
  [PASS] fabrication_incidents_per_100_acs   : 0.0000 (target 0 verifier-detected fabrication incidents per 100 ACs)
  [CAPT] semantic_miss_incidents_per_100_acs : 0.0000 (target sample and report evidence-backed-but-semantically-wrong incidents per 100 ACs)
  [CAPT] median_chars_per_ac                 : 1820.0000 (target capture baseline median chars per AC)
  [PASS] new_domain_cost                     : 42 (target <= 50 LOC and <= 1 YAML for one new profile/domain)
--------------------------------------------------------------------------------------------
  one_shot_pass_rate                     : 0.5000
  k_recovery_rate                        : 0.7500
  fabrication_incidents_per_100_acs      : 0.0000
  semantic_miss_incidents_per_100_acs    : 0.0000
  median_chars_per_ac                    : 1820.0000
  new_domain_loc_delta                   : 42
  new_domain_yaml_delta                  : 1
```

## Delta

- Fabrication incidents per 100 ACs: -25.0000
- Semantic-miss incidents per 100 ACs: -12.5000
- Median chars ratio: 1.4054
- Claim-term guard semantic-miss incidents per 100 ACs: -12.5000
- Claim-term guard median chars ratio: 1.4054

## Gate interpretation

- TraceGuard reduces fixture fabrication incidents to 0 per 100 ACs.
- The deterministic claim-term guard rejects the fixture semantic miss without reintroducing fabrication.
- One-shot pass rate drops because unsupported legacy self-reports are rejected instead of counted as accepted.
- Median chars stay within the <= 1.5x C.4 budget guardrail.
