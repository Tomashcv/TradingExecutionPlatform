from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sp1execution.broker.instruments import resolve_us_stock, resolve_vuaa_eur
from sp1execution.broker.trading212 import Trading212Client
from sp1execution.config import Settings
from sp1execution.engine.capital import initial_capital_eur
from sp1execution.engine.membership import (
    archive_latest_ivv,
    freeze_month_from_archive,
    load_latest_frozen_membership,
)
from sp1execution.engine.planner import (
    InstrumentQuote,
    make_orders,
    position_quantities,
    value_position_eur,
)
from sp1execution.engine.reconciliation_v04 import (
    HistoryPaginationError,
    HistorySchemaError,
    reconcile_accepted_attempts,
)
from sp1execution.engine.strategy_engine import (
    decision_id,
    event_type,
    replay_robust,
    target_mix_for_event,
)
from sp1execution.execution.cycle_v04 import cmd_cycle
from sp1execution.market_data.yahoo_chart import YahooChartProvider
from sp1execution.state.journal import Journal

SCHEMA_VERSION = "0.3.3q1"


def _context():
    settings = Settings.from_env()
    broker = Trading212Client(settings)
    market = YahooChartProvider()
    journal = Journal()
    membership = load_latest_frozen_membership()
    overlay = replay_robust(market.completed_daily_history("IVV"))
    return settings, broker, market, journal, membership, overlay


def _logical_quotes(
    broker: Trading212Client,
    market: YahooChartProvider,
    symbols,
):
    instruments = broker.instruments()
    out = {}
    raw_quotes = {}

    for symbol in symbols:
        inst = resolve_us_stock(instruments, symbol)
        q = market.quote(symbol)
        if q.currency != "USD":
            raise RuntimeError(f"Expected USD quote for {symbol}, got {q.currency}")
        out[symbol] = InstrumentQuote(
            logical_symbol=symbol,
            broker_ticker=inst.ticker,
            price=q.price,
            currency="USD",
        )
        raw_quotes[symbol] = q

    vuaa = resolve_vuaa_eur(instruments)
    q_vuaa = market.quote("VUAA.DE")
    if q_vuaa.currency != "EUR":
        raise RuntimeError(f"Expected EUR quote for VUAA.DE, got {q_vuaa.currency}")
    out["VUAA"] = InstrumentQuote(
        logical_symbol="VUAA",
        broker_ticker=vuaa.ticker,
        price=q_vuaa.price,
        currency="EUR",
    )
    raw_quotes["VUAA"] = q_vuaa

    fx = market.quote("EURUSD=X")
    if fx.price <= 0:
        raise RuntimeError("Invalid EURUSD quote.")
    raw_quotes["EURUSD"] = fx
    return out, raw_quotes, fx.price


def _portfolio_values(
    *,
    broker: Trading212Client,
    market: YahooChartProvider,
    symbols,
    positions,
):
    quotes, raw_quotes, eurusd = _logical_quotes(broker, market, symbols)
    quantities = position_quantities(positions)
    values = {}
    for logical, quote in quotes.items():
        quantity = quantities.get(quote.broker_ticker, 0.0)
        values[logical] = value_position_eur(quantity, quote, eurusd)
    return quotes, raw_quotes, eurusd, values


