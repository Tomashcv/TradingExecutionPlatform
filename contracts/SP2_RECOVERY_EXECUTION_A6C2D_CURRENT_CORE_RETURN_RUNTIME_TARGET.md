# SP2 Recovery Execution — A6C2D

## Current CORE_RETURN runtime target freeze

Status: runtime-target adapter semantics frozen.

Broker POST authorization: false.

Live authorization: false.

## Purpose

A6C2D bridges the already validated current stateful CORE_RETURN replay
into the A6B2B `RuntimeRecoveryTarget` ABI.

It does not create another strategy state machine.

The economic state remains defined by the frozen rule:

`SP2_RECOVERY_CORE_RETURN_D40_H378_V1`

## Upstream evidence

A6C2C semantic replay was completed through the last completed US
session:

`2026-08-14`

Runtime as-of:

`2026-08-15`

Observed current state:

- phase: `NORMAL`;
- current recovery target: `0`;
- cycle id: none;
- first actual entry session: none;
- fixed exit session: none;
- old ATH: none;
- old ATH recovered flag: false;
- open D40 events: 0;
- state revision: 18;
- historical source cycles: 3;
- post-2024 source events: 0;
- historical final events: 10;
- post-2024 final events: 0.

The two independent in-memory replays matched on:

- semantic state;
- pending-event inventory;
- transition inventory.

Only volatile `created_at` / `updated_at` audit metadata differed and is
not part of semantic state.

A6C2C replay decision evidence:

`865129b524cdf5e2bf12df01efec49df3cba6f09bc49b6f01ad64f3315eddf54`

## Frozen ABI

A6C2D emits the already-existing:

`RuntimeRecoveryTarget`

with:

- `asof_date`;
- `effective_date`;
- `target_recovery_weight`;
- `directive`;
- `source_kind`;
- `source_id`;
- `source_sha256`;
- frozen CORE_RETURN rule id.

Current runtime source kind is exactly:

`VALIDATED_RUNTIME_PROVIDER`

## Snapshot directive semantics

A state snapshot is not itself a transition event.

A6C2D therefore maps:

- `RECOVERY_ACTIVE` -> `ACTIVE_TARGET`;
- `NORMAL` -> `NORMAL`;
- `WAIT_D40` -> `NORMAL`;
- `OLD_ATH_GUARD` -> `NORMAL`.

A6C2D does not infer `EXIT_TO_NORMAL` from a state snapshot.

`EXIT_TO_NORMAL` remains a valid A6B2B directive, but it requires
explicit transition evidence from a transition/event provider.

This prevents a zero-weight snapshot from being falsely described as
an exit that happened on that exact session.

## Frozen consistency gates

The adapter fails closed unless replay evidence says all of the
following are true:

- semantic state deterministic;
- pending-event inventory deterministic;
- transition inventory deterministic;
- state-machine consistency passed.

The target must be one of the frozen ladder values:

- 0%;
- 10%;
- 30%;
- 60%;
- 100%.

Phase consistency is also enforced.

Examples:

### NORMAL

Requires:

- target = 0;
- no open D40;
- no active cycle;
- no first-entry session;
- no fixed-exit session;
- no retained old ATH.

### WAIT_D40

Requires:

- target = 0;
- active cycle;
- old ATH;
- at least one open D40 event;
- no first actual entry yet.

### RECOVERY_ACTIVE

Requires:

- positive frozen-ladder target;
- active cycle;
- old ATH;
- first actual entry;
- fixed H378 exit session.

### OLD_ATH_GUARD

Requires:

- target = 0;
- active cycle context;
- old ATH;
- no open D40 event.

## Provenance

The runtime target source id is:

`CORE_RETURN_STATEFUL_REPLAY_RUNTIME_TARGET_V1:<effective-date>:<phase>`

The source SHA-256 is deterministic over the semantic replay evidence,
including:

- runtime/effective dates;
- phase and target;
- cycle-state fields;
- D40 inventory;
- state revision;
- source/final event counts;
- determinism gates;
- market-provider id;
- market-provider decision SHA;
- stitched market-series SHA;
- upstream replay decision SHA;
- frozen CORE_RETURN rule id.

Volatile audit timestamps are excluded.

## Market provider

Expected upstream current market provider:

`YAHOO_CHART_IVV_ADJCLOSE_FULLHISTORY_BRIDGE_V1`

A6C2D itself performs no network access.

## Reserve separation

The `reserve_bucket_eur` observed inside a historical/in-memory recovery
state replay is NOT operational strategy-reserve authority.

A6C2D does not ingest it and cannot use it to size physical capital.

Operational strategy reserve remains governed by the separately frozen
A6B1 capital-scope semantics.

Therefore:

`REPLAY_RESERVE_BUCKET_CAN_DEFINE_STRATEGY_RESERVE = false`

## Current decision

For the validated session ending 2026-08-14:

- phase = `NORMAL`;
- target recovery weight = `0`;
- directive = `NORMAL`.

Thus the current strategy target is:

- recovery sleeve: 0%;
- core SP2: 100%.

This does not itself create or authorize any trade.

## Safety

A6C2D performs no:

- market-data requests;
- persistent database creation;
- broker GET;
- broker POST;
- orders;
- live authorization.

`NETWORK_PERFORMED_BY_PROVIDER = false`

`DATABASE_CREATED_BY_PROVIDER = false`

`BROKER_GET_AUTHORIZED = false`

`BROKER_POST_AUTHORIZED = false`

`LIVE_EXECUTION_AUTHORIZED = false`
