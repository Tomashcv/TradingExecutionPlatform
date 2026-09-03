# SP2 Recovery Execution — A6B1

## Scoped reserve and dynamic physical-plan integration

Status: Demo planning integration only.

Broker POST authorization: false.

Live authorization: false.

## Strategy cash scope

Trading 212 `cash.availableToTrade` is NOT strategy reserve.

It is only a broker-side feasibility ceiling.

The strategy reserve must be supplied from explicit strategy-scoped
state.

For a brand-new operational state with no proven durable reserve:

`strategy_reserve_eur = 0`

No reserve may be inferred from:

- excess Trading 212 account cash;
- initial strategy capital;
- portfolio appreciation;
- unverified historical dividends;
- unsettled sale proceeds.

## Inherited capital semantics

The inherited executor already separates strategy capital from broker
account value.

Its durable state contains `strategy_cash_eur`.

Its existing execution contract treats Trading 212
`availableToTrade` only as an additional BUY feasibility guard.

A6B1 preserves that separation.

## EUR physical valuation

A physical position is valued using:

`walletImpact.currentValue`

only when:

`walletImpact.currency == EUR`

No manual USD/EUR conversion is introduced.

The private A6A stage observed Demo position values at runtime. Exact account
values are intentionally omitted from the public portfolio release. Those
values were runtime observations, not frozen strategy constants.

## Dynamic SP2 composition

A5C's AAPL/NVDA inventory check is retained only as a diagnostic
helper for the A5C snapshot.

It is NOT the long-term authority for SP2 constituents.

A6B1 requires the caller to supply the CURRENT CAUSAL two-ticker SP2
composition.

This preserves future monthly SP2 membership changes.

No AAPL/NVDA permanent membership assumption is introduced.

## Recovery mapping

The A5C recovery mapping remains frozen:

- research proxy: `SOXX`
- UCITS ISIN: `IE00BMC38736`
- Trading 212 Demo ticker: `SMHm_EQ`
- account/listing currency: EUR

## Physical planning

A6B1 delegates strategy allocation mathematics to the frozen A4
constructor.

For recovery increases:

1. use explicit strategy reserve first;
2. sell SP2 proportionally for any deficit;
3. do not authorize recovery BUY until required SELL legs are filled
   and usable proceeds are confirmed.

For recovery decreases / H378 exits:

1. sell recovery;
2. require explicit CURRENT CAUSAL SP2 return weights;
3. do not use a 50/50 fallback;
4. require recovery SELL fill and proceeds confirmation before SP2 BUY.

## Broker cash

Large unrelated broker cash must never inflate strategy NAV or remove
the SELL-before-BUY requirement.

For example, an account may expose EUR 39,954.21
`availableToTrade` while the strategy itself contains approximately
EUR 10,000.

Only explicit strategy-scoped reserve belongs in strategy NAV.

## Safety

A6B1 performs no:

- HTTP requests;
- broker GETs;
- broker POSTs;
- order creation;
- database creation;
- live execution.

`BROKER_POST_AUTHORIZED = false`

`LIVE_EXECUTION_AUTHORIZED = false`
