# SP1Execution v0.4 — M0 durable state contract

## Purpose

M0 introduces durable orthogonal machine state without changing the
v0.3 execution path and without submitting, cancelling, or modifying
broker orders.

The legacy tables `decisions`, `kv`, and `order_attempts` remain
untouched and are preserved during migration.

## State dimensions

### Lifecycle

- `DEMO`
- `LIVE_DISARMED`
- `LIVE_ARMED`

There is no automatic transition from DEMO to LIVE.

### Entry policy

- `UNSET`
- `IMMEDIATE_SP2`
- `WAIT_CASH`

`WAIT_CASH` is a user-selected entry policy and is not treated as
validated ROBUST research.

### Entry state

- `UNINITIALIZED`
- `WAIT_CASH`
- `CRASH_BUY`
- `HANDOFF_TO_SP2`
- `ENTRY_COMPLETE`

### Strategy state

- `INACTIVE`
- `NORMAL`
- `CRASH`
- `POST_HANDOFF`

### Membership state

- `UNINITIALIZED`
- `ACTIVE`
- `MONTH_END_PENDING`
- `REBALANCE_PENDING`

### Execution state

- `IDLE`
- `PLAN_CREATED`
- `SELL_PENDING`
- `BUY_PENDING`
- `RECONCILING`
- `PARTIAL_FILL`
- `RECONCILIATION_REQUIRED`
- `FAILED`

## Durable execution workflow

A future rebalance is represented by an `execution_workflows` row and
ordered `execution_legs`.

The workflow survives process termination and machine reboot.

A mixed SELL+BUY transition must eventually execute as:

1. SELL intent
2. broker acceptance
3. SELL fill reconciliation
4. real cash/proceeds update
5. BUY planning from real available strategy cash
6. BUY intent
7. broker acceptance
8. BUY fill reconciliation
9. final position reconciliation
10. state commit

No BUY phase may bridge from unrelated account cash.

## Capital

`capital_ledger` is append-only.

The migrated Demo state records:

- original SP1 capital basis;
- actual bootstrap broker debit;
- broker FX/conversion fees;
- strategy cash;
- external cash debt caused by bootstrap overshoot.

Future fills must use broker-confirmed wallet impact rather than
estimated order notional.

## Hard invariants

1. `ENTRY_COMPLETE` requires an active two-symbol membership.
2. `IDLE` cannot coexist with an ACTIVE execution workflow.
3. Negative strategy cash must be fully represented by external cash debt.
4. Active overlay must remain within `[0, 1]`.
5. SP2 internal mix must sum to 1.
6. Migration cannot alter row counts of legacy execution tables.
7. M0 performs no broker operation.
8. Unknown or contradictory future broker state must fail closed.
