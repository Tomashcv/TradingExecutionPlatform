from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sp1execution.broker.history_v04 import CanonicalHistoricalOrder

CENT = Decimal("0.01")
TOL = 0.000001


class CapitalLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class NormalizedFillEvent:
    event_key: str
    broker_order_id: str
    fill_id: str
    ticker: str
    side: str
    filled_at: str
    quantity: float
    price: float
    cash_delta_eur: float
    fee_eur: float
    fx_rate: float | None
    wallet_net_value_eur: float
    payload: str


@dataclass(frozen=True)
class AppliedFillResult:
    event_key: str
    status: str
    old_revision: int
    new_revision: int
    old_strategy_cash_eur: float
    new_strategy_cash_eur: float
    old_external_cash_debt_eur: float
    new_external_cash_debt_eur: float
    external_debt_change_eur: float
    old_realized_fees_eur: float
    new_realized_fees_eur: float


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _float_money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def normalize_order_fills(
    order: CanonicalHistoricalOrder,
) -> list[NormalizedFillEvent]:
    side = order.side.upper()

    if side not in {"BUY", "SELL"}:
        raise CapitalLedgerError(f"Unsupported broker side for capital ledger: {order.side!r}")

    if not order.fills:
        return []

    events: list[NormalizedFillEvent] = []

    for fill in sorted(
        order.fills,
        key=lambda row: (row.filled_at, row.fill_id),
    ):
        if fill.wallet_currency != "EUR":
            raise CapitalLedgerError(
                f"Capital ledger requires EUR wallet impact; got {fill.wallet_currency!r}"
            )

        if fill.wallet_net_value is None:
            raise CapitalLedgerError(f"Missing wallet netValue for fill {fill.fill_id}")

        wallet_value = _money(abs(fill.wallet_net_value))

        if wallet_value < 0:
            raise CapitalLedgerError("wallet net value cannot be negative")

        cash_delta = -wallet_value if side == "BUY" else wallet_value

        fee_cost = Decimal("0.00")
        fee_rows = []

        for fee in fill.fees:
            if fee.currency != "EUR":
                raise CapitalLedgerError(
                    f"Capital ledger requires EUR fee currency; got {fee.currency!r}"
                )

            raw_amount = Decimal(str(fee.amount))
            economic_cost = max(Decimal(0), -raw_amount)
            fee_cost += economic_cost

            fee_rows.append(
                {
                    "name": fee.name,
                    "currency": fee.currency,
                    "raw_amount": float(raw_amount),
                    "economic_cost_eur": _float_money(economic_cost),
                    "charged_at": fee.charged_at,
                }
            )

        fee_cost = fee_cost.quantize(CENT, rounding=ROUND_HALF_UP)

        if fill.fx_rate is not None and fill.fx_rate <= 0:
            raise CapitalLedgerError(f"Invalid fxRate for fill {fill.fill_id}: {fill.fx_rate}")

        payload_obj = {
            "schema": "t212_fill_wallet_impact_v1",
            "broker_order_id": order.broker_order_id,
            "fill_id": fill.fill_id,
            "ticker": order.ticker,
            "side": side,
            "filled_at": fill.filled_at,
            "quantity": fill.quantity,
            "price": fill.price,
            "wallet_currency": fill.wallet_currency,
            "wallet_net_value_raw": fill.wallet_net_value,
            "wallet_net_value_eur_abs": _float_money(wallet_value),
            "cash_delta_eur": _float_money(cash_delta),
            "fx_rate": fill.fx_rate,
            "fees": fee_rows,
            "fee_eur": _float_money(fee_cost),
            "fee_included_in_wallet_net_value": True,
            "realized_fx_pnl_inferred": False,
        }

        events.append(
            NormalizedFillEvent(
                event_key=(f"t212:fill:{order.broker_order_id}:{fill.fill_id}"),
                broker_order_id=order.broker_order_id,
                fill_id=fill.fill_id,
                ticker=order.ticker,
                side=side,
                filled_at=fill.filled_at,
                quantity=float(fill.quantity),
                price=float(fill.price),
                cash_delta_eur=_float_money(cash_delta),
                fee_eur=_float_money(fee_cost),
                fx_rate=fill.fx_rate,
                wallet_net_value_eur=_float_money(wallet_value),
                payload=_canonical_json(payload_obj),
            )
        )

    return events


