# SP1Execution v0.4 M8A - Trading212 position normalization

M8A fixes a production schema mismatch discovered during the current Demo
migration/reconciliation probe.

## Real Trading212 position schema

The current Trading212 Demo `/equity/portfolio`-style payload places the
instrument ticker at:

`position["instrument"]["ticker"]`

The legacy planner expected:

`position["ticker"]`

That caused `position_quantities()` to return an empty mapping for the real
AAPL/NVDA Demo sleeve.

## Canonical normalization

`position_quantities()` accepts both:

- legacy flat `ticker`;
- current nested `instrument.ticker`.

If both are present they must agree. Missing tickers, duplicate tickers, or
invalid quantities fail closed instead of silently being treated as zero.

All current consumers share this helper, including:

- planner valuation and `make_orders`;
- M5B source-position capture;
- M5B SELL/BUY reconciliation;
- M5B post-SELL BUY replanning;
- M7 market snapshots;
- legacy CLI portfolio valuation.

## Real Demo reconciliation evidence

The frozen bootstrap quantities are:

- AAPL_US_EQ: 50.0
- NVDA_US_EQ: 25.0

The Trading212 Demo read-only evidence captured on 2026-08-14 matched these
quantities exactly.

The broker position `walletImpact.totalCost` values summed to EUR 10,030.74.
Adding the frozen EUR 10.00 FX fees reproduces the frozen bootstrap broker
debit of EUR 10,00.00.

## Safety

M8A does not place orders. The freeze script runs unit/full-suite QA first,
then performs only read-only Demo GET verification of positions and pending
orders. The operational SQLite database must remain byte-for-byte unchanged.