def _strategy_plan():
    settings, broker, market, journal, membership, overlay = _context()
    account = broker.account_summary()
    positions = broker.positions()

    account_total = float(account["totalValue"])
    initial_capital = initial_capital_eur()

    previous_membership_raw = journal.get_kv("active_membership")
    previous_membership = (
        tuple(previous_membership_raw["symbols"]) if previous_membership_raw is not None else None
    )
    previous_overlay = journal.get_kv("active_overlay")
    previous_mix = journal.get_kv("sp2_mix")

    valuation_symbols = set(membership.symbols)
    if previous_membership is not None:
        valuation_symbols.update(previous_membership)

    quotes, raw_quotes, eurusd, current_values = _portfolio_values(
        broker=broker,
        market=market,
        symbols=tuple(sorted(valuation_symbols)),
        positions=positions,
    )

    event = event_type(
        previous_membership=previous_membership,
        current_membership=membership.symbols,
        previous_overlay=previous_overlay,
        current_overlay=overlay.target_sp500,
    )

    if previous_membership is None:
        # Only bootstrap may inject fresh cash, and only up to the configured sleeve.
        strategy_nav = initial_capital
        existing_sp1_value = sum(
            current_values.get(symbol, 0.0) for symbol in set(membership.symbols) | {"VUAA"}
        )
        if existing_sp1_value > max(5.0, 0.001 * initial_capital):
            raise RuntimeError(
                "Bootstrap refused: SP1 instruments already have material positions. "
                "Cannot distinguish ownership from another strategy."
            )
        capital_policy = "BOOTSTRAP_FROM_ACCOUNT_ONCE"
    else:
        # Once active, this strategy is self-financing. No replenishment from the
        # rest of the Trading212 account after drawdowns.
        owned_symbols = set(previous_membership) | {"VUAA"}
        strategy_nav = sum(max(0.0, current_values.get(symbol, 0.0)) for symbol in owned_symbols)
        if strategy_nav <= 1.0:
            raise RuntimeError(
                "Active SP1 sleeve has no material marked value; refusing external top-up."
            )
        capital_policy = "SELF_FINANCING_NO_EXTERNAL_TOPUP"

    targets = target_mix_for_event(
        event=event,
        membership=membership.symbols,
        overlay=overlay.target_sp500,
        current_values_eur=current_values,
        previous_mix=previous_mix,
    )

    # A changed Top2 set must also explicitly liquidate the outgoing SP1 name.
    if targets and previous_membership is not None:
        for symbol in previous_membership:
            if symbol not in membership.symbols:
                targets[symbol] = 0.0

    if event == "NO_TRADE_TRUE_HOLD":
        orders = []
    else:
        orders, _ = make_orders(
            nav_eur=strategy_nav,
            target_weights=targets,
            quotes=quotes,
            positions=positions,
            eurusd=eurusd,
        )

    did = decision_id(
        membership_month=membership.month,
        symbols=membership.symbols,
        overlay=overlay,
        initial_capital_eur=initial_capital,
        schema_version=SCHEMA_VERSION,
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": did,
        "environment": settings.t212_env,
        "event": event,
        "membership": {
            "month": membership.month,
            "source_as_of": membership.source_as_of,
            "symbols": list(membership.symbols),
        },
        "previous_membership": (None if previous_membership is None else list(previous_membership)),
        "overlay": {
            "as_of": overlay.as_of,
            "mode": overlay.mode,
            "ivv_close": overlay.ivv_close,
            "old_peak": overlay.old_peak,
            "trough": overlay.trough,
            "drawdown_pct": overlay.drawdown * 100,
            "recovery_pct": None if overlay.recovery is None else overlay.recovery * 100,
            "target_sp500_pct": overlay.target_sp500 * 100,
        },
        "account_total_eur": account_total,
        "strategy_initial_capital_eur": initial_capital,
        "strategy_nav_eur": strategy_nav,
        "capital_policy": capital_policy,
        "eurusd": eurusd,
        "quotes": {
            key: {
                "price": value.price,
                "currency": value.currency,
                "age_seconds": value.age_seconds,
            }
            for key, value in raw_quotes.items()
        },
        "current_values_eur": current_values,
        "target_weights": targets,
        "orders": [
            {
                "logical_symbol": order.logical_symbol,
                "broker_ticker": order.broker_ticker,
                "quantity": order.quantity,
                "side": order.side,
                "estimated_notional_eur": order.estimated_notional_eur,
                "delta_eur": order.delta_eur,
            }
            for order in orders
        ],
    }
    return settings, broker, market, journal, membership, overlay, payload


