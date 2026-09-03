from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sp1execution.broker.instruments import resolve_us_stock, resolve_vuaa_eur
from sp1execution.broker.trading212 import Trading212Client
from sp1execution.config import Settings
from sp1execution.engine.membership import load_latest_frozen_membership
from sp1execution.engine.planner import (
    InstrumentQuote,
    make_orders,
    position_quantities,
    value_position_eur,
)
from sp1execution.engine.strategy_engine import (
    event_type,
    replay_robust,
    target_mix_for_event,
)
from sp1execution.execution.broker_executor_v04 import (
    ReconciliationResult,
    SubmissionResult,
    create_and_start_from_fresh_orders,
    reconcile_current_phase,
    replan_and_install_buys,
    submit_current_phase,
)
from sp1execution.execution.recovery_v04 import classify_recovery
from sp1execution.execution.workflow_v04 import start_workflow
from sp1execution.market_data.yahoo_chart import YahooChartProvider
from sp1execution.state.transitions_v04 import (
    begin_month_end,
    classify_month_end,
    commit_membership_rebalance,
    transition_state,
)
from sp1execution.state.v04_store import connect

M7_SCHEMA = "m7_cycle_v1"
NY = ZoneInfo("America/New_York")
QUOTE_MAX_AGE_SECONDS = 300.0


class CycleError(RuntimeError):
    pass


class CycleSafetyError(CycleError):
    pass


@dataclass(frozen=True)
class MarketSnapshot:
    positions: list[dict[str, Any]]
    quotes: dict[str, InstrumentQuote]
    current_values_eur: dict[str, float]
    eurusd: float
    strategy_broker_tickers: tuple[str, ...]
    max_quote_age_seconds: float


@dataclass(frozen=True)
class CycleResult:
    action: str
    reason: str
    workflow_id: str | None = None
    decision_id: str | None = None
    broker_order_ids: tuple[str, ...] = ()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(sql, params).fetchone()
        return None if row is None else dict(row)
    finally:
        con.row_factory = previous


