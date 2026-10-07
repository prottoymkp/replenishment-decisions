"""Explainable CDC-to-store transfers and cash-constrained supplier buying."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from src.config import VERSION, Scenario
from src.data_io import fingerprint
from src.forecast import Forecaster, estimate_history
from src.sql_staging import stage_position


def _as_date(value: object) -> date:
    return pd.Timestamp(value).date()


def _cents(value: object) -> int:
    cents = round(float(value) * 100)
    if abs(float(value) * 100 - cents) > 1e-6:
        raise ValueError("Unit costs require at most two decimal places")
    return int(cents)


def valid_purchase(quantity: int, moq: int, pack: int) -> bool:
    return quantity == 0 or (quantity >= moq and quantity % pack == 0)


def round_purchase(quantity: float, moq: int, pack: int) -> int:
    if quantity <= 0:
        return 0
    return int(math.ceil(max(quantity, moq) / pack) * pack)


@dataclass
class DecisionResult:
    asof: date
    run_id: str
    scenario: Scenario
    input_fingerprint: str
    buys: pd.DataFrame
    transfers: pd.DataFrame
    review: pd.DataFrame
    position: pd.DataFrame
    forecast_scores: pd.DataFrame
    timeline: pd.DataFrame

    @property
    def spend_cents(self) -> int:
        return int(self.buys["cash_cents"].sum()) if len(self.buys) else 0

    @property
    def transfer_cost_cents(self) -> int:
        return int(self.transfers["recommended_qty"].sum()) * self.scenario.transfer_cost_cents_per_unit


def prepare_forecaster(data: dict[str, pd.DataFrame]) -> Forecaster:
    """Fit model selection on middle six months, retaining last six for simulation."""
    history = estimate_history(data["fact_sales"])
    first = min(history["date"])
    last = max(history["date"])
    if (last - first).days < 690:
        raise ValueError("Portfolio v1 requires at least 23 months of daily sales history")
    forecaster = Forecaster(history)
    forecaster.fit(first + timedelta(days=364), first + timedelta(days=546))
    return forecaster


def _open_purchase_supply(data: dict[str, pd.DataFrame], asof: date) -> dict[str, list[tuple[date, int]]]:
    received = (
        data["fact_movements"]
        .loc[
            (data["fact_movements"]["movement_type"] == "SUPPLIER_RECEIPT")
            & (data["fact_movements"]["date"] <= asof)
        ]
        .groupby("reference_id")["delta_units"]
        .sum()
        .to_dict()
    )
    result: dict[str, list[tuple[date, int]]] = {}
    for po in data["fact_open_purchase_orders"].itertuples(index=False):
        if _as_date(po.order_date) > asof:
            continue
        cancelled = (
            int(po.qty_cancelled)
            if pd.notna(po.cancellation_date) and _as_date(po.cancellation_date) <= asof
            else 0
        )
        remaining = int(po.qty_ordered) - cancelled - int(received.get(po.po_id, 0))
        if remaining > 0:
            due = max(asof + timedelta(days=1), _as_date(po.expected_delivery_date))
            result.setdefault(po.sku_id, []).append((due, remaining))
    return result


def _inbound_transfers(
    data: dict[str, pd.DataFrame], asof: date
) -> dict[tuple[str, str], list[tuple[date, int]]]:
    received = (
        data["fact_movements"]
        .loc[
            (data["fact_movements"]["movement_type"] == "TRANSFER_RECEIPT")
            & (data["fact_movements"]["date"] <= asof)
        ]
        .groupby("reference_id")["delta_units"]
        .sum()
        .to_dict()
    )
    result: dict[tuple[str, str], list[tuple[date, int]]] = {}
    for transfer in data["fact_transfers"].itertuples(index=False):
        if _as_date(transfer.dispatch_date) > asof:
            continue
        remaining = int(transfer.qty_dispatched) - int(received.get(transfer.transfer_id, 0))
        if remaining > 0:
            due = max(asof + timedelta(days=1), _as_date(transfer.expected_arrival_date))
            result.setdefault((transfer.sku_id, transfer.destination_loc), []).append((due, remaining))
    return result


def _first_shortage(
    free: float, forecast: np.ndarray, inbound: list[tuple[date, int]], asof: date, starting_day: int = 1
) -> int:
    current = free
    due = {(_as_date(day) - asof).days: qty for day, qty in inbound}
    for day, demand in enumerate(forecast, start=1):
        current += due.get(day, 0) - float(demand)
        if day >= starting_day and current < 0:
            return day
    return len(forecast) + 1


def decide(
    data: dict[str, pd.DataFrame],
    scenario: Scenario = Scenario(),
    asof: date | None = None,
    forecaster: Forecaster | None = None,
) -> DecisionResult:
    """Return draft actions; money is computed in integer USD cents."""
    asof = asof or max(data["fact_inventory_snapshot"]["date"])
    asof = _as_date(asof)
    forecaster = forecaster or prepare_forecaster(data)
    position = stage_position(data, asof)
    if position.empty:
        raise ValueError(f"No inventory snapshot on {asof}")
    pos = {(row.sku_id, row.location_id): row for row in position.itertuples(index=False)}
    locations = data["dim_locations"]
    cdc = str(locations.loc[locations["location_type"] == "CDC", "location_id"].iloc[0])
    stores = sorted(locations.loc[locations["location_type"] == "Store", "location_id"].astype(str))
    open_pos = _open_purchase_supply(data, asof)
    transit = _inbound_transfers(data, asof)
    max_days = 180
    z = NormalDist().inv_cdf(scenario.service_level)
    transfer_rows: list[dict[str, object]] = []
    buy_candidates: list[dict[str, object]] = []
    review_rows: list[dict[str, object]] = []
    timeline_rows: list[dict[str, object]] = []
    for item in data["dim_items"].itertuples(index=False):
        sku = item.sku_id
        weak_locations = [loc for loc in stores if forecaster.evidence(sku, loc, asof) < 30]
        if weak_locations:
            for loc in weak_locations:
                review_rows.append(
                    dict(
                        sku_id=sku,
                        location=loc,
                        action="REVIEW",
                        status="REVIEW_REQUIRED",
                        reason="Fewer than 30 usable demand observations in the last 90 days",
                    )
                )
            continue
        unit_cents = _cents(item.unit_cost_usd)
        lead = max(1, int(item.supplier_lead_time_days) + scenario.lead_buffer_days)
        arrival_day = lead + scenario.transfer_lead_days
        store_forecasts = {loc: forecaster.predict(sku, loc, asof, max_days) for loc in stores}
        combined = sum(store_forecasts.values(), np.zeros(max_days))
        cdc_row = pos.get((sku, cdc))
        if cdc_row is None:
            raise ValueError(f"Missing CDC inventory for {sku}")
        cdc_free = max(0, int(cdc_row.on_hand_units) - int(cdc_row.allocated_units))
        for loc in stores:
            stock = pos.get((sku, loc))
            if stock is None:
                raise ValueError(f"Missing store inventory for {sku} at {loc}")
            forecast = store_forecasts[loc]
            free = max(0, int(stock.on_hand_units) - int(stock.allocated_units))
            existing = transit.get((sku, loc), [])
            first_short = _first_shortage(free, forecast[:45], existing, asof)
            target_horizon = scenario.transfer_lead_days + scenario.store_target_days
            incoming = sum(q for day, q in existing if (_as_date(day) - asof).days <= target_horizon)
            safety = z * forecaster.sigma.get(sku, 0.25) * math.sqrt(target_horizon)
            target = math.ceil(float(forecast[:target_horizon].sum()) + safety)
            need = max(0, target - free - incoming)
            timeline_stock = free
            due = {(_as_date(day) - asof).days: q for day, q in existing}
            for day_no in range(1, 46):
                timeline_stock += due.get(day_no, 0) - float(forecast[day_no - 1])
                if day_no in (1, 7, 14, 30, 45):
                    timeline_rows.append(
                        dict(
                            sku_id=sku,
                            location=loc,
                            day=day_no,
                            date=asof + timedelta(days=day_no),
                            projected_units=round(timeline_stock, 1),
                        )
                    )
            transfer_rows.append(
                dict(
                    sku_id=sku,
                    style_color_size=f"{item.style} / {item.color} / {item.size}",
                    source=cdc,
                    destination=loc,
                    current_on_hand=int(stock.on_hand_units),
                    inbound_on_order=sum(q for _, q in existing),
                    days_of_supply=round(free / max(0.1, float(np.mean(forecast[:30]))), 1),
                    unconstrained_qty=need,
                    recommended_qty=0,
                    expected_arrival=asof + timedelta(days=scenario.transfer_lead_days),
                    first_shortage_day=first_short,
                    unit_cost_usd=float(item.unit_cost_usd),
                    status="NO_TRANSFER_NEEDED" if need == 0 else "PENDING",
                    reason="Store stock covers transfer horizon"
                    if need == 0
                    else "CDC supply required to protect store coverage",
                )
            )
        # Treat transfers as internal movement: stock is subtracted at the CDC only when assigned below.
        free_network = cdc_free + sum(
            max(0, int(pos[(sku, loc)].on_hand_units) - int(pos[(sku, loc)].allocated_units))
            for loc in stores
        )
        lead_supply = sum(q for due, q in open_pos.get(sku, []) if (due - asof).days <= lead)
        target_horizon = min(max_days, lead + scenario.target_days)
        target_supply = sum(q for due, q in open_pos.get(sku, []) if (due - asof).days <= target_horizon)
        sigma = forecaster.sigma.get(sku, 0.25)
        reorder_point = float(combined[:lead].sum()) + z * sigma * math.sqrt(lead)
        target = float(combined[:target_horizon].sum()) + z * sigma * math.sqrt(target_horizon)
        need = (
            max(0, math.ceil(target - free_network - target_supply))
            if free_network + lead_supply <= reorder_point
            else 0
        )
        rounded = round_purchase(need, int(item.moq_units), int(item.pack_multiple))
        if rounded == 0:
            continue
        first_recoverable = _first_shortage(free_network, combined, open_pos.get(sku, []), asof, arrival_day)
        first_shortage = _first_shortage(free_network, combined, open_pos.get(sku, []), asof)
        margin = max(0.0, float(item.wholesale_price_usd) - float(item.unit_cost_usd))
        avoidable_units = min(
            rounded, float(combined[arrival_day - 1 : min(max_days, arrival_day + 30)].sum())
        )
        margin_per_dollar = avoidable_units * margin / max(1, rounded * float(item.unit_cost_usd))
        daily_rate = max(0.1, float(combined[:30].mean()))
        buy_candidates.append(
            dict(
                sku_id=sku,
                style_color_size=f"{item.style} / {item.color} / {item.size}",
                supplier_id=item.supplier_id,
                location=cdc,
                current_on_hand=int(cdc_row.on_hand_units),
                inbound_on_order=sum(q for _, q in open_pos.get(sku, [])),
                days_of_supply=round(free_network / daily_rate, 1),
                reorder_point=round(reorder_point, 1),
                unconstrained_qty=need,
                moq_adjusted_qty=rounded,
                recommended_qty=0,
                moq_units=int(item.moq_units),
                pack_multiple=int(item.pack_multiple),
                unit_cost_usd=float(item.unit_cost_usd),
                unit_cost_cents=unit_cents,
                cash_cents=0,
                expected_arrival=asof + timedelta(days=lead),
                first_recoverable_shortage_day=first_recoverable,
                first_shortage_day=first_shortage,
                margin_per_dollar=margin_per_dollar,
                status="PENDING",
                reason="",
            )
        )
    # Allocate physical CDC stock to the earliest store shortage first.
    by_sku = {}
    for row in transfer_rows:
        by_sku.setdefault(row["sku_id"], []).append(row)
    for sku, rows in by_sku.items():
        available = max(0, int(pos[(sku, cdc)].on_hand_units) - int(pos[(sku, cdc)].allocated_units))
        for row in sorted(rows, key=lambda r: (r["first_shortage_day"], r["destination"])):
            if row["unconstrained_qty"] <= 0:
                continue
            quantity = min(int(row["unconstrained_qty"]), available)
            row["recommended_qty"] = quantity
            row["status"] = "RECOMMENDED" if quantity == row["unconstrained_qty"] else "PARTIAL_SOURCE_LIMIT"
            row["reason"] = (
                "Transfer before projected shortage"
                if quantity
                else "CDC stock unavailable; buyer review required"
            )
            available -= quantity
    if scenario.policy == "baseline":
        buy_candidates.sort(key=lambda r: (r["days_of_supply"], r["sku_id"]))
    else:
        buy_candidates.sort(
            key=lambda r: (r["first_recoverable_shortage_day"], -r["margin_per_dollar"], r["sku_id"])
        )
    remaining = scenario.budget_cents
    for row in buy_candidates:
        full = int(row["moq_adjusted_qty"])
        unit = int(row["unit_cost_cents"])
        affordable = min(full, remaining // unit // int(row["pack_multiple"]) * int(row["pack_multiple"]))
        minimum = round_purchase(1, int(row["moq_units"]), int(row["pack_multiple"]))
        approved = affordable if affordable >= minimum else 0
        assert valid_purchase(approved, int(row["moq_units"]), int(row["pack_multiple"]))
        row["recommended_qty"] = approved
        row["cash_cents"] = approved * unit
        remaining -= approved * unit
        codes = []
        if full > row["unconstrained_qty"]:
            codes.append("ADJUSTED_MOQ_PACK")
        if approved == 0:
            codes.append("DEFERRED_CASH_LIMIT")
            row["status"] = "DEFERRED"
        elif approved < full:
            codes.append("PARTIAL_CASH_LIMIT")
            row["status"] = "PARTIAL"
        else:
            row["status"] = "RECOMMENDED"
        if row["first_shortage_day"] < (row["expected_arrival"] - asof).days:
            codes.append("UNAVOIDABLE_BEFORE_ARRIVAL")
        row["reason"] = ", ".join(codes) or "Funded purchase covers projected shortage"
    buys = pd.DataFrame(buy_candidates)
    transfers = pd.DataFrame(transfer_rows)
    review = pd.DataFrame(review_rows, columns=["sku_id", "location", "action", "status", "reason"])
    source_root = Path(__file__).resolve().parents[1]
    source_bytes = b"".join(
        (source_root / name).read_bytes()
        for name in (
            "src/config.py",
            "src/forecast.py",
            "src/replenishment_engine.py",
            "src/sql_staging.py",
            "sql/staging.sql",
        )
    )
    implementation_digest = hashlib.sha256(source_bytes).hexdigest()[:12]
    digest = hashlib.sha256(
        (fingerprint(data) + str(asof) + repr(asdict(scenario)) + VERSION + implementation_digest).encode()
    ).hexdigest()[:12]
    return DecisionResult(
        asof,
        digest,
        scenario,
        fingerprint(data),
        buys,
        transfers,
        review,
        position,
        forecaster.scores,
        pd.DataFrame(timeline_rows),
    )
