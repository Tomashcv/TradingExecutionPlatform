# SP2 Recovery Execution — A5C

## Trading 212 Demo instrument and portfolio adapter

Status: Demo integration preparation only.

Live authorization: false.

Broker POST authorization: false.

## Proven Demo discovery

The Trading 212 Demo account returned:

- primary account currency: `EUR`;
- exact recovery ISIN: `IE00BMC38736`;
- three accessible listings for that ISIN;
- exactly one EUR listing:
  - ticker `SMHm_EQ`;
  - type `ETF`;
  - name `VanEck Semiconductor (Acc)`.

Therefore the execution mapping frozen by A5C is:

- research proxy: `SOXX`;
- physical UCITS ISIN: `IE00BMC38736`;
- Trading 212 Demo ticker: `SMHm_EQ`;
- execution currency: `EUR`.

The GBP and USD listings are not selected.

## Demo SP2 inventory observed

The read-only Demo snapshot contained:

- `AAPL_US_EQ`;
- `NVDA_US_EQ`.

For both positions:

- total quantity equaled `quantityAvailableForTrading`;
- `quantityInPies` was zero.

A5C does not freeze quantities as strategy constants.
They are runtime state only.

## Cash

A5C uses only:

`cash.availableToTrade`

as the broker-reported currently usable cash snapshot.

A5C does NOT infer that proceeds from a new sale become instantly
usable.

Unsettled-proceeds reuse remains unproven and unauthorized.

## Fail-closed portfolio scope

The adapter permits only:

- current SP2 holdings;
- the frozen recovery ticker.

Unexpected unrelated positions cause the strategy-inventory adapter
to fail closed.

This is a scoped execution account assumption.

## Position tradability

A strategy position is regarded as fully available for the executor
only when:

- `quantityAvailableForTrading == quantity`;
- `quantityInPies == 0`.

Partial availability or Pie-locked quantity fails closed.

## Safety

A5C performs no:

- HTTP calls;
- broker GETs;
- broker POSTs;
- order creation;
- operational DB creation;
- live authorization.

`LIVE_EXECUTION_AUTHORIZED = false`

`BROKER_POST_AUTHORIZED = false`
