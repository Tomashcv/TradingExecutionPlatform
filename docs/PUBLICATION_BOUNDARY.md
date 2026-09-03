# Publication boundary

This portfolio release exposes the execution architecture, source code, tests and protocol contracts while removing account-specific evidence.

Excluded from the public release:

- real broker order identifiers and fill receipts;
- demo-account balances and observed position snapshots;
- generated operational evidence;
- runtime databases, caches and logs;
- credentials and private `.env` files.

The committed `.env.example` contains empty credential fields only and keeps live trading disabled.

`contracts/research/` contains compact derived research fixtures used to reproduce deterministic recovery semantics. It does not contain broker credentials or account-level execution evidence.
