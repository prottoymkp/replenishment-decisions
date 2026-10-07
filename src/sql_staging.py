"""DuckDB staging and inventory-ledger reconciliation."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "staging.sql"


def stage_position(data: dict[str, pd.DataFrame], asof: object) -> pd.DataFrame:
    """Compute on-hand + open supply - allocated, with as-of receipts."""
    with duckdb.connect(":memory:") as con:
        for name, frame in data.items():
            if name.startswith("dim_") or name.startswith("fact_"):
                con.register(name, frame)
        return con.execute(SQL_PATH.read_text(encoding="utf-8"), [asof] * 6).df()


def reconcile_ledger(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Find days whose snapshots disagree with receipts, transfers, and sales."""
    with duckdb.connect(":memory:") as con:
        for name in ("fact_inventory_snapshot", "fact_movements", "fact_sales"):
            con.register(name, data[name])
        return con.execute("""
            WITH moves AS (
                SELECT date, sku_id, location_id, SUM(delta_units) delta
                FROM fact_movements GROUP BY 1,2,3
            ), sales AS (
                SELECT date, sku_id, location_id, SUM(units_sold) sold
                FROM fact_sales GROUP BY 1,2,3
            ), snapshots AS (
                SELECT date, sku_id, location_id, on_hand_units,
                       LAG(on_hand_units) OVER (PARTITION BY sku_id, location_id ORDER BY date) previous_on_hand
                FROM fact_inventory_snapshot
            )
            SELECT s.date, s.sku_id, s.location_id, s.previous_on_hand, s.on_hand_units,
                   COALESCE(m.delta, 0) delta_units, COALESCE(v.sold, 0) units_sold
            FROM snapshots s
            LEFT JOIN moves m USING (date, sku_id, location_id)
            LEFT JOIN sales v USING (date, sku_id, location_id)
            WHERE s.previous_on_hand IS NOT NULL
              AND s.on_hand_units <> s.previous_on_hand + COALESCE(m.delta, 0) - COALESCE(v.sold, 0)
            ORDER BY s.date, s.sku_id, s.location_id
        """).df()