def cmd_status(_: argparse.Namespace) -> int:
    settings, broker, _, journal, membership, overlay = _context()
    account = broker.account_summary()
    positions = broker.positions()

    print("SP1Execution status v0.3.3")
    print(f"environment={settings.t212_env}")
    print("LIVE_APPROVED=0")
    print(f"MEMBERSHIP_MONTH={membership.month}")
    print(f"MEMBERSHIP_SOURCE_AS_OF={membership.source_as_of}")
    print("MEMBERSHIP_SYMBOLS=" + ",".join(membership.symbols))
    print(f"ROBUST_AS_OF={overlay.as_of}")
    print(f"ROBUST_MODE={overlay.mode}")
    print(f"IVV_CLOSE={overlay.ivv_close:.6f}")
    print(f"IVV_OLD_PEAK={overlay.old_peak:.6f}")
    print(f"IVV_DRAWDOWN_PCT={overlay.drawdown * 100:.4f}")
    if overlay.trough is not None:
        print(f"IVV_TROUGH={overlay.trough:.6f}")
    if overlay.recovery is not None:
        print(f"IVV_RECOVERY_PCT={overlay.recovery * 100:.4f}")
    print(f"TARGET_SP500_PCT={overlay.target_sp500 * 100:.1f}")
    print(f"TARGET_SP2_PCT={(1 - overlay.target_sp500) * 100:.1f}")
    print(f"ACCOUNT_TOTAL_EUR={float(account['totalValue']):.2f}")
    print(f"SP1_INITIAL_CAPITAL_EUR={initial_capital_eur():.2f}")
    print(f"POSITIONS_COUNT={len(positions)}")
    print("JOURNAL_ACTIVE_MEMBERSHIP=" + json.dumps(journal.get_kv("active_membership")))
    print("JOURNAL_ACTIVE_OVERLAY=" + json.dumps(journal.get_kv("active_overlay")))
    return 0


def cmd_plan(_: argparse.Namespace) -> int:
    _, _, _, journal, membership, overlay, payload = _strategy_plan()
    did = payload["decision_id"]

    existing = journal.get_decision(did)
    if existing is None:
        journal.put_decision(did, "PLANNED", payload)
        status = "NEW"
    else:
        status = f"EXISTS_{existing['status']}"

    print("DECISION_ID=" + did)
    print("DECISION_RECORD=" + status)
    print("EVENT=" + payload["event"])
    print("MEMBERSHIP=" + ",".join(membership.symbols))
    print(f"OVERLAY_SP500_PCT={overlay.target_sp500 * 100:.1f}")
    print("TARGET_WEIGHTS=" + json.dumps(payload["target_weights"], sort_keys=True))
    print(f"ACCOUNT_TOTAL_EUR={payload['account_total_eur']:.2f}")
    print(f"SP1_INITIAL_CAPITAL_EUR={payload['strategy_initial_capital_eur']:.2f}")
    print(f"SP1_STRATEGY_NAV_EUR={payload['strategy_nav_eur']:.2f}")
    print("CAPITAL_POLICY=" + payload["capital_policy"])
    print(f"EURUSD={payload['eurusd']:.6f}")
    print(f"ORDER_COUNT={len(payload['orders'])}")
    for order in payload["orders"]:
        print("ORDER=" + json.dumps(order, sort_keys=True))
    print("DEMO_EXECUTION_READY=1" if payload["orders"] else "DEMO_EXECUTION_READY=0")
    return 0


def _activate_filled_decision(journal: Journal, decision: dict) -> None:
    payload = decision["payload"]
    journal.set_kv(
        "active_membership",
        {
            "month": payload["membership"]["month"],
            "symbols": payload["membership"]["symbols"],
        },
    )
    journal.set_kv(
        "active_overlay",
        payload["overlay"]["target_sp500_pct"] / 100.0,
    )

    targets = payload["target_weights"]
    sp2_total = sum(targets.get(symbol, 0.0) for symbol in payload["membership"]["symbols"])
    if sp2_total > 1e-12:
        journal.set_kv(
            "sp2_mix",
            {symbol: targets[symbol] / sp2_total for symbol in payload["membership"]["symbols"]},
        )


