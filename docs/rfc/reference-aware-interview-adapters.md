# Reference-Aware Interview Adapters

Status: implemented v1 contract for issue #1239.

## Decision

References and glossary material may help users understand vocabulary and
compare examples, but they are not requirement authority. Ouroboros therefore
uses a derived pre-Seed `RequirementCandidate` projection instead of importing
auto-mode `SeedDraftLedger` semantics into the generic interview path.

The projection separates three axes:

| Axis | Values |
| --- | --- |
| Content source | `user_stated`, `reference_derived`, `model_inferred`, `repo_observed` |
| Resolution | `confirmed`, `needs_confirmation`, `unknown`, `conflicting` |
| Confirmation authority | `user`, `repo_evidence`, `none` |

User confirmation changes resolution and authority. It does not rewrite the
original content source. Evidence lineage is validated independently of model
output before promotion.

## Promotion

- Confirmed user statements may become hard requirements or acceptance
  criteria.
- Reference-derived and model-inferred candidates require explicit user
  confirmation.
- Repo-observed candidates may become context or existing-system constraints;
  desired future behavior still requires user authority.
- Required unknown or conflicting candidates block Seed generation and require
  reopening the interview.
- Optional unconfirmed candidates remain observable but are omitted from the
  Seed contract.

`RequirementDistillation` is an invalidatable read model. The transcript,
reference cues, contrast answers, and repository evidence remain canonical.
Persisted distillation is accepted only when schema version, input revision, and
input fingerprint still match.

## Interview Adapters

`ouroboros_interview` accepts optional `confused_terms` and `references`
parameters. They are strictly validated before plugin or in-process dispatch.
Start-turn adapter inputs are queued so the first question remains the normal
Socratic base-frame question.

After the first answer:

- explicit term confusion may inject at most three matching glossary entries;
- an unresolved reference cue produces one deterministic contrast question;
- reference URLs and file references are stored as user-provided cues only and
  are not fetched or read.

The v1 built-in `ui_ux_basics` YAML pack is a vocabulary aid, not an expert or
requirements source.

## Seed And Auto Boundaries

In-process and plugin Seed generation evaluate the same promotion policy before
constructing or delegating a Seed. For reference-aware in-process generation,
acceptance criteria are limited to promoted candidates so product references
cannot silently create contract fields.

The auto bridge maps only promoted candidates into `SeedDraftLedger`. Confirmed
reference-derived material maps as user-authorized preference, never as
`REPO_FACT` or `EXISTING_CONVENTION`. Required unconfirmed reference material
surfaces `reference_confirmation_required`.

## V1 Non-Goals

- persona or `DomainProfile` changes;
- vocabulary-density or repeated-failure triggers;
- custom adapter pack registration;
- web retrieval or reference expansion;
- legal, medical, or compliance packs;
- public Seed schema changes;
- implementation dependency on issue #1389.