def _rows(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.row_factory = previous


def _machine_state(con: sqlite3.Connection) -> dict[str, Any]:
    row = _row(con, "SELECT * FROM machine_state WHERE id=1")
    if row is None:
        raise CycleError("machine_state singleton missing")
    return row


def _active_membership(state: dict[str, Any]) -> dict[str, Any]:
    raw = state["active_membership_json"]
    if raw is None:
        raise CycleSafetyError(
            "M7 requires migrated active membership; M8 handles bootstrap migration"
        )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CycleSafetyError("invalid active_membership_json")
    return value


def _membership_payload(membership: Any) -> dict[str, Any]:
    symbols = list(membership.symbols)
    if len(symbols) != 2 or len(set(symbols)) != 2:
        raise CycleSafetyError("frozen membership must contain exactly two symbols")
    return {
        "month": str(membership.month),
        "symbols": symbols,
        "source_as_of": str(membership.source_as_of),
        "source": str(getattr(membership, "source", "")),
    }


def _month_key(value: str) -> tuple[int, int]:
    try:
        year, month = value.split("-")
        result = int(year), int(month)
    except Exception as exc:
        raise CycleSafetyError(f"invalid membership month: {value!r}") from exc
    if not 1 <= result[1] <= 12:
        raise CycleSafetyError(f"invalid membership month: {value!r}")
    return result


def _is_regular_session(now: datetime) -> bool:
    if now.tzinfo is None:
        raise CycleSafetyError("cycle clock must be timezone-aware")
    ny = now.astimezone(NY)
    minutes = ny.hour * 60 + ny.minute
    return ny.weekday() < 5 and 9 * 60 + 35 <= minutes <= 15 * 60 + 55


def resolve_state_db_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
    elif os.environ.get("SP1_STATE_DB"):
        path = Path(os.environ["SP1_STATE_DB"]).expanduser()
    else:
        path = Path(__file__).resolve().parents[3] / "state/sp1execution.sqlite"
    if not path.exists():
        raise CycleSafetyError(f"v0.4 state database does not exist: {path}")
    return path.resolve()


def _default_overlay_loader(market: Any) -> Any:
    return replay_robust(market.completed_daily_history("IVV"))


def _default_snapshot_builder(
    broker: Any,
    market: Any,
    symbols: tuple[str, ...],
) -> MarketSnapshot:
    instruments = broker.instruments()
    positions = broker.positions(force_refresh=True)
    quotes: dict[str, InstrumentQuote] = {}
    ages: list[float] = []

    for symbol in symbols:
        inst = resolve_us_stock(instruments, symbol)
        q = market.quote(symbol)
        if q.currency != "USD" or q.price <= 0:
            raise CycleSafetyError(f"invalid USD quote for {symbol}")
        quotes[symbol] = InstrumentQuote(
            logical_symbol=symbol,
            broker_ticker=inst.ticker,
            price=float(q.price),
            currency="USD",
        )
        ages.append(float(q.age_seconds))

    vuaa_inst = resolve_vuaa_eur(instruments)
    vuaa_q = market.quote("VUAA.DE")
    if vuaa_q.currency != "EUR" or vuaa_q.price <= 0:
        raise CycleSafetyError("invalid EUR VUAA quote")
    quotes["VUAA"] = InstrumentQuote(
        logical_symbol="VUAA",
        broker_ticker=vuaa_inst.ticker,
        price=float(vuaa_q.price),
        currency="EUR",
    )
    ages.append(float(vuaa_q.age_seconds))

    fx = market.quote("EURUSD=X")
    if fx.price <= 0:
        raise CycleSafetyError("invalid EURUSD quote")
    ages.append(float(fx.age_seconds))

    quantities = position_quantities(positions)
    current_values: dict[str, float] = {}
    for logical, quote in quotes.items():
        current_values[logical] = value_position_eur(
            quantities.get(quote.broker_ticker, 0.0),
            quote,
            float(fx.price),
        )

    return MarketSnapshot(
        positions=positions,
        quotes=quotes,
        current_values_eur=current_values,
        eurusd=float(fx.price),
        strategy_broker_tickers=tuple(sorted(quote.broker_ticker for quote in quotes.values())),
        max_quote_age_seconds=max(ages),
    )


def _assert_fresh_snapshot(snapshot: MarketSnapshot, *, now: datetime) -> None:
    if not _is_regular_session(now):
        raise CycleSafetyError(
            "trade planning/replanning requires regular US session 09:35-15:55 New York"
        )
    if snapshot.max_quote_age_seconds > QUOTE_MAX_AGE_SECONDS:
        raise CycleSafetyError(f"fresh quote guard failed: {snapshot.max_quote_age_seconds:.0f}s")


def _durable_pending_membership_candidate(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    row = _row(
        con,
        """
        SELECT payload
        FROM state_transitions
        WHERE dimension='MEMBERSHIP'
          AND to_state='REBALANCE_PENDING'
        ORDER BY revision_after DESC
        LIMIT 1
        """,
    )
    if row is None:
        raise CycleSafetyError("REBALANCE_PENDING lacks durable membership transition evidence")
    payload = json.loads(row["payload"])
    candidate = payload.get("candidate_membership")
    if not isinstance(candidate, dict):
        raise CycleSafetyError("REBALANCE_PENDING transition lacks candidate_membership")
    return candidate


def _prepare_membership(
    con: sqlite3.Connection,
    *,
    candidate: dict[str, Any],
    created_at: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    state = _machine_state(con)
    active = _active_membership(state)
    membership_state = str(state["membership_state"])
    active_month = str(active["month"])
    candidate_month = str(candidate["month"])

    if _month_key(candidate_month) < _month_key(active_month):
        raise CycleSafetyError("frozen membership is older than durable active membership")

    if membership_state == "ACTIVE":
        if candidate_month == active_month:
            if set(candidate["symbols"]) != set(active["symbols"]):
                raise CycleSafetyError("same membership month changed Top2 set after activation")
            return active, active, False

        begin_month_end(
            con,
            event_key=f"m7:membership:{candidate_month}:begin",
            candidate_membership=candidate,
            created_at=created_at,
        )
        classify_month_end(
            con,
            event_key=f"m7:membership:{candidate_month}:classify",
            candidate_membership=candidate,
            created_at=created_at,
        )
        state = _machine_state(con)
        membership_state = str(state["membership_state"])
        if membership_state == "ACTIVE":
            new_active = _active_membership(state)
            return new_active, new_active, False
        if membership_state != "REBALANCE_PENDING":
            raise CycleSafetyError(
                f"unexpected membership state after classification: {membership_state}"
            )
        return active, candidate, True

    if membership_state == "MONTH_END_PENDING":
        classify_month_end(
            con,
            event_key=f"m7:membership:{candidate_month}:classify",
            candidate_membership=candidate,
            created_at=created_at,
        )
        state = _machine_state(con)
        if state["membership_state"] == "ACTIVE":
            new_active = _active_membership(state)
            return new_active, new_active, False
        if state["membership_state"] != "REBALANCE_PENDING":
            raise CycleSafetyError("MONTH_END_PENDING classification produced unexpected state")
        return active, candidate, True

    if membership_state == "REBALANCE_PENDING":
        durable = _durable_pending_membership_candidate(con)
        if str(durable["month"]) != candidate_month or set(durable["symbols"]) != set(
            candidate["symbols"]
        ):
            raise CycleSafetyError(
                "current frozen membership disagrees with durable pending candidate"
            )
        return active, durable, True

    raise CycleSafetyError(f"M7 cannot operate membership_state={membership_state}")


def _sp2_mix_for_event(
    *,
    event: str,
    membership: tuple[str, str],
    current_values_eur: dict[str, float],
    previous_mix: dict[str, float] | None,
) -> dict[str, float]:
    if event in {"BOOTSTRAP_INITIAL_ALLOCATION", "MONTHLY_MEMBERSHIP_CHANGE"}:
        return {membership[0]: 0.5, membership[1]: 0.5}

    current = {
        symbol: max(0.0, float(current_values_eur.get(symbol, 0.0))) for symbol in membership
    }
    total = sum(current.values())
    if total > 1e-9:
        return {symbol: current[symbol] / total for symbol in membership}

    if previous_mix and set(previous_mix) == set(membership):
        return {symbol: float(previous_mix[symbol]) for symbol in membership}

    return {membership[0]: 0.5, membership[1]: 0.5}


def _decision_id(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:16]
    return f"m7:{payload['overlay']['as_of']}:{payload['membership']['month']}:{digest}"


def _overlay_dict(overlay: Any) -> dict[str, Any]:
    return {
        "as_of": str(overlay.as_of),
        "ivv_close": float(overlay.ivv_close),
        "mode": str(overlay.mode),
        "old_peak": float(overlay.old_peak),
        "trough": None if overlay.trough is None else float(overlay.trough),
        "drawdown": float(overlay.drawdown),
        "recovery": None if overlay.recovery is None else float(overlay.recovery),
        "target_sp500": float(overlay.target_sp500),
        "last_event": str(overlay.last_event),
    }


def _apply_control_target(
    con: sqlite3.Connection,
    *,
    decision: dict[str, Any],
    created_at: str,
) -> None:
    overlay = decision["overlay"]
    mode = str(overlay["mode"])
    rearm_old_ath = float(overlay["old_peak"]) if mode == "POST_HANDOFF" else None

    updates: dict[str, Any] = {
        "active_overlay": float(overlay["target_sp500"]),
        "old_peak": float(overlay["old_peak"]),
        "trough": overlay["trough"],
        "rearm_old_ath": rearm_old_ath,
        "sp2_mix_json": decision["sp2_mix_after"],
    }

    transition_state(
        con,
        event_key=f"m7:{decision['decision_id']}:strategy-finalize",
        dimension="STRATEGY",
        to_state=mode,
        reason="m7_reconciled_strategy_target_commit",
        decision_id=decision["decision_id"],
        payload={
            "schema": M7_SCHEMA,
            "decision_id": decision["decision_id"],
            "overlay": decision["overlay"],
            "sp2_mix_after": decision["sp2_mix_after"],
            "updates": updates,
        },
        updates=updates,
        allow_same_state=True,
        created_at=created_at,
    )

    if decision["membership_requires_rebalance"]:
        commit_membership_rebalance(
            con,
            event_key=f"m7:{decision['decision_id']}:membership-finalize",
            new_membership=decision["membership"],
            created_at=created_at,
            decision_id=decision["decision_id"],
        )


def _transition_exists(con: sqlite3.Connection, event_key: str) -> bool:
    return (
        _row(
            con,
            "SELECT 1 AS ok FROM state_transitions WHERE event_key=?",
            (event_key,),
        )
        is not None
    )


def _pending_complete_workflow(
    con: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    candidates = []
    for workflow in _rows(
        con,
        """
        SELECT *
        FROM execution_workflows
        WHERE status='COMPLETE'
        ORDER BY updated_at,workflow_id
        """,
    ):
        target = json.loads(workflow["target_payload"])
        decision = target.get("decision")
        if not isinstance(decision, dict) or decision.get("schema") != M7_SCHEMA:
            continue
        did = str(decision["decision_id"])
        strategy_done = _transition_exists(con, f"m7:{did}:strategy-finalize")
        membership_done = not decision["membership_requires_rebalance"] or _transition_exists(
            con, f"m7:{did}:membership-finalize"
        )
        if not (strategy_done and membership_done):
            candidates.append((workflow, decision))

    if len(candidates) > 1:
        raise CycleSafetyError("multiple COMPLETE M7 workflows await control finalization")
    return None if not candidates else candidates[0]


def _workflow_decision(
    con: sqlite3.Connection,
    workflow_id: str,
) -> dict[str, Any]:
    row = _row(
        con,
        "SELECT target_payload FROM execution_workflows WHERE workflow_id=?",
        (workflow_id,),
    )
    if row is None:
        raise CycleError(f"unknown workflow: {workflow_id}")
    target = json.loads(row["target_payload"])
    decision = target.get("decision")
    if not isinstance(decision, dict) or decision.get("schema") != M7_SCHEMA:
        raise CycleSafetyError("recoverable workflow is not owned by M7 cycle schema")
    return decision


def _default_submission_guard(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    broker: Any,
    market: Any,
    now: datetime,
    overlay_loader: Callable[[Any], Any],
) -> None:
    if not _is_regular_session(now):
        raise CycleSafetyError("broker POST requires regular US session 09:35-15:55 New York")

    decision = _workflow_decision(con, workflow_id)
    latest = overlay_loader(market)
    latest_overlay = _overlay_dict(latest)
    if _canonical_json(latest_overlay) != _canonical_json(decision["overlay"]):
        raise CycleSafetyError(
            "workflow signal snapshot is stale relative to latest completed IVV state"
        )

    ages: list[float] = []
    for symbol in decision["valuation_symbols"]:
        ages.append(float(market.quote(symbol).age_seconds))
    ages.append(float(market.quote("VUAA.DE").age_seconds))
    ages.append(float(market.quote("EURUSD=X").age_seconds))
    if max(ages) > QUOTE_MAX_AGE_SECONDS:
        raise CycleSafetyError("submission quote freshness guard failed")

    workflow = _row(
        con,
        "SELECT phase FROM execution_workflows WHERE workflow_id=?",
        (workflow_id,),
    )
    if workflow is None:
        raise CycleError(f"unknown workflow: {workflow_id}")

    if workflow["phase"] == "BUY":
        leg = _row(
            con,
            """
            SELECT payload
            FROM execution_legs
            WHERE workflow_id=? AND side='BUY' AND status='PLANNED'
            ORDER BY leg_index
            LIMIT 1
            """,
            (workflow_id,),
        )
        if leg is not None:
            required = float(json.loads(leg["payload"])["estimated_notional_eur"])
            account = broker.account_summary(force_refresh=True)
            available = float(account["cash"]["availableToTrade"])
            if available + 0.01 < required:
                raise CycleSafetyError(
                    "broker available cash is below next strategy-authorized BUY"
                )


def _plan_new_work(
    con: sqlite3.Connection,
    *,
    broker: Any,
    market: Any,
    now: datetime,
    created_at: str,
    membership_loader: Callable[[], Any],
    overlay_loader: Callable[[Any], Any],
    snapshot_builder: Callable[[Any, Any, tuple[str, ...]], MarketSnapshot],
) -> CycleResult:
    candidate = _membership_payload(membership_loader())
    previous_membership, desired_membership, needs_rebalance = _prepare_membership(
        con,
        candidate=candidate,
        created_at=created_at,
    )

    state = _machine_state(con)
    overlay = overlay_loader(market)
    overlay_payload = _overlay_dict(overlay)

    previous_symbols = tuple(previous_membership["symbols"])
    desired_symbols = tuple(desired_membership["symbols"])

    event = event_type(
        previous_membership=previous_symbols,
        current_membership=desired_symbols,
        previous_overlay=float(state["active_overlay"]),
        current_overlay=float(overlay.target_sp500),
    )

    if event == "NO_TRADE_TRUE_HOLD":
        previous_mix = json.loads(state["sp2_mix_json"])
        seed = {
            "schema": M7_SCHEMA,
            "membership": desired_membership,
            "overlay": overlay_payload,
            "event": event,
            "sp2_mix_after": previous_mix,
            "membership_requires_rebalance": False,
        }
        did = _decision_id(seed)
        decision = {**seed, "decision_id": did}
        _apply_control_target(con, decision=decision, created_at=created_at)
        return CycleResult(
            action="NO_TRADE_CONTROL_COMMITTED",
            reason="true_hold_no_trade_strategy_snapshot_committed",
            decision_id=did,
        )

    if not _is_regular_session(now):
        return CycleResult(
            action="WAIT_REGULAR_SESSION",
            reason="trade_required_but_fresh_plan_waits_for_regular_us_session",
        )

    valuation_symbols = tuple(sorted(set(previous_symbols) | set(desired_symbols)))
    snapshot = snapshot_builder(broker, market, valuation_symbols)
    _assert_fresh_snapshot(snapshot, now=now)

    previous_mix = json.loads(state["sp2_mix_json"])
    target_weights = target_mix_for_event(
        event=event,
        membership=desired_symbols,
        overlay=float(overlay.target_sp500),
        current_values_eur=snapshot.current_values_eur,
        previous_mix=previous_mix,
    )
    if needs_rebalance:
        for symbol in previous_symbols:
            if symbol not in desired_symbols:
                target_weights[symbol] = 0.0

    invested = sum(
        max(0.0, float(snapshot.current_values_eur.get(symbol, 0.0)))
        for symbol in set(valuation_symbols) | {"VUAA"}
    )
    nav_eur = invested + float(state["strategy_cash_eur"])
    if nav_eur <= 1.0:
        raise CycleSafetyError(f"strategy NAV is not positive: {nav_eur:.2f}")

    orders, _ = make_orders(
        nav_eur=nav_eur,
        target_weights=target_weights,
        quotes=snapshot.quotes,
        positions=snapshot.positions,
        eurusd=snapshot.eurusd,
    )

    sp2_mix_after = _sp2_mix_for_event(
        event=event,
        membership=desired_symbols,
        current_values_eur=snapshot.current_values_eur,
        previous_mix=previous_mix,
    )
    seed = {
        "schema": M7_SCHEMA,
        "event": event,
        "membership": desired_membership,
        "previous_membership": previous_membership,
        "membership_requires_rebalance": needs_rebalance,
        "overlay": overlay_payload,
        "target_weights": target_weights,
        "sp2_mix_after": sp2_mix_after,
        "strategy_broker_tickers": list(snapshot.strategy_broker_tickers),
        "valuation_symbols": list(valuation_symbols),
        "strategy_nav_eur": nav_eur,
        "source_state_revision": int(state["revision"]),
    }
    did = _decision_id(seed)
    decision = {**seed, "decision_id": did}

    if not orders:
        _apply_control_target(con, decision=decision, created_at=created_at)
        return CycleResult(
            action="NO_ORDERS_CONTROL_COMMITTED",
            reason="fresh_plan_within_execution_tolerance",
            decision_id=did,
        )

    create_and_start_from_fresh_orders(
        con,
        decision_id=did,
        decision_payload=decision,
        positions=snapshot.positions,
        orders=orders,
        created_at=created_at,
    )
    return CycleResult(
        action="WORKFLOW_CREATED",
        reason="fresh_trade_plan_persisted_no_broker_post_this_cycle",
        workflow_id=f"m5b:{did}",
        decision_id=did,
    )


def run_cycle(
    con: sqlite3.Connection,
    *,
    broker: Any,
    market: Any,
    allow_demo_submit: bool,
    now_fn: Callable[[], datetime] | None = None,
    membership_loader: Callable[[], Any] | None = None,
    overlay_loader: Callable[[Any], Any] | None = None,
    snapshot_builder: Callable[[Any, Any, tuple[str, ...]], MarketSnapshot] | None = None,
    submission_guard: Callable[..., None] | None = None,
) -> CycleResult:
    now_fn = now_fn or (lambda: datetime.now(UTC))
    membership_loader = membership_loader or load_latest_frozen_membership
    overlay_loader = overlay_loader or _default_overlay_loader
    snapshot_builder = snapshot_builder or _default_snapshot_builder
    submission_guard = submission_guard or _default_submission_guard

    now = now_fn()
    if now.tzinfo is None:
        raise CycleSafetyError("cycle clock must be timezone-aware")
    created_at = now.astimezone(UTC).isoformat()

    pending_complete = _pending_complete_workflow(con)
    if pending_complete is not None:
        workflow, decision = pending_complete
        _apply_control_target(con, decision=decision, created_at=created_at)
        return CycleResult(
            action="CONTROL_FINALIZED",
            reason="completed_execution_promoted_to_durable_control_state",
            workflow_id=str(workflow["workflow_id"]),
            decision_id=str(decision["decision_id"]),
        )

    recovery = classify_recovery(con, created_at=created_at)

    if recovery.action == "NO_WORKFLOW":
        return _plan_new_work(
            con,
            broker=broker,
            market=market,
            now=now,
            created_at=created_at,
            membership_loader=membership_loader,
            overlay_loader=overlay_loader,
            snapshot_builder=snapshot_builder,
        )

    workflow_id = recovery.workflow_id
    if workflow_id is None:
        raise CycleError("recovery action requires workflow_id")

    if recovery.action == "START_WORKFLOW":
        start_workflow(con, workflow_id=workflow_id, created_at=created_at)
        return CycleResult(
            action="WORKFLOW_STARTED",
            reason=recovery.reason,
            workflow_id=workflow_id,
        )

    if recovery.action in {"SUBMIT_SELL", "SUBMIT_BUY"}:
        if not allow_demo_submit:
            return CycleResult(
                action="SUBMISSION_BLOCKED_CONFIRM_REQUIRED",
                reason="pass --confirm-demo to permit this durable DEMO submission",
                workflow_id=workflow_id,
            )

        submission_guard(
            con,
            workflow_id=workflow_id,
            broker=broker,
            market=market,
            now=now,
            overlay_loader=overlay_loader,
        )
        result: SubmissionResult = submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: datetime.now(UTC).isoformat(),
        )
        return CycleResult(
            action="SELL_SUBMITTED" if result.phase == "SELL" else "BUY_SUBMITTED",
            reason="m5b_durable_intent_then_demo_post",
            workflow_id=workflow_id,
            broker_order_ids=result.broker_order_ids,
        )

    if recovery.action in {"RECONCILE_SELL", "RECONCILE_BUY"}:
        result: ReconciliationResult = reconcile_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            created_at=created_at,
        )
        return CycleResult(
            action="SELL_RECONCILED" if result.phase == "SELL" else "BUY_RECONCILED",
            reason="known_broker_order_reconciled_without_new_post",
            workflow_id=workflow_id,
        )

    if recovery.action == "REPLAN_BUYS":
        decision = _workflow_decision(con, workflow_id)
        if not _is_regular_session(now):
            return CycleResult(
                action="WAIT_REGULAR_SESSION",
                reason="buy_replan_waits_for_fresh_regular_session_quotes",
                workflow_id=workflow_id,
            )
        snapshot = snapshot_builder(
            broker,
            market,
            tuple(decision["valuation_symbols"]),
        )
        _assert_fresh_snapshot(snapshot, now=now)
        replan_and_install_buys(
            con,
            workflow_id=workflow_id,
            target_weights={str(k): float(v) for k, v in decision["target_weights"].items()},
            quotes=snapshot.quotes,
            positions=snapshot.positions,
            eurusd=snapshot.eurusd,
            created_at=created_at,
        )
        return CycleResult(
            action="BUYS_REPLANNED",
            reason="post_sell_buy_plan_rebuilt_from_real_positions_and_m3_cash",
            workflow_id=workflow_id,
            decision_id=decision["decision_id"],
        )

    if recovery.action == "MANUAL_RECONCILIATION":
        return CycleResult(
            action="MANUAL_RECONCILIATION_REQUIRED",
            reason=recovery.reason,
            workflow_id=workflow_id,
        )

    if recovery.action == "FAILED":
        return CycleResult(
            action="FAILED_MANUAL_REVIEW",
            reason=recovery.reason,
            workflow_id=workflow_id,
        )

    if recovery.action == "COMPLETE":
        return CycleResult(
            action="COMPLETE",
            reason=recovery.reason,
            workflow_id=workflow_id,
        )

    raise CycleError(f"unsupported recovery action: {recovery.action}")


def cmd_cycle(args: Any) -> int:
    settings = Settings.from_env()
    if settings.t212_env != "demo":
        raise CycleSafetyError("M7 cycle is DEMO-only; SP1_T212_ENV must be demo")

    db_path = resolve_state_db_path(getattr(args, "db_path", None))
    broker = Trading212Client(settings)
    market = YahooChartProvider()

    con = connect(db_path)
    try:
        result = run_cycle(
            con,
            broker=broker,
            market=market,
            allow_demo_submit=bool(getattr(args, "confirm_demo", False)),
        )
    finally:
        con.close()

    print("SP1Execution cycle v0.4 M7")
    print(f"ACTION={result.action}")
    print(f"REASON={result.reason}")
    if result.workflow_id is not None:
        print(f"WORKFLOW_ID={result.workflow_id}")
    if result.decision_id is not None:
        print(f"DECISION_ID={result.decision_id}")
    if result.broker_order_ids:
        print("BROKER_ORDER_IDS=" + ",".join(result.broker_order_ids))
    print("LIVE_APPROVED=0")
    return 0
