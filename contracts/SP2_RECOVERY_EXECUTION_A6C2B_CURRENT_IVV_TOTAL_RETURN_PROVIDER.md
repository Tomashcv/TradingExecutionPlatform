# SP2 Recovery Execution — A6C2B

## Current IVV total-return provider

Status: provider semantics frozen.

Live authorization: false.

Broker POST authorization: false.

## Frozen CORE_RETURN trigger basis

A6C2A3 established numerical identity between the frozen canonical
`ivv_nav` surface and normalized IVV adjusted-close geometry.

The historical trigger therefore remains:

- asset: IVV;
- economic surface: total-return / adjusted-close geometry;
- stored frozen historical surface: normalized `ivv_nav`;
- positive drawdown:
  `1 - current / causal_running_ATH`.

The absolute adjusted-close number itself is not strategy state.

## Current transport

Current transport candidate:

`Yahoo Chart API — IVV — daily adjusted close`

A6C2B does not perform HTTP requests.

Raw Yahoo bytes are caller-supplied to the pure provider.

## Historical compatibility gate

Before any post-2024 continuation may be accepted, the current Yahoo
payload must satisfy all of the following against the frozen canonical
history:

1. all 5,996 frozen sessions from 2001-01-03 through 2024-11-01 are
   present;
2. no extra Yahoo session exists inside that frozen interval;
3. maximum daily return revision is no more than 0.10 basis point;
4. frozen source-cycle signature is reproduced exactly;
5. frozen final D40/H378 schedule is reproduced exactly.

The 0.10 bp value is an input-transport sanity ceiling.

It is NOT:

- a trading threshold;
- a crash threshold;
- a tuning parameter;
- a new research parameter.

## Empirical A6C2B0A evidence

On 2026-08-15 the live read-only compatibility replay observed:

- frozen sessions: 5,996;
- missing frozen sessions: 0;
- extra sessions inside frozen window: 0;
- maximum daily-return difference:
  0.017737003695 bp;
- mean daily-return difference:
  0.002809521816 bp;
- maximum positive-drawdown difference:
  0.000167294722 percentage points;
- source-cycle count:
  3 frozen / 3 live;
- source-cycle signature match:
  true;
- final D40/H378 event count:
  10 frozen / 10 live;
- final schedule signature match:
  true.

Thus current Yahoo adjusted-return geometry reproduced every frozen
CORE_RETURN decision over the historical validation window.

## Frozen anchor

Frozen history remains authoritative through:

`2024-11-01`

The provider retains the frozen IVV NAV on that session.

For every later accepted Yahoo session:

`stitched_value[t] = frozen_anchor_NAV * yahoo_adjclose[t] / yahoo_adjclose[anchor]`

Therefore future Yahoo adjusted-close rescaling cannot replace the
frozen historical level.

Only return geometry after the frozen anchor is imported.

## Completed-session boundary

The provider MUST receive:

`last_completed_us_session`

from a separate causal calendar/runtime layer.

The provider does not infer that today's Yahoo daily bar is complete.

Any raw Yahoo observations after `last_completed_us_session` are
ignored for the stitched causal surface.

The completed session itself must exist in the Yahoo payload.

## Frozen CORE_RETURN rule

No strategy rule changes.

Rule:

`SP2_RECOVERY_CORE_RETURN_D40_H378_V1`

Source ladder:

- positive drawdown >=30% -> target sleeve 10%;
- positive drawdown >=35% -> target sleeve 30%;
- positive drawdown >=45% -> target sleeve 60%;
- positive drawdown >=50% -> target sleeve 100%.

Temporal rule remains frozen:

- source signal at completed close;
- source execution T+1;
- each entry/scale delayed D40;
- first actual delayed entry starts one H378 clock;
- later scale-ups do not reset H378;
- old ATH strictly before first delayed entry cancels cycle;
- old ATH on first-entry session does not cancel;
- H378 fixed exit;
- old-ATH guard semantics unchanged.

## Provider output

The provider returns a causal stitched tuple of:

`RecoveryInputRow(date, close)`

through the explicitly supplied completed US session.

It also returns:

- frozen and post-anchor row counts;
- source hashes;
- stitched-series hash;
- provider-decision hash;
- latest normalized value;
- causal running ATH;
- latest positive drawdown;
- historical transport error diagnostics;
- exact source-cycle and final-schedule compatibility flags.

## What A6C2B does NOT do

A6C2B does not:

- decide the current recovery target;
- infer D40/H378 state from the latest drawdown alone;
- create durable recovery state;
- create an operational database;
- use broker positions;
- perform broker GETs;
- perform broker POSTs;
- create orders;
- authorize live trading.

The current CORE_RETURN target requires a later full stateful replay
through the current completed session.

## Safety constants

`ABSOLUTE_YAHOO_ADJ_CLOSE_LEVEL_AUTHORITY = false`

`TOTAL_RETURN_GEOMETRY_AUTHORITY = true`

`NETWORK_PERFORMED_BY_PROVIDER = false`

`BROKER_POST_AUTHORIZED = false`

`LIVE_EXECUTION_AUTHORIZED = false`
