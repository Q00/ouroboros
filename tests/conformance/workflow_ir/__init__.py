# Workflow IR conformance harness — Wave 1 N-2 (parent #956, umbrella #1131).
#
# This package holds deterministic offline fixtures that lock down the
# Workflow IR v1 boundary:
#
#   * Negative graph-shape fixtures (#956 validator MUST reject):
#       - dangling edge
#       - duplicate node id
#       - unreachable terminal
#       - missing schema ref (evidence/input)
#       - illegal transition (self-loop)
#
#   * Positive runtime-shape fixtures (#956 lifecycle MUST accept):
#       - legal node-state transitions
#       - terminal-state-emitted-once
#       - blocked / failed / cancelled / timed_out distinction
#
#   * Plugin firewall contract fixture (#939 boundary, read-only here):
#       - blocked permission cannot present as success-node completion
#
# No network. No model providers. No live runner. No plugin dispatch.
