# SP2 Recovery Execution — A6B2B

## Causal runtime input contract

Status: provider boundary only.

Broker POST authorization: false.

Live authorization: false.

## Why this stage exists

The frozen research repository contains authoritative historical SP2
membership and recovery research.

It does not by itself provide a validated current 2026 SP2 membership
feed.

Historical files such as:

- `sp2_true_hold_2001_2024.csv`;
- `topn_frontier_v042_membership_changes.csv`;
- v0.49 historical event schedules;

remain regression/replay authorities only.

They must not silently become current runtime inputs.

## SP2 runtime authority

A current physical plan requires an explicit
`RuntimeSP2Composition`.

It contains:

- causal `asof_date`;
- signal date;
- effective date;
- exactly two distinct ranked tickers;
- immutable provider/source identifier;
- SHA-256 of the provider snapshot;
- source classification.

For a CURRENT runtime plan the source classification must be:

`VALIDATED_RUNTIME_PROVIDER`

`FROZEN_RESEARCH_REPLAY` is permitted as a typed replay object but is
explicitly rejected by `CausalRuntimeInputs`.

Broker positions are not an SP2 membership provider.

## Recovery runtime authority

A current recovery target requires an explicit
`RuntimeRecoveryTarget`.

Its rule id is fixed to:

`SP2_RECOVERY_CORE_RETURN_D40_H378_V1`

Only the frozen recovery ladder states are accepted:

- 0%
- 10%
- 30%
- 60%
- 100%

No 35%, 45%, timing modification, or new threshold is introduced.

The accepted runtime directives are:

- `NORMAL`
- `ACTIVE_TARGET`
- `EXIT_TO_NORMAL`

A current runtime target must also come from a
`VALIDATED_RUNTIME_PROVIDER`.

Historical replay schedules cannot be passed off as current state.

## Causality

No effective date may be after `asof_date`.

For SP2 membership:

`signal_date <= effective_date <= asof_date`

No future data is permitted.

## Historical research remains frozen

A6B2B does not:

- alter SP2 TRUE HOLD;
- alter CORE_RETURN D40/H378;
- retune thresholds;
- search new historical variants;
- replace frozen datasets;
- modify v0.49 research.

## Broker separation

Trading 212 positions may verify whether the physical account matches
the required strategy state.

They may not decide what the strategy state should be.

Therefore:

`BROKER_POSITIONS_CAN_DEFINE_SP2 = false`

## Next provider work

Following this contract, two runtime providers must be implemented and
validated separately:

1. current causal SP2 monthly membership provider;
2. current causal CORE_RETURN recovery-state provider.

Only after both satisfy this contract may the complete Demo physical
snapshot planner consume them.

## Safety

A6B2B performs no:

- market-data requests;
- broker GETs;
- broker POSTs;
- orders;
- operational database creation;
- live authorization.

`BROKER_POST_AUTHORIZED = false`

`LIVE_EXECUTION_AUTHORIZED = false`