def _reconcile_decision(
    *,
    journal: Journal,
    broker: Trading212Client,
    decision: dict,
) -> str:
    attempts = journal.accepted_order_attempts(decision["decision_id"])
    if not attempts:
        return "NO_ACCEPTED_ORDERS"

    pending_payload = broker.pending_orders()

    try:
        states = reconcile_accepted_attempts(
            attempts=attempts,
            pending_payload=pending_payload,
            fetch_history_page=lambda path: broker._request("GET", path),
        )
    except (HistorySchemaError, HistoryPaginationError, TypeError):
        journal.update_decision_status(
            decision["decision_id"],
            "AMBIGUOUS_REQUIRES_RECONCILIATION",
        )
        raise

    printable = [
        {
            "id": row.broker_order_id,
            "ticker": row.ticker,
            "state": row.state,
            "expectedQuantity": row.expected_quantity,
            "filledQuantity": row.filled_quantity,
            "brokerStatus": row.broker_status,
            "evidenceSource": row.evidence_source,
        }
        for row in states
    ]
    print("BROKER_ORDER_STATES=" + json.dumps(printable, sort_keys=True))

    state_set = {row.state for row in states}

    if state_set == {"FILLED"}:
        _activate_filled_decision(journal, decision)
        journal.update_decision_status(
            decision["decision_id"],
            "RECONCILED_FILLED",
        )
        return "FILLED"

    if "FAILED" in state_set:
        journal.update_decision_status(
            decision["decision_id"],
            "RECONCILIATION_FAILED",
        )
        return "FAILED"

    if "PARTIAL" in state_set:
        journal.update_decision_status(
            decision["decision_id"],
            "PARTIAL_REQUIRES_RECONCILIATION",
        )
        return "PARTIAL"

    if "UNKNOWN" in state_set:
        journal.update_decision_status(
            decision["decision_id"],
            "SUBMITTED_PENDING_RECONCILIATION",
        )
        return "UNKNOWN_OR_HISTORY_PROPAGATING"

    journal.update_decision_status(
        decision["decision_id"],
        "SUBMITTED_PENDING_RECONCILIATION",
    )
    return "PENDING"


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if settings.t212_env != "demo":
        raise RuntimeError("Reconciliation command currently supports demo only.")

    broker = Trading212Client(settings)
    journal = Journal()
    decision = journal.get_decision(args.decision_id)
    if decision is None:
        raise RuntimeError(f"Unknown decision_id: {args.decision_id}")

    result = _reconcile_decision(
        journal=journal,
        broker=broker,
        decision=decision,
    )
    print("RECONCILIATION_RESULT=" + result)
    print("LIVE_APPROVED=0")
    return 0


