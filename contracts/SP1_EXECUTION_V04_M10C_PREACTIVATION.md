# SP1Execution v0.4 M10C - Automated Demo pre-activation gate

M10C is the final pre-activation gate before the unattended Trading212 Demo
forward timer is enabled.

## Preconditions

- M10B is frozen at `sp1-v0.4-m10b-forward-runner`.
- The operational SQLite state is still the frozen M8/M9/M10B state.
- The repository worktree is clean.
- `SP1_T212_ENV=demo`.
- `SP1_LIVE_TRADING=false`.
- the installed systemd user units byte-match the frozen repository units.
- the forward timer is disabled and inactive.
- user lingering is enabled.

User lingering is required so the user systemd manager can survive logout and
start at boot independently of an interactive desktop session.

## Controlled dry-run

M10C imports the frozen runner as a Python module and executes `run_once()`
with:

- a temporary SQLite path;
- a temporary JSONL log;
- `confirm_demo=True`;
- `dry_run=True`;
- an explicitly synthetic in-session UTC timestamp.

The expected result is `DRY_RUN_READY`. This proves the automation wrapper
can traverse its Demo-only, New-York-session, and audit-log gates without
calling `sp1exec cycle` or a broker.

## Activation boundary

M10C does **not** enable or start the timer.

The next milestone may activate
`sp1execution-demo-forward.timer`. Once activated, future in-session timer
runs are allowed to invoke `sp1exec cycle --confirm-demo` and therefore may
make Trading212 Demo reads and, only when the durable recovery/state machine
authorizes it, Demo order POSTs.

Live trading remains unapproved.
