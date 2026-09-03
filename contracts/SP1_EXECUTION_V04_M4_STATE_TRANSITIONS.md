# SP1Execution v0.4 M4 — durable control-state transitions

M4 operationalizes three orthogonal durable dimensions.

## Entry

Allowed one-way paths:

- UNINITIALIZED -> WAIT_CASH
- UNINITIALIZED -> ENTRY_COMPLETE
- WAIT_CASH -> CRASH_BUY
- CRASH_BUY -> HANDOFF_TO_SP2
- HANDOFF_TO_SP2 -> ENTRY_COMPLETE

WAIT_CASH remains research-unapproved unless separately validated.
The state machine supports it mechanically without asserting edge.

## Strategy

Allowed cycle:

- INACTIVE -> NORMAL
- NORMAL -> CRASH
- CRASH -> POST_HANDOFF
- POST_HANDOFF -> NORMAL

Same-state STRATEGY updates are allowed only as durable ROBUST
snapshot/threshold updates, e.g. CRASH 10% -> CRASH 30%.

CRASH requires positive S&P overlay plus old_peak/trough.

POST_HANDOFF requires zero S&P overlay plus old_peak/trough/rearm_old_ath.

NORMAL requires zero S&P overlay.

## Membership

Allowed cycle:

- UNINITIALIZED -> ACTIVE
- ACTIVE -> MONTH_END_PENDING
- MONTH_END_PENDING -> ACTIVE for the same Top2 set
- MONTH_END_PENDING -> REBALANCE_PENDING for a changed Top2 set
- REBALANCE_PENDING -> ACTIVE only after fully reconciled execution

A #1/#2 rank swap with the same two companies is no trade.
The membership month advances but the existing internal SP2 mix is preserved.

A changed Top2 set does NOT update active membership until the
rebalance has completed and reconciled. On commit the new SP2 mix is 50/50.

## Atomicity and idempotency

Every transition:

- runs under BEGIN IMMEDIATE;
- increments machine_state.revision exactly once;
- appends exactly one state_transitions row;
- has a globally unique event_key;
- exact replay is a no-op;
- conflicting replay fails closed.

Control-state transitions require:

- execution_state == IDLE;
- zero ACTIVE execution workflows.

This prevents strategy/membership truth from moving ahead of broker truth.

M4 does not submit, cancel, or modify broker orders.


## Entry-completion invariant

The frozen M0 validator requires `ENTRY_COMPLETE` to have an active strategy.

Therefore the final transition into `ENTRY_COMPLETE` atomically couples:

- `entry_state -> ENTRY_COMPLETE`
- `strategy_state: INACTIVE -> NORMAL`

inside the same SQLite transaction and machine-state revision.

This is not premature strategy activation: `ENTRY_COMPLETE` is only legal
after the corresponding initial allocation or handoff has been fully
reconciled.

The coupled activation is explicitly recorded in the ENTRY transition payload
for auditability.