def _fresh_execution_orders(payload, broker, market, membership):
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            "Execution refused: legacy decision predates terminal-reconciliation v0.3.3."
        )

    account = broker.account_summary(force_refresh=True)
    positions = broker.positions(force_refresh=True)
    available_cash = float(account["cash"]["availableToTrade"])

    previous_membership_raw = payload.get("previous_membership")
    previous_membership = (
        None if previous_membership_raw is None else tuple(previous_membership_raw)
    )

    valuation_symbols = set(membership.symbols)
    if previous_membership is not None:
        valuation_symbols.update(previous_membership)

    quotes, raw_quotes, eurusd, current_values = _portfolio_values(
        broker=broker,
        market=market,
        symbols=tuple(sorted(valuation_symbols)),
        positions=positions,
    )

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    minutes = now_ny.hour * 60 + now_ny.minute
    regular_session = now_ny.weekday() < 5 and 9 * 60 + 35 <= minutes <= 15 * 60 + 55

    max_allowed_age = 300 if regular_session else 36 * 60 * 60
    max_age = max(q.age_seconds for q in raw_quotes.values())

    if max_age > max_allowed_age:
        raise RuntimeError(
            f"Execution refused: quote set too old ({max_age:.0f}s > {max_allowed_age}s)."
        )

    if previous_membership is None:
        nav = float(payload["strategy_initial_capital_eur"])
        existing_sp1_value = sum(
            current_values.get(symbol, 0.0) for symbol in set(membership.symbols) | {"VUAA"}
        )
        if existing_sp1_value > max(5.0, 0.001 * nav):
            raise RuntimeError(
                "Bootstrap execution refused: SP1 instruments already contain material positions."
            )
    else:
        owned_symbols = set(previous_membership) | {"VUAA"}
        nav = sum(max(0.0, current_values.get(symbol, 0.0)) for symbol in owned_symbols)
        if nav <= 1.0:
            raise RuntimeError(
                "Execution refused: active sleeve cannot be valued without external top-up."
            )

    targets = payload["target_weights"]
    if not targets:
        return [], raw_quotes

    orders, _ = make_orders(
        nav_eur=nav,
        target_weights=targets,
        quotes=quotes,
        positions=positions,
        eurusd=eurusd,
    )

    buy_notional = sum(order.estimated_notional_eur for order in orders if order.side == "BUY")
    sell_notional = sum(order.estimated_notional_eur for order in orders if order.side == "SELL")

    if previous_membership is None:
        hard_ceiling = float(payload["strategy_initial_capital_eur"])
        if buy_notional > hard_ceiling + 0.01:
            raise RuntimeError(
                f"Execution refused: bootstrap buys {buy_notional:.2f} exceed "
                f"SP1 ceiling {hard_ceiling:.2f}."
            )
        if available_cash + 0.01 < buy_notional:
            raise RuntimeError(
                "Execution refused: insufficient broker cash for isolated bootstrap."
            )
    elif buy_notional > 0 and sell_notional > 0:
        raise RuntimeError(
            "Execution refused: mixed SELL+BUY rebalance requires two-phase "
            "sell-fill-then-buy executor. External account cash may not bridge SP1."
        )

    return orders, raw_quotes


def cmd_execute_demo(args: argparse.Namespace) -> int:
    if not args.confirm_demo:
        raise RuntimeError("Execution refused: pass --confirm-demo explicitly.")

    settings, broker, market, journal, membership, _ = _context()
    if settings.t212_env != "demo":
        raise RuntimeError("Execution refused: SP1_T212_ENV must be demo.")

    decision = journal.get_decision(args.decision_id)
    if decision is None:
        raise RuntimeError(f"Unknown decision_id: {args.decision_id}")
    if decision["payload"].get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            "Execution refused: legacy decision is superseded by terminal-reconciliation v0.3.3."
        )
    if decision["status"] != "PLANNED":
        raise RuntimeError(f"Decision is not executable from status {decision['status']}.")
    if journal.has_order_attempts(args.decision_id):
        raise RuntimeError(
            "Execution refused: order attempts already exist for this decision. "
            "Reconcile manually; automatic retry is forbidden."
        )

    latest_completed_overlay = replay_robust(market.completed_daily_history("IVV"))
    signal_as_of = decision["payload"]["overlay"]["as_of"]
    if latest_completed_overlay.as_of != signal_as_of:
        raise RuntimeError(
            "Execution refused: stale decision. "
            f"decision_signal={signal_as_of} "
            f"latest_completed_signal={latest_completed_overlay.as_of}"
        )

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    minutes = now_ny.hour * 60 + now_ny.minute
    regular_session = now_ny.weekday() < 5 and 9 * 60 + 35 <= minutes <= 15 * 60 + 55

    if regular_session:
        print("SUBMISSION_MODE=REGULAR_SESSION")
    else:
        print("SUBMISSION_MODE=QUEUED_NEXT_REGULAR_SESSION")
        print("ORDER_BEHAVIOR=QUEUE_UNTIL_NEXT_REGULAR_MARKET_OPEN")

    print("EXTENDED_HOURS=0")

    pending = broker.pending_orders()
    if pending:
        raise RuntimeError(
            f"Execution refused: broker has pending orders before this decision: {pending}"
        )

    orders, _ = _fresh_execution_orders(
        decision["payload"],
        broker,
        market,
        membership,
    )
    if not orders:
        journal.update_decision_status(args.decision_id, "NO_ORDERS")
        print("NO_ORDERS_AFTER_FRESH_RECONCILIATION=1")
        return 0

    journal.update_decision_status(args.decision_id, "SUBMITTING")

    try:
        for order in orders:
            journal.record_order_attempt(
                decision_id=args.decision_id,
                ticker=order.broker_ticker,
                quantity=order.quantity,
                side=order.side,
                status="INTENT_RECORDED",
            )
            response = broker.market_order_demo_only(
                order.broker_ticker,
                order.quantity,
            )
            broker_order_id = None
            if isinstance(response, dict) and response.get("id") is not None:
                broker_order_id = str(response["id"])
            journal.record_order_attempt(
                decision_id=args.decision_id,
                ticker=order.broker_ticker,
                quantity=order.quantity,
                side=order.side,
                status="BROKER_ACCEPTED",
                broker_order_id=broker_order_id,
                response=response,
            )
            print(
                "SUBMITTED="
                + json.dumps(
                    {
                        "ticker": order.broker_ticker,
                        "quantity": order.quantity,
                        "side": order.side,
                        "broker_response": response,
                    },
                    sort_keys=True,
                )
            )
    except Exception:
        journal.update_decision_status(
            args.decision_id,
            "AMBIGUOUS_REQUIRES_RECONCILIATION",
        )
        raise

    journal.update_decision_status(
        args.decision_id,
        "SUBMITTED_PENDING_RECONCILIATION",
    )
    print("DEMO_SUBMISSION_COMPLETE=1")
    print("RECONCILE_AFTER_SECONDS=10")
    print(f"RECONCILE_COMMAND=sp1exec reconcile --decision-id {args.decision_id}")
    print("LIVE_APPROVED=0")
    return 0


