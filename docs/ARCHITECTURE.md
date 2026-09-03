# Architecture

## Control flow

The execution path is intentionally decomposed so that broker interaction is not allowed to silently become the system of record.

1. **Causal inputs** are admitted through market-data, membership and recovery providers.
2. **Strategy/planning** produces a deterministic target or no-op decision.
3. **Durable state** records decision identity and the machine transition.
4. **Submission guards** verify environment, authorization and duplicate state.
5. **Broker execution** is serialized around the non-idempotent submission window.
6. **Reconciliation** compares broker positions, pending orders and local state.
7. **Recovery** makes replay/crash handling explicit and idempotent.

## Main packages

- `broker/` — Trading 212 adapter, instrument mapping and history reads.
- `engine/` — capital, membership, planner and reconciliation logic.
- `execution/` — guarded broker executor, durable cycle workflow and recovery.
- `state/` — SQLite journal and versioned machine-state transitions.
- `recovery/` — delayed-event compiler/dispatcher, reserve state and physical target construction.
- `market_data/` — paper/demo providers used by the frozen execution path.

## Failure philosophy

The system is fail-closed. An unknown broker outcome is not treated as permission to retry an order. A conflicting durable replay is an error. A missing causal input blocks a state transition rather than being filled with future information or an inferred value.
