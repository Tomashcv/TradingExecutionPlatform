# SP2 Recovery Execution — A6C1B

## Current causal SP2 provider

Status: provider semantics frozen.

Broker POST authorization: false.

Live authorization: false.

## Runtime source

The current SP2 membership provider uses the official holdings
publication for:

`iShares Core S&P 500 ETF (IVV)`

as an operational S&P-500-weight proxy.

The raw source is hashed before use.

The provider itself is pure and performs no HTTP request.

## Observed A6C1A source

The read-only A6C1A probe observed:

- source as-of: 2026-08-13;
- equity holdings: 504;
- rank 1: NVDA, 8.12%;
- rank 2: AAPL, 6.67%;
- rank 3: MSFT, 5.49%;
- Top-2 / Top-3 weight gap: 1.18 percentage points;
- Top-2 set: AAPL / NVDA;
- raw SHA-256:
  `3dba6523e7203616972ac0912b6eca0835163ba71c773710c0af61fc272f44a8`.

Those values are observations, not permanent strategy membership.

## Ranking

The provider ranks equity holdings by:

1. published `Weight (%)`;
2. `Market Value` only as a rounding-tie refinement;
3. ticker only after the economically relevant fields differ.

An exact equality of both weight and market value at the #2/#3
boundary fails closed.

## Dynamic membership

AAPL and NVDA are NOT frozen as permanent SP2 constituents.

Every accepted raw holdings snapshot is independently ranked.

However, Trading 212 execution-symbol mappings are separately
validated operational objects.

A6C1B currently freezes only mappings directly observed/validated in
Demo:

- AAPL -> `AAPL_US_EQ`
- NVDA -> `NVDA_US_EQ`

If a future Top-2 contains another ticker, A6C1B fails closed instead
of guessing a Trading 212 symbol.

A broader mapping provider may be added later without changing the
SP2 research rule.

## BOOTSTRAP_CURRENT_STATE

This mode exists only to initialize/reconcile a new executor before
durable SP2 membership state exists.

Requirements include:

- official source not future-dated;
- source no more than seven calendar days old;
- effective date equals runtime as-of date;
- no historical replay;
- no broker-position-derived membership.

This is an operational initialization mechanism, not a new monthly
research rule.

The caller must later enforce that durable membership is genuinely
uninitialized before using this mode.

## MONTH_END_SIGNAL

All ordinary future membership decisions use this mode.

Requirements include:

- exact expected monthly signal date;
- holdings source as-of must equal that signal date;
- effective date must follow signal date;
- runtime as-of must not precede effective date.

The exact US-session next-trading-day check remains delegated to a
calendar layer.

A6C1B does not guess holidays or exchange sessions.

## TRUE HOLD semantics

Rank order is preserved for provenance.

The strategy's existing TRUE HOLD mechanics remain unchanged:

- same Top-2 set -> no membership trade;
- rank swap within the same set -> no trade;
- set change -> rebalance the new two-name set under the canonical
  membership-change rule.

A6C1B does not retune that rule.

## Separation from broker state

Trading 212 positions may be used to verify physical account state.

They may not choose SP2 membership.

`BROKER_POSITIONS_CAN_DEFINE_SP2 = false`

## Historical research

Frozen historical membership/replay data remains regression evidence,
not current runtime state.

No research history is modified or reoptimized.

## Safety

A6C1B performs no:

- HTTP requests;
- broker GETs;
- broker POSTs;
- order creation;
- operational database creation;
- live execution.

`BROKER_POST_AUTHORIZED = false`

`LIVE_EXECUTION_AUTHORIZED = false`
