# Source tour: three Ouroboros mechanisms and the lines that limit them

Product claims should be greppable. This page maps the three mechanisms our
docs talk about most to the exact lines that implement them, including the
limits. If you are writing a teardown, start here: these are the files we
would want you to judge us by.

## Stop 1: The success contract omits the answer key

`src/ouroboros/orchestrator/contract_redaction.py`

- `hidden_contract_variants` enumerates raw, quoted, and escaped forms for exact
  masking.
- `contains_transformed_hidden_contract_value` applies bounded, fail-closed
  HTML/Unicode/terminal normalization before retry output crosses into a worker
  prompt. Stateful terminal controls are rejected; harmless SGR styling may be
  normalized away.
- `retry_hints.py` assembles retry hints deterministically, with no model call.
  The command-output tail passes through the shared exact and transformed-value
  boundary before inclusion.

The claim's exact scope: when an acceptance criterion defines a verify
command or an expected-output assertion, those values are omitted from the
worker's contract block, with no flag to put them back. The scope is the
contract block: the docs say so, and
[`docs/hidden-checklist-convergence/requirements.md:17`](../hidden-checklist-convergence/requirements.md)
states the boundary explicitly.

## Stop 2: The ambiguity gate is a number, not a vibe

`src/ouroboros/bigbang/ambiguity.py`

- `ambiguity.py:37`: `AMBIGUITY_THRESHOLD = 0.2` gates seed generation.
- `ambiguity.py:48-50`: the score is weighted: goal clarity 0.40, constraint
  clarity 0.30, success-criteria clarity 0.30.
- `ambiguity.py:42-45`: per-axis floors (0.75 / 0.65 / 0.70, plus a fourth
  0.60 floor for brownfield context) mean one strong axis cannot carry two
  weak ones.
- `seed_generator.py:1770`: the domain gate; `force` is the one portable
  bypass, and it is explicit (`authoring_handlers.py:1464` passes it straight
  through).
- `interview_driver.py:125`: the unattended auto mode has its own readiness
  constant (`BACKEND_READY_AMBIGUITY_THRESHOLD`, also 0.20, defined
  independently).

The gate does not judge whether your idea is good. A request can score well
and still describe a bad idea clearly. It only guarantees that a vague spec
cannot exist without someone explicitly choosing it.

## Stop 3: Reviewer independence is a labeled state

`src/ouroboros/evaluation/reviewer_independence.py`

- `reviewer_independence.py:50`: evaluation runs carry one of four
  independence labels (`INDEPENDENT` is one of them, not the default
  assumption).
- `reviewer_independence.py:148`: `filter_voter_models` removes voters that
  share a vendor with the executor.
- `reviewer_independence.py:46` and `:168`: minimum viable jury is 2; if
  same-vendor removal would go below that, the original roster is rolled
  back rather than pretending a one-voter jury is a jury.
- `reviewer_independence.py:167`: voters of unknown vendor are not removed,
  because same-vendor cannot be proven. The conservative direction is to
  keep and label.
- `consensus.py:388`: the resulting state is recorded in the consensus
  output, so you can see what kind of jury judged a run.

## How to use this page

Choose one symbol above and verify its coordinate against current `main`. If
it moved, report the symbol and the old and new line numbers. That gives us
a documentation bug we can reproduce and fix without guessing.
