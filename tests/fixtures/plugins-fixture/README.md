# `plugins-fixture` — local mirror of Q00/ouroboros-plugins for E2E tests

This fixture mirrors the post-Sprint-1 layout of
[Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins) so the
E2E contract proof (`tests/integration/plugin/test_e2e.py`, Q00/ouroboros#733)
can run without cloning a remote repo or hitting the network.

It carries:

- `plugins/github-pr-ops/ouroboros.plugin.json` — manifest reflecting the
  locked decisions (8+2 fields, `merge` removed, single 3-value risk enum,
  schema_version `0.1`).
- `plugins/github-pr-ops/github_pr_ops/__main__.py` — a minimal Python
  entrypoint that the firewall subprocess-launches. The fixture
  entrypoint is **deterministic** and does not contact GitHub: it accepts
  argv, prints a JSON receipt to stdout, and uses its return code to
  signal success vs failure for the various E2E paths.

Updating the fixture: when the upstream `Q00/ouroboros-plugins` PRs
(#13/#16/#17/#19/#20) merge, run a follow-up sync to confirm the
fixture content still matches; the test asserts `merge` is absent so
any drift fails CI.
