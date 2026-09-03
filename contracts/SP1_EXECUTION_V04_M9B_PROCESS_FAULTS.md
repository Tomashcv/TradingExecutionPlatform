# SP1Execution v0.4 M9B - Process-level fault injection

M9B promotes the M9A duplicate-submission fix from an in-process concurrency
regression to real Linux process-level fault injection.

## Scenario 1: competing processes

Two independent Python processes open the same file-backed SQLite workflow.
Process A holds the M9A submission `flock` while blocked inside a fake broker
POST. Process B attempts to submit the same durable SELL leg.

Required result:

- Process B fails on the submission lock before broker POST;
- exactly one fake broker POST exists;
- the winning leg becomes `BROKER_ACCEPTED`;
- recovery is `RECONCILE_SELL`;
- recovery does not permit resubmission.

## Scenario 2: SIGKILL in the ambiguous broker window

Process A durably records `INTENT_RECORDED`, reaches the fake broker POST, and
is killed with SIGKILL before broker acceptance can be persisted.

The operating system releases the `flock` when the process dies. A new
process may therefore enter the submission wrapper, but the pre-existing M5B
durable-intent invariant must stop it before any second broker POST.

Required result:

- exactly one fake broker POST remains;
- automatic retry raises the ambiguous-intent safety error;
- recovery is `MANUAL_RECONCILIATION`;
- `execution_state=RECONCILIATION_REQUIRED`;
- workflow status is `RECONCILIATION_REQUIRED`;
- resubmission is not permitted.

All M9B tests use temporary SQLite databases and fake brokers. No Trading212
request or operational SQLite mutation is authorized.
