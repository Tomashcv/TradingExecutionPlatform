# A2.1 — CORE_RETURN drawdown semantic correction

## Finding

A1 encoded drawdown with a negative convention:

`close / ATH - 1`

This contradicted the byte-frozen C2 contract.

The frozen C2 definition is:

`1 - close / causal_running_ath`

Therefore the canonical drawdown is positive:

- 30% decline -> `0.30`
- 35% decline -> `0.35`
- 45% decline -> `0.45`
- 50% decline -> `0.50`

## Nature of correction

This is an execution-adapter bug correction.

It is NOT:

- historical threshold retuning;
- strategy optimization;
- asset reselection;
- delay retuning;
- hold retuning.

The frozen research rule is unchanged.

## New invariant

The Python adapter test loads the frozen C2 JSON directly and requires
`FROZEN_LADDER` to equal the contract's `trigger_ladder`.

This prevents an internally self-consistent test suite from silently using a
different drawdown sign convention again.

## Authorization

No broker calls.
No Demo orders.
No live orders.

`LIVE_EXECUTION_AUTHORIZED=0`
