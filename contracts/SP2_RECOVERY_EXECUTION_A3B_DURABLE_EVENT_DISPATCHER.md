# A3B — durable CORE_RETURN event dispatcher

## Scope

A3B connects the frozen causal A3 compiler to the additive A2 SQLite
recovery state.

It still has no broker execution responsibility.

## Durable D40 lifecycle

Every causal ENTRY_OR_SCALE source event is persisted as:

`PENDING -> MATURED -> APPLIED`

The source signal and source T+1 session are durable provenance.

The D40 maturity session is derived only from the canonical US session
calendar.

## Restart / crash recovery

The dispatcher intentionally uses separate durable commits around:

1. MATURED status;
2. economic recovery-state transition;
3. APPLIED status.

If the process dies after step 2 but before step 3, the unique transition
journal is replayed idempotently and the durable event is completed without
creating a second economic transition.

## First entry / hold clock

The first applied D40 entry stores:

- first_actual_entry_session;
- fixed_exit_session.

The H378 clock is never reset by a later scale-up.

Any pending delayed scale whose maturity is at or after fixed exit is
cancelled, matching frozen C2 schedule semantics.

## Source 55% handoff

The inherited 55% recovery handoff remains source-cycle segmentation only.

It creates no durable CORE_RETURN exit.

## Old ATH cancellation

A cycle in WAIT_D40 is cancelled if old ATH is recovered before the first
D40 execution.

Execution-session actions are processed before that session's completed
close observation.

Therefore old ATH recovery on the same session as the first D40 execution
does not cancel the cycle.

This implements the frozen requirement:

`strictly before first delayed entry`

## Guard

At H378:

`RECOVERY_ACTIVE -> OLD_ATH_GUARD`

If old ATH had already recovered during H378, rearm may occur immediately
after the fixed exit.

Otherwise the dispatcher remains in OLD_ATH_GUARD until the old ATH close
is observed.

## Missed execution sessions

A3B deliberately fails closed if:

- a D40 maturity session is skipped;
- the H378 fixed exit session is skipped.

It does not silently execute late.

Operational settlement/fill lag policy is not frozen here and must be
studied separately before broker integration.

## Physical execution boundary

`APPLIED` in A3B means the strategy-state event was durably applied.

It does NOT yet mean:

- a Trading212 order was sent;
- an order was filled;
- sale proceeds settled;
- recovery UCITS exposure physically changed.

That mapping belongs to later physical execution phases.

## Authorization

No broker calls.
No Demo orders.
No live orders.

`LIVE_EXECUTION_AUTHORIZED=0`
