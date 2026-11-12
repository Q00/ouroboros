"""Example questions chosen by execution: probe runs and the deterministic core.

Several candidate solutions, each written under a different reading of a
goal, run the same small probes. Where their outputs differ, the requester
is asked which output is right. This package holds the part of that flow
that writes the candidates, runs the probes and decides where outputs
differ. Models only state readings and write candidates; no model decides
anything and no pattern or keyword rule decides anything. Every decision is
made on canonical bytes, enums and the controller's own value comparator
(``boundary.harness.equal``). ``record``, ``disagreement`` and ``cards``
are pure (no I/O); ``probe_runner`` starts sandboxed processes.

Import from the submodules; this package re-exports nothing.

- ``record``: the probe outcome record and its canonical bytes and digest.
- ``disagreement``: stability across the two repeats, dropped probes,
  grouping of candidate outputs, and the relation of each group to the base.
- ``cards``: the partition signature and the question cards built from
  probes that split the candidates the same way, in a deterministic order.
- ``probe_wrapper``: the product program that runs one probe on one tree and
  reports one outcome record (standard library only; it runs in the tree's
  interpreter).
- ``probe_runner``: runs every probe on the base and on every candidate
  tree, twice per tree, each time on a fresh copy through the check
  execution entry point (``boundary.check_env.check_command``).
- ``readings``: the one model call that states readings of the goal, kept
  only when each names a span contained in the goal.
- ``candidates``: the opt-in candidate step (``examples.enabled``, off by
  default): k candidate solutions in isolated worktrees, their probes run,
  the question cards built.

Nothing here is wired into ``auto`` yet.
"""