def cmd_archive_holdings(_: argparse.Namespace) -> int:
    path = archive_latest_ivv()
    print(f"ARCHIVED={path}")
    return 0


def cmd_freeze_month(args: argparse.Namespace) -> int:
    path = freeze_month_from_archive(args.month)
    print(f"FROZEN={path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sp1exec")
    sub = parser.add_subparsers(required=True)

    status = sub.add_parser(
        "status",
        help="Current frozen membership + ROBUST state.",
    )
    status.set_defaults(func=cmd_status)

    plan = sub.add_parser(
        "plan",
        help="Create an idempotent isolated-sleeve strategy decision.",
    )
    plan.set_defaults(func=cmd_plan)

    execute = sub.add_parser(
        "execute-demo",
        help="Submit a planned isolated-sleeve decision to Trading212 DEMO only.",
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--confirm-demo", action="store_true")
    execute.set_defaults(func=cmd_execute_demo)

    reconcile = sub.add_parser(
        "reconcile",
        help="Reconcile a submitted DEMO decision against broker order states.",
    )
    reconcile.add_argument("--decision-id", required=True)
    reconcile.set_defaults(func=cmd_reconcile)

    archive = sub.add_parser(
        "archive-holdings",
        help="Archive the latest IVV holdings snapshot for future month-end freeze.",
    )
    archive.set_defaults(func=cmd_archive_holdings)

    freeze = sub.add_parser(
        "freeze-month",
        help="Freeze the last archived IVV Top2 snapshot for YYYY-MM.",
    )
    freeze.add_argument("--month", required=True)
    freeze.set_defaults(func=cmd_freeze_month)

    cycle = sub.add_parser(
        "cycle",
        help="Advance the durable v0.4 execution state machine by one safe action.",
    )
    cycle.add_argument(
        "--confirm-demo",
        action="store_true",
        help="Permit a DEMO broker POST only when recovery says submission is safe.",
    )
    cycle.add_argument(
        "--db-path",
        default=None,
        help="Optional v0.4 SQLite path; defaults to SP1_STATE_DB or repository state DB.",
    )
    cycle.set_defaults(func=cmd_cycle)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
