# SP1Execution v0.4 M6 — crash/reboot recovery core

M6 classifies the exact next safe action from durable SQLite state.

It does not predict markets and does not submit broker orders itself.

## Safety property

After any process crash or restart, recovery may produce only:

- a local deterministic transition;
- a broker reconciliation/read step;
- a broker submission only when no ambiguous prior POST exists;
- or a fail-closed manual reconciliation state.

It must never infer that an unknown POST failed and automatically retry it.

## Recovery actions

- `NO_WORKFLOW`
- `START_WORKFLOW`
- `SUBMIT_SELL`
- `RECONCILE_SELL`
- `REPLAN_BUYS`
- `SUBMIT_BUY`
- `RECONCILE_BUY`
- `MANUAL_RECONCILIATION`
- `FAILED`
- `COMPLETE`

Only `SUBMIT_SELL` and `SUBMIT_BUY` are order-submission-capable actions.

`RECONCILE_SELL` and `RECONCILE_BUY` require broker reads but never broker POST.

## Ambiguous POST window

A durable `INTENT_RECORDED` leg without a durable broker order ID is ambiguous.

On recovery it is persisted as:

- workflow `RECONCILIATION_REQUIRED`;
- machine execution state `RECONCILIATION_REQUIRED`.

Automatic resubmission is forbidden.

This covers both:

- crash after intent before POST;
- crash/network loss after POST before broker acceptance persistence.

The conservative treatment is intentional because those states are
indistinguishable after restart.

## Known broker order IDs

Once a broker order ID is durable, recovery never submits that leg again.

Pending, partial, stale/old, or otherwise unfinished known orders are routed
to reconciliation.

Age alone never turns a known order into a retry.

## SELL -> BUY boundary

After SELL reconciliation reaches the durable `RECONCILE/RECONCILING`
boundary, recovery returns `REPLAN_BUYS`.

This ensures the M5B planner rebuilds BUYs from current positions and the
M3 fill-based strategy cash ledger.

## BUY cash boundary

A PLANNED BUY is recoverable as `SUBMIT_BUY` only if durable
`machine_state` has:

- no external cash debt;
- non-negative strategy cash.

The M5B submit path still performs its own per-BUY estimated-notional hard
cash guard before POST.

## Fault-injection coverage

Tests cover restart at:

- workflow created before start;
- SELL phase before POST;
- after durable SELL intent;
- after POST before broker ID persistence;
- after broker acceptance;
- stale/timeout known order;
- partial SELL;
- confirmed SELL cash applied before workflow transition;
- after SELL reconciliation before BUY replan;
- after BUY plan before POST;
- after durable BUY intent;
- after BUY broker acceptance;
- partial BUY;
- confirmed BUY cash applied before final workflow transition;
- completed workflow;
- existing reconciliation-required state;
- multiple recoverable workflows;
- non-IDLE state without workflow;
- planned BUY with durable external debt.

## Scope

M6 tests use only local SQLite and deterministic fault-state construction.

No real Trading212 calls.
No operational database writes.
No new Demo orders.


## M6 hardening before freeze

Before freeze:

- an explicit `workflow_id` cannot bypass the global sole-recoverable-workflow
  invariant;
- a crash during a multi-SELL submission batch may resume only the still
  `PLANNED` SELL work while preserving known broker order IDs;
- sequential BUY recovery is regression-tested so a confirmed prior BUY plus
  a later `PLANNED` BUY resolves to the next guarded BUY submission.

These tests preserve the rule that known or ambiguous broker work is never
blindly duplicated.