def _same_optional_float(
    left: float | None,
    right: float | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return abs(float(left) - float(right)) <= TOL


def _load_machine_state(
    con: sqlite3.Connection,
) -> sqlite3.Row:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT
                revision,
                strategy_cash_eur,
                external_cash_debt_eur,
                realized_fees_eur,
                realized_fx_eur
            FROM machine_state
            WHERE id=1
            """
        ).fetchone()
    finally:
        con.row_factory = old_factory

    if row is None:
        raise CapitalLedgerError("machine_state singleton missing")

    return row


def _existing_event(
    con: sqlite3.Connection,
    event_key: str,
) -> sqlite3.Row | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            """
            SELECT
                event_key,
                decision_id,
                broker_order_id,
                fill_id,
                event_type,
                cash_delta_eur,
                fee_eur,
                fx_rate,
                payload
            FROM capital_ledger
            WHERE event_key=?
            """,
            (event_key,),
        ).fetchone()
    finally:
        con.row_factory = old_factory


def _assert_existing_matches(
    row: sqlite3.Row,
    *,
    event: NormalizedFillEvent,
    decision_id: str | None,
) -> None:
    expected_type = f"BROKER_FILL_{event.side}"

    checks = {
        "event_key": event.event_key,
        "decision_id": decision_id,
        "broker_order_id": event.broker_order_id,
        "fill_id": event.fill_id,
        "event_type": expected_type,
        "payload": event.payload,
    }

    for key, expected in checks.items():
        if row[key] != expected:
            raise CapitalLedgerError(
                f"Conflicting replay for {event.event_key}: "
                f"{key}={row[key]!r} expected={expected!r}"
            )

    if abs(float(row["cash_delta_eur"]) - event.cash_delta_eur) > TOL:
        raise CapitalLedgerError(f"Conflicting cash delta replay for {event.event_key}")

    if abs(float(row["fee_eur"]) - event.fee_eur) > TOL:
        raise CapitalLedgerError(f"Conflicting fee replay for {event.event_key}")

    if not _same_optional_float(row["fx_rate"], event.fx_rate):
        raise CapitalLedgerError(f"Conflicting fxRate replay for {event.event_key}")


def apply_fill_event(
    con: sqlite3.Connection,
    event: NormalizedFillEvent,
    *,
    decision_id: str | None = None,
    created_at: str | None = None,
) -> AppliedFillResult:
    if created_at is None:
        created_at = event.filled_at

    if con.in_transaction:
        raise CapitalLedgerError(
            "apply_fill_event requires a connection outside an active transaction"
        )

    con.execute("BEGIN IMMEDIATE")

    try:
        existing = _existing_event(con, event.event_key)

        state = _load_machine_state(con)

        old_revision = int(state["revision"])
        old_cash = _money(float(state["strategy_cash_eur"]))
        old_debt = _money(float(state["external_cash_debt_eur"]))
        old_fees = _money(float(state["realized_fees_eur"]))

        if existing is not None:
            _assert_existing_matches(
                existing,
                event=event,
                decision_id=decision_id,
            )

            con.commit()

            return AppliedFillResult(
                event_key=event.event_key,
                status="ALREADY_APPLIED",
                old_revision=old_revision,
                new_revision=old_revision,
                old_strategy_cash_eur=_float_money(old_cash),
                new_strategy_cash_eur=_float_money(old_cash),
                old_external_cash_debt_eur=_float_money(old_debt),
                new_external_cash_debt_eur=_float_money(old_debt),
                external_debt_change_eur=0.0,
                old_realized_fees_eur=_float_money(old_fees),
                new_realized_fees_eur=_float_money(old_fees),
            )

        cash_delta = _money(event.cash_delta_eur)
        fee_cost = _money(event.fee_eur)

        new_cash = old_cash + cash_delta
        new_debt = max(Decimal("0.00"), -new_cash)
        new_fees = old_fees + fee_cost
        new_revision = old_revision + 1

        con.execute(
            """
            INSERT INTO capital_ledger(
                event_key,
                decision_id,
                broker_order_id,
                fill_id,
                event_type,
                cash_delta_eur,
                fee_eur,
                fx_rate,
                payload,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.event_key,
                decision_id,
                event.broker_order_id,
                event.fill_id,
                f"BROKER_FILL_{event.side}",
                _float_money(cash_delta),
                _float_money(fee_cost),
                event.fx_rate,
                event.payload,
                created_at,
            ),
        )

        con.execute(
            """
            UPDATE machine_state
            SET
                revision=?,
                strategy_cash_eur=?,
                external_cash_debt_eur=?,
                realized_fees_eur=?,
                updated_at=?
            WHERE id=1
            """,
            (
                new_revision,
                _float_money(new_cash),
                _float_money(new_debt),
                _float_money(new_fees),
                created_at,
            ),
        )

        con.commit()

        return AppliedFillResult(
            event_key=event.event_key,
            status="APPLIED",
            old_revision=old_revision,
            new_revision=new_revision,
            old_strategy_cash_eur=_float_money(old_cash),
            new_strategy_cash_eur=_float_money(new_cash),
            old_external_cash_debt_eur=_float_money(old_debt),
            new_external_cash_debt_eur=_float_money(new_debt),
            external_debt_change_eur=_float_money(new_debt - old_debt),
            old_realized_fees_eur=_float_money(old_fees),
            new_realized_fees_eur=_float_money(new_fees),
        )

    except Exception:
        con.rollback()
        raise


def apply_order_fills(
    con: sqlite3.Connection,
    order: CanonicalHistoricalOrder,
    *,
    decision_id: str | None = None,
) -> list[AppliedFillResult]:
    results = []

    for event in normalize_order_fills(order):
        results.append(
            apply_fill_event(
                con,
                event,
                decision_id=decision_id,
                created_at=event.filled_at,
            )
        )

    return results
