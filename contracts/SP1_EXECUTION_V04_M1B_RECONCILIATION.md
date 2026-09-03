# SP1Execution v0.4 M1B — reconciliation adapter

M1B replaces the v0.3 flat-history assumption in CLI reconciliation.

Evidence priority:

1. exact broker order ID in canonical nested historical order/fill data;
2. pending order endpoint;
3. absent from both => UNKNOWN.

Hard rules:

- intended ticker must match historical broker order;
- intended quantity magnitude must match;
- malformed history fails closed;
- pagination loops or excessive pages fail closed;
- UNKNOWN never becomes FILLED;
- M1B creates, cancels, or modifies no broker order.

Only a set of fully reconciled FILLED orders can activate a decision.
