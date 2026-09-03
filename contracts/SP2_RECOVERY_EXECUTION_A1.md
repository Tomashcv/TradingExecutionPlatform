# SP2RecoveryExecution A1

## Frozen strategy identity

Rule:

`SP2_RECOVERY_CORE_RETURN_D40_H378_V1`

- S&P/IVV previous-running-ATH crash detector
- -30% -> 10%
- -35% -> 30%
- -45% -> 60%
- -50% -> 100%
- source signal close T
- source execution T+1
- every positive entry/scale event delayed 40 canonical US trading intervals
- first actual delayed entry anchors one H378 clock
- later scale-ups do not reset H378
- fixed H378 exit
- no recovery-percent exit
- no new crash cycle until fixed exit and old ATH recovery
- historical recovery proxy: SOXX
- execution UCITS identity: ISIN IE00BMC38736

## Scope

A1 only introduces a pure strategy-policy module and byte-identical frozen C2
research artifacts.

It does not modify the inherited durable executor.

No broker calls.
No Demo orders.
No live orders.

`LIVE_EXECUTION_AUTHORIZED=0`
