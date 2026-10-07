"""Six-month policy replay with latent demand used only for outcome scoring."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from statistics import NormalDist

import pandas as pd

from src.config import Scenario
from src.forecast import Forecaster
from src.replenishment_engine import decide


class _FrozenForecaster:
    """Prevent the simulator from learning demand realized after its start."""

    def __init__(self, model: Forecaster, cutoff: date):
        self.model = model
        self.cutoff = cutoff
        self.sigma = model.sigma
        self.scores = model.scores

    def predict(self, sku: str, loc: str, asof: date, horizon: int):
        return self.model.predict(sku, loc, self.cutoff, horizon)

    def evidence(self, sku: str, loc: str, asof: date) -> int:
        return self.model.evidence(sku, loc, self.cutoff)


def replay(
    data: dict[str, pd.DataFrame],
    truth: pd.DataFrame,
    forecaster: Forecaster,
    scenario: Scenario,
    cutoff: date,
) -> dict[str, float | int | str]:
    """Replay policy daily; both policies receive identical exogenous demand and lead rules."""
    cutoff = pd.Timestamp(cutoff).date()
    last_day = max(data["fact_inventory_snapshot"]["date"])
    items = data["dim_items"].set_index("sku_id")
    sku_ids = sorted(items.index)
    locations = data["dim_locations"]
    cdc = str(locations.loc[locations["location_type"] == "CDC", "location_id"].iloc[0])
    stores = sorted(locations.loc[locations["location_type"] == "Store", "location_id"])
    opening = data["fact_inventory_snapshot"]
    opening = opening[opening["date"] == cutoff]
    if len(opening) != len(sku_ids) * (len(stores) + 1):
        raise ValueError("Complete opening inventory is required for the replay")
    stock = {(r.sku_id, r.location_id): int(r.on_hand_units) for r in opening.itertuples(index=False)}
    allocated = {(r.sku_id, r.location_id): int(r.allocated_units) for r in opening.itertuples(index=False)}
    truth_lookup = {
        (r.date, r.sku_id, r.location_id): int(r.true_demand_units) for r in truth.itertuples(index=False)
    }
    frozen = _FrozenForecaster(forecaster, cutoff)
    # Historical orders dispatched at cutoff are common to both policies. Future original-policy orders are excluded.
    historical_pos = data["fact_open_purchase_orders"]
    historical_pos = historical_pos[historical_pos["order_date"] <= cutoff].copy()
    historical_transfers = data["fact_transfers"]
    historical_transfers = historical_transfers[historical_transfers["dispatch_date"] <= cutoff].copy()
    past_movements = data["fact_movements"]
    actual_future = past_movements[past_movements["date"] > cutoff]
    known_po_ids = set(historical_pos["po_id"])
    known_transfer_ids = set(historical_transfers["transfer_id"])
    receipt_events: dict[date, list[tuple[str, str, int, str]]] = defaultdict(list)
    for movement in actual_future.itertuples(index=False):
        if movement.movement_type == "SUPPLIER_RECEIPT" and movement.reference_id in known_po_ids:
            due = movement.date + timedelta(days=max(0, scenario.lead_buffer_days))
            receipt_events[due].append(
                (movement.sku_id, cdc, int(movement.delta_units), movement.reference_id)
            )
        elif movement.movement_type == "TRANSFER_RECEIPT" and movement.reference_id in known_transfer_ids:
            receipt_events[movement.date].append(
                (movement.sku_id, movement.location_id, int(movement.delta_units), movement.reference_id)
            )
    purchase_rows = historical_pos.to_dict("records")
    transfer_rows = historical_transfers.to_dict("records")
    movement_rows = past_movements[past_movements["date"] <= cutoff].to_dict("records")
    pending_transfer: dict[str, tuple[str, str, int]] = {}
    for transfer in historical_transfers.itertuples(index=False):
        if any(r[3] == transfer.transfer_id for events in receipt_events.values() for r in events):
            pending_transfer[transfer.transfer_id] = (
                transfer.sku_id,
                transfer.destination_loc,
                int(transfer.qty_dispatched),
            )
    demand_total = fulfilled = lost = stockout_days = purchase_cents = transfer_operating_cents = 0
    inventory_values = []
    monthly_spend: dict[str, int] = defaultdict(int)
    day = cutoff + timedelta(days=1)
    while day <= last_day:
        for sku, loc, qty, reference in receipt_events.pop(day, []):
            stock[(sku, loc)] += qty
            movement_rows.append(
                dict(
                    date=day,
                    sku_id=sku,
                    location_id=loc,
                    movement_type="TRANSFER_RECEIPT"
                    if reference.startswith("HTR-") or reference.startswith("SIM-T")
                    else "SUPPLIER_RECEIPT",
                    delta_units=qty,
                    reference_id=reference,
                )
            )
            pending_transfer.pop(reference, None)
        if day.day == 1:
            # The decision snapshot is the balance before today's demand.
            latest = pd.DataFrame(
                [
                    dict(
                        date=day,
                        sku_id=sku,
                        location_id=loc,
                        on_hand_units=stock[(sku, loc)],
                        allocated_units=allocated[(sku, loc)],
                    )
                    for sku in sku_ids
                    for loc in [cdc, *stores]
                ]
            )
            current_data = dict(data)
            current_data["fact_inventory_snapshot"] = latest
            current_data["fact_open_purchase_orders"] = pd.DataFrame(purchase_rows)
            current_data["fact_transfers"] = pd.DataFrame(transfer_rows)
            current_data["fact_movements"] = pd.DataFrame(movement_rows)
            result = decide(current_data, scenario, day, frozen)
            for order in result.buys.itertuples(index=False):
                qty = int(order.recommended_qty)
                if not qty:
                    continue
                reference = f"SIM-P-{scenario.policy}-{day}-{order.sku_id}"
                lead = max(
                    1, int(items.loc[order.sku_id, "supplier_lead_time_days"]) + scenario.lead_buffer_days
                )
                expected = day + timedelta(days=lead)
                delay = 12 if (int(order.sku_id[-3:]) + day.toordinal()) % 7 == 0 else 0
                receipt_events[expected + timedelta(days=delay)].append((order.sku_id, cdc, qty, reference))
                purchase_rows.append(
                    dict(
                        po_id=reference,
                        sku_id=order.sku_id,
                        destination_loc=cdc,
                        order_date=day,
                        expected_delivery_date=expected,
                        qty_ordered=qty,
                        qty_cancelled=0,
                        cancellation_date=pd.NaT,
                    )
                )
                purchase_cents += qty * int(order.unit_cost_cents)
                monthly_spend[day.strftime("%Y-%m")] += qty * int(order.unit_cost_cents)
        if (day - cutoff).days % 7 == 1:
            # A weekly, common transfer policy keeps stores supplied between buying reviews.
            for sku in sku_ids:
                needs = []
                for loc in stores:
                    forecast = frozen.predict(sku, loc, day, 9)
                    incoming = sum(
                        q
                        for _, (pending_sku, pending_loc, q) in pending_transfer.items()
                        if pending_sku == sku and pending_loc == loc
                    )
                    free = max(0, stock[(sku, loc)] - allocated[(sku, loc)])
                    target = (
                        float(forecast.sum())
                        + NormalDist().inv_cdf(scenario.service_level) * forecaster.sigma.get(sku, 0.25) * 3
                    )
                    need = max(0, int(round(target)) - free - incoming)
                    days_supply = free / max(0.1, float(forecast.mean()))
                    needs.append((days_supply, loc, need))
                for _, loc, need in sorted(needs):
                    available = max(0, stock[(sku, cdc)] - allocated[(sku, cdc)])
                    qty = min(need, available)
                    if not qty:
                        continue
                    reference = f"SIM-T-{scenario.policy}-{day}-{sku}-{loc}"
                    stock[(sku, cdc)] -= qty
                    transfer_operating_cents += qty * scenario.transfer_cost_cents_per_unit
                    movement_rows.append(
                        dict(
                            date=day,
                            sku_id=sku,
                            location_id=cdc,
                            movement_type="TRANSFER_DISPATCH",
                            delta_units=-qty,
                            reference_id=reference,
                        )
                    )
                    due = day + timedelta(days=scenario.transfer_lead_days)
                    receipt_events[due].append((sku, loc, qty, reference))
                    pending_transfer[reference] = (sku, loc, qty)
                    transfer_rows.append(
                        dict(
                            transfer_id=reference,
                            sku_id=sku,
                            source_loc=cdc,
                            destination_loc=loc,
                            dispatch_date=day,
                            expected_arrival_date=due,
                            qty_dispatched=qty,
                        )
                    )
        for sku in sku_ids:
            for loc in stores:
                demand = truth_lookup.get((day, sku, loc), 0)
                sold = min(demand, stock[(sku, loc)])
                stock[(sku, loc)] -= sold
                demand_total += demand
                fulfilled += sold
                lost += demand - sold
                stockout_days += int(demand > sold)
        value = 0.0
        for sku in sku_ids:
            cost = float(items.loc[sku, "unit_cost_usd"])
            owned_units = sum(stock[(sku, loc)] for loc in [cdc, *stores])
            owned_units += sum(q for _, (s, _, q) in pending_transfer.items() if s == sku)
            value += owned_units * cost
        inventory_values.append(value)
        day += timedelta(days=1)
    if any(spend > scenario.budget_cents for spend in monthly_spend.values()):
        raise AssertionError("Replay exceeded a monthly budget")
    return dict(
        policy=scenario.policy,
        budget_usd=scenario.budget_cents / 100,
        supplier_delay_days=scenario.lead_buffer_days,
        unit_fill_rate=round(fulfilled / max(1, demand_total), 4),
        demand_units=demand_total,
        fulfilled_units=fulfilled,
        lost_units=lost,
        stockout_sku_store_days=stockout_days,
        purchase_commitments_usd=round(purchase_cents / 100, 2),
        transfer_operating_cost_usd=round(transfer_operating_cents / 100, 2),
        average_inventory_value_usd=round(sum(inventory_values) / max(1, len(inventory_values)), 2),
        replay_days=len(inventory_values),
        max_monthly_spend_usd=max(monthly_spend.values(), default=0) / 100,
    )


def compare_presets(
    data: dict[str, pd.DataFrame], truth: pd.DataFrame, forecaster: Forecaster
) -> pd.DataFrame:
    """Equal-condition baseline comparisons for three documented scenarios."""
    first = min(data["fact_sales"]["date"])
    cutoff = first + timedelta(days=546)
    presets = [
        ("Base case", Scenario()),
        ("Tight budget", Scenario(budget_cents=2_500_000)),
        ("Supplier delay", Scenario(lead_buffer_days=20)),
    ]
    rows = []
    for name, settings in presets:
        for policy in ("baseline", "proposed"):
            row = replay(data, truth, forecaster, replace(settings, policy=policy), cutoff)
            rows.append(dict(scenario=name, **row))
    return pd.DataFrame(rows)
