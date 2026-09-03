# SP1Execution v0.4 M9A - Duplicate-submission concurrency lock

A deterministic M9 fault-injection probe ran two concurrent SQLite
connections against the same workflow and SELL leg. Before this milestone,
both callers reached the broker POST and the fake broker observed two distinct
orders for one strategy intent. Only the later broker order ID remained in the
single durable leg.

M9A serializes `submit_current_phase()` for each file-backed operational
SQLite database with a non-blocking POSIX `flock`. The lock identity is the
SHA-256 of the canonical main SQLite database path and the lock file lives in
the system temporary directory.

The lock is held across durable intent recording, broker POST, and durable
broker-acceptance persistence. A competing submitter fails closed with
`PhaseNotReady` before reaching the broker.

The existing crash-window invariant remains unchanged: if a process dies
after `INTENT_RECORDED` but before a durable broker order ID exists, recovery
requires reconciliation and automatic resubmission remains prohibited.

Regression requirements:

- exactly one fake broker POST for concurrent submitters on one DB;
- losing caller blocked by the submission lock;
- winning leg ends `BROKER_ACCEPTED`;
- recovery is `RECONCILE_SELL`;
- recovery does not permit a resubmission;
- independent SQLite databases do not block each other.

M9A performs no real broker requests or orders and must not mutate the
operational SQLite database.
