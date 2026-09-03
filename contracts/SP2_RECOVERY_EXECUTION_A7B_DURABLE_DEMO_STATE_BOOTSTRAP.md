# SP2 Recovery Execution — A7B

## Durable Demo state bootstrap

Status: first operational Demo state bootstrap authorized.

This bootstrap consumes the frozen A6E snapshot solely as
bootstrap evidence.

It does not turn broker holdings into strategy authority.

## Strategy authority

Current SP2 membership authority remains:

`ISHARES_IVV_OFFICIAL_HOLDINGS_PROXY_V1`

Frozen bootstrap membership:

- AAPL
- NVDA

Execution mapping:

- AAPL -> AAPL_US_EQ
- NVDA -> NVDA_US_EQ

Current CORE_RETURN state:

- phase: NORMAL
- target: 0%
- reserve: EUR 0

## Capital scope

Strategy capital basis:

`EUR 10,000`

Current marked strategy NAV at the bootstrap snapshot:

`EUR 10,013.13`

Broker-wide available cash is not strategy capital and is not strategy
reserve.

Initial durable:

- strategy_cash_eur = 0
- external_cash_debt_eur = 0
- recovery reserve = 0

## TRUE HOLD bootstrap

The bootstrap must not rebalance the two existing SP2 positions.

The private lineage initialized the durable SP2 mix from the already-existing
Demo positions so that bootstrap did not manufacture a rebalance. Exact account
values are intentionally omitted from the public portfolio release.

Membership is still provider-defined. Broker values define only the
initial physical mix required to avoid manufacturing a bootstrap trade.

## Machine state

The first durable machine state is:

- lifecycle_state = DEMO
- entry_policy = IMMEDIATE_SP2
- entry_state = ENTRY_COMPLETE
- strategy_state = NORMAL
- membership_state = ACTIVE
- execution_state = IDLE
- active_membership_month = 2026-08
- active_overlay = 0

No pending membership change exists.

## Recovery state

The recovery durable singleton starts:

- phase = NORMAL
- current_target = 0
- reserve_bucket_eur = 0

No historical recovery transition rows are fabricated during bootstrap.

Historical replay remains upstream evidence, not operational event
history.

## Bootstrap provenance

The operational DB contains a dedicated bootstrap provenance record
linking it to:

- A6E record SHA-256;
- A6D snapshot decision SHA-256;
- current SP2 provider decision SHA-256;
- current CORE_RETURN replay decision SHA-256;
- runtime as-of date;
- last completed US session.

## Zero-trade invariant

State initialization itself must create:

- zero decisions;
- zero execution workflows;
- zero execution legs;
- zero order attempts;
- zero broker orders.

No broker POST is authorized.

## Safety

Demo only.

`LIVE_EXECUTION_AUTHORIZED = false`
