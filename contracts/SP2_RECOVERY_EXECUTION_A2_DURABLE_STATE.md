# SP2RecoveryExecution A2 — durable recovery state

## Objective

Add an independent durable state layer for the frozen SP2 Recovery
CORE_RETURN policy without reinterpreting or mutating the inherited
SP1Execution v0.4 control-state schema.

## Additive SQLite tables

### recovery_state_v01

Singleton economic recovery state:

- phase
- cycle_id
- old_ath
- current_target
- first_actual_entry_session
- fixed_exit_session
- old_ath_recovered
- reserve_bucket_eur

### recovery_pending_events_v01

Durable D40 source events:

- source event key
- cycle identity
- source signal session
- source execution session
- maturity session
- frozen target
- lifecycle status

### recovery_transitions_v01

Idempotent recovery-state transition journal.

Replay must match:

- destination phase
- reason
- exact state updates
- payload

Conflicting replay fails closed.

### recovery_reserve_ledger_v01

Idempotent reserve accounting provenance.

The reserve bucket cannot become negative.

## Frozen causal constraints represented

- NORMAL -> WAIT_D40
- WAIT_D40 -> RECOVERY_ACTIVE only after a delayed event is applied
- WAIT_D40 may cancel to NORMAL if old ATH recovers strictly before first entry
- first actual entry freezes the H378 clock
- later scale-ups may only maintain or increase recovery exposure
- later scale-ups cannot reset first entry or H378
- fixed exit moves to OLD_ATH_GUARD
- OLD_ATH_GUARD must explicitly rearm to NORMAL
- no RECOVERY_ACTIVE direct rearm to NORMAL

## Reserve semantics

`reserve_bucket_eur` is an accounting bucket, not a claim that the future
physical reserve instrument is cash.

The frozen research policy is:

`TBILL_CRASH_RESERVE_NET28`

The actual live reserve instrument remains unresolved.

A future physical adapter must distinguish:

- reserve market value
- settled broker cash
- unsettled proceeds
- liquidity available for orders

before reserve-funded broker orders can be authorized.

## Isolation

A2 does not modify:

- machine_state
- state_transitions
- execution_workflows
- execution_legs
- capital_ledger
- Trading212 client
- broker executor
- cycle orchestrator

A2 performs no broker calls and no orders.

`LIVE_EXECUTION_AUTHORIZED=0`
