# SP1Execution v0.4 M10D - Automated Demo forward activation

M10D activates the unattended Trading212 Demo forward timer after the frozen
M10C pre-activation gate.

Activation is allowed only when the repository, operational SQLite state,
Demo-only environment, linger setting, installed systemd units, and frozen
runner all match the M10C/M10B evidence.

The activation script must itself run outside 09:35-15:55
America/New_York. It first performs a controlled real systemd service start.
Because the runner is outside its US regular-session window, the service must
append `SKIP_OUTSIDE_US_REGULAR_WINDOW` and must not invoke `sp1exec cycle`.

A systemd oneshot service that has never run may legitimately report
`Unit ... not loaded` to `systemctl reset-failed` even though its unit file is
installed and loadable. M10D treats only that exact reset-failed condition as
benign, performs `daemon-reload`, verifies the unit with `systemctl cat`, and
then starts the service normally.

After the zero-broker service proof, M10D enables and starts
`sp1execution-demo-forward.timer`.

From future in-session timer events onward, the service may invoke
`sp1exec cycle --confirm-demo`, which may perform Trading212 Demo reads and
only those Demo order POSTs authorized by the frozen durable state machine.

Live trading remains unapproved. M10D marks the prospective Demo forward test
ACTIVE, not completed or validated.
