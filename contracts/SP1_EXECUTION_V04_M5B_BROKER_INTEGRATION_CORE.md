# SP1Execution v0.4 M5B — broker integration core

M5B connects the frozen M5A workflow to the existing Trading212 Demo client,
M1 nested-history reconciliation, the M3 capital ledger, and the frozen planner.

## Scope

M5B is an integration core. The implementation script and tests perform no real
broker calls and do not write to the operational SQLite database.

CLI/cycle wiring and restart recovery are separate follow-up milestones.

## Submission

Only `settings.t212_env == "demo"` is accepted.

Each broker POST is preceded by a durable M5A `INTENT_RECORDED` write.

A broker response must contain a durable order ID before the leg becomes
`BROKER_ACCEPTED`.

If POST outcome is ambiguous, the workflow moves to
`RECONCILIATION_REQUIRED`; automatic retry is prohibited.

Unrelated pending broker orders block new submission in order to preserve
strategy isolation inside the shared Demo account.

## Reconciliation and capital

Accepted legs are reconciled through the frozen nested Trading212 history
parser/reconciler.

Canonical historical fills are passed to M3 `apply_order_fills` before workflow
phase advancement. Partial confirmed fills therefore update strategy cash and
fees while BUY remains blocked.

M3 fill event keys provide repeated-reconciliation idempotence.

## SELL before BUY

Initial fresh plans create only SELL legs. Cached BUY legs from the initial plan
are not executed.

After every SELL leg is FILLED and broker positions match the expected post-SELL
position vector, the workflow enters `RECONCILING`.

BUY legs are rebuilt from a fresh market/position snapshot. The planner NAV is:

`fresh marked SP1 positions + durable strategy_cash_eur`

No broker-account available-cash value is used for strategy BUY authority.

If the fresh snapshot still requires a SELL, BUY is blocked fail-closed.

M5A then enforces that BUY estimated notional cannot exceed durable realized
strategy cash and that external strategy debt is zero.

## Position reconciliation

When every phase order reports FILLED, broker positions must match the position
vector implied by:

`source broker positions + signed workflow quantities`

within the fractional-share tolerance before phase completion.

A mismatch prevents transition to the next phase/IDLE and creates no new order.

## Quantity sign

M5A legs hold positive quantity magnitudes.

Trading212 receives:

- SELL: negative quantity
- BUY: positive quantity

with the frozen four-decimal planner precision.


## M5B hardening before freeze

Before freeze, M5B adds two safety properties required by the shared-account
capital model.

### Sequential BUY execution

SELL legs may be submitted together, because they release strategy capital.

BUY legs are different: only one BUY leg may be in flight at a time. The next
BUY is not submitted until the previous BUY is reconciled to FILLED and its real
Trading212 wallet impact has been applied to the M3 capital ledger.

Before every BUY POST, M5B re-reads durable `machine_state` and requires:

- `external_cash_debt_eur <= 0.01`;
- non-negative durable strategy cash within the cent tolerance;
- the next leg's persisted estimated EUR notional to fit inside current durable
  strategy cash.

This prevents a second BUY from being bridged by unrelated broker-account cash
when the first BUY filled above its estimate.

If a real BUY fill itself creates external strategy debt despite the planning
buffer, the fill remains truthfully recorded by M3 but the workflow is moved to
`RECONCILIATION_REQUIRED` and no further order may be sent automatically.

### Explicit strategy instrument scope

Every M5B decision must carry `strategy_broker_tickers`.

The workflow snapshots only positions in that explicit ticker set. Unrelated
positions in the shared Trading212 account are not treated as SP1 capital and do
not enter SP1 position reconciliation or NAV.

This does not solve same-instrument sharing: until virtual per-strategy lots are
implemented, another strategy must not independently own/trade an SP1 ticker in
the same broker account.

### Progressive BUY position reconciliation

During sequential BUY execution, expected broker positions are computed from:

`scoped source positions + all filled SELL quantities + only FILLED BUY quantities`

PLANNED future BUY legs are never included in the expected position vector.

A position mismatch still creates zero new order. Repeated history reconciliation
remains cash-idempotent through the frozen M3 fill event keys.
