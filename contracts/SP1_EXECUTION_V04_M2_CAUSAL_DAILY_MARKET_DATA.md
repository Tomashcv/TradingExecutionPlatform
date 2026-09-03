# SP1Execution v0.4 M2 — causal daily market-data contract

## Problem eliminated

The v0.3 signal path requested:

`range=max&interval=1d`

Yahoo was observed to return:

`dataGranularity=1mo`

That monthly response was then interpreted as daily data.

## v0.4 contract

Daily strategy history MUST use explicit:

- `period1`
- `period2`
- `interval=1d`
- `includePrePost=false`

The response MUST satisfy:

- `meta.dataGranularity == "1d"`
- `meta.exchangeTimezoneName == "America/New_York"`
- strictly increasing timestamps
- unique timestamps
- unique NY session dates
- no weekend session dates
- positive closes
- sufficient history

The current NY session is excluded before 16:15 America/New_York.

The legacy `range_="max"` function argument may be accepted for
backwards compatibility, but it is never forwarded to Yahoo for
daily strategy history.

Any schema/granularity/session contradiction fails closed.

M2 performs no broker calls and creates no orders.
