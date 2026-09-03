# SP1Execution v0.4 M1A — Trading212 historical orders

Observed Demo schema:

- `items[].order.id`
- `items[].order.ticker`
- `items[].order.status`
- `items[].order.quantity`
- `items[].order.filledQuantity`
- `items[].fill.id`
- `items[].fill.filledAt`
- `items[].fill.price`
- `items[].fill.quantity`
- `items[].fill.walletImpact`
- `nextPagePath`

The previous flat `items[].id/status/filledQuantity` assumption is invalid.

Safety contract:

1. Unknown status never becomes FILLED.
2. Exact broker order ID, ticker, and intended quantity are validated.
3. Multiple fills for one order are merged.
4. Duplicate identical fills are idempotent.
5. Conflicting snapshots/fills fail closed.
6. Pagination follows only `nextPagePath`.
7. Pagination loops and page-limit exhaustion fail closed.
8. Fee signs remain as returned by the broker.
9. M1A performs no broker calls and creates no orders.
