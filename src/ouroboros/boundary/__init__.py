"""Check-package boundary: checks frozen from a Seed before any worker starts.

A check package holds, for a Seed's acceptance criteria, per-criterion
oracles (declared inputs and expected outcomes, including held-out cases the
worker never sees) and model-written script checks where no oracle can
express a criterion. It is frozen and admitted on the base checkout before
the worker starts, and after the worker stops it decides the criteria an
admitted check covers; every other criterion is decided by the existing
verifier.

Import from the submodules; this package re-exports nothing.

- Model and journal: ``package`` (the package and its Seed linkage),
  ``oracle`` (oracle data), ``harness`` (the product-owned harness frozen into
  every oracle package), ``binding`` (late bindings and check tiers),
  ``oracle_build`` (building oracle specs and packages from data), ``tree``
  (byte manifests of checkouts), ``receipts`` (admission and verification
  receipts), ``events`` and ``ledger`` (the journal and its ordering rules).
"""
