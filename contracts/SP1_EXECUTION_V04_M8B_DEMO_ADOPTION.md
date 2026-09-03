# SP1Execution v0.4 M8B - Demo adoption

M8B adopts the already-existing Trading212 Demo sleeve into the durable M7
cycle state machine without creating a trade.

## Preconditions

- lifecycle: DEMO
- entry: ENTRY_COMPLETE
- strategy: NORMAL
- membership: ACTIVE, 2026-07, AAPL/NVDA
- execution: IDLE
- durable workflows: 0
- durable legs: 0
- capital basis: EUR 10,000.00
- strategy cash: EUR 0.00
- external cash debt: EUR 0.00
- realized fees: EUR 10.00
- broker positions:
  - AAPL_US_EQ 50.0
  - NVDA_US_EQ 25.0
- broker pending orders: empty

## Exact rehearsal gate

Immediately before the operational mutation, the final-adoption script copies
the operational SQLite DB and runs the real `sp1exec cycle` against the copy.

The adoption is authorized only if that copy returns exactly:

- ACTION=NO_TRADE_CONTROL_COMMITTED
- DECISION_ID=m7:2026-08-13:2026-07:70e9c484a88155b7
- zero workflows
- zero execution legs
- unchanged capital ledger
- execution state IDLE

Any changed causal decision fails closed and requires a fresh M8B rehearsal.

## Operational adoption

The same `sp1exec cycle` command is then run against the operational v0.4 DB,
without `--confirm-demo`.

The allowed mutation is one local STRATEGY control transition NORMAL -> NORMAL
that persists the causal ROBUST state snapshot. No broker POST is authorized.

## Safety evidence

The committed JSON evidence pack records:

- operational DB hashes before and after;
- a SHA-256-pinned SQLite backup taken before mutation;
- read-only Demo position and pending-order reconciliation;
- exact rehearsal action and decision ID;
- machine state before and after;
- the single appended control transition;
- zero workflows and zero legs;
- unchanged capital ledger;
- zero broker POST authorization and zero created broker orders.

This milestone adopts execution state only. It does not change the frozen
strategy, membership, crash thresholds, capital basis, or existing positions.
