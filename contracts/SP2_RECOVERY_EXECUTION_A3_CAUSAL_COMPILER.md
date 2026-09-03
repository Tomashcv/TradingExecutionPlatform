# A3 — causal CORE_RETURN event compiler

## Purpose

A3 reconstructs the frozen historical CORE_RETURN execution schedule from
the causal IVV history.

The frozen final schedule is used only as an oracle.

The compiler does not read the final schedule in order to decide events.

## Source state

The inherited `RobustState` is reused as the causal source-cycle compiler.

Its role is limited to:

- causal running old ATH;
- 30/35/45/50% ladder;
- source ladder increases;
- 55% trough-to-old-ATH handoff;
- old-ATH rearm.

The 55% handoff is SOURCE CYCLE SEGMENTATION ONLY.

It does not exit CORE_RETURN.

## CORE_RETURN transformation

For every positive source ladder event:

1. signal is known at close T;
2. source execution anchor is canonical US session T+1;
3. each source event is independently delayed by 40 canonical US trading
   intervals;
4. the first actual delayed entry anchors the 378 interval hold clock;
5. later delayed scale-ups do not reset the clock;
6. entries maturing at or after fixed exit are discarded;
7. fixed exit returns recovery-sleeve target to zero.

## Cancellation

A source cycle is cancelled only when its old ATH is recovered STRICTLY
before its first delayed D40 entry.

A 55% source handoff before D40 does not cancel the CORE_RETURN cycle.

COVID is the historical regression case for this distinction.

## Guard

After fixed exit, no new recovery cycle is permitted until the old ATH is
recovered.

Effective release:

`max(fixed H378 exit, old ATH recovery)`

Historical required releases:

- cycle 1: 2005-12-13
- cycle 2: 2012-08-16
- cycle 3: 2021-11-16

## Exact historical replay gates

A3 must reproduce:

- 10 / 10 source causal events:
  - 7 ladder events
  - 3 source handoffs
- 7 / 7 D40 events exactly 40 canonical intervals
- 3 / 3 H378 exits exactly 378 canonical intervals
- 3 / 3 guard-release dates
- 10 / 10 final C2 schedule rows

No approximate schedule match is accepted.

## Research freeze

This is execution-semantic transcription only.

It performs no:

- threshold retuning;
- delay retuning;
- hold retuning;
- asset reselection;
- subset reselection;
- historical optimization.

## Broker boundary

A3 performs no broker calls and creates no orders.

`LIVE_EXECUTION_AUTHORIZED=0`
