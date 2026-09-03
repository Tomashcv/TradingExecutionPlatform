# SP1Execution v0.4 M10B - Automated Demo forward runner

M10B introduces the automation wrapper and systemd user units for the
Trading212 Demo forward test. The timer is deliberately installed but left
disabled; activation is a separate M10C step.

## Safety model

The runner is Demo-only and fails closed unless:

- `SP1_T212_ENV=demo`;
- `SP1_LIVE_TRADING=false`.

The systemd service passes `--confirm-demo`, which authorizes only the
pre-existing M7/M9 Demo submission path. Live trading remains unapproved.

The runner adds a whole-cycle non-blocking POSIX `flock`, keyed by the
canonical operational SQLite path. This is broader than the M9A
submission-window lock and prevents overlapping automated cycle invocations.

The runner executes only during 09:35-15:55 America/New_York on weekdays.
Outside that window it records a skip and performs no cycle call. Market
holidays remain protected by the existing M7 quote-freshness and broker
submission guards.

Each invocation appends a compact JSONL record to
`logs/sp1_demo_forward_v04.jsonl`. The log contains action/status metadata but
does not serialize secrets or the environment.

## Scheduler

The user timer checks every 15 minutes on weekdays:

`Mon..Fri *-*-* *:00/15:00`

The runner itself applies the New York session gate, avoiding dependence on
the host timezone.

`Persistent=true` is enabled. M10A found `Linger=no`, therefore M10B does not
claim reboot/logout autonomy. M10C must address user lingering before the
forward timer is enabled for unattended operation.

## M10B boundary

M10B may:

- create the runner, tests, contract, and unit files;
- install the unit files into the user systemd directory;
- reload the user systemd manager;
- validate unit syntax.

M10B must not enable or start the timer, call Trading212, create broker orders,
or mutate the operational SQLite database.
