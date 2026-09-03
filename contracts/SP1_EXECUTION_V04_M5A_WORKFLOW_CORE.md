# SP1Execution v0.4 M5A — durable two-phase workflow core

M5A implements the persistent execution workflow without broker calls.

Mixed rebalances follow:

`IDLE -> PLAN_CREATED -> SELL_PENDING -> RECONCILING -> BUY_PENDING -> IDLE`

Core invariants:

- SELL legs always precede BUY legs.
- Every broker POST must be preceded by durable `INTENT_RECORDED`.
- Broker acceptance records the exact broker order ID.
- An intent without a durable broker order ID is ambiguous and forces `RECONCILIATION_REQUIRED`; automatic retry is forbidden.
- PARTIAL SELL blocks BUY.
- UNKNOWN broker state fails closed.
- BUY legs may be installed only after every SELL is FILLED.
- BUY installation requires external cash debt to be zero.
- Planned BUY notional may not exceed realized strategy cash.
- Execution legs store positive quantity magnitude; broker submission signs SELL negative and BUY positive.
- The workflow becomes COMPLETE before machine execution returns to IDLE.

M5A does not call Trading212 and does not modify the operational SQLite database.

M5B will integrate nested Trading212 reconciliation, M3 fill-ledger application, fresh post-SELL BUY replanning, and the CLI/cycle surface.

## M5A hardening before freeze

Before freeze, the workflow core is strengthened so that:

- BUY capital authority comes only from durable `machine_state` values.
- Caller-supplied strategy cash/debt inputs are removed from BUY installation.
- Every BUY leg requires explicit EUR notional evidence.
- Reconciliation cannot promote an unsubmitted leg: each reconciled leg requires
  a durable broker order ID and a prior accepted/pending/partial/filled state.
- Ambiguous-intent handling requires a real ACTIVE workflow.
- Execution transition replay is checked before same-state early return, so a
  reused event key with conflicting semantics fails closed.

These invariants are regression-tested before the M5A freeze tag is created.
