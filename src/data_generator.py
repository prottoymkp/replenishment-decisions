"""Reproducible, movement-consistent synthetic footwear retailer."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

TABLES = (
    "dim_items",
    "dim_locations",
    "fact_sales",
    "fact_inventory_snapshot",
    "fact_open_purchase_orders",
    "fact_movements",
    "fact_transfers",
)


def generate(seed: int = 42) -> dict[str, pd.DataFrame]:
    """Return 730 daily dates; latent demand is returned solely for evaluation."""
    rng = np.random.default_rng(seed)
    start = date(2024, 10, 1)
    dates = [start + timedelta(days=i) for i in range(730)]
    styles = ["Stride", "Canvas", "Trail", "Court", "Flex", "Urban"]
    colors = ["Black", "White", "Navy", "Sand", "Grey", "Olive"]
    items = []
    for i, (style, size) in enumerate((s, z) for s in styles for z in (7, 8, 9, 10)):
        cost = round(12.0 + (i % 6) * 2.5 + (i % 4), 2)
        items.append(
            dict(
                sku_id=f"FW-{i + 1:03d}",
                style=style,
                color=colors[i // 4],
                size=str(size),
                category="Footwear",
                supplier_id=f"SUP-{i // 8 + 1}",
                unit_cost_usd=cost,
                wholesale_price_usd=round(cost * 1.85, 2),
                moq_units=48 if i % 3 else 60,
                pack_multiple=12,
                supplier_lead_time_days=65 + i % 6 * 5,
            )
        )
    locations = [dict(location_id="CDC", location_name="Central Distribution Center", location_type="CDC")]
    locations += [
        dict(location_id=f"STORE-{i}", location_name=f"Store {i}", location_type="Store") for i in range(1, 4)
    ]
    sku_ids = [x["sku_id"] for x in items]
    stores = [x["location_id"] for x in locations if x["location_type"] == "Store"]
    balance = {
        (sku, loc): (300 if loc == "CDC" else 30 + (int(sku[-3:]) % 4) * 5)
        for sku in sku_ids
        for loc in ["CDC", *stores]
    }
    supplier_due: dict[date, list[tuple[str, str, int]]] = defaultdict(list)
    transfer_due: dict[date, list[tuple[str, str, int]]] = defaultdict(list)
    sales, snapshots, pos, movements, transfers, truth = [], [], [], [], [], []
    for day_index, day in enumerate(dates):
        for po_id, sku, quantity in supplier_due.pop(day, []):
            balance[(sku, "CDC")] += quantity
            movements.append(
                dict(
                    date=day,
                    sku_id=sku,
                    location_id="CDC",
                    movement_type="SUPPLIER_RECEIPT",
                    delta_units=quantity,
                    reference_id=po_id,
                )
            )
        for transfer_id, sku, quantity in transfer_due.pop(day, []):
            dest = next(t["destination_loc"] for t in transfers if t["transfer_id"] == transfer_id)
            balance[(sku, dest)] += quantity
            movements.append(
                dict(
                    date=day,
                    sku_id=sku,
                    location_id=dest,
                    movement_type="TRANSFER_RECEIPT",
                    delta_units=quantity,
                    reference_id=transfer_id,
                )
            )
        if day_index % 60 == 0:
            for idx, item in enumerate(items):
                sku = item["sku_id"]
                po_id = f"HPO-{day_index:03d}-{idx:02d}"
                qty = 180 + (idx % 4) * 12
                expected = day + timedelta(days=item["supplier_lead_time_days"])
                delay = 12 if (idx + day_index // 60) % 7 == 0 else 0
                actual = expected + timedelta(days=delay)
                cancelled = 24 if idx == 5 and day_index == 360 else 0
                cancel_date = day + timedelta(days=10) if cancelled else pd.NaT
                pos.append(
                    dict(
                        po_id=po_id,
                        sku_id=sku,
                        destination_loc="CDC",
                        order_date=day,
                        expected_delivery_date=expected,
                        qty_ordered=qty,
                        qty_cancelled=cancelled,
                        cancellation_date=cancel_date,
                    )
                )
                received = qty - cancelled
                if actual in dates:
                    if idx % 8 == 0 and received >= 60:
                        supplier_due[actual].append((po_id, sku, received - 36))
                        later = actual + timedelta(days=7)
                        if later in dates:
                            supplier_due[later].append((po_id, sku, 36))
                    else:
                        supplier_due[actual].append((po_id, sku, received))
        if day_index == 650:
            # A recent CDC receipt creates a real transfer opportunity at the public review date.
            sku = "FW-013"
            po_id = "HPO-TRANSFER-CASE"
            due = day + timedelta(days=65)
            pos.append(
                dict(
                    po_id=po_id,
                    sku_id=sku,
                    destination_loc="CDC",
                    order_date=day,
                    expected_delivery_date=due,
                    qty_ordered=96,
                    qty_cancelled=0,
                    cancellation_date=pd.NaT,
                )
            )
            supplier_due[due].append((po_id, sku, 96))
        if day_index % 7 == 0:
            for sku in sku_ids:
                for loc in stores:
                    need = max(0, 55 - balance[(sku, loc)])
                    qty = min(need, balance[(sku, "CDC")])
                    if qty <= 0:
                        continue
                    transfer_id = f"HTR-{day_index:03d}-{sku}-{loc}"
                    balance[(sku, "CDC")] -= qty
                    due = day + timedelta(days=2)
                    transfers.append(
                        dict(
                            transfer_id=transfer_id,
                            sku_id=sku,
                            source_loc="CDC",
                            destination_loc=loc,
                            dispatch_date=day,
                            expected_arrival_date=due,
                            qty_dispatched=qty,
                        )
                    )
                    movements.append(
                        dict(
                            date=day,
                            sku_id=sku,
                            location_id="CDC",
                            movement_type="TRANSFER_DISPATCH",
                            delta_units=-qty,
                            reference_id=transfer_id,
                        )
                    )
                    if due in dates:
                        transfer_due[due].append((transfer_id, sku, qty))
        for idx, sku in enumerate(sku_ids):
            for store_index, loc in enumerate(stores):
                base = (0.35 if idx % 7 == 0 else 0.65 + (idx % 5) * 0.17) * (1 + store_index * 0.13)
                weekly = 1.26 if day.weekday() in (4, 5) else 1.0
                season = 1.36 if day.month in (11, 12, 4) else (0.82 if day.month in (6, 7) else 1.0)
                trend = 1 + day_index / 730 * 0.14
                demand = int(rng.poisson(base * weekly * season * trend))
                if idx % 7 == 0 and rng.random() < 0.42:
                    demand = 0
                # A documented localized interruption makes zero sales ambiguous.
                if idx == 0 and loc == "STORE-1" and 420 <= day_index < 428:
                    demand = max(demand, 4)
                on_hand = balance[(sku, loc)]
                sold = min(demand, on_hand)
                balance[(sku, loc)] -= sold
                sales.append(
                    dict(date=day, sku_id=sku, location_id=loc, units_sold=sold, stockout_flag=demand > sold)
                )
                truth.append(dict(date=day, sku_id=sku, location_id=loc, true_demand_units=demand))
        for sku in sku_ids:
            for loc in ["CDC", *stores]:
                snapshots.append(
                    dict(
                        date=day,
                        sku_id=sku,
                        location_id=loc,
                        on_hand_units=balance[(sku, loc)],
                        allocated_units=0,
                    )
                )
    tables = dict(
        dim_items=pd.DataFrame(items),
        dim_locations=pd.DataFrame(locations),
        fact_sales=pd.DataFrame(sales),
        fact_inventory_snapshot=pd.DataFrame(snapshots),
        fact_open_purchase_orders=pd.DataFrame(pos),
        fact_movements=pd.DataFrame(movements),
        fact_transfers=pd.DataFrame(transfers),
        truth_demand=pd.DataFrame(truth),
    )
    return tables


def write_csv(directory: str | Path, seed: int = 42) -> dict[str, pd.DataFrame]:
    """Write public input tables; write truth separately for policy evaluation only."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tables = generate(seed)
    for name, frame in tables.items():
        frame.to_csv(directory / f"{name}.csv", index=False)
    return tables


if __name__ == "__main__":
    write_csv(Path(__file__).resolve().parents[1] / "data" / "generated")
