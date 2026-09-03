# SP1Execution v0.4 M3 — fill-based capital ledger

## Source of economic truth

Future SP1 capital accounting is driven by confirmed broker fills.

For each canonical Trading212 fill:

- broker order ID is exact;
- fill ID is exact;
- wallet currency must be EUR;
- `walletImpact.netValue` is the cash-impact magnitude;
- BUY => negative strategy cash delta;
- SELL => positive strategy cash delta;
- broker fee/tax rows are retained separately as fee evidence.

## Critical no-double-count rule

Observed Trading212 Demo BUY fills prove that the EUR
`walletImpact.netValue` already includes the FX fee.

Therefore:

`strategy_cash += signed(walletImpact.netValue)`

and NOT:

`strategy_cash += signed(walletImpact.netValue) - fee`

The fee is added to `realized_fees_eur` for reporting/evidence only.

## External cash debt

`external_cash_debt_eur` mirrors the strategy cash deficit:

`max(0, -strategy_cash_eur)`

A SELL therefore repays the bootstrap deficit first naturally.
A real broker BUY overshoot is recorded truthfully even if it creates
a new deficit; later execution layers must prevent such overshoots
before order submission.

The ledger never hides an already-observed broker fill.

## Idempotency

Event key:

`t212:fill:<broker_order_id>:<fill_id>`

Exact replay is a no-op.

A conflicting replay of the same event key fails closed.

## Existing Demo bootstrap

The two historical Demo BUY fills are NOT re-applied to the operational
ledger because M0 already migrated their aggregate wallet impact.

Public portfolio fixture: M3 proves the same read-only consistency using synthetic values:

- AAPL cash delta: -5000.00 EUR
- NVDA cash delta: -5000.00 EUR
- aggregate: -10000.00 EUR
- aggregate fees: 10.00 EUR

In the original private lineage this check was bound to frozen demo evidence; the public fixture preserves only the arithmetic contract.

## Scope

M3 does not submit, cancel, or modify broker orders.

`realized_fx_eur` is not inferred from `fxRate`; an FX rate alone is
not realized FX P&L.
