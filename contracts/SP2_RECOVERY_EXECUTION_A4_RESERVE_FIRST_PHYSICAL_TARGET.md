# SP2 Recovery Execution — A4

## Reserve-first physical target constructor

Status: execution-adapter research / Demo preparation only.

Live execution authorization: **false**.

Broker POST authorization: **false**.

## Frozen predecessor

A3B commit:

`6caf9f190179b19079db077ce5d8261d1bb78fb8`

A4 does not modify:

- frozen CORE_RETURN rule;
- D40;
- H378;
- crash ladder;
- causal compiler;
- durable dispatcher;
- historical research selection.

## Recovery instrument identity

Historical research proxy:

`SOXX`

Physical UCITS instrument identity:

`IE00BMC38736`

No Trading 212 broker symbol/listing is frozen in A4.

Listing selection belongs to a later instrument-adapter validation.

## NAV definition

For A4 planning:

`strategy NAV = physical SP2 value + recovery value + usable reserve`

`reserve_available_eur` means capital already represented to A4
as usable/settled reserve funding.

A4 does not infer broker settlement status.

## Recovery increase / scale-up

For a target recovery weight:

1. calculate target recovery notional from total strategy NAV;
2. subtract already-held recovery value;
3. use available reserve first;
4. fund any remaining deficit by proportional sales of the
   current physical SP2 holdings;
5. do not assume sale proceeds are immediately reusable.

If an SP2 sale is required:

- SELL-before-BUY is required;
- every required SELL must be FILLED before BUY authorization;
- usable proceeds/settlement must be confirmed by the later
  execution layer;
- A4 itself never authorizes a broker POST.

## Recovery reduction / H378 exit

Recovery sale proceeds return to the **current causal SP2 composition**.

A4 has no 50/50 fallback.

The current causal SP2 weights must be supplied explicitly by
the caller for any recovery-to-SP2 rotation.

This remains true if:

- SP2 was previously fully sold at a 100% recovery target;
- SP2 membership changed while recovery was active;
- old physical weights no longer represent the causal core.

Untouched reserve remains reserve.

## Costs, fills and settlement

A4 outputs nominal pre-execution notionals.

It does not pretend that:

- sale notional equals settled proceeds;
- execution costs are zero;
- fills are instantaneous;
- partial fills do not occur.

The later execution workflow must cap BUYs to actually confirmed
usable cash after fills, costs and broker settlement semantics.

## Safety

A4 performs:

- no network I/O;
- no broker calls;
- no broker POST;
- no operational DB creation;
- no live authorization.

`LIVE_EXECUTION_AUTHORIZED = false`
