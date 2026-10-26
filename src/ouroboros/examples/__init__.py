"""Example questions chosen by execution: the deterministic core.

Several candidate solutions, each written under a different reading of a
goal, run the same small probes. Where their outputs differ, the requester
is asked which output is right. This package holds the part of that flow
that decides where outputs differ. It is pure: no I/O, no model call, no
pattern or keyword rule. Every decision is made on canonical bytes, enums
and the controller's own value comparator (``boundary.harness.equal``).

Import from the submodules; this package re-exports nothing.

- ``record``: the probe outcome record and its canonical bytes and digest.
- ``disagreement``: stability across the two repeats, dropped probes,
  grouping of candidate outputs, and the relation of each group to the base.
- ``cards``: the partition signature and the question cards built from
  probes that split the candidates the same way, in a deterministic order.

Nothing here is wired into ``auto`` or any runtime yet.
"""
