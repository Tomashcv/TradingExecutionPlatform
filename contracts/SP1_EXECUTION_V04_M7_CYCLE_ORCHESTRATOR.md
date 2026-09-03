# SP1Execution v0.4 M7 - durable `sp1exec cycle`

M7 wires frozen M0-M6 components into one restart-safe orchestration command.

## Authority

The v0.4 `machine_state`, `state_transitions`, `execution_workflows`,
`execution_legs`, and M3 `capital_ledger` are authoritative. The legacy
`Journal` KV state is not read by the M7 decision path.

## Database path

`sp1exec cycle` resolves the database in this order: explicit `--db-path`,
`SP1_STATE_DB`, then the repository-anchored `state/sp1execution.sqlite`.
It does not depend on the shell working directory.

## Ordering and restart safety

A cycle first finalizes any COMPLETE M7 workflow whose control target is not
yet promoted. It then invokes M6 recovery. New market work is considered only
when no recoverable execution exists.

A fresh trade cycle creates and starts a durable workflow but performs no
broker POST in that same invocation. Submission is a later cycle.

## Demo submission gate

M7 is DEMO-only. A submission-capable recovery action requires
`--confirm-demo`. Without it the workflow stays durable and no POST occurs.

M5B remains responsible for intent-before-POST, local idempotence, sequential
BUYs, and ambiguous POST fail-closed behavior.

## Session and freshness

Fresh trade planning, post-SELL BUY replanning, and POST submission require
the regular US session 09:35-15:55 New York. Planning/replanning quote age
must be at most 300 seconds.

Before POST, the M7 guard verifies that the workflow signal still equals the
latest completed IVV signal. A stale signal causes zero POST.

Trading212 `availableToTrade` is only an additional BUY feasibility guard.
Durable M3 strategy cash remains capital authority.

## Membership

A newer frozen month is passed through frozen M4 transitions. Same Top2 set
advances the month without trading. Changed Top2 set becomes
`REBALANCE_PENDING`; the previous active membership remains authoritative
until the execution workflow is COMPLETE and then
`commit_membership_rebalance` promotes the new set.

## ROBUST state

ROBUST uses completed IVV daily closes only. For trade events the target
strategy state is stored in the workflow and promoted only after fills are
fully reconciled.

For `POST_HANDOFF`, `rearm_old_ath` is deterministically the frozen
`old_peak`; otherwise it is `None`.

The M7 strategy transition payload repeats all control updates, including
SP2 mix, so event-key replay cannot silently accept a different target.

## SP2 TRUE HOLD drift

Overlay-only rotations preserve the current/pre-liquidation internal SP2
drift mix. That mix is stored in the decision so a later handoff from 100%
S&P can restore the preserved proportions even when current SP2 holdings are
zero. A monthly Top2 set change resets the mix to 50/50.

## Development gate

M7 QA uses only in-memory SQLite and fake broker/market dependencies. The
implementation script performs no real Trading212 calls, creates no Demo
orders, and must leave the operational database byte-for-byte unchanged.
It runs targeted tests, the full suite, Ruff, compileall, and `git diff --check`.

The implementation script does not commit or tag M7. Freeze is a separate
step after review of a green QA result.


## M7 hardening before freeze

Two execution-sensitive cases are explicitly frozen:

- `POST_HANDOFF -> NORMAL` rearm is a local control-state transition when the
  overlay remains 0%. It must not wait for market hours and must not create an
  empty trade plan.
- Broker submission compares the complete causal ROBUST overlay snapshot, not
  only the `as_of` date. A same-date data revision or changed mode, target,
  peak, or trough is treated as stale and causes zero POST.

The exact-match case is regression-tested to ensure the stronger signal guard
does not block a valid durable submission.
